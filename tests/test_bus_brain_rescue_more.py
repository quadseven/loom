"""More rescue tests for closed-epic tickets when rescue epics are full.

A goal at its open epic cap with all intake epics full (including its existing
rescue epics) opens further rescue intake epics while stranded closed-epic
tickets remain, at most ceil(stranded / 15) per pass (loom#2993).
"""

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


class ClosedEpicRescueMoreTests(unittest.TestCase):

    def full_epics_repo(self):
        """Goal #1 at open-epic cap (5 plain epics + 1 intake epic #60 full)
        plus one full rescue epic #61 open. Total 7 open epics, all intake
        epics full."""
        fake = OutsideEpicRepo()
        # Intake epic #60 is full with 15 tickets
        held60 = [add_ticket(fake, 600 + n, 60) for n in range(brain.EPIC_TICKET_CAP)]
        add_epic(fake, 60, brain.INTAKE_TITLE, tickets=held60)
        # Five plain open epics: total 6 open epics (at open-epic cap)
        for n in range(brain.GOAL_EPIC_CAP - 1):
            add_epic(fake, 31 + n, "Epic: the work %d" % (31 + n))
        # One full rescue epic #61 open (15 tickets)
        held61 = [add_ticket(fake, 620 + n, 61) for n in range(brain.EPIC_TICKET_CAP)]
        add_epic(fake, 61, brain.INTAKE_TITLE + " (batch 2)", tickets=held61)
        return fake

    def move(self, fake):
        return brain.move_outside_tickets(GitHub("t", transport=fake), REPO, 1)

    def test_stranded_tickets_move_into_a_second_rescue_epic_when_all_intake_full(self):
        fake = self.full_epics_repo()
        # Closed epic #20 holding 20 open tickets
        closed_tickets = [add_ticket(fake, 700 + n, 20) for n in range(20)]
        add_epic(fake, 20, "Epic: closed work", state="closed", tickets=closed_tickets)
        fake.open_tickets = [fake.issues[700 + n] for n in range(20)]

        before = set(fake.issues)
        self.assertEqual(self.move(fake), (20, 0))
        opened = [n for n in fake.subs[1] if n not in before and n not in (60, 61)]
        self.assertEqual(len(opened), 2)
        self.assertTrue(all(brain.is_intake_epic(fake.issues[o]) for o in opened))
        placed = fake.subs[opened[0]] + fake.subs[opened[1]]
        self.assertEqual(sorted(placed), list(range(700, 720)))
        self.assertEqual(fake.subs[20], [])
        self.assertEqual(fake.issues[20]["state"], "closed")

    def test_loose_tickets_open_no_epic_past_the_cap_and_none_move(self):
        fake = self.full_epics_repo()
        # Only loose tickets under goal #1 (no closed-epic tickets)
        goal_url = "https://api.github.com/repos/%s/issues/1" % REPO
        loose = [item(800 + n, body="## Why\nwhy\n\nPart of #1\n",
                      labels=("loom:ticket",),
                      created_at="2030-01-%02dT00:00:00Z" % (n % 28 + 1),
                      parent_issue_url=goal_url) for n in range(20)]
        for t in loose:
            fake.issues[t["number"]] = t
            fake.subs[1].append(t["number"])
        fake.open_tickets = loose

        before = set(fake.issues)
        self.assertEqual(self.move(fake), (0, 20))
        opened = [n for n in fake.subs[1] if n not in before]
        self.assertEqual(opened, [])
        self.assertEqual([c for c in fake.writes() if c[1].endswith("/sub_issues")], [])

    def test_goal_at_cap_with_room_in_intake_opens_area_epic_for_stranded_area_ticket(self):
        # Goal at open-epic cap (6 epics):
        # - intake epic #60 has room (0 tickets)
        # - area epic #70 for "stability" is full (15 tickets)
        # - 4 plain open epics
        # Closed epic #20 holds stranded area ticket #21 (area:stability).
        fake = OutsideEpicRepo()
        fake.labels = [{"name": "area:stability", "description": "Service stability"}]
        add_epic(fake, 60, brain.INTAKE_TITLE, tickets=[])
        held_area = [add_ticket(fake, 700 + n, 70) for n in range(brain.EPIC_TICKET_CAP)]
        add_epic(fake, 70, "Epic: stability", tickets=held_area)
        fake.issues[70]["labels"].append({"name": "area:stability"})
        for n in range(brain.GOAL_EPIC_CAP - 2):
            add_epic(fake, 31 + n, "Epic: plain %d" % (31 + n))

        add_epic(fake, 20, "Epic: closed work", state="closed",
                 tickets=[add_ticket(fake, 21, 20)])
        fake.issues[21]["labels"].append({"name": "area:stability"})
        fake.open_tickets = [fake.issues[21]]

        before = set(fake.issues)
        self.assertEqual(self.move(fake), (1, 0))
        opened = [n for n in fake.subs[1] if n not in before and n != 60]
        self.assertEqual(len(opened), 1)
        self.assertIn("area:stability", [l["name"] for l in fake.issues[opened[0]]["labels"]])
        self.assertEqual(fake.subs[opened[0]], [21])

    def test_refused_link_opens_no_further_epic_on_second_pass(self):
        fake = self.full_epics_repo()
        add_epic(fake, 20, "Epic: closed work", state="closed",
                 tickets=[add_ticket(fake, 721, 20)])
        fake.open_tickets = [fake.issues[721]]

        before_pass1 = set(fake.issues)
        with unittest.mock.patch.object(brain, "_move_to_epic", return_value=False):
            self.assertEqual(self.move(fake), (0, 1))
            opened_pass1 = [n for n in fake.subs[1] if n not in before_pass1 and n not in (60, 61)]
            self.assertEqual(len(opened_pass1), 1)

            before_pass2 = set(fake.issues)
            self.assertEqual(self.move(fake), (0, 1))
            opened_pass2 = [n for n in fake.subs[1] if n not in before_pass2]
            self.assertEqual(opened_pass2, [])

    def test_more_than_seventy_five_stranded_tickets_open_more_than_five_rescue_epics(self):
        # 80 stranded tickets under closed epic #20 with existing epics full:
        # opens 6 rescue epics (ceil(80 / 15) = 6), not capped at 5.
        fake = self.full_epics_repo()
        closed_tickets = [add_ticket(fake, 700 + n, 20) for n in range(80)]
        add_epic(fake, 20, "Epic: closed work", state="closed", tickets=closed_tickets)
        fake.open_tickets = [fake.issues[700 + n] for n in range(80)]

        before = set(fake.issues)
        self.assertEqual(self.move(fake), (80, 0))
        opened = [n for n in fake.subs[1] if n not in before and n not in (60, 61)]
        self.assertEqual(len(opened), 6)
        self.assertTrue(all(brain.is_intake_epic(fake.issues[o]) for o in opened))
        placed = sum((fake.subs[o] for o in opened), [])
        self.assertEqual(sorted(placed), list(range(700, 780)))
        self.assertEqual(fake.subs[20], [])
        self.assertEqual(fake.issues[20]["state"], "closed")


if __name__ == "__main__":
    unittest.main()
