from __future__ import annotations

import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain, proof  # noqa: E402
from bus.github import GitHub  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401
from test_bus_brain import (REPO, TICKET_BODY, Fake, on_main, record,  # noqa: E402
                            reply)

TRUSTED = "trusted-login"
ENV = {"LOOM_BRIEF_AUTHORS": TRUSTED}


def factory_issue(author=TRUSTED, body=TICKET_BODY):
    return {"state": "open", "body": body, "user": {"login": author},
            "labels": [{"name": "loom:factory"}, {"name": "loom:shipped"},
                       {"name": "awaiting-live-check"}]}


class VerifyFactoryIssueTests(unittest.TestCase):
    def move(self, issue, *tail):
        fake = Fake(reply(200, issue), *tail)
        with unittest.mock.patch.dict(os.environ, ENV):
            brain.move(GitHub("t", transport=fake), REPO, 9, "verified", record=record())
        return fake

    def test_a_trusted_factory_issue_is_verified_and_an_incident_or_stranger_is_not(self):
        fake = self.move(factory_issue(), *on_main(), reply(201, {}), reply(200, []))
        post, put = fake.calls[3], fake.calls[4]
        self.assertEqual(post[0], "POST")
        self.assertTrue(post[2]["body"].startswith(proof.PROOF_MARKER))
        self.assertIn("loom:verified", put[2]["labels"])
        self.assertNotIn("loom:shipped", put[2]["labels"])
        incident = factory_issue(body=TICKET_BODY + "\n" + brain.FACTORY_INCIDENT_MARKER)
        for issue in (incident, factory_issue(author="stranger")):
            with self.subTest(author=issue["user"]["login"]):
                with self.assertRaisesRegex(ValueError, "only a loom:ticket is verified"):
                    self.move(issue)


if __name__ == "__main__":
    unittest.main()
