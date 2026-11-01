"""What needs a person now, read from the tickets (loom#2387).

    python3 -m bus.needs_you [--repo OWNER/NAME ...]

A label says an ask was made, not that it is still open. A brain that counted
needs:human, carried a list between ticks, or read one page of comments told
the operator something needed them when the answer was already on the ticket:
GitHub's comments API returns the oldest 30 by default, so `.[-1]` of a read
with no pagination is not the newest comment.

This command writes nothing. For each open issue or pull request that holds
an ask label or a needs:<login> label it pages every comment and event, runs
bus/ask_state.py, and puts the item in one section:

- answered: the answer is in the ticket; the label pass has not caught up.
- verification: the station's own tag (criteria_gate.ask), not a person's
  question. A later person ask replaces it.
- waiting: a wait:* label, or a Blocked on section naming an open issue.
- no written ask: a label with no ask in words. Nobody can act on it.
- check first: open, but a pull request naming the ticket merged after the
  ask. That may be the answer: read it. A merged pull request is not proof,
  so the ask stays open and stays printed.
- needs you: a written ask to a person, open, with no later answer.

An item that could not be read is listed as unread and the command exits 1:
an unread ticket is never a ticket with nothing to do.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

from dashboard import operators

from . import answer, ask_state, brain, criteria_gate, decision_claim, status
from .github import GitHub, GitHubError, auth_from_env
from .labels import needs_human

NEEDS_YOU, CHECK_FIRST, NO_ASK = "needs you", "check first", "no written ask"
WAITING, VERIFICATION, ANSWERED = "waiting", "verification", "answered"
HEADINGS = (
    (NEEDS_YOU, "Needs you"),
    (CHECK_FIRST, "Check first: a pull request naming it merged after the ask"),
    (NO_ASK, "No written ask: a label, not a question"),
    (WAITING, "Waiting: nobody can act on these today"),
    (VERIFICATION, "Station verification tags: not asks to a person"),
    (ANSWERED, "Answered: the label has not caught up"),
)
THE_OPERATOR = "the operator"
_ASK_SIGNS = ("needs-operator", "needs:human", "loom:human")


class Item(NamedTuple):
    ref: str            # OWNER/NAME#N
    section: str        # one of HEADINGS
    state: str          # ask_state's answered, waiting or open
    who: Tuple[str, ...]
    ask: str            # verbatim, "" when none is written
    asked_at: str       # "" when it cannot be read
    url: str
    latest_by: str      # "" with no comment
    latest_at: str
    claims: Tuple[str, ...] = ()
    answer: Optional[str] = None
    merged: Tuple[str, ...] = ()


def _names(issue: Dict[str, Any]) -> List[str]:
    return [str(x.get("name") if isinstance(x, dict) else x) for x in issue.get("labels") or []]


def waits_on_person(names: Sequence[str]) -> bool:
    """The labels ask a person: an ask label or a needs:<login> label."""
    return needs_human(names) or bool(operators.needs_of(names))


def addressed_to(names: Sequence[str]) -> Tuple[str, ...]:
    """Who the labels name: each needs:<login>, and the operator for a label
    that names nobody."""
    people = list(operators.needs_of(names))
    return tuple(people + ([THE_OPERATOR] if set(names) & set(_ASK_SIGNS) else []))


def _stamp(when: Optional[datetime]) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if when else ""


def written_ask(issue: Dict[str, Any], comments: Sequence[Any],
                names: Sequence[str]) -> Tuple[str, Optional[Dict[str, Any]]]:
    """(the ask as written, the comment holding it): the newest needs:human
    comment, else the body's "## Ask" section, else the newest comment that
    names the ask. ("", None) when none is written."""
    chosen = operators.ask_comment(comments, operators.ask_names(names), strict=True)
    marked = chosen is not None and answer.HUMAN_MARKER in str(chosen.get("body") or "")
    section = operators.body_ask(issue.get("body"))
    if section and not marked:
        return section, None
    return (operators.ask_full(chosen.get("body")), chosen) if chosen is not None else ("", None)


def merged_after(timeline: Iterable[Any], since: Optional[datetime]) -> Tuple[str, ...]:
    """Each pull request that names the ticket and merged after the ask."""
    found: List[str] = []
    for event in timeline:
        if since is None or not isinstance(event, dict) or event.get("event") != "cross-referenced":
            continue
        source = (event.get("source") or {}).get("issue") or {}
        merged = decision_claim.when((source.get("pull_request") or {}).get("merged_at"))
        if merged is not None and merged > since:
            found.append("%s#%s" % ((source.get("repository") or {}).get("full_name") or "?",
                                    source.get("number")))
    return tuple(dict.fromkeys(found))


def _station_tag(chosen: Optional[Dict[str, Any]], trusted: Sequence[str]) -> bool:
    """The ask is the station's verification tag. Only a trusted author's
    counts: anyone can type the words on a public repo."""
    if chosen is None or not criteria_gate.is_verification_ask(chosen.get("body")):
        return False
    return brain.comment_login(chosen).lower() in {str(t).lower() for t in trusted}


def _section(said: str, tag: bool, ask: str) -> str:
    """Where an item goes before its merged pull requests are read."""
    if said == ask_state.ANSWERED:
        return ANSWERED
    if tag:
        return VERIFICATION
    if said == ask_state.WAITING:
        return WAITING
    return NEEDS_YOU if ask else NO_ASK


def judge(repo: str, issue: Dict[str, Any], comments: Sequence[Any], events: Sequence[Any],
          trusted: Sequence[str], ops: Sequence[str], blockers: Sequence[int] = (),
          timeline: Iterable[Any] = ()) -> Item:
    """One item's place, from its whole comment and event history (oldest
    first) and, for an item that would need you, its timeline."""
    names = _names(issue)
    said = ask_state.state(issue, comments, events, trusted, ops, blockers)
    ask, chosen = written_ask(issue, comments, names)
    section = _section(said, _station_tag(chosen, trusted), ask)
    since = ask_state.asked_at(comments, events)
    merged = merged_after(timeline, since) if section == NEEDS_YOU else ()
    real = [c for c in comments if isinstance(c, dict) and not c.get(status.EXPANDED)]
    latest = real[-1] if real else {}
    return Item("%s#%s" % (repo, issue.get("number")), CHECK_FIRST if merged else section, said,
                addressed_to(names), ask,
                str((chosen or {}).get("created_at") or "") or _stamp(since),
                str((chosen or {}).get("html_url") or issue.get("html_url") or ""),
                brain.comment_login(latest), str(latest.get("created_at") or ""),
                tuple(n for n in names if n.startswith("claim:")),
                ask_state.answer_of(issue, comments, events, trusted, ops), merged)


def read_item(gh: Any, repo: str, issue: Dict[str, Any], trusted: Sequence[str],
              ops: Sequence[str]) -> Item:
    """One item, read whole. Every list goes through paginate: one page of
    comments is the oldest ones."""
    base = "/repos/%s/issues/%d" % (repo, int(issue["number"]))
    comments = gh.paginate(base + "/comments?per_page=100")
    events = gh.paginate(base + "/events?per_page=100")
    blockers = ask_state.open_blockers(gh.rest, repo, issue)
    item = judge(repo, issue, comments, events, trusted, ops, blockers)
    if item.section != NEEDS_YOU:
        return item
    return judge(repo, issue, comments, events, trusted, ops, blockers,
                 gh.paginate(base + "/timeline?per_page=100"))


def read_repo(gh: Any, repo: str, trusted: Sequence[str],
              ops: Sequence[str]) -> Tuple[List[Item], List[str]]:
    """(the repo's items, what could not be read)."""
    try:
        issues = gh.paginate("/repos/%s/issues?state=open&per_page=100" % repo)
    except (GitHubError, OSError) as err:
        return [], ["%s: %s" % (repo, err)]
    items, unread = [], []
    for issue in issues:
        if not isinstance(issue, dict) or not waits_on_person(_names(issue)):
            continue
        try:
            items.append(read_item(gh, repo, issue, trusted, ops))
        except (GitHubError, OSError) as err:
            unread.append("%s#%s: %s" % (repo, issue.get("number"), err))
    return items, unread


def _quoted(text: str) -> List[str]:
    return ["    > " + line for line in re.split(r"\r?\n", text.strip())]


def _lines(item: Item) -> List[str]:
    """One item: repo#N, state, the ask, who, its time, the latest comment."""
    out = ["- %s: %s" % (item.ref, item.state)]
    out.append("  to %s, asked %s" % (" and ".join(item.who) or "nobody named",
                                      item.asked_at or "at an unreadable time"))
    out.append("  latest comment: %s" % ("%s at %s" % (item.latest_by, item.latest_at)
                                         if item.latest_at else "none"))
    if item.claims:
        out.append("  held by %s" % ", ".join(item.claims))
    if item.merged:
        out.append("  merged after the ask: %s" % ", ".join(item.merged))
    if item.answer is not None:
        out.append("  answer:")
        out += _quoted(item.answer) if item.answer else ["    > a Decision heading in the body"]
    if item.ask:
        out.append("  ask (%s):" % item.url if item.url else "  ask:")
        out += _quoted(item.ask)
    return out


def render(items: Sequence[Item], unread: Sequence[str]) -> str:
    """The report. A section with nothing in it is left out, except Needs you."""
    out: List[str] = []
    for section, heading in HEADINGS:
        found = sorted((i for i in items if i.section == section), key=lambda i: i.ref)
        if not found and section != NEEDS_YOU:
            continue
        out.append("%s: %s" % (heading, len(found) if found or unread else "nothing"))
        for item in found:
            out += _lines(item)
        out.append("")
    if unread:
        out.append("Unread, so this report is incomplete: %d" % len(unread))
        out += ["- " + line for line in unread]
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def run(gh: Any, repos: Sequence[str], trusted: Sequence[str], ops: Sequence[str]) -> Tuple[str, int]:
    """(the report, the exit code)."""
    items: List[Item] = []
    unread: List[str] = []
    for repo in repos:
        found, missed = read_repo(gh, repo, trusted, ops)
        items += found
        unread += missed
    return render(items, unread), 1 if unread else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m bus.needs_you")
    parser.add_argument("--repo", action="append", default=[], metavar="OWNER/NAME",
                        help="a repo to read, repeatable (default: LOOM_REPOS)")
    args = parser.parse_args(argv)
    repos = args.repo or os.environ.get("LOOM_REPOS", "").split()
    trusted = brain.trusted_authors()
    if not repos or not trusted:
        # With no trusted login no brain's record counts, and every answered
        # ask would read as open.
        print("error: set LOOM_BRIEF_AUTHORS, and LOOM_REPOS or --repo", file=sys.stderr)
        return 2
    gh = GitHub(auth_from_env())
    gh.status_in_body = True  # read as the brain reads (bus.brain.main)
    gh.status_authors |= {login.lower() for login in trusted}
    text, code = run(gh, repos, trusted, answer.operators(trusted=trusted))
    sys.stdout.write(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
