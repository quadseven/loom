"""The Results view: done and failed over the last day, week and month."""

import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import results, server  # noqa: E402

NOW = datetime(2030, 1, 31, 12, 0, tzinfo=timezone.utc)
REPO = "example-owner/sample"


def pr(number, ref, merged=None, closed=None):
    return {"number": number, "head": {"ref": ref}, "base": {"repo": {"full_name": REPO}},
            "merged_at": merged, "closed_at": closed or merged}


def payload(**extra):
    data = {
        "pull_requests": [
            pr(1, "loom/1-a", merged="2030-01-31T08:00:00Z"),       # day
            pr(2, "loom/2-b", merged="2030-01-27T08:00:00Z"),       # week
            pr(3, "loom/3-c", merged="2030-01-10T08:00:00Z"),       # month
            pr(4, "feature/x", merged="2030-01-31T09:00:00Z"),      # day, not a lane
            pr(5, "loom/5-d", closed="2030-01-30T09:00:00Z"),       # closed unmerged, week
        ],
        "tickets": {REPO: [
            {"number": 10, "closed_at": "2030-01-31T07:00:00Z", "state_reason": "completed"},
            {"number": 11, "closed_at": "2030-01-25T07:00:00Z", "state_reason": "not_planned"},
            {"number": 12, "closed_at": None, "state_reason": None},
        ]},
        "issue_comments": [
            {"repo": REPO, "created_at": "2030-01-31T06:00:00Z", "body": "<!-- loom:proof v1 -->\nProof"},
            {"repo": REPO, "created_at": "2030-01-29T06:00:00Z",
             "body": "Lane codex failed: [work] validation failed"},
            {"repo": REPO, "created_at": "2030-01-29T06:00:00Z",
             "body": "Lane codex failed: [limit] out of credit"},
        ],
        "hot_spots_error": "",
    }
    data.update(extra)
    return data


def values(data, name):
    return next(row["values"] for row in data["rows"] if row["name"] == name)


class ResultsCounts(unittest.TestCase):
    def test_each_window_counts_its_own_events(self):
        data = results.build_results(payload(), NOW)
        self.assertEqual(values(data, "Lane pull requests merged"), {"day": 1, "week": 2, "month": 3})
        # Never lane and other work added up: one non-lane merge.
        self.assertEqual(values(data, "Pull requests merged outside the lanes"),
                         {"day": 1, "week": 1, "month": 1})
        self.assertEqual(values(data, "Tickets closed as done"), {"day": 1, "week": 1, "month": 1})
        self.assertEqual(values(data, "Tickets closed as not planned"), {"day": 0, "week": 1, "month": 1})
        self.assertEqual(values(data, "Lane pull requests closed unmerged"), {"day": 0, "week": 1, "month": 1})

    def test_comment_counts_stop_at_their_reach_and_skip_limits(self):
        data = results.build_results(payload(), NOW)
        self.assertEqual(values(data, "Live proofs posted"), {"day": 1, "week": 1, "month": None})
        # A limit failure says nothing about the work.
        self.assertEqual(values(data, "Lane runs failed at the work"), {"day": 0, "week": 1, "month": None})

    def test_unread_comments_say_not_looked_with_the_reason(self):
        data = results.build_results(payload(hot_spots_error="example-owner/sample comments: 502"), NOW)
        self.assertEqual(values(data, "Live proofs posted"), {"day": None, "week": None, "month": None})
        page = results.render_results(data)
        self.assertIn("not looked", page)
        self.assertIn("comments: 502", page)

    def test_the_week_by_repo(self):
        data = results.build_results(payload(), NOW)
        self.assertEqual(data["repos"], [(REPO, {"merged": 2, "failed": 1})])


class ResultsPage(unittest.TestCase):
    def test_the_page_is_routed_and_in_the_nav(self):
        sent = {}

        class Handler:
            path = "/results"

            def send_page(self, body):
                sent["body"] = body

        class State:
            def response(self):
                return dict(payload(), stale=False)

        server._dispatch_get(Handler(), State())
        self.assertIn("Done and failed", sent["body"])
        self.assertIn('href="/results"', sent["body"])


if __name__ == "__main__":
    unittest.main()
