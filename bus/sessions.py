"""Read each lane's own session record into one list of chat events.

The one parser for transcripts: the local viewer (lanes/viewer/serve.py) and
the station's uploads (bus/transcripts.py) both read through it.

Event kinds: user, assistant, thinking, tool (with tool, input, output), log.
Every event carries ts, an ISO 8601 UTC time or None when the record has
none; a tool's output_ts is when its result came back.
Sources, per lane (a slot such as anthropic-2 reads like its lane):
  claude      Claude Code .jsonl under the operator's ~/.claude/projects, in
              the project directory named after the ticket's worktree.
  codex       the codex rollout .jsonl whose session cwd is the worktree.
  API lanes   archived to logs/<slot>/<ticket>/: Claude Code .jsonl (anthropic)
              or opencode's SQLite store (openrouter, opencode-go, zen, ...).
Every lane also writes logs/<slot>/<ticket>.log, what the runner and the CLI
printed; without a transcript it is the record.

Transcript files are written by agents, so they are untrusted: names are
validated, SQLite is opened read-only and immutable, and nothing is executed.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

from .labels import LANES

TICKET = re.compile(r"^[A-Za-z0-9._-]+$")
SLOT = re.compile(r"^([a-z][a-z-]*[a-z])-([0-9]+)$")
# A runner or brain log line starts with its UTC time in brackets.
LOG_TS = re.compile(r"^\[(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)\]")
LOG_CHUNK = 20000


def lane_of(slot: str) -> str | None:
    """The provider lane a slot runs (anthropic-2 -> anthropic), or None."""
    m = SLOT.match(slot)
    if m and m.group(1) in LANES:
        return m.group(1)
    return slot if slot in LANES else None


def claude_project(claude_projects: Path, cwd: str) -> Path:
    """Claude Code names a project dir after the cwd with "/" and "." as "-"."""
    return claude_projects / cwd.replace("/", "-").replace(".", "-")


def _check(slot: str, ticket: str) -> str:
    lane = lane_of(slot)
    if lane is None or not TICKET.match(ticket) or ticket in (".", ".."):
        raise ValueError(f"unsafe lane or ticket: {slot!r} {ticket!r}")
    return lane


def session_events(slot: str, ticket: str, loom: Path, claude_projects: Path,
                   codex_sessions: Path) -> list[dict]:
    """The agent's own transcript of one run, or [] when there is none."""
    lane = _check(slot, ticket)
    archive = loom / "logs" / slot / ticket
    tree = str(loom / "worktrees" / ticket)
    if lane == "claude":
        # Distinct tickets can share one mangled project dir; keep only
        # sessions whose recorded cwd is this ticket's worktree.
        return claude_dir(claude_project(claude_projects, tree), cwd=tree)
    if lane == "codex":
        rollout = _codex_rollout(codex_sessions, Path(tree))
        return codex_events(rollout.read_text(errors="replace").splitlines()) if rollout else []
    events = claude_dir(archive / ".claude")
    if not events:
        db = archive / ".local" / "share" / "opencode" / "opencode.db"
        if db.is_file() and not db.is_symlink():
            events = opencode_events(db)
    return events


def log_path(slot: str, ticket: str, loom: Path) -> Path:
    _check(slot, ticket)
    return loom / "logs" / slot / f"{ticket}.log"


def load_events(slot: str, ticket: str, loom: Path, claude_projects: Path,
                codex_sessions: Path) -> list[dict]:
    """The transcript, else the lane log, as the viewer shows a run."""
    events = session_events(slot, ticket, loom, claude_projects, codex_sessions)
    if not events:
        log = log_path(slot, ticket, loom)
        if log.is_file():
            events = log_events(log.read_text(errors="replace"))
    return events


def log_events(text: str, chunk: int = LOG_CHUNK) -> list[dict]:
    """A log as events: a new event at each timestamped line, and a long run
    of untimed lines (what a CLI printed) split at line ends near `chunk`."""
    events: list[dict] = []
    cur: list[str] = []
    size = 0
    ts = None

    def flush() -> None:
        if cur and "".join(cur).strip():
            events.append({"kind": "log", "ts": ts, "text": "".join(cur)})
        cur.clear()

    for line in text.splitlines(keepends=True):
        m = LOG_TS.match(line)
        if m or (cur and size + len(line) > chunk):
            flush()
            size = 0
            if m:
                ts = m.group(1)
        cur.append(line)
        size += len(line)
    flush()
    return events


def claude_dir(root: Path, cwd: str | None = None, since: float | None = None) -> list[dict]:
    """Every Claude Code .jsonl under root, oldest first. With cwd, only
    sessions recorded there; with since, only files written since then."""
    if not root.is_dir() or root.is_symlink():
        return []
    files = sorted((p for p in root.rglob("*.jsonl") if p.is_file() and not p.is_symlink()
                    and (since is None or p.stat().st_mtime >= since)),
                   key=lambda p: p.stat().st_mtime)
    events: list[dict] = []
    for f in files:
        lines = f.read_text(errors="replace").splitlines()
        if cwd is not None and not _has_cwd(lines, cwd):
            continue
        events += claude_events(lines)
    return events


def _rows(lines: list[str]):
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            yield row


def _has_cwd(lines: list[str], cwd: str) -> bool:
    return any(row.get("cwd") == cwd for row in _rows(lines))


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
    return ""


