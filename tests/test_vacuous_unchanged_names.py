"""The base-test gate runs only tests the change itself made or touched.

Two lane faults on 2026-11-03 and 11-04 failed good work as "a changed test
passed on base":

- A patch that added one name to a tuple constant line
  (``A, B = 1, 2`` -> ``A, B, C = 1, 2, 3``) marked every test that used
  ``A`` or ``B`` as a changed fixture user.
- A fix round merges the default branch into the pull request branch after
  the base is taken, so a fixture and tests the default branch changed were
  counted as the round's own changes and run on the old branch head.
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


TUPLE_BASE = (
    "import app, unittest\n"
    "ONE, TWO = 1, 2\n"
    "class Uses(unittest.TestCase):\n"
    "    def test_uses_one(self):\n"
    "        self.assertEqual(app.double(ONE), 2)\n"
    "    def test_uses_two(self):\n"
    "        self.assertEqual(app.double(TWO), 4)\n"
)

TUPLE_HEAD = (
    "import app, unittest\n"
    "ONE, TWO, THREE = 1, 2, 3\n"
    "class Uses(unittest.TestCase):\n"
    "    def test_uses_one(self):\n"
    "        self.assertEqual(app.double(ONE), 2)\n"
    "    def test_uses_two(self):\n"
    "        self.assertEqual(app.double(TWO), 4)\n"
    "    def test_triples_three(self):\n"
    "        self.assertEqual(app.triple(THREE), 9)\n"
)


class ATupleConstantLineThatGainsAName(unittest.TestCase):
    def test_tests_of_the_unchanged_names_are_not_run_on_base(self):
        with tempfile.TemporaryDirectory() as root:
            repo = _Repo(root)
            repo.write({"app.py": "def double(x):\n    return 2 * x\n",
                        "tests/__init__.py": "",
                        "tests/test_uses.py": TUPLE_BASE})
            base = repo.commit("base")
            repo.write({"app.py": "def double(x):\n    return 2 * x\n"
                                  "def triple(x):\n    return 3 * x\n",
                        "tests/test_uses.py": TUPLE_HEAD})
            head = repo.commit("head")
            result = vacuous.git_base_test_results(root, base, head, timeout=20)
        self.assertEqual(result.passed_on_base, [])
        self.assertEqual(result.failed_on_base,
                         ["tests.test_uses.Uses.test_triples_three"])

    def test_a_name_whose_value_changes_still_marks_its_users(self):
        with tempfile.TemporaryDirectory() as root:
            repo = _Repo(root)
            repo.write({"app.py": "def double(x):\n    return 2 * x\n",
                        "tests/__init__.py": "",
                        "tests/test_uses.py": TUPLE_BASE})
            base = repo.commit("base")
            repo.write({"tests/test_uses.py": TUPLE_BASE.replace(
                "ONE, TWO = 1, 2", "ONE, TWO = 1, 3")})
            head = repo.commit("head")
            result = vacuous.git_base_test_results(root, base, head, timeout=20)
        self.assertEqual(result.passed_on_base, [])
        self.assertEqual(result.failed_on_base,
                         ["tests.test_uses.Uses.test_uses_two"])


HARNESS_BASE = (
    "import app, unittest\n"
    "def harness():\n"
    "    return app.value()\n"
    "class Harnessed(unittest.TestCase):\n"
    "    def test_harness_reads_the_value(self):\n"
    "        self.assertEqual(harness(), 1)\n"
)

HARNESS_MAIN = (
    "import app, unittest\n"
    "def harness():\n"
    "    value = app.value()\n"
    "    return value\n"
    "class Harnessed(unittest.TestCase):\n"
    "    def test_harness_reads_the_value(self):\n"
    "        self.assertEqual(harness(), 1)\n"
    "    def test_harness_is_callable(self):\n"
    "        self.assertTrue(callable(harness))\n"
)


class AFixRoundThatMergedTheDefaultBranch(unittest.TestCase):
    def _fix_round(self, root):
        repo = _Repo(root)
        repo.write({"app.py": "def value():\n    return 1\n",
                    "tests/__init__.py": "",
                    "tests/test_harness.py": HARNESS_BASE})
        repo.commit("fork point")
        repo.git("checkout", "-q", "-b", "pr")
        repo.write({"app.py": "def value():\n    return 1\n"
                              "def bonus():\n    return 5\n"})
        base = repo.commit("the pull request so far")
        repo.git("checkout", "-q", "-")
        repo.write({"tests/test_harness.py": HARNESS_MAIN})
        upstream = repo.commit("the default branch changes the harness")
        repo.git("checkout", "-q", "pr")
        repo.git("-c", "user.name=Test", "-c", "user.email=test@example.com",
                 "merge", "-q", "--no-edit", upstream)
        repo.write({"app.py": "def value():\n    return 1\n"
                              "def bonus():\n    return 6\n",
                    "tests/test_bonus.py":
                        "import app, unittest\n"
                        "class Bonus(unittest.TestCase):\n"
                        "    def test_bonus_is_six(self):\n"
                        "        self.assertEqual(app.bonus(), 6)\n"})
        head = repo.commit("the fix round")
        return base, upstream, head

    def test_default_branch_tests_are_not_the_rounds_own(self):
        with tempfile.TemporaryDirectory() as root:
            base, upstream, head = self._fix_round(root)
            result = vacuous.git_base_test_results(root, base, head, timeout=20,
                                                   upstream=upstream)
        self.assertEqual(result.passed_on_base, [])
        self.assertEqual(result.failed_on_base,
                         ["tests.test_bonus.Bonus.test_bonus_is_six"])

    def test_the_command_line_takes_the_upstream(self):
        with tempfile.TemporaryDirectory() as root:
            base, upstream, head = self._fix_round(root)
            out = io.StringIO()
            with redirect_stdout(out):
                code = vacuous.main([root, base, head, "--timeout", "20",
                                     "--upstream", upstream])
        self.assertEqual((code, out.getvalue()), (0, ""))


class TheLaneRunnerPassesTheUpstream(unittest.TestCase):
    def test_final_base_tests_names_the_fix_rounds_upstream(self):
        script = (Path(__file__).resolve().parent.parent / "lanes" / "run_lane.sh").read_text()
        start = script.index("final_base_tests() {")
        body = script[start:script.index("\n}\n", start)]
        self.assertIn('[ -z "${FIX_UPSTREAM:-}" ] || printf -v command '
                      "'%s --upstream %q' \"$command\" \"$FIX_UPSTREAM\"", body)


if __name__ == "__main__":
    unittest.main()
