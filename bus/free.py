"""Open issues free for an operator to take (docs/LANES.md, "Two Looms on
shared repos").

A second operator working the same repos as a Loom cannot tell from the
labels alone which open issues are free: loom:triage and loom:rewritten read
as owned, though only an assignment, a claim:<login> label or an open Loom
ticket means someone is on it. This lists, per repo, the open issues that
are free for one operator: owned by no one else (bus.tickets.owned_elsewhere,
the rule triage and the station use, so this view cannot disagree with the
factory), not an epic or goal home, not a Loom ticket, and covered by no
open Loom ticket. An issue whose Loom ticket is already closed or merged is
listed apart, so the operator can verify it and close the original.

A ticket covers an issue when its body says "Rewritten from issue M." (what
`bus.brain brief --refs` writes) or "Refs #M" / "Refs OWNER/NAME#M" (older
rewrites), or when the issue has a "Rewritten as ticket #N" or "Briefed as
#N" comment naming it.

    python3 -m bus.free OWNER/NAME [OWNER/NAME ...] --operator LOGIN [--json]

It only reads. With no --operator it lists what is free for anyone: no
assignee and no claim label at all.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote

from . import tickets

FREE, CHECK, TAKEN = "free", "check", "taken"

_REWRITTEN_FROM_RE = re.compile(r"\bRewritten from issue (\d+)\b", re.I)
_REFS_RE = re.compile(r"\bRefs[ \t]+(?:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+))?#(\d+)\b", re.I)
_COMMENT_TICKET_RE = re.compile(r"\b(?:Rewritten as ticket|Briefed as)[ \t]+#(\d+)\b", re.I)

Key = Tuple[str, int]


def _names(issue: Dict[str, Any]) -> List[str]:
    out = []
    for label in issue.get("labels") or []:
        name = label.get("name") if isinstance(label, dict) else label
        if isinstance(name, str):
            out.append(name.lower())
    return out


def is_epic(issue: Dict[str, Any]) -> bool:
    """An epic: labelled epic, or a parent of sub-issues. Triage's own test
    (bus.brain._is_epic), so the two never disagree on what an epic is."""
    from .brain import _is_epic
    return _is_epic(issue)


def covered_issues(repo: str, ticket: Dict[str, Any]) -> List[Key]:
    """The (repo, number) issues a ticket's body says it was written from."""
    body = str(ticket.get("body") or "")
    found = [(repo, int(n)) for n in _REWRITTEN_FROM_RE.findall(body)]
    found += [(named or repo, int(n)) for named, n in _REFS_RE.findall(body)]
    return list(dict.fromkeys(found))


def comment_tickets(comments: Iterable[Any]) -> List[int]:
    """The ticket numbers an issue's "Rewritten as ticket #N" or "Briefed as
    #N" comments name, oldest first."""
    found: List[int] = []
    for comment in comments or []:
        body = comment.get("body") if isinstance(comment, dict) else comment
        found += [int(n) for n in _COMMENT_TICKET_RE.findall(str(body or ""))]
    return list(dict.fromkeys(found))


def ticket_done(ticket: Dict[str, Any]) -> bool:
    """A ticket that no longer covers its issue: closed, or merged (its state
    label is merged, shipped or verified)."""
    return ticket.get("state") == "closed" or tickets.ticket_state(ticket) in tickets.MERGED_STATES


def coverage(tickets_by_repo: Dict[str, Sequence[Dict[str, Any]]]) -> Dict[Key, List[Tuple[str, Dict[str, Any]]]]:
    """{(repo, issue number): [(ticket repo, ticket)]} for every Loom ticket
    whose body names the issue it was written from."""
    out: Dict[Key, List[Tuple[str, Dict[str, Any]]]] = {}
    for repo, found in tickets_by_repo.items():
        for ticket in found or []:
            for key in covered_issues(repo, ticket):
                out.setdefault(key, []).append((repo, ticket))
    return out


def _ref(repo: str, ticket_repo: str, number: Any) -> str:
    return "#%s" % number if ticket_repo == repo else "%s#%s" % (ticket_repo, number)


