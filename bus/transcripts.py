"""One finished run as the body the station uploads to the dashboard.

    {ticket, slot, model, outcome, started_at, finished_at,
     events: [{ts, role, kind, text}]}

kind is text, tool_use, tool_result or log; role is user, assistant,
thinking, tool or runner. The events come from the one transcript parser
(bus/sessions.py, which the viewer reads too): the CLI's own session record,
then the lane log of the run's last attempt. Every text is scrubbed of local
paths and credential shapes, each event is capped at EVENT_MAX characters, and
the body at BODY_MAX bytes, dropping the oldest tool results first.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import sessions
from . import text as _text

EVENT_MAX = 20000
BODY_MAX = 8 * 1024 * 1024
CUT = "\n[cut]"
REDACTED = "[redacted]"
# Redaction patterns for provider key shapes (none of them is a secret).
TOKENS = [
    # GitHub: classic, OAuth, app, refresh and fine-grained tokens.
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})"),
    # Anthropic, then OpenAI (sk-, sk-proj-, sk-svcacct-, sk-admin-).
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    # AWS: an access key id, and a 40-character base64 secret key (with a
    # digit, an upper and a lower case letter, so no word or git sha matches).
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"(?<![A-Za-z0-9+/])(?=[A-Za-z0-9+/]{0,39}[0-9])(?=[A-Za-z0-9+/]{0,39}[A-Z])"
               r"(?=[A-Za-z0-9+/]{0,39}[a-z])[A-Za-z0-9+/]{40}(?![A-Za-z0-9+/=])"),
    # Cloudflare: a 40-character API token of the same mix, and a 37-character
    # hex global API key.
    re.compile(r"(?<![A-Za-z0-9_-])(?=[A-Za-z0-9_-]{0,39}[0-9])(?=[A-Za-z0-9_-]{0,39}[A-Z])"
               r"(?=[A-Za-z0-9_-]{0,39}[a-z])[A-Za-z0-9_-]{40}(?![A-Za-z0-9_-])"),
    re.compile(r"\b[0-9a-f]{37}\b"),
    # Any bearer token.
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
]
# A home directory as Claude Code names a project after it (-Users-name-...).
MANGLED_HOME_RE = re.compile(r"(?<![A-Za-z0-9_.-])-(?:Users|home)-[A-Za-z0-9._-]+")
# The runner's line that starts an attempt (lanes/run_lane.sh).
# The dashboard files a run under its ticket, owner/name#N. A brief names it
# in its Issue: line; its file name (owner.name-N, with -rN on a retry) is the
# fallback. A brain tick is filed as brain/<UTC start>.
ISSUE_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[0-9]+$")
BRIEF_NAME_RE = re.compile(r"^([A-Za-z0-9-]+)\.([A-Za-z0-9_.-]+?)-([0-9]+)(?:-r[0-9]+)?$")
# A brief with no Model: line ran on its lane's default; the dashboard needs
# a non-empty model, and a guessed name would be fabricated.
MODEL_MISSING = "not recorded"
START_RE = re.compile(r"^\[(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)\] \S+ start, lane ", re.M)
ROLES = {"user": "user", "assistant": "assistant", "thinking": "thinking"}


def scrub(text: str) -> str:
    """The text without local paths or credential shapes."""
    text = MANGLED_HOME_RE.sub("<path>", _text.scrub_paths(text))
    for pattern in TOKENS:
        text = pattern.sub(REDACTED, text)
    return text


def _cap(text: str, tail: bool = False) -> str:
    """At most EVENT_MAX characters: the head, or for a log the tail (where
    the outcome is)."""
    if len(text) <= EVENT_MAX:
        return text
    keep = EVENT_MAX - len(CUT)
    return CUT.lstrip("\n") + "\n" + text[-keep:] if tail else text[:keep] + CUT


def _input_text(tool: str, inp: Any) -> str:
    """A tool call as text: its name, then each input as key: value."""
    if not isinstance(inp, dict):
        return "%s: %s" % (tool, inp if isinstance(inp, str) else json.dumps(inp))
    lines = [tool]
    for key, value in inp.items():
        lines.append("%s: %s" % (key, value if isinstance(value, str) else json.dumps(value)))
    return "\n".join(lines)


def _event(ts: Optional[str], role: str, kind: str, text: str) -> Dict[str, Any]:
    # A log line with no time of its own sends "": the dashboard takes only
    # strings, and refused a whole run for one null ts (2026-10-14).
    return {"ts": ts or "", "role": role, "kind": kind,
            "text": _cap(scrub(text), tail=kind == "log")}


def convert(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Parser events (bus/sessions.py) as upload events: a tool call becomes
    a tool_use and, when it has output, a tool_result."""
    out = []
    for e in events:
        kind, ts = e.get("kind"), e.get("ts")
        if kind in ROLES:
            out.append(_event(ts, ROLES[kind], "text", str(e.get("text") or "")))
        elif kind == "tool":
            tool = str(e.get("tool") or "?")
            out.append(_event(ts, "assistant", "tool_use", _input_text(tool, e.get("input") or {})))
            if e.get("output"):
                out.append(_event(ts, "tool", "tool_result", str(e["output"])))
        elif kind == "log":
            out.append(_event(ts, "runner", "log", str(e.get("text") or "")))
    return out


