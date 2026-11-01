"""Consolidate goal homes: a plan file names the few top-level goals and what
becomes of every other home (docs/BRAIN.md, Consolidating goal homes).

A plan names real repos, so it stays a local file and never lands in this
repo:

    {"top": ["OWNER/NAME#N"],
     "homes": [{"home": "OWNER/NAME#N", "action": "sub-goal", "parent": "OWNER/NAME#N"},
               {"home": "OWNER/NAME#N", "action": "queue", "parent": "OWNER/NAME#N"},
               {"home": "OWNER/NAME#N", "action": "merge", "parent": "OWNER/NAME#N"}]}

- sub-goal: the home rolls up into `parent`. Its own tickets stay on it.
- queue: the same for a loom:backlog home. It stays open and labelled, because
  intake needs it and onboard opens a new one if it closes, and the Goals page
  leaves it out of the parent's rows.
- merge: the home's open tickets move to `parent`, then the home closes as a
  duplicate. `parent` must be open and in the same repo.

The Goals page reads a `Parent goal:` line on a home and the `Part of` line
or sub-issue parent on an epic, so each kind gets the pointer the page reads.

survey() only reads, and apply() writes nothing when survey found a problem:
plans go stale, and a merge into a closed home would orphan its tickets. A run
stopped part way is finished by running it again, since a home already in
place is left alone, and a home keeps its tickets until every move is done.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from . import brain, notify, shadow
from .github import GitHub, GitHubError

MAX_TOP = 10
ACTIONS = ("sub-goal", "queue", "merge")
_REF = re.compile(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)")
_ISSUE_URL = re.compile(r"/repos/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/(\d+)$")
_REPO_URL = re.compile(r"/repos/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)$")
_PARENT_LINE = re.compile(r"^([ \t]*(?:[-*][ \t]*)?Parent goal[ \t]*:[ \t]*)"
                          r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#(\d+)\b", re.I | re.M)
_PART_OF_LINE = re.compile(r"^([ \t]*Part of[ \t]+)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#?(\d+)\b",
                           re.I | re.M)


@dataclass(frozen=True)
class Entry:
    home: str
    action: str
    parent: str


@dataclass(frozen=True)
class Plan:
    top: Tuple[str, ...]
    homes: Tuple[Entry, ...]


@dataclass
class Step:
    """One home's write. `tickets` are the open tickets a merge moves."""
    entry: Entry
    issue: Dict[str, Any]
    line: str
    tickets: List[Dict[str, Any]] = field(default_factory=list)
    writes: bool = True


@dataclass
class Survey:
    steps: List[Step] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)

    def lines(self) -> List[str]:
        return [step.line for step in self.steps] + self.skipped


@dataclass
class Result:
    changed: int = 0
    moved: int = 0
    notes: List[str] = field(default_factory=list)


def _split(ref: str) -> Tuple[str, int]:
    match = _REF.fullmatch(str(ref))
    if not match:
        raise ValueError("expected OWNER/NAME#N, got %r" % (ref,))
    return match.group(1), int(match.group(2))


def _entry(raw: Any) -> Entry:
    if not isinstance(raw, dict):
        raise ValueError("a home is an object with home, action and parent: %r" % (raw,))
    entry = Entry(str(raw.get("home")), str(raw.get("action")), str(raw.get("parent")))
    if entry.action not in ACTIONS:
        raise ValueError("%s: action is one of %s" % (entry.home, ", ".join(ACTIONS)))
    home_repo, _ = _split(entry.home)
    parent_repo, _ = _split(entry.parent)
    if entry.action == "merge" and home_repo != parent_repo:
        raise ValueError("%s: a merge stays in the same repo" % entry.home)
    return entry


def _check_reaches_top(plan: Plan) -> None:
    parents = {entry.home: entry.parent for entry in plan.homes}
    for entry in plan.homes:
        seen, at = {entry.home}, entry.parent
        while at in parents and at not in seen:
            seen.add(at)
            at = parents[at]
        if at not in plan.top:
            raise ValueError("%s reaches no top-level home" % entry.home)


def parse_plan(data: Any) -> Plan:
    """The plan from its JSON form; ValueError for a plan that cannot be right
    whatever the tracker says."""
    if not isinstance(data, dict):
        raise ValueError("a plan is an object with top and homes")
    plan = Plan(tuple(str(ref) for ref in data.get("top") or ()),
                tuple(_entry(raw) for raw in data.get("homes") or ()))
    for ref in plan.top:
        _split(ref)
    if len(plan.top) > MAX_TOP:
        raise ValueError("a plan keeps %d or fewer top-level homes, not %d" % (MAX_TOP, len(plan.top)))
    named = list(plan.top) + [entry.home for entry in plan.homes]
    twice = sorted({ref for ref in named if named.count(ref) > 1})
    if twice:
        raise ValueError("listed more than once: %s" % ", ".join(twice))
    _check_reaches_top(plan)
    return plan