def _not_work(issue: Dict[str, Any]) -> Optional[str]:
    """Why the issue is no one's work to take: an epic, goal home or ticket."""
    names = _names(issue)
    if is_epic(issue):
        return "an epic"
    if "loom:goal" in names:
        return "a goal home"
    if "loom:ticket" in names:
        return "a Loom ticket"
    return None


def _covered(repo: str, issue: Dict[str, Any],
             covering: Sequence[Tuple[str, Dict[str, Any]]]) -> Optional[Tuple[str, str]]:
    """(taken or check, why) from the Loom tickets covering the issue, or
    None when no ticket covers it and it is not marked rewritten."""
    refs = lambda found: ", ".join(_ref(repo, r, t.get("number")) for r, t in found)
    open_tickets = [(r, t) for r, t in covering if not ticket_done(t)]
    if open_tickets:
        return TAKEN, "Loom ticket %s is open" % refs(open_tickets)
    if covering:
        how = "/".join(dict.fromkeys("closed" if t.get("state") == "closed" else "merged"
                                     for _, t in covering))
        return CHECK, "Loom ticket %s is %s; verify and close this issue" % (refs(covering), how)
    if "loom:rewritten" in _names(issue):
        return CHECK, "loom:rewritten, but no ticket naming it was found; check before taking it"
    return None


def classify(repo: str, issue: Dict[str, Any], operator: str,
             covering: Sequence[Tuple[str, Dict[str, Any]]] = ()) -> Tuple[str, str]:
    """(free | check | taken, why) for one open issue, for one operator login
    ("" for anyone). Ownership is bus.tickets.owned_elsewhere with the
    operator as the only own login: the one rule triage and the station use."""
    operator = (operator or "").lower()
    owner = tickets.owned_elsewhere(issue, [operator] if operator else [])
    if owner is not None:
        return TAKEN, "held by %s (assigned or claim:%s)" % (owner, owner)
    why = _not_work(issue)
    if why:
        return TAKEN, why
    verdict = _covered(repo, issue, covering)
    if verdict:
        return verdict
    mine = operator and operator in tickets.assignees(issue) + tickets.claimed_by(issue)
    return FREE, "yours (assigned or claim:%s)" % operator if mine else "no owner and no Loom ticket"


def loom_state(issue: Dict[str, Any]) -> str:
    """The issue's loom: labels, comma separated, or "none"."""
    found = [n for n in _names(issue) if n.startswith("loom:")]
    return ", ".join(found) if found else "none"


def free_issues(repo: str, issues: Iterable[Dict[str, Any]], operator: str,
                covers: Dict[Key, List[Tuple[str, Dict[str, Any]]]]) -> List[Dict[str, Any]]:
    """Each open issue of the repo (pull requests left out) as {repo, number,
    title, html_url, bucket, reason, loom}, free ones first, oldest first."""
    out = []
    for issue in issues:
        if not isinstance(issue, dict) or "pull_request" in issue or issue.get("state") == "closed":
            continue
        number = int(issue.get("number") or 0)
        bucket, reason = classify(repo, issue, operator, covers.get((repo, number), ()))
        out.append({"repo": repo, "number": number, "title": str(issue.get("title") or ""),
                    "html_url": issue.get("html_url"), "bucket": bucket, "reason": reason,
                    "loom": loom_state(issue)})
    order = {FREE: 0, CHECK: 1, TAKEN: 2}
    return sorted(out, key=lambda e: (order[e["bucket"]], e["number"]))


Lookup = Callable[[str, int], Optional[Dict[str, Any]]]


def needs_comments(entry: Dict[str, Any]) -> bool:
    """Whether an entry's own comments may still name a ticket covering it:
    it reads free, or rewritten with no ticket found, and a Loom has
    labelled it (an issue no Loom ever labelled was never briefed)."""
    unresolved = entry["reason"].startswith("loom:rewritten, but no ticket")
    return (entry["bucket"] == FREE or unresolved) and entry["loom"] != "none"


