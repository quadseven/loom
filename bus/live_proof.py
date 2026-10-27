"""The read-only proof a shipped ticket names, run once its window has passed.

Loom stopped at the merge: in one product's backlog 62 open tickets still
carried merged, shipped or verified and 130 awaited a live check, and an
overnight sweep of 80 of them found about half already built, needing only
their live proof read and the ticket closed (#1608). The close gate
(bus/criteria_gate.py) refuses a close without evidence; this module goes
and gets the evidence.

Each live criterion of the ticket names at most one proof step (`step_of`):

- log: the criterion's backticked or double-quoted strings, searched in the
  log from the deploy on, once the window the criterion names ("within 24
  hours", "over 3 days") has passed after the deploy. The lines that match
  are quoted, never more than evidence.MAX_LINES and only safe ones
  (evidence.safe_line).
- url, gh or kubectl: the read-only checks bus/criteria_gate.py runs.
- query: one SELECT the criterion gives, read through the station's query
  source.
- an adapter step, the templated form a repo's live-evidence adapter
  answers (bus/live_adapter.py, loom#1770): a `log:<source>` span with the
  strings that must appear in that log since the deploy (or must not, over
  the window named), or a `sql:<SELECT ...>` span with the strings its
  answer must contain. The target repo declares how each is read; Loom
  knows only the form.

A step that would write is refused, not run (`refusal`): a `gh api` call
with a method or a field, a kubectl verb other than get or rollout status, a
ship, a roll, a restart, a deploy or any other command, and SQL other than
one plain SELECT. A refused step is no evidence; it never runs to make some.
The runner checks again just before it runs a step (`run`), so a step built
by hand is refused too.

`prove` runs the due steps and asks the close gate (criteria_gate.unverified)
whether every criterion now has evidence. A proven ticket closes quoting it
(`close_note`); any other gets one status line naming what is still missing
and when the station looks again (`status_line`).
"""
from __future__ import annotations

import re
import shlex
import time
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

from . import criteria_gate, evidence

LOG = "log"
QUERY = "query"
# A failed or missing proof is read again after this long, as the station's
# live checks are (station.LIVE_CHECK_RETRY).
RETRY = 6 * 3600
STATUS_WIDTH = 160
# The first word of a span that names a command, not a log string. Only the
# shapes criteria_gate._command accepts run; every other one is refused. A
# word that is also plain English (make) counts only as `make <target>`.
_COMMANDS = frozenset((
    "ansible", "ansible-playbook", "aws", "bash", "curl", "docker", "gh", "git", "helm",
    "kubectl", "launchctl", "mysql", "npm", "psql", "pulumi", "python", "python3",
    "sh", "sqlite3", "ssh", "sudo", "systemctl", "terraform", "wget", "zsh"))
_MAKE_RE = re.compile(r"^make(?:\s+-\S+)*\s+[\w./-]+$")
# A span shaped like SQL: a statement keyword with the words that follow it.
_SQL_RE = re.compile(
    r"^\s*(?:(?:select|with)\b.*\bfrom\b|insert\s+into\b|update\s+\S+\s+set\b"
    r"|delete\s+from\b|(?:drop|alter|create|truncate)\s+(?:table|index|view|database"
    r"|schema|trigger)\b|(?:grant|revoke)\b.*\bon\b|(?:pragma|attach|vacuum)\b)", re.I | re.S)
_SQL_WRITE_RE = re.compile(
    r"\b(?:insert|update|delete|drop|alter|create|truncate|grant|revoke|replace|merge"
    r"|pragma|attach|detach|vacuum|reindex|into|lock|call|exec|execute|copy|set)\b", re.I)


class Step(NamedTuple):
    """One read-only proof step: its kind (log, query, url, gh or kubectl),
    the target (a log string, a SELECT, a URL, an API path or an argv
    string), the strings the answer must contain, and the hours after the
    deploy it waits before it runs. A log step read through a repo's
    adapter names its `source` (the log the adapter serves) and may name
    `absent` strings that must not appear; its target is "" when it names
    only absent ones."""
    kind: str
    target: str
    expects: Tuple[str, ...]
    hours: float
    source: str = ""
    absent: Tuple[str, ...] = ()


