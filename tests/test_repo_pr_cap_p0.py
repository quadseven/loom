"""A P0 ticket is claimed and assigned over its repo's open pull request cap."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bus.github import GitHub
from test_bus_station import AUTHORS, Base, BODY_WITH_CRITERIA, REPO, station

P0_BODY = "Priority: P0\n" + BODY_WITH_CRITERIA
P2_BODY = "Priority: P2\n" + BODY_WITH_CRITERIA
WAIT = "its repo is at its open pull request cap"


class P0OverCapTest(Base):
    def setUp(self):
        super().setUp()
        self.fake.default_body = BODY_WITH_CRITERIA

    def pulls(self, count):
        self.fake.pulls = [dict(number=100 + i, state="open", draft=False,
                                user={"login": AUTHORS[0]}, body="",
                                head={"ref": "loom/%d-work" % (100 + i)})
                           for i in range(count)]

    def claim(self):
        self.fake.add(12)
        runner = station.Station(GitHub("token", transport=self.fake), self.settings)
        return runner, runner._claim(REPO, "codex", self.fake.issues[12])

    def factory(self):
        runner = station.Station(GitHub("token", transport=self.fake), self.settings)
        return runner, runner._load_factory()

    def test_a_p0_ticket_is_claimed_over_the_cap(self):
        self.pulls(9)
        self.fake.default_body = P0_BODY
        runner, claimed = self.claim()
        self.assertTrue(claimed)
        self.assertNotIn("open pull requests (cap", str(self.fake.comments[12]))
        self.assertEqual(runner._gate_held(), [])

    def test_a_p2_ticket_is_still_held_over_the_cap(self):
        self.pulls(9)
        self.fake.default_body = P2_BODY
        runner, claimed = self.claim()
        self.assertFalse(claimed)
        self.assertIn("9 open pull requests (cap 8)", str(self.fake.comments[12]))

    def test_the_factory_gives_a_p0_ticket_over_the_cap_no_cap_wait(self):
        self.pulls(9)
        self.fake.add(12, state="queued", body=P0_BODY)
        _, counter = self.factory()
        counter._assign_queued_ticket(REPO, self.fake.issues[12])
        self.assertNotEqual(counter.waits.get("%s#12" % REPO), WAIT)

    def test_the_factory_still_makes_a_p2_ticket_over_the_cap_wait(self):
        self.pulls(9)
        self.fake.add(13, state="queued", body=P2_BODY)
        _, counter = self.factory()
        counter._assign_queued_ticket(REPO, self.fake.issues[13])
        self.assertEqual(counter.waits.get("%s#13" % REPO), WAIT)

if __name__ == "__main__":
    unittest.main()
