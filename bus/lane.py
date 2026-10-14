"""GitHub-pulled work for remote lanes."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote

from . import change_class, hunt_plan, pr_repair, tickets
from .github import GitHub, GitHubError, token_from_env
from .labels import REMOTE_LANES

CLAIM_RE = re.compile(r"^<!-- loom:remote-claim v1 ([^ ]+) ([0-9]+) ([0-9.]+) -->$")
CLAIM_AGE = 2 * 3600
PR_AGE = 3 * 3600


def _names(issue: Dict[str, Any]) -> List[str]:
    return [str(x.get("name") if isinstance(x, dict) else x)
            for x in issue.get("labels") or []]


def _comments(gh: GitHub, repo: str, number: int) -> List[Dict[str, Any]]:
    return [c for c in gh.paginate("/repos/%s/issues/%d/comments" % (repo, number))
            if isinstance(c, dict)]


def _claims(comments: Sequence[Dict[str, Any]], lane: str) -> List[Tuple[int, float]]:
    out = []
    for index, comment in enumerate(comments):
        match = CLAIM_RE.fullmatch(str(comment.get("body") or "").strip())
        if match and match.group(1) == lane:
            try:
                out.append((index, float(match.group(3))))
            except ValueError:
                continue
    return out


def _hand_back(gh: GitHub, repo: str, issue: Dict[str, Any], lane: str,
               reason: str) -> None:
    number = int(issue["number"])
    labels = [x for x in _names(issue) if x not in ("loom:running", "lane:" + lane)]
    labels.append("loom:queued")
    gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number), {"labels": labels})
    gh.rest("POST", "/repos/%s/issues/%d/comments" % (repo, number),
            {"body": "[transient] %s" % reason})


def _pull_for(gh: GitHub, repo: str, branch: str) -> Optional[Dict[str, Any]]:
    """The pull request whose head is the ticket branch, open or closed, or
    None. One request: GitHub filters by head instead of listing every pull."""
    owner = repo.split("/", 1)[0]
    found = gh.paginate("/repos/%s/pulls?state=all&head=%s"
                        % (repo, quote("%s:%s" % (owner, branch), safe="")))
    return next((p for p in found if isinstance(p, dict)), None)


def _to_pr(gh: GitHub, repo: str, issue: Dict[str, Any], lane: str) -> None:
    labels = [x for x in _names(issue) if x not in ("loom:running", "loom:queued")]
    gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, int(issue["number"])),
            {"labels": labels + ["loom:pr"]})


def _recover_and_count(gh: GitHub, repos: Sequence[str], lane: str,
                       now: float) -> int:
    """Tickets the remote lane holds and still works on. A held ticket whose
    branch already has a pull request moves to loom:pr; one with no pull
    request goes back to the queue when its last heartbeat is older than
    CLAIM_AGE or its first claim older than PR_AGE."""
    active = 0
    for repo in repos:
        found = gh.paginate("/repos/%s/issues?state=open&labels=loom:running,lane:%s"
                            % (repo, quote(lane, safe="")))
        for issue in found:
            if not isinstance(issue, dict) or "pull_request" in issue:
                continue
            claims = [at for _, at in _claims(_comments(gh, repo, int(issue["number"])), lane)]
            if not claims:
                continue
            branch = tickets.branch_for(int(issue["number"]), str(issue.get("title") or ""))
            if _pull_for(gh, repo, branch) is not None:
                _to_pr(gh, repo, issue, lane)
                continue
            if now - max(claims) > CLAIM_AGE or now - min(claims) > PR_AGE:
                _hand_back(gh, repo, issue, lane,
                           "remote lane claim expired without a pull request")
                continue
            active += 1
    return active


def _candidates(gh: GitHub, repos: Sequence[str], lane: str) -> List[Tuple[str, Dict[str, Any]]]:
    """(repo, issue) for each public queued ticket assigned to the lane in one
    of its tiers, oldest first."""
    found: List[Tuple[str, str, Dict[str, Any]]] = []
    for repo in repos:
        if not tickets.REPO_RE.fullmatch(repo):
            raise ValueError("expected OWNER/NAME, got %r" % repo)
        info = gh.rest("GET", "/repos/%s" % repo).data
        if not isinstance(info, dict) or info.get("private") is not False:
            continue
        for item in gh.paginate("/repos/%s/issues?state=open&labels=loom:queued,lane:%s"
                                % (repo, quote(lane, safe=""))):
            if _takes(item, lane):
                found.append((str(item.get("created_at") or ""), repo, item))
    found.sort(key=lambda row: (row[0], row[1], int(row[2]["number"])))
    return [(repo, item) for _, repo, item in found]


def _takes(item: Any, lane: str) -> bool:
    if not isinstance(item, dict) or "pull_request" in item:
        return False
    names = _names(item)
    if "loom:queued" not in names or "lane:" + lane not in names:
        return False
    try:
        difficulty = tickets.parse(item.get("body"))[0].get("Difficulty", "")
    except ValueError:
        return False
    return difficulty in REMOTE_LANES[lane]["tiers"]


def _take(gh: GitHub, repo: str, issue: Dict[str, Any], lane: str,
          now: float) -> Dict[str, Any]:
    number = int(issue["number"])
    labels = [x for x in _names(issue) if x != "loom:queued"] + ["loom:running"]
    gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number), {"labels": labels})
    gh.rest("POST", "/repos/%s/issues/%d/comments" % (repo, number), {
        "body": "<!-- loom:remote-claim v1 %s %d %.3f -->" % (lane, number, now)})
    info = gh.rest("GET", "/repos/%s" % repo).data
    default = str((info.get("default_branch") if isinstance(info, dict) else "") or "main")
    return {"repo": repo, "number": number, "base": default,
            "brief": tickets.render_brief(repo, issue, lane)}


def claim(gh: GitHub, repos: Sequence[str], lane: str = "muse",
          now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Claim the oldest public queued issue assigned to a configured lane."""
    if lane not in REMOTE_LANES:
        raise ValueError("unknown remote lane: %s" % lane)
    now = time.time() if now is None else now
    if _recover_and_count(gh, repos, lane, now) >= REMOTE_LANES[lane]["slots"]:
        return None
    for repo, issue in _candidates(gh, repos, lane):
        recent = [at for _, at in _claims(_comments(gh, repo, int(issue["number"])), lane)
                  if now - at < CLAIM_AGE]
        if len(recent) >= REMOTE_LANES[lane]["slots"]:
            continue
        result = _take(gh, repo, issue, lane, now)
        print(json.dumps(result))
        return result
    return None


