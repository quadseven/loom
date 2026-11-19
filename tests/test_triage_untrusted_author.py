import os
import unittest
from unittest import mock

from bus import brain
from bus.github import GitHub
from test_bus_brain import BODY, INTAKE, REPO, Routes, item


class TriageUntrustedAuthorTest(unittest.TestCase):
    def test_ready_issue_from_untrusted_author_stays_in_triage(self):
        issue = item(5, body=BODY, user={"login": "example-outsider"})
        routes = Routes([issue])
        with mock.patch.dict(os.environ, {
            "LOOM_BRIEF_AUTHORS": "example-operator",
            "LOOM_TRUSTED_BRIEFERS": "example-briefer",
        }), mock.patch.object(brain, "intake_epic", return_value=INTAKE), \
                mock.patch.object(brain, "_epic_tickets", return_value=[]):
            lines = brain.triage(GitHub("t", transport=routes), REPO, 1)

        self.assertEqual(routes.writes(), [
            ("POST", "/repos/%s/issues/5/labels" % REPO, {"labels": ["loom:triage"]}),
        ])
        self.assertEqual(lines, ["#5 loom:triage, untrusted author"])
