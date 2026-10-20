"""The journal and report comment formats from docs/BRAIN.md."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple, Union

from . import text as _text
from .github import GitHub
from .handoff import (EARLIER_CLOSE, EARLIER_OPEN, JOURNAL_MARKER as _MARKER, _TICK,
                      journal_holder, split_journal)

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
    rest = text[len(JOURNAL_MARKER):].lstrip("\r\n")
    loose = _LOOSE_TICK.match(rest)
    if loose:
        # A tick header the brain wrote by hand ("Tick 2026-10-20 23:07Z,
        # muse") is replaced, never stacked under the canonical line
        # (loom#1241: a project issue, 2026-10-20 23:12Z).
        named = loose.group(1)
        if named and holder is not None and named != holder:
            raise ValueError("the journal's Tick line names %s, not %s" % (named, holder))
        holder = holder if holder is not None else named
        rest = rest[loose.end():].lstrip("\r\n")
    if holder is None:
        raise ValueError("a journal needs the line \"Tick <UTC time>, <holder>\" after the "
                         "marker, or pass --holder to add it (docs/BRAIN.md)")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", holder):
        raise ValueError("holder must be letters, digits, dot, dash or underscore")
    tick = "Tick %s, %s" % (_when(now or datetime.now(timezone.utc)), holder)
    return "%s\n%s\n%s" % (JOURNAL_MARKER, tick, rest)


# A first line that reads as a tick header but is not the canonical one:
# "Tick" and a time with a space in it, or no holder. Group 1 is the holder
# it names. Prose that only starts with the word ("Tick evidence: ...") is
# not a header.
_LOOSE_TICK = re.compile(r"Tick[ \t]+\d[^\n]*?(?:,[ \t]*([A-Za-z0-9._-]+))?[ \t]*(?:\r?\n|$)")


def as_journal(text: str, holder: Optional[str]) -> str:
    """`text` as a journal tick: a loom journal, report or reply comes back
    unchanged; plain prose gets the journal marker, so tick evidence edits
    the holder's journal of the day instead of becoming one more comment
    that emails the issue's watchers (loom#1241). Plain
    prose needs a holder, and any other HTML-comment marker is refused."""
    if text.startswith((JOURNAL_MARKER, REPORT_MARKER)) or REPLY_RE.match(text):
        return text
    if text.lstrip().startswith("<!--"):
        raise ValueError("comment must start with a loom journal, report or reply marker")
    if holder is None:
        raise ValueError("a comment with no loom marker is posted as a journal tick, which "
                         "needs --holder for its Tick line (docs/BRAIN.md)")
    return "%s\n%s" % (JOURNAL_MARKER, text.lstrip("\r\n"))


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


# GitHub refuses a comment over 65536 characters; an edited journal keeps
# under this by dropping its oldest ticks.
JOURNAL_MAX = 60000


def merged(new: str, old: str, limit: int = JOURNAL_MAX) -> str:
    """The journal `new` with the ticks of `old` (the holder's journal of the
    same day) below it under the Earlier block, newest first, dropping the
    oldest ticks until the whole fits `limit`."""
    earlier = [part[len(_MARKER):].strip("\n") for _, part in reversed(split_journal(old))]
    while earlier:
        body = "%s\n\n%s\n\n%s\n\n%s" % (new.rstrip("\n"), EARLIER_OPEN,
                                         "\n\n".join(earlier), EARLIER_CLOSE)
        if len(body) <= limit:
            return body
        earlier.pop()
    return new


def _self_login(gh: GitHub) -> str:
    """The login this client posts as: the App's bot login, else the token's
    user (read once and kept on the client)."""
    login = gh.app_login()
    if login:
        return login
    if getattr(gh, "_journal_login", None) is None:
        data = gh.rest("GET", "/user").data
        gh._journal_login = str((data or {}).get("login") or "") if isinstance(data, dict) else ""
    return gh._journal_login


def todays_journal(gh: GitHub, repo: str, issue_number: int,
                   text: str) -> Optional[Dict[str, Any]]:
    """This client's journal comment for the same holder on the same UTC day
    as `text`'s Tick line, the newest such; None when there is none. One read
    of the day's comments (since= filters on the last edit)."""
    tick = re.search(r"^Tick (\d{4}-\d{2}-\d{2})T\S*, (\S+)\s*$", text, re.M)
    if not tick:
        return None
    day, holder = tick.group(1), tick.group(2)
    me = _self_login(gh).lower()
    found = None
    for c in gh.paginate("/repos/%s/issues/%d/comments?since=%sT00:00:00Z&per_page=100"
                         % (repo, int(issue_number), day)):
        if _mine_today(c, me, day, holder) and (
                found is None or str(c.get("created_at")) > str(found.get("created_at"))):
            found = c
    return found


def _mine_today(c: Any, me: str, day: str, holder: str) -> bool:
    """Whether comment c is this client's (login `me`) journal for `holder`,
    created on UTC `day`: never another login's or another holder's."""
    if not me or not isinstance(c, dict) or c.get("id") is None:
        return False
    body = str(c.get("body") or "")
    return (str((c.get("user") or {}).get("login") or "").lower() == me
            and body.startswith(_MARKER)
            and str(c.get("created_at") or "")[:10] == day
            and journal_holder({"body": body}) == holder)


def post(gh: GitHub, repo: str, issue_number: int, text: str,
         holder: Optional[str] = None, trusted: Sequence[str] = (),
         now: Optional[datetime] = None):
    """Post a journal, report or reply. A journal edits this holder's journal
    of the same UTC day in place, newest tick first, and is posted new only
    for the day's first tick: each new comment emails the operator
    (2026-10-18). Plain prose with a holder is a journal tick (as_journal).
    A report is refused with ReportTooSoon, and nothing posted, when another
    report went out on the same goal home within REPORT_GAP (report_hold
    says whose count)."""
    text = as_journal(text, holder)
    if text.startswith(JOURNAL_MARKER):
        text = with_tick(text, holder)
        _text.check(text)
        mine = todays_journal(gh, repo, issue_number, text)
        if mine is not None:
            body = merged(text, str(mine.get("body") or ""))
            _text.check(body)
            return gh.rest("PATCH", "/repos/%s/issues/comments/%d" % (repo, int(mine["id"])),
                           {"body": body})
    _text.check(text)
    if text.startswith(REPORT_MARKER):
        _check_report_gap(gh, repo, issue_number, now or datetime.now(timezone.utc), trusted)
    return gh.rest("POST", "/repos/%s/issues/%d/comments" % (repo, int(issue_number)),
                   {"body": text})
