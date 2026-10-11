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

Lane routing (v1): $200 Anthropic API credits burn FIRST (expires Oct 21).
  hard   -> anthropic (API credits) -> claude-opus (subscription) -> sparks-qwen
  medium -> anthropic (API credits) -> claude-sonnet/claude/codex -> sparks-qwen
  easy   -> anthropic (API credits) -> opencode (OpenRouter free) -> sparks-qwen

Sparks (Nvidia DGX) runs Qwen Coder for code, not just tests/builds.
Sparks never touches the live WoW realm.

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

# Lane tiers: which difficulties each lane can handle
# Priority: API credits first (burn $200 before Oct 21), then subscriptions,
# then free/compute lanes.
#
# Frontier models (hard tickets): anthropic API, claude-opus, codex-astra
# Strong models (medium): claude-sonnet, codex-sol
# Fast/free (easy): opencode, sparks-qwen
LANE_TIERS = {
    "anthropic": {"hard", "medium", "easy"},  # $200 credits, FIRST priority
    "claude-opus": {"hard", "medium"},         # subscription, frontier
    "codex-astra": {"hard", "medium"},         # subscription, frontier planning
    "claude": {"medium", "easy"},              # subscription, sonnet
    "codex": {"medium", "easy"},               # subscription, sol (default)
    "sparks-qwen": {"hard", "medium", "easy"}, # DGX Qwen Coder, code + tests
    "opencode": {"easy"},                      # OpenRouter free tier
    "sparks": set(),  # legacy compute-only entry, use sparks-qwen for code
}

# Preferred lane per difficulty (first available wins).
# Anthropic API credits are always first until exhausted (Oct 21 expiry).
# Frontier: Opus and Astra can both do big planning, not just Claude.
DIFFICULTY_LANE = {
    "hard": ["anthropic", "claude-opus", "codex-astra", "sparks-qwen"],
    "medium": ["anthropic", "claude", "codex", "sparks-qwen"],
    "easy": ["anthropic", "opencode", "sparks-qwen", "claude", "codex"],
}


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
    """Pick the best lane for a ticket by difficulty and availability."""
    for lane in DIFFICULTY_LANE.get(difficulty, []):
        # Check lane heartbeat (skip if stale or over budget)
        usage_file = STATE / "usage" / f"{lane}.json"
        if usage_file.exists():
            try:
                usage = json.loads(usage_file.read_text())
                # Skip if marked unavailable or rate-limited
                if usage.get("unavailable") or usage.get("rate_limited"):
                    continue
            except (json.JSONDecodeError, OSError):
                pass
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
