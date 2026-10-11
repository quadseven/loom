#!/usr/bin/env bash
# Loom sparks lane  -  the workshop. Compute, not a model lane.
#
# test.sh runs the full gates for a ticket's worktree on the sparks box:
#   python -B test suites, C++ builds, whatever the repo's gates are.
# Called by lane runners or the dispatcher before a PR is handed to the
# shipper. Rule: sparks power Loom. Sparks never touch a live realm.
set -euo pipefail

TREE="$1"   # path to the worktree to test

cd "$TREE"

if [ -f "pyproject.toml" ] || [ -f "requirements.txt" ]; then
  python3 -B -m pytest -x -q
fi

if [ -f "CMakeLists.txt" ]; then
  cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
  cmake --build build -j"$(nproc)"
  ctest --test-dir build --output-on-failure
fi

echo "GATES GREEN: $TREE"