def load_plan(path: str) -> Plan:
    with open(path, encoding="utf-8") as handle:
        return parse_plan(json.load(handle))


def _read(gh: GitHub, ref: str, cache: Dict[str, Optional[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    """The issue, or None when it does not exist."""
    if ref not in cache:
        repo, number = _split(ref)
        try:
            cache[ref] = gh.rest("GET", "/repos/%s/issues/%d" % (repo, number)).data or {}
        except GitHubError as err:
            if err.status not in (404, 410):
                raise
            cache[ref] = None
    return cache[ref]


def _names(issue: Dict[str, Any]) -> List[str]:
    return [str(label.get("name") or "").lower() for label in issue.get("labels") or []]


def _is_open(issue: Optional[Dict[str, Any]]) -> bool:
    return bool(issue) and issue.get("state") == "open"


def _is_epic(issue: Dict[str, Any]) -> bool:
    """As the Goals page reads it: an epic that is not also a goal home."""
    return "epic" in _names(issue) and "loom:goal" not in _names(issue)


def _open_tickets(gh: GitHub, ref: str) -> List[Dict[str, Any]]:
    """Open issues under the home: its sub-issues (in any repo), and tickets
    in its repo with a Part of line naming it."""
    repo, number = _split(ref)
    found: Dict[Any, Dict[str, Any]] = {}
    children = list(gh.paginate("/repos/%s/issues/%d/sub_issues?per_page=100" % (repo, number)))
    for issue in children + brain._part_of_issues(gh, repo, number):
        if issue.get("state") == "open":
            found[issue.get("id")] = issue
    return list(found.values())


def _named_ref(repo: str, match: "re.Match[str]") -> str:
    return "%s#%s" % (match.group(2) or repo, match.group(3))


def _short(repo: str, target: str) -> str:
    """How a line in `repo` names `target`."""
    target_repo, number = _split(target)
    return "#%d" % number if target_repo == repo else target


def _appended(body: str, line: str) -> str:
    return "%s\n\n%s\n" % (body.rstrip("\n"), line) if body.strip() else line + "\n"


def _with_parent_line(body: str, repo: str, target: str) -> str:
    match = _PARENT_LINE.search(body)
    if not match:
        return _appended(body, "Parent goal: " + target)
    if _named_ref(repo, match) == target:
        return body
    return body[:match.start()] + match.group(1) + target + body[match.end():]


def _with_part_of(body: str, repo: str, target: str) -> str:
    named = _short(repo, target)
    match = _PART_OF_LINE.search(body)
    if not match:
        return _appended(body, "Part of " + named)
    if _named_ref(repo, match) == target:
        return body
    return body[:match.start()] + match.group(1) + named + body[match.end():]


def _issue_repo(issue: Dict[str, Any], fallback: str) -> str:
    match = _REPO_URL.search(str(issue.get("repository_url") or ""))
    return match.group(1) if match else fallback


def _link_parent(issue: Dict[str, Any]) -> Optional[str]:
    match = _ISSUE_URL.search(str(issue.get("parent_issue_url") or ""))
    return "%s#%s" % match.groups() if match else None


def _new_body(entry: Entry, issue: Dict[str, Any]) -> str:
    repo, _ = _split(entry.home)
    body = str(issue.get("body") or "")
    if _is_epic(issue):
        return _with_part_of(body, repo, entry.parent)
    return _with_parent_line(body, repo, entry.parent)


def _epic_in_place(entry: Entry, issue: Dict[str, Any]) -> bool:
    """An epic with no Part of line sits where its sub-issue link says."""
    body = str(issue.get("body") or "")
    return not _PART_OF_LINE.search(body) and _link_parent(issue) == entry.parent


def _needs_write(entry: Entry, issue: Dict[str, Any]) -> bool:
    if _is_epic(issue) and _epic_in_place(entry, issue):
        return False
    if _new_body(entry, issue) != str(issue.get("body") or ""):
        return True
    return _is_epic(issue) and _link_parent(issue) != entry.parent


def _survey_merge(gh: GitHub, entry: Entry, issue: Dict[str, Any], parent: Optional[Dict[str, Any]],
                  found: Survey) -> None:
    if "loom:backlog" in _names(issue):
        found.problems.append("%s is a backlog home: it stays open, so it is a queue, not a merge"
                              % entry.home)
    if not _is_open(parent):
        found.problems.append("%s merges into %s, which is closed or missing: its open tickets would"
                              " be orphaned" % (entry.home, entry.parent))
    tickets = _open_tickets(gh, entry.home)
    found.steps.append(Step(entry, issue, "merge %s into %s: %d open tickets move" % (
        entry.home, entry.parent, len(tickets)), tickets,
        writes=bool(tickets) or _is_open(issue)))


def _survey_move(gh: GitHub, entry: Entry, issue: Dict[str, Any], parent: Optional[Dict[str, Any]],
                 found: Survey) -> None:
    if entry.action == "queue" and "loom:backlog" not in _names(issue):
        found.problems.append("%s is a queue in the plan but has no loom:backlog label" % entry.home)
    if parent is None or (_is_open(issue) and not _is_open(parent)):
        found.problems.append("%s goes under %s, which is closed or missing" % (entry.home, entry.parent))
    writes = _needs_write(entry, issue)
    line = "%s %s under %s%s" % (entry.action, entry.home, entry.parent, "" if writes else ": in place")
    if not _is_open(issue):
        held = len(_open_tickets(gh, entry.home))
        if held:
            line += ": closed, still holds %d open ticket(s)" % held
    found.steps.append(Step(entry, issue, line, writes=writes))


def survey(gh: GitHub, plan: Plan) -> Survey:
    """Read every home the plan names and say what apply would do. No writes."""
    found = Survey()
    cache: Dict[str, Optional[Dict[str, Any]]] = {}
    for ref in plan.top:
        if not _is_open(_read(gh, ref, cache)):
            found.problems.append("top-level %s is closed or missing" % ref)
    for entry in plan.homes:
        issue = _read(gh, entry.home, cache)
        if issue is None:
            found.skipped.append("%s: no such issue, nothing to do" % entry.home)
            continue
        parent = _read(gh, entry.parent, cache)
        if entry.action == "merge":
            _survey_merge(gh, entry, issue, parent, found)
        else:
            _survey_move(gh, entry, issue, parent, found)
    return found


def _link(gh: GitHub, target: str, issue: Dict[str, Any], result: Result) -> None:
    """Make the issue a sub-issue of target. A refusal is noted, not fatal: a
    home holds GitHub's limit of 100 sub-issues, and the Part of line then
    carries the link, as it does for every ticket past the limit."""
    repo, number = _split(target)
    payload: Dict[str, Any] = {"sub_issue_id": issue["id"]}
    if _link_parent(issue):
        payload["replace_parent"] = True
    try:
        gh.rest("POST", "/repos/%s/issues/%d/sub_issues" % (repo, number), payload)
    except GitHubError as err:
        if err.status != 422:
            raise
        result.notes.append("%s refused the sub-issue link for #%s; its Part of line carries it (%s)"
                            % (target, issue.get("number"), err))


def _patch_body(gh: GitHub, repo: str, issue: Dict[str, Any], body: str) -> None:
    if body != str(issue.get("body") or ""):
        gh.rest("PATCH", "/repos/%s/issues/%d" % (repo, int(issue["number"])), {"body": body})


def _merge(gh: GitHub, step: Step, result: Result) -> None:
    """Move every open ticket, then close the home. A failed move raises
    before the close, so the home keeps what has not moved."""
    repo, number = _split(step.entry.home)
    for ticket in step.tickets:
        ticket_repo = _issue_repo(ticket, repo)
        _link(gh, step.entry.parent, ticket, result)
        _patch_body(gh, ticket_repo, ticket,
                    _with_part_of(str(ticket.get("body") or ""), ticket_repo, step.entry.parent))
        result.moved += 1
    if _is_open(step.issue):
        notify.post(gh, repo, number, "Merged into %s: %d open ticket(s) moved there." % (
            step.entry.parent, len(step.tickets)), notify.CLOSED)
        gh.rest("PATCH", "/repos/%s/issues/%d" % (repo, number),
                {"state": "closed", "state_reason": "duplicate"})


def _reparent(gh: GitHub, step: Step, result: Result) -> None:
    repo, _ = _split(step.entry.home)
    _patch_body(gh, repo, step.issue, _new_body(step.entry, step.issue))
    if _is_epic(step.issue) and _link_parent(step.issue) != step.entry.parent:
        _link(gh, step.entry.parent, step.issue, result)


def _shadowed(steps: Sequence[Step]) -> Set[str]:
    repos = {_split(step.entry.home)[0] for step in steps if step.writes}
    return {repo for repo in repos if shadow.active(repo)}


def apply(gh: GitHub, found: Survey) -> Result:
    """Run a surveyed plan; ValueError, with nothing written, on any problem
    or when a repo it writes to is in shadow mode."""
    if found.problems:
        raise ValueError("%d problem(s) in the plan; nothing written" % len(found.problems))
    held = _shadowed(found.steps)
    if held:
        raise ValueError("in shadow mode, nothing written: %s" % ", ".join(sorted(held)))
    result = Result()
    for step in found.steps:
        if not step.writes:
            continue
        (_merge if step.entry.action == "merge" else _reparent)(gh, step, result)
        result.changed += 1
    return result


def run(gh: GitHub, path: str, write: bool) -> int:
    """The consolidate-goals command: 0 when the plan holds, 1 when it does not."""
    found = survey(gh, load_plan(path))
    for line in found.lines():
        print(line)
    for problem in found.problems:
        print("problem: " + problem)
    if found.problems:
        return 1
    if write:
        result = apply(gh, found)
        for note in result.notes:
            print("note: " + note, file=sys.stderr)
        print("%d home(s) changed, %d open ticket(s) moved" % (result.changed, result.moved))
    return 0
