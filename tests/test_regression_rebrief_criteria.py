"""Regression re-briefs retain the new brief's own work and criteria."""
from __future__ import annotations

import json
import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain  # noqa: E402
from bus.github import GitHub, refuse_incomplete_close  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401


class RegressionRebriefCriteriaTests(unittest.TestCase):
    def test_rebrief_path_updates_top_level_contract_and_close_gate(self):
        issue = {"number": 5, "state": "open",
                 "body": ("Difficulty: easy\n\n## Why\nThe shipped behavior regressed.\n\n"
                          "## Acceptance criteria\n- [x] Old behavior is covered.\n"),
                 "labels": [{"name": "loom:ticket"}, {"name": "loom:shipped"}]}
        calls = []

        def transport(method, url, headers, body):
            calls.append((method, url, json.loads(body) if body else None))
            return 200, {}, b"{}"

        gh = GitHub("placeholder", transport=transport)
        with unittest.mock.patch.object(brain, "_goal_children", return_value=[issue]), \
                unittest.mock.patch.object(brain, "_regression_attempt", return_value=None), \
                unittest.mock.patch.object(brain, "_regressed_ticket", return_value=issue), \
                unittest.mock.patch.object(brain, "_regression_slot", return_value=None), \
                unittest.mock.patch.object(brain, "_regression_rebriefs", return_value=0):
            brain._brief_regression(
                gh, "example-owner/example-repo", 1, "Restore slot fallback",
                "Regresses #5\nfallback was not selected\n\n", "codex", None,
                "## What\nUse the base lane settings file when the runner slot file is absent.\n\n"
                "## Acceptance criteria\n- [ ] Add coverage for the base settings fallback.\n"
                "- [ ] Preserve the slot-specific settings when present.\n",
                5, issue=issue)

        body = next(call[2]["body"] for call in calls if call[0] == "PATCH")
        regression = body.split("## Regression", 1)[1]
        self.assertIn("### What\n\nUse the base lane settings file", regression)
        self.assertIn("### Previous acceptance criteria\n\n- [x] Old behavior is covered.", regression)
        self.assertIn("## Acceptance criteria\n\n- [ ] Add coverage for the base settings fallback.", body)
        self.assertIn("- [ ] Preserve the slot-specific settings when present.", body)
        self.assertEqual(len([item for item in brain.proof.criteria(body) if not item[0]]), 2)
        checked_issue = {"body": body, "labels": []}
        close_gh = GitHub("placeholder", transport=lambda method, url, headers, payload:
                          (200, {}, json.dumps(checked_issue).encode()))
        with self.assertRaisesRegex(ValueError, "unchecked acceptance criterion"):
            refuse_incomplete_close(close_gh, "PATCH", "/repos/example-owner/example-repo/issues/5",
                                    {"state": "closed"})

    def test_omitted_sections_do_not_create_empty_regression_headings(self):
        calls = []

        def transport(method, url, headers, body):
            calls.append((method, json.loads(body) if body else None))
            return 200, {}, b"{}"

        gh = GitHub("placeholder", transport=transport)
        brain._rebrief_regressed(gh, "example-owner/example-repo",
                                 {"number": 5, "body": "## Why\nOld."}, "Regression",
                                 ["evidence"], None, None)
        body = next(data["body"] for method, data in calls if method == "PATCH")
        self.assertNotIn("### What", body)
        self.assertNotIn("### Previous acceptance criteria", body)
        self.assertNotIn("## Acceptance criteria", body)


if __name__ == "__main__":
    unittest.main()
