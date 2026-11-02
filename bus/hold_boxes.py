"""Unticked hold-before-merge boxes on the tickets a pull request names.

On 2026-11-01 a pull request merged with every gate green while its linked
ticket still had an unticked box saying to keep the pull request blocked
until a live check passed: no gate read the ticket (loom#2392). The shared
merge gate (scripts/pr-merge-gated.sh, its ticket-holds row) and
`bus.brain merge` read the boxes the same way, with the same patterns:

- An explicit hold always holds: "keep this PR blocked", "do not merge",
  "decides to merge".
- A box that says "before ... merge" holds, unless it is about after-merge
  or live proof ("Live:", "after the deploy").

Epics are skipped: their boxes are their children. A ticket that cannot be
read fails closed.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

Ticket = Tuple[str, int]

_TICKET = r"(?:[\w.-]+/[\w.-]+)?#\d+"
_TICKET_REFS = re.compile(r"\b(?:refs|part of)\s+({0}(?:\s*,\s*{0})*)".format(_TICKET), re.I)
_TICKET_ONE = re.compile(r"(?:([\w.-]+/[\w.-]+))?#(\d+)")
_UNTICKED_BOX = re.compile(r"^\s*[-*+]\s+\[ \]\s+(.+)$", re.M)
_STRONG_HOLD = re.compile(
    r"keep (?:this|the) (?:pr|pull request) (?:blocked|held|open|unmerged)"
    r"|\bdo not merge\b|\bdon't merge\b"
    r"|\bdecides? to merge\b",
    re.I,
)
_WEAK_HOLD = re.compile(r"\bbefore\b[^\n]*?\bmerg(?:e|es|ed|ing)\b", re.I)
_AFTER_MERGE = re.compile(
    r"^\s*(?:\*\*)?live\b\s*(?:\*\*)?:"
    r"|\bafter (?:the )?(?:deploy\w*|roll\w*|merg\w*)\b",
    re.I,
)


def linked_tickets(body: str, repo: str) -> List[Ticket]:
    """Every (repo, number) the body names with Refs or Part of, in order, once each."""
    found: List[Ticket] = []
    for refs in _TICKET_REFS.finditer(body or ""):
        for owner_repo, number in _TICKET_ONE.findall(refs.group(1)):
            ticket = (owner_repo or repo, int(number))
            if ticket not in found:
                found.append(ticket)
    return found


def hold_boxes(body: str) -> List[str]:
    """The unticked boxes in a ticket body that ask to hold the merge."""
    held = []
    for match in _UNTICKED_BOX.finditer(body or ""):
        text = match.group(1).strip()
        if _STRONG_HOLD.search(text) or (_WEAK_HOLD.search(text)
                                         and not _AFTER_MERGE.search(text)):
            held.append(text)
    return held


def hold_box_reason(ticket_body: str) -> str:
    """The first unticked hold box of a ticket body, as a reason, else ""."""
    boxes = hold_boxes(ticket_body)
    if not boxes:
        return ""
    return "unticked hold-before-merge box %r" % boxes[0][:80]


def _is_epic(issue: Dict[str, Any]) -> bool:
    names = [str((label or {}).get("name") or "").lower() for label in issue.get("labels") or []]
    return "epic" in names or bool(re.match(r"\s*epic\s*:", str(issue.get("title") or ""), re.I))


def refusal(body: str, repo: str, read: Callable[[Ticket], Dict[str, Any]],
            known: Optional[Dict[Ticket, Dict[str, Any]]] = None,
            extra: Sequence[Ticket] = ()) -> str:
    """Why a merge must wait for a ticket's hold box, as "blocked: ...", else "".
    It reads every ticket in `extra` (the pull request's own ticket) and every
    one the body names with Refs or Part of; `known` holds tickets already
    read. `read` raises on a ticket it cannot read, which fails closed."""
    known = dict(known or {})
    tickets = list(extra) + [t for t in linked_tickets(body, repo) if t not in extra]
    found = []
    for ticket in tickets:
        ref = "%s#%d" % ticket
        try:
            issue = known[ticket] if ticket in known else (read(ticket) or {})
        except Exception as err:  # noqa: BLE001 - any failed read fails closed
            found.append("%s unreadable (%s)" % (ref, type(err).__name__))
            continue
        if _is_epic(issue):
            continue
        reason = hold_box_reason(str(issue.get("body") or ""))
        if reason:
            found.append("%s %s" % (ref, reason))
    return "blocked: " + "; ".join(found) if found else ""
