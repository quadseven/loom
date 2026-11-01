"""The review bot's complexity rule, read before a lane pushes."""
import pathlib
import subprocess
import sys
import tempfile
import textwrap
import unittest

from bus import complexity_gate

ROOT = pathlib.Path(__file__).resolve().parent.parent


def tangled(branches: int) -> str:
    """A function with `branches` flat if statements: cyclomatic 1 + branches."""
    lines = ["def run(a):"] + ["    if a == %d:\n        a += 1" % n for n in range(branches)]
    return "\n".join(lines + ["    return a", ""])


def git(tree, *args):
    return subprocess.run(["git", "-C", str(tree), "-c", "user.name=t",
                           "-c", "user.email=t@example.invalid"] + list(args),
                          check=True, capture_output=True, text=True).stdout


class ScanTest(unittest.TestCase):
    def test_measures_agree_with_the_review_bot_on_known_functions(self):
        # Worked by hand from the review bot's rule: one + if + for + the
        # comprehension + the extra boolean operand + with + assert.
        source = textwrap.dedent("""\
            def f(a, b):
                if a and b:
                    for x in a:
                        with x:
                            assert [y for y in x]
            """)
        self.assertEqual(complexity_gate.scores(source)["f"], (7, 1 + 2 + 1))

    def test_one_more_branch_in_a_function_over_the_cap_is_refused(self):
        found = complexity_gate.scan("big.py", tangled(24), tangled(22), set(range(1, 9)))
        self.assertEqual([f.name for f in found], ["run"])
        self.assertIn("23->25 cyclomatic", found[0].text)

    def test_an_over_cap_function_this_change_did_not_raise_passes(self):
        self.assertEqual(
            complexity_gate.scan("big.py", tangled(24), tangled(24), {2}), [])

    def test_an_over_cap_function_the_change_did_not_touch_passes(self):
        source = "def small():\n    return 1\n\n" + tangled(30)
        self.assertEqual(complexity_gate.scan("big.py", source, tangled(5), {1}), [])

    def test_a_new_function_over_the_cap_is_refused(self):
        found = complexity_gate.scan("new.py", tangled(20), None, set(range(1, 40)))
        self.assertEqual([f.name for f in found], ["run"])

    def test_a_file_that_does_not_parse_yields_nothing(self):
        self.assertEqual(complexity_gate.scan("x.py", "def (", None, {1}), [])

    def test_the_failure_text_names_one_literal_next_action(self):
        found = complexity_gate.scan("big.py", tangled(24), tangled(22), set(range(1, 9)))
        text = complexity_gate.failure_text(found)
        self.assertIn("Next action: move the lines this change added to `run` into a new "
                      "small function, and call it from `run`.", text)
        self.assertEqual(text.count("Next action:"), 1)


class CommandTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tree = pathlib.Path(self._tmp.name)
        git(self.tree, "init", "-q", "-b", "main")
        (self.tree / "big.py").write_text(tangled(22))
        git(self.tree, "add", "big.py")
        git(self.tree, "commit", "-qm", "seed")
        self.base = git(self.tree, "rev-parse", "HEAD").strip()

    def run_gate(self):
        return subprocess.run(
            [sys.executable, "-B", "-m", "bus.complexity_gate", "--tree", str(self.tree),
             "--base", self.base], cwd=ROOT, capture_output=True, text=True)

    def commit(self, source):
        (self.tree / "big.py").write_text(source)
        git(self.tree, "commit", "-qam", "edit")

    def test_exit_one_with_the_failure_text_on_a_raised_function(self):
        self.commit(tangled(24))
        got = self.run_gate()
        self.assertEqual(got.returncode, 1, got.stderr)
        self.assertIn("big.py", got.stdout)
        self.assertIn("Next action:", got.stdout)

    def test_exit_zero_on_a_change_that_moved_the_logic_out(self):
        self.commit(tangled(22) + "\n\ndef added(a):\n    return a + 1\n")
        got = self.run_gate()
        self.assertEqual((got.returncode, got.stdout), (0, ""), got.stderr)

    def test_exit_two_when_the_base_cannot_be_read(self):
        self.base = "0" * 40
        self.assertEqual(self.run_gate().returncode, 2)


if __name__ == "__main__":
    unittest.main()
