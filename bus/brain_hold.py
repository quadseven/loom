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

Triage issues (loom#1884): most held issues are not tickets at all but
loom:triage issues, which the rewrite step itself held (bus/rewrite.py
apply_answer adds needs-brain when it proposes a person) and then never
retried, because its candidate list leaves held issues out. The same
factory pass sends an old Loom-set triage hold to the operator once with
the question the rewrite proposed, and removes it from triage.

The rewrite's ask (loom#2936): apply_answer printed the ask of a person-only
answer to the station log and wrote nothing on the issue, so `reason` found
no note. It now leaves the ask as one routine event on the issue's status
comment (ask_note, under ASK_MARKER), and `reason` returns that ask whole.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Sequence

from . import labels
from .github import GitHub

LABEL = "needs-brain"
# Equal to bus.brain.HUMAN_MARKER and bus.answer.HUMAN_MARKER.
HUMAN_MARKER = "<!-- loom:human v1 -->"
# A hold this old is resolved; a younger one may still be answered.
HOLD_AGE = 24 * 3600
# A ticket with an open lane pull request that has waited on the brain this
# long goes to a person once per head commit (loom#1250), before the 6-hour
# lane-pr-stalled invariant (bus/invariants.py) fires.
STALL_AGE = 4 * 3600
# Last line of that ask: the pull request head it was posted for.
STALL_MARKER = "<!-- loom:brain-stall %s -->"
# At most this many holds are resolved a run, and a run at most every
# LIFT_EVERY seconds, stamped however the run ends (bus/station.py
# lift_brain_holds): on 2026-10-20 an hourly sweep stamped only on success
# ran every pass and settled 91 sources in minutes.
LIFT_PER_RUN = 5
LIFT_EVERY = 1800
# On the ticket's status comment: the factory resolved this hold.
LIFT_MARKER = "<!-- loom:hold-lifted -->"
# On the operator ask for an expired triage hold.
TRIAGE_ASK_MARKER = "<!-- loom:triage-brain-ask -->"
TRIAGE_LABEL = "loom:triage"
# On a triage issue's status comment: the ask of the rewrite answer that held
# the issue (loom#2936). Only proposed: it is not an ask to a person, and it
# carries no HUMAN_MARKER.
ASK_MARKER = "<!-- loom:rewrite-ask -->"
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


def _first_line(body: str, limit: int = REASON_MAX) -> str:
    """The body's first line with words, cleaned; cut at `limit` characters
    (0: never cut)."""
    for line in body.splitlines():
        line = re.sub(r"<!--.*?-->", "", line).strip().lstrip("#>*- ").strip()
        if line:
            line = re.sub(r"[`@]", "", re.sub(r"\s+", " ", line))
            if limit and len(line) > limit:
                line = line[:limit].rsplit(" ", 1)[0] + "..."
            return line
    return ""


def ask_note(kind: str, ask: str) -> str:
    """The routine event the rewrite step leaves when it holds a triage issue
    for the brain: the kind and the ask its answer proposed, on one line (no
    comment marker, backtick or at-sign of the model's survives, so the ask
    can close no marker and mention nobody)."""
    text = re.sub(r"<!--.*?-->", "", str(ask), flags=re.S)
    text = re.sub(r"<!--|-->|[`@]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return ("%s\nThe rewrite proposed person-only work (Kind: %s) and held the issue for "
            "the brain. Its ask: %s" % (ASK_MARKER, kind, text))


def ask_of(body: Any) -> str:
    """The line of an ask_note, whole; "" for any other text."""
    text = str(body or "")
    if ASK_MARKER not in text:
        return ""
    return _first_line(text, 0)


def _near_notes(comments: Sequence[Any], at: float) -> List[Dict[str, Any]]:
    return sorted((c for c in comments if isinstance(c, dict) and at
                   and abs(_ts(c.get("created_at")) - at) <= REASON_WINDOW),
                  key=lambda c: _ts(c.get("created_at")), reverse=True)


def reason(comments: Sequence[Any], at: float, state: str) -> str:
    """Why the ticket was held: the first line of the newest trusted note
    within REASON_WINDOW of the label event, else what is known (its state).
    The rewrite's own ask among those notes (ask_note) is the reason, whole."""
    near = _near_notes(comments, at)
    for comment in near:
        ask = ask_of(comment.get("body"))
        if ask:
            return ask.removesuffix(".")
    for comment in near:
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


def triaged(gh: GitHub, repo: str) -> List[Dict[str, Any]]:
    """The repo's open triage issues, including those outside any goal tree."""
    data = gh.paginate("/repos/%s/issues?labels=loom%%3Atriage&state=open" % repo)
    return [i for i in data if isinstance(i, dict) and "pull_request" not in i
            and "number" in i and labels.in_triage(
                [label.get("name", "") for label in i.get("labels") or []])]


def triage_ask(comments: Sequence[Any], at: float) -> str:
    """Use the recorded question, or ask for a ruling on triage itself."""
    kind, ask = "decision", "Should Loom turn this issue into a ticket, hold it, or close it?"
    for comment in _near_notes(comments, at):
        proposed = re.fullmatch(r"The rewrite proposed person-only work \(Kind: ([a-z]+)\) "
                                r"and held the issue for the brain\. Its ask: (.+)",
                                ask_of(comment.get("body")))
        if proposed:
            kind, ask = proposed.groups()
            break
    return "%s\n%s\nKind: %s\n%s" % (HUMAN_MARKER, ask, kind, TRIAGE_ASK_MARKER)


def repair_note(found: Setter, hours: float, why: str) -> str:
    return ("%s\nThe needs-brain hold %s set %.0f hours ago (%s) is lifted: the pull "
            "request's body now passes the plan check, so the factory moves the ticket "
            "again." % (LIFT_MARKER, found.login, hours, why))


def stall_ask(repo: str, ticket: int, number: int, sha: str, hours: float) -> str:
    """The comment that sends a ticket stalled on the brain to a person: the
    loom:human marker, the one exact ask, and the head it was posted for."""
    return ("%s\nKind: decision\nPull request #%d of %s#%d has waited on the brain "
            "(needs-brain) for %.0f hours and no brain tick has taken it. Merge it with "
            "python3 -m bus.brain merge %s#%d, or send it back to a lane with "
            "python3 -m bus.brain fix %s#%d --ticket %s#%d --file FIX.md.\n%s"
            % (HUMAN_MARKER, number, repo, ticket, hours, repo, number, repo, number, repo,
               ticket, STALL_MARKER % sha))


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

