"""A lane pull request that weakens its own tests is knowable from the diff.

The review bot's weakened-test send-backs rose from none to 18 in a week
(loom#2162): assertions deleted, tests skipped, an equality check loosened
to a truthiness check. Each one cost a full review round-trip to discover
what the diff already said. The station runs this scan when it first
evaluates a lane pull request's head and bounces a weakening diff back to
the lane at once, naming the lines, before the review round.

The scan is mechanical, on purpose. It reads the pull request's file
entries (the shape bus/brief_check.py's `measurement_files` reads) and
reports, in test files only:

- a deleted assertion: an assertion line removed whose text does not
  reappear among the file's added lines and whose subject no added
  assertion in its hunk still checks (a moved or reworded assertion is
  not a deletion);
- a skipped test: an added skip marker (`@pytest.mark.skip`, `it.skip`,
  `#[ignore]`, their kin) that was not there before;
- a loosened assertion: within one hunk, an assertion whose subject stays
  the same while its check drops from a strong form (an equality) to a
  weak one (truthiness, existence).

An entry whose patch GitHub withheld cannot be judged and is left to the
review, as is any weakening outside these shapes: the scan is a
pre-check, never the only review.
"""
from __future__ import annotations

import re
from typing import List, Mapping, Optional, Sequence, Tuple

_TEST_SEGMENTS = {"test", "tests", "__tests__", "spec", "specs", "testing"}
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_CALL_RE = re.compile(r"\b(assert\w*|expect)\s*(?:\.\s*(\w+)\s*)?\(")
_MATCHER_RE = re.compile(r"\.\s*(to\w+)\s*\(")
_BARE_RE = re.compile(r"^\s*assert\s+(.+?)\s*$")
_SKIP_RE = re.compile(
    r"@pytest\.mark\.skip\b|pytest\.skip\s*\(|@unittest\.skip\b|self\.skipTest\s*\("
    r"|\b(?:it|test|describe)\.skip\s*\(|\bx(?:it|test|describe)\s*\("
    r"|#\[ignore\]|@Disabled\b|\bt\.Skip\s*\(")
# Check forms that pin a value (an equality) and forms that only ask that
# something exists or is truthy. A drop from the first set to the second,
# on the same subject, is a loosening; any other pairing is a rewrite the
# review judges, not this scan.
_STRONG = {"assertEqual", "assertEquals", "assertIs", "assertAlmostEqual",
           "assertCountEqual", "assertDictEqual", "assertListEqual",
           "assertTupleEqual", "assertSetEqual", "assertSequenceEqual",
           "toBe", "toEqual", "toStrictEqual", "equal", "strictEqual",
           "deepEqual", "deepStrictEqual", "assert=="}
_WEAK = {"assertTrue", "assert_", "assertIsNotNone", "toBeTruthy",
         "toBeDefined", "ok", "assert"}
SHOWN = 120


def is_test_path(path: str) -> bool:
    """Whether the path names a test file, by the conventions of the repos
    the lanes work in: a test directory, a `test_` or `_test` file, a
    `.test.` or `.spec.` file, or a Java `Test`/`Tests` class."""
    parts = [p for p in re.split(r"[/\\]", str(path or "")) if p]
    if not parts:
        return False
    base = parts[-1].lower()
    if any(part.lower() in _TEST_SEGMENTS for part in parts[:-1]):
        return True
    return (base.startswith("test_") or base.startswith("test.")
            or "_test." in base or ".test." in base or ".spec." in base
            or base.endswith("test.java") or base.endswith("tests.java"))


def _norm(text: str) -> str:
    return " ".join(str(text).split())


def _shown(text: str) -> str:
    return _norm(text)[:SHOWN]


def _subject(line: str, open_at: int) -> str:
    """The first argument inside the call whose `(` sits at `open_at`: the
    value the assertion checks, up to the first comma at its own depth."""
    depth = 0
    out: List[str] = []
    for ch in line[open_at:]:
        if ch == "(":
            depth += 1
            if depth == 1:
                continue
        elif ch == ")":
            depth -= 1
            if depth == 0:
                break
        if depth == 1 and ch == ",":
            break
        if depth >= 1:
            out.append(ch)
    return _norm("".join(out))


