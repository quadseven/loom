"""A fix round deepens its shallow tree twice before it gives up (loom#3108).

Two fetches on one shallow tree can race ("shallow file has changed since we
read it"). The runner used to swallow that failure, then fail the merge with
"unrelated histories" and report it as the lane's work before the agent ran.
update_fix_branch is run here against a stub git, so no network is used."""

import re
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "lanes" / "run_lane.sh"

STUB = """#!/usr/bin/env bash
echo "$*" >> "$STUB_CALLS"
case " $* " in
  *" fetch -q --unshallow "*)
    n=$(cat "$STUB_COUNT" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$STUB_COUNT"
    if [ "$n" -le "$STUB_UNSHALLOW_FAILS" ]; then
      echo 'fatal: shallow file has changed since we read it' >&2; exit 128
    fi
    touch "$STUB_DEEP"; exit 0 ;;
  *" merge-base --is-ancestor "*) exit 1 ;;
  *" merge-base "*) [ -e "$STUB_DEEP" ]; exit $? ;;
  *" rev-parse "*) echo 0123456789abcdef; exit 0 ;;
esac
exit 0
"""


def shell_function(script, name):
    match = re.search(r"^%s\(\) \{\n.*?^\}\n" % name, script, re.S | re.M)
    if not match:
        raise AssertionError("missing shell function: %s" % name)
    return match.group(0)


class UnshallowRetryTest(unittest.TestCase):
    def run_update(self, fails):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            (bin_dir / "git").write_text(STUB, encoding="utf-8")
            (bin_dir / "git").chmod(0o755)
            (bin_dir / "sleep").write_text("#!/bin/sh\nexit 0\n",
                                           encoding="utf-8")
            (bin_dir / "sleep").chmod(0o755)
            shell = """set -e
export PATH={bin}:$PATH
export STUB_CALLS={calls} STUB_COUNT={count} STUB_DEEP={deep}
export STUB_UNSHALLOW_FAILS={fails}
TREE=/nowhere BASE_BRANCH=main NET_TIMEOUT=10 LOG={log} TICKET=T1 BRANCH=fix
commit_msg() {{ printf '%s' "$1"; }}
{function}
set +e
update_fix_branch
echo "RC=$?"
echo "REASON=$UPDATE_REASON"
""".format(bin=shlex.quote(str(bin_dir)), calls=shlex.quote(str(root / "calls")),
           count=shlex.quote(str(root / "count")), deep=shlex.quote(str(root / "deep")),
           fails=fails, log=shlex.quote(str(root / "log")),
           function=shell_function(RUNNER.read_text(encoding="utf-8"), "update_fix_branch"))
            result = subprocess.run(["bash", "-c", shell], capture_output=True, text=True)
            calls = (root / "calls").read_text(encoding="utf-8")
            return result, calls

    def test_a_raced_unshallow_fetch_is_tried_again_and_the_merge_runs(self):
        result, calls = self.run_update(1)
        self.assertIn("RC=0", result.stdout.splitlines(), result.stderr)
        self.assertEqual(calls.count("fetch -q --unshallow"), 2)
        self.assertIn(" merge --no-edit -m ", calls)
        # Both refusals: no merge is attempted and the reason names the tree,
        # asserted here so this module holds one test that fails on main.
        result, calls = self.run_update(2)
        self.assertIn("RC=1", result.stdout.splitlines())
        self.assertIn("REASON=could not deepen the shallow tree to merge main",
                      result.stdout.splitlines())
        self.assertNotIn(" merge --no-edit", calls)


if __name__ == "__main__":
    unittest.main()
