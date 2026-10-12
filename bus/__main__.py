"""python3 -m bus <command>"""
from __future__ import annotations

import argparse
import re
import sys
from typing import List, Optional

from . import journal, labels, lock
from .github import GitHub, GitHubError, token_from_env

REPO_RE = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")


def _repo(value: str) -> str:
    if not REPO_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("expected OWNER/NAME, got %r" % value)
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
    s = sub.add_parser("journal", help="post a journal or report comment")
    s.add_argument("target", type=_issue, metavar="OWNER/NAME#N")
    s.add_argument("--file", required=True)
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
    journal.post(gh, repo, int(num), text)
    print("posted to %s" % args.target)
    return 0


def main(argv: Optional[List[str]] = None, gh: Optional[GitHub] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if gh is None:
            gh = GitHub(token_from_env())
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
