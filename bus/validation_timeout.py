"""Recognize validation failures made only of timeouts in untouched tests.

An untouched test file does not establish why it timed out: source changes
can cause the hang too. The first matching failure is retried by design;
the station's existing two-strike policy bounds consecutive retries. Check
output is diagnostic input, not trusted attribution, and test-controlled
text can influence that first classification.
"""
from __future__ import annotations

import re
import subprocess
import sys
from itertools import islice
from pathlib import Path


# These character/section limits bound diagnostic parsing, not test execution.
# Exceeding either limit keeps the ordinary work-failure classification.
MAX_OUTPUT_CHARACTERS = 8 * 1024 * 1024
MAX_ERROR_SECTIONS = 512

ERROR = re.compile(r"^ERROR: (.+)$", re.MULTILINE)
TEST_MODULE = re.compile(r"\(([A-Za-z_][A-Za-z0-9_.]*)\)")
EXCEPTION = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*):", re.MULTILINE)


def only_untouched_test_timeouts(output: str, changed_paths: set[str],
                                 test_paths: set[str]) -> bool:
    """Whether unittest reported only TimeoutExpired errors in unchanged tests."""
    # AI-REVIEW 2026-11-12 [deepseek-v4.1-flash/opencode-go + gpt-6.1-sol/native verification] E03 (PR #3019):
    # Reject large diagnostics before allocating matches or sections.
    # The section slices themselves are linear.
    if len(output) > MAX_OUTPUT_CHARACTERS:
        return False
    failures = list(islice(ERROR.finditer(output), MAX_ERROR_SECTIONS + 1))
    if not failures or len(failures) > MAX_ERROR_SECTIONS:
        return False
    sections = [output[match.start():failures[i + 1].start() if i + 1 < len(failures)
                       else len(output)] for i, match in enumerate(failures)]
    if "FAIL:" in output:
        return False
    # AI-REVIEW 2026-11-11 [gpt-6.1-sol] R4 (PR #3019): a timeout cause
    # must not hide the terminal error of a chained traceback.
    for section in sections:
        exceptions = EXCEPTION.findall(section)
        if not exceptions or exceptions[-1] != "subprocess.TimeoutExpired":
            return False
    modules = []
    for failure in failures:
        match = TEST_MODULE.search(failure.group(1))
        if not match:
            return False
        modules.append(match.group(1))
    for module in modules:
        candidates = {path for path in test_paths
                      if _module_matches_test_path(module, path)}
        if not candidates or candidates.intersection(changed_paths):
            return False
    return True


def _module_matches_test_path(module: str, path: str) -> bool:
    """Match unittest IDs with or without their leading tests package."""
    if not path.endswith(".py"):
        return False
    name = path[:-3].replace("/", ".")
    aliases = {name}
    if name.startswith("tests."):
        aliases.add(name[len("tests."):])
    return any(module == alias or module.startswith(alias + ".") for alias in aliases)


def main() -> int:
    if len(sys.argv) != 4:
        return 2
    # AI-REVIEW 2026-11-12 [deepseek-v4.1-flash/opencode-go + gpt-6.1-sol/native verification] E03 (PR #3019):
    # Bound the initial read before the predicate can inspect its length.
    output = sys.stdin.read(MAX_OUTPUT_CHARACTERS + 1)
    if len(output) > MAX_OUTPUT_CHARACTERS:
        return 1
    tree, base, revision = sys.argv[1:]
    try:
        repo = Path(tree)
        # AI-REVIEW 2026-11-12 [gpt-6.1-sol/native verification] R5 (PR #3019):
        # The primary model was undisclosed; verified both calls retain the
        # runner's Git safety overrides for an agent-controlled checkout.
        git_safety = ["-c", "core.hooksPath=/dev/null",
                      "-c", "core.fsmonitor=false"]
        changed = subprocess.run(["git", *git_safety, "diff", "--name-only", base, revision],
                                 cwd=repo, check=True, capture_output=True,
                                 text=True).stdout.splitlines()
        tests = subprocess.run(["git", *git_safety, "ls-tree", "-r", "--name-only", revision,
                                "--", "tests"], cwd=repo, check=True,
                               capture_output=True, text=True).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):
        print("validation timeout classifier could not read git state", file=sys.stderr)
        return 1
    test_paths = {path for path in tests
                  if path.rsplit("/", 1)[-1].startswith("test") and path.endswith(".py")}
    return 0 if only_untouched_test_timeouts(output, set(changed), test_paths) else 1


if __name__ == "__main__":
    raise SystemExit(main())
