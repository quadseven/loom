import os
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

RUNNER_PATH = Path(__file__).resolve().parent.parent / ".github" / "unittest_parallel.py"
SPEC = importlib.util.spec_from_file_location("unittest_parallel", RUNNER_PATH)
_test_runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = _test_runner
SPEC.loader.exec_module(_test_runner)


class LocalGitPushTest(unittest.TestCase):
    def test_preflight_ignores_injected_git_hooks_and_allows_local_push(self):
        with tempfile.TemporaryDirectory() as directory:
            hooks = Path(directory) / "hooks"
            hooks.mkdir()
            hook = hooks / "pre-push"
            hook.write_text("#!/bin/sh\necho injected-hook-ran >&2\nexit 1\n")
            hook.chmod(0o755)
            previous = os.environ.get("GIT_CONFIG_COUNT")
            previous_key = os.environ.get("GIT_CONFIG_KEY_0")
            previous_value = os.environ.get("GIT_CONFIG_VALUE_0")
            previous_home = os.environ.get("HOME")
            os.environ["HOME"] = directory
            os.environ.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.hooksPath",
                              GIT_CONFIG_VALUE_0=str(hooks))
            try:
                env = _test_runner.child_env(Path(__file__).parent)
                self.assertNotIn("GIT_CONFIG_COUNT", env)
                self.assertIsNone(_test_runner.local_push_check(Path(__file__).parent))
            finally:
                for name, value in (("GIT_CONFIG_COUNT", previous),
                                    ("GIT_CONFIG_KEY_0", previous_key),
                                    ("GIT_CONFIG_VALUE_0", previous_value)):
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value
                if previous_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = previous_home

    def test_preflight_reports_the_git_error_text(self):
        with tempfile.TemporaryDirectory() as directory:
            fake_git = Path(directory) / "git"
            fake_git.write_text("#!/bin/sh\necho 'fatal: local push denied by injected policy' >&2\nexit 1\n")
            fake_git.chmod(0o755)
            old_path = os.environ.get("PATH", "")
            os.environ["PATH"] = directory + os.pathsep + old_path
            try:
                error = _test_runner.local_push_check(Path(__file__).parent)
            finally:
                os.environ["PATH"] = old_path
        self.assertIn("local push denied by injected policy", error)

    def _fake_shim(self, directory):
        shim = Path(directory) / "git"
        shim.write_text("#!/bin/bash\n# The session git shim, installed ahead of the real git.\n"
                        "echo 'fleet git: pre-push is missing; refused' >&2\nexit 1\n")
        shim.chmod(0o755)

    def _with_path(self, directory, check, shim_only=False):
        before = dict(os.environ)
        os.environ["PATH"] = directory if shim_only else directory + os.pathsep + before.get("PATH", "")
        try:
            return check()
        finally:
            os.environ.clear()
            os.environ.update(before)

    def test_preflight_bypasses_the_fleet_git_shim(self):
        with tempfile.TemporaryDirectory() as directory:
            self._fake_shim(directory)
            result = self._with_path(directory, lambda: _test_runner.local_push_check(
                Path(__file__).parent))
        self.assertIsNone(result)

    def test_preflight_passes_without_a_shim(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self._with_path(directory, lambda: _test_runner.local_push_check(
                Path(__file__).parent))
        self.assertIsNone(result)

    def test_preflight_does_not_modify_the_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            self._fake_shim(directory)

            def check():
                os.environ["FLEET_REAL_GIT"] = "/nonexistent/git"
                snapshot = dict(os.environ)
                _test_runner.local_push_check(Path(__file__).parent)
                return snapshot == dict(os.environ)

            self.assertTrue(self._with_path(directory, check))

    def test_shim_only_path_falls_back_to_unmodified_path(self):
        with tempfile.TemporaryDirectory() as directory:
            self._fake_shim(directory)
            env = self._with_path(directory, lambda: _test_runner.child_env(
                Path(__file__).parent), shim_only=True)
        self.assertEqual(env["PATH"], directory)


if __name__ == "__main__":
    unittest.main()
