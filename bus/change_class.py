"""Classify a pull request's changed files into one change class.

Highest wins: sensitive, logic, tests, docs.

- sensitive: any path matching the repo's sensitive globs, read from
  .loom/sensitive-paths on the default branch (one glob per line,
  lines starting with # are comments). No file means no sensitive class.
- logic: any other behavior-carrying file (not docs, not a test file).
- tests: only test files (the test-file rule from the no-test check:
  a test or tests folder in the path, or test_, _test., .test. or
  .spec. in the name).
- docs: only *.md, docs/, LICENSE, *.txt.

The lane runner writes an ## Intent section at the top of every lane PR
body with the class, one line per behavior change, and the counts of
files by class. The PR also gets a class:<name> label.
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import re
import sys
from pathlib import PurePosixPath
from typing import Dict, List, Sequence

CLASSES = ("sensitive", "logic", "tests", "docs")
SENSITIVE_FILE = ".loom/sensitive-paths"
SUMMARY_FILE = ".loom/summary.md"

DOC_EXTS = (".md", ".txt")


def parse_patterns(text: str) -> List[str]:
    """Globs from a sensitive-paths file body; # comments and blanks ignored."""
    out: List[str] = []
    for line in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        out.append(stripped)
    return out


def load_patterns(path: str) -> List[str]:
    """Globs from a sensitive-paths file on disk; missing file means []."""
    try:
        with open(path, encoding="utf-8") as fh:
            return parse_patterns(fh.read())
    except OSError:
        return []


def is_docs(path: str) -> bool:
    lowered = (path or "").lower().strip()
    if not lowered:
        return False
    base = lowered.rsplit("/", 1)[-1]
    if base == "license":
        return True
    if lowered == "license":
        return True
    if lowered.endswith(DOC_EXTS):
        return True
    if lowered == "docs" or lowered.startswith("docs/"):
        return True
    return False


_TEST_FOLDERS = {"test", "tests", "t", "spec", "__tests__"}
_TEST_NAME_RE = re.compile(r"^test_|_test\.|\.test\.|\.spec\.")


def is_test(path: str) -> bool:
    """The existing test-file rule: a test/tests folder in the path or a
    test-like file name."""
    lowered = (path or "").lower()
    if not lowered:
        return False
    folders, _, name = lowered.rpartition("/")
    if bool(_TEST_FOLDERS & set(folders.split("/"))):
        return True
    if name.startswith("test"):
        return True
    return bool(_TEST_NAME_RE.search(name))


def is_sensitive(path: str, patterns: Sequence[str]) -> bool:
    """Does path match any sensitive glob? A pattern ending in / is a
    directory prefix (matches the dir itself and everything under it)."""
    p = (path or "").strip()
    if not p or not patterns:
        return False
    norm = p[2:] if p.startswith("./") else p
    if not norm:
        return False
    for pat in patterns:
        pat = (pat or "").strip()
        if not pat:
            continue
        if pat.endswith("/"):
            prefix = pat.rstrip("/")
            if not prefix:
                continue
            if norm == prefix or norm.startswith(prefix + "/"):
                return True
            continue
        if fnmatch.fnmatchcase(norm, pat):
            return True
        try:
            if PurePosixPath(norm).match(pat):
                return True
        except (ValueError, IndexError):
            pass
        base = norm.rsplit("/", 1)[-1]
        if fnmatch.fnmatchcase(base, pat):
            return True
    return False


def file_class(path: str, patterns: Sequence[str] = ()) -> str:
    """The class of one file: sensitive, tests, docs, else logic."""
    if is_sensitive(path, patterns):
        return "sensitive"
    if is_test(path):
        return "tests"
    if is_docs(path):
        return "docs"
    return "logic"


def count_by_class(files: Sequence[str],
                   patterns: Sequence[str] = ()) -> Dict[str, int]:
    counts = {name: 0 for name in CLASSES}
    for path in files or []:
        counts[file_class(str(path), patterns)] += 1
    return counts


def classify(files: Sequence[str], patterns: Sequence[str] = ()) -> str:
    """One class for a file list, highest wins. Empty means docs."""
    counts = count_by_class(files, patterns)
    if counts["sensitive"]:
        return "sensitive"
    if counts["logic"]:
        return "logic"
    if counts["tests"]:
        return "tests"
    return "docs"


def _clean_line(line: str) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]", " ", line or "").strip()
    text = re.sub(r"\s+", " ", text)
    if text.startswith(("- ", "* ", "+ ")):
        text = text[2:].strip()
    return text[:300]


def behavior_lines(summary_text: str = "", what_text: str = "") -> List[str]:
    """One line per behavior change: non-empty lines of the lane's final
    summary, else non-empty lines of the ticket's What section."""
    for source in (summary_text, what_text):
        lines = [_clean_line(x) for x in (source or "").splitlines()]
        lines = [x for x in lines if x]
        if lines:
            return lines
    return ["No behavior change described."]


def render_intent(change_class: str, behaviors: Sequence[str],
                  counts: Dict[str, int]) -> str:
    """The ## Intent section for the top of a lane PR body."""
    if change_class not in CLASSES:
        raise ValueError("unknown class %r" % (change_class,))
    lines = ["## Intent", "Class: %s" % change_class]
    for item in behaviors or []:
        cleaned = _clean_line(str(item))
        if cleaned:
            lines.append("- %s" % cleaned)
    parts = ["%d %s" % (counts[name], name)
             for name in CLASSES if counts.get(name)]
    lines.append("Files: %s" % (", ".join(parts) if parts else "0 docs"))
    return "\n".join(lines)


def with_intent(body: str, intent: str) -> str:
    """The PR body with the Intent section at the top, replacing nothing."""
    body = (body or "").replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    intent = (intent or "").strip("\n")
    if not intent:
        return body
    if not body:
        return intent
    return intent + "\n\n" + body


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Classify changed files.")
    parser.add_argument("--files", default="",
                        help="file with one changed path per line")
    parser.add_argument("--sensitive", default="",
                        help="file holding sensitive globs (.loom/sensitive-paths)")
    parser.add_argument("--summary", default="",
                        help="file holding the lane's final summary (one change per line)")
    parser.add_argument("--what", default="",
                        help="file holding the ticket's What section as fallback")
    args = parser.parse_args(argv)

    files = [x.strip() for x in _read(args.files).splitlines() if x.strip()]
    patterns = parse_patterns(_read(args.sensitive)) if args.sensitive else []
    behaviors = behavior_lines(_read(args.summary), _read(args.what))
    name = classify(files, patterns)
    counts = count_by_class(files, patterns)
    sys.stdout.write(render_intent(name, behaviors, counts) + "\n")
    sys.stdout.write("class:%s\n" % name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
