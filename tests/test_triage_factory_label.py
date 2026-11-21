"""A lone loom:factory label does not hide a hand-filed issue from triage."""
from __future__ import annotations

import unittest

from bus import brain

MARKER = "<!-- loom:factory-incident v1 -->"


def issue(labels, body="body"):
    return {"number": 5, "state": "open", "title": "Something",
            "body": body, "labels": [{"name": n} for n in labels]}


class TriageFactoryLabelTest(unittest.TestCase):
    def test_factory_label_alone_is_triaged(self):
        self.assertIsNone(brain._triage_skip(issue(["loom:factory", "bug"])))

    def test_incident_marker_is_still_skipped(self):
        self.assertEqual("factory incident",
                         brain._triage_skip(issue(["loom:factory"], MARKER)))

    def test_factory_with_ticket_is_still_skipped(self):
        self.assertEqual("already labelled",
                         brain._triage_skip(issue(["loom:factory", "loom:ticket"])))


if __name__ == "__main__":
    unittest.main()
