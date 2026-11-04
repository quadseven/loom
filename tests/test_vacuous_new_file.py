"""Tests for fixture finding with newly added test files."""
import unittest
from unittest import mock

from bus import vacuous


class NewTestFileFixtureTests(unittest.TestCase):
    def test_diff_findings_skips_fixture_finding_for_new_test_file(self):
        """diff_findings given a code change plus an added test file whose
        tests use a module-level helper defined in that file returns no
        'changes a fixture used by test' finding."""
        code_sources = {"app.py": "VALUE = 1\n"}
        code_patches = {"app.py": "@@\n-VALUE = 0\n+VALUE = 1\n"}
        test_sources = {
            "tests/test_new_module.py": (
                "HELPER = 'value'\n"
                "def test_uses_helper():\n"
                "    assert HELPER == 'value'\n"
            )
        }
        test_patches = {
            "tests/test_new_module.py": (
                "@@ -0,0 +1,4 @@\n"
                "+HELPER = 'value'\n"
                "+\n"
                "+def test_uses_helper():\n"
                "+    assert HELPER == 'value'\n"
            )
        }
        findings = vacuous.diff_findings(
            code_sources, test_sources, test_patches, "",
            code_patches,
            added_test_files={"tests/test_new_module.py"}
        )
        fixture_findings = [f for f in findings if f.startswith("changes a fixture")]
        self.assertEqual(fixture_findings, [])

    def test_pull_findings_skips_fixture_finding_for_added_test_file(self):
        """pull_findings with a fake client whose pulls files answer lists
        the test file with status 'added' and a code file with a change
        returns no fixture finding."""
        gh = mock.MagicMock()
        # Mock the PR files response: one added test file, one modified code file
        gh.rest.return_value.data = [
            {"filename": "tests/test_new_module.py", "status": "added",
             "patch": (
                 "@@ -0,0 +1,4 @@\n"
                 "+HELPER = 'value'\n"
                 "+\n"
                 "+def test_uses_helper():\n"
                 "+    assert HELPER == 'value'\n"
             )},
            {"filename": "app.py", "status": "modified",
             "patch": "@@\n-VALUE = 0\n+VALUE = 1\n"},
        ]
        # Mock the contents response for the code file
        def rest_side_effect(method, url, ok=()):
            if "contents/app.py" in url:
                return mock.MagicMock(data={"content": "VkFMVUUgPSAxCg=="})  # base64 of "VALUE = 1\n"
            if "contents/tests/test_new_module.py" in url:
                return mock.MagicMock(data={"content": "SEVMUEVSIH0gJ3ZhbHVlJwoKZGVmIHRlc3RfdXNlc19oZWxwZXIoKToKICAgIGFzc2VydCBIRUxQRVIgPT0gJ3ZhbHVlJwo="})  # base64 of test file
            return mock.MagicMock(data=[])
        gh.rest.side_effect = rest_side_effect

        findings = vacuous.pull_findings(gh, "owner/repo", 123, "head-sha", "")
        fixture_findings = [f for f in findings if f.startswith("changes a fixture")]
        self.assertEqual(fixture_findings, [])


if __name__ == "__main__":
    unittest.main()