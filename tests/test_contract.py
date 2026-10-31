import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import contract  # noqa: E402


class ContractFormTests(unittest.TestCase):
    def test_key_value_lines_pass_through(self):
        text = "# comment\ninterpreter: uv\nsubmodules: no\ncheck: cd server && make test\n"
        self.assertEqual(contract.normalize(text),
                         "interpreter: uv\nsubmodules: no\ncheck: cd server && make test\n")

    def test_a_key_value_check_may_hold_a_pipe(self):
        text = "check: git ls-files | grep -c x\n"
        self.assertEqual(contract.normalize(text), "check: git ls-files | grep -c x\n")

    def test_pipe_lines_become_one_check_in_kind_order(self):
        # infra-public's form: refused as an unknown line before (2026-10-16).
        text = ("# header\n"
                "test | python3 t.py | .github/workflows/a.yml | tests\n"
                "lint | python3 lint.py | .github/workflows/b.yml | lint\n"
                "test | node --test x.test.js | .github/workflows/c.yml | node\n")
        self.assertEqual(contract.normalize(text),
                         "interpreter: python3\ncheck: python3 lint.py && python3 t.py && node --test x.test.js\n")

    def test_tables_become_one_check_and_uv_is_the_interpreter(self):
        text = ('[lint]\ncommand = "uv run ruff check ."\nworkflow = "a.yml"\nstep = "lint"\n\n'
                '[type]\ncommand = ""\nworkflow = ""\nstep = "none"\n\n'
                '[test.scorer]\ncommand = "node --test s.test.mjs"\nworkflow = "b.yml"\nstep = "s"\n')
        self.assertEqual(contract.normalize(text),
                         "interpreter: uv\ncheck: uv run ruff check . && node --test s.test.mjs\n")

    def test_array_tables_read_like_tables(self):
        # The 2026-10-20 contract a lane wrote: refused as an unknown line.
        text = ('[[lint]]\ncommand = "uv run ruff check ."\nworkflow = "a.yml"\nstep = "lint"\n\n'
                '[[test]]\ncommand = "uv run pytest"\nstep = "t"\n\n'
                '[[test]]\ncommand = "node --test s.test.mjs"\n')
        self.assertEqual(contract.normalize(text),
                         "interpreter: uv\ncheck: uv run ruff check . && uv run pytest && node --test s.test.mjs\n")
        self.assertEqual(contract.normalize('[[test.scorer]]\ncommand = "make t"\n'),
                         "interpreter: make\ncheck: make t\n")

    def test_a_malformed_table_header_is_refused(self):
        for text in ('[[lint]\ncommand = "x"\n', '[lint]]\ncommand = "x"\n'):
            with self.assertRaises(ValueError, msg=text):
                contract.normalize(text)

    def test_a_contract_listing_no_command_checks_nothing(self):
        self.assertEqual(contract.normalize("# no pull-request workflows here\n"), "check: true\n")
        self.assertEqual(contract.normalize("[type]\ncommand = \"\"\n"), "check: true\n")

    def test_unreadable_contracts_are_refused(self):
        for text in ("", "interpreter: uv\nbogus line\n", "lint |  | a.yml | s\n",
                     "deploy | make | a.yml | s\n", "[lint]\nrun = \"x\"\n", "[lint]\ncommand =\n"):
            with self.assertRaises(ValueError, msg=text):
                contract.normalize(text)

    def test_unquoted_command_parses(self):
        text = ('[lint]\ncommand = uv run ruff check .\nworkflow = a.yml\nstep = lint\n')
        self.assertEqual(contract.normalize(text),
                         "interpreter: uv\ncheck: uv run ruff check .\n")

    def test_quoted_command_parses_unchanged(self):
        text = '[test]\ncommand = "uv run pytest -q"\n'
        self.assertEqual(contract.normalize(text),
                         "interpreter: uv\ncheck: uv run pytest -q\n")

    def test_unquoted_four_tables_join_in_kind_order(self):
        text = ('[lint]\ncommand = uv run ruff check .\nworkflow = a.yml\nstep = lint\n\n'
                '[format]\ncommand = uv run ruff format --check .\nworkflow = a.yml\nstep = format\n\n'
                '[type]\ncommand = uv run ty check\nworkflow = a.yml\nstep = type\n\n'
                '[test]\ncommand = uv run pytest -q\nworkflow = a.yml\nstep = test\n')
        self.assertEqual(contract.normalize(text),
                         "interpreter: uv\ncheck: uv run ruff check . && uv run ruff format --check . && uv run ty check && uv run pytest -q\n")


if __name__ == "__main__":
    unittest.main()


class LoomOwnContractTests(unittest.TestCase):
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def test_loom_s_own_check_runs_the_suite_the_way_ci_does(self):
        # 2026-10-24: the contract ran `unittest discover` serially (808 s for
        # 3596 tests) while CI runs .github/unittest_parallel.py (about 140 s).
        # A lane runs the check twice per ticket, so every Loom ticket spent
        # about 27 minutes in checks before and after the agent's own work.
        with open(os.path.join(self.ROOT, ".loom", "validation.conf"), encoding="utf-8") as fh:
            check = next(line.split(":", 1)[1].strip() for line in fh
                         if line.startswith("check:"))
        with open(os.path.join(self.ROOT, ".github", "workflows", "test.yml"), encoding="utf-8") as fh:
            workflow = fh.read()
        self.assertIn(".github/unittest_parallel.py", workflow)
        self.assertIn(".github/unittest_parallel.py", check)


class AffectedContractTests(unittest.TestCase):
    """loom#1773: an affected command runs after the check with the pull
    request's changed files, so it is kept apart from the check."""

    def test_an_affected_line_passes_through_with_the_check(self):
        text = "check: make test\naffected: scripts/affected.sh\n"
        self.assertEqual(contract.normalize(text), text)
        self.assertEqual(contract.fields(text),
                         {"check": "make test", "affected": "scripts/affected.sh"})

    def test_an_affected_pipe_line_or_table_is_not_joined_into_the_check(self):
        pipes = ("test | python3 t.py | a.yml | tests\n"
                 "affected | python3 affected.py | b.yml | per-dir\n")
        self.assertEqual(contract.normalize(pipes),
                         "interpreter: python3\ncheck: python3 t.py\naffected: python3 affected.py\n")
        tables = '[test]\ncommand = "make t"\n\n[affected]\ncommand = "make affected"\n'
        self.assertEqual(contract.normalize(tables),
                         "interpreter: make\ncheck: make t\naffected: make affected\n")

    def test_a_contract_with_only_an_affected_command_checks_nothing_else(self):
        self.assertEqual(contract.normalize("affected | ./per-dir.sh\n"),
                         "check: true\naffected: ./per-dir.sh\n")
