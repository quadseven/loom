"""Find the files a lane agent added only to probe the sandbox.

A lane that tests whether it can write files must not carry the evidence
in its pull request. A new file is a sandbox probe when all of these hold:

- the diff adds it (it did not exist on the base revision),
- it is empty or a single line,
- its name starts with ``.test``, or it sits under a top-level ``bin/``
  directory that is itself new in the diff,
- nothing else in the diff references it, by path or by file name.

Files under .github/ remain subject to the brief's path check.

``find_probes(tree, base, head)`` returns the probe paths. As a script it
prints one probe path per line::

    python3 -m bus.probe_drop <tree> <base> <head>
"""
from __future__ import annotations

import re
import subprocess
import sys
from typing import List, Tuple


def _git(tree: str, *args: str) -> str:
    proc = subprocess.run(["git", "-C", tree, *args], capture_output=True,
                          text=True, check=False)
    return proc.stdout


def added_files(tree: str, base: str, head: str) -> List[str]:
    """Paths the diff adds between base and head."""
    names = _git(tree, "diff", "--diff-filter=A", "--name-only", base, head)
    return [name for name in names.splitlines() if name]


def line_count(tree: str, rev: str, path: str) -> int:
    """Number of lines in path at rev, counted on bytes so binary is safe."""
    proc = subprocess.run(["git", "-C", tree, "show", "%s:%s" % (rev, path)],
                          capture_output=True, check=False)
    blob = proc.stdout
    if not blob:
        return 0
    return blob.count(b"\n") + (0 if blob.endswith(b"\n") else 1)


def short_file(tree: str, rev: str, path: str) -> bool:
    """True when the file at rev is empty or a single line."""
    return line_count(tree, rev, path) <= 1


def bin_is_new(tree: str, base: str) -> bool:
    """True when no top-level bin/ directory exists on the base revision."""
    kind = subprocess.run(["git", "-C", tree, "cat-file", "-t", base + ":bin"],
                          capture_output=True, text=True, check=False)
    return kind.stdout.strip() != "tree"


def probe_name(path: str, new_bin: bool) -> bool:
    """True when the path looks like a probe: .test* or under a new bin/."""
    if path.startswith(".github/"):
        return False
    name = path.rsplit("/", 1)[-1]
    if name.startswith(".test"):
        return True
    return new_bin and path.startswith("bin/")


def _sections(tree: str, base: str, head: str) -> List[Tuple[str, str]]:
    """(path, section text) for every file the diff touches."""
    sections: List[Tuple[str, str]] = []
    current: List[str] = []
    path = ""
    for line in _git(tree, "diff", base, head).splitlines(keepends=True):
        if line.startswith("diff --git "):
            if current:
                sections.append((path, "".join(current)))
            target = line.rstrip("\n").split(" ")[-1]
            path = target[2:] if target.startswith("b/") else target
            current = [line]
        elif current:
            current.append(line)
    if current:
        sections.append((path, "".join(current)))
    return sections


def _mentions(text: str, path: str, name: str) -> bool:
    if path in text:
        return True
    # The bare file name only counts when it appears as a path component,
    # so prose like "# test" never reads as a reference to bin/test.
    hit = re.search(r"/" + re.escape(name) + r"(?![\w.-])", text)
    return hit is not None


def referenced_elsewhere(sections: List[Tuple[str, str]], path: str) -> bool:
    """True when another file's diff section mentions the path or its name."""
    name = path.rsplit("/", 1)[-1]
    return any(other != path and _mentions(body, path, name)
               for other, body in sections)


def find_probes(tree: str, base: str, head: str) -> List[str]:
    """Paths the agent's commits added that only probe the sandbox."""
    new_bin = bin_is_new(tree, base)
    sections = _sections(tree, base, head)
    probes = []
    for path in added_files(tree, base, head):
        if not short_file(tree, head, path):
            continue
        if not probe_name(path, new_bin):
            continue
        if referenced_elsewhere(sections, path):
            continue
        probes.append(path)
    return probes


def main(argv: List[str]) -> int:
    if len(argv) != 4:
        print("usage: python3 -m bus.probe_drop <tree> <base> <head>",
              file=sys.stderr)
        return 2
    for path in find_probes(argv[1], argv[2], argv[3]):
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