class Answer(NamedTuple):
    """What a repo's live-evidence adapter answered (bus/live_adapter.py):
    the lines it printed (None when it gave no answer, with `error` saying
    why), the hours of log it says it read (`loom-evidence-window-hours:`),
    and the matches it says it counted (`loom-evidence-count:`), each None
    when it said nothing."""
    lines: Optional[List[str]]
    hours: Optional[float] = None
    count: Optional[int] = None
    error: str = ""


class Sources(NamedTuple):
    """Where the steps read from. `log(literal, since)` returns the log
    lines since the epoch time `since` that contain `literal`, or None when
    the source gave no answer; `query(sql)` returns the rows as text lines,
    or None. Either may be None when the station has no such source.
    `source_log(source, literal, since)` and `sql(statement)` read through
    the repo's live-evidence adapter and return an Answer; None when the
    repo declares no adapter for them."""
    log: Optional[Callable[[str, float], Optional[List[str]]]] = None
    query: Optional[Callable[[str], Optional[List[str]]]] = None
    gh: Any = None
    fetch: Optional[Callable[[str, int], Tuple[int, str, str]]] = None
    runner: Optional[Callable[..., Any]] = None
    source_log: Optional[Callable[[str, str, float], Answer]] = None
    sql: Optional[Callable[[str], Answer]] = None


class Outcome(NamedTuple):
    """What a proof pass found. `evidence` maps each passed criterion's
    text to what was seen (with the quoted lines); `missing` lists
    (criterion, why) for each one still without evidence; `next_at` is when
    the station looks again, None when no proof Loom can run will change
    the answer (a person must act)."""
    proven: bool
    evidence: Dict[str, str]
    missing: List[Tuple[str, str]]
    next_at: Optional[float]


def _spans(text: str) -> List[str]:
    return [a or b for a, b in evidence.NEEDLE_RE.findall(text or "")]


def _command_like(span: str) -> bool:
    try:
        first = shlex.split(span)[:1]
    except ValueError:
        first = span.split()[:1]
    return (bool(first and first[0] in _COMMANDS) or bool(_MAKE_RE.match(span))
            or bool(_SQL_RE.match(span)))


def _select(span: str) -> bool:
    """Whether a span is one plain SELECT (or WITH ... SELECT) and nothing
    that writes: no second statement, no INTO, no write keyword."""
    body = span.strip().rstrip(";").strip()
    return (bool(re.match(r"^(?:select|with)\b", body, re.I)) and bool(_SQL_RE.match(body))
            and ";" not in body and not _SQL_WRITE_RE.search(body))


# The SHOW forms a read-only SQL step may use; nothing after them can write.
_SHOW_RE = re.compile(
    r"^show\s+(?:full\s+)?(?:databases|schemas|tables|columns|fields|index|indexes|keys"
    r"|create\s+table|table\s+status|grants|variables|status|processlist)\b[^;]*$", re.I)
# A source name a `log:` span may give.
SOURCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
MAX_SQL = 1000
_LOG_PREFIX = "log:"
_SQL_PREFIX = "sql:"


def sql_refusal(statement: str) -> str:
    """Why a statement may not run as a proof, or "" when it is one plain
    SELECT (or WITH ... SELECT) or one SHOW: no second statement, nothing
    that writes. The adapter runner asks again just before it runs."""
    body = statement.strip().rstrip(";").strip()
    if not body.isascii() or len(body) > MAX_SQL or any(
            ord(char) < 32 and char not in "\t\n" for char in body):
        return "the statement is not plain ASCII of at most %d characters" % MAX_SQL
    if _select(body) or (_SHOW_RE.match(body) and ";" not in body):
        return ""
    return "`%s` is not one read-only SELECT or SHOW" % " ".join(body.split())[:STATUS_WIDTH]


