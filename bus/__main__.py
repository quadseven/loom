"""python3 -m bus <command>"""
from __future__ import annotations

import argparse
import re
import sys
from typing import Callable, List, Optional

from . import app, brain, journal, labels, lock, station
from .github import GitHub, GitHubError, Transport, auth_from_env, openssl_sign

REPO_RE = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")


def _repo(value: str) -> str:
    if not REPO_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("expected OWNER/NAME, got %r" % value)
    return value


LOGIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")


def _login(value: str) -> str:
    if not LOGIN_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("expected a GitHub login, got %r" % value)
    return value


def _number(value: str) -> str:
    if not value.isdigit():
        raise argparse.ArgumentTypeError("expected a number, got %r" % value)
    return value


def _issue(value: str) -> str:
    repo, _, num = value.partition("#")
    if not REPO_RE.fullmatch(repo) or not num.isdigit():
        raise argparse.ArgumentTypeError("expected OWNER/NAME#N, got %r" % value)
    return value


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python3 -m bus")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("labels", help="create the loom labels on repos")
    s.add_argument("repos", nargs="+", type=_repo, metavar="OWNER/NAME")
    s = sub.add_parser("lock", help="take the brain tick lock")
    s.add_argument("repo", type=_repo, metavar="OWNER/NAME")
    s.add_argument("--holder", required=True)
    s.add_argument("--minutes", type=int, default=15)
    s = sub.add_parser("unlock", help="release the brain tick lock")
    s.add_argument("repo", type=_repo, metavar="OWNER/NAME")
    s.add_argument("--holder", required=True)
    s = sub.add_parser("journal", help="post a journal, report or reply comment; a file with no marker is a journal tick")
    s.add_argument("target", type=_issue, metavar="OWNER/NAME#N")
    s.add_argument("--file", required=True)
    s.add_argument("--holder", help="the brain posting a journal; adds its Tick line when missing")
    sub.add_parser("station", help="one station pass: claim briefed tickets, report outcomes")
    s = sub.add_parser("app", help="set up the Loom's own GitHub App")
    app_sub = s.add_subparsers(dest="app_command", required=True)
    a = app_sub.add_parser("manifest", help="print the App manifest and the page that creates it")
    a.add_argument("--owner", required=True, type=_login, help="the user or organisation login")
    a.add_argument("--org", action="store_true", help="the owner is an organisation")
    a.add_argument("--name", help="the App's name (default loom-OWNER)")
    a.add_argument("--form", action="store_true",
                   help="print an HTML page whose one button creates the App")
    app_sub.add_parser("token", help="print the installation token of the App the "
                       "LOOM_GITHUB_APP_* settings name, for gh and git (loom#1142)")
    app_sub.add_parser("login", help="print the \"<slug>[bot]\" login of the App the "
                       "LOOM_GITHUB_APP_* settings name, for the brain's journal")
    a = app_sub.add_parser("ids", help="list the App's installations and print its id settings")
    a.add_argument("--app-id", required=True, type=_number)
    return p


def _run(args: argparse.Namespace, gh: GitHub) -> int:
    if args.command == "labels":
        for repo in args.repos:
            changed = labels.ensure_labels(gh, repo)
            print("%s: %s" % (repo, ", ".join(changed) if changed else "no changes"))
        return 0
    if args.command == "lock":
        ok = lock.acquire(gh, args.repo, args.holder, args.minutes)
        print("lock acquired" if ok else "lock held by someone else")
        return 0 if ok else 1
    if args.command == "unlock":
        ok = lock.release(gh, args.repo, args.holder)
        print("lock released" if ok else "lock not released")
        return 0 if ok else 1
    repo, _, num = args.target.partition("#")
    with open(args.file, encoding="utf-8") as fh:
        text = fh.read()
    try:
        journal.post(gh, repo, int(num), text, args.holder, brain.trusted_authors())
    except journal.ReportTooSoon as held:
        # The goal is already reported: not a failed step, so exit 0.
        print("not posted: %s" % held)
        return 0
    print("posted to %s" % args.target)
    return 0


def _app(args: argparse.Namespace, transport: Optional[Transport],
         sign: Optional[Callable[[bytes, str], bytes]]) -> int:
    if args.app_command == "manifest":
        return app.print_manifest(args.owner, args.org, args.name, args.form, sys.stdout)
    try:
        if args.app_command == "token":
            return app.print_token(sys.stdout, transport=transport, sign=sign or openssl_sign)
        if args.app_command == "login":
            return app.print_login(sys.stdout, transport=transport, sign=sign or openssl_sign)
        return app.ids(args.app_id, transport=transport, sign=sign or openssl_sign)
    except GitHubError as err:
        print("error: %s" % err, file=sys.stderr)
        return 2


def main(argv: Optional[List[str]] = None, gh: Optional[GitHub] = None,
         app_transport: Optional[Transport] = None,
         app_sign: Optional[Callable[[bytes, str], bytes]] = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "app":
        # No personal token: the manifest needs none, and ids signs as the App.
        return _app(args, app_transport, app_sign)
    if args.command == "station":
        try:
            return station.run(station.Settings.from_env(), gh)
        except GitHubError as err:
            print("error: %s (status %s)" % (err, err.status), file=sys.stderr)
            return 2
        except (ValueError, OSError) as err:
            print("error: %s" % err, file=sys.stderr)
            return 2
    try:
        if gh is None:
            gh = GitHub(auth_from_env(), unsubscribe=True)
        try:
            return _run(args, gh)
        finally:
            print("requests: %d" % gh.requests, file=sys.stderr)
    except GitHubError as err:
        print("error: %s (status %s)" % (err, err.status), file=sys.stderr)
        return 2
    except (ValueError, OSError) as err:
        print("error: %s" % err, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
