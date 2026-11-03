import unittest

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
