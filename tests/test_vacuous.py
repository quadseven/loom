import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from bus import vacuous


class VacuousTests(unittest.TestCase):
    def test_names_a_builtin_mapping_with_missing_key_overrides(self):
        source = "class _MonitorMap(dict):\n def __getitem__(self, key): return 1\n"
        self.assertEqual(vacuous.container_lookup_overrides(source), [
            "_MonitorMap.__getitem__"])

    def test_does_not_flag_a_lookup_that_delegates_to_the_builtin(self):
        source = "class LookupMap(dict):\n def __getitem__(self, key): return super().__getitem__(key)\n"
        self.assertEqual(vacuous.container_lookup_overrides(source), [])

    def test_ignores_an_existing_override_when_the_diff_changes_another_line(self):
        source = ("class LookupMap(dict):\n def __getitem__(self, key): return 1\n"
                  "\ndef unrelated():\n return 2\n")
        patch = "@@ -5 +5 @@\n- return 2\n+ return 3\n"
        self.assertEqual(vacuous.container_lookup_overrides(source, patch), [])

    def test_fixture_edit_flags_only_tests_that_reference_the_fixture(self):
        tests = "EXPECTED = {'x': 1}\ndef test_uses_expected():\n assert EXPECTED\ndef test_other():\n assert True\n"
        patch = "@@\n-EXPECTED = {'x': 0}\n+EXPECTED = {'x': 1}\n"
        self.assertEqual(vacuous.fixture_users(tests, patch), ["test_uses_expected"])

    def test_ticket_request_for_fixture_changes_exempts_fixture_finding(self):
        tests = "EXPECTED = {'x': 1}\ndef test_expected():\n assert EXPECTED\n"
        finding = vacuous.diff_findings(
            {"app.py": "VALUE = 1"}, {"tests/test_app.py": tests},
            {"tests/test_app.py": "-EXPECTED = 0\n+EXPECTED = 1"},
            "Please update expected values.")
        self.assertEqual(finding, [])

    def test_test_paths_reject_absolute_and_parent_components(self):
        for path in ("/outside/test_x.py", "tests/../outside.py"):
            with self.assertRaises(ValueError):
                vacuous.safe_test_path(path)

    def test_new_test_file_is_overlaid_and_tested_one_at_a_time(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, "app.py"), "w") as stream:
                stream.write("VALUE = 1\n")
            source = "import app\nimport unittest\nclass TestThing(unittest.TestCase):\n def test_new(self): self.assertEqual(app.VALUE, 1)\n"
            result = vacuous.base_test_results(root, {"tests/test_new.py": source}, timeout=5)
            self.assertEqual(result.passed_on_base, ["tests.test_new.TestThing.test_new"])

    def test_one_failing_test_does_not_hide_a_different_passing_test(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, "app.py"), "w") as stream:
                stream.write("VALUE = 1\n")
            source = ("import app, unittest\nclass TestThing(unittest.TestCase):\n"
                      " def test_fails(self): self.assertEqual(app.VALUE, 2)\n"
                      " def test_passes(self): self.assertEqual(app.VALUE, 1)\n")
            result = vacuous.base_test_results(root, {"tests/test_case.py": source}, timeout=5)
            self.assertEqual(result.passed_on_base, ["tests.test_case.TestThing.test_passes"])

    def test_git_base_check_copies_new_test_file_and_reports_each_test(self):
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root)

            def git(*args):
                subprocess.run(["git", "-C", root, *args], check=True,
                               capture_output=True)

            git("init", "-q")
            (repo / "app.py").write_text("VALUE = 1\n")
            (repo / "tests").mkdir()
            (repo / "tests" / "__init__.py").write_text("")
            git("add", ".")
            git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "base")
            base = subprocess.check_output(["git", "-C", root, "rev-parse", "HEAD"], text=True).strip()
            (repo / "app.py").write_text("VALUE = 2\n")
            source = ("import app, unittest\nclass TestNew(unittest.TestCase):\n"
                      " def test_fails_on_base(self): self.assertEqual(app.VALUE, 2)\n"
                      " def test_passes_on_base(self): self.assertEqual(app.VALUE, 1)\n")
            (repo / "tests" / "test_new.py").write_text(source)
            git("add", ".")
            git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "head")
            head = subprocess.check_output(["git", "-C", root, "rev-parse", "HEAD"], text=True).strip()
            result = vacuous.git_base_test_results(root, base, head, timeout=5)
            self.assertEqual(result.passed_on_base, ["tests.test_new.TestNew.test_passes_on_base"])
            self.assertEqual(result.failed_on_base, ["tests.test_new.TestNew.test_fails_on_base"])