def _adapter_step(text: str, spans: List[str], hours: float) -> Tuple[Optional[Step], str]:
    """(the adapter step a criterion names in the templated form, or None;
    why it has none). ("", "") when it uses no template."""
    for span in spans:
        if span.lower().startswith(_SQL_PREFIX):
            statement = span[len(_SQL_PREFIX):].strip()
            why = sql_refusal(statement)
            if why:
                return None, why
            rest = tuple(s for s in spans if s != span)
            return Step(QUERY, statement, rest, hours, source="adapter"), ""
        for span in spans:
            if span.lower().startswith(_LOG_PREFIX):
                source = span[len(_LOG_PREFIX):].strip()
                if not SOURCE_RE.match(source):
                    return None, "`%s` names no log source a repo adapter can serve" % span
                absent = tuple(n for n in evidence.absent_needles(text) if n != span)
                present = [n for n in evidence.needles(text) if n != span and n not in absent]
                if not present and not absent:
                    return None, "`%s` names no string to find in that log" % span
                # Templates are strings with angle brackets or ellipsis. Use the first non-template
                # present needle as the search target.
                target = ""
                for p in present:
                    if not (p.startswith('<') or p.endswith('>') or p == '...'):
                        target = p
                        break
                if not target:
                    # All present needles are templates, which should not happen in valid tickets.
                    # Fall back to the first one, which will fail during execution.
                    if present:
                        target = present[0]
                    elif not absent:
                        return None, "`%s` names no string to find in that log" % span
                return Step(LOG, target, tuple(present[1:]), hours, source=source,
                            absent=absent), ""
    return None, ""


def refusal(span: str) -> str:
    """Why a span names a proof step that is not read-only, or "" when it is
    read-only or names no command at all (a plain log string)."""
    span = span.strip()
    if not _command_like(span):
        return ""
    if _SQL_RE.match(span):
        return "" if _select(span) else "`%s` is not one read-only SELECT" % span
    if criteria_gate._command(span) is not None:
        return ""
    return ("`%s` is not a read-only step: only a `gh api` GET, `kubectl get`, `kubectl "
            "rollout status` or one SELECT runs, never a write, ship, roll or restart" % span)


def step_of(text: str) -> Tuple[Optional[Step], str]:
    """(the proof step a live criterion names, or None; why it has none):
    a refused step names its refusal, a criterion with no step says so."""
    spans = _spans(text)
    for span in spans:
        why = refusal(span)
        if why:
            return None, why
    hours = evidence.required_span(text) or 0.0
    step, why = _adapter_step(text, spans, hours)
    if step is not None or why:
        return step, why
    for span in spans:
        if _select(span):
            return Step(QUERY, span.strip(), tuple(s for s in spans if s != span), hours), ""
    check = criteria_gate.check_of(text)
    if check is not None:
        return Step(check.kind, check.target, check.expects, hours), ""
    if evidence.absent_needles(text):
        # An absence is proven by a whole window answering, which the verify
        # run's absence section reads (station._no_live_evidence).
        return None, "it names a string that must be absent, read by the verify run"
    found = evidence.needles(text)
    if found:
        return Step(LOG, found[0], tuple(found[1:]), hours), ""
    return None, "no proof Loom can run"


def _step_refusal(step: Step) -> str:
    if step.kind == LOG and step.source and not SOURCE_RE.match(step.source):
        return "`%s` is not a log source name" % step.source
    if step.kind == LOG:
        return refusal(step.target) or next(
            (refusal(n) for n in step.expects + step.absent if refusal(n)), "")
    if step.kind == QUERY and step.source:
        return sql_refusal(step.target)
    if step.kind == QUERY:
        return "" if _select(step.target) else "`%s` is not one read-only SELECT" % step.target
    if step.kind == "url":
        return "" if criteria_gate.URL_RE.fullmatch(step.target) else \
            "`%s` is not an https URL" % step.target
    if step.kind == "gh":
        return refusal("gh api %s" % step.target)
    if step.kind == "kubectl":
        return refusal(step.target) or ("" if step.target.startswith("kubectl ") else
                                        "`%s` is not a kubectl read" % step.target)
    return "a proof step of kind %r is not read-only" % step.kind


def _matching(lines: Sequence[str], needle: str) -> List[str]:
    _, pattern = evidence.needle_pattern(needle)
    return [line for line in lines if (pattern.search(line) if pattern else needle in line)]


