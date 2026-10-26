"""The Needs-you page lists every open item that waits on a person
(loom#810): needs-operator asks and needs:<login> items, each once, with who
it waits on, the ask (from the body or from a comment) and a per-person
filter."""
import json
import os
import unittest
from unittest import mock

from dashboard import needs_you, server
from dashboard.github import OPERATOR_CLAIMS_SECONDS, TicketStore
from tests.test_dashboard_operators_page import ENV, FakeGitHub, _issue

REPO = "example-owner/app"
OWNER = "repo-owner"
OPERATOR = "second-operator"
LOOM = "loom-bot"


def _fixture():
    """An app repo with: #40 waiting on OWNER (its ask in the newest comment
    naming the label, then a later unrelated comment), #41 waiting on
    OPERATOR (ask in the body), #42 a needs-operator issue whose ask is only
    in a comment, and #43 labelled both needs-operator and needs:OWNER."""
    owner_ask = _issue(40, "Grant the deploy role", ["needs:" + OWNER], hours=60)
    owner_ask["comments"] = 3
    body_ask = _issue(41, "Rotate the webhook secret", ["needs:" + OPERATOR], hours=20)
    body_ask["body"] = "Context first.\n\n## Ask\n\nRotate the secret in the store.\n\n## Why\n\nIt leaked."
    body_ask["comments"] = 2
    operator_ask = _issue(42, "Approve the spend", ["needs-operator"], hours=10)
    operator_ask["body"] = "The lane ran out of credit.\nMore context."
    operator_ask["comments"] = 1
    both = _issue(43, "Restore the token", ["needs-operator", "needs:" + OWNER], hours=5)
    both["body"] = "## Ask\n\nMint a new token.\n"
    pages = {
        "/repos/%s/labels" % REPO: [{"name": "needs:" + OWNER}, {"name": "needs:" + OPERATOR},
                                    {"name": "needs-operator"}],
        "/repos/%s/issues?labels=needs%%3A%s&" % (REPO, OWNER): [owner_ask, both],
        "/repos/%s/issues?labels=needs%%3A%s&" % (REPO, OPERATOR): [body_ask],
        "/repos/%s/issues?labels=needs-operator&" % REPO: [operator_ask, both],
    }
    url = "https://github.com/%s/issues/%d#issuecomment-%d"
    rest = {
        "/repos/%s/issues/40/comments?per_page=100&page=1" % REPO: [
            {"body": "Opened for the deploy work.", "html_url": url % (REPO, 40, 1)},
            {"body": "<!-- ccqw:handoff v1 -->\nLabelled needs:%s.\n\n**Ask:** add the deploy role to the "
                     "runner account.\n\nDetail." % OWNER, "html_url": url % (REPO, 40, 2)},
            {"body": "Grug board: go.", "html_url": url % (REPO, 40, 3)}],
        "/repos/%s/issues/42/comments?per_page=100&page=1" % REPO: [
            {"body": "## Ask\n\nApprove 20 more credits for the lane.\n", "html_url": url % (REPO, 42, 9)}],
    }
    return FakeGitHub(pages, rest)


class NeedsYouPageTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, dict(ENV, LOOM_BRIEF_AUTHORS=LOOM))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.gh = _fixture()
        self.store = TicketStore([REPO], gh=self.gh, budget_floor=500)
        self.store._waits_refreshed_at = float("inf")
        self.store.refresh()

    def page(self, document=None, who=""):
        return server._needs_you(document or {}, self.store, who=who)

    def cards(self, body):
        return [part.split("</article>")[0] for part in body.split('<article class="card ask-card"')[1:]]

    def card(self, body, number):
        return next(card for card in self.cards(body) if "%s#%d<" % (REPO, number) in card
                    or "%s#%d -" % (REPO, number) in card)

    def test_needs_login_items_appear_marked_as_needing_a_human_with_who_they_wait_on(self):
        body = self.page()
        self.assertEqual(len(self.cards(body)), 4)
        owner = self.card(body, 40)
        self.assertIn("waits on %s" % OWNER, owner)
        self.assertIn("needs a human", owner)
        self.assertIn("Grant the deploy role", owner)
        self.assertIn("waits on %s" % OPERATOR, self.card(body, 41))
        self.assertIn("waits on the operator", self.card(body, 42))
        self.assertNotIn("Nothing needs you right now", body)

    def test_the_ask_is_the_body_section_else_the_comment_that_names_it_linked(self):
        body = self.page()
        # The newest comment that names the needs label, not the later bot
        # comment, and linked.
        owner = self.card(body, 40)
        self.assertIn("**Ask:** add the deploy role to the runner account.", owner)
        self.assertIn('href="https://github.com/%s/issues/40#issuecomment-2">from a comment</a>' % REPO, owner)
        self.assertNotIn("Grug board", owner)
        self.assertNotIn("No exact ask", owner)
        # A needs-operator issue with no "## Ask" in its body takes it from
        # its comment.
        operator = self.card(body, 42)
        self.assertIn("Approve 20 more credits for the lane.", operator)
        self.assertIn("issuecomment-9", operator)
        self.assertNotIn("No exact ask", operator)
        # A body "## Ask" section wins and needs no comment read.
        self.assertIn("<blockquote>Rotate the secret in the store.</blockquote>", self.card(body, 41))
        self.assertNotIn("/issues/41/comments", " ".join(self.gh.calls))
        self.assertNotIn("/issues/43/comments", " ".join(self.gh.calls))

    def test_an_item_waiting_on_two_shows_once_naming_both(self):
        body = self.page()
        self.assertEqual(body.count("%s#43 -" % REPO), 1)
        self.assertIn("waits on the operator, %s" % OWNER, self.card(body, 43))

    def test_bare_and_owner_qualified_refs_are_one_item(self):
        # Hunt's document names the repo bare; the store names it OWNER/NAME.
        document = {"needs_operator": [{"repo": "app", "number": 42, "title": "Approve the spend",
                                        "unblocks": 3, "html_url": "https://github.com/%s/issues/42" % REPO}],
                    "ready_for_human": [{"repo": "app", "number": 40, "title": "Grant the deploy role"},
                                        {"repo": "app", "number": 50, "title": "Epic: tidy up"}]}
        body = self.page(document)
        self.assertEqual(len(self.cards(body)), 4)
        self.assertNotIn("app#42 -", body.replace("%s#42 -" % REPO, ""))
        self.assertIn("unblocks 3", self.card(body, 42))
        self.assertIn("From the backlog (1)", body)
        self.assertNotIn(">app#40<", body)

    def test_filter_chips_count_each_login_and_the_operator_and_filter_by_query(self):
        from dashboard import needs_you
        body = self.page()
        chips = body.split('<nav class="chips needs-filter"')[1].split("</nav>")[0]
        self.assertIn('<a class="chip" href="/needs-you" aria-current="true">All <b>4</b></a>', chips)
        self.assertIn('<a class="chip" href="/needs-you?who=%s">%s <b>2</b></a>' % (OWNER, OWNER), chips)
        self.assertIn('<a class="chip" href="/needs-you?who=%s">%s <b>1</b></a>' % (OPERATOR, OPERATOR), chips)
        self.assertIn('<a class="chip" href="/needs-you?who=operator">Operator <b>2</b></a>', chips)
        self.assertLess(chips.index(OWNER), chips.index(OPERATOR))

        class Handler:
            def send_page(self, body, code=200):
                self.result = (code, body)
        handler = Handler()
        handler.path = "/needs-you?who=%s" % OWNER.upper()
        with mock.patch.object(server, "_backlog_bytes", return_value=json.dumps({}).encode()):
            server._dispatch_get(handler, self.store)
        code, filtered = handler.result
        self.assertEqual(code, 200)
        self.assertEqual(sorted(card.split(" -")[0].rsplit("#", 1)[-1] for card in
                                (c.split("<span class=\"ask-ref\">", 1)[1] for c in self.cards(filtered))), ["40", "43"])
        self.assertIn('href="/needs-you?who=%s" aria-current="true"' % OWNER, filtered)
        operator = self.page(who=needs_you.who_param("operator"))
        self.assertEqual(len(self.cards(operator)), 2)
        self.assertIn("waits on the operator", self.card(operator, 42))
        nobody = self.page(who=needs_you.who_param("someone-else"))
        self.assertEqual(self.cards(nobody), [])
        self.assertIn("Nothing waits on someone-else right now.", nobody)
        self.assertEqual(needs_you.who_param("bad login!"), "")

    def test_the_read_note_says_when_the_needs_items_were_read_once(self):
        self.store._snapshot["operator_claims"]["read_at"] -= 6 * 60 + 5
        note = self.page().split('<p class="machine-fact needs-read">')[1].split("</p>")[0]
        self.assertEqual(note, "needs:&lt;login&gt; items read 6 minutes ago, every 15 minutes.")

    def test_the_floor_counts_needs_login_items_too(self):
        floor = server._floor(self.store)
        card = floor.split('<span class="label">Needs you</span>')[1].split("</section>")[0]
        self.assertIn('<strong class="count">4</strong> asks', card)

    def test_unchanged_items_keep_their_comment_ask_without_a_read(self):
        self.gh.calls.clear()
        self.store._snapshot["operator_claims"]["read_at"] -= OPERATOR_CLAIMS_SECONDS
        self.store.refresh()
        self.assertNotIn("/comments?per_page=", " ".join(self.gh.calls))
        body = self.page()
        self.assertIn("Approve 20 more credits for the lane.", self.card(body, 42))
        self.assertIn("**Ask:** add the deploy role", self.card(body, 40))


