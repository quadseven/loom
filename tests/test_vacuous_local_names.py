import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from bus import vacuous


class LocalNameFixtureTests(unittest.TestCase):
    def test_new_tests_do_not_mark_existing_tests_as_fixture_users(self):
        source = '''
ROWS = []
def world(**kw):
    return kw

class ExistingTests:
    def test_existing_cat(self):
        cat = world(title="old")
        self.assertTrue(cat)

    def test_existing_plan(self):
        p = world(title="old")
        self.assertTrue(p)

class AddedTests:
    def test_added_cat(self):
        cat = world(title="new")
        self.assertTrue(cat)

    def test_added_plan(self):
        p = world(title="new")
        self.assertTrue(p)
'''
        patch = '''
@@ -1,4 +1,4 @@
 ROWS = []
-def world(**kwargs):
+def world(**kw):
     return kwargs
@@ -12,3 +12,12 @@
         self.assertTrue(cat)
 
+class AddedTests:
+    def test_added_cat(self):
+        cat = world(title="new")
+        self.assertTrue(cat)
+
+    def test_added_plan(self):
+        p = world(title="new")
+        self.assertTrue(p)
'''
        self.assertEqual(vacuous.changed_test_names(
            "tests/test_example.py", source, patch), [
                "tests.test_example.AddedTests.test_added_cat",
                "tests.test_example.AddedTests.test_added_plan",
            ])


class FixtureUserVerdictTests(unittest.TestCase):
    def test_fixture_only_passes_do_not_fail_the_base_verdict(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, "app.py"), "w") as stream:
                stream.write("VALUE = 1\n")
            source = (
                "import app\n"
                "import unittest\n"
                "\n"
                "def world(*args):\n"
                "    return app.VALUE\n"
                "\n"
                "\n"
                "class ExistingTests(unittest.TestCase):\n"
                "    def test_existing_world(self):\n"
                "        self.assertEqual(world(), 1)\n"
                "\n"
                "\n"
                "class AddedTests(unittest.TestCase):\n"
                "    def test_added_world(self):\n"
                "        self.assertEqual(world(), 2)\n"
                "\n"
                "    def test_added_passing(self):\n"
                "        self.assertEqual(world(), 1)\n")
            patch = (
                "@@ -4 +4 @@\n"
                "-def world():\n"
                "+def world(*args):\n"
                "     return app.VALUE\n"
                "@@ -12 +12,7 @@\n"
                " \n"
                "+class AddedTests(unittest.TestCase):\n"
                "+    def test_added_world(self):\n"
                "+        self.assertEqual(world(), 2)\n"
                "+\n"
                "+    def test_added_passing(self):\n"
                "+        self.assertEqual(world(), 1)\n")
            path = "tests/test_example.py"
            # The untouched test stays named as a user of the changed
            # helper, alongside the tests the patch itself adds.
            self.assertEqual(vacuous.changed_test_names(path, source, patch), [
                "tests.test_example.AddedTests.test_added_passing",
                "tests.test_example.AddedTests.test_added_world",
                "tests.test_example.ExistingTests.test_existing_world",
            ])
            result = vacuous.base_test_results(
                root, {path: source}, timeout=20, test_patches={path: patch})
            # The fixture-only test passes on base but must not fail the
            # verdict; a test the patch adds that passes on base still does.
            self.assertEqual(result.passed_on_base, [
                "tests.test_example.AddedTests.test_added_passing"])
            self.assertEqual(result.failed_on_base, [
                "tests.test_example.AddedTests.test_added_world"])

    def test_git_base_verdict_ignores_fixture_only_passes(self):
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root)

            def git(*args):
                subprocess.run(["git", "-C", root, *args], check=True,
                               capture_output=True)

            git("init", "-q")
            (repo / "app.py").write_text("VALUE = 1\n")
            (repo / "tests").mkdir()
            (repo / "tests" / "__init__.py").write_text("")
            base_source = (
                "import app\n"
                "import unittest\n"
                "\n"
                "def world():\n"
                "    return app.VALUE\n"
                "\n"
                "class ExistingTests(unittest.TestCase):\n"
                "    def test_existing_world(self):\n"
                "        self.assertEqual(world(), 1)\n")
            (repo / "tests" / "test_example.py").write_text(base_source)
            git("add", ".")
            git("-c", "user.name=Test", "-c", "user.email=test@example.com",
                "commit", "-qm", "base")
            base = subprocess.check_output(
                ["git", "-C", root, "rev-parse", "HEAD"], text=True).strip()
            (repo / "app.py").write_text("VALUE = 2\n")
            head_source = base_source.replace(
                "def world():", "def world(*args):") + (
                "\n"
                "class AddedTests(unittest.TestCase):\n"
                "    def test_added_world(self):\n"
                "        self.assertEqual(world(), 2)\n")
            (repo / "tests" / "test_example.py").write_text(head_source)
            git("add", ".")
            git("-c", "user.name=Test", "-c", "user.email=test@example.com",
                "commit", "-qm", "head")
            head = subprocess.check_output(
                ["git", "-C", root, "rev-parse", "HEAD"], text=True).strip()
            result = vacuous.git_base_test_results(root, base, head, timeout=20)
            self.assertEqual(result.passed_on_base, [])
            self.assertEqual(result.failed_on_base, [
                "tests.test_example.AddedTests.test_added_world"])
