"""Read each lane's own session record into one list of chat events.

Event kinds: user, assistant, thinking, tool (with tool, input, output), log.
Each event carries ts, the UTC time the CLI recorded for it, or None.
Sources, per lane:
  claude      Claude Code .jsonl under the operator's ~/.claude/projects, in
              the project directory named after the ticket's worktree.
  anthropic   Claude Code .jsonl archived to logs/anthropic/<ticket>/.
  openrouter,
  opencode-go opencode's SQLite store (1.x or 2.0) archived to logs/<lane>/<ticket>/.
  codex       the codex rollout .jsonl whose session cwd is the worktree.
A slot (opencode-go-3) reads as its lane, from its own logs/<slot>/.
Without a transcript, the lane log (what the CLI printed) is the record.

One parser for every reader: the viewer (lanes/viewer/serve.py) shows these
events, and the station (bus/transcripts.py) uploads them.

Transcript files are written by agents, so they are untrusted: names are
validated, SQLite is opened read-only and immutable, and nothing is executed.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

LANES = ("codex", "claude", "openrouter", "opencode-go", "anthropic")
TICKET = re.compile(r"^[A-Za-z0-9._-]+$")
SLOT = re.compile(r"^(?P<lane>[a-z][a-z-]*[a-z])(-[0-9]+)?$")


def provider(slot: str) -> str | None:
    """The lane a slot belongs to (opencode-go-3 -> opencode-go), or None."""
    m = SLOT.match(slot)
    return m["lane"] if m and m["lane"] in LANES else None


def load_events(slot: str, ticket: str, loom: Path, claude_projects: Path,
                codex_sessions: Path) -> list[dict]:
    """Events for one ticket of a lane or one of its slots (logs/<slot>/)."""
    if provider(slot) is None:
        raise ValueError(f"unsafe lane or ticket: {slot!r} {ticket!r}")
    events = session_events(slot, ticket, loom, claude_projects, codex_sessions)
    return events or log_events(loom / "logs" / slot / f"{ticket}.log")


def log_events(log: Path) -> list[dict]:
    """The lane log as one event, or none when there is no log."""
    if not log.is_file() or log.is_symlink():
        return []
    return [{"kind": "log", "text": log.read_text(errors="replace"), "ts": None}]


def session_events(slot: str, ticket: str, loom: Path, claude_projects: Path,
                   codex_sessions: Path) -> list[dict]:
    """The CLI's own session record for one ticket, without the log fallback;
    none for a lane that keeps no transcript."""
    lane = provider(slot)
    if not SLOT.match(slot) or not TICKET.match(ticket) or ticket in (".", ".."):
        raise ValueError(f"unsafe lane or ticket: {slot!r} {ticket!r}")
    archive = loom / "logs" / slot / ticket
    events: list[dict] = []
    if lane == "claude":
        tree = str(loom / "worktrees" / ticket)
        # Claude Code names the project dir after the cwd with "/" and "." as
        # "-", so distinct tickets can share one; keep only sessions whose
        # recorded cwd is this ticket's worktree.
        events = claude_dir(project_dir(claude_projects, Path(tree)), cwd=tree)
    elif lane == "anthropic":
        events = claude_dir(archive)
    elif lane in ("openrouter", "opencode-go"):
        db = archive / ".local" / "share" / "opencode" / "opencode.db"
        packed = db.with_name("opencode.db.gz")
        if db.is_file():
            events = opencode_events(db)
        elif packed.is_file() and not packed.is_symlink():
            # The station compresses a finished run's store (loom#1808).
            events = opencode_gz_events(packed)
    elif lane == "codex":
        rollout = _codex_rollout(codex_sessions, loom / "worktrees" / ticket)
        if rollout:
            events = codex_events(rollout.read_text(errors="replace").splitlines())
    return events


def project_dir(claude_projects: Path, cwd: Path) -> Path:
    """Claude Code's project directory for sessions started in cwd: the path
    with "/" and "." as "-"."""
    return claude_projects / str(cwd).replace("/", "-").replace(".", "-")


def claude_dir(root: Path, cwd: str | None = None, since: float | None = None,
               until: float | None = None) -> list[dict]:
    """Every Claude Code session under root, oldest first: only those that
    recorded cwd when given, and only files last written between since and
    until (epoch seconds) when given."""
    if not root.is_dir():
        return []
    files = sorted((p for p in root.rglob("*.jsonl") if p.is_file() and not p.is_symlink()),
                   key=lambda p: p.stat().st_mtime)
    files = [p for p in files if (since is None or p.stat().st_mtime >= since)
             and (until is None or p.stat().st_mtime <= until)]
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


def _stamp(value) -> str | None:
    """A recorded time as UTC ISO 8601: an ISO string as written, or epoch
    milliseconds; None for anything else."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value / 1000))
    return None


