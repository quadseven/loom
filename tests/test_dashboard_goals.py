"""Goals and Floor read what each goal home says, fast (2026-10-15).

Live, every Goals card read "not looked", the Floor's Since last report read
"section missing" four times, and every page took 8 to 12 seconds: these pin
the readers and the one-pass footer index that fixed them.
"""
import os
import re
import unittest
import unittest.mock
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


class LastTick(unittest.TestCase):
    def render(self, journal):
        from dashboard.github import _floor_context

        class State:
            context = _floor_context([], [{"body": journal, "created_at": "2026-10-15T06:19:48Z"}], [], [])
            data = {}

            def response(self):
                return {"tickets": {}, "operator_items": []}
        return server._floor(State())

    def test_a_journal_without_a_next_line_shows_its_lead_as_the_last_tick(self):
        body = self.render("<!-- loom:journal v1 -->\nBrain tick journal, lane machine.\n\n"
                           "- Merged the stray-file fix once checks were green.\n- Deferred meow-now.")
        self.assertIn("Brain, last tick", body)
        self.assertIn("Merged the stray-file fix once checks were green.", body)
        self.assertNotIn("no goal journal found", body)

    def test_a_next_line_is_still_the_next_move(self):
        body = self.render("<!-- loom:journal v1 -->\nTick, brain\n- Next: ship #5")
        self.assertIn("Brain&#x27;s next move", body)
        self.assertIn("ship #5", body)


class PrioritiesCard(unittest.TestCase):
    def render(self, rows):
        from dashboard.github import _floor_context, ticket_record

        class State:
            context = _floor_context([], [], [], [])
            data = {}

            def response(self):
                return {"tickets": {REPO: [ticket_record(REPO, r) for r in rows]},
                        "operator_items": [], "footer_models": {}}
        return server._floor(State())

    @staticmethod
    def issue(number, state, priority=None, created="2026-10-01T00:00:00Z", labels=()):
        return {"number": number, "title": "Ticket %d" % number, "created_at": created,
                "body": ("Priority: %s\n\n## Why\nx" % priority) if priority else "## Why\nx",
                "labels": [{"name": "loom:ticket"}, {"name": "loom:" + state}]
                + [{"name": x} for x in labels]}

    def test_the_floor_lists_waiting_tickets_in_the_order_lanes_take_them(self):
        body = self.render([
            self.issue(1, "queued", "P3"), self.issue(2, "briefed"),
            self.issue(3, "pr", "P0", "2026-10-09T00:00:00Z"),
            self.issue(4, "merged", "P0"),
            self.issue(5, "failed", "P3", labels=("loom:regression",)),
            self.issue(6, "running", "P1"), self.issue(7, "queued", "P2", "2026-10-02T00:00:00Z")])
        card = body[body.index('class="card priorities-card"'):]
        card = card[:card.index("</section>")]
        order = [int(n) for n in re.findall(r'<span class="muted">%s#(\d+)' % REPO, card)]
        self.assertEqual(order, [5, 3, 6, 2, 7])
        self.assertIn("P0</span>", card)
        self.assertNotIn("Ticket 4", card)

    def test_a_closed_ticket_is_never_waiting_or_moving(self):
        # Tickets are read with state=all: a closed revert still labelled
        # briefed sat at the top of the card (2026-10-15).
        closed = dict(self.issue(8, "briefed", labels=("loom:revert",)), state="closed")
        body = self.render([closed, self.issue(9, "queued", "P2")])
        card = body[body.index('class="card priorities-card"'):]
        card = card[:card.index("</section>")]
        self.assertNotIn("Ticket 8", card)
        self.assertIn("Ticket 9", card)
        moving = body[body.index("Moving now"):body.index("Moving now") + 4000]
        self.assertNotIn("Ticket 8", moving)

    def test_an_empty_floor_says_nothing_is_waiting(self):
        self.assertIn("Nothing is waiting.", self.render([self.issue(4, "verified")]))


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


class UnprovenWork(unittest.TestCase):
    def rows(self, *states):
        return [{"repo": REPO, "number": 7 + n, "goal": REPO + "#30", "state": state}
                for n, state in enumerate(states)]

    def test_only_merged_or_shipped_tickets_count_as_unproven(self):
        card = goals.goal_context(self.rows("shipped", "merged", "verified", "queued"),
                                  [], {}, [], None, NOW)[REPO + "#30"]
        self.assertEqual(card["unproven"], 2)
        self.assertEqual(card["unproven_limit"], 15)

    def test_a_goal_card_shows_the_count_against_the_limit(self):
        from dashboard.github import ticket_record

        def issue(number, state):
            return {"number": number, "title": "Ticket %d" % number,
                    "created_at": "2026-10-15T10:00:00Z",
                    "body": "Part of %s#30\n\n## Why\nx" % REPO,
                    "labels": [{"name": "loom:ticket"}, {"name": "loom:" + state}]}

        rows = [ticket_record(REPO, issue(7, "shipped")), ticket_record(REPO, issue(8, "verified"))]

        class State:
            context = {}
            data = {}

            def response(self):
                return {"tickets": {REPO: rows}, "operator_items": [],
                        "goal_context": goals.goal_context(rows, [], {}, [], None, NOW)}

        body = server._goals(State())
        self.assertIn("unproven work 1/15", body)
        self.assertNotIn("over limit", body)

    def test_an_over_limit_goal_card_says_no_new_build_tickets(self):
        from dashboard.github import ticket_record

        def issue(number):
            return {"number": number, "title": "Ticket %d" % number,
                    "created_at": "2026-10-15T10:00:00Z",
                    "body": "Part of %s#30\n\n## Why\nx" % REPO,
                    "labels": [{"name": "loom:ticket"}, {"name": "loom:shipped"}]}

        rows = [ticket_record(REPO, issue(7)), ticket_record(REPO, issue(8))]

        class State:
            context = {}
            data = {}

            def response(self):
                return {"tickets": {REPO: rows}, "operator_items": [],
                        "goal_context": goals.goal_context(rows, [], {}, [], None, NOW)}

        with unittest.mock.patch.dict(os.environ, {"LOOM_UNVERIFIED_LIMIT": "1"}):
            body = server._goals(State())
        self.assertIn("unproven work 2/1", body)
        self.assertIn("over limit: no new build tickets", body)


if __name__ == "__main__":
    unittest.main()