class RefKeyTest(unittest.TestCase):
    def test_a_bare_name_maps_to_its_one_owner_and_an_ambiguous_one_stays_bare(self):
        from dashboard import needs_you
        names = needs_you.full_names(["one-owner/loom", "one-owner/grug", "other-owner/grug"])
        self.assertEqual(needs_you.ref_key("loom", 687, names), needs_you.ref_key("one-owner/loom", 687, names))
        self.assertNotEqual(needs_you.ref_key("grug", 1, names), needs_you.ref_key("one-owner/grug", 1, names))


class GoalCapsTest(NeedsYouPageTest):
    """The station's goals stuck at their in-flight cap (loom#1831)."""
    GOAL = {"goal": "%s#5" % REPO, "since": 0.0, "in_flight": 32, "cap": 28, "no_lane": 20}

    def post(self, body, auth="Bearer YOUR_UPLOAD_TOKEN_HERE"):
        import io
        from email.message import Message
        handler_type = server._handler_for(self.store)
        handler = handler_type.__new__(handler_type)
        handler.path = "/api/goal-caps"
        handler.headers = Message()
        raw = json.dumps(body).encode()
        handler.headers["Content-Length"] = str(len(raw))
        if auth:
            handler.headers["Authorization"] = auth
        handler.rfile = io.BytesIO(raw)
        result = []
        handler.send_json = lambda code, value: result.append((code, value))
        with mock.patch.dict(os.environ, {"LOOM_UPLOAD_TOKEN": "YOUR_UPLOAD_TOKEN_HERE"}):
            handler.do_POST()
        return result[0]

    def test_a_goal_stuck_at_its_cap_shows_with_its_no_lane_count(self):
        now = 10 * 3600.0
        with mock.patch.object(server.time, "time", return_value=now):
            self.assertEqual(self.post({"goals": [self.GOAL]}, auth="")[0], 401)
            self.assertEqual(self.post({"goals": [self.GOAL]}), (201, {"goals": 1}))
            body = self.page()
        self.assertIn("Goals stuck at their cap (1)", body)
        self.assertIn("%s#5</strong> stuck at its cap for 10 hours: 32 of 28 in "
                      "flight, 20 blocked on no lane." % REPO, body)

    def test_an_old_or_empty_report_shows_nothing(self):
        self.assertEqual(needs_you.goal_caps_section({"goals": [self.GOAL], "received_at": 0.0},
                                                     needs_you.GOAL_CAPS_MAX_AGE + 1), "")
        self.assertEqual(needs_you.goal_caps_section({"goals": [], "received_at": 0.0}, 1.0), "")
        self.assertEqual(needs_you.goal_caps_section(None, 1.0), "")

    def test_a_malformed_report_is_refused(self):
        for body in ({}, {"goals": "x"}, {"goals": [dict(self.GOAL, goal="<b>x</b>")]},
                     {"goals": [dict(self.GOAL, no_lane="3")]},
                     {"goals": [dict(self.GOAL, cap=True)]}):
            self.assertEqual(self.post(body)[0], 400, body)


