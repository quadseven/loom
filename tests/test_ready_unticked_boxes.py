import json
import unittest
from unittest import mock

from bus import brain, merge_verdict
from bus.github import GitHub

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

    def test_status_passes_the_associated_ticket_to_the_ready_verdict(self):
        ticket = {"number": 10, "state": "open", "body": TICKET_BODY,
                  "labels": [{"name": "loom:ticket"}, {"name": "loom:pr"}]}
        pr = {"number": 11, "title": "Work", "body": PR_BODY_UNTICKED,
              "head": {"ref": "loom/10-work", "sha": "head"},
              "labels": [{"name": "class:logic"}]}
        with mock.patch.object(brain, "_status_pr_line") as status_line, \
                mock.patch.object(brain, "_stacked_base_blocker", return_value=""), \
                mock.patch.object(brain, "_pending_fix_blocker", side_effect=lambda *args: args[-1]), \
                mock.patch.object(brain, "_hold_blocker", return_value=""), \
                mock.patch.object(brain, "_goal_rule_line", side_effect=lambda *args: args[-1]):
            status_line.return_value = "- #11 Work - blocked: criteria not ticked"
            gh = GitHub("t", transport=lambda *args: (200, {}, json.dumps(ticket).encode()))
            brain._pull_request_line(gh, "example-owner/example-repo", pr,
                                     {"prs": {11: clean_node(PR_BODY_UNTICKED)}},
                                     BOT, (), "Grug - Elder", {}, None, ())
        self.assertEqual(status_line.call_args.kwargs["ticket"], ticket)

    def test_pull_request_with_code_ticked_and_only_live_box_open_reads_ready(self):
        ticket = {"number": 10, "body": TICKET_BODY}
        node = clean_node(body=PR_BODY_CODE_TICKED)
        verdict = merge_verdict.ready_verdict(node, BOT, ticket=ticket)
        self.assertTrue(verdict.ready)
        self.assertEqual(verdict.text, "READY")
        self.assertEqual(verdict.blockers, ())


if __name__ == "__main__":
    unittest.main()
