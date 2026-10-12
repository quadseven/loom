"""One finished run as the dashboard's upload body (docs/LANES.md, "Transcripts").

    {ticket, slot, model, outcome, started_at, finished_at,
     events: [{ts, role, kind, text}]}

kind is text, tool_use, tool_result or log; role is user, assistant,
thinking, tool or runner. The run is read through bus/sessions.py, the
parser the local viewer uses. Every text is scrubbed before it leaves the
machine: key shapes become [redacted], the run's worktree becomes ".", and
any other absolute path becomes <path> (the station's scrub). Each event is
capped at EVENT_MAX characters and the body at BODY_MAX bytes of JSON, which
drops the oldest tool results first.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import sessions
from .station import scrub_paths

EVENT_MAX = 20000
BODY_MAX = 8 * 1024 * 1024
REDACTED = "[redacted]"
TRUNCATED = "\n[truncated]"
# Read at most this much of a log (the brain's log grows across ticks).
LOG_READ_MAX = 16 * 1024 * 1024
_NOT_IN_A_WORD = r"(?<![A-Za-z0-9_-])"
# Key shapes, each a whole match, or a named setting whose value (group 2)
# is replaced and whose name (group 1) is kept.
TOKENS = [re.compile(_NOT_IN_A_WORD + p) for p in (
    r"gh[pousr]_[A-Za-z0-9]{30,}",                     # GitHub
    r"github_pat_[A-Za-z0-9_]{30,}",                   # GitHub fine-grained
    r"sk-ant-[A-Za-z0-9_-]{20,}",                      # Anthropic
    r"sk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}",  # OpenAI
    r"(?:AKIA|ASIA)[0-9A-Z]{16}(?![0-9A-Z])",          # AWS access key id
    r"cf[a-z]{1,3}_[A-Za-z0-9_-]{30,}",                # Cloudflare (prefixed)
)]
SETTINGS = [re.compile(p, re.I) for p in (
    # AWS secret keys and session tokens, Cloudflare tokens and keys: plain
    # base64-ish strings, so only recognisable by the name in front.
    r"((?:aws_secret_access_key|aws_session_token|secretaccesskey|sessiontoken)"
    r"[\"']?\s*[:=]\s*[\"']?)([A-Za-z0-9/+=]{16,})",
    r"((?:cloudflare|cf)[A-Za-z_]*(?:token|key)[\"']?\s*[:=]\s*[\"']?)([A-Za-z0-9_-]{30,})",
    r"(\bbearer\s+)([A-Za-z0-9._~+/=-]{16,})",
    r"(x-api-key[\"']?\s*[:=]\s*[\"']?)([A-Za-z0-9._-]{16,})",
)]
START_RE = re.compile(r" start, lane [a-z0-9-]+(?:, model ([A-Za-z0-9._:/@-]+))?\s*$", re.M)
MODEL_RE = re.compile(r"^Model:[ \t]*([A-Za-z0-9._:/@-]+)[ \t]*$", re.M)
MODEL_ID = re.compile(r"^[A-Za-z0-9._:][A-Za-z0-9._:/@-]*$")


def redact(text: str) -> str:
    for rx in TOKENS:
        text = rx.sub(REDACTED, text)
    for rx in SETTINGS:
        text = rx.sub(lambda m: m.group(1) + REDACTED, text)
    return text


def clean(text: str, tree: Optional[str] = None) -> str:
    """Keys redacted, the worktree as ".", other paths scrubbed, capped."""
    text = redact(text)
    if tree:
        text = re.sub(re.escape(tree) + r"(?![A-Za-z0-9._-])", ".", text)
    text = scrub_paths(text)
    if len(text) > EVENT_MAX:
        text = text[:EVENT_MAX - len(TRUNCATED)] + TRUNCATED
    return text


def utc(at: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))


def upload_events(events: List[Dict[str, Any]], tree: Optional[str] = None,
                  start: Optional[str] = None) -> List[Dict[str, Any]]:
    """Parser events as upload events. An event without a time takes the one
    before it (or the run's start)."""
    out: List[Dict[str, Any]] = []
    last = start

    def add(ts: Optional[str], role: str, kind: str, text: str) -> None:
        nonlocal last
        last = ts or last
        if text.strip():
            out.append({"ts": last, "role": role, "kind": kind, "text": clean(text, tree)})

    for e in events:
        kind, ts = e.get("kind"), e.get("ts")
        if kind in ("user", "assistant", "thinking"):
            add(ts, kind, "text", str(e.get("text") or ""))
        elif kind == "tool":
            inp = e.get("input")
            add(ts, "assistant", "tool_use",
                "%s %s" % (e.get("tool") or "?", json.dumps(inp, sort_keys=True)))
            add(e.get("output_ts"), "tool", "tool_result", str(e.get("output") or ""))
        elif kind == "log":
            add(ts, "runner", "log", str(e.get("text") or ""))
    return out


def fit(body: Dict[str, Any], limit: int = BODY_MAX) -> Dict[str, Any]:
    """Drop events, oldest tool results first, then the oldest of the rest,
    until the body's JSON is at most `limit` bytes."""
    events = body["events"]
    # json.dumps separates list items with ", ".
    sizes = [len(json.dumps(e)) + 2 for e in events]
    total = len(json.dumps(dict(body, events=[]))) + sum(sizes)
    if total <= limit:
        return body
    order = ([i for i, e in enumerate(events) if e["kind"] == "tool_result"]
             + [i for i, e in enumerate(events) if e["kind"] != "tool_result"])
    drop = set()
    for i in order:
        if total <= limit:
            break
        drop.add(i)
        total -= sizes[i]
    return dict(body, events=[e for i, e in enumerate(events) if i not in drop])


def make_body(ticket: str, slot: str, model: str, outcome: str,
              started_at: Optional[str], finished_at: Optional[str],
              events: List[Dict[str, Any]]) -> Dict[str, Any]:
    return fit({"ticket": ticket, "slot": slot, "model": model, "outcome": outcome,
                "started_at": started_at, "finished_at": finished_at, "events": events})


def _read_tail(path: Path) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - LOG_READ_MAX))
            data = fh.read()
    except OSError:
        return ""
    if size > LOG_READ_MAX:
        data = data[data.find(b"\n") + 1:]  # start on a whole line
    return data.decode("utf-8", errors="replace")


