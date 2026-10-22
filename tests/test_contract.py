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
                     "deploy | make | a.yml | s\n", "[lint]\nrun = \"x\"\n"):
            with self.assertRaises(ValueError, msg=text):
                contract.normalize(text)


if __name__ == "__main__":
    unittest.main()
