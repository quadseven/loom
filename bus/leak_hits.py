"""Name each public repo leak scan hit by rule class and place, never by text.

The scanner (infra-public .github/scripts/check_private_leaks.py) prints one
line per hit, `  LABEL:N  [rule] <shown> - why`: LABEL is the scanned range,
`SHA patch`, `SHA message` or the scanned file, and N counts lines of the text
it scanned (the -U0 diff, not the file). A deny-list hit is never echoed, but a
shape hit is, and its text is private too. The runner used to park with only
the scanner's first line, the counts of rules it loaded, so the next lane could
not tell what to remove and repeated the refusal (loom#2804).

This turns the output into one line safe for a public ticket: each hit's rule
class and its path:line in the lane's own tree, up to five, never the matched
text. A file whose own name was flagged is not named. The full output stays in
the lane log.

    python3 -m bus.leak_hits --diff RANGE < scanner-output   (run in the tree)
    python3 -m bus.leak_hits --text NAME < scanner-output
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

LIMIT = 5
HIT_RE = re.compile(r"^\s+(?P<label>\S(?:.*?\S)?):(?P<line>\d+)\s+\[(?P<rule>[A-Za-z0-9_-]{1,40})\]")
SHA_RE = re.compile(r"[0-9a-f]{7,40}")
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,\d+)? @@")
# The scanner's own flags (git_diff, scan_commits), so its line numbers hold.
DIFF_ARGS = ["diff", "-U0", "--text", "--no-textconv", "--no-ext-diff"]
SHOW_ARGS = ["show", "-U0", "--format=", "--no-color", "--no-ext-diff", "--text",
             "--no-textconv", "--remerge-diff"]
FLAGGED = "a file whose name was flagged"

# A place in a scanned diff: (path, new-file line) for an added line, or
# (path, None) for a header line that carries the path itself.
Place = Tuple[str, Optional[int]]


def hits(output: str) -> List[Tuple[str, int, str]]:
    """Each (label, line, rule) the scanner printed, in order."""
    found = []
    for line in output.splitlines():
        m = HIT_RE.match(line)
        if m:
            found.append((m.group("label"), int(m.group("line")), m.group("rule")))
    return found


def _header_path(line: str) -> Optional[str]:
    """The path a diff header line names, or None for a line of another kind."""
    if line.startswith("diff --git "):
        return line.rsplit(" b/", 1)[-1]
    for head in ("rename to ", "copy to "):
        if line.startswith(head):
            return line[len(head):]
    return None


def places(diff: str) -> Dict[int, Place]:
    """Map each line number of a -U0 diff the scanner reads to its place."""
    mapped: Dict[int, Place] = {}
    path, new = "", 0
    for number, line in enumerate(diff.splitlines(), 1):
        named = _header_path(line)
        if named is not None:
            path = named
            mapped[number] = (path, None)
        elif line.startswith("+++ "):
            continue
        elif (hunk := HUNK_RE.match(line)):
            new = int(hunk.group("start"))
        elif line.startswith("+"):
            mapped[number] = (path, new)
            new += 1
        elif line.startswith(" "):
            new += 1
    return mapped


def _git(args: List[str]) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, errors="replace",
                          check=True).stdout


def _diff_place(label: str, rng: str, cache: Dict[str, Dict[int, Place]]) -> Optional[Dict[int, Place]]:
    """The place map of the text a diff-mode label names, or None for a message."""
    if label.endswith(" message"):
        return None
    if label not in cache:
        if label.endswith(" patch"):
            cache[label] = places(_git(SHOW_ARGS + [label[:-len(" patch")]]))
        else:
            cache[label] = places(_git(DIFF_ARGS + [rng]))
    return cache[label]


def _locate(found: List[Tuple[str, int, str]], rng: str) -> Tuple[List[Tuple[str, str, str]], set]:
    """Each hit as (rule, path, where), and the paths whose own header hit."""
    cache: Dict[str, Dict[int, Place]] = {}
    located, flagged = [], set()
    for label, number, rule in found:
        mapped = _diff_place(label, rng, cache)
        if mapped is None:
            sha = label.split()[0]
            commit = "commit %s" % sha if SHA_RE.fullmatch(sha) else "a commit"
            located.append((rule, "", "%s message line %d" % (commit, number)))
            continue
        path, line = mapped.get(number, ("", None))
        if not path:
            located.append((rule, "", "diff line %d" % number))
        elif line is None:
            flagged.add(path)
            located.append((rule, path, ""))
        else:
            located.append((rule, path, "%s:%d" % (path, line)))
    return located, flagged


def describe_diff(found: List[Tuple[str, int, str]], rng: str) -> List[str]:
    """Each hit as `[rule] path:line`; a flagged file name is never repeated."""
    located, flagged = _locate(found, rng)
    return ["[%s] %s" % (rule, FLAGGED if path in flagged else where)
            for rule, path, where in located]


def describe_text(found: List[Tuple[str, int, str]], name: str) -> List[str]:
    """Each hit in a scanned text file as `[rule] NAME line N`."""
    return ["[%s] %s line %d" % (rule, name, number) for _label, number, rule in found]


def summary(shown: List[str]) -> str:
    """One line: each distinct hit, up to LIMIT, and how many more."""
    distinct = list(dict.fromkeys(shown))
    if not distinct:
        return "the scanner named no hit line; its output is in the lane log"
    line = "%d hit%s: %s" % (len(distinct), "" if len(distinct) == 1 else "s",
                             ", ".join(distinct[:LIMIT]))
    if len(distinct) > LIMIT:
        line += ", and %d more in the lane log" % (len(distinct) - LIMIT)
    return line


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--diff", help="the range the scanner read, run in its tree")
    src.add_argument("--text", help="what the scanned text file was, such as 'PR body'")
    args = ap.parse_args(argv)
    found = hits(sys.stdin.read())
    shown = describe_diff(found, args.diff) if args.diff else describe_text(found, args.text)
    print(summary(shown))
    return 0


if __name__ == "__main__":
    sys.exit(main())
