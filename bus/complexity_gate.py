"""The review bot's complexity rule, read on a lane's commits before a push.

A lane change that added a branch to a function already over the cap came
back from review a whole round later, and the next round did the same
(loom#1785: 25 against a cap of 15, moved from 23). The measures here copy the
review bot's (grug: services/_shared/personas/code_reviewer/complexity.py), so
a change to one is a change to both.

The rule is stricter than the review bot's on one point: the review bot lets
a function over the cap rise by a little, and this refuses any rise. A
function over the cap is never asked to come under it: the text names one
action the lane can take by itself, which is to move the lines it added into a
new small function. The runner gives the agent that text in the same run,
LOOM_PREPUSH_RETRIES times, and pushes either way (lanes/run_lane.sh, prepush_gate), since the review bot
stays the judge.

Exit 0 nothing refused, 1 refused with the text on stdout, 2 no check ran.
"""
from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from typing import Dict, List, NamedTuple, Optional, Sequence, Set, Tuple

CYCLOMATIC_CAP = 15
COGNITIVE_CAP = 25
GIT_TIMEOUT = 60
_CYCLOMATIC_NODES = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler,
                     ast.With, ast.AsyncWith, ast.IfExp, ast.comprehension, ast.Assert)
# Python 3.9, the lane machines' system python, has no match statement.
_MATCH_CASE = getattr(ast, "match_case", ())
_NESTERS = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler)
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
_HUNK = re.compile(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@")
_SIDE = re.compile(r"^\+\+\+ b/(.+)$")
# The action a function's refusal asks for, also the first step of a fix round
# (bus/brain.py, _fix_body).
NEXT_ACTION = ("move the lines this change added to `%s` into a new small function, "
               "and call it from `%s`.")


class Finding(NamedTuple):
    path: str
    line: int
    name: str
    text: str


def next_action(name: str) -> str:
    """The one action that clears a refusal of function `name`."""
    return NEXT_ACTION % (name, name)


def cyclomatic(function: ast.AST) -> int:
    """One, plus one per decision point, as the review bot counts them."""
    score = 1
    for node in ast.walk(function):
        if isinstance(node, _CYCLOMATIC_NODES):
            score += 1
        elif isinstance(node, ast.BoolOp):
            score += len(node.values) - 1
        elif isinstance(node, _MATCH_CASE):
            pattern = node.pattern
            if not isinstance(pattern, ast.MatchAs) or pattern.pattern is not None:
                score += 1
    return score


def cognitive(function: ast.AST) -> int:
    """Control flow costs one plus its depth; a boolean sequence costs one."""
    def walk(node: ast.AST, depth: int) -> int:
        total = 0
        for child in ast.iter_child_nodes(node):
            cost, inner = 0, depth
            if isinstance(child, _NESTERS):
                cost, inner = 1 + depth, depth + 1
            elif isinstance(child, ast.BoolOp):
                cost = 1
            elif isinstance(child, _FUNCTIONS):
                inner = depth + 1
            total += cost + walk(child, inner)
        return total
    return walk(function, 0)


def _functions(source: str) -> List[ast.AST]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    return [node for node in ast.walk(tree) if isinstance(node, _FUNCTIONS)]


def scores(source: str) -> Dict[str, Tuple[int, int]]:
    """Function name -> (cyclomatic, cognitive). A name used twice keeps its
    worst score, so a gentler twin never reads as a rise."""
    found: Dict[str, Tuple[int, int]] = {}
    for node in _functions(source):
        seen = found.get(node.name, (0, 0))
        found[node.name] = (max(seen[0], cyclomatic(node)), max(seen[1], cognitive(node)))
    return found


def _raised(now: Tuple[int, int], before: Optional[Tuple[int, int]]) -> bool:
    """Over a cap at head and, on a function the base holds, higher than it was."""
    over = (now[0] > CYCLOMATIC_CAP, now[1] > COGNITIVE_CAP)
    if before is None:
        return any(over)
    return (over[0] and now[0] > before[0]) or (over[1] and now[1] > before[1])


def _describe(path: str, name: str, now: Tuple[int, int],
              before: Optional[Tuple[int, int]]) -> str:
    moved = ("; this change moved it %d->%d cyclomatic, %d->%d cognitive"
             % (before[0], now[0], before[1], now[1])) if before else "; a new function"
    return "%s: `%s` is cyclomatic %d (cap %d), cognitive %d (cap %d)%s." % (
        path, name, now[0], CYCLOMATIC_CAP, now[1], COGNITIVE_CAP, moved)


def scan(path: str, head: str, base: Optional[str], changed: Set[int]) -> List[Finding]:
    """The functions at `head` a changed line touches that this change took
    over a cap or raised while over it."""
    before = scores(base) if base is not None else {}
    found: List[Finding] = []
    for node in _functions(head):
        end = getattr(node, "end_lineno", node.lineno)
        touched = sorted(line for line in changed if node.lineno <= line <= end)
        now = (cyclomatic(node), cognitive(node))
        was = before.get(node.name)
        if touched and _raised(now, was):
            found.append(Finding(path, touched[0], node.name,
                                 _describe(path, node.name, now, was)))
    return found


def failure_text(found: Sequence[Finding]) -> str:
    """What the agent reads: each refusal, then the one action, for the first."""
    name = found[0].name
    lines = [finding.text for finding in found]
    lines.append("Next action: %s Do not restructure the rest of `%s`; it may stay over the "
                 "cap as long as this change does not raise it. A function other than `%s` "
                 "listed here takes the same action." % (next_action(name), name, name))
    return "\n".join(lines)


def changed_lines(diff: str) -> Dict[str, Set[int]]:
    """Path -> the head-side lines a unified diff adds."""
    found: Dict[str, Set[int]] = {}
    current: Optional[Set[int]] = None
    for line in diff.splitlines():
        side = _SIDE.match(line)
        hunk = _HUNK.match(line)
        if side:
            current = found.setdefault(side.group(1), set())
        elif hunk and current is not None:
            start, count = int(hunk.group(1)), int(hunk.group(2) or 1)
            current.update(range(start, start + count))
    return found


def _git(tree: str, *args: str) -> "subprocess.CompletedProcess[bytes]":
    """git over a lane's tree, which a hook or fsmonitor of its own cannot steer."""
    return subprocess.run(["git", "-C", tree, "-c", "core.hooksPath=/dev/null",
                           "-c", "core.fsmonitor=false"] + list(args),
                          capture_output=True, timeout=GIT_TIMEOUT, check=False)


def _show(tree: str, revision: str, path: str) -> Optional[str]:
    """A file at a revision; None when that revision does not hold it."""
    got = _git(tree, "show", "--no-textconv", "%s:%s" % (revision, path))
    return got.stdout.decode("utf-8", "replace") if got.returncode == 0 else None


def run(tree: str, base: str) -> List[Finding]:
    """Every refusal in what HEAD adds to `base`. Raises when git cannot say."""
    diff = _git(tree, "diff", "-U0", "--no-color", "--no-ext-diff", "--no-textconv", base,
                "HEAD", "--", "*.py")
    if diff.returncode != 0:
        raise RuntimeError(diff.stderr.decode("utf-8", "replace").strip()[:300])
    found: List[Finding] = []
    for path, lines in changed_lines(diff.stdout.decode("utf-8", "replace")).items():
        head = _show(tree, "HEAD", path)
        if head is not None and lines:
            found += scan(path, head, _show(tree, base, path), lines)
    return found


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tree", required=True)
    parser.add_argument("--base", required=True)
    args = parser.parse_args(argv)
    try:
        found = run(args.tree, args.base)
    except (RuntimeError, OSError, subprocess.TimeoutExpired, MemoryError) as err:
        print("complexity check could not run: %s" % err, file=sys.stderr)
        return 2
    if found:
        print(failure_text(found))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
