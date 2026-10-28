"""The journal and report comment formats from docs/BRAIN.md."""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, Optional, Sequence, Tuple, Union

from . import handoff
from . import notify as _notify
from . import quiet as _quiet
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


# A real goal's report on GitHub (2026-10-25, "info overload"): its first line,
# at most this many lines after it, then "Needs you:" and "Waiting on you:".
# check_report refuses a longer one.
REPORT_LINES_MAX = 5


class ReportTooSoon(ValueError):
    """A report not posted: another went out on the goal home within REPORT_GAP."""


class ReportInvalid(ValueError):
    """A report not posted: its Needs you / Waiting on you lines contradict
    each other (loom#1775)."""


class ReportBacklog(ValueError):
    """A report not posted: the goal home is a per-repo backlog goal, whose
    status is one line in the station's twice-daily digest, never a comment."""


class ReportQuiet(ValueError):
    """A report not posted: the goal is quiet since its last report
    (bus/quiet.py), and its heartbeat is not due or has no report to edit."""


def quiet_message(still: "_quiet.Quiet") -> str:
    return ("quiet since %s: nothing in the goal's scope moved since its last report, so "
            "no report is owed; its heartbeat edits that report in place from %s"
            % (_when(still.since), _when(still.next_heartbeat())))


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


_STEPS_LINE = re.compile(r"^[ \t]*-[ \t]+Steps:.*(?:\r?\n|$)", re.M)


def with_steps(text: str, line: str) -> str:
    """The journal tick `text` with the station's per-step durations as its
    last line (loom#2104), in place of any "- Steps:" line already there."""
    return "%s\n%s" % (_STEPS_LINE.sub("", text).rstrip("\n"), line)


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
    # A report edited in place counts from its edit (loom#1560).
    found = handoff.expand_journals([c for c in found if isinstance(c, dict)], at_key="created_at")
    held = report_hold(((str((c.get("user") or {}).get("login") or ""), c.get("created_at"),
                         c.get("body")) for c in found), now, trusted)
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
    """The login this client posts as: the App's bot login, else
    LOOM_GITHUB_LOGIN (brain.sh sets it for an agent that holds only an
    installation token, which GET /user refuses), else the token's user (read
    once and kept on the client)."""
    login = gh.app_login() or os.environ.get("LOOM_GITHUB_LOGIN", "").strip()
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
         now: Optional[datetime] = None,
         quiet: Optional[Callable[[], Optional["_quiet.Quiet"]]] = None,
         backlog: Optional[Callable[[], bool]] = None,
         steps: Optional[str] = None):
    """Post a journal, report or reply. A journal edits this holder's journal
    of the same UTC day in place, newest tick first, and is posted new only
    for the day's first tick: each new comment emails the operator
    (2026-10-18). Plain prose with a holder is a journal tick (as_journal).
    A report is refused with ReportTooSoon, and nothing posted, when another
    report went out on the same goal home within REPORT_GAP (report_hold
    says whose count). `quiet` reads whether the goal is quiet since its last
    report (brain.goal_quiet): a quiet goal's report is refused with
    ReportQuiet, and once its last report is quiet.HEARTBEAT_EVERY old the
    report is that week's heartbeat, edited into this client's newest
    report instead of posted (loom#1534). `backlog` says whether the goal
    home is a backlog goal, whose report is refused with ReportBacklog.
    `steps` is the tick's "- Steps:" line (tick_clock.footer), put last in
    a journal tick and never in a report or reply."""
    text = as_journal(text, holder)
    if text.startswith(JOURNAL_MARKER):
        text = with_tick(text, holder)
        if steps:
            text = with_steps(text, steps)
        _text.check(text)
        mine = todays_journal(gh, repo, issue_number, text)
        if mine is not None:
            body = merged(text, str(mine.get("body") or ""))
            _text.check(body)
            return gh.rest("PATCH", "/repos/%s/issues/comments/%d" % (repo, int(mine["id"])),
                           {"body": body})
    _text.check(text)
    if text.startswith(REPORT_MARKER):
        check_report(text)
        now = now or datetime.now(timezone.utc)
        _check_report_gap(gh, repo, issue_number, now, trusted)
        if backlog is not None and backlog():
            raise ReportBacklog("a backlog goal home posts no report; its status is one line "
                                "in the twice-daily digest")
        still = quiet() if quiet is not None else None
        if still is not None:
            if not still.heartbeat_due(now):
                raise ReportQuiet(quiet_message(still))
            return _heartbeat(gh, repo, issue_number, text, still, now)
        mine = _newest_report(gh, repo, issue_number) if _ask(text) is not None else None
        if mine is not None and _same_ask(text, str(mine.get("body") or "")):
            # Nothing new is asked of the operator: the report is edited into
            # the last one, as an edit sends no email (loom#1560).
            body = "%s\n%s" % (_REPORTED_RE.sub("", text).rstrip("\n"),
                                handoff.REPORTED_STAMP % _when(now))
            _text.check(body)
            return gh.rest("PATCH", "/repos/%s/issues/comments/%d" % (repo, int(mine["id"])),
                           {"body": body})
    if text.startswith(REPORT_MARKER):
        # Only an edit in place may carry an edit time (handoff.reported_at).
        text = _REPORTED_RE.sub("", text)
    return _notify.post(gh, repo, issue_number, text, _notify.JOURNAL)


