"""A fix round's tests of the pull request's own code pass the base-test gate.

A fix round starts from the pull request head, and the base-test gate runs
the round's changed tests against that start. The head already holds the
code the pull request adds, so a new test of that code passes there, and the
round failed with "a changed test passed on base" although the same test
fails on the default branch, as a real test of the change should (loom#2831,
seen on 2026-11-07 on two fix rounds of one pull request).
"""
import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from bus import vacuous


class _Repo:
    def __init__(self, root: str):
        self.root = root
        self.path = Path(root)
        self.git("init", "-q")

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", self.root, *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def write(self, files):
        for name, text in files.items():
            target = self.path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)

    def commit(self, message: str) -> str:
        self.git("add", ".")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.com",
                 "commit", "-qm", message)
        return self.git("rev-parse", "HEAD")


def _fix_round(root: str):
    """(default, start, head): the default branch, the pull request head the
    round starts from (it already defines moved()), and the round's head,
    which adds a test of moved() and a test of value(), which the default
    branch already has."""
    repo = _Repo(root)
    repo.write({"app.py": "def value():\n    return 1\n",
                "tests/__init__.py": ""})
    default = repo.commit("the default branch")
    repo.git("checkout", "-q", "-b", "pr")
    repo.write({"app.py": "def value():\n    return 1\n"
                          "def moved():\n    return 2\n"})
    start = repo.commit("the pull request moves a function")
    repo.write({"tests/test_moved.py":
                    "import app, unittest\n"
                    "class Moved(unittest.TestCase):\n"
                    "    def test_moved_returns_two(self):\n"
                    "        self.assertEqual(app.moved(), 2)\n"
                    "    def test_value_returns_one(self):\n"
                    "        self.assertEqual(app.value(), 1)\n"})
    head = repo.commit("the fix round adds direct tests")
    return default, start, head


OWN = "tests.test_moved.Moved.test_moved_returns_two"
VACUOUS = "tests.test_moved.Moved.test_value_returns_one"


class AFixRoundTestOfThePullRequestsOwnCode(unittest.TestCase):
    def test_a_test_that_fails_on_the_default_branch_is_the_pull_requests_own(self):
        with tempfile.TemporaryDirectory() as root:
            default, start, head = _fix_round(root)
            result = vacuous.git_base_test_results(root, start, head, timeout=20,
                                                   default=default)
        self.assertNotIn(OWN, result.passed_on_base)
        self.assertIn(OWN, result.failed_on_base)

    def test_a_test_that_passes_on_both_is_still_refused(self):
        with tempfile.TemporaryDirectory() as root:
            default, start, head = _fix_round(root)
            result = vacuous.git_base_test_results(root, start, head, timeout=20,
                                                   default=default)
        self.assertEqual(result.passed_on_base, [VACUOUS])

    def test_without_the_default_both_pass_on_the_rounds_start_as_before(self):
        with tempfile.TemporaryDirectory() as root:
            _, start, head = _fix_round(root)
            result = vacuous.git_base_test_results(root, start, head, timeout=20)
        self.assertEqual(sorted(result.passed_on_base), sorted([OWN, VACUOUS]))

    def test_the_command_line_takes_the_default(self):
        with tempfile.TemporaryDirectory() as root:
            default, start, head = _fix_round(root)
            out = io.StringIO()
            with redirect_stdout(out):
                code = vacuous.main([root, start, head, "--timeout", "20",
                                     "--default", default])
        self.assertEqual((code, out.getvalue()), (1, "base-pass: %s\n" % VACUOUS))


class TheLaneRunnerPassesTheDefault(unittest.TestCase):
    def test_final_base_tests_names_the_fix_rounds_default_merge_base(self):
        script = (Path(__file__).resolve().parent.parent / "lanes" / "run_lane.sh").read_text()
        start = script.index("final_base_tests() {")
        body = script[start:script.index("\n}\n", start)]
        self.assertIn('safe_git merge-base HEAD "origin/$FIX_DEFAULT"', body)
        self.assertIn("printf -v command '%s --default %q' \"$command\" \"$default_rev\"",
                      body)
        self.assertIn('[ -n "${FIX_PR:-}" ] && [ -n "${FIX_DEFAULT:-}" ]', body)


if __name__ == "__main__":
    unittest.main()
