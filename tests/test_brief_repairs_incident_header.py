"""A brief that names an open base-check incident of its repo carries the
Repairs incident header without --repairs-incident."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain, incident_probe  # noqa: E402
from bus.github import GitHub  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401
from test_bus_brain import BODY, EPIC, REPO, Fake, goal, reply  # noqa: E402

FACTORY = "example-owner/factory-repo"


def incident(repo=REPO, kind="base-check", state="open"):
    return {"number": 31, "state": state, "labels": [{"name": "loom:factory"}],
            "body": "%s\n\nRepo: %s\nKind: %s\nTicket: t\n" % (
                incident_probe.INCIDENT_MARKER, repo, kind)}


class BriefRepairsIncidentHeaderTests(unittest.TestCase):
    def _filed(self, found, **kwargs):
        fake = Fake(reply(200, {"private": True}), reply(200, goal()),
                    reply(200, found), reply(200, []),
                    reply(201, {"id": 202, "html_url": "https://example.com/t"}),
                    reply(201, {}))
        env = {"LOOM_SELF_REPO": FACTORY, "LOOM_HOME": self.home}
        body = BODY + "\nRefs %s#31\n" % FACTORY
        brain.brief(GitHub("t", transport=fake), REPO, EPIC, "Ticket", body, "codex",
                    difficulty="medium", env=env, **kwargs)
        return next(c[2] for c in fake.calls
                    if c[0] == "POST" and c[1].endswith("/issues"))["body"]

    def test_header_is_written_only_for_an_open_base_check_incident_of_the_repo(self):
        with tempfile.TemporaryDirectory() as home:
            self.home = home
            self.assertIn("Repairs incident: #31\n", self._filed(incident()))
            # Kept rules, asserted after the new case: a closed incident, one of
            # another repo and one of another kind add no header.
            for other in (incident(state="closed"), incident(repo="other/repo"),
                          incident(kind="decision")):
                self.assertNotIn("Repairs incident", self._filed(other))
            with self.assertRaisesRegex(ValueError, "--repairs-incident"):
                self._filed(incident(), repairs_incident=32)
            # A private repo's incident names it by the alias the lane host keeps.
            os.makedirs(os.path.join(home, "state", "incidents"))
            alias = os.path.join(home, "state", "incidents",
                                 REPO.replace("/", "_") + ".alias")
            with open(alias, "w", encoding="utf-8") as handle:
                handle.write("0a1b2c3d")
            self.assertIn("Repairs incident: #31\n",
                          self._filed(incident(repo="private repo 0a1b2c3d")))


if __name__ == "__main__":
    unittest.main()
