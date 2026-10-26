"""needs-brain holds that nothing resolves (loom#1872).

needs-brain holds a ticket for the brain: the factory moves it no more. The
remote brain that answered these holds was retired on 2026-10-24, and on
2026-10-26 96 open tickets carried the label, the oldest since 2026-10-19,
holding their goals' cap room the whole time. The factory now resolves a
hold older than HOLD_AGE that Loom itself set (bus/factory.py brain_holds):
a pull request that fails only the plan check gets its body rebuilt and the
hold lifted; any other goes to the hand-off pool (labels.HANDOFF) with one
note.

Who set a hold is read from the issue's newest "labeled" event for the
label, never from a comment anyone could write: a login in LOOM_BRIEF_AUTHORS
that is a GitHub App ("<slug>[bot]") is Loom; any other login is a person,
whose hold is never lifted here. An event with no actor, or no event at all,
leaves the setter unknown: that hold is left and counted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Sequence

LABEL = "needs-brain"
# A hold this old is resolved; a younger one may still be answered.
HOLD_AGE = 24 * 3600
# At most this many holds are resolved a run, and a run at most every
# LIFT_EVERY seconds, stamped however the run ends (bus/station.py
# lift_brain_holds): on 2026-10-20 an hourly sweep stamped only on success
# ran every pass and settled 91 sources in minutes.
LIFT_PER_RUN = 5
LIFT_EVERY = 1800
# On the ticket's status comment: the factory resolved this hold.
LIFT_MARKER = "<!-- loom:hold-lifted -->"
# A note this close to the label event, before or after it, names the hold.
REASON_WINDOW = 600
REASON_MAX = 160

LOOM, PERSON, UNKNOWN = "loom", "person", "unknown"


@dataclass(frozen=True)
class Setter:
    """Who added the current hold (LOOM, PERSON or UNKNOWN), their login,
    and when (epoch seconds; 0 when no event says)."""
    kind: str
    login: str
    at: float


def _ts(value: Any) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def loom_logins(trusted: Iterable[str]) -> set:
    """The trusted logins that are GitHub Apps: Loom's own identities. A
    trusted person (the operator) is not among them."""
    return {str(t).lower() for t in trusted if str(t).lower().endswith("[bot]")}


def setter(events: Sequence[Any], loom: Iterable[str]) -> Setter:
    """Who added the label, from the newest "labeled" event for it."""
    ours = {x.lower() for x in loom}
    labeled = [e for e in events if isinstance(e, dict) and e.get("event") == "labeled"
               and str((e.get("label") or {}).get("name") or "") == LABEL]
    if not labeled:
        return Setter(UNKNOWN, "", 0.0)
    last = max(enumerate(labeled), key=lambda pair: (_ts(pair[1].get("created_at")), pair[0]))[1]
    actor = last.get("actor")
    login = str(actor.get("login") or "").lower() if isinstance(actor, dict) else ""
    at = _ts(last.get("created_at"))
    if not login:
        return Setter(UNKNOWN, "", at)
    return Setter(LOOM if login in ours else PERSON, login, at)


def _first_line(body: str) -> str:
    for line in body.splitlines():
        line = re.sub(r"<!--.*?-->", "", line).strip().lstrip("#>*- ").strip()
        if line:
            line = re.sub(r"[`@]", "", re.sub(r"\s+", " ", line))
            if len(line) > REASON_MAX:
                line = line[:REASON_MAX].rsplit(" ", 1)[0] + "..."
            return line
    return ""


def reason(comments: Sequence[Any], at: float, state: str) -> str:
    """Why the ticket was held: the first line of the newest trusted note
    within REASON_WINDOW of the label event, else what is known (its state)."""
    near = [c for c in comments if isinstance(c, dict) and at
            and abs(_ts(c.get("created_at")) - at) <= REASON_WINDOW]
    for comment in sorted(near, key=lambda c: _ts(c.get("created_at")), reverse=True):
        line = _first_line(str(comment.get("body") or ""))
        if line:
            return line
    return "no note names it; it was held at loom:%s" % (state or "unknown")


def age(found: Setter, issue: Dict[str, Any], now: float) -> float:
    """Seconds the hold has stood: from its label event, else from the
    issue's creation (an upper bound, used only to count it)."""
    since = found.at or _ts(issue.get("created_at"))
    return max(0.0, now - since)


def handoff_note(found: Setter, hours: float, why: str, handoff: str) -> str:
    return ("%s\nHeld for the brain (needs-brain) for %.0f hours, set by %s: %s. No brain "
            "resolves these holds since the remote one retired, so the factory took the hold "
            "off and put the ticket in the hand-off pool (%s), out of its goal's in-flight "
            "count: the second operator's session may claim it. Taking the label off returns "
            "it to this Loom's queue." % (LIFT_MARKER, hours, found.login, why, handoff))


def repair_note(found: Setter, hours: float, why: str) -> str:
    return ("%s\nThe needs-brain hold %s set %.0f hours ago (%s) is lifted: the pull "
            "request's body now passes the plan check, so the factory moves the ticket "
            "again." % (LIFT_MARKER, found.login, hours, why))


def counts() -> Dict[str, int]:
    """A repo's holds older than HOLD_AGE: all of them, those a person set,
    those whose setter is unknown, those Loom set (the ones a run may
    resolve), and those resolved this run."""
    return {"old": 0, PERSON: 0, UNKNOWN: 0, LOOM: 0, "lifted": 0}


def report_rows(report: Dict[str, Dict[str, int]]) -> List[Dict[str, Any]]:
    """The dashboard's form of a brain_holds report: one row a repo with
    any old hold, sorted by name."""
    return [{"repo": repo, "old": int(c.get("old", 0)), "person": int(c.get(PERSON, 0)),
             "unknown": int(c.get(UNKNOWN, 0))}
            for repo, c in sorted(report.items()) if c.get("old")]

