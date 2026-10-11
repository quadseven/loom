#!/usr/bin/env python3
"""Loom dispatcher v1 - the foreman's loop.

Watches ~/loom/goals/ for goals, ~/loom/tickets/<goal>/ for tickets,
routes each ticket to a lane by difficulty, writes briefs to
~/loom/briefs/<lane>/, and tracks state in ~/loom/state/ for the dashboard.

Difficulty definitions:
  hard   - Architecture, complex refactors, novel problems, multi-file
           changes with subtle interactions. Needs frontier reasoning.
  medium - Feature implementation, bug fixes with clear scope,
           single-component changes. Strong model handles it.
  easy   - Simple fixes, docs, config, boilerplate. Any model works.

Lane routing (v2): only lanes with a worker are routed, and only while that
worker's runner reports a fresh, available heartbeat (state/usage/<lane>.json,
written by lanes/run_lane.sh every pass). Anthropic API credits go first
(they expire 2026-10-21), then subscriptions, then free lanes.
  hard   -> anthropic -> claude -> codex
  medium -> anthropic -> claude -> codex -> opencode-go
  easy   -> anthropic -> openrouter (free) -> opencode-go -> codex -> claude

The Sparks lane is deferred until the Sparks overhaul is done (#9).

Usage:
  python3 dispatch.py --once        # single pass
  python3 dispatch.py --loop        # run forever (30s interval)
  python3 dispatch.py --goal <file> # ingest a new goal file
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

LOOM = Path.home() / "loom"
GOALS = LOOM / "goals"
TICKETS = LOOM / "tickets"
BRIEFS = LOOM / "briefs"
STATE = LOOM / "state"

# Preferred lanes per difficulty, first available wins. Every lane here has
# a runner in lanes/run_lane.sh; a lane without one must not be listed.
DIFFICULTY_LANE = {
    "hard": ["anthropic", "claude", "codex"],
    "medium": ["anthropic", "claude", "codex", "opencode-go"],
    "easy": ["anthropic", "openrouter", "opencode-go", "codex", "claude"],
}

# A heartbeat older than this means the lane's runner is not running.
HEARTBEAT_MAX_AGE_S = 600


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def load_state() -> dict:
    f = STATE / "dispatcher.json"
    if f.exists():
        return json.loads(f.read_text())
    return {"goals": {}, "last_run": None}


def save_state(state: dict) -> None:
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    (STATE / "dispatcher.json").write_text(json.dumps(state, indent=2))


def get_ticket_status(ticket_path: Path) -> str:
    """Check if a ticket has been briefed, is in progress, or done."""
    meta_file = ticket_path.with_suffix(".json")
    if meta_file.exists():
        meta = json.loads(meta_file.read_text())
        return meta.get("status", "pending")
    return "pending"


def set_ticket_status(ticket_path: Path, status: str, lane: str = None, **extra) -> None:
    meta_file = ticket_path.with_suffix(".json")
    meta = {}
    if meta_file.exists():
        meta = json.loads(meta_file.read_text())
    meta.update({"status": status, "updated": datetime.now(timezone.utc).isoformat()})
    if lane:
        meta["lane"] = lane
    meta.update(extra)
    meta_file.write_text(json.dumps(meta, indent=2))


def pick_lane(difficulty: str, state: dict) -> str | None:
    """The first lane for `difficulty` whose runner reports a fresh, available
    heartbeat. No heartbeat, a stale one, or an unreadable one means no
    worker is taking briefs there, so the lane gets nothing."""
    now = time.time()
    for lane in DIFFICULTY_LANE.get(difficulty, []):
        try:
            beat = json.loads((STATE / "usage" / f"{lane}.json").read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not beat.get("available"):
            continue
        if now - float(beat.get("at", 0)) > HEARTBEAT_MAX_AGE_S:
            continue
        return lane
    return None


def render_brief(ticket_path: Path, lane: str) -> Path:
    """Render a ticket as a brief in the lane's inbox."""
    ticket_text = ticket_path.read_text()
    # Parse frontmatter for repo/branch/model hints
    lines = ticket_text.splitlines()
    repo = "quadseven/loom"  # default
    branch = None
    model = None
    difficulty = "medium"

    for line in lines:
        if line.startswith("Repo:"):
            repo = line.split(":", 1)[1].strip()
        elif line.startswith("Branch:"):
            branch = line.split(":", 1)[1].strip()
        elif line.startswith("Model:"):
            model = line.split(":", 1)[1].strip()
        elif line.startswith("Difficulty:"):
            difficulty = line.split(":", 1)[1].strip().lower()

    ticket_id = ticket_path.stem
    if not branch:
        branch = f"loom/{ticket_id}"

    brief = f"""# Brief: {ticket_id}

Repo: {repo}
Branch: {branch}
Lane: {lane}
Difficulty: {difficulty}
"""
    if model:
        brief += f"Model: {model}\n"
    brief += f"""
## Ticket

{ticket_text}

## Definition of done

- Failing test first (python -B), then implementation
- All relevant test suites pass
- Leave the change on the brief's branch and stop. The lane runner commits
  what is left, pushes, and opens the PR; the orchestrator answers review
  and ships.
- Never merge, never close issues or PRs, never push to another branch.
- No secrets in code, ASCII only, scrubbed for public
"""

    inbox = BRIEFS / lane
    inbox.mkdir(parents=True, exist_ok=True)
    brief_path = inbox / f"{ticket_id}.md"
    brief_path.write_text(brief)
    return brief_path


def process_goal(goal_name: str, state: dict) -> int:
    """Route all pending tickets for a goal. Returns count briefed."""
    ticket_dir = TICKETS / goal_name
    if not ticket_dir.exists():
        return 0

    briefed = 0
    for ticket_path in sorted(ticket_dir.glob("*.md")):
        # Skip meta files
        if ticket_path.suffix != ".md":
            continue
        status = get_ticket_status(ticket_path)
        if status != "pending":
            continue

        # Parse difficulty
        difficulty = "medium"
        for line in ticket_path.read_text().splitlines()[:10]:
            if line.startswith("Difficulty:"):
                difficulty = line.split(":", 1)[1].strip().lower()
                break

        lane = pick_lane(difficulty, state)
        if not lane:
            log(f"  no lane available for {ticket_path.stem} ({difficulty})")
            continue

        brief_path = render_brief(ticket_path, lane)
        set_ticket_status(ticket_path, "briefed", lane=lane,
                          brief=str(brief_path))
        log(f"  briefed {ticket_path.stem} -> {lane} ({difficulty})")
        briefed += 1

    return briefed


def run_once() -> dict:
    """Single dispatcher pass. Returns summary."""
    state = load_state()
    total_briefed = 0

    # Find all goals with ticket directories
    if TICKETS.exists():
        for goal_dir in sorted(TICKETS.iterdir()):
            if goal_dir.is_dir():
                n = process_goal(goal_dir.name, state)
                total_briefed += n
                if n:
                    log(f"goal {goal_dir.name}: {n} tickets briefed")

    save_state(state)
    return {"briefed": total_briefed, "at": state["last_run"]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="single pass")
    ap.add_argument("--loop", action="store_true", help="run forever")
    ap.add_argument("--interval", type=int, default=30, help="loop seconds")
    args = ap.parse_args()

    if args.loop:
        log("dispatcher loop starting")
        while True:
            try:
                result = run_once()
                if result["briefed"]:
                    log(f"pass complete: {result['briefed']} briefed")
            except Exception as e:
                log(f"pass failed: {e}")
            time.sleep(args.interval)
    else:
        result = run_once()
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
