"""The journal and report comment formats from docs/BRAIN.md."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Sequence, Tuple, Union

from . import text as _text
from .github import GitHub
from .handoff import _TICK

JOURNAL_MARKER = "<!-- loom:journal v1 -->"
REPORT_MARKER = "<!-- loom:report v1 -->"
# The brain's answer to an operator note; the station sends it to the
# Discord message it names (docs/LANES.md, "Talking to the brain").
REPLY_RE = re.compile(r"<!-- loom:reply v1 discord=([0-9]{1,20}) -->")
# Two loom:report comments on one goal home closer than this are one too many
# (the report-cadence invariant). The station's standup goes out as the Loom
# App once a goal's report is 1 hour 45 minutes old; a brain that trusted only
# the operator's login then posted its own report 7 to 70 minutes later, and
# thirteen invariants were filed for it on 2026-10-18.
REPORT_GAP = 90 * 60


class ReportTooSoon(ValueError):
    """A report not posted: another went out on the goal home within REPORT_GAP."""


def _at(value: Any) -> Optional[datetime]:
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def report_hold(comments: Iterable[Tuple[str, Any, Any]], now: datetime,
                trusted: Sequence[str] = ()) -> Optional[Tuple[str, datetime]]:
    """(login, createdAt) of the newest loom:report among (login, createdAt,
    body) comments that is less than REPORT_GAP old at `now`, else None.

    Any GitHub App's report counts (a "<slug>[bot]" login: the Loom App's
    standup, whether or not this brain trusts that login), and so does a
    `trusted` login's. With no trusted logins every author's report counts.
    Only a person outside the trust list is left out, so a stranger's comment
    on a public goal home cannot hold its reports back."""
    allowed = {a.lower() for a in trusted}
    newest: Optional[Tuple[str, datetime]] = None
    for login, created, body in comments:
        when = _at(created)
        name = str(login or "").lower()
        if (when is None or not str(body or "").lstrip().startswith(REPORT_MARKER)
                or (allowed and not name.endswith("[bot]") and name not in allowed)
                or (now - when).total_seconds() >= REPORT_GAP):
            continue
        if newest is None or when > newest[1]:
            newest = (str(login), when)
    return newest


def hold_message(held: Tuple[str, datetime], now: datetime) -> str:
    login, when = held
    return ("%s posted a report on this goal home at %s, %d minutes ago; reports on one "
            "goal home go out at least %d minutes apart, so the next may go out at %s"
            % (login, _when(when), max(0, int((now - when).total_seconds() // 60)),
               REPORT_GAP // 60, _when(when + timedelta(seconds=REPORT_GAP))))


def _when(value: Any) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)


def _items(value: Union[str, Iterable[str], None]) -> str:
    if value is None:
        return "none"
    if isinstance(value, str):
        return value.strip() or "none"
    parts = [str(v) for v in value]
    return ", ".join(parts) if parts else "none"


def render_journal(when, holder, shipped, briefed, waiting, asks,
                   requests, minutes, measure=None) -> str:
    head = [JOURNAL_MARKER, "Tick %s, %s" % (_when(when), holder)]
    if measure is not None:
        head.append("- Measure: %s" % _items(measure))
    return "\n".join(head + [
        "- Shipped: %s" % _items(shipped),
        "- Briefed: %s" % _items(briefed),
        "- Waiting: %s" % _items(waiting),
        "- Asks: %s" % _items(asks),
        "- Cost: %s requests, %s minutes" % (requests, minutes),
    ])


def render_report(when, holder, measure, shipped, in_flight, blocked,
                  next_step) -> str:
    return "\n".join([
        REPORT_MARKER,
        "Report %s, %s" % (_when(when), holder),
        "- Measure: %s" % _items(measure),
        "- Shipped: %s" % _items(shipped),
        "- In flight: %s" % _items(in_flight),
        "- Blocked: %s" % _items(blocked),
        "- Next: %s" % _items(next_step),
    ])


def with_tick(text: str, holder: Optional[str], now: Optional[datetime] = None) -> str:
    """A journal with its "Tick <UTC time>, <holder>" line, which is how a
    peer brain tells whose journal it is (handoff.journal_holder). A journal
    without one counts as another brain's, so each brain read its own last
    tick as a peer's and kept itself to resume items for hours (2026-10-16:
    no tick journaled that line, and no brain merged, briefed or triaged).
    With a holder the line is added after the marker when missing; a line
    naming a different holder, or no line and no holder, is refused."""
    found = _TICK.search(text)
    if found:
        if holder is not None and found.group(1) != holder:
            raise ValueError("the journal's Tick line names %s, not %s" % (found.group(1), holder))
        return text
    if holder is None:
        raise ValueError("a journal needs the line \"Tick <UTC time>, <holder>\" after the "
                         "marker, or pass --holder to add it (docs/BRAIN.md)")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", holder):
        raise ValueError("holder must be letters, digits, dot, dash or underscore")
    rest = text[len(JOURNAL_MARKER):].lstrip("\r\n")
    tick = "Tick %s, %s" % (_when(now or datetime.now(timezone.utc)), holder)
    return "%s\n%s\n%s" % (JOURNAL_MARKER, tick, rest)


def _check_report_gap(gh: GitHub, repo: str, issue_number: int, now: datetime,
                      trusted: Sequence[str]) -> None:
    """Raise ReportTooSoon when a report went out on the goal home within
    REPORT_GAP. `since` filters on the update time, which is never before the
    creation time, so one page holds every comment created in the gap."""
    since = _when(now - timedelta(seconds=REPORT_GAP))
    found = gh.paginate("/repos/%s/issues/%d/comments?since=%s"
                        % (repo, int(issue_number), since))
    held = report_hold(((str((c.get("user") or {}).get("login") or ""), c.get("created_at"),
                         c.get("body")) for c in found if isinstance(c, dict)), now, trusted)
    if held is not None:
        raise ReportTooSoon(hold_message(held, now))


def post(gh: GitHub, repo: str, issue_number: int, text: str,
         holder: Optional[str] = None, trusted: Sequence[str] = (),
         now: Optional[datetime] = None):
    """Post a journal, report or reply. A report is refused with ReportTooSoon,
    and nothing posted, when another report went out on the same goal home
    within REPORT_GAP (report_hold says whose count)."""
    if not (text.startswith((JOURNAL_MARKER, REPORT_MARKER)) or REPLY_RE.match(text)):
        raise ValueError("comment must start with a loom journal, report or reply marker")
    if text.startswith(JOURNAL_MARKER):
        text = with_tick(text, holder)
    _text.check(text)
    if text.startswith(REPORT_MARKER):
        _check_report_gap(gh, repo, issue_number, now or datetime.now(timezone.utc), trusted)
    return gh.rest("POST", "/repos/%s/issues/%d/comments" % (repo, int(issue_number)),
                   {"body": text})
