import base64
import unittest

from bus import vacuous


class NewTestFileFixtureTests(unittest.TestCase):
    def test_diff_findings_ignores_helpers_in_an_added_test_module(self):
        source = "def helper():\n return 1\ndef test_new():\n assert helper() == 1\n"
        patch = "@@ -0,0 +1,4 @@\n+def helper():\n+ return 1\n+def test_new():\n+ assert helper() == 1\n"
        findings = vacuous.diff_findings(
            {"bus/app.py": "VALUE = 1\n"},
            {"tests/test_vacuous_new_file.py": source},
            {"tests/test_vacuous_new_file.py": patch})
        self.assertEqual(findings, [])

    def test_pull_findings_ignores_files_reported_as_added(self):
        test_path = "tests/test_vacuous_new_file.py"
        code_path = "bus/app.py"
        test_source = "def helper():\n return 1\ndef test_new():\n assert helper() == 1\n"
        code_source = "VALUE = 1\n"

        class Response:
            def __init__(self, data):
                self.data = data

        class Client:
            def rest(self, method, path, **kwargs):
                if "/pulls/" in path:
                    return Response([
                        {"filename": test_path, "status": "added",
                         "patch": "@@ -0,0 +1,4 @@\n+def helper():\n+ return 1\n+def test_new():\n+ assert helper() == 1\n"},
                        {"filename": code_path, "status": "modified",
                         "patch": "@@ -1 +1 @@\n-VALUE = 0\n+VALUE = 1\n"},
                    ])
                content = test_source if test_path in path else code_source
                return Response({"content": base64.b64encode(
                    content.encode("utf-8")).decode("ascii")})

        self.assertEqual(vacuous.pull_findings(Client(), "repo", 1, "head"), [])