def _ends(events: List[Dict[str, Any]]):
    times = [e["ts"] for e in events if e.get("ts")]
    return (times[0], times[-1]) if times else (None, None)


def run_body(loom: str, slot: str, ticket: str, outcome: str, brief: str,
             user_home: str, finished_fallback: float) -> Optional[Dict[str, Any]]:
    """The body for one lane run whose brief (text `brief`) is in done/ or
    failed/, or None when the run left no record at all."""
    root, home = Path(loom), Path(user_home)
    session = sessions.session_events(slot, ticket, root, home / ".claude" / "projects",
                                      home / ".codex" / "sessions")
    log_text = _read_tail(sessions.log_path(slot, ticket, root))
    logs = sessions.log_events(log_text)
    if not session and not logs:
        return None
    m = MODEL_RE.search(brief)
    model = m.group(1) if m else ""
    started = None
    for e in logs:
        start = START_RE.search(e["text"])
        if start:
            started = e["ts"]
            model = model or (start.group(1) or "")
            break
    first, last = _ends(session + logs)
    started = started or first
    finished = _ends(logs)[1] or last or utc(finished_fallback)
    tree = str(root / "worktrees" / ticket)
    return make_body(ticket, slot, model, outcome, started, finished,
                     upload_events(session + logs, tree, started))


def brain_body(loom: str, user_home: str, started: float, finished: float,
               outcome: str, model: Any) -> Dict[str, Any]:
    """The body for one brain tick: its Claude Code sessions (files written
    since the tick started) and its lines of brain.log. The model comes from
    state/brain.json, so anything but a model id is dropped."""
    model = model if isinstance(model, str) and MODEL_ID.match(model) else ""
    work = os.path.join(loom, "brain")
    project = sessions.claude_project(Path(user_home) / ".claude" / "projects", work)
    session = sessions.claude_dir(project, since=started - 5)
    lo, hi = utc(started - 120), utc(finished + 60)
    logs = [e for e in sessions.log_events(_read_tail(Path(loom) / "logs" / "brain.log"))
            if e["ts"] and lo <= e["ts"] <= hi]
    start = utc(started)
    return make_body("brain/" + start, "brain", model, outcome, start, utc(finished),
                     upload_events(session + logs, work, start))
