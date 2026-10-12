"""Commands used by the Loom brain during a tick."""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote

from .github import GitHub, GitHubError, token_from_env
from .labels import LANES

STATES = ("queued", "briefed", "running", "pr", "failed", "shipped", "verified")
SECTIONS = ("## Why", "## What", "## Acceptance criteria", "## Size", "## Out of scope")
JOURNAL_MARKER = "<!-- loom:journal v1 -->"
LANES_MARKER = "<!-- loom:lanes v1 -->"


def _repo(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        raise ValueError("repo must be OWNER/NAME")
    return value


def _issues(gh: GitHub, repo: str) -> List[Dict[str, Any]]:
    data = gh.paginate("/repos/%s/issues?state=open" % repo)
    return [x for x in data if "pull_request" not in x]


def _labels(issue: Dict[str, Any]) -> List[str]:
    return [str(x.get("name", "")) for x in issue.get("labels", [])]


def _graphql_status(gh: GitHub, repo: str, prs: List[Dict[str, Any]],
                    goals: List[Dict[str, Any]]) -> Dict[str, Any]:
    pr_ids = [int(x["number"]) for x in prs]
    goal_ids = [int(x["number"]) for x in goals]
    # A single query fetches every PR's combined check rollup and review threads,
    # plus comments on the goal issues for journal timestamps.
    # GitHub GraphQL does not support a dynamic issue-number list as a top-level
    # node argument. Build aliases from the numbers while retaining one request.
    owner, name = repo.split("/", 1)
    fields = []
    for n in pr_ids:
        fields.append("p%d: pullRequest(number:%d) { commits(last:1) { nodes { commit { statusCheckRollup { state } } } } reviewThreads(first:100) { nodes { isResolved } } }" % (n, n))
    for n in goal_ids:
        fields.append("g%d: issue(number:%d) { comments(last:100) { nodes { body createdAt } } }" % (n, n))
    if not fields:
        return {"prs": {}, "goals": {}}
    q = "query { repository(owner:%s, name:%s) { %s } }" % (
        _gql(owner), _gql(name), " ".join(fields))
    result = gh.graphql(q) or {}
    repo_data = result.get("repository") or {}
    return {"prs": {n: repo_data.get("p%d" % n) for n in pr_ids},
            "goals": {n: repo_data.get("g%d" % n) for n in goal_ids}}


def _gql(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _age(issue: Dict[str, Any], now: datetime) -> int:
    created = datetime.strptime(issue["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return max(0, int((now - created).total_seconds() // 3600))


def _goal_lines(goals: List[Dict[str, Any]], data: Dict[str, Any]) -> List[str]:
    lines = ["### Goals"]
    for issue in goals:
        node = data["goals"].get(int(issue["number"])) or {}
        comments = (node.get("comments") or {}).get("nodes") or []
        latest = [c.get("createdAt", "") for c in comments
                  if JOURNAL_MARKER in c.get("body", "")]
        lines.append("- #%s %s - latest journal %s" % (
            issue["number"], issue.get("title", ""), max(latest) if latest else "none"))
    return lines


def _ticket_lines(tickets: List[Dict[str, Any]], now: datetime) -> List[str]:
    lines = ["### Tickets"]
    for state in STATES:
        lines.append("#### %s" % state)
        for issue in tickets:
            if "loom:" + state not in _labels(issue):
                continue
            lane = next((x for x in _labels(issue) if x.startswith("lane:")), "none")
            lines.append("- #%s %s - %s - %dh" % (
                issue["number"], issue.get("title", ""), lane, _age(issue, now)))
    return lines


def _pr_line(pr: Dict[str, Any], node: Optional[Dict[str, Any]]) -> str:
    node = node or {}
    commits = (node.get("commits") or {}).get("nodes") or []
    rollup = commits[-1].get("commit", {}).get("statusCheckRollup") if commits else None
    threads = (node.get("reviewThreads") or {}).get("nodes") or []
    unresolved = sum(1 for t in threads if not t.get("isResolved"))
    return "- #%s %s - checks %s - %d unresolved threads" % (
        pr["number"], pr.get("title", ""), (rollup or {}).get("state", "UNKNOWN"), unresolved)


def _repo_section(gh: GitHub, repo: str, now: datetime) -> List[str]:
    issues = _issues(gh, repo)
    goals = [i for i in issues if "loom:goal" in _labels(i)]
    tickets = [i for i in issues if "loom:ticket" in _labels(i)]
    prs = [i for i in gh.paginate("/repos/%s/pulls?state=open" % repo)
           if str(i.get("head", {}).get("ref", "")).startswith("loom/")]
    data = _graphql_status(gh, repo, prs, goals)
    lines = ["", "## %s" % repo, ""] + _goal_lines(goals, data) + [""]
    lines += _ticket_lines(tickets, now) + ["", "### Pull requests"]
    lines += [_pr_line(pr, data["prs"].get(int(pr["number"]))) for pr in prs]
    lines += ["", "### Needs operator"]
    lines += ["- #%s %s" % (i["number"], i.get("title", ""))
              for i in issues if "needs-operator" in _labels(i)]
    return lines


def _lane_status(gh: GitHub, lanes_repo: str) -> str:
    found = [i for i in _issues(gh, lanes_repo) if "loom:lanes" in _labels(i)]
    if not found:
        return "no lane status yet"
    comments = gh.paginate("/repos/%s/issues/%s/comments" % (lanes_repo, found[0]["number"]))
    marked = [c for c in comments if c.get("body", "").startswith(LANES_MARKER)]
    if not marked:
        return "no lane status yet"
    return max(marked, key=lambda c: c.get("created_at", ""))["body"]


def status(gh: GitHub, repos: Sequence[str], lanes_repo: str = "quadseven/loom") -> str:
    now = datetime.now(timezone.utc)
    out: List[str] = ["# Loom status"]
    for repo in repos:
        out += _repo_section(gh, repo, now)
    out += ["", "## Lane status", _lane_status(gh, lanes_repo)]
    return "\n".join(out)


def _check_brief_args(title: str, body: str, lane: Optional[str],
                      model: Optional[str], base: Optional[str]) -> None:
    if lane is not None and lane not in LANES:
        raise ValueError("unknown lane")
    # The same patterns lanes/run_lane.sh applies, so a header line can never
    # smuggle in another field.
    if model is not None and not re.fullmatch(r"[A-Za-z0-9._:][A-Za-z0-9._:/@-]*", model):
        raise ValueError("model is not a model id")
    if base is not None and not re.fullmatch(r"[A-Za-z0-9._][A-Za-z0-9._/-]*", base):
        raise ValueError("base is not a branch name")
    if "\n" in title or "\r" in title:
        raise ValueError("title is one line")
    if any(section not in body for section in SECTIONS):
        raise ValueError("body must contain all required sections")


def _check_goal_capacity(gh: GitHub, repo: str, goal: int) -> None:
    try:
        goal_issue = gh.rest("GET", "/repos/%s/issues/%d" % (repo, goal)).data
    except GitHubError as exc:
        if exc.status == 404:
            raise ValueError("goal must be an open loom:goal issue") from None
        raise
    if goal_issue.get("state") != "open" or "loom:goal" not in _labels(goal_issue):
        raise ValueError("goal must be an open loom:goal issue")
    children = gh.paginate("/repos/%s/issues/%d/sub_issues?per_page=100" % (repo, goal))
    count = sum(1 for i in children if i.get("state") == "open" and "loom:ticket" in _labels(i))
    if count >= 5:
        raise ValueError("goal already has 5 open tickets")


def _ticket_body(body: str, difficulty: Optional[str], model: Optional[str],
                 base: Optional[str]) -> str:
    headers = ["%s: %s" % (k, v) for k, v in
               (("Difficulty", difficulty), ("Model", model), ("Base", base)) if v]
    return ("\n".join(headers) + "\n\n" if headers else "") + body


def brief(gh: GitHub, repo: str, goal: int, title: str, body: str,
          lane: Optional[str] = None, difficulty: Optional[str] = None,
          model: Optional[str] = None, base: Optional[str] = None) -> str:
    _check_brief_args(title, body, lane, model, base)
    _check_goal_capacity(gh, repo, goal)
    names = ["loom:ticket", "loom:briefed" if lane else "loom:queued"]
    if lane:
        names.append("lane:" + lane)
    issue = gh.rest("POST", "/repos/%s/issues" % repo, {
        "title": title, "body": _ticket_body(body, difficulty, model, base),
        "labels": names}).data
    gh.rest("POST", "/repos/%s/issues/%d/sub_issues" % (repo, goal),
            {"sub_issue_id": issue["id"]})
    return str(issue.get("html_url", ""))


def move(gh: GitHub, repo: str, number: int, state: str,
         lane: Optional[str] = None, note: Optional[str] = None) -> None:
    if state not in STATES:
        raise ValueError("invalid state")
    if lane is not None and (state != "briefed" or lane not in LANES):
        raise ValueError("lane is valid only for briefed and must be known")
    issue = gh.rest("GET", "/repos/%s/issues/%d" % (repo, number)).data
    if issue.get("state") != "open":
        raise ValueError("cannot move a closed issue")
    have = _labels(issue)
    kept = [x for x in have if x not in ["loom:" + item for item in STATES]
            and not (lane is not None and x.startswith("lane:"))]
    kept.append("loom:" + state)
    if lane:
        kept.append("lane:" + lane)
    if state == "briefed" and sum(1 for x in kept if x.startswith("lane:")) != 1:
        raise ValueError("briefed ticket must have exactly one lane label")
    gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number), {"labels": kept})
    if note is not None:
        gh.rest("POST", "/repos/%s/issues/%d/comments" % (repo, number), {"body": note})


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python3 -m bus.brain")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("status"); s.add_argument("repos", nargs="+"); s.add_argument("--lanes-repo", default="quadseven/loom")
    b = sub.add_parser("brief"); b.add_argument("repo"); b.add_argument("--goal", type=int, required=True); b.add_argument("--title", required=True); b.add_argument("--file", required=True); b.add_argument("--lane"); b.add_argument("--difficulty", choices=("easy", "medium", "hard")); b.add_argument("--model"); b.add_argument("--base")
    m = sub.add_parser("move"); m.add_argument("ticket"); m.add_argument("--to", choices=STATES, required=True); m.add_argument("--lane"); m.add_argument("--note")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    gh: Optional[GitHub] = None
    try:
        args = parser().parse_args(argv)
        gh = GitHub(token_from_env())
        if args.command == "status":
            repos = [_repo(x) for x in args.repos]
            lanes_repo = _repo(args.lanes_repo)
            print(status(gh, repos, lanes_repo))
        elif args.command == "brief":
            repo = _repo(args.repo)
            with open(args.file, "r", encoding="utf-8") as stream:
                url = brief(gh, repo, args.goal, args.title, stream.read(), args.lane, args.difficulty, args.model, args.base)
            print(url)
        else:
            match = re.fullmatch(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([0-9]+)", args.ticket)
            if not match:
                raise ValueError("ticket must be OWNER/NAME#N")
            move(gh, match.group(1), int(match.group(2)), args.to, args.lane, args.note)
        return 0
    except (ValueError, OSError, GitHubError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1 if isinstance(exc, ValueError) else 2
    finally:
        if gh is not None:
            print("GitHub requests: %d" % gh.requests, file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
