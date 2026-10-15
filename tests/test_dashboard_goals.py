"""Goals and Floor read what each goal home says, fast (2026-10-15).

Live, every Goals card read "not looked", the Floor's Since last report read
"section missing" four times, and every page took 8 to 12 seconds: these pin
the readers and the one-pass footer index that fixed them.
"""
import unittest
from datetime import datetime, timezone

from dashboard import goals, server, views

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
REPO = "example-owner/sample"


def comment(issue, body, at="2026-10-15T10:00:00Z"):
    return {"issue_url": "https://api.github.com/repos/%s/issues/%d" % (REPO, issue),
            "body": body, "created_at": at}


class Sections(unittest.TestCase):
    def test_brain_report_lines_are_read(self):
        body = ("<!-- loom:report v1 -->\nBetter.\n\nGoing well: two merged.\n"
                "Not going well: one failed.\nStill stuck since last report: #4.\nNext: ship #5.")
        self.assertEqual(goals.sections(body), {"well": "two merged.", "not well": "one failed.",
                                                "stuck": "#4.", "next": "ship #5."})

    def test_station_standup_headings_are_read(self):
        body = ("<!-- loom:report v1 -->\n3 verified.\n\n### Going well\nVerified: #1 One. Shipped: #2.\n\n"
                "### Not going well\nFailed: none.\n\n### Still stuck\nnone\n\n### Next\nIn flight: #3.\n\n"
                "### Needs you\nnothing")
        found = goals.sections(body)
        self.assertEqual(found["well"], "Verified: #1 One.")
        self.assertEqual(found["not well"], "Failed: none.")
        self.assertEqual(found["next"], "In flight: #3.")
        self.assertNotIn("needs you", found)


class GoalContext(unittest.TestCase):
    def setUp(self):
        self.rows = [{"repo": REPO, "number": 7, "goal": REPO + "#30"},
                     {"repo": REPO, "number": 8, "goal": REPO + "#30"}]

    def test_title_report_and_verified_days(self):
        comments = [comment(30, "<!-- loom:report v1 -->\nOlder.", "2026-10-14T10:00:00Z"),
                    comment(30, "<!-- loom:report v1 -->\nTwo tickets verified today.\n\nGoing well: yes."),
                    comment(7, "<!-- loom:proof v1 -->\nProof", "2026-10-15T09:00:00Z"),
                    comment(8, "<!-- loom:proof v1 -->\nProof", "2026-10-13T09:00:00Z"),
                    comment(8, "<!-- loom:proof v1 -->\nProof", "2026-10-05T09:00:00Z")]
        found = goals.goal_context(self.rows, comments, {REPO + "#30": "Ship it"}, [], None, NOW)
        card = found[REPO + "#30"]
        self.assertEqual(card["title"], "Ship it")
        self.assertEqual(card["report"], "Two tickets verified today.")
        self.assertEqual(card["history"], [0, 0, 0, 0, 1, 0, 1])
        self.assertEqual((card["metric"], card["previous"]), ("2", "1"))

    def test_the_metrics_goal_shows_the_factory_line(self):
        metrics = [{"body": "<!-- loom:metrics v1 -->\n2026-10-14: 4/9 pass; streak 0"},
                   {"body": "<!-- loom:metrics v1 -->\n2026-10-15: 5/9 pass; streak 0"}]
        card = goals.goal_context(self.rows, [], {}, metrics, REPO + "#30", NOW)[REPO + "#30"]
        self.assertEqual((card["metric"], card["previous"]), ("5/9", "4/9"))
        self.assertEqual(card["history"][-2:], [4, 5])

    def test_a_goal_with_no_report_says_so_instead_of_inventing_one(self):
        card = goals.goal_context(self.rows, [], {}, [], None, NOW)[REPO + "#30"]
        self.assertIsNone(card["report"])
        self.assertIsNone(card["title"])


class FooterIndex(unittest.TestCase):
    def test_each_ticket_gets_its_newest_footer_model_in_one_pass(self):
        prs = [{"body": "Loom ticket example-owner.sample-7, lane codex, model old.", "updated_at": "2026-10-01"},
               {"body": "Loom ticket example-owner.sample-7-r2, lane codex, model new.", "updated_at": "2026-10-02"},
               {"body": "no footer", "updated_at": "2026-10-03"}]
        self.assertEqual(views.footer_models(prs), {REPO + "#7": "new"})


class ApiTickets(unittest.TestCase):
    def test_raw_sources_are_served_as_counts(self):
        payload = {"tickets": {}, "pull_requests": [{"body": "x" * 1000}] * 3,
                   "issue_comments": [{}] * 5, "review_comments": [], "footer_models": {"a": "b"}}
        out = server._api_tickets(payload)
        for key in server.RAW_SOURCES:
            self.assertNotIn(key, out)
        self.assertEqual(out["source_counts"]["pull_requests"], 3)
        self.assertEqual(out["source_counts"]["issue_comments"], 5)


if __name__ == "__main__":
    unittest.main()
