#!/usr/bin/env python3
"""Loom state publisher - pushes dashboard state to GitHub.

Collects state from ~/loom/state/, ~/loom/tickets/, ~/loom/briefs/ and
pushes it to the loom repo's `state` branch as state.json. The OKE dashboard
fetches it from raw.githubusercontent.com every 15s.

Usage:
  python3 publish_state.py          # single push
  python3 publish_state.py --loop   # push every 30s
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

LOOM = Path.home() / "loom"
GITHUB_API = "https://api.github.com"
REPO = "quadseven/loom"
BRANCH = "state"

# PAT via the github-pat skill's surrogate mechanism
sys.path.insert(0, "/home/hatch/workspace/skills/github-pat/bin")
try:
    from importlib.machinery import SourceFileLoader
    _gh = SourceFileLoader("ghapi", "/home/hatch/workspace/skills/github-pat/bin/gh-api.py").load_module()
    gh_api = _gh.api
    HAS_GH = True
except Exception:
    HAS_GH = False


def collect_state() -> dict:
    """Gather all Loom state for the dashboard."""
    state = {
        "goals": [],
        "lanes": {},
        "updated": datetime.now(timezone.utc).isoformat(),
    }

    tickets_dir = LOOM / "tickets"
    if tickets_dir.exists():
        for goal_dir in sorted(tickets_dir.iterdir()):
            if not goal_dir.is_dir():
                continue
            goal = {"name": goal_dir.name, "tickets": []}
            for ticket_path in sorted(goal_dir.glob("*.md")):
                meta_path = ticket_path.with_suffix(".json")
                meta = {}
                if meta_path.exists():
                    try:
                        meta = json.loads(meta_path.read_text())
                    except (json.JSONDecodeError, OSError):
                        pass
                try:
                    title = ticket_path.read_text().splitlines()[0][:80]
                except (OSError, IndexError):
                    title = ticket_path.stem
                goal["tickets"].append({
                    "id": ticket_path.stem,
                    "title": title,
                    "status": meta.get("status", "pending"),
                    "lane": meta.get("lane", "-"),
                    "updated": meta.get("updated", "-"),
                })
            state["goals"].append(goal)

    briefs_dir = LOOM / "briefs"
    if briefs_dir.exists():
        for lane_dir in sorted(briefs_dir.iterdir()):
            if not lane_dir.is_dir():
                continue
            pending = [p for p in lane_dir.glob("*.md") if p.is_file()]
            done_dir = lane_dir / "done"
            done = list(done_dir.glob("*.md")) if done_dir.exists() else []
            usage_file = LOOM / "state" / "usage" / f"{lane_dir.name}.json"
            usage = {}
            if usage_file.exists():
                try:
                    usage = json.loads(usage_file.read_text())
                except (json.JSONDecodeError, OSError):
                    pass
            state["lanes"][lane_dir.name] = {
                "pending": len(pending),
                "done": len(done),
                "pending_tickets": [p.stem for p in pending[:5]],
                "usage": usage,
            }

    disp_file = LOOM / "state" / "dispatcher.json"
    if disp_file.exists():
        try:
            state["dispatcher"] = json.loads(disp_file.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    return state


def push_state(state: dict) -> bool:
    """Push state.json to the loom repo's state branch."""
    if not HAS_GH:
        print("GitHub API not available", flush=True)
        return False

    content = base64.b64encode(json.dumps(state, indent=2).encode()).decode()

    # Get current state branch SHA (or create it)
    try:
        ref = gh_api("GET", f"/repos/{REPO}/git/ref/heads/{BRANCH}")
        base_sha = ref["object"]["sha"]
        # Get current file SHA
        try:
            existing = gh_api("GET", f"/repos/{REPO}/contents/state.json?ref={BRANCH}")
            file_sha = existing["sha"]
        except Exception:
            file_sha = None
    except Exception:
        # Branch doesn't exist; create from main
        main = gh_api("GET", f"/repos/{REPO}/git/ref/heads/main")
        main_sha = main["object"]["sha"]
        gh_api("POST", f"/repos/{REPO}/git/refs",
               {"ref": f"refs/heads/{BRANCH}", "sha": main_sha})
        base_sha = main_sha
        file_sha = None

    # Create blob and update file via git database API
    blob = gh_api("POST", f"/repos/{REPO}/git/blobs",
                  {"content": content, "encoding": "base64"})
    # Get the branch's current tree
    commit = gh_api("GET", f"/repos/{REPO}/git/commits/{base_sha}")
    tree = gh_api("POST", f"/repos/{REPO}/git/trees", {
        "base_tree": commit["tree"]["sha"],
        "tree": [{"path": "state.json", "mode": "100644",
                  "type": "blob", "sha": blob["sha"]}],
    })
    new_commit = gh_api("POST", f"/repos/{REPO}/git/commits", {
        "message": f"Loom state update {datetime.now(timezone.utc).strftime('%H:%M:%S')}",
        "tree": tree["sha"],
        "parents": [base_sha],
    })
    gh_api("PATCH", f"/repos/{REPO}/git/refs/heads/{BRANCH}",
           {"sha": new_commit["sha"]})
    return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=30)
    args = ap.parse_args()

    def once():
        state = collect_state()
        if push_state(state):
            print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] state pushed "
                  f"({len(state['goals'])} goals)", flush=True)
        # Also write locally for the local dashboard
        (LOOM / "state" / "dashboard.json").write_text(json.dumps(state, indent=2))

    if args.loop:
        while True:
            try:
                once()
            except Exception as e:
                print(f"publish failed: {e}", flush=True)
            time.sleep(args.interval)
    else:
        once()


if __name__ == "__main__":
    main()
