"""The Needs-you page lists every open item that waits on a person
(loom#810): needs-operator asks and needs:<login> items, each once, with who
it waits on, the ask (from the body or from a comment) and a per-person
filter."""
import json
import os
import unittest
from unittest import mock

from dashboard import server
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
                                (c.split("<span>", 1)[1] for c in self.cards(filtered))), ["40", "43"])
        self.assertIn('href="/needs-you?who=%s" aria-current="true"' % OWNER, filtered)
        operator = self.page(who=needs_you.who_param("operator"))
        self.assertEqual(len(self.cards(operator)), 2)
        self.assertIn("waits on the operator", self.card(operator, 42))
        nobody = self.page(who=needs_you.who_param("someone-else"))
        self.assertEqual(self.cards(nobody), [])
        self.assertIn("Nothing waits on someone-else right now.", nobody)
        self.assertEqual(needs_you.who_param("bad login!"), "")

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


if __name__ == "__main__":
    unittest.main()
