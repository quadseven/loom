"""Turn one triaged issue into a ticket or an operator ask."""
from __future__ import annotations

import json
import re
import shlex
import subprocess
from typing import Any, Dict

from . import brain, text, tickets
from .github import GitHub


AGENT_TIMEOUT = 180
HUMAN_NEED = re.compile(
    r"\b(?:money|pay|payment|purchas\w*|buy|budget|hardware|device|account|"
    r"credential|password|secret|token|operator|decid\w*|decision|approv\w*)\b",
    re.IGNORECASE)


def _prompt(issue: Dict[str, Any], comments: list) -> str:
    source = {
        "title": issue.get("title") or "",
        "body": issue.get("body") or "",
        "labels": [label.get("name") for label in issue.get("labels", [])],
        "last_10_comments": [comment.get("body") or "" for comment in comments[-10:]],
    }
    return (
        "Rewrite this triaged issue as a self-contained Loom ticket. The body "
        "must follow the docs/BRAIN.md brief format, without header lines:\n"
        "## Why\n...\n## What\n...\n## Acceptance criteria\n- [ ] ...\n"
        "## Size\nXS | S | M | L\n## Out of scope\n- ...\n\n"
        "Return exactly one JSON object, with no Markdown wrapper: "
        '{"action":"brief","title":"...","difficulty":"easy|medium|hard",'
        '"body":"..."}, or {"action":"human","ask":"..."} only if money, '
        "hardware, an account, a credential, or an operator decision is needed, "
        'or {"action":"skip","reason":"..."}. Do not include private '
        "identifiers or credentials in the answer. Never claim the original "
        "issue is closed.\n\nIssue:\n%s\n" % json.dumps(source)
    )


def _answer(raw: str) -> Dict[str, str]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("agent answer is not JSON: %s" % exc.msg) from None
    if not isinstance(data, dict):
        raise ValueError("agent answer is not an object")
    action = data.get("action")
    fields = {"brief": {"action", "title", "difficulty", "body"},
              "human": {"action", "ask"}, "skip": {"action", "reason"}}
    if action not in fields or set(data) != fields[action]:
        raise ValueError("agent answer has unknown action or fields")
    if any(not isinstance(data[key], str) or not data[key].strip() for key in fields[action]):
        raise ValueError("agent answer has an empty field")
    if action == "brief":
        if data["difficulty"] not in tickets.DIFFICULTIES:
            raise ValueError("invalid difficulty")
        if "\n" in data["title"] or "\r" in data["title"]:
            raise ValueError("title is not one line")
        headers, _ = tickets.parse(data["body"])
        if headers:
            raise ValueError("brief body must not include ticket headers")
        missing = brain.missing_sections(data["body"])
        if missing:
            raise ValueError("brief missing %s" % ", ".join(missing))
        text.check(data["title"] + "\n" + data["body"])
    else:
        text.check(data["ask"] if action == "human" else data["reason"])
        if action == "human" and not HUMAN_NEED.search(data["ask"]):
            raise ValueError("human ask does not name an operator-only need")
    return data


def one(gh: GitHub, repo: str, goal: int, issue: Dict[str, Any], command: str) -> str:
    """Run the configured command once. Return the validated action."""
    argv = shlex.split(command)
    if not argv:
        raise ValueError("rewrite command is empty")
    number = int(issue["number"])
    comments = gh.paginate("/repos/%s/issues/%d/comments?per_page=100" % (repo, number))
    result = subprocess.run(argv, input=_prompt(issue, comments), capture_output=True,
                            text=True, timeout=AGENT_TIMEOUT, check=False)
    if result.returncode:
        raise ValueError("agent command exited %d" % result.returncode)
    answer = _answer(result.stdout)
    if answer["action"] == "brief":
        brain.brief(gh, repo, goal, answer["title"], answer["body"],
                    difficulty=answer["difficulty"], refs=number)
    elif answer["action"] == "human":
        brain.human(gh, repo, number, answer["ask"])
    return answer["action"]
