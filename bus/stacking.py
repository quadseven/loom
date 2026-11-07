"""GitHub stack ordering for lane pull requests."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import hunk_overlap

OVERLAP_WAIT_STATUS = "stack-overlap: wait:gate"
OVERLAP_CLEAR_STATUS = "stack-overlap: clear"


def _members(stack: Dict[str, Any]) -> List[int]:
    pulls = stack.get("pull_requests")
    if not isinstance(pulls, list):
        raise ValueError("pull request stack has no ordered members")
    numbers = []
    for pull in pulls:
        value = pull.get("number") if isinstance(pull, dict) else None
        if not isinstance(value, int) or value < 1:
            raise ValueError("pull request stack has an invalid member")
        numbers.append(value)
    return numbers


def _stack_for(gh: Any, repo: str, number: int) -> Optional[Dict[str, Any]]:
    data = gh.paginate("/repos/%s/stacks?pull_request=%d" % (repo, number))
    if not isinstance(data, list):
        raise ValueError("GitHub returned an invalid pull request stack list")
    stacks = [stack for stack in data if isinstance(stack, dict)]
    if len(stacks) > 1:
        raise ValueError("pull request belongs to multiple stacks")
    return stacks[0] if stacks else None


def link_pull_requests(gh: Any, repo: str, lower: int, upper: int) -> None:
    """Create or extend a stack ordered from its bottom pull request upward."""
    if lower < 1 or upper < 1 or lower == upper:
        raise ValueError("stack members must be distinct positive pull request numbers")
    lower_stack = _stack_for(gh, repo, lower)
    upper_stack = _stack_for(gh, repo, upper)
    if upper_stack:
        members = _members(upper_stack)
        if (lower_stack and lower_stack.get("number") == upper_stack.get("number")
                and lower in members and upper in members
                and members.index(lower) < members.index(upper)):
            return
        raise ValueError("upper pull request already belongs to a different stack order")
    if lower_stack:
        members = _members(lower_stack)
        if not members or members[-1] != lower:
            raise ValueError("named lower pull request is not the top of its stack")
        number = lower_stack.get("number")
        if not isinstance(number, int) or number < 1:
            raise ValueError("existing pull request stack has no usable number")
        gh.rest("POST", "/repos/%s/stacks/%d/add" % (repo, number),
                {"pull_requests": [upper]})
        return
    gh.rest("POST", "/repos/%s/stacks" % repo,
            {"pull_requests": [lower, upper]})


def has_unmerged_lower(gh: Any, repo: str, upper: int) -> bool:
    """Whether a stack member has an earlier pull request not yet merged."""
    stack = _stack_for(gh, repo, upper)
    if stack is None:
        return False
    members = _members(stack)
    if upper not in members:
        raise ValueError("GitHub's stack response omitted the requested pull request")
    position = members.index(upper)
    for number in members[:position]:
        pull = gh.rest("GET", "/repos/%s/pulls/%d" % (repo, number)).data
        if not isinstance(pull, dict) or not pull.get("merged_at"):
            return True
    return False


def overlapping_open_pull_request(gh: Any, repo: str, number: int) -> Optional[int]:
    """Find the oldest open same-base pull request with overlapping hunks."""
    current = gh.rest("GET", "/repos/%s/pulls/%d" % (repo, number)).data
    if not isinstance(current, dict):
        raise ValueError("GitHub returned an invalid pull request")
    base = current.get("base")
    base_ref = base.get("ref") if isinstance(base, dict) else None
    created = str(current.get("created_at") or "")
    if not base_ref or not created:
        raise ValueError("pull request has no usable base or creation time")
    pulls = gh.paginate("/repos/%s/pulls?state=open&per_page=100" % repo)
    if not isinstance(pulls, list):
        raise ValueError("GitHub returned an invalid open pull request list")
    candidates = sorted((pull for pull in pulls if isinstance(pull, dict)
                         and pull.get("state") == "open"
                         and int(pull.get("number") or 0) != number
                         and str(((pull.get("base") or {}).get("ref") or "")) == base_ref
                         and str(pull.get("created_at") or "") < created),
                        key=lambda pull: str(pull.get("created_at") or ""))
    current_files = hunk_overlap.pull_files(gh, repo, number)
    for older in candidates:
        older_number = int(older.get("number") or 0)
        if older_number < 1:
            continue
        if hunk_overlap.files_overlap(hunk_overlap.pull_files(gh, repo, older_number),
                                      current_files):
            return older_number
    return None
