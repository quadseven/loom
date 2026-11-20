"""Plan a lane restore after a reboot, and keep secrets out of the snapshot.

The restore script feeds this module the manifest, the newest session
snapshot and the names of the agents already running, and runs the commands
it returns. No tests need a shell or a workspace CLI.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Sequence

# How each coding CLI resumes a recorded session.
RESUME_ARGS = {
    "codex": lambda sid: ["resume", sid],
    "agy": lambda sid: ["--conversation", sid],
    "claude": lambda sid: ["--resume", sid],
}
RESUMED, RUNNING = "resumed", "already-running"
FRESH, ASK, FAILED = "fresh-primed", "awaiting-decision", "failed"
# The wrapper's token variables and the usual key shapes.
SECRET = re.compile(
    r"(GH_TOKEN|GITHUB_TOKEN|ghs_[A-Za-z0-9]{8,}|ghp_[A-Za-z0-9]{8,}"
    r"|github_pat_[A-Za-z0-9_]{8,}|sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{12,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY)")


def _lane(lane: Dict[str, Any], snap: Dict[str, Any],
          running: Sequence[str]) -> Dict[str, Any]:
    name = str(lane["name"])
    out: Dict[str, Any] = {"name": name, "command": None}
    if name in running:
        out["outcome"] = RUNNING
        return out
    policy = str(lane.get("restore_policy") or "ask")
    sid = str(snap.get("session_id") or "")
    kind = str(lane["kind"])
    if kind not in RESUME_ARGS:
        out["outcome"] = FAILED
        return out
    if policy not in ("resume", "ask", "fresh"):
        out["outcome"] = FAILED
        return out
    if policy == "resume" and not sid:
        policy = "ask"
    if policy == "ask":
        out["outcome"] = ASK
        return out
    args = list(lane.get("flags") or [])
    if policy == "resume":
        args += RESUME_ARGS[kind](sid)
    command = ["agent", "start", name, "--kind", kind]
    if snap.get("pane"):
        command += ["--pane", str(snap["pane"])]
    out["command"] = command + ["--"] + args
    out["cwd"] = lane.get("cwd")
    out["outcome"] = RESUMED if policy == "resume" else FRESH
    return out


def plan(manifest: Dict[str, Any], snapshot: Dict[str, Any],
         running: Sequence[str]) -> Dict[str, Any]:
    """One entry per manifest lane (the `agent start` command to run, or
    none, and its outcome) and the exit code: 0 only when every lane is
    resumed or was already running."""
    seen = snapshot.get("lanes") or {}
    lanes = [_lane(lane, seen.get(lane["name"]) or {}, running)
             for lane in manifest.get("lanes") or []]
    ok = all(x["outcome"] in (RESUMED, RUNNING) for x in lanes)
    return {"lanes": lanes, "exit": 0 if ok else 1}


def secrets_in(text: str) -> List[str]:
    """The secret-shaped strings in text; a snapshot with any is not written."""
    return [m.group(0) for m in SECRET.finditer(text)]


def check_snapshot(text: str) -> str:
    """Return text unchanged, or raise when it carries a secret."""
    if secrets_in(text):
        raise ValueError("the snapshot holds a secret")
    return text
