"""Recognize validation failures made only of timeouts in untouched tests."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


ERROR = re.compile(r"^ERROR: (.+)$", re.MULTILINE)
TEST_MODULE = re.compile(r"\(([A-Za-z_][A-Za-z0-9_.]*)\)")


def only_untouched_test_timeouts(output: str, changed_paths: set[str]) -> bool:
    """Whether unittest reported only TimeoutExpired errors in unchanged tests."""
    failures = list(ERROR.finditer(output))
    if not failures:
        return False
    sections = [output[match.start():failures[i + 1].start() if i + 1 < len(failures)
                       else len(output)] for i, match in enumerate(failures)]
    if "FAIL:" in output or any("subprocess.TimeoutExpired" not in section
                                for section in sections):
        return False
    modules = []
    for failure in failures:
        match = TEST_MODULE.search(failure.group(1))
        if not match:
            return False
        modules.append(match.group(1))
    for module in modules:
        parts = module.split(".")
        if parts[0] == "tests":
            candidates = {"/".join(parts[:end]) + ".py"
                          for end in range(2, len(parts) - 1)}
        else:
            candidates = {parts[0] + ".py", "tests/" + parts[0] + ".py"}
        if candidates.intersection(changed_paths):
            return False
    return True


def main() -> int:
    if len(sys.argv) != 4:
        return 2
    output = sys.stdin.read()
    tree, base, revision = sys.argv[1:]
    try:
        changed = subprocess.run(["git", "diff", "--name-only", base, revision],
                                 cwd=Path(tree), check=True, capture_output=True,
                                 text=True).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):
        return 1
    return 0 if only_untouched_test_timeouts(output, set(changed)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
