"""Render chat events as a page that reads like a terminal session.

Every string from a transcript is agent-written and escaped. The source stays
ASCII; glyphs come from HTML entities.
"""

from __future__ import annotations

import html
import json

DOT = "&#9679;"      # filled circle
ELBOW = "&#9151;"    # bottom-left corner, as under a tool call
SPARK = "&#10043;"   # thinking marker
MAX_LINES = 12

STYLE = """
body { background: #1a1a1a; color: #d6d6d6; margin: 0;
  font: 13px/1.5 ui-monospace, "SF Mono", Menlo, monospace; }
main { max-width: 1400px; margin: 0 auto; padding: 24px 28px 80px; }
header { color: #8a8a8a; border-bottom: 1px solid #333; padding-bottom: 12px; margin-bottom: 18px; }
header b { color: #e8e8e8; }
a { color: #6cb6ff; }
.ev { margin: 10px 0; white-space: pre-wrap; word-break: break-word; }
.user { background: #262626; border-left: 3px solid #d29922; padding: 8px 12px; }
.user .p { color: #d29922; }
.dot { color: #e8e8e8; }
.tool .dot { color: #3fb950; }
.tool .name { color: #e8e8e8; font-weight: 600; }
.out { color: #8a8a8a; margin-left: 2ch; }
.thinking { color: #8a8a8a; font-style: italic; }
.diff { margin: 4px 0 0 2ch; }
.del { background: #4b1818; color: #ffb3b3; }
.add { background: #12361d; color: #aff5b4; }
details > summary { cursor: pointer; color: #8a8a8a; }
.log { background: #222; padding: 10px; }
table { border-collapse: collapse; width: 100%; }
td, th { text-align: left; padding: 6px 10px; border-bottom: 1px solid #2c2c2c; }
.st-done { color: #3fb950; } .st-failed { color: #f85149; } .st-queued { color: #d29922; }
"""


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


def _summary(inp) -> str:
    if not isinstance(inp, dict):
        return str(inp)
    for key in ("command", "file_path", "filePath", "pattern", "path", "url", "query"):
        if key in inp:
            return str(inp[key])
    return json.dumps(inp)[:200]


def _folded(text: str) -> str:
    lines = text.rstrip("\n").split("\n")
    if len(lines) <= MAX_LINES:
        return esc("\n".join(lines))
    head = esc("\n".join(lines[:MAX_LINES]))
    rest = esc("\n".join(lines[MAX_LINES:]))
    return f"{head}\n<details><summary>+{len(lines) - MAX_LINES} more lines</summary>{rest}</details>"


def _diff(inp: dict) -> str:
    old = inp.get("old_string")
    new = inp.get("new_string", inp.get("content"))
    rows = []
    if isinstance(old, str):
        rows += [f'<div class="del">- {esc(line)}</div>' for line in old.split("\n")]
    if isinstance(new, str):
        rows += [f'<div class="add">+ {esc(line)}</div>' for line in new.split("\n")]
    return f'<div class="diff">{"".join(rows)}</div>' if rows else ""


def event_html(e: dict) -> str:
    kind = e.get("kind")
    if kind == "user":
        return f'<div class="ev user"><span class="p">&gt;</span> {_folded(e.get("text", ""))}</div>'
    if kind == "assistant":
        return f'<div class="ev"><span class="dot">{DOT}</span> {esc(e.get("text", ""))}</div>'
    if kind == "thinking":
        return (f'<div class="ev thinking"><details><summary>{SPARK} Thinking</summary>'
                f'{esc(e.get("text", ""))}</details></div>')
    if kind == "tool":
        tool, inp = e.get("tool", "?"), e.get("input") or {}
        head = f'<span class="dot">{DOT}</span> <span class="name">{esc(tool)}({esc(_summary(inp))})</span>'
        body = ""
        if isinstance(inp, dict) and tool.lower() in ("edit", "write", "multiedit"):
            body = _diff(inp)
        out = e.get("output") or ""
        if out:
            body += f'<div class="out">{ELBOW} {_folded(out)}</div>'
        return f'<div class="ev tool">{head}{body}</div>'
    if kind == "log":
        return f'<div class="ev log">{_folded(e.get("text", ""))}</div>'
    return ""


def page(ticket: str, lane: str, events: list[dict]) -> str:
    body = "".join(event_html(e) for e in events) or '<p class="out">No transcript for this ticket.</p>'
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>{esc(ticket)}</title>'
            f'<style>{STYLE}</style></head><body><main><header><a href="/">Loom lanes</a> / '
            f'<b>{esc(ticket)}</b> on lane <b>{esc(lane)}</b> &middot; {len(events)} events</header>'
            f'{body}</main></body></html>')


def index(rows: list[dict]) -> str:
    trs = "".join(
        f'<tr><td><a href="/t/{esc(r["lane"])}/{esc(r["ticket"])}">{esc(r["ticket"])}</a></td>'
        f'<td>{esc(r["lane"])}</td><td class="st-{esc(r["status"])}">{esc(r["status"])}</td></tr>'
        for r in rows)
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>Loom lanes</title>'
            f'<style>{STYLE}</style></head><body><main><header><b>Loom lanes</b> &middot; '
            f'agent sessions on this machine</header><table><tr><th>Ticket</th><th>Lane</th>'
            f'<th>Status</th></tr>{trs}</table></main></body></html>')