def heartbeat(gh: GitHub, repo: str, number: int, lane: str = "muse",
              now: Optional[float] = None) -> None:
    if lane not in REMOTE_LANES:
        raise ValueError("unknown remote lane: %s" % lane)
    now = time.time() if now is None else now
    issue = gh.rest("GET", "/repos/%s/issues/%d" % (repo, number)).data or {}
    if "loom:running" not in _names(issue) or "lane:" + lane not in _names(issue):
        raise ValueError("ticket is not held by this remote lane")
    gh.rest("POST", "/repos/%s/issues/%d/comments" % (repo, number), {
        "body": "<!-- loom:remote-claim v1 %s %d %.3f -->" % (lane, number, now)})


def lane_heartbeat(gh: GitHub, repo: str, lane: str = "muse",
                   now: Optional[float] = None) -> None:
    """Publish remote worker availability on the station's lane status issue."""
    if lane not in REMOTE_LANES:
        raise ValueError("unknown remote lane: %s" % lane)
    now = time.time() if now is None else now
    issues = gh.paginate("/repos/%s/issues?state=open&labels=loom:lanes" % repo)
    issue = next((x for x in issues if isinstance(x, dict) and "pull_request" not in x), None)
    if issue is None:
        issue = gh.rest("POST", "/repos/%s/issues" % repo, {
            "title": "Loom lane status", "labels": ["loom:lanes"],
            "body": "The station keeps lane status current here."}).data
    if not isinstance(issue, dict) or "number" not in issue:
        raise ValueError("could not locate the lane status issue")
    body = "<!-- loom:remote-heartbeat v1 %s %.3f -->" % (lane, now)
    # One comment per lane, edited in place: a heartbeat every tick must not
    # grow the status issue by a comment each time.
    mine = re.compile(r"^<!-- loom:remote-heartbeat v1 %s [0-9.]+ -->$" % re.escape(lane))
    for comment in _comments(gh, repo, int(issue["number"])):
        if mine.fullmatch(str(comment.get("body") or "").strip()) and comment.get("id"):
            gh.rest("PATCH", "/repos/%s/issues/comments/%d" % (repo, int(comment["id"])),
                    {"body": body})
            return
    gh.rest("POST", "/repos/%s/issues/%d/comments" % (repo, issue["number"]), {"body": body})


def _git(*args: str) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    if result.returncode:
        raise OSError("git %s failed: %s" % (args[0], result.stderr.strip()))
    return result.stdout.strip()


def _what(body: str) -> str:
    match = re.search(r"^## What\s*\n(.*?)(?=^## |\Z)", body, re.M | re.S)
    return match.group(1).strip() if match else ""