# [ \t] and not \s: a "Needs you:" with nothing after it must not read the
# next line (the "Waiting on you:" line) as its ask.
_NEEDS_YOU = re.compile(r"needs you:[ \t]*(.*)$", re.I | re.M)
_WAITING = re.compile(r"^[ \t]*(?:[-*][ \t]+)?waiting on you:[ \t]*(.*)$", re.I | re.M)
_REPORTED_RE = re.compile(r"^<!-- loom:reported [^>]*-->[ \t]*\n?", re.M)
# A count of open loom:human issues: the label and a number on one line.
_COMMENT_LINE = re.compile(r"^[ \t]*<!--.*-->[ \t]*$")
_HUMAN_COUNT = re.compile(r"loom:human", re.I)
_NOTHING = re.compile(r"(?:nothing|none|no one|n/a)\b", re.I)


def waiting_line(by_repo: Dict[str, int], share: int = 0) -> str:
    """The report's "Waiting on you:" line: the open loom:human issues by
    repo, how many of them sit under this goal, and where they are listed
    (the daily digest). "Waiting on you: nothing" when there are none. It is
    its own line, apart from "Needs you:", so that "nothing" never sits next
    to a non-zero count and a moving count never reads as a new ask (loom#1775)."""
    counts = [(repo, int(n)) for repo, n in by_repo.items() if int(n) > 0]
    total = sum(n for _, n in counts)
    if total == 0:
        return "Waiting on you: nothing"
    counts.sort(key=lambda item: (-item[1], item[0]))
    return ("Waiting on you: %d open loom:human (%s; %d of them under this goal), listed in "
            "the daily digest" % (total, ", ".join("%d in %s" % (n, repo) for repo, n in counts),
                                  int(share)))


def check_report(text: str) -> None:
    """Raise ReportInvalid when a report's "Needs you:" line carries a count of
    loom:human issues, or its "Waiting on you:" line says nothing beside a
    number. On 2026-10-25 a report ended "Needs you: nothing. loom:human: 28 in
    <repo>.", and the operator could not tell whether anything was needed. The
    count belongs on the "Waiting on you:" line (waiting_line), and "Needs you:"
    names only what only the operator can do for this goal."""
    body = _REPORTED_RE.sub("", text)
    said = [line for line in body.splitlines()
            if line.strip() and not _COMMENT_LINE.match(line)
            and not _NEEDS_YOU.search(line) and not _WAITING.match(line)]
    if len(said) > 1 + REPORT_LINES_MAX:
        raise ReportInvalid(
            "the report has %d lines besides \"Needs you:\"; write its first line and at most "
            "%d more (one line each for going well, not going well, still stuck and next), then "
            "\"Needs you:\" and \"Waiting on you:\" (docs/MUSE.md)" % (len(said), REPORT_LINES_MAX))
    needs = _NEEDS_YOU.findall(body)
    if needs and _HUMAN_COUNT.search(needs[-1]) and re.search(r"\d", needs[-1]):
        says = " says nothing is needed and" if _NOTHING.match(needs[-1].strip()) else ""
        raise ReportInvalid(
            "the \"Needs you:\" line%s carries a loom:human count. \"Needs you:\" names only "
            "what only the operator can do for this goal, or \"nothing new for this goal\"; "
            "put the count on its own line after it, \"Waiting on you: N open loom:human (...)\" "
            "or \"Waiting on you: nothing\" (docs/MUSE.md)" % says)
    waiting = _WAITING.findall(body)
    if waiting and _NOTHING.match(waiting[-1].strip()) and re.search(r"\d", waiting[-1]):
        raise ReportInvalid(
            "the \"Waiting on you:\" line says nothing next to a count; write "
            "\"Waiting on you: nothing\" only when no loom:human issue is open")


def _ask(body: str) -> Optional[str]:
    """What a report asks of the operator: its "Needs you:" text, compared
    without case, spacing or a closing period; None when it has none. Only
    that line: the "Waiting on you:" line below it moves with the count of
    open loom:human issues and is not an ask (loom#1775)."""
    found = _NEEDS_YOU.findall(_REPORTED_RE.sub("", body))
    if not found:
        return None
    return " ".join(found[-1].lower().split()).rstrip(". ")


def _same_ask(new: str, old: str) -> bool:
    asked = _ask(new)
    return asked is not None and asked == _ask(old)


def _newest_report(gh: GitHub, repo: str, issue_number: int) -> Optional[Dict[str, Any]]:
    """This client's newest report comment on the goal home, or None."""
    me = _self_login(gh).lower()
    mine = None
    for c in gh.paginate("/repos/%s/issues/%d/comments?per_page=100" % (repo, int(issue_number))):
        if (isinstance(c, dict) and c.get("id") is not None and me
                and str((c.get("user") or {}).get("login") or "").lower() == me
                and str(c.get("body") or "").startswith(REPORT_MARKER)
                and (mine is None or str(c.get("created_at")) >= str(mine.get("created_at")))):
            mine = c
    return mine


def _heartbeat(gh: GitHub, repo: str, issue_number: int, text: str,
               still: "_quiet.Quiet", now: datetime):
    """A quiet goal's weekly heartbeat: `text` edited into this client's
    newest report on the goal home, never a new comment (an edit sends no
    email). With no report of this client's there, ReportQuiet."""
    me = _self_login(gh).lower()
    mine = _newest_report(gh, repo, issue_number)
    if mine is None:
        raise ReportQuiet("%s; %s has no report on this goal home to edit, and a heartbeat "
                          "never posts a new one" % (quiet_message(still), me or "this client"))
    body = _quiet.heartbeat(text, now)
    _text.check(body)
    return gh.rest("PATCH", "/repos/%s/issues/comments/%d" % (repo, int(mine["id"])),
                   {"body": body})
