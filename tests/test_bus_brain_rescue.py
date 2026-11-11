"""A goal at its open epic cap still gets its tickets out of a closed epic.

Tickets under a closed epic move into an open intake epic. When the goal is
at GOAL_EPIC_CAP open epics and its intake epics are full, the rescue opens
further intake epics past the cap for them while stranded closed-epic tickets
remain (at most ceil(stranded / 15) per pass).
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


CLOSED_URL = "https://api.github.com/repos/%s/issues/20" % REPO


def add_epic(fake, number, title, state="open", tickets=()):
    """An epic of goal #1 holding the given ticket numbers as sub-issues."""
    fake.issues[number] = dict(item(number, body="Part of #1\n", labels=("epic",),
                                    state=state), title=title)
    fake.subs[1].append(number)
    fake.subs[number] = list(tickets)


def add_ticket(fake, number, epic, state="open"):
    """An open ticket under the epic by sub-issue link and Part of line."""
    url = "https://api.github.com/repos/%s/issues/%d" % (REPO, epic)
    fake.issues[number] = item(number, body="## Why\nwhy\n\nPart of #%d\n" % epic,
                               labels=("loom:ticket",), state=state,
                               created_at="2030-01-%02dT00:00:00Z" % (number % 28 + 1),
                               parent_issue_url=url)
    return number


class ClosedEpicRescueTests(unittest.TestCase):

    def cap_repo(self, open_epics=None, intake_full=True):
        """Goal #1 at GOAL_EPIC_CAP open epics: intake epic #60 (full when
        intake_full), the rest plain open epics, and closed epic #20 holding
        two open tickets #21 and #22."""
        fake = OutsideEpicRepo()
        if open_epics is None:
            open_epics = brain.GOAL_EPIC_CAP - 1
        held = [add_ticket(fake, 600 + n, 60) for n in range(brain.EPIC_TICKET_CAP)] \
            if intake_full else []
        add_epic(fake, 60, brain.INTAKE_TITLE, tickets=held)
        for n in range(open_epics):
            add_epic(fake, 31 + n, "Epic: the work %d" % (31 + n))
        add_epic(fake, 20, "Epic: closed work", state="closed",
                 tickets=[add_ticket(fake, 21, 20), add_ticket(fake, 22, 20)])
        fake.open_tickets = [fake.issues[21], fake.issues[22]]
        return fake

    def move(self, fake):
        return brain.move_outside_tickets(GitHub("t", transport=fake), REPO, 1)

    def test_tickets_under_a_closed_epic_move_under_a_new_intake_epic_at_the_cap(self):
        fake = self.cap_repo()
        before = set(fake.issues)
        self.assertEqual(self.move(fake), (2, 0))
        opened = [n for n in fake.subs[1] if n not in before and n != 60]
        self.assertEqual(len(opened), 1)
        self.assertTrue(brain.is_intake_epic(fake.issues[opened[0]]))
        self.assertEqual(sorted(fake.subs[opened[0]]), [21, 22])
        self.assertNotIn(21, fake.subs[20])
        self.assertNotIn(22, fake.subs[20])
        self.assertEqual(fake.issues[20]["state"], "closed")

    def test_the_rescue_epic_is_open_and_its_tickets_name_it(self):
        fake = self.cap_repo()
        before = set(fake.issues)
        self.move(fake)
        made = [n for n in fake.subs[1] if n not in before and n != 60][0]
        self.assertEqual(fake.issues[made]["state"], "open")
        self.assertEqual(fake.issues[21]["body"], "## Why\nwhy\n\nPart of #%d\n" % made)
        self.assertEqual(fake.issues[22]["body"], "## Why\nwhy\n\nPart of #%d\n" % made)

    def test_the_rescue_opens_a_second_epic_when_the_first_rescue_epic_is_full(self):
        # Seven open epics: one past the cap already, its intake epic full.
        # Two stranded tickets open one further rescue epic, not two.
        fake = self.cap_repo(open_epics=brain.GOAL_EPIC_CAP)
        before = set(fake.issues)
        self.assertEqual(self.move(fake), (2, 0))
        opened = [n for n in fake.subs[1] if n not in before and n != 60]
        self.assertEqual(len(opened), 1)
        self.assertEqual(sorted(fake.subs[opened[0]]), [21, 22])
        self.assertEqual([c for c in fake.writes() if c[1].endswith("/sub_issues")], [
            ("POST", "/repos/%s/issues/1/sub_issues" % REPO, {"sub_issue_id": 1000 + opened[0]}),
            ("POST", "/repos/%s/issues/%d/sub_issues" % (REPO, opened[0]),
             {"sub_issue_id": 1021, "replace_parent": True}),
            ("POST", "/repos/%s/issues/%d/sub_issues" % (REPO, opened[0]),
             {"sub_issue_id": 1022, "replace_parent": True}),
        ])

    test_the_rescue_does_not_open_a_second_epic_past_the_bound = (
        test_the_rescue_opens_a_second_epic_when_the_first_rescue_epic_is_full
    )

    def test_a_loose_ticket_ahead_of_a_rescue_ticket_does_not_strand_it(self):
        fake = self.cap_repo()
        loose = item(10, body="## Why\nwhy\n\nPart of #1\n", labels=("loom:ticket",),
                     created_at="2029-01-01T00:00:00Z",
                     parent_issue_url="https://api.github.com/repos/%s/issues/1" % REPO)
        fake.issues[10] = loose
        fake.subs[1].append(10)
        fake.open_tickets = [loose, fake.issues[21], fake.issues[22]]
        self.assertEqual(self.move(fake), (2, 1))
        self.assertIn(10, fake.subs[1])
        opened = [n for n in fake.subs[1] if n not in (1, 10, 20, 31, 32, 33, 34, 35, 60)]
        self.assertEqual(len(opened), 1)
        self.assertEqual(sorted(fake.subs[opened[0]]), [21, 22])

    def test_a_goal_at_the_cap_with_room_in_its_intake_epic_opens_nothing(self):
        fake = self.cap_repo(intake_full=False)
        before = set(fake.issues)
        self.assertEqual(self.move(fake), (2, 0))
        self.assertEqual({n for n in fake.subs[1] if n not in before}, set())
        self.assertEqual(sorted(fake.subs[60]), [21, 22])


if __name__ == "__main__":
    unittest.main()