def _claude_user(content, calls: dict, ts: str | None = None) -> list[dict]:
    """A user row: the prompt text, or tool results joined to their calls."""
    if isinstance(content, str):
        return [{"kind": "user", "text": content, "ts": ts}]
    out = []
    for c in content or []:
        if not isinstance(c, dict):
            continue
        if c.get("type") == "tool_result" and c.get("tool_use_id") in calls:
            calls[c["tool_use_id"]]["output"] = _text(c.get("content"))
        elif c.get("type") == "text":
            out.append({"kind": "user", "text": c.get("text", ""), "ts": ts})
    return out


def _claude_block(c: dict, calls: dict, ts: str | None = None) -> dict | None:
    """One block of an assistant row as an event (tool calls are remembered)."""
    t = c.get("type")
    if t == "text" and c.get("text", "").strip():
        return {"kind": "assistant", "text": c["text"], "ts": ts}
    if t == "thinking" and c.get("thinking", "").strip():
        return {"kind": "thinking", "text": c["thinking"], "ts": ts}
    if t == "tool_use":
        call = {"kind": "tool", "tool": c.get("name", "?"), "input": c.get("input") or {}, "output": "",
                "ts": ts}
        calls[c.get("id", "")] = call
        return call
    return None


def claude_events(lines: list[str]) -> list[dict]:
    """Claude Code transcript lines -> events; tool results join their call."""
    events: list[dict] = []
    calls: dict[str, dict] = {}
    for row in _rows(lines):
        content = (row.get("message") or {}).get("content")
        ts = _stamp(row.get("timestamp"))
        if row.get("type") == "user":
            events += _claude_user(content, calls, ts)
        elif row.get("type") == "assistant":
            for c in content or []:
                ev = _claude_block(c, calls, ts) if isinstance(c, dict) else None
                if ev:
                    events.append(ev)
    return events


def opencode_gz_events(packed: Path) -> list[dict]:
    """opencode_events of a gzip-compressed store, unpacked to a private
    temporary file that is removed after the read."""
    fd, tmp = tempfile.mkstemp(suffix=".db")
    try:
        with os.fdopen(fd, "wb") as out, gzip.open(packed, "rb") as src:
            shutil.copyfileobj(src, out, 1 << 20)
        return opencode_events(Path(tmp))
    except (OSError, EOFError) as err:
        # A store that cannot be unpacked reads as none, as an unreadable
        # SQLite store does in opencode_events, and the reason is said.
        print(f"cannot unpack session store {packed}: {err}", file=sys.stderr)
        return []
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def opencode_events(db: Path) -> list[dict]:
    """opencode SQLite store (1.18 message/part, or 2.0 session_message) -> events, oldest first."""
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
    except sqlite3.Error:
        return []
    try:
        con.execute("PRAGMA trusted_schema=OFF")
        tables = {r[0] for r in con.execute("select name from sqlite_master where type = 'table'")}
        if "session_message" in tables and "part" not in tables:
            return _opencode2_events(con.execute(
                "select type, data from session_message order by seq").fetchall())
        rows = con.execute(
            "select m.data, p.data, p.time_created from part p join message m on m.id = p.message_id "
            "order by p.time_created, p.id").fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    events: list[dict] = []
    for mdata, pdata, created in rows:
        try:
            role = json.loads(mdata).get("role")
            part = json.loads(pdata)
        except ValueError:
            continue
        kind, ts = part.get("type"), _stamp(created)
        if kind == "text" and part.get("text", "").strip():
            events.append({"kind": "user" if role == "user" else "assistant", "text": part["text"],
                           "ts": ts})
        elif kind == "reasoning" and part.get("text", "").strip():
            events.append({"kind": "thinking", "text": part["text"], "ts": ts})
        elif kind == "tool":
            state = part.get("state") or {}
            events.append({"kind": "tool", "tool": part.get("tool", "?"),
                           "input": state.get("input") or {},
                           "output": state.get("output") or state.get("error") or "", "ts": ts})
    return events


