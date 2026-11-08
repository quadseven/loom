"""A shared, replay-safe budget for outcomes that leave the same boxes unproven."""
import hashlib
import json
import re

MARKER = "<!-- loom:no-change-budget v1 -->"
RESET = "<!-- loom:no-change-reset v1 -->"
FILE = "no-change-budget.json"


def valid_entry(entry):
    if not isinstance(entry, dict):
        return False
    runs = entry.get("runs", {})
    return (isinstance(runs, dict) and all(valid_step(step) for step in runs.values())
            and isinstance(entry.get("count", 0), int)
            and isinstance(entry.get("at", 0), (int, float))
            and isinstance(entry.get("resets", []), list)
            and (entry.get("promoted") is None or valid_plan(entry["promoted"])))


def valid_plan(plan):
    return isinstance(plan, dict) and (not plan or (
        isinstance(plan.get("lane"), str) and bool(plan["lane"].strip())
        and isinstance(plan.get("model"), str) and bool(plan["model"].strip())
        and type(plan.get("rung")) is int and plan["rung"] > 0))


def valid_step(step):
    if not isinstance(step, dict) or not isinstance(step.get("done"), bool):
        return False
    action = step.get("action")
    if action not in {"retry", "promote", "hold", "ignore"}:
        return False
    if "plan" in step and (action != "promote" or not valid_plan(step["plan"])):
        return False
    if step["done"]:
        return True
    marker = step.get("marker")
    return (action != "ignore" and isinstance(marker, str)
            and re.fullmatch(r"[0-9a-f]{16}", marker) is not None)


def key(boxes):
    texts = sorted(set(" ".join(text.split()) for text in boxes))
    return hashlib.sha256(json.dumps(texts).encode()).hexdigest()


def outcome(data, ticket, boxes, run, limit, at=None):
    entry = data.setdefault(ticket, {})
    runs = entry.setdefault("runs", {})
    fingerprint = key(boxes)
    if run in runs and (runs[run]["done"] or entry.get("key") == fingerprint):
        return entry, runs[run]
    if at is not None and at < entry.get("at", 0):
        runs[run] = {"action": "ignore", "done": True}
        return entry, runs[run]
    if at is not None:
        entry["at"] = at
    if entry.get("key") != fingerprint:
        for old in runs.values():
            old["done"] = True
        entry.update(key=fingerprint, count=0, promoted=None)
    entry["count"] = entry.get("count", 0) + 1
    action = "retry"
    if entry.get("promoted"):
        action = "hold"
    elif entry["count"] >= limit:
        action = "promote"
    step = {"action": action, "done": False, "marker": key([run])[:16]}
    runs[run] = step
    return entry, step


def reset(data, ticket, run, at=None):
    entry = data.setdefault(ticket, {})
    if run in entry.get("resets", []):
        return False
    entry.setdefault("resets", []).append(run)
    if at is not None and at < entry.get("at", 0):
        return True
    if at is not None:
        entry["at"] = at
    for step in entry.get("runs", {}).values():
        step["done"] = True
    entry.update(key=None, count=0, promoted=None)
    return True


def active(comments):
    active = False
    for comment in comments:
        text = str(comment.get("body") or "")
        if MARKER in text:
            active = True
        if RESET in text:
            active = False
    return active