def resolve_comments(repo: str, entries: List[Dict[str, Any]], issues: Dict[int, Dict[str, Any]],
                     operator: str, comments_of: Callable[[int], Sequence[Any]],
                     lookup: Lookup) -> None:
    """Settle, in place, the entries whose "Rewritten as ticket #N" or
    "Briefed as #N" comments name a ticket no ticket body links back: one
    comment read per such entry (needs_comments) and one lookup per ticket
    a comment names."""
    for entry in entries:
        if not needs_comments(entry):
            continue
        found = []
        for number in comment_tickets(comments_of(entry["number"])):
            ticket = lookup(repo, number)
            if isinstance(ticket, dict):
                found.append((repo, ticket))
        if found:
            entry["bucket"], entry["reason"] = classify(repo, issues[entry["number"]], operator, found)
    order = {FREE: 0, CHECK: 1, TAKEN: 2}
    entries.sort(key=lambda e: (order[e["bucket"]], e["number"]))


def collect(gh: Any, repos: Sequence[str], operator: str) -> Dict[str, List[Dict[str, Any]]]:
    """{repo: entries} read from GitHub: each repo's open issues and its Loom
    tickets (open and closed), then the comments of the entries that still
    read free or unresolved (needs_comments)."""
    open_issues = {repo: gh.paginate("/repos/%s/issues?state=open" % quote(repo, safe="/"))
                   for repo in repos}
    loom_tickets = {repo: gh.paginate("/repos/%s/issues?labels=loom%%3Aticket&state=all"
                                      % quote(repo, safe="/")) for repo in repos}
    covers = coverage(loom_tickets)
    index = {(repo, int(t["number"])): t for repo, found in loom_tickets.items()
             for t in found if isinstance(t, dict) and t.get("number") is not None}

    def lookup(where: str, number: int) -> Optional[Dict[str, Any]]:
        if (where, number) in index:
            return index[(where, number)]
        return gh.rest("GET", "/repos/%s/issues/%d" % (quote(where, safe="/"), number)).data

    out = {}
    for repo in repos:
        issues = {int(i["number"]): i for i in open_issues[repo]
                  if isinstance(i, dict) and "pull_request" not in i}
        entries = free_issues(repo, issues.values(), operator, covers)

        def comments_of(number: int, repo: str = repo) -> Sequence[Any]:
            return gh.paginate("/repos/%s/issues/%d/comments" % (quote(repo, safe="/"), number))

        resolve_comments(repo, entries, issues, operator, comments_of, lookup)
        out[repo] = entries
    return out


def render_text(found: Dict[str, List[Dict[str, Any]]], operator: str) -> str:
    who = operator or "anyone"
    lines = []
    for repo, entries in found.items():
        free = [e for e in entries if e["bucket"] == FREE]
        check = [e for e in entries if e["bucket"] == CHECK]
        lines.append("%s: %d open issues, %d free for %s, %d to check" % (
            repo, len(entries), len(free), who, len(check)))
        for title, group in (("free", free), ("check", check)):
            for e in group:
                lines.append("  %s #%d %s [%s] (loom: %s)" % (title, e["number"], e["title"],
                                                             e["reason"], e["loom"]))
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None, gh: Any = None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m bus.free",
                                description="List open issues free for an operator to take (read only).")
    p.add_argument("repos", nargs="+", metavar="OWNER/NAME")
    p.add_argument("--operator", default="", help="the GitHub login taking work; empty for anyone")
    p.add_argument("--json", action="store_true", help="print the entries as JSON")
    args = p.parse_args(argv)
    for repo in args.repos:
        if not tickets.REPO_RE.fullmatch(repo):
            p.error("expected OWNER/NAME, got %r" % repo)
    from .github import GitHub, GitHubError, auth_from_env
    try:
        if gh is None:
            gh = GitHub(auth_from_env())
        found = collect(gh, args.repos, args.operator)
    except (GitHubError, OSError, ValueError) as err:
        print("error: %s" % err, file=sys.stderr)
        return 2
    print(json.dumps(found, indent=2) if args.json else render_text(found, args.operator))
    return 0


if __name__ == "__main__":
    sys.exit(main())
