"""Find out-of-brief additions and scratch files in a lane agent's commits.

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

- it sits at the top level beside tracked top-level files of its
  extension, and a tracked file the diff edits wires it in: a Python
  ``import <stem>`` / ``from <stem> import``, or its full name. A repo
  that keeps its modules at the top level had every new module refused,
  since no brief can name a file the agent has not chosen yet (loom#1705).

Everything else is stray: a new file at the top level, under a new
top-level directory, or under a hidden one, that the brief never names.
Only added files count; an edit to a tracked file is never stray. A file
an extra revision already holds (the default branch a fix round merged in)
was not added by the agent and is never stray.

``stray_files(tree, base, head, brief_text, also)`` returns the stray
paths. As a script it prints one stray path per line, made printable::

    python3 -m bus.scope_check <tree> <base> <head> <brief> [<rev>...]

With ``--scratch``, it prints only stray files safe to drop: unreferenced
top-level test/debug files, object files and build output (BUILD_OUTPUT_DIRS),
top-level shell here-document temp files (``sh-thd-*``) and ``*.patch`` files,
and .loom metadata other than the validation contract. New .github files are
never dropped.

It exits 2 on a usage error or when git cannot read a revision, so the
runner never mistakes a failed check for a clean one.
"""
from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import sys
from typing import Iterable, List, Sequence, Set

# Characters that may sit inside a path token in the brief's prose.
_PATH_CHAR = r"[\w./-]"


# Build and tool output an agent's own commands leave in the tree: a stray file
# under one of these is dropped, never refused (ten refusals on 2026-10-21/22
# were a Gradle daemon cache, a build/ output and the like). A folder the base
# tracks is in scope, so it is never stray and never dropped.
BUILD_OUTPUT_DIRS = ("build_test/", "build/", ".gradle/")

# Hidden directories whose new files GitHub runs: never in scope unnamed.
RUNNABLE_DIRS = (".github/workflows/", ".github/actions/")


class GitError(RuntimeError):
    """git could not answer, so the check has no result."""


def _git(tree: str, *args: str) -> str:
    proc = subprocess.run(["git", "-C", tree, "-c", "core.quotePath=false", *args],
                          capture_output=True, check=False)
    if proc.returncode != 0:
        raise GitError("git %s: %s" % (" ".join(args), proc.stderr.decode(errors="replace").strip()))
    return proc.stdout.decode(errors="surrogateescape")


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


def edited_files(tree: str, base: str, head: str) -> List[str]:
    """Tracked paths the diff edits between base and head."""
    return _names(_git(tree, "diff", "-z", "--no-renames", "--diff-filter=M",
                       "--name-only", base, head))


# Top-level names that tools load or run on their own: never in scope unnamed.
AUTO_RUN_NAMES = frozenset(("conftest.py", "setup.py", "sitecustomize.py",
                            "usercustomize.py", "noxfile.py", "Makefile"))


def _grep(tree: str, head: str, args: Sequence[str], paths: Sequence[str]) -> bool:
    proc = subprocess.run(["git", "-C", tree, "grep", "-I", "-q", *args, head, "--",
                           *(":(literal)" + p for p in paths)],
                          capture_output=True, check=False)
    if proc.returncode not in (0, 1):
        raise GitError("git grep failed: %s" % proc.stderr.decode(errors="replace").strip())
    return proc.returncode == 0


def wired_top_level(tree: str, head: str, path: str, tracked: Set[str],
                    edited: Sequence[str]) -> bool:
    """True when a new top-level file sits beside tracked top-level files of
    its extension and a tracked file the diff edits imports it or names it.
    Only edited tracked files count, so two new files cannot vouch for each
    other; hidden, auto-run and standard-library names never qualify."""
    name, dot, suffix = path.rpartition(".")
    if ("/" in path or not dot or not name or path.startswith(".")
            or not path.isprintable() or path in AUTO_RUN_NAMES):
        return False
    if suffix == "py" and name in getattr(sys, "stdlib_module_names", ()):
        return False
    if not edited or not any("/" not in t and t.endswith(dot + suffix) for t in tracked):
        return False
    if _grep(tree, head, ["-w", "-F", "-e", path], edited):
        return True
    return (suffix == "py" and name.isidentifier() and _grep(
        tree, head, ["-E", "-e", r"^[[:space:]]*(from|import)[[:space:]]+%s([[:space:].,]|$)" % name],
        edited))


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
    edited: List[str] = []
    for path in added_files(tree, base, head):
        if (path in elsewhere or named(brief_text, path) or in_known_dir(path, dirs)
                or in_known_hidden_subdir(path, tracked)):
            continue
        if "/" not in path:
            edited = edited or edited_files(tree, base, head)
            if wired_top_level(tree, head, path, tracked, edited):
                continue
        stray.append(path)
    return stray


def referenced_by_tracked_file(tree: str, head: str, path: str) -> bool:
    """Whether a reference or ambiguous name prevents dropping this file."""
    if not path.isprintable():
        return True  # Control characters make pattern input and runner output ambiguous.
    name = path.rsplit("/", 1)[-1]
    patterns = os.fsencode(path) + b"\n" + os.fsencode(name) + b"\n"
    proc = subprocess.run(["git", "-C", tree, "grep", "-I", "-l", "-z", "-F",
                           "-f", "-", head, "--", "."],
                          input=patterns, capture_output=True, check=False)
    if proc.returncode not in (0, 1):
        raise GitError("git grep failed: %s" % proc.stderr.decode(errors="replace").strip())
    return any(found != (head + ":" + path).encode() for found in proc.stdout.split(b"\0") if found)


def scratch_files(tree: str, base: str, head: str, brief_text: str,
                  also: Sequence[str] = ()) -> List[str]:
    """New out-of-path agent files safe to remove before the scope check."""
    scratch = []
    for path in stray_files(tree, base, head, brief_text, also):
        if path.startswith(".github/"):
            continue
        if path.startswith(".loom/") and path != ".loom/validation.conf":
            scratch.append(path)
        elif path.startswith(BUILD_OUTPUT_DIRS) or path.endswith(".o"):
            scratch.append(path)
        elif "/" not in path and (path.startswith("sh-thd-") or path.endswith(".patch")):
            scratch.append(path)
        elif "/" not in path and (fnmatch.fnmatchcase(path, "test_*") or
                                  fnmatch.fnmatchcase(path, "*_debug*")):
            if not referenced_by_tracked_file(tree, head, path):
                scratch.append(path)
    return scratch


def printable(path: str) -> str:
    """The path with every character outside printable ASCII as '?'."""
    return "".join(c if " " <= c <= "~" else "?" for c in path)


def main(argv: List[str]) -> int:
    args = argv[1:]
    scratch = bool(args and args[0] == "--scratch")
    if scratch:
        args = args[1:]
    if len(args) < 4:
        print("usage: python3 -m bus.scope_check [--scratch] <tree> <base> <head> <brief> [<rev>...]",
              file=sys.stderr)
        return 2
    tree, base, head, brief = args[:4]
    try:
        with open(brief, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        find = scratch_files if scratch else stray_files
        paths: Iterable[str] = find(tree, base, head, text, args[4:])
    except (OSError, GitError) as exc:
        print("scope_check: %s" % exc, file=sys.stderr)
        return 2
    for path in paths:
        print(printable(path))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
