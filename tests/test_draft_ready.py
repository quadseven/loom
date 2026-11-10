import unittest

from bus import brain, merge_verdict


BOT = "grug-tribe"


def ready_node():
    return {
        "commits": {"nodes": [{"commit": {
            "committedDate": "2030-01-01T12:45:00Z",
            "statusCheckRollup": {"state": "SUCCESS"},
        }}]},
        "reviews": {"nodes": []},
        "reviewThreads": {"totalCount": 0, "nodes": []},
        "isDraft": True,
    }


class DraftReadyTests(unittest.TestCase):
    def test_draft_pull_request_blocks_ready_verdict(self):
        verdict = merge_verdict.ready_verdict(ready_node(), BOT)

        self.assertFalse(verdict.ready)
        self.assertIn("draft", verdict.blockers)
        self.assertIn("lane", verdict.actors)

    def test_ready_fields_requests_is_draft(self):
        self.assertIn("isDraft", brain.READY_FIELDS)


if __name__ == "__main__":
    unittest.main()
