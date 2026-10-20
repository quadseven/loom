#!/usr/bin/env python3
"""Run discovered unittest modules concurrently and combine their output."""
from __future__ import annotations

import argparse
import concurrent.futures
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


def run_module(module: str, test_dir: Path) -> tuple[str, subprocess.CompletedProcess[str]]:
    env = os.environ.copy()
    python_path = str(test_dir)
    if env.get("PYTHONPATH"):
        python_path += os.pathsep + env["PYTHONPATH"]
    env["PYTHONPATH"] = python_path
    # --durations prints each module's slowest tests, so a stall like the
    # reverse-DNS lookup in issue 1188 shows up by name in the CI log.
    result = subprocess.run([sys.executable, "-B", "-m", "unittest", module, "-v", "--durations", "5"],
                            cwd=test_dir.parent, env=env, text=True, capture_output=True)
    return module, result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tests-dir", type=Path, default=Path("tests"))
    args = parser.parse_args()
    test_dir = args.tests_dir.resolve()
    modules = discovered_modules(test_dir)
    if not modules:
        print("Ran 0 tests")
        return 0

    failed = False
    total = 0
    max_workers = min(len(modules), os.cpu_count() or 1)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        results = list(pool.map(lambda name: run_module(name, test_dir), modules))

    for module, result in results:
        output = result.stdout + result.stderr
        print("===== %s =====" % module)
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
    print("Parallel unittest total: Ran %d tests across %d modules" % (total, len(modules)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
