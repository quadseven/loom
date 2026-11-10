import unittest

from bus import merge_verdict

BOT = "grug-tribe"


def clean_node(body=""):
    return {
        "commits": {"nodes": [{"commit": {"committedDate": "2030-01-01T12:45:00Z",
                                          "statusCheckRollup": {"state": "SUCCESS"}}}]},
        "reviews": {"nodes": []},
        "reviewThreads": {"totalCount": 0, "nodes": []},
        "body": body,
    }


TICKET_BODY = """
## Acceptance criteria
- [ ] Build the new module in bus/sample.py
- [ ] Live: after the station deploys this, observe no failure over 24 hours
"""

PR_BODY_UNTICKED = """
## Acceptance criteria
- [ ] Build the new module in bus/sample.py
- [ ] Live: after the station deploys this, observe no failure over 24 hours
"""

PR_BODY_CODE_TICKED = """
## Acceptance criteria
- [x] Build the new module in bus/sample.py
- [ ] Live: after the station deploys this, observe no failure over 24 hours
"""


class ReadyUntickedBoxesTests(unittest.TestCase):
    def test_clean_lane_pull_request_leaving_code_box_unticked_is_blocked(self):
        ticket = {"number": 10, "body": TICKET_BODY}
        node = dict(clean_node(body=PR_BODY_UNTICKED), ticket=ticket)
        verdict = merge_verdict.ready_verdict(node, BOT)
        self.assertFalse(verdict.ready)
        self.assertTrue(verdict.text.startswith("blocked:"))
        self.assertIn("criteria not ticked", verdict.text)
        self.assertIn("criteria not ticked", verdict.blockers)
        self.assertEqual(verdict.actors, ("lane",))

    def test_pull_request_with_code_ticked_and_only_live_box_open_reads_ready(self):
        ticket = {"number": 10, "body": TICKET_BODY}
        node = clean_node(body=PR_BODY_CODE_TICKED)
        verdict = merge_verdict.ready_verdict(node, BOT, ticket=ticket)
        self.assertTrue(verdict.ready)
        self.assertEqual(verdict.text, "READY")
        self.assertEqual(verdict.blockers, ())


if __name__ == "__main__":
    unittest.main()
