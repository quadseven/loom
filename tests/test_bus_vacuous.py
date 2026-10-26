"""Tests for the vacuous-test gate (loom#1681)."""
from __future__ import annotations

import unittest
from bus import vacuous


class VacuousFindingsTest(unittest.TestCase):
    def test_builtin_dict_subclass_overrides_getitem(self):
        changed = {
            "src/mydict.py": """
class MyDict(dict):
    def __getitem__(self, key):
        return super().get(key, "default")
"""
        }
        findings = vacuous.findings(changed, "", "")
        self.assertEqual(len(findings), 1)
        self.assertIn("subclasses builtin dict", findings[0])
        self.assertIn("__getitem__", findings[0])
        self.assertIn("MyDict", findings[0])

    def test_builtin_list_subclass_overrides_contains(self):
        changed = {
            "src/mylist.py": """
class MyList(list):
    def __contains__(self, item):
        return True
"""
        }
        findings = vacuous.findings(changed, "", "")
        self.assertEqual(len(findings), 1)
        self.assertIn("subclasses builtin list", findings[0])
        self.assertIn("__contains__", findings[0])

    def test_builtin_set_subclass_overrides_get(self):
        changed = {
            "src/myset.py": """
class MySet(set):
    def get(self, key, default=None):
        return default
"""
        }
        findings = vacuous.findings(changed, "", "")
        self.assertEqual(len(findings), 1)
        self.assertIn("subclasses builtin set", findings[0])
        self.assertIn("get", findings[0])

    def test_normal_dict_subclass_no_override(self):
        changed = {
            "src/mydict.py": """
class MyDict(dict):
    def extra_method(self):
        return 42
"""
        }
        findings = vacuous.findings(changed, "", "")
        self.assertEqual(findings, [])

    def test_fixture_edited_with_code_no_ticket_ask(self):
        changed = {
            "src/calculator.py": "def add(a, b): return a + b + 1",
            "tests/test_calculator.py": """
EXPECTED = {"add(1, 2)": 4}
def test_add():
    assert add(1, 2) == EXPECTED["add(1, 2)"]
"""
        }
        findings = vacuous.findings(changed, "", "")
        self.assertEqual(len(findings), 1)
        self.assertIn("edits the expected table or fixture", findings[0])
        self.assertIn("test_add", findings[0])

    def test_fixture_edited_with_code_ticket_asks(self):
        changed = {
            "src/calculator.py": "def add(a, b): return a + b + 1",
            "tests/test_calculator.py": """
EXPECTED = {"add(1, 2)": 4}
def test_add():
    assert add(1, 2) == EXPECTED["add(1, 2)"]
"""
        }
        findings = vacuous.findings(changed, "Please update the expected fixture table", "")
        self.assertEqual(findings, [])

    def test_fixture_edited_with_code_pr_body_asks(self):
        changed = {
            "src/calculator.py": "def add(a, b): return a + b + 1",
            "tests/test_calculator.py": """
EXPECTED = {"add(1, 2)": 4}
def test_add():
    assert add(1, 2) == EXPECTED["add(1, 2)"]
"""
        }
        findings = vacuous.findings(changed, "", "This PR updates the expected fixture table")
        self.assertEqual(len(findings), 1)
        self.assertIn("edits the expected table or fixture", findings[0])
        self.assertIn("test_add", findings[0])

    def test_only_fixture_no_code_change(self):
        changed = {
            "tests/test_calculator.py": """
EXPECTED = {"add(1, 2)": 4}
def test_add():
    assert add(1, 2) == EXPECTED["add(1, 2)"]
"""
        }
        findings = vacuous.findings(changed, "", "")
        self.assertEqual(findings, [])

    def test_only_code_change_no_test(self):
        changed = {
            "src/calculator.py": "def add(a, b): return a + b + 1",
        }
        findings = vacuous.findings(changed, "", "")
        self.assertEqual(findings, [])

    def test_test_passes_on_base(self):
        changed = {
            "src/calculator.py": "def add(a, b): return a + b",
            "tests/test_calculator.py": """
def test_add():
    assert add(1, 2) == 3
"""
        }
        def run_on_base(path, name):
            return path == "tests/test_calculator.py" and name == "test_add"
        findings = vacuous.findings(changed, "", "", run_on_base=run_on_base)
        self.assertEqual(len(findings), 1)
        self.assertIn("passes against the base commit", findings[0])
        self.assertIn("test_add", findings[0])

    def test_test_fails_on_base(self):
        changed = {
            "src/calculator.py": "def add(a, b): return a + b",
            "tests/test_calculator.py": """
def test_add():
    assert add(1, 2) == 3
"""
        }
        def run_on_base(path, name):
            return False
        findings = vacuous.findings(changed, "", "", run_on_base=run_on_base)
        self.assertEqual(findings, [])

    def test_only_added_or_changed_test_names_are_checked(self):
        changed = {"tests/test_calculator.py": """
def test_new_regression():
    assert add(1, 2) == 4

def test_older_case():
    assert add(2, 2) == 4
"""}
        checked = []

        def run_on_base(path, name):
            checked.append(name)
            return name == "test_new_regression"

        findings = vacuous.findings(changed, "", "", run_on_base=run_on_base)
        self.assertEqual(checked, ["test_new_regression", "test_older_case"])
        self.assertEqual(len(findings), 1)
        self.assertIn("test_new_regression", findings[0])

    def test_new_test_file_is_checked_on_base(self):
        changed = {"tests/test_new.py": "def test_new_behavior():\n    assert True\n"}
        findings = vacuous.findings(changed, "", "",
                                    run_on_base=lambda path, name: True)
        self.assertEqual(len(findings), 1)
        self.assertIn("test_new_behavior", findings[0])

    def test_non_test_file_not_flagged(self):
        changed = {
            "src/calculator.py": "def add(a, b): return a + b",
            "docs/readme.md": "# Calculator",
        }
        findings = vacuous.findings(changed, "", "")
        self.assertEqual(findings, [])

    def test_multiple_builtin_overrides(self):
        changed = {
            "src/containers.py": """
class MyDict(dict):
    def __getitem__(self, key):
        return super().get(key, "default")

class MyList(list):
    def __contains__(self, item):
        return True
"""
        }
        findings = vacuous.findings(changed, "", "")
        self.assertEqual(len(findings), 2)

    def test_nested_class_not_flagged(self):
        changed = {
            "src/outer.py": """
class Outer:
    class Inner(dict):
        def __getitem__(self, key):
            return super().get(key, "default")
"""
        }
        findings = vacuous.findings(changed, "", "")
        self.assertEqual(len(findings), 1)
        self.assertIn("Inner", findings[0])


class IsTestFileTest(unittest.TestCase):
    def test_test_folder(self):
        self.assertTrue(vacuous._is_test("tests/test_foo.py"))
        self.assertTrue(vacuous._is_test("test/foo_test.py"))
        self.assertTrue(vacuous._is_test("spec/foo_spec.py"))

    def test_test_name_prefix(self):
        self.assertTrue(vacuous._is_test("test_foo.py"))
        self.assertTrue(vacuous._is_test("foo_test.py"))
        self.assertTrue(vacuous._is_test("foo.test.py"))
        self.assertTrue(vacuous._is_test("foo.spec.py"))

    def test_non_test_file(self):
        self.assertFalse(vacuous._is_test("src/foo.py"))
        self.assertFalse(vacuous._is_test("lib/foo.py"))


class IsCodeFileTest(unittest.TestCase):
    def test_python_file(self):
        self.assertTrue(vacuous._is_code("src/foo.py"))

    def test_js_file(self):
        self.assertTrue(vacuous._is_code("src/foo.js"))

    def test_non_code_file(self):
        self.assertFalse(vacuous._is_code("docs/readme.md"))
        self.assertFalse(vacuous._is_code("config.yaml"))


if __name__ == "__main__":
    unittest.main()
