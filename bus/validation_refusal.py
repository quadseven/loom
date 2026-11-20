"""Recognize a failed final check made only of sandbox refusals.

A check the base run never executed can fail inside the lane sandbox, which
refuses what building its environment needs (a download, a write outside the
tree). Such a failure says nothing about the change, so it is parked as an
environment fault, not as the agent's work. Check output is diagnostic input,
not trusted attribution: any section that ends in another exception, or any
assertion failure, keeps the ordinary work classification.
"""
from __future__ import annotations

import re
import sys
from itertools import islice

MAX_OUTPUT_CHARACTERS = 8 * 1024 * 1024
MAX_SECTIONS = 512

SECTION = re.compile(r"^(?:ERROR|FAIL): ", re.MULTILINE)
EXCEPTION = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception)): (.*)$", re.MULTILINE)
REFUSED = "Operation not permitted"


def only_sandbox_refusals(output: str) -> bool:
    """Whether every failure section ends in a PermissionError for a refused operation."""
    if len(output) > MAX_OUTPUT_CHARACTERS:
        return False
    starts = list(islice(SECTION.finditer(output), MAX_SECTIONS + 1))
    if not 1 <= len(starts) <= MAX_SECTIONS:
        return False
    for i, match in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(output)
        found = EXCEPTION.findall(output[match.start():end])
        if not found:
            return False
        name, text = found[-1]
        if name != "PermissionError" or REFUSED not in text:
            return False
    return True


def main() -> int:
    output = sys.stdin.read(MAX_OUTPUT_CHARACTERS + 1)
    return 0 if only_sandbox_refusals(output) else 1


if __name__ == "__main__":
    raise SystemExit(main())
