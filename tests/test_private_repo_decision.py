"""Private repos stay on the Mac with a cap (decision 0004).

The decision must name the lane workflow repository, the OIDC subject
anchor and the log exposure, and must record the Mac cap. This test
reads the decision doc and the cluster-lanes design doc the way a
reader reads them.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DECISION = ROOT / "docs" / "decisions" / "0004-private-repos-on-cluster-lanes.md"
DESIGN = ROOT / "docs" / "LANES_ON_K8S.md"


class PrivateRepoDecision(unittest.TestCase):
    def setUp(self):
        self.assertTrue(DECISION.is_file(), "docs/decisions/0004 is missing")
        self.decision = DECISION.read_text(encoding="utf-8")
        self.design = DESIGN.read_text(encoding="utf-8")

    def test_the_decision_names_the_repository(self):
        self.assertIn("quadseven/loom", self.decision)

    def test_the_decision_names_the_oidc_anchor(self):
        self.assertIn("OIDC", self.decision)
        self.assertIn(".github/workflows/lane.yml", self.decision)
        self.assertTrue(re.search(r"\bmain\b", self.decision) is not None)

    def test_the_decision_names_the_log_exposure(self):
        text = self.decision.lower()
        self.assertIn("log", text)
        self.assertIn("public", text)
        self.assertIn("actions", text)
        self.assertIn("mac", text)

    def test_the_decision_records_the_mac_cap(self):
        self.assertIn("cap", self.decision.lower())
        self.assertIn("3", self.decision)

    def test_the_design_records_the_same_cap(self):
        self.assertIn("cap", self.design.lower())
        self.assertIn("Mac", self.design)


if __name__ == "__main__":
    unittest.main()