def _absence(step: Step, needle: str, answer: Answer, lines: List[str],
             safe: List[str], count: int, since: float, now: float) -> Tuple[bool, str]:
    """(whether an absent string is shown absent, what was seen): no match
    over a window the adapter says it read in full, at least the hours the
    criterion names, else all of the time since the deploy."""
    needed = step.hours if step.hours > 0 else max(0.0, (now - since) / 3600)
    if answer.hours is None:
        return False, ("the `%s` log adapter did not say how much log it read, so "
                       "`%s` cannot be shown absent" % (step.source, needle))
    if answer.hours + 1e-6 < needed:
        return False, ("the `%s` log adapter read %.1f hours, short of the %.1f "
                       "needed to show `%s` absent" % (step.source, answer.hours, needed, needle))
    if count or lines:
        return False, "the `%s` log names `%s` %d times since the deploy%s" % (
            step.source, needle, max(count, len(lines)), _quoted(safe))
    return True, "`%s` absent from the `%s` log over the %.1f hours read" % (
        needle, step.source, answer.hours)


def _quoted(safe: List[str]) -> str:
    return "".join("\n    > " + line for line in safe[:evidence.MAX_LINES])


def _source_needle(step: Step, needle: str, since: float, now: float,
                    sources: Sources) -> Tuple[bool, str]:
    """(passed, what was seen) for one string of an adapter log step."""
    literal, pattern = evidence.needle_pattern(needle)
    if not literal:
        return False, "`%s` is too general to search" % needle
    answer = sources.source_log(step.source, literal, since)
    if answer.lines is None:
        return False, "the `%s` log adapter gave no answer for `%s` (%s)" % (
            step.source, needle, answer.error or "no reason given")
    # A line stamped before the deploy is never its evidence.
    candidates = [line for line in answer.lines
                  if (evidence.line_time(line) or since) >= since]
    lines = _matching(candidates, needle)
    safe = [line for line in lines if evidence.safe_line(line)]
    count = answer.count if answer.count is not None else len(lines)
    if pattern is not None and answer.count is not None and answer.count > len(candidates):
        return False, ("the `%s` log adapter counted %d candidates but returned only %d "
                       "sample lines to check `%s`" % (
                           step.source, answer.count, len(candidates), needle))
    elif pattern is not None:
        count = len(lines)
    if needle in step.absent:
        return _absence(step, needle, answer, lines, safe, count, since, now)
    # A templated string must match the full template, not just the fixed piece.
    if pattern is None:
        # No template parts - use the count unchanged
        if not count and not safe:
            return False, "no `%s` log line since the deploy names `%s`" % (step.source, needle)
        return True, "`%s` named %d times in the `%s` log since the deploy%s" % (
            needle, count, step.source, _quoted(safe))
    if not safe:
        return False, "no `%s` log line since the deploy names `%s`" % (step.source, needle)
    return True, "`%s` named %d times in the `%s` log since the deploy%s" % (
        needle, len(safe), step.source, _quoted(safe))


def _source_log_step(step: Step, since: float, now: float,
                     sources: Sources) -> Tuple[bool, str]:
    """A log step read through the repo's adapter: each wanted string must
    have a match since the deploy (a quoted safe line or a count the adapter
    gives), and each absent one none (_absence)."""
    if sources.source_log is None:
        return False, "the repo declares no live-evidence log adapter"
    wanted = ((step.target,) if step.target else ()) + step.expects
    seen: List[str] = []
    for needle in wanted + step.absent:
        ok, what = _source_needle(step, needle, since, now, sources)
        if not ok:
            return False, what
        seen.append(what)
    return True, "read through the repo's live-evidence adapter: " + "; ".join(seen)


def _log_step(step: Step, since: float, sources: Sources) -> Tuple[bool, str]:
    if sources.log is None:
        return False, "the station has no log source"
    wanted = (step.target,) + step.expects
    quoted: List[str] = []
    for needle in wanted:
        literal, _ = evidence.needle_pattern(needle)
        if not literal:
            return False, "`%s` is too general to search" % needle
        lines = sources.log(literal, since)
        if lines is None:
            return False, "the log source gave no answer for `%s`" % needle
        safe = [line for line in _matching(lines, needle) if evidence.safe_line(line)]
        if not safe:
            return False, "no log line since the deploy names `%s`" % needle
        quoted.extend("    > " + line for line in safe[:evidence.MAX_LINES])
    return True, "\n".join(["log lines since the deploy name %s:" % ", ".join(
        "`%s`" % n for n in wanted)] + quoted)


