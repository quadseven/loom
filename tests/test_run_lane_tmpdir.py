"""The lane's temp dirs: the environments run_lane.sh builds set TMPDIR
to a directory that exists, so git on the lane hosts prints no confstr
warning into the run log or the agent's output.

The check side runs the runner's own contract_run_sandboxed, extracted
from the script the way the contract_run tests in test_run_lane.py run
it, with a check home whose tmp dir does not exist yet: on the base
branch the check is handed TMPDIR naming that missing directory, so
the test fails there. The run side extracts and runs run_tmpdir, the
setup the runner exports for itself and the agents that inherit its
environment; on the base branch that function does not exist. The
runner's temp dir is its own, outside the agent home, so a third test
runs drop_run_scratch over it and asserts it still exists.
"""

import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.test_run_lane import SANDBOX_HELPERS

RUNNER = Path(__file__).resolve().parent.parent / "lanes" / "run_lane.sh"


def function_text(name):
    text = RUNNER.read_text()
    match = re.search(r"^%s\(\) \{[^\n]*\}\n" % re.escape(name),
                      text, re.M)
    if match:
        return match.group(0)
    match = re.search(r"^%s\(\) \{\n.*?^\}\n" % re.escape(name),
                      text, re.S | re.M)
    return match.group(0) if match else None


class LaneTmpdirTest(unittest.TestCase):
    def test_the_check_environment_tmpdir_exists(self):
        text = RUNNER.read_text()
        funcs = ""
        for name in ("contract_command_ok", "contract_run_sandboxed"):
            function = function_text(name)
            self.assertIsNotNone(function, "run_lane.sh has no %s()" % name)
            funcs += function
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fakes = root / "fakebin"
            fakes.mkdir()
            for tool, body in (("sandbox-exec", "#!/bin/bash\nshift 2\nexec \"$@\"\n"),
                               ("timeout", "#!/bin/bash\nshift\nexec \"$@\"\n")):
                fake = fakes / tool
                fake.write_text(body)
                fake.chmod(0o755)
            check_home = root / "check"
            log = root / "log"
            script = (funcs + SANDBOX_HELPERS
                      + 'sandbox_profile() { echo "(version 1)"; }\n'
                      + 'contract_run_sandboxed "$1"\n')
            env = {"PATH": f"{fakes}:/usr/bin:/bin", "LOG": str(log),
                   "TREE": str(root), "CHECK_HOME": str(check_home),
                   "CHECK_CLI": "bash", "CHECK_TIMEOUT": "30"}
            result = subprocess.run(
                ["bash", "-c", script, "test",
                 "printf 'tmpdir=%s exists=%s\\n' \"$TMPDIR\" "
                 "\"$([ -d \"$TMPDIR\" ] && echo yes || echo no)\""],
                env=env, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"tmpdir={check_home / 'tmp'} exists=yes",
                          log.read_text())

    def test_the_run_tmpdir_the_agent_inherits_exists(self):
        function = function_text("run_tmpdir")
        self.assertIsNotNone(function, "run_lane.sh has no run_tmpdir()")
        with tempfile.TemporaryDirectory() as tmp:
            loom_home = Path(tmp) / "loom"
            result = subprocess.run(
                ["bash", "-c",
                 '%s\nexport LOOM_HOME="$1" SLOT="$2"\nrun_tmpdir' % function,
                 "bash", str(loom_home), "codex-1"],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            tmpdir = result.stdout.strip()
            self.assertEqual(tmpdir, str(loom_home / "run" / "codex-1-tmp"))
            self.assertTrue(Path(tmpdir).is_dir())

    def test_the_run_tmpdir_survives_drop_run_scratch(self):
        funcs = ""
        for name in ("run_tmpdir", "drop_check_home", "drop_run_scratch"):
            function = function_text(name)
            self.assertIsNotNone(function, "run_lane.sh has no %s()" % name)
            funcs += function
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            loom_home = root / "loom"
            agent_home = root / "agent-home"
            script = (funcs
                      + 'export LOOM_HOME="$1" SLOT=codex-1 LANE=codex\n'
                      + 'export LOOM_DROP_TREES=1 AGENT_HOME="$2" '
                        'CHECK_HOME="$3" TICKET= WORKDIR=\n'
                      + 'TMPDIR="$(run_tmpdir)" || exit 1\n'
                      + 'drop_run_scratch\n'
                      + 'printf "tmpdir=%s exists=%s\\n" "$TMPDIR" '
                        '"$([ -d "$TMPDIR" ] && echo yes || echo no)"\n')
            result = subprocess.run(
                ["bash", "-c", script, "bash", str(loom_home),
                 str(agent_home), str(root / "check")],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(
                f"tmpdir={loom_home / 'run' / 'codex-1-tmp'} exists=yes",
                result.stdout)


if __name__ == "__main__":
    unittest.main()