if __name__ == "__main__":
    unittest.main()


class ExecutiveSummaryTest(NeedsYouPageTest):
    """loom#810 follow-up: the page reads as an executive summary. A headline
    says how much waits and on whom, a short list says where to start, and
    the items are grouped by the kind of action they need, oldest first."""

    def test_the_page_opens_with_a_headline_naming_the_counts_and_the_oldest_item(self):
        body = self.page()
        summary = body.split('<section class="needs-summary', 1)[1].split("</section>", 1)[0]
        self.assertIn("4 items wait on people", summary)
        self.assertIn("2 on %s" % OWNER, summary)
        self.assertIn("Oldest: %s#40" % REPO, summary)
        self.assertLess(body.index("needs-summary"), body.index('class="card ask-card"'))

    def test_start_here_lists_at_most_three_items(self):
        body = self.page()
        start = body.split('<ol class="needs-start">', 1)[1].split("</ol>", 1)[0]
        self.assertEqual(start.count("<li>"), 3)
        self.assertIn("%s#40" % REPO, start)

    def test_items_are_grouped_by_the_action_they_need_oldest_first(self):
        body = self.page()
        self.assertIn("Access and credentials (3)", body)
        self.assertIn("Decisions (1)", body)
        access = body.split("Access and credentials (3)", 1)[1].split("</section>", 1)[0]
        order = [access.index("%s#%d -" % (REPO, n)) for n in (40, 41, 43)]
        self.assertEqual(order, sorted(order))
        self.assertNotIn("%s#42 -" % REPO, access)

    def test_each_item_is_one_line_that_opens_to_the_whole_ask(self):
        card = needs_you.card({"repo": REPO, "number": 7, "title": "Long ask", "who": [OWNER],
                               "unblocks": 0, "body_ask": "Rotate the key. Then paste its id here.",
                               "comment_ask": None, "comment_url": ""})
        line, rest = card.split("</summary>", 1)
        self.assertIn("Long ask", line)
        self.assertIn("Rotate the key.", line)
        self.assertNotIn("paste its id", line)
        self.assertIn("<blockquote>Rotate the key. Then paste its id here.</blockquote>", rest)

    def test_start_here_names_the_longest_waiting_items(self):
        start = self.page().split('<ol class="needs-start">', 1)[1].split("</ol>", 1)[0]
        self.assertLess(start.index("%s#40" % REPO), start.index("%s#41" % REPO))

    def test_the_action_kind_comes_from_the_ask(self):
        kind = needs_you.action_kind
        self.assertEqual(kind({"body_ask": "Plug a monitor into the box and power it on."}), "device")
        self.assertEqual(kind({"body_ask": "Create an OAuth client and paste its id."}), "access")
        self.assertEqual(kind({"body_ask": "Reply yes or no."}), "decide")
        self.assertEqual(kind({"body_ask": "Merge PR 12 as is, or reply hold."}), "decide")
        self.assertEqual(kind({"body_ask": "", "comment_ask": ""}), "missing")
        self.assertEqual(kind({"body_ask": "Look at the log."}), "other")

    def test_the_lead_skips_the_lead_in_that_only_says_who_it_waits_on(self):
        lead = needs_you.first_sentence
        self.assertEqual(lead("**Needs the operator (one ask).** Merge PR 12 as is, or reply hold. More."),
                         "Merge PR 12 as is, or reply hold.")
        self.assertEqual(lead("Exact ask (owner): open Accounts and reconnect it. Then sync."),
                         "open Accounts and reconnect it.")
        self.assertEqual(lead("Ask (needs:ccqw): one open box is left."), "one open box is left.")
