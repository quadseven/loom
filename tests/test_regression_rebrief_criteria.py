"""Regression re-briefs retain the new brief's own work and criteria."""
from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain  # noqa: E402
from bus.github import GitHub  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401


class RegressionRebriefCriteriaTests(unittest.TestCase):
    def test_rebrief_writes_brief_what_and_unticked_criteria(self):
        issue = {"number": 5, "state": "open",
                 "body": "Difficulty: easy\n\n## Why\nThe shipped behavior regressed.",
                 "labels": [{"name": "loom:ticket"}, {"name": "loom:verified"}]}
        calls = []

        def transport(method, url, headers, body):
            calls.append((method, url, json.loads(body) if body else None))
            return 200, {}, b"{}"

        gh = GitHub("placeholder", transport=transport)
        brain._rebrief_regressed(
            gh, "example-owner/example-repo", issue, "Restore slot fallback",
            ["fallback was not selected"], "codex", None,
            "Use the base lane settings file when the runner slot file is absent.",
            "- [ ] Add coverage for the base settings fallback.\n"
            "- [ ] Preserve the slot-specific settings when present.")

        body = next(call[2]["body"] for call in calls if call[0] == "PATCH")
        regression = body.split("## Regression", 1)[1]
        self.assertIn("### What\n\nUse the base lane settings file", regression)
        self.assertIn("- [ ] Add coverage for the base settings fallback.", regression)
        self.assertIn("- [ ] Preserve the slot-specific settings when present.", regression)
        self.assertNotIn("- [x]", regression.lower())


if __name__ == "__main__":
    unittest.main()