def pr_body(repo: str, issue: Dict[str, Any], kind: str, counts: Dict[str, int],
            lane: str = "muse") -> str:
    """The pull request body every lane writes (lanes/run_lane.sh pr_body,
    rendered by bus/pr_repair.py): the Intent section, Why, Summary,
    Acceptance criteria, Size, Out of scope, the Loom footer and a Refs line.
    Never a closing keyword: the ticket closes only once it is verified.
    ValueError when the body would fail the review bot's plan checks."""
    number = int(issue["number"])
    brief = tickets.render_brief(repo, issue, lane)
    model = os.environ.get("LOOM_%s_MODEL" % lane.upper().replace("-", "_"), lane)
    body = pr_repair._render_generated(brief, lane, model, tickets.ticket_name(repo, number))
    intent = change_class.render_intent(kind, change_class.behavior_lines(
        "", _what(str(issue.get("body") or ""))), counts)
    body = change_class.with_intent(body, intent)
    found = hunt_plan.problems(body)
    if found:
        raise ValueError("the pull request body would fail the review bot's plan checks: "
                         + "; ".join(found) + " (fix the ticket's brief sections)")
    return body


def open_pr(gh: GitHub, repo: str, number: int, base: str,
            title: Optional[str] = None) -> Dict[str, Any]:
    """Open a PR for the already-pushed loom ticket branch."""
    issue = gh.rest("GET", "/repos/%s/issues/%d" % (repo, number)).data or {}
    branch = _git("branch", "--show-current")
    if branch != tickets.branch_for(number, str(issue.get("title") or "")):
        raise ValueError("current branch does not match the ticket branch")
    files = _git("diff", "--name-only", "%s...HEAD" % base).splitlines()
    if not files:
        raise ValueError("branch has no changes against base")
    patterns_data = gh.rest("GET", "/repos/%s/contents/%s" %
                            (repo, quote(change_class.SENSITIVE_FILE, safe="/")), ok=(404,)).data
    patterns = []
    if isinstance(patterns_data, dict) and patterns_data.get("content"):
        import base64
        patterns = change_class.parse_patterns(base64.b64decode(patterns_data["content"]).decode("utf-8"))
    kind = change_class.classify(files, patterns)
    body = pr_body(repo, issue, kind, change_class.count_by_class(files, patterns))
    made = gh.rest("POST", "/repos/%s/pulls" % repo, {
        "title": title or str(issue.get("title") or ""), "head": branch,
        "base": base, "body": body}).data
    if not isinstance(made, dict) or "number" not in made:
        raise ValueError("GitHub did not return the created pull request")
    gh.rest("POST", "/repos/%s/issues/%d/labels" % (repo, made["number"]),
            {"labels": ["class:" + kind]})
    labels = [x for x in _names(issue) if x not in ("loom:running", "loom:queued")]
    labels.append("loom:pr")
    gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number), {"labels": labels})
    return made


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="python3 -m bus.lane")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("claim")
    p.add_argument("--lane", required=True)
    p.add_argument("--repos", default=os.environ.get("LOOM_REPOS", ""))
    p = sub.add_parser("heartbeat")
    p.add_argument("ticket", nargs="?", help="OWNER/NAME#N; omit to report lane availability")
    p.add_argument("--lane", required=True)
    p.add_argument("--repo", default=os.environ.get("LOOM_STATUS_REPO", ""))
    p.add_argument("--repos", default=os.environ.get("LOOM_REPOS", ""))
    p = sub.add_parser("pr")
    p.add_argument("ticket", help="OWNER/NAME#N")
    p.add_argument("--base", required=True)
    p.add_argument("--title")
    args = parser.parse_args(argv)
    try:
        gh = GitHub(token_from_env())
        if args.command == "claim":
            result = claim(gh, args.repos.replace(",", " ").split(), args.lane)
            return 0 if result else 1
        repo, _, raw = args.ticket.partition("#")
        if args.command == "heartbeat" and not args.ticket:
            status_repo = args.repo or (args.repos.replace(",", " ").split() or [""])[0]
            if not tickets.REPO_RE.fullmatch(status_repo):
                raise ValueError("heartbeat without a ticket requires --repo or LOOM_STATUS_REPO")
            lane_heartbeat(gh, status_repo, args.lane)
            return 0
        if not tickets.REPO_RE.fullmatch(repo) or not raw.isdigit():
            raise ValueError("expected OWNER/NAME#N")
        number = int(raw)
        if args.command == "heartbeat":
            heartbeat(gh, repo, number, args.lane)
        else:
            made = open_pr(gh, repo, number, args.base, args.title)
            print(made.get("html_url", ""))
        return 0
    except (GitHubError, OSError, ValueError) as err:
        print("error: %s" % err, file=__import__("sys").stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