def _iso(value) -> str | None:
    """An ISO 8601 string as given, or epoch milliseconds as ISO; else None."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value / 1000.0))
    return None


def _claude_user(content, calls: dict, ts) -> list[dict]:
    """A user row: the prompt text, or tool results joined to their calls."""
    if isinstance(content, str):
        return [{"kind": "user", "ts": ts, "text": content}]
    out = []
    for c in content or []:
        if not isinstance(c, dict):
            continue
        if c.get("type") == "tool_result" and c.get("tool_use_id") in calls:
            call = calls[c["tool_use_id"]]
            call["output"] = _text(c.get("content"))
            call["output_ts"] = ts
        elif c.get("type") == "text":
            out.append({"kind": "user", "ts": ts, "text": c.get("text", "")})
    return out


def _claude_block(c: dict, calls: dict, ts) -> dict | None:
    """One block of an assistant row as an event (tool calls are remembered)."""
    t = c.get("type")
    if t == "text" and c.get("text", "").strip():
        return {"kind": "assistant", "ts": ts, "text": c["text"]}
    if t == "thinking" and c.get("thinking", "").strip():
        return {"kind": "thinking", "ts": ts, "text": c["thinking"]}
    if t == "tool_use":
        call = {"kind": "tool", "ts": ts, "tool": c.get("name", "?"),
                "input": c.get("input") or {}, "output": "", "output_ts": None}
        calls[c.get("id", "")] = call
        return call
    return None


def claude_events(lines: list[str]) -> list[dict]:
    """Claude Code transcript lines -> events; tool results join their call."""
    events: list[dict] = []
    calls: dict[str, dict] = {}
    for row in _rows(lines):
        msg = row.get("message") if isinstance(row.get("message"), dict) else {}
        content = msg.get("content")
        ts = _iso(row.get("timestamp"))
        if row.get("type") == "user":
            events += _claude_user(content, calls, ts)
        elif row.get("type") == "assistant":
            for c in content or []:
                ev = _claude_block(c, calls, ts) if isinstance(c, dict) else None
                if ev:
                    events.append(ev)
    return events


def opencode_events(db: Path) -> list[dict]:
    """opencode 1.18 SQLite store -> events, oldest first."""
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
    except sqlite3.Error:
        return []
    try:
        con.execute("PRAGMA trusted_schema=OFF")
        rows = con.execute(
            "select m.data, p.data, p.time_created from part p join message m on m.id = p.message_id "
            "order by p.time_created, p.id").fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    events = (_opencode_part(*row) for row in rows)
    return [ev for ev in events if ev]


def _opencode_part(mdata, pdata, created) -> dict | None:
    """One opencode message part as an event, or None."""
    try:
        role = json.loads(mdata).get("role")
        part = json.loads(pdata)
    except (ValueError, TypeError, AttributeError):
        return None
    if not isinstance(part, dict):
        return None
    kind, ts = part.get("type"), _iso(created)
    if kind == "text" and part.get("text", "").strip():
        return {"kind": "user" if role == "user" else "assistant", "ts": ts,
                "text": part["text"]}
    if kind == "reasoning" and part.get("text", "").strip():
        return {"kind": "thinking", "ts": ts, "text": part["text"]}
    if kind == "tool":
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        took = state.get("time") if isinstance(state.get("time"), dict) else {}
        return {"kind": "tool", "ts": ts, "tool": part.get("tool", "?"),
                "input": state.get("input") or {},
                "output": state.get("output") or state.get("error") or "",
                "output_ts": _iso(took.get("end")) or ts}
    return None


def _codex_rollout(sessions: Path, tree: Path) -> Path | None:
    if not sessions.is_dir():
        return None
    want = str(tree)
    best = None
    for f in sessions.rglob("rollout-*.jsonl"):
        if not f.is_file() or f.is_symlink():
            continue
        try:
            with f.open(errors="replace") as fh:
                first = json.loads(fh.readline() or "{}")
        except (OSError, ValueError):
            continue
        if (first.get("payload") or {}).get("cwd") == want:
            if best is None or f.stat().st_mtime > best.stat().st_mtime:
                best = f
    return best


def _codex_item(p: dict, ts) -> dict | None:
    """A codex response item as an event; outputs are handled by the caller."""
    t = p.get("type")
    if t == "message" and p.get("role") in ("user", "assistant"):
        text = _text(p.get("content"))
        return {"kind": p["role"], "ts": ts, "text": text} if text.strip() else None
    if t == "reasoning":
        text = _text(p.get("summary"))
        return {"kind": "thinking", "ts": ts, "text": text} if text.strip() else None
    if t in ("custom_tool_call", "function_call"):
        raw = p.get("input") or p.get("arguments") or ""
        return {"kind": "tool", "ts": ts, "tool": p.get("name", "?"),
                "input": {"command": raw if isinstance(raw, str) else json.dumps(raw)},
                "output": "", "output_ts": None}
    return None


def codex_events(lines: list[str]) -> list[dict]:
    """codex rollout lines -> events; a call's output joins the call."""
    events: list[dict] = []
    last_call: dict | None = None
    for row in _rows(lines):
        if row.get("type") != "response_item":
            continue
        p = row.get("payload") or {}
        ts = _iso(row.get("timestamp"))
        if p.get("type") in ("custom_tool_call_output", "function_call_output"):
            if last_call is not None:
                out = p.get("output")
                last_call["output"] = out if isinstance(out, str) else _text(out)
                last_call["output_ts"] = ts
            continue
        ev = _codex_item(p, ts)
        if ev:
            events.append(ev)
            if ev["kind"] == "tool":
                last_call = ev
    return events
