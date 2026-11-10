"""The lane's temp dirs: every environment run_lane.sh builds (the run's
own, the agent's and the validation check's) sets TMPDIR to a directory
that exists, so git on the lane hosts prints no confstr warning into the
run log or the agent's output.

The setup functions are extracted from the runner and run for real, the
way tests/test_run_lane.py runs fix_items: on the base branch neither
function exists, so both tests fail there.
"""

import re
import subprocess
import tempfile
import unittest
from pathlib import Path

RUNNER = Path(__file__).resolve().parent.parent / "lanes" / "run_lane.sh"


def function_text(name):
    match = re.search(r"^%s\(\) \{\n.*?^\}\n" % re.escape(name),
                      RUNNER.read_text(), re.S | re.M)
    return match.group(0) if match else None


def run_setup(name, var, home):
    function = function_text(name)
    if function is None:
        raise AssertionError("run_lane.sh has no %s() setup function" % name)
    result = subprocess.run(
        ["bash", "-c", '%s\nexport %s="$1"\n%s' % (function, var, name),
         "bash", str(home)],
        capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError("%s failed: %s" % (name, result.stderr))
    return result.stdout.strip()


class LaneTmpdirTest(unittest.TestCase):
    def test_the_check_environment_tmpdir_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            check_home = Path(tmp) / "check"
            tmpdir = run_setup("check_tmpdir", "CHECK_HOME", check_home)
            self.assertEqual(tmpdir, str(check_home / "tmp"))
            self.assertTrue(Path(tmpdir).is_dir())

    def test_the_run_tmpdir_the_agent_inherits_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent_home = Path(tmp) / "home"
            tmpdir = run_setup("run_tmpdir", "AGENT_HOME", agent_home)
            self.assertEqual(tmpdir, str(agent_home / "tmp"))
            self.assertTrue(Path(tmpdir).is_dir())


if __name__ == "__main__":
    unittest.main()
