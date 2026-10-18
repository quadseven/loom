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

    def test_only_shipped_tickets_count_as_unproven(self):
        # A merged ticket waits on a deploy, not on a verifier (2026-10-17).
        card = goals.goal_context(self.rows("shipped", "merged", "verified", "queued"),
                                  [], {}, [], None, NOW)[REPO + "#30"]
        self.assertEqual(card["unproven"], 1)
        self.assertEqual(card["unproven_limit"], 30)

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
        self.assertIn("unproven work 1/30", body)
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


def goal_rows(*specs):
    """Ticket rows from (number, goal number, state) through ticket_record."""
    from dashboard.github import ticket_record
    return [ticket_record(REPO, {
        "number": number, "title": "Ticket %d" % number, "created_at": "2026-10-15T10:00:00Z",
        "body": "Part of %s#%d\n\n## Why\nx" % (REPO, goal),
        "labels": [{"name": "loom:ticket"}, {"name": "loom:" + state}]}) for number, goal, state in specs]


def goals_state(rows, context, error=""):
    class State:
        data = {}

        def response(self):
            return {"tickets": {REPO: rows}, "operator_items": [], "goal_context": context,
                    "goal_context_error": error}
    return State()


def cards(body):
    return re.findall(r'<article class="goal-card card">(.*?)</article>', body, re.S)


class RollUp(unittest.TestCase):
    """Small goals roll up into their parent goal (loom#609): one repo showed
    nine goal cards that read as fragments of one bigger goal."""

    def test_the_parent_goal_line_and_the_sub_issue_link_name_the_parent(self):
        self.assertEqual(goals.parent_of(REPO, {"body": "## Why\nx\n\nParent goal: example-owner/app#5\n"}),
                         "example-owner/app#5")
        self.assertEqual(goals.parent_of(REPO, {"body": "- Parent goal: #30"}), REPO + "#30")
        self.assertEqual(goals.parent_of(REPO, {"body": "", "parent_issue_url":
                                                "https://api.github.com/repos/%s/issues/30" % REPO}),
                         REPO + "#30")
        self.assertIsNone(goals.parent_of(REPO, {"body": "Part of #30"}))

    def test_a_goal_with_a_parent_goal_line_renders_as_a_row_inside_its_parents_card(self):
        rows = goal_rows((7, 30, "pr"), (8, 41, "running"), (9, 41, "queued"), (10, 41, "verified"))
        child = {"number": 41, "title": "Smaller goal", "body": "Parent goal: %s#30" % REPO}
        parents = {REPO + "#41": goals.parent_of(REPO, child)}
        comments = [comment(41, "<!-- loom:report v1 -->\nChild report.", "2026-10-15T09:00:00Z")]
        context = goals.goal_context(rows, comments, {REPO + "#30": "Main goal", REPO + "#41": "Smaller goal"},
                                     [], None, NOW, parents=parents)
        body = server._goals(goals_state(rows, context))
        found = cards(body)
        self.assertEqual(len(found), 1, "the smaller goal must not get its own card")
        self.assertIn("Main goal", found[0])
        children = found[0][found[0].index('class="goal-children"'):]
        self.assertIn("Smaller goal", children)
        self.assertIn("sample#41 / 2 in flight / report", children)
        self.assertIn('href="/tickets?goal=%s"' % server.quote(REPO + "#41", safe=""), children)

    def test_a_parent_with_no_tickets_of_its_own_still_gets_the_card(self):
        rows = goal_rows((8, 41, "running"))
        context = goals.goal_context(rows, [], {REPO + "#30": "Main goal"}, [], None, NOW,
                                     parents={REPO + "#41": REPO + "#30"})
        found = cards(server._goals(goals_state(rows, context)))
        self.assertEqual(len(found), 1)
        self.assertIn("Main goal", found[0])
        self.assertIn("goal-children", found[0])

    def test_a_parent_loop_still_shows_each_goal_once(self):
        tree = goals.rollup(["a#1", "a#2"], {"a#1": "a#2", "a#2": "a#1"})
        self.assertEqual(sum(1 + len(children) for children in tree.values()), 2)


