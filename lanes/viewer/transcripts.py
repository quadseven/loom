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
        project = claude_projects / str(loom / "worktrees" / ticket).replace("/", "-").replace(".", "-")
        events = _claude_dir(project)
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


def _claude_dir(root: Path) -> list[dict]:
    if not root.is_dir():
        return []
    files = sorted((p for p in root.rglob("*.jsonl") if p.is_file() and not p.is_symlink()),
                   key=lambda p: p.stat().st_mtime)
    events: list[dict] = []
    for f in files:
        events += claude_events(f.read_text(errors="replace").splitlines())
    return events


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
    return ""


def claude_events(lines: list[str]) -> list[dict]:
    """Claude Code transcript lines -> events; tool results join their call."""
    events: list[dict] = []
    calls: dict[str, dict] = {}
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("type") not in ("user", "assistant"):
            continue
        content = (row.get("message") or {}).get("content")
        if row["type"] == "user":
            if isinstance(content, str):
                events.append({"kind": "user", "text": content})
                continue
            for c in content or []:
                if c.get("type") == "tool_result" and c.get("tool_use_id") in calls:
                    calls[c["tool_use_id"]]["output"] = _text(c.get("content"))
                elif c.get("type") == "text":
                    events.append({"kind": "user", "text": c.get("text", "")})
            continue
        for c in content or []:
            if c.get("type") == "text" and c.get("text", "").strip():
                events.append({"kind": "assistant", "text": c["text"]})
            elif c.get("type") == "thinking" and c.get("thinking", "").strip():
                events.append({"kind": "thinking", "text": c["thinking"]})
            elif c.get("type") == "tool_use":
                call = {"kind": "tool", "tool": c.get("name", "?"),
                        "input": c.get("input") or {}, "output": ""}
                calls[c.get("id", "")] = call
                events.append(call)
    return events


def opencode_events(db: Path) -> list[dict]:
    """opencode 1.18 SQLite store -> events, oldest first."""
    con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
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


def codex_events(lines: list[str]) -> list[dict]:
    """codex rollout lines -> events; a call's output joins the call."""
    events: list[dict] = []
    last_call: dict | None = None
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("type") != "response_item":
            continue
        p = row.get("payload") or {}
        t = p.get("type")
        if t == "message" and p.get("role") in ("user", "assistant"):
            text = _text(p.get("content"))
            if text.strip():
                events.append({"kind": p["role"], "text": text})
        elif t == "reasoning":
            text = _text(p.get("summary"))
            if text.strip():
                events.append({"kind": "thinking", "text": text})
        elif t in ("custom_tool_call", "function_call"):
            raw = p.get("input") or p.get("arguments") or ""
            last_call = {"kind": "tool", "tool": p.get("name", "?"),
                         "input": {"command": raw if isinstance(raw, str) else json.dumps(raw)},
                         "output": ""}
            events.append(last_call)
        elif t in ("custom_tool_call_output", "function_call_output") and last_call is not None:
            out = p.get("output")
            last_call["output"] = out if isinstance(out, str) else _text(out)
    return events
