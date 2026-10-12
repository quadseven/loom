"""Who owns each stage of a goal's work, and what a peer may finish.

A brain that leaves ready work for later (a merge, a failed ticket, an ask)
records it on the goal home as a stage record, `deferred`, with a resume
item. That yields the stage: the next brain that holds the tick lock may
finish the item, however recently the deferring brain journaled. A `done`
record, or a change to the item itself (the pull request merged, closed or
pushed to; the ticket relabelled or closed), invalidates every older resume
item for it. Ready work nobody recorded is offered the same way once it has
waited LOOM_STAGE_OVERDUE_HOURS (default 2) untouched.

Record format, one comment on the goal home:

    <!-- loom:stage v1 -->
    Stage: merge
    Item: #4
    Action: deferred
    Holder: muse
    Version: <head sha, or the ticket's state labels>
    Resume: <what the next brain does>
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import lock as _lock
from . import text as _text
from .github import GitHub, GitHubError

STAGE_MARKER = "<!-- loom:stage v1 -->"
# review and merge act on a pull request; verify, failed and ask on an issue;
# brief and report on the goal itself.
STAGES = ("review", "merge", "verify", "failed", "ask", "brief", "report")
PR_STAGES = ("review", "merge")
GOAL_STAGES = ("brief", "report")
ACTIONS = ("deferred", "done")
DEFAULT_OVERDUE_HOURS = 2
DEFAULT_LOCK_REPO = "quadseven/loom"
_FIELD = re.compile(r"^(Stage|Item|Action|Holder|Version|Resume): ?(.*)$", re.M)
_VERSION_LABELS = ("needs-operator",)


def overdue_hours(env: Optional[Dict[str, str]] = None) -> int:
    raw = (os.environ if env is None else env).get("LOOM_STAGE_OVERDUE_HOURS", "")
    return int(raw) if raw.isdigit() and int(raw) >= 1 else DEFAULT_OVERDUE_HOURS


def lock_repo(env: Optional[Dict[str, str]] = None) -> str:
    return (os.environ if env is None else env).get("LOOM_STATUS_REPO") or DEFAULT_LOCK_REPO


def _utc(now: Optional[datetime]) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    return now if now.tzinfo else now.replace(tzinfo=timezone.utc)


def _time(value: Any) -> Optional[datetime]:
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def require_lock(gh: GitHub, repo: str, holder: str,
                 now: Optional[datetime] = None) -> None:
    """Refuse unless `holder` holds the tick lock in `repo`, unexpired: only
    the current lock holder acts."""
    _lock._check_holder(holder)
    current = _lock._read_lock(gh, repo)
    if current is None or current[0] != holder:
        raise ValueError("the tick lock is not held by %s" % holder)
    if current[1] is None or current[1] <= _utc(now):
        raise ValueError("the tick lock held by %s has expired" % holder)


def issue_version(labels: Iterable[str]) -> str:
    """An issue item's version: its loom state labels and whether it is an ask.
    A ticket moved on (re-briefed, merged) or an ask answered changes it."""
    names = sorted(x for x in labels if x.startswith("loom:") or x in _VERSION_LABELS)
    return ",".join(names) or "open"


def render(stage: str, item: int, action: str, holder: str, version: str,
           resume: str = "") -> str:
    lines = [STAGE_MARKER, "Stage: %s" % stage, "Item: #%d" % item,
             "Action: %s" % action, "Holder: %s" % holder, "Version: %s" % version]
    if resume.strip():
        lines.append("Resume: %s" % " ".join(resume.split()))
    return "\n".join(lines)


def parse(body: str) -> Optional[Dict[str, Any]]:
    """The fields of a stage record, or None for any other comment."""
    if not (body or "").startswith(STAGE_MARKER):
        return None
    fields = {k.lower(): v.strip() for k, v in _FIELD.findall(body)}
    item = re.fullmatch(r"#([0-9]+)", fields.get("item", ""))
    if (fields.get("stage") not in STAGES or fields.get("action") not in ACTIONS
            or not item or not fields.get("holder")):
        return None
    fields["item"] = int(item.group(1))
    fields.setdefault("version", "")
    fields.setdefault("resume", "")
    return fields


def records(comments: Iterable[Dict[str, Any]],
            authors: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
    """Stage records from goal home comments (GraphQL nodes: body, createdAt,
    author.login), oldest first. With `authors`, records from anyone else are
    data, never ownership."""
    out = []
    for c in comments:
        login = ((c.get("author") or {}).get("login") or "")
        if authors and login not in authors:
            continue
        found = parse(c.get("body", ""))
        at = _time(c.get("createdAt"))
        if found and at:
            found["at"] = at
            out.append(found)
    return sorted(out, key=lambda r: r["at"])


def _key(record: Dict[str, Any]) -> Tuple[str, int]:
    # A pull request's review and merge are one piece of work.
    return ("pr" if record["stage"] in PR_STAGES else record["stage"], record["item"])


def _when(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def yielded(recs: Sequence[Dict[str, Any]], versions: Dict[int, str],
            goal: int) -> List[Dict[str, Any]]:
    """Resume items still waiting: the newest record for the item is a
    deferral, and the item has not changed since. `versions` maps each open
    item to its current version; a missing item is closed or merged."""
    latest: Dict[Tuple[str, int], Dict[str, Any]] = {}
    times: Dict[Tuple[str, int], int] = {}
    for r in recs:
        key = _key(r)
        if r["action"] == "done" or (key in latest and latest[key]["version"] != r["version"]):
            times[key] = 0
        if r["action"] == "deferred":
            times[key] = times.get(key, 0) + 1
        latest[key] = r
    out = []
    for key, r in latest.items():
        if r["action"] != "deferred":
            continue
        now_version = "goal" if r["stage"] in GOAL_STAGES else versions.get(r["item"])
        if now_version is None or now_version != r["version"]:
            continue
        out.append(dict(r, goal=goal, times=times[key], reason="yielded"))
    return sorted(out, key=lambda r: r["at"])


def overdue(ready: Iterable[Tuple[str, int, Optional[datetime]]], taken: Iterable[int],
            now: datetime, hours: int) -> List[Dict[str, Any]]:
    """Ready items (stage, number, last update) untouched for `hours`, that
    no resume item already covers."""
    skip = set(taken)
    out = []
    for stage, number, updated in ready:
        if number in skip or updated is None:
            continue
        if (now - updated).total_seconds() >= hours * 3600:
            out.append({"stage": stage, "item": number, "at": updated, "reason": "overdue"})
    return out


def stage_lines(goal: int, recs: Sequence[Dict[str, Any]]) -> List[str]:
    """Per stage: the brain responsible (the newest record's holder, or any
    brain once it was yielded) and the last completed action."""
    lines = []
    for stage in STAGES:
        mine = [r for r in recs if r["stage"] == stage]
        if not mine:
            continue
        last = mine[-1]
        done = [r for r in mine if r["action"] == "done"]
        owner = ("yielded by %s, any lock holder" % last["holder"]
                 if last["action"] == "deferred" else last["holder"])
        finished = ("#%d by %s at %s" % (done[-1]["item"], done[-1]["holder"], _when(done[-1]["at"]))
                    if done else "none")
        lines.append("- #%d %s: responsible %s; last done %s" % (goal, stage, owner, finished))
    return lines


def takeover_line(item: Dict[str, Any]) -> str:
    if item["reason"] == "yielded":
        line = "- %s #%d (goal #%d) - yielded by %s at %s, deferred %d times" % (
            item["stage"], item["item"], item["goal"], item["holder"], _when(item["at"]),
            item["times"])
        return line + (" - resume: %s" % item["resume"] if item["resume"] else "")
    return "- %s #%d - overdue, untouched since %s" % (item["stage"], item["item"], _when(item["at"]))


def _version(gh: GitHub, repo: str, stage: str, goal: int, item: int) -> str:
    if stage in GOAL_STAGES:
        if item != goal:
            raise ValueError("a %s record's item is the goal itself" % stage)
        return "goal"
    if stage in PR_STAGES:
        pr = gh.rest("GET", "/repos/%s/pulls/%d" % (repo, item)).data or {}
        sha = (pr.get("head") or {}).get("sha")
        if pr.get("state") != "open" or not sha:
            raise ValueError("pull request is not open")
        return str(sha)
    issue = gh.rest("GET", "/repos/%s/issues/%d" % (repo, item)).data or {}
    if issue.get("state") != "open" or "pull_request" in issue:
        raise ValueError("item is not an open issue")
    return issue_version(str(x.get("name", "")) for x in issue.get("labels", []))


def record(gh: GitHub, repo: str, goal: int, stage: str, action: str, holder: str,
           item: Optional[int] = None, resume: str = "",
           lock_in: Optional[str] = None, now: Optional[datetime] = None) -> str:
    """Post a stage record on the goal home, as the current lock holder only.
    A deferral names what the next brain resumes; done closes the item."""
    if stage not in STAGES:
        raise ValueError("unknown stage")
    if action not in ACTIONS:
        raise ValueError("action is deferred or done")
    if action == "deferred" and not resume.strip():
        raise ValueError("a deferral needs a resume item")
    _text.check(resume)
    require_lock(gh, lock_in or lock_repo(), holder, now)
    item = goal if item is None else item
    try:
        home = gh.rest("GET", "/repos/%s/issues/%d" % (repo, goal)).data or {}
    except GitHubError as exc:
        if exc.status == 404:
            raise ValueError("goal must be an open loom:goal issue") from None
        raise
    names = [str(x.get("name", "")) for x in home.get("labels", [])]
    if home.get("state") != "open" or "loom:goal" not in names:
        raise ValueError("goal must be an open loom:goal issue")
    version = _version(gh, repo, stage, goal, item)
    body = render(stage, item, action, holder, version, resume)
    gh.rest("POST", "/repos/%s/issues/%d/comments" % (repo, goal), {"body": body})
    return body