class ReportFreshness(unittest.TestCase):
    QUIET_TEXT = "No report on this goal in the last two weeks"

    def render(self, comments, unread=None, now=None):
        rows = goal_rows((7, 30, "pr"))
        context = goals.goal_context(rows, comments, {}, [], None, now or NOW, unread=unread)
        return cards(server._goals(goals_state(rows, context)))[0]

    def test_a_goal_with_a_standup_in_the_last_two_hours_has_no_quiet_line(self):
        recent = datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
        card = self.render([comment(30, "<!-- loom:report v1 -->\n15 in flight since the last report.", recent)],
                           now=datetime.now(timezone.utc))
        self.assertNotIn(self.QUIET_TEXT, card)
        self.assertIn("15 in flight since the last report.", card)
        self.assertRegex(card, r"last report .*\(\d+m ago\)")

    def test_a_report_older_than_two_weeks_is_quiet_and_says_when_it_was(self):
        card = self.render([comment(30, "<!-- loom:report v1 -->\nOld.", "2026-09-20T10:00:00Z")])
        self.assertIn(self.QUIET_TEXT, card)
        self.assertIn("Last report Sep 20", card)

    def test_an_unread_comment_feed_names_its_cause_instead_of_no_report(self):
        card = self.render([], unread={REPO: REPO + " comments: GET ... -> 502"})
        self.assertNotIn(self.QUIET_TEXT, card)
        self.assertIn("report not read: %s comments: GET ... -&gt; 502" % REPO, card)
        self.assertIn("not read", card[card.index("goal-metric"):card.index("sparkline")])
        self.assertNotIn('<div class="metric">-</div>', card)

    def test_a_goal_missing_from_the_snapshot_shows_why_instead_of_a_dash(self):
        rows = goal_rows((7, 30, "pr"))
        card = cards(server._goals(goals_state(rows, {}, "the dashboard has not read its sources yet")))[0]
        self.assertNotIn('<div class="metric">-</div>', card)
        self.assertNotIn(self.QUIET_TEXT, card)
        self.assertIn("verified with proof: the dashboard has not read its sources yet", card)
        self.assertIn("report not read: the dashboard has not read its sources yet", card)


class FakeGitHub:
    """Just enough of bus.github.GitHub for one TicketStore pass."""

    def __init__(self, pages, issues):
        self.pages, self.issues, self.calls = pages, issues, []
        self.rate = {"core": (4000, 5000, 0)}

    def paginate(self, path):
        self.calls.append(path)
        for prefix, items in self.pages.items():
            if path.startswith(prefix):
                return list(items)
        return []

    def paginate_parallel(self, path, workers=8):
        return self.paginate(path)

    def rest(self, method, path, body=None):
        self.calls.append(path)

        class Response:
            data = self.issues.get(path)
        return Response()


class StoreGoals(unittest.TestCase):
    """The live cause (2026-10-17): a refresh below the request budget floor
    dropped goal_context, so every Goals card read "-", "the goal has no
    snapshot data" and "No report on this goal in the last two weeks", even
    for a goal with a standup every two hours."""

    def setUp(self):
        env = {key: "" for key in ("LOOM_IMPROVEMENT_HOMES", "LOOM_STATUS_REPO", "LOOM_METRICS_GOAL",
                                   "LOOM_BRIEF_AUTHORS", "LOOM_BACKLOG_SOURCE")}
        patcher = unittest.mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        from dashboard.github import TicketStore
        stamp = datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
        tickets = [{"number": n, "title": "Ticket %d" % n, "state": "open", "created_at": stamp,
                    "body": "Part of #%d\n\n## Why\nx" % goal,
                    "labels": [{"name": "loom:ticket"}, {"name": "loom:running"}]}
                   for n, goal in ((7, 30), (8, 41))]
        standup = {"id": 1, "body": "<!-- loom:report v1 -->\n1 in flight since the last report.",
                   "created_at": stamp, "user": {"login": "station"},
                   "issue_url": "https://api.github.com/repos/%s/issues/30" % REPO}
        self.gh = FakeGitHub(
            {"/repos/%s/issues?labels=loom%%3Aticket" % REPO: tickets,
             "/repos/%s/issues/comments" % REPO: [standup]},
            {"/repos/%s/issues/30" % REPO: {"number": 30, "title": "Main goal", "body": "## Why\nx"},
             "/repos/%s/issues/41" % REPO: {"number": 41, "title": "Smaller goal",
                                            "body": "## Why\nx\n\nParent goal: %s#30" % REPO}})
        self.store = TicketStore([REPO], gh=self.gh, budget_floor=500)
        self.store._waits_refreshed_at = float("inf")

    def test_a_pass_below_the_budget_floor_keeps_the_goal_cards_of_the_last_pass(self):
        self.store.refresh()
        self.store._last_remaining, self.store._reset_at = 10, 2 ** 40
        self.store.refresh()
        found = cards(server._goals(self.store))
        self.assertEqual(len(found), 1)
        self.assertIn("Main goal", found[0])
        self.assertIn("1 in flight since the last report.", found[0])
        self.assertNotIn("No report on this goal in the last two weeks", found[0])
        self.assertIn("Smaller goal", found[0][found[0].index("goal-children"):])

    def test_a_first_pass_below_the_floor_says_why_the_cards_are_empty(self):
        self.store._last_remaining, self.store._reset_at = 10, 2 ** 40
        self.store.refresh()
        self.assertIn("request budget is below its floor", self.store.response()["goal_context_error"])