def _opencode2_events(rows: list) -> list[dict]:
    """opencode 2.0 session_message rows (type, data): a user row's data.text,
    an assistant row's data.content[] of text, reasoning and tool items. The
    rows carry no time the station relies on, so ts is None."""
    events: list[dict] = []
    for kind, raw in rows:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if kind == "user":
            text = data.get("text")
            if isinstance(text, str) and text.strip():
                events.append({"kind": "user", "text": text, "ts": None})
        elif kind == "assistant":
            for c in data.get("content") or []:
                ev = _opencode2_item(c) if isinstance(c, dict) else None
                if ev:
                    events.append(ev)
    return events


def _opencode2_item(c: dict) -> dict | None:
    t, text = c.get("type"), c.get("text")
    if t == "text" and isinstance(text, str) and text.strip():
        return {"kind": "assistant", "text": text, "ts": None}
    if t == "reasoning" and isinstance(text, str) and text.strip():
        return {"kind": "thinking", "text": text, "ts": None}
    if t == "tool":
        state = c.get("state") if isinstance(c.get("state"), dict) else {}
        out = state.get("content") or state.get("error") or ""
        if not isinstance(out, str):
            out = _text(out) if isinstance(out, list) else json.dumps(out)
        return {"kind": "tool", "tool": c.get("name", "?"), "input": state.get("input") or {},
                "output": out, "ts": None}
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


def _codex_item(p: dict, ts: str | None = None) -> dict | None:
    """A codex response item as an event; outputs are handled by the caller."""
    t = p.get("type")
    if t == "message" and p.get("role") in ("user", "assistant"):
        text = _text(p.get("content"))
        return {"kind": p["role"], "text": text, "ts": ts} if text.strip() else None
    if t == "reasoning":
        text = _text(p.get("summary"))
        return {"kind": "thinking", "text": text, "ts": ts} if text.strip() else None
    if t in ("custom_tool_call", "function_call"):
        raw = p.get("input") or p.get("arguments") or ""
        return {"kind": "tool", "tool": p.get("name", "?"),
                "input": {"command": raw if isinstance(raw, str) else json.dumps(raw)}, "output": "",
                "ts": ts}
    return None


def codex_events(lines: list[str]) -> list[dict]:
    """codex rollout lines -> events; a call's output joins the call."""
    events: list[dict] = []
    last_call: dict | None = None
    for row in _rows(lines):
        if row.get("type") != "response_item":
            continue
        p = row.get("payload") or {}
        if p.get("type") in ("custom_tool_call_output", "function_call_output"):
            if last_call is not None:
                out = p.get("output")
                last_call["output"] = out if isinstance(out, str) else _text(out)
            continue
        ev = _codex_item(p, _stamp(row.get("timestamp")))
        if ev:
            events.append(ev)
            if ev["kind"] == "tool":
                last_call = ev
    return events
