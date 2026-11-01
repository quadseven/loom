"""The answer a fake GitHub gives to status's sub-issue query
(brain._sub_issue_tickets), built from the same REST-shaped issue lists the
fake serves at /issues/N/sub_issues, so a test's sub-issues read the same
either way (loom#2461)."""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, List

_ALIAS = re.compile(r"e(\d+): issue\(number:(\d+)\) \{ subIssues\(first:100\)")


def asks(query: str) -> bool:
    """Whether a GraphQL query is the sub-issue query."""
    return bool(_ALIAS.search(query or ""))


def numbers(query: str) -> List[int]:
    """The issue numbers the sub-issue query asks for, in order."""
    return [int(n) for _, n in _ALIAS.findall(query or "")]


def node(issue: Dict[str, Any]) -> Dict[str, Any]:
    """A REST issue as the query's GraphQL node."""
    return {"number": issue.get("number"), "title": issue.get("title"),
            "state": str(issue.get("state") or "open").upper(),
            "createdAt": issue.get("created_at"), "closedAt": issue.get("closed_at"),
            "updatedAt": issue.get("updated_at"),
            "labels": {"nodes": [{"name": x.get("name") if isinstance(x, dict) else x}
                                 for x in issue.get("labels") or []]}}


def answer(query: str, subs: Callable[[int], List[Dict[str, Any]]]) -> Dict[str, Any]:
    """The query's answer body, each issue's sub-issues from subs(number)."""
    repository = {}
    for n in numbers(query):
        found = [x for x in subs(n) if isinstance(x, dict)]
        repository["e%d" % n] = {"subIssues": {"totalCount": len(found),
                                               "nodes": [node(x) for x in found]}}
    return {"data": {"repository": repository}}