class GoalSourcesAreTrusted(unittest.TestCase):
    """Only configured authors and configured repos shape the goal cards."""

    def store(self, home_comments=(), goal_41_body="## Why\nx", extra_issues=None, env=None):
        base = {key: "" for key in ("LOOM_IMPROVEMENT_HOMES", "LOOM_STATUS_REPO", "LOOM_METRICS_GOAL",
                                    "LOOM_BRIEF_AUTHORS", "LOOM_BACKLOG_SOURCE")}
        base.update(env or {})
        patcher = unittest.mock.patch.dict(os.environ, base)
        patcher.start()
        self.addCleanup(patcher.stop)
        from dashboard.github import TicketStore
        stamp = datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
        tickets = [{"number": n, "title": "Ticket %d" % n, "state": "open", "created_at": stamp,
                    "body": "Part of #%d\n\n## Why\nx" % goal,
                    "labels": [{"name": "loom:ticket"}, {"name": "loom:running"}]}
                   for n, goal in ((7, 30), (8, 41))]
        issues = {"/repos/%s/issues/30" % REPO: {"number": 30, "title": "Main goal", "body": "## Why\nx"},
                  "/repos/%s/issues/41" % REPO: {"number": 41, "title": "Smaller goal", "body": goal_41_body}}
        issues.update(extra_issues or {})
        gh = FakeGitHub({"/repos/%s/issues?labels=loom%%3Aticket" % REPO: tickets,
                         "/repos/%s/issues/30/comments" % REPO: list(home_comments)}, issues)
        store = TicketStore([REPO], gh=gh, budget_floor=500)
        store._waits_refreshed_at = float("inf")
        store.refresh()
        return store, gh

    @staticmethod
    def home_comment(cid, login, body, at):
        return {"id": cid, "user": {"login": login}, "body": body, "created_at": at,
                "issue_url": "https://api.github.com/repos/%s/issues/30" % REPO}

    def test_a_report_shaped_comment_from_an_untrusted_login_does_not_change_the_last_report(self):
        comments = [
            self.home_comment(1, "station", "<!-- loom:report v1 -->\nReal standup.", "2026-10-17T10:00:00Z"),
            self.home_comment(2, "stranger", "<!-- loom:report v1 -->\nForged standup.\n- Measure: forged",
                              "2026-10-17T11:00:00Z"),
            self.home_comment(3, "stranger", "<!-- loom:metrics v1 -->\n2026-10-17: 9/9 pass; streak 9",
                              "2026-10-17T11:00:00Z")]
        store, _ = self.store(comments, env={"LOOM_BRIEF_AUTHORS": "station",
                                             "LOOM_IMPROVEMENT_HOMES": REPO + "#30",
                                             "LOOM_METRICS_GOAL": REPO + "#30"})
        card = store.response()["goal_context"][REPO + "#30"]
        self.assertEqual(card["report"], "Real standup.")
        self.assertEqual(card["report_time"], "2026-10-17T10:00:00Z")
        self.assertIsNone(card["measure"])
        self.assertNotEqual(card["metric"], "9/9")
        self.assertNotIn("Forged standup.", server._goals(store))

    def test_an_untrusted_report_or_journal_changes_no_view(self):
        comments = [
            self.home_comment(1, "station", "<!-- loom:report v1 -->\nReal standup.", "2026-10-17T10:00:00Z"),
            self.home_comment(2, "station", "<!-- loom:journal v1 -->\nTick, brain\n- Next: real move",
                              "2026-10-17T10:00:00Z"),
            self.home_comment(3, "stranger", "<!-- loom:report v1 -->\nForged standup.", "2026-10-17T11:00:00Z"),
            self.home_comment(4, "stranger", "<!-- loom:journal v1 -->\nTick, brain\n- Next: forged move\n"
                              "- Friction: forged friction #7", "2026-10-17T11:00:00Z"),
            self.home_comment(5, "stranger", "<!-- loom:metrics v1 -->\n2026-10-17: 9/9 pass; streak 9",
                              "2026-10-17T11:00:00Z"),
            self.home_comment(6, "stranger", "<!-- loom:lanes v1 -->\nforged lanes", "2026-10-17T11:00:00Z")]
        store, _ = self.store(comments, env={"LOOM_BRIEF_AUTHORS": "station",
                                             "LOOM_IMPROVEMENT_HOMES": REPO + "#30",
                                             "LOOM_METRICS_GOAL": REPO + "#30"})
        payload = store.response()
        floor = server._floor(store)
        self.assertIn("Real standup.", floor)
        self.assertIn("real move", floor)
        self.assertNotIn("Forged", floor)
        self.assertNotIn("forged", floor)
        logins = {(c.get("user") or {}).get("login") for c in
                  payload["improving"]["journals"] + payload["improving"]["metrics_comments"] + payload["lanes"]}
        self.assertEqual(logins, {"station"})
        improving = views.render_improving(views.build_improvements(
            payload["improving"]["journals"], [], payload["improving"]["metrics_comments"]))
        self.assertNotIn("forged", improving)
        self.assertNotIn("9/9", improving)
        self.assertNotIn("Forged standup.", server._goals(store))

    def test_a_parent_line_naming_a_repo_outside_the_configured_list_is_ignored_and_never_read(self):
        store, gh = self.store(goal_41_body="Parent goal: other-owner/elsewhere#5")
        self.assertFalse([path for path in gh.calls if "other-owner/elsewhere" in path])
        self.assertIsNone(store.response()["goal_context"][REPO + "#41"]["parent"])
        self.assertEqual(len(cards(server._goals(store))), 2)

    def test_a_parent_that_is_not_a_goal_home_is_ignored(self):
        store, _ = self.store(goal_41_body="Parent goal: #50", extra_issues={
            "/repos/%s/issues/50" % REPO: {"number": 50, "title": "Plain issue", "body": "", "labels": []}})
        self.assertIsNone(store.response()["goal_context"][REPO + "#41"]["parent"])
        self.assertEqual(len(cards(server._goals(store))), 2)

    def test_a_parent_labelled_as_a_goal_home_is_followed(self):
        store, _ = self.store(goal_41_body="Parent goal: #50", extra_issues={
            "/repos/%s/issues/50" % REPO: {"number": 50, "title": "Big goal", "body": "",
                                           "labels": [{"name": "loom:goal"}]}})
        found = cards(server._goals(store))
        self.assertEqual(len(found), 2)
        big = next(card for card in found if "Big goal" in card)
        self.assertIn("Smaller goal", big[big.index("goal-children"):])


