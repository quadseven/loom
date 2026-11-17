import unittest
from unittest import mock

from bus import brain
from bus.github import GitHub
from test_bus_brain import BODY, REPO, Routes, item


class TriageUntrustedAuthorTest(unittest.TestCase):
    def test_ready_issue_from_untrusted_author_stays_in_triage(self):
        issue = item(5, body=BODY, user={"login": "outside-author"})
        routes = Routes([issue])
        with mock.patch.dict("os.environ", {"LOOM_BRIEF_AUTHORS": "trusted-author",
                                             "LOOM_TRUSTED_BRIEFERS": ""}), \
                mock.patch.object(brain, "intake_epic", return_value=3), \
                mock.patch.object(brain, "_epic_tickets", return_value=[]):
            lines = brain.triage(GitHub("t", transport=routes), REPO, 1)

        self.assertEqual(routes.labelled(), {5: ["loom:triage"]})
        self.assertFalse(any(path.endswith("/sub_issues") for _, path, _ in routes.writes()))
        self.assertEqual(lines, ["#5 loom:triage, untrusted author"])