def _assertion(line: str) -> Optional[Tuple[str, str]]:
    """(verb, subject) of the assertion a line makes, or None. The verb is
    the check's form: `assertEqual`, a matcher's `toBe`, or `assert==` /
    `assert` for a bare assert with or without an equality."""
    call = _CALL_RE.search(line)
    if call:
        verb = call.group(2) or call.group(1)
        if call.group(1) == "expect":
            matcher = _MATCHER_RE.search(line, call.end())
            verb = matcher.group(1) if matcher else "expect"
        return verb, _subject(line, call.end() - 1)
    bare = _BARE_RE.match(line)
    if bare:
        rest = bare.group(1)
        subject = re.split(r"==|!=", rest, maxsplit=1)[0].strip()
        return ("assert==" if ("==" in rest or "!=" in rest) else "assert"), _norm(subject)
    return None


def _strength(verb: str) -> int:
    if verb in _STRONG:
        return 2
    if verb in _WEAK:
        return 1
    return 0


def _hunks(patch: str) -> List[Tuple[List[Tuple[int, str]], List[Tuple[int, str]]]]:
    """(removed, added) per hunk, each a list of (line number, text): the
    number a removed line had before the change, an added line after it."""
    hunks: List[Tuple[List[Tuple[int, str]], List[Tuple[int, str]]]] = []
    removed: Optional[List[Tuple[int, str]]] = None
    added: Optional[List[Tuple[int, str]]] = None
    old_no = new_no = 0
    for line in str(patch).splitlines():
        head = _HUNK_RE.match(line)
        if head:
            removed, added = [], []
            hunks.append((removed, added))
            old_no, new_no = int(head.group(1)), int(head.group(2))
            continue
        if removed is None or added is None:
            continue
        if line.startswith("-"):
            removed.append((old_no, line[1:]))
            old_no += 1
        elif line.startswith("+"):
            added.append((new_no, line[1:]))
            new_no += 1
        elif line.startswith("\\"):
            continue
        else:
            old_no += 1
            new_no += 1
    return hunks


def _hunk_findings(path: str, removed: List[Tuple[int, str]],
                   added: List[Tuple[int, str]], added_texts: set,
                   removed_texts: set) -> List[str]:
    """One hunk's findings. A loosened or skipped finding is numbered by
    the added line, the line in the pull request's head the lane must fix;
    a deleted assertion exists only before the change, so its finding
    carries the number it had and quotes its text."""
    found: List[str] = []
    added_asserts = [(no, text, _assertion(text)) for no, text in added]
    loosened_from = set()
    loosened_to = set()
    for no, text in removed:
        mine = _assertion(text)
        if mine is None or _strength(mine[0]) < 2 or not mine[1]:
            continue
        for a_no, a_text, a_assert in added_asserts:
            if a_assert is None or a_no in loosened_to:
                continue
            if a_assert[1] == mine[1] and 0 < _strength(a_assert[0]) < _strength(mine[0]):
                loosened_from.add(no)
                loosened_to.add(a_no)
                found.append("%s:%d: assertion loosened: %s -> %s"
                             % (path, a_no, _shown(text), _shown(a_text)))
                break
    for no, text in removed:
        mine = _assertion(text)
        if no in loosened_from or mine is None:
            continue
        if _norm(text) in added_texts:
            continue  # the assertion moved; it is still there
        if mine[1] and any(a_assert and a_assert[1] == mine[1]
                           for _, _, a_assert in added_asserts):
            continue  # reworded in its hunk; the pairing above judged it
        found.append("%s:%d: assertion deleted: %s" % (path, no, _shown(text)))
    for no, text in added:
        if _SKIP_RE.search(text) and _norm(text) not in removed_texts:
            found.append("%s:%d: test skipped: %s" % (path, no, _shown(text)))
    return found


def find(entries: Sequence[Mapping[str, object]]) -> List[str]:
    """One line per weakening in the pull request's test files, naming the
    file, the line and what was weakened, in the order the diff shows
    them. An entry with no patch cannot be judged and is skipped."""
    found: List[str] = []
    for entry in entries:
        path = str(entry.get("filename") or "")
        patch = entry.get("patch")
        if not is_test_path(path) or not isinstance(patch, str) or not patch:
            continue
        hunks = _hunks(patch)
        added_texts = {_norm(text) for _, added_lines in hunks for _, text in added_lines}
        removed_texts = {_norm(text) for removed_lines, _ in hunks for _, text in removed_lines}
        for removed, added in hunks:
            found.extend(_hunk_findings(path, removed, added, added_texts, removed_texts))
    return found
