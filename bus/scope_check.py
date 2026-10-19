"""Find the files a lane agent's commits add outside its ticket's brief.

A lane run must not push files that have nothing to do with its ticket (a
hook config, a scratch file, a test script left in the workspace). A file
the diff adds is in scope when either holds:

- the brief names it, or names a directory above it (as ``dir/`` or as
  part of a longer path such as ``dir/file.py``), or
- its first path component is a directory the base revision already
  tracks, and that directory is not hidden (its name does not start
  with ``.``), or
- it sits in a subdirectory of a hidden top-level directory that the base
  revision already tracks (``.github/scripts/tests/``), outside
  RUNNABLE_DIRS. A pull request runs its own workflow files, so a new
  workflow or action must be named in the brief.

Everything else is stray: a new file at the top level, under a new
top-level directory, or under a hidden one, that the brief never names.
Only added files count; an edit to a tracked file is never stray. A file
an extra revision already holds (the default branch a fix round merged in)
was not added by the agent and is never stray.

``stray_files(tree, base, head, brief_text, also)`` returns the stray
paths. As a script it prints one stray path per line, made printable::

    python3 -m bus.scope_check <tree> <base> <head> <brief> [<rev>...]

It exits 2 on a usage error or when git cannot read a revision, so the
runner never mistakes a failed check for a clean one.
"""
from __future__ import annotations

import re
import subprocess
import sys
from typing import Iterable, List, Sequence, Set

# Characters that may sit inside a path token in the brief's prose.
_PATH_CHAR = r"[\w./-]"


# Hidden directories whose new files GitHub runs: never in scope unnamed.
RUNNABLE_DIRS = (".github/workflows/", ".github/actions/")


class GitError(RuntimeError):
    """git could not answer, so the check has no result."""


def _git(tree: str, *args: str) -> str:
    proc = subprocess.run(["git", "-C", tree, "-c", "core.quotePath=false", *args],
                          capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise GitError("git %s: %s" % (" ".join(args), proc.stderr.strip()))
    return proc.stdout


def _names(out: str) -> List[str]:
    return [name for name in out.split("\0") if name]


def added_files(tree: str, base: str, head: str) -> List[str]:
    """Paths the diff adds between base and head; a rename counts as an add."""
    return _names(_git(tree, "diff", "-z", "--no-renames", "--diff-filter=A",
                       "--name-only", base, head))


def tracked_files(tree: str, rev: str) -> Set[str]:
    """Every file path rev tracks."""
    return set(_names(_git(tree, "ls-tree", "-r", "-z", "--name-only", rev)))


def top_dirs(tree: str, rev: str) -> Set[str]:
    """The top-level directories rev tracks."""
    return set(_names(_git(tree, "ls-tree", "-d", "-z", "--name-only", rev)))


def named(brief_text: str, path: str) -> bool:
    """True when the brief names the path or a directory above it."""
    exact = r"(?<!%s)%s(?![\w/-]|\.\w)" % (_PATH_CHAR, re.escape(path))
    if re.search(exact, brief_text):
        return True
    parts = path.split("/")[:-1]
    for depth in range(1, len(parts) + 1):
        prefix = "/".join(parts[:depth]) + "/"
        if re.search(r"(?<!%s)%s" % (_PATH_CHAR, re.escape(prefix)), brief_text):
            return True
    return False


def in_known_dir(path: str, dirs: Set[str]) -> bool:
    """True when the path sits under a tracked, non-hidden top-level directory."""
    if "/" not in path:
        return False
    first = path.split("/", 1)[0]
    return first in dirs and not first.startswith(".")


def in_known_hidden_subdir(path: str, tracked: Set[str]) -> bool:
    """True when the path's directory is a subdirectory of a hidden
    top-level directory that `tracked` already has files in, and is not
    one GitHub runs. A test beside a tracked .github script was refused
    (2026-10-19) while scratch files such as .loom/test.txt stay stray."""
    parent = path.rsplit("/", 1)[0] + "/" if "/" in path else ""
    if not parent.startswith(".") or parent.count("/") < 2:
        return False
    if any(parent.startswith(d) for d in RUNNABLE_DIRS):
        return False
    return any(name.startswith(parent) for name in tracked)


def stray_files(tree: str, base: str, head: str, brief_text: str,
                also: Sequence[str] = ()) -> List[str]:
    """Paths the agent's commits added that the brief does not allow."""
    dirs = top_dirs(tree, base)
    tracked = tracked_files(tree, base)
    elsewhere: Set[str] = set()
    for rev in also:
        dirs |= top_dirs(tree, rev)
        elsewhere |= tracked_files(tree, rev)
    stray = []
    for path in added_files(tree, base, head):
        if (path in elsewhere or named(brief_text, path) or in_known_dir(path, dirs)
                or in_known_hidden_subdir(path, tracked)):
            continue
        stray.append(path)
    return stray


def printable(path: str) -> str:
    """The path with every character outside printable ASCII as '?'."""
    return "".join(c if " " <= c <= "~" else "?" for c in path)


def main(argv: List[str]) -> int:
    if len(argv) < 5:
        print("usage: python3 -m bus.scope_check <tree> <base> <head> <brief> [<rev>...]",
              file=sys.stderr)
        return 2
    tree, base, head, brief = argv[1:5]
    try:
        with open(brief, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        paths: Iterable[str] = stray_files(tree, base, head, text, argv[5:])
    except (OSError, GitError) as exc:
        print("scope_check: %s" % exc, file=sys.stderr)
        return 2
    for path in paths:
        print(printable(path))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
