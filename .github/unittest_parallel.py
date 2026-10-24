#!/usr/bin/env python3
"""Run discovered unittest modules concurrently and combine their output."""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys


def discovered_modules(test_dir: Path) -> list[str]:
    names = set()
    for path in test_dir.rglob("test*.py"):
        relative = path.relative_to(test_dir)
        parents = relative.parents[:-1]
        if any(not (test_dir / parent / "__init__.py").is_file() for parent in parents):
            continue
        module_parts = relative.with_suffix("").parts
        names.add(".".join(module_parts))
    return sorted(names)


# Imports every module once in a child process and prints its test ids, so a
# module's import-time side effects never touch the runner. A module whose ids
# cannot be listed maps to None and runs whole, so its import error still fails
# the step as that module's own failure.
LIST_IDS = """
import json, sys, unittest
def ids(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from ids(test)
        else:
            yield test.id()
found = {}
for module in sys.argv[1:]:
    try:
        names = list(ids(unittest.defaultTestLoader.loadTestsFromName(module)))
    except Exception:
        names = None
    if names is not None and any(name.startswith("unittest.") for name in names):
        names = None
    found[module] = names
print(json.dumps(found))
"""


def child_env(test_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    python_path = str(test_dir)
    if env.get("PYTHONPATH"):
        python_path += os.pathsep + env["PYTHONPATH"]
    env["PYTHONPATH"] = python_path
    return env


def work_items(modules: list[str], test_dir: Path, shard_size: int) -> list[tuple[str, list[str]]]:
    """Split each module with more than shard_size tests into round-robin shards.

    One module bounded the step's wall time: test_run_lane holds over 200
    tests, each building its own temporary repo in setUp, so its tests can run
    in separate processes. Every other module runs whole by name, as before.
    Each item is (header, unittest arguments)."""
    listed = subprocess.run([sys.executable, "-B", "-c", LIST_IDS, *modules],
                            cwd=test_dir.parent, env=child_env(test_dir),
                            text=True, capture_output=True)
    try:
        found = json.loads(listed.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        found = {}
    items = []
    for module in modules:
        ids = found.get(module)
        if not ids or len(ids) <= shard_size:
            items.append((module, [module]))
            continue
        count = math.ceil(len(ids) / shard_size)
        for index in range(count):
            items.append(("%s [%d/%d]" % (module, index + 1, count), ids[index::count]))
    return items


def run_module(args: list[str], test_dir: Path) -> subprocess.CompletedProcess[str]:
    # --durations prints each module's slowest tests, so a stall like the
    # reverse-DNS lookup in issue 1188 shows up by name in the CI log.
    return subprocess.run([sys.executable, "-B", "-m", "unittest", *args, "-v", "--durations", "5"],
                          cwd=test_dir.parent, env=child_env(test_dir),
                          text=True, capture_output=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tests-dir", type=Path, default=Path("tests"))
    parser.add_argument("--shard-size", type=int, default=60)
    args = parser.parse_args()
    test_dir = args.tests_dir.resolve()
    modules = discovered_modules(test_dir)
    if not modules:
        print("Ran 0 tests")
        return 0

    items = work_items(modules, test_dir, max(1, args.shard_size))
    failed = False
    total = 0
    max_workers = min(len(items), os.cpu_count() or 1)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        # Discovery order, not most-tests first: test count does not predict
        # time. Most-tests first queued test_dashboard (43 tests, 610s on the
        # macOS runner) behind the 53-test shards and made the step 140s
        # slower (run 39471928241).
        results = list(pool.map(lambda item: (item[0], run_module(item[1], test_dir)), items))

    for header, result in results:
        output = result.stdout + result.stderr
        print("===== %s =====" % header)
        print(output, end="" if output.endswith("\n") else "\n")
        summaries = re.findall(r"^Ran (\d+) tests? in ", output, re.MULTILINE)
        if summaries:
            total += int(summaries[-1])
        if result.returncode:
            failed = True
        for line in output.splitlines():
            if "... skipped " in line and "LOOM_TEST_OPENCODE2" not in line:
                print("::error::a test skipped; the sandbox tests must run in CI")
                failed = True
    print("Parallel unittest total: Ran %d tests across %d modules in %d processes"
          % (total, len(modules), len(items)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
