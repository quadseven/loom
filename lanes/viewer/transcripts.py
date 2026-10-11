"""Read each lane's own session record into one list of chat events.

Event kinds: user, assistant, thinking, tool (with tool, input, output), log.
Sources, per lane:
  claude      Claude Code .jsonl under the operator's ~/.claude/projects, in
              the project directory named after the ticket's worktree.
  anthropic   Claude Code .jsonl archived to logs/anthropic/<ticket>/.
  openrouter,
  opencode-go opencode's SQLite store archived to logs/<lane>/<ticket>/.
  codex       the codex rollout .jsonl whose session cwd is the worktree.
Without a transcript, the lane log (what the CLI printed) is the record.

Transcript files are written by agents, so they are untrusted: names are
validated, SQLite is opened read-only and immutable, and nothing is executed.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

LANES = ("codex", "claude", "openrouter", "opencode-go", "anthropic")
TICKET = re.compile(r"^[A-Za-z0-9._-]+$")


def load_events(lane: str, ticket: str, loom: Path, claude_projects: Path,
                codex_sessions: Path) -> list[dict]:
    if lane not in LANES or not TICKET.match(ticket) or ticket in (".", ".."):
        raise ValueError(f"unsafe lane or ticket: {lane!r} {ticket!r}")
    archive = loom / "logs" / lane / ticket
    events: list[dict] = []
    if lane == "claude":
        tree = str(loom / "worktrees" / ticket)
        # Claude Code names the project dir after the cwd with "/" and "." as
        # "-", so distinct tickets can share one; keep only sessions whose
        # recorded cwd is this ticket's worktree.
        project = claude_projects / tree.replace("/", "-").replace(".", "-")
        events = _claude_dir(project, cwd=tree)
    elif lane == "anthropic":
        events = _claude_dir(archive)
    elif lane in ("openrouter", "opencode-go"):
        db = archive / ".local" / "share" / "opencode" / "opencode.db"
        if db.is_file():
            events = opencode_events(db)
    elif lane == "codex":
        rollout = _codex_rollout(codex_sessions, loom / "worktrees" / ticket)
        if rollout:
            events = codex_events(rollout.read_text(errors="replace").splitlines())
    if not events:
        log = loom / "logs" / lane / f"{ticket}.log"
        if log.is_file():
            events = [{"kind": "log", "text": log.read_text(errors="replace")}]
    return events


def _claude_dir(root: Path, cwd: str | None = None) -> list[dict]:
    if not root.is_dir():
        return []
    files = sorted((p for p in root.rglob("*.jsonl") if p.is_file() and not p.is_symlink()),
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


def _claude_user(content, calls: dict) -> list[dict]:
    """A user row: the prompt text, or tool results joined to their calls."""
    if isinstance(content, str):
        return [{"kind": "user", "text": content}]
    out = []
    for c in content or []:
        if not isinstance(c, dict):
            continue
        if c.get("type") == "tool_result" and c.get("tool_use_id") in calls:
            calls[c["tool_use_id"]]["output"] = _text(c.get("content"))
        elif c.get("type") == "text":
            out.append({"kind": "user", "text": c.get("text", "")})
    return out


def _claude_block(c: dict, calls: dict) -> dict | None:
    """One block of an assistant row as an event (tool calls are remembered)."""
    t = c.get("type")
    if t == "text" and c.get("text", "").strip():
        return {"kind": "assistant", "text": c["text"]}
    if t == "thinking" and c.get("thinking", "").strip():
        return {"kind": "thinking", "text": c["thinking"]}
    if t == "tool_use":
        call = {"kind": "tool", "tool": c.get("name", "?"), "input": c.get("input") or {}, "output": ""}
        calls[c.get("id", "")] = call
        return call
    return None


def claude_events(lines: list[str]) -> list[dict]:
    """Claude Code transcript lines -> events; tool results join their call."""
    events: list[dict] = []
    calls: dict[str, dict] = {}
    for row in _rows(lines):
        content = (row.get("message") or {}).get("content")
        if row.get("type") == "user":
            events += _claude_user(content, calls)
        elif row.get("type") == "assistant":
            for c in content or []:
                ev = _claude_block(c, calls) if isinstance(c, dict) else None
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
            "select m.data, p.data from part p join message m on m.id = p.message_id "
            "order by p.time_created, p.id").fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    events: list[dict] = []
    for mdata, pdata in rows:
        try:
            role = json.loads(mdata).get("role")
            part = json.loads(pdata)
        except ValueError:
            continue
        kind = part.get("type")
        if kind == "text" and part.get("text", "").strip():
            events.append({"kind": "user" if role == "user" else "assistant", "text": part["text"]})
        elif kind == "reasoning" and part.get("text", "").strip():
            events.append({"kind": "thinking", "text": part["text"]})
        elif kind == "tool":
            state = part.get("state") or {}
            events.append({"kind": "tool", "tool": part.get("tool", "?"),
                           "input": state.get("input") or {},
                           "output": state.get("output") or state.get("error") or ""})
    return events


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


def _codex_item(p: dict) -> dict | None:
    """A codex response item as an event; outputs are handled by the caller."""
    t = p.get("type")
    if t == "message" and p.get("role") in ("user", "assistant"):
        text = _text(p.get("content"))
        return {"kind": p["role"], "text": text} if text.strip() else None
    if t == "reasoning":
        text = _text(p.get("summary"))
        return {"kind": "thinking", "text": text} if text.strip() else None
    if t in ("custom_tool_call", "function_call"):
        raw = p.get("input") or p.get("arguments") or ""
        return {"kind": "tool", "tool": p.get("name", "?"),
                "input": {"command": raw if isinstance(raw, str) else json.dumps(raw)}, "output": ""}
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
        ev = _codex_item(p)
        if ev:
            events.append(ev)
            if ev["kind"] == "tool":
                last_call = ev
    return events
