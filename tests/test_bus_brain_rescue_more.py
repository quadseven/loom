"""Rescue intake capacity scales with tickets stranded under closed epics."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bus import brain  # noqa: E402
from bus.github import GitHub  # noqa: E402
from tests.shadow_env import setUpModule, tearDownModule  # noqa: E402,F401
from tests.test_bus_brain import REPO, OutsideEpicRepo, item  # noqa: E402
from tests.test_bus_brain_rescue import add_epic, add_ticket  # noqa: E402


class MoreClosedEpicRescueTests(unittest.TestCase):

    def capped_repo(self, stranded):
        fake = OutsideEpicRepo()
        for epic_number in range(60, 60 + brain.GOAL_EPIC_CAP):
            add_epic(fake, epic_number, brain.INTAKE_TITLE,
                     tickets=[add_ticket(fake, epic_number * 10 + n, epic_number)
                              for n in range(brain.EPIC_TICKET_CAP)])
        add_epic(fake, 70, brain.INTAKE_TITLE,
                 tickets=[add_ticket(fake, 700 + n, 70)
                          for n in range(brain.EPIC_TICKET_CAP)])
        add_epic(fake, 20, "Epic: closed work", state="closed",
                 tickets=[add_ticket(fake, 21 + n, 20) for n in range(stranded)])
        fake.open_tickets = [fake.issues[21 + n] for n in range(stranded)]
        return fake

    def move(self, fake):
        return brain.move_outside_tickets(GitHub("t", transport=fake), REPO, 1)

    def test_twenty_stranded_tickets_open_a_second_rescue_epic(self):
        fake = self.capped_repo(20)
        before = set(fake.issues)
        self.assertEqual(self.move(fake), (20, 0))
        opened = [n for n in fake.subs[1] if n not in before]
        self.assertEqual(len(opened), 2)
        self.assertEqual(sorted(fake.subs[opened[0]] + fake.subs[opened[1]]),
                         list(range(21, 41)))

    def test_loose_tickets_do_not_open_rescue_epics_past_the_cap(self):
        fake = self.capped_repo(0)
        loose = item(10, body="## Why\nwhy\n\nPart of #1\n", labels=("loom:ticket",),
                     parent_issue_url="https://api.github.com/repos/%s/issues/1" % REPO)
        fake.issues[10] = loose
        fake.subs[1].append(10)
        fake.open_tickets = [loose]
        before = set(fake.issues)
        self.assertEqual(self.move(fake), (0, 1))
        self.assertEqual({n for n in fake.subs[1] if n not in before}, set())


if __name__ == "__main__":
    unittest.main()
