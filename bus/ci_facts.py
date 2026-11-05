"""What a red CI job's log says failed: the failing tests, the assertion
lines, and a command the failure text names to regenerate a file.

A fix round's brief quoted the log's tail, and a lane had to find the
failing test and the fix in it. A lane pull request in the deploy repo
failed a suite that compares a generated rbac.yaml with its source; the
failure text named the command that regenerates it, and the fix rounds still
edited the file by hand (loom#1773). The station puts these facts at the top
of each failing check in a loom:fix comment (bus/brain.py _fix_body), and a
regenerate command first of all.

A review app's check-run output is a reviewer's prose, not a log. On loom#665
the station read the start of an Elder "Suggested fix:" paragraph as a
regenerate command and told the lane to run it every round (loom#1898). In
review text (ReviewText) only what the reviewer marks as a command is one: a
line in a shell-tagged fenced block, or a "Run:" or "Regenerate:" line.
"Suggested fix:" lines are kept as suggestions, quoted as the finding to
address, never as a command.

Pure text: no GitHub reads, no imports from the rest of bus/.
"""
from __future__ import annotations

import re
from typing import List, NamedTuple, Sequence

TESTS_MAX = 10
ASSERTIONS_MAX = 8
COMMANDS_MAX = 2
LINE_MAX = 300
COMMAND_MAX = 200

# pytest's short summary ("FAILED tests/test_x.py::test_y - AssertionError"),
# unittest ("FAIL: test_y (pkg.mod.Case.test_y)"), go ("--- FAIL: TestY"),
# TAP and node --test ("not ok 3 - renders the page").
_PYTEST = re.compile(r"^(?:FAILED|ERROR)\s+(\S+::\S+)")
_UNITTEST = re.compile(r"^(?:FAIL|ERROR):\s+(\w+)\s+\(([\w.]+)\)")
_GO = re.compile(r"^\s*--- FAIL:\s+(\S+)")
_TAP = re.compile(r"^\s*not ok\s+\d+\s+-\s+(.+?)\s*$")
_ASSERTION = re.compile(
    r"AssertionError|\bassert\b|^\s*E\s{2,}\S|\bExpected\b|\bexpected\b.*\b(?:got|but|to)\b"
    r"|\bnot equal\b|\bmismatch\b|\bdiffers\b")
# A line that says a file must be regenerated, and the command in it.
_REGEN_HINT = re.compile(
    r"regenerat|re-?render|out of date|out-of-date|\bstale\b|not up to date|\bdrift"
    r"|was not updated|\bto update\b|\bupdate it with\b", re.I)
_QUOTED = re.compile(r"`([^`]+)`|'([^']+)'|\"([^\"]+)\"")
_AFTER_VERB = re.compile(r"\b(?:run|rerun|re-run|with|using|via)\b:?\s+(.+)$", re.I)
# A command that writes a file with a redirect, the shape of a render step:
# "python3 tool.py --render-rbac > deploy/rbac.yaml".
_RENDER = re.compile(r"((?:[\w./-]+=\S+\s+)*[\w./-]+(?:\s+[^\s>|;&`'\"]+)*\s+--?[\w-]+"
                     r"(?:\s+[^\s>|;&`'\"]+)*\s*>\s*[\w./-]+)")
_LEADING_VERB = re.compile(r"^(?:(?:please|try|run|rerun|re-run|with|using|via)\s+)+", re.I)
_PROGRAM = re.compile(r"^(?:[\w./-]+=\S+\s+)*[\w./-]+$")
# Review text: a fence line and its language tag, the tags that mark a shell
# command, a line that marks a command, and a "Suggested fix:" line.
_FENCE = re.compile(r"^\s*(?:`{3,}|~{3,})\s*([\w+-]*)")
_SHELL_TAGS = ("sh", "bash", "shell", "console", "zsh")
_MARKED = re.compile(r"^\s*(?:[-*]\s+)?(?:\*\*)?(?:Run|Regenerate)(?:\*\*)?:(?:\*\*)?\s+(.+)$")
_SUGGESTED = re.compile(r"^\s*(?:[-*]\s+)?(?:\*\*)?Suggested fix\b", re.I)
SUGGESTIONS_MAX = 4


class ReviewText(str):
    """A failing check's text that a reviewer wrote (a review app's check-run
    output), not a CI log: only what it marks as a command is one."""


class Facts(NamedTuple):
    tests: List[str]
    assertions: List[str]
    regenerate: List[str]
    suggestions: List[str]