if __name__ == "__main__":
    unittest.main()


class EpicsOnGoals(unittest.TestCase):
    """loom#639: a ticket under an epic records the epic and the epic's goal,
    and Goals lists each goal's epics with their ticket counts."""

    def setUp(self):
        env = {key: "" for key in ("LOOM_IMPROVEMENT_HOMES", "LOOM_STATUS_REPO", "LOOM_METRICS_GOAL",
                                   "LOOM_BRIEF_AUTHORS", "LOOM_BACKLOG_SOURCE")}
        patcher = unittest.mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def store(self):
        from dashboard.github import TicketStore
        stamp = datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
        url = "https://api.github.com/repos/%s/issues/%d"

        def ticket(number, state, **extra):
            return dict({"number": number, "title": "Ticket %d" % number, "state": "open",
                         "created_at": stamp, "body": "## Why\nx",
                         "labels": [{"name": "loom:ticket"}, {"name": "loom:" + state}]}, **extra)
        tickets = [
            ticket(7, "running", parent_issue_url=url % (REPO, 40)),       # a sub-issue of epic 40
            ticket(8, "queued", body="## Why\nx\n\nPart of #40\n"),        # names epic 40
            ticket(9, "pr", body="## Why\nx\n\nPart of #41\n"),            # under closed epic 41
            ticket(10, "queued", body="## Why\nx\n\nPart of #30\n"),       # straight under the goal
        ]
        issues = {
            "/repos/%s/issues/30" % REPO: {"number": 30, "title": "Main goal", "body": "## Why\nx",
                                           "labels": [{"name": "loom:goal"}]},
            "/repos/%s/issues/40" % REPO: {"number": 40, "title": "Epic: the first chunk",
                                           "state": "open", "body": "Done when: x\n\nPart of #30\n",
                                           "labels": [{"name": "epic"}]},
            "/repos/%s/issues/41" % REPO: {"number": 41, "title": "Epic: an older chunk",
                                           "state": "closed", "body": "",
                                           "parent_issue_url": url % (REPO, 30),
                                           "labels": [{"name": "epic"}]},
        }
        gh = FakeGitHub({"/repos/%s/issues?labels=loom%%3Aticket" % REPO: tickets}, issues)
        store = TicketStore([REPO], gh=gh, budget_floor=500)
        store._waits_refreshed_at = float("inf")
        store.refresh()
        return store, gh

    def test_a_sub_issue_of_an_epic_records_the_epic_and_its_goal(self):
        store, _ = self.store()
        rows = {row["number"]: row for row in store.data[REPO]}
        self.assertEqual((rows[7]["epic"], rows[7]["goal"]), (REPO + "#40", REPO + "#30"))
        self.assertEqual((rows[8]["epic"], rows[8]["goal"]), (REPO + "#40", REPO + "#30"))
        self.assertEqual((rows[9]["epic"], rows[9]["goal"]), (REPO + "#41", REPO + "#30"))
        self.assertEqual((rows[10]["epic"], rows[10]["goal"]), (None, REPO + "#30"))

    def test_goals_lists_each_epic_under_its_goal_with_its_ticket_count(self):
        store, _ = self.store()
        found = cards(server._goals(store))
        self.assertEqual(len(found), 1, "an epic is not a goal card of its own")
        card = found[0]
        self.assertIn("Main goal", card)
        self.assertIn("open epics 1/6", card)
        epics = card[card.index('class="goal-epics"'):]
        self.assertIn("Epic: the first chunk", epics)
        self.assertIn("sample#40 / tickets 2/15 / 2 in flight", epics)
        self.assertIn('href="/tickets?epic=%s"' % server.quote(REPO + "#40", safe=""), epics)
        closed = epics[epics.index("goal-epics-closed"):]
        self.assertIn("1 closed epic", closed)
        self.assertIn("Epic: an older chunk", closed)
        self.assertIn("4 tickets on this goal", card)

    def test_the_tickets_page_filters_by_epic_and_the_header_names_epic_and_goal(self):
        from dashboard import ticket, tickets_table
        store, _ = self.store()
        rows = store.data[REPO]
        body = tickets_table.render(rows, {"epic": [REPO + "#40"]}, goal_titles={}, entered={},
                                    now=0, ticket_href=lambda row: "/t", short_ref=server._short_ref)
        self.assertIn("epic sample#40", body)
        self.assertIn("Ticket 7", body)
        self.assertIn("Ticket 8", body)
        self.assertNotIn("Ticket 9", body)
        self.assertNotIn("Ticket 10", body)
        self.assertIn("Filter by epic", body)
        record = next(row for row in rows if row["number"] == 7)
        header = ticket._header(record, [])
        self.assertIn(" / epic sample#40 / goal sample#30", header)
