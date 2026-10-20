"""Find untracked additions that match the lane scratch-file rules."""
from __future__ import annotations

import subprocess
from pathlib import PurePosixPath
from typing import List

from bus.scope_check import named


def _git(tree: str, *args: str):
    return subprocess.run(["git", "-C", tree, *args], capture_output=True,
                          text=True, check=False)


def _referenced(tree: str, rev: str, path: str) -> bool:
    proc = _git(tree, "grep", "-I", "-F", "-q", "-e", path, "-e",
                PurePosixPath(path).name, rev, "--")
    if proc.returncode not in (0, 1):
        raise RuntimeError("git grep failed: " + proc.stderr.strip())
    return proc.returncode == 0


def _scratch_rule(path: str) -> bool:
    if path.startswith(".github/"):
        return False
    parts = PurePosixPath(path).parts
    name = parts[-1]
    return ((len(parts) == 1 and (name.startswith("test_") or "_debug" in name))
            or name.endswith(".o") or path.startswith("build_test/")
            or (path.startswith(".loom/") and path != ".loom/validation.conf"))


def scratch_files(tree: str, base: str, head: str, brief_text: str) -> List[str]:
    """Return matching added files not referenced by any base tracked file."""
    added = _git(tree, "diff", "-z", "--no-renames", "--diff-filter=A",
                 "--name-only", base, head)
    if added.returncode != 0:
        raise RuntimeError("git diff failed: " + added.stderr.strip())
    paths = [path for path in added.stdout.split("\0") if path]
    return [path for path in paths if _scratch_rule(path) and not named(brief_text, path)
            and not _referenced(tree, base, path)]


def main(argv: List[str]) -> int:
    if len(argv) != 5:
        print("usage: python3 -m bus.scratch_drop <tree> <base> <head> <brief>")
        return 2
    try:
        with open(argv[4], encoding="utf-8", errors="replace") as brief:
            paths = scratch_files(argv[1], argv[2], argv[3], brief.read())
    except RuntimeError as exc:
        print("scratch_drop: " + str(exc))
        return 2
    for path in paths:
        print("".join(c if " " <= c <= "~" else "?" for c in path))
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv))
