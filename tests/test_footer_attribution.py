from __future__ import annotations

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import pr_repair  # noqa: E402
from dashboard import views  # noqa: E402


BRIEF = """# Brief: example-owner.example-repo-42

Repo: example-owner/example-repo
Branch: loom/42-fix-the-thing
Lane: muse
Issue: example-owner/example-repo#42

## Why
The thing is broken.

## Summary
Fix the thing.

## Acceptance criteria
- [ ] The thing works
- [ ] A test covers the thing
- [ ] Nothing else changes

## Size
S: one function.

## Out of scope
- Other things
"""


class FooterValidationTest(unittest.TestCase):
    def test_render_raises_for_model_with_whitespace(self):
        with self.assertRaises(ValueError):
            pr_repair._render_generated(
                pr_repair._normalise(BRIEF), "muse", "muse spark", "T42")

    def test_render_raises_for_empty_model(self):
        with self.assertRaises(ValueError):
            pr_repair._render_generated(
                pr_repair._normalise(BRIEF), "muse", "", "T42")

    def test_render_raises_for_lane_with_whitespace(self):
        with self.assertRaises(ValueError):
            pr_repair._render_generated(
                pr_repair._normalise(BRIEF), "my lane", "muse-spark-1.3", "T42")

    def test_rendered_footer_parses_with_dashboard_regex(self):
        body = pr_repair._render_generated(
            pr_repair._normalise(BRIEF), "muse", "muse-spark-1.3",
            "example-owner.example-repo-42")
        m = views.FOOTER.search(body)
        self.assertIsNotNone(
            m, "rendered footer must parse with dashboard/views.py FOOTER")
        self.assertEqual(
            m.groups(),
            ("example-owner", "example-repo", "42", "muse", "muse-spark-1.3"))


class LaneInstallModelTest(unittest.TestCase):
    def test_muse_model_default_present(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, "lanes", "mac", "install.sh")
        with open(path, encoding="utf-8") as fh:
            content = fh.read()
        m = re.search(
            r'^LOOM_MUSE_MODEL="\$\{LOOM_MUSE_MODEL:-(.+?)\}"$', content, re.M)
        self.assertIsNotNone(m, "install.sh must default LOOM_MUSE_MODEL")
        self.assertTrue(m.group(1).strip(), "default must be non-empty")
        self.assertNotIn(" ", m.group(1),
                         "default model id must be a single token")


if __name__ == "__main__":
    unittest.main()
