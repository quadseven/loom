"""A missing run brief can be recovered from the ticket before the PR opens."""

import json
import unittest

from bus import hunt_plan, pr_repair
from tests import test_run_lane as runner


class MissingBriefBodyTest(unittest.TestCase):
    def setUp(self):
        self.lane = runner.LaneRunnerTest()
        self.lane.setUp()
        self.addCleanup(self.lane.tearDown)

    def run_without_brief(self, ticket_body=None):
        self.lane.drop_brief()
        brief = self.lane.inbox / "T042-fix-the-thing.md"
        ticket = self.lane.home / "ticket.json"
        if ticket_body is not None:
            ticket.write_text(json.dumps({"number": 42, "title": "Recover body",
                                          "body": ticket_body}))
        result = self.lane.run_lane(FAKE_REMOVE_BRIEF=str(brief), FAKE_GH_TICKET=str(ticket))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(brief.exists())
        return self.lane.body.read_text()

    def test_missing_brief_recovers_sections_from_ticket(self):
        self.lane.write_tool(self.lane.bin / "mktemp", runner.FAKE_SANDBOXED_MKTEMP)
        why = "Reviewers need the reason for this change."
        body = self.run_without_brief(
            f"## Why\n{why}\n\n## Acceptance criteria\n- [ ] The body opens\n\n"
            "## Size\nS\n\n## Out of scope\n- Other runners\n")
        self.assertIn(f"## Why\n{why}\n", body)
        self.assertIn("\nSize: S\n", body)
        self.assertIn("## Out of scope\n- Other runners\n", body)
        self.assertIn("Refs %s#%d" % ("example/repo", 42), body)
        self.assertEqual(hunt_plan.problems(body), [])
        self.assertEqual(self.lane.sticky_bodies(), [])

    def test_unreadable_ticket_logs_missing_brief_without_plan_hold(self):
        body = self.run_without_brief()
        self.assertRegex(self.lane.run_log(),
                         r"\[factory\].*brief file missing.*T042-fix-the-thing\.md")
        self.assertNotIn(pr_repair.PLAN_HOLD_MARKER, body)
        self.assertNotIn("The ticket lacks what the runner writes",
                         "\n".join(self.lane.all_comment_bodies()))

    def test_existing_brief_with_empty_why_posts_plan_hold(self):
        brief = runner.BRIEF.replace("The thing is broken for every user.", "")
        brief = brief.replace("Make the thing work.", "").replace("# T042: Fix the thing", "")
        self.lane.drop_brief(text=brief)
        result = self.lane.run_lane()
        self.assertEqual(result.returncode, 0, result.stderr)
        notes = self.lane.sticky_bodies()
        self.assertEqual(len(notes), 1)
        self.assertIn("The ticket lacks what the runner writes", notes[0])
        self.assertIn("## Why has 0 words", notes[0])