def _size(value: Any) -> int:
    return len(json.dumps(value).encode("utf-8"))


def limit(body: Dict[str, Any]) -> Dict[str, Any]:
    """The body at most BODY_MAX bytes as JSON: the oldest tool results go
    first, then, if it is still too big, the oldest events of any kind."""
    events = body["events"]
    total = _size(dict(body, events=[])) + sum(_size(e) + 1 for e in events)
    if total <= BODY_MAX:
        return body
    keep = [True] * len(events)
    order = ([i for i, e in enumerate(events) if e["kind"] == "tool_result"]
             + [i for i, e in enumerate(events) if e["kind"] != "tool_result"])
    for i in order:
        if total <= BODY_MAX:
            break
        keep[i] = False
        total -= _size(events[i]) + 1
    return dict(body, events=[e for e, k in zip(events, keep, strict=True) if k])


def _utc(at: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))


def ticket_ref(name: str, issue: str = "") -> str:
    """owner/name#N for a brief: its Issue: line when that is well formed,
    else parsed from the brief's file name. ValueError when neither is."""
    issue = (issue or "").strip()
    if ISSUE_RE.match(issue):
        return issue
    m = BRIEF_NAME_RE.match(name)
    if not m:
        raise ValueError("brief %s names no ticket" % name)
    return "%s/%s#%s" % m.groups()


def run_body(home: Path, slot: str, ticket: str, outcome: str, model: str,
             claude_projects: Path, codex_sessions: Path, issue: str = "") -> Dict[str, Any]:
    """A lane run (briefs/<slot>/done|failed/<ticket>.md) as an upload body,
    filed under the ticket the brief's Issue: line names (ticket_ref).
    Raises ValueError for an unsafe slot or ticket name."""
    events = sessions.session_events(slot, ticket, home, claude_projects, codex_sessions)
    log = home / "logs" / slot / ("%s.log" % ticket)
    started, finished = None, None
    for e in sessions.log_events(log):
        # The log holds every attempt on this slot; this run is the last.
        starts = list(START_RE.finditer(e["text"]))
        if starts:
            started = starts[-1].group(1)
            e = dict(e, text=e["text"][starts[-1].start():])
        events.append(e)
        finished = _utc(log.stat().st_mtime)
    if finished is None:
        brief = home / "briefs" / slot / outcome / ("%s.md" % ticket)
        finished = _utc(brief.stat().st_mtime if brief.exists() else time.time())
    # A log with no start line (an older runner, or one cut short) still
    # gives the run a start: the dashboard refuses a body without one.
    started = started or finished
    return limit({"ticket": ticket_ref(ticket, issue), "slot": slot,
                  "model": model or MODEL_MISSING, "outcome": outcome,
                  "started_at": started, "finished_at": finished, "events": convert(events)})


def brain_body(home: Path, started: float, finished: float, outcome: str, model: str,
               claude_projects: Path) -> Dict[str, Any]:
    """A brain tick (lanes/mac/brain.sh) as an upload body: the Claude Code
    sessions run in $LOOM_HOME/brain and last written during the tick (its
    end is whole seconds, so a second later still counts)."""
    work = home / "brain"
    events = sessions.claude_dir(sessions.project_dir(claude_projects, work), cwd=str(work),
                                 since=started, until=finished + 1)
    return limit({"ticket": "brain/" + _utc(started), "slot": "brain", "model": model, "outcome": outcome,
                  "started_at": _utc(started), "finished_at": _utc(finished),
                  "events": convert(events)})