def _clean(line: str) -> str:
    return "".join(c if " " <= c <= "~" else " " for c in line).strip()[:LINE_MAX]


def _test_name(line: str) -> str:
    found = _PYTEST.match(line)
    if found:
        return found.group(1)
    found = _UNITTEST.match(line)
    if found:
        test, where = found.group(1), found.group(2)
        return where if where.endswith("." + test) else "%s.%s" % (where, test)
    found = _GO.match(line) or _TAP.match(line)
    return found.group(1) if found else ""


def _command_ok(text: str) -> bool:
    """A plausible shell command: printable ASCII, bounded, at least a
    program and one argument, no backtick, and its first word a program."""
    text = text.strip()
    words = text.split()
    return (0 < len(text) <= COMMAND_MAX and len(words) >= 2 and "`" not in text
            and text == "".join(c for c in text if " " <= c <= "~")
            and bool(_PROGRAM.match(words[0])) and not words[0].endswith(":"))


def _trimmed(text: str) -> str:
    return text.strip().rstrip(".,;:)").strip()


def regenerate_commands(line: str) -> List[str]:
    """The regenerate commands one log line names: a quoted command or the
    text after "run"/"with" on a line that says a file is stale or must be
    regenerated, or any command that renders into a file with a redirect."""
    line = _clean(line)
    found: List[str] = []
    if _REGEN_HINT.search(line):
        for match in _QUOTED.finditer(line):
            text = _trimmed(next(g for g in match.groups() if g is not None))
            if _command_ok(text):
                found.append(text)
        if not found:
            after = _AFTER_VERB.search(line)
            if after and _command_ok(_trimmed(after.group(1))):
                found.append(_trimmed(after.group(1)))
    if not found:
        for match in _RENDER.finditer(line):
            text = _LEADING_VERB.sub("", _trimmed(match.group(1)))
            if _command_ok(text):
                found.append(text)
    return found


def names_a_regenerate_command(line: str) -> bool:
    """Whether a log line names a regenerate command (log_excerpt keeps it)."""
    return bool(regenerate_commands(line))


def review_commands(text: str) -> List[str]:
    """The commands a reviewer marks in review text: each line of a fenced
    block tagged sh, bash, shell, console or zsh (a leading "$ " dropped),
    and the rest of a "Run:" or "Regenerate:" line outside a fence. Prose,
    an untagged fence and a "suggestion" fence give none (loom#1898)."""
    found: List[str] = []
    fence = None
    for raw in (text or "").replace("\r", "").split("\n"):
        opened = _FENCE.match(raw)
        if opened:
            fence = None if fence is not None else opened.group(1).lower()
            continue
        line = _clean(raw)
        if fence is not None:
            if fence in _SHELL_TAGS:
                cmd = _trimmed(line[2:] if line.startswith("$ ") else line)
                if _command_ok(cmd):
                    found.append(cmd)
            continue
        marked = _MARKED.match(line)
        if marked:
            cmd = _trimmed(marked.group(1))
            if len(cmd) > 1 and cmd[0] == cmd[-1] == "`":
                cmd = cmd[1:-1].strip()
            if _command_ok(cmd):
                found.append(cmd)
    return found


def suggestions(text: str) -> List[str]:
    """The "Suggested fix:" lines in review text: guidance the brief quotes
    as the finding to address, never a command to run (loom#1898)."""
    return [_clean(line) for line in (text or "").replace("\r", "").split("\n")
            if _SUGGESTED.match(line)]


def _unique(items: Sequence[str], limit: int) -> List[str]:
    seen: List[str] = []
    for item in items:
        if item and item not in seen:
            seen.append(item)
    return seen[:limit]


def facts(text: str, review: bool = False) -> Facts:
    """The failing tests, assertion lines and regenerate commands in a
    failing check's log text, each list deduplicated and bounded. For review
    text the commands are only those the reviewer marks (review_commands),
    and its "Suggested fix:" lines are kept as suggestions."""
    lines = (text or "").replace("\r", "").split("\n")
    tests = [_test_name(line) for line in lines]
    assertions = [_clean(line) for line in lines if _ASSERTION.search(line)]
    if review:
        commands, suggested = review_commands(text), suggestions(text)
    else:
        commands, suggested = [cmd for line in lines for cmd in regenerate_commands(line)], []
    return Facts(_unique(tests, TESTS_MAX), _unique(assertions, ASSERTIONS_MAX),
                 _unique(commands, COMMANDS_MAX), _unique(suggested, SUGGESTIONS_MAX))
