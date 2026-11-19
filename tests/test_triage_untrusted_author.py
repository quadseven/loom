import os
import unittest
from unittest import mock

from bus import brain
from bus.github import GitHub
from test_bus_brain import INTAKE, REPO, Routes, item


class UntrustedAuthorTriageTests(unittest.TestCase):
    def test_ready_issue_from_an_untrusted_author_stays_in_triage(self):
        routes = Routes([item(5, user={"login": "example-outsider"})])
        with mock.patch.dict(os.environ, {"LOOM_BRIEF_AUTHORS": "example-operator",
                                          "LOOM_TRUSTED_BRIEFERS": "example-briefer"}, clear=True), \
                mock.patch.object(brain, "intake_epic", return_value=INTAKE), \
                mock.patch.object(brain, "_epic_tickets", return_value=[]):
            lines = brain.triage(GitHub("t", transport=routes), REPO, 1)

        self.assertEqual(routes.writes(), [
            ("POST", "/repos/%s/issues/5/labels" % REPO, {"labels": ["loom:triage"]}),
        ])
        self.assertEqual(lines, ["#5 loom:triage, untrusted author"])