def _query_step(step: Step, sources: Sources) -> Tuple[bool, str]:
    if sources.sql is not None:
        answer = sources.sql(step.target)
        if answer.lines is None:
            return False, "the repo's SQL adapter gave no answer (%s)" % (
                answer.error or "no reason given")
        rows = [row for row in answer.lines if evidence.safe_line(row)]
    elif step.source:
        return False, "the repo declares no live-evidence SQL adapter"
    elif sources.query is None:
        return False, "the station has no query source"
    else:
        found = sources.query(step.target)
        if found is None:
            return False, "the query source gave no answer"
        rows = found
    text = "\n".join(rows)
    missing = [e for e in step.expects if e not in text]
    if missing or not rows:
        return False, "the query answered %d rows, missing %s" % (
            len(rows), ", ".join("`%s`" % m for m in missing) or "any row")
    return True, "the query answered %d rows%s" % (len(rows), "".join(
        ", found `%s`" % e for e in step.expects))


def run(step: Step, since: float, sources: Sources,
        now: Optional[float] = None) -> Tuple[bool, str]:
    """(passed, what was seen) for one step, refused without running when it
    is not read-only. What was seen quotes safe log lines and otherwise names
    only the status and the strings found or missing."""
    why = _step_refusal(step)
    if why:
        return False, "refused: %s" % why
    if step.kind == LOG and step.source:
        return _source_log_step(step, since, time.time() if now is None else now, sources)
    if step.kind == LOG:
        return _log_step(step, since, sources)
    if step.kind == QUERY:
        return _query_step(step, sources)
    check = criteria_gate.Check(step.kind, step.target, step.expects)
    return criteria_gate.run(check, sources.gh, fetch=sources.fetch, runner=sources.runner)


def prove(body: str, deployed_at: float, sources: Sources, now: Optional[float] = None,
          passed: Sequence[str] = ()) -> Outcome:
    """Run each live criterion's due proof step and ask the close gate
    whether every criterion now has evidence. `deployed_at` is when the
    ticket's change went live; a step waits until its window has passed
    after it. `passed` holds the keys of criteria the station already
    has evidence for (criteria_gate.unverified)."""
    now = time.time() if now is None else now
    seen: Dict[str, str] = {}
    why_not: Dict[str, str] = {}
    later: List[float] = []
    for found, text in criteria_gate.needing_evidence(body):
        if found == criteria_gate.OPERATOR:
            why_not[text] = "an operator action"
            continue
        step, why = step_of(text)
        if step is None:
            why_not[text] = why
            continue
        due = deployed_at + step.hours * 3600
        if now < due:
            why_not[text] = "its window has not passed"
            later.append(due)
            continue
        ok, what = run(step, deployed_at, sources, now=now)
        if ok:
            seen[text] = what
        else:
            why_not[text] = what
            later.append(now + RETRY)
    passed_keys = list(passed) + [criteria_gate.key(t) for t in seen]
    gaps = criteria_gate.unverified(body, passed_keys)
    missing = [(t, why_not.get(t, "no evidence")) for _, t in gaps]
    # A wait that ends is worth a look; a refusal or an operator action is not.
    return Outcome(not gaps, seen, missing, min(later) if later and missing else None)


def _when(at: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))


def status_line(outcome: Outcome) -> str:
    """The one status line for a ticket whose proof is not met: the first
    missing criterion and why, how many more, and when the station looks
    again; "" for a proven ticket."""
    if outcome.proven or not outcome.missing:
        return ""
    text, why = outcome.missing[0]
    more = " and %d more" % (len(outcome.missing) - 1) if len(outcome.missing) > 1 else ""
    first_line = why.splitlines()[0] if why else why
    after = ("next check %s" % _when(outcome.next_at) if outcome.next_at is not None
             else "no further check: a person decides")
    return "Live proof missing: %s (%s)%s; %s." % (
        " ".join(text.split())[:STATUS_WIDTH], first_line, more, after)


def close_note(outcome: Outcome) -> str:
    """The close comment of a proven ticket, quoting each live criterion's
    evidence."""
    lines = ["Every acceptance criterion has evidence; the station read each live one "
             "itself, read-only:"]
    for text, what in outcome.evidence.items():
        lines.append("- %s: %s" % (" ".join(text.split())[:STATUS_WIDTH], what))
    return "\n".join(lines)
