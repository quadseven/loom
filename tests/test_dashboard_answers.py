"""Answering a Needs-you card from the dashboard (loom#1777): the buttons a
card offers, the answer POST (who may answer, one comment, the labels), the
on-demand ask read and the answered-not-yet-applied state."""
import io
import json
import os
import unittest
from email.message import Message
from unittest import mock

from bus import answer, brain, operator_hold
from dashboard import answers, github as dashboard_github, server
from dashboard.github import TicketStore
from tests.test_dashboard_operators_page import ENV, FakeGitHub, _issue

REPO = "example-owner/app"
OWNER = "repo-owner"
LOOM = "loom-bot"
TOKEN = "answer-secret-for-tests"
URL = "https://github.com/%s/issues/%d#issuecomment-%d"


class _Resp:
    def __init__(self, data):
        self.data, self.headers = data, {}


class AnswerGitHub(FakeGitHub):
    """The operators page fake, as the Loom App, with writes recorded and
    applied: a posted comment is read back, a removed label is gone."""

    auth = "app"

    def __init__(self, pages, rest, issues):
        super().__init__(pages, rest)
        self.issues, self.writes, self.next_id = issues, [], 500

    def rest(self, method, path, body=None, ok=()):
        if method == "GET":
            number = path.rsplit("/", 1)[-1]
            if number.isdigit() and int(number) in self.issues:
                self.calls.append(path)
                return _Resp(self.issues[int(number)])
            return super().rest(method, path, body)
        self.writes.append((method, path, body))
        number = int(path.split("/issues/")[1].split("/")[0])
        issue = self.issues[number]
        if path.endswith("/comments"):
            self.next_id += 1
            made = {"id": self.next_id, "body": body["body"], "user": {"login": LOOM + "[bot]"},
                    "created_at": "2030-01-01T12:00:00Z", "html_url": URL % (REPO, number, self.next_id)}
            page = "/repos/%s/issues/%d/comments?per_page=100&page=1" % (REPO, number)
            self.rest_data[page] = list(self.rest_data.get(page) or []) + [made]
            issue["comments"] = int(issue.get("comments") or 0) + 1
            return _Resp(made)
        if method == "DELETE":
            gone = path.rsplit("/", 1)[-1].replace("%3A", ":")
            issue["labels"] = [x for x in issue["labels"] if x["name"] != gone]
        elif path.endswith("/labels"):
            issue["labels"] = issue["labels"] + [{"name": name} for name in body["labels"]]
        return _Resp({})


def _fixture():
    """#40 waits on OWNER, its ask in the body (yes or no); #42 is a
    loom:human ticket whose lane pull request #8 is held for the operator;
    #44 waits on OWNER with its ask in a comment."""
    yes_no = _issue(40, "Grant the deploy role", ["needs:" + OWNER], hours=50)
    yes_no["body"] = "## Ask\n\nAdd the deploy role to the runner account? Reply yes or no.\n"
    held = _issue(42, "Move the service", ["loom:ticket", "loom:pr", "loom:human"], hours=20)
    held["comments"] = 1
    later = _issue(44, "Pick the store", ["needs:" + OWNER], hours=10)
    later["comments"] = 1
    issues = {40: yes_no, 42: held, 44: later}
    pages = {
        "/repos/%s/labels" % REPO: [{"name": "needs:" + OWNER}, {"name": "loom:human"},
                                    {"name": answer.ANSWERED_LABEL}],
        "/repos/%s/issues?labels=needs%%3A%s&" % (REPO, OWNER): [yes_no, later],
        "/repos/%s/issues?labels=loom%%3Ahuman&" % REPO: [held],
    }
    # As bus.brain.human writes it: the ask, then its Kind line.
    kind, ask = brain.human_kind(operator_hold.ask(8, [operator_hold.GENERIC_WHY]))
    hold_ask = "%s\n%s\nKind: %s" % (brain.HUMAN_MARKER, ask, kind)
    rest = {
        "/repos/%s/issues/42/comments?per_page=100&page=1" % REPO: [
            {"id": 9, "body": hold_ask, "user": {"login": LOOM}, "created_at": "2030-01-01T09:00:00Z",
             "html_url": URL % (REPO, 42, 9)}],
        "/repos/%s/issues/44/comments?per_page=100&page=1" % REPO: [
            {"id": 11, "body": "Ask: which store holds the key? Reply a or b: (a) the vault (b) the "
                               "parameter store.", "user": {"login": LOOM},
             "created_at": "2030-01-01T09:30:00Z", "html_url": URL % (REPO, 44, 11)}],
    }
    return AnswerGitHub(pages, rest, issues)


class Call:
    """One request through the dashboard's handler."""

    def __init__(self, state):
        self.handler_type = server._handler_for(state)

    def __call__(self, method, path, body=None, headers=None):
        handler = self.handler_type.__new__(self.handler_type)
        handler.path = path
        handler.headers = Message()
        raw = b"" if body is None else json.dumps(body).encode()
        handler.headers["Content-Length"] = str(len(raw))
        for name, value in (headers or {}).items():
            handler.headers[name] = value
        handler.rfile = io.BytesIO(raw)
        result = []
        handler.send_json = lambda code, value: result.append((code, value))
        handler.send_text = lambda code, value: result.append((code, value))
        (handler.do_POST if method == "POST" else handler.do_GET)()
        return result[0]


class AnswerTest(unittest.TestCase):
    env = {"LOOM_BRIEF_AUTHORS": LOOM, "LOOM_OPERATORS": "%s second-operator" % OWNER,
           "LOOM_ANSWER_TOKEN": TOKEN, "LOOM_IDENTITY_HEADER": "", "LOOM_UPLOAD_TOKEN": ""}

    def setUp(self):
        patcher = mock.patch.dict(os.environ, dict(ENV, **self.env))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.gh = _fixture()
        self.store = TicketStore([REPO], gh=self.gh, budget_floor=500)
        self.store._waits_refreshed_at = float("inf")
        # The pass reads no ask comment, so #42 and #44 wait for one.
        with mock.patch.object(dashboard_github, "OPERATOR_ASK_READS", 0):
            self.store.refresh()
        self.call = Call(self.store)

    def page(self):
        return server._needs_you({}, self.store)

    def card(self, body, number):
        return next(part.split("</article>")[0] for part in body.split('<article class="card ask-card"')[1:]
                    if "%s#%d -" % (REPO, number) in part)

    def answer_it(self, number=40, choice="yes", note="", login=OWNER, token=TOKEN, headers=None):
        sent = dict(headers or {})
        if token:
            sent["Authorization"] = "Bearer " + token
        return self.call("POST", "/api/needs-you/answer",
                         {"repo": REPO, "number": number, "choice": choice, "note": note, "as": login}, sent)

    def comments_posted(self):
        return [body for method, path, body in self.gh.writes if method == "POST" and path.endswith("/comments")]

    def read_ask(self, number):
        return self.call("GET", "/api/needs-you/ask?repo=%s&number=%d" % (REPO, number))


class Buttons(AnswerTest):
    def test_each_card_offers_the_answers_its_ask_names_and_a_note(self):
        body = self.page()
        yes_no = self.card(body, 40)
        self.assertIn('value="Yes"', yes_no)
        self.assertIn('value="No"', yes_no)
        self.assertIn('name="note"', yes_no)
        self.assertIn("Answering from this browser", body)

    def test_a_loom_human_ticket_is_a_card_and_its_hold_ask_offers_merge_or_hold(self):
        self.assertIn("ask-missing", self.card(self.page(), 42))
        code, found = self.read_ask(42)
        self.assertEqual(code, 200)
        self.assertIn("Pull request #8 waits for you", found["ask"])
        self.assertIn('value="Merge"', found["card"])
        self.assertIn('value="Hold"', found["card"])
        self.assertIn('value="Merge"', self.card(self.page(), 42))

    def test_without_the_github_app_the_page_says_answers_need_it(self):
        self.gh.auth = "token"
        body = self.page()
        self.assertIn("needs the Loom GitHub App", self.card(body, 40))
        self.assertNotIn("<button", self.card(body, 40))
        code, _ = self.answer_it()
        self.assertEqual(code, 503)
        self.assertEqual(self.comments_posted(), [])


class Identity(AnswerTest):
    def test_a_missing_or_wrong_token_and_a_login_outside_the_operators_are_refused(self):
        for token, login, code in ((None, OWNER, 401), ("wrong", OWNER, 401), (TOKEN, "stranger", 403)):
            with self.subTest(token=token, login=login):
                self.assertEqual(self.answer_it(token=token, login=login)[0], code)
        self.assertEqual(self.gh.writes, [])

    def test_an_identity_header_maps_to_an_operator_and_anyone_else_is_refused(self):
        env = {"LOOM_IDENTITY_HEADER": "Tailscale-User-Login", "LOOM_OPERATORS": "owner@github=" + OWNER}
        with mock.patch.dict(os.environ, env):
            code, _ = self.answer_it(token=None, headers={"Tailscale-User-Login": "stranger@github"})
            self.assertEqual(code, 403)
            self.assertEqual(self.gh.writes, [])
            code, found = self.answer_it(token=None, headers={"Tailscale-User-Login": "owner@github"})
        self.assertEqual(code, 200, found)
        self.assertEqual(found["answer"]["login"], OWNER)


class Posting(AnswerTest):
    def test_an_operator_posts_exactly_one_comment_and_the_wait_labels_come_off(self):
        code, found = self.answer_it(note="the role is fine")
        self.assertEqual(code, 200, found)
        self.assertEqual(found["status"], "posted")
        self.assertEqual(self.comments_posted(), [
            {"body": "<!-- loom:answer v1 --> Answer from repo-owner (via the dashboard): Yes. the role is fine"}])
        self.assertIn(("DELETE", "/repos/%s/issues/40/labels/needs%%3A%s" % (REPO, OWNER), None), self.gh.writes)
        self.assertIn(("POST", "/repos/%s/issues/40/labels" % REPO, {"labels": [answer.ANSWERED_LABEL]}),
                      self.gh.writes)
        self.assertEqual([x["name"] for x in self.gh.issues[40]["labels"]], [answer.ANSWERED_LABEL])
        self.assertIn('data-state="pending"', found["card"])

    def test_a_loom_human_answer_takes_loom_human_off(self):
        self.read_ask(42)
        code, found = self.answer_it(number=42, choice="merge")
        self.assertEqual(code, 200, found)
        self.assertIn("loom:human", found["removed"])
        self.assertEqual([x["name"] for x in self.gh.issues[42]["labels"]],
                         ["loom:ticket", "loom:pr", answer.ANSWERED_LABEL])

    def test_a_second_click_posts_nothing_even_after_a_restart(self):
        self.assertEqual(self.answer_it()[0], 200)
        code, _ = self.answer_it()
        self.assertIn(code, (409, 429))
        self.store.answers = None  # a restart forgets what it posted
        self.gh.issues[40]["labels"].append({"name": "needs:" + OWNER})
        code, found = self.answer_it()
        self.assertEqual(code, 200, found)
        self.assertEqual(found["status"], "already")
        self.assertEqual(len(self.comments_posted()), 1)
        self.assertEqual([x["name"] for x in self.gh.issues[40]["labels"]], [answer.ANSWERED_LABEL])

    def test_an_answer_the_card_does_not_offer_or_an_unlisted_item_is_refused(self):
        self.assertEqual(self.answer_it(choice="maybe")[0], 400)
        self.assertEqual(self.answer_it(number=99)[0], 404)
        self.assertEqual(self.answer_it(note="this fixes #3")[0], 400)
        self.assertEqual(self.comments_posted(), [])

    def test_each_operator_is_rate_limited(self):
        with mock.patch.object(answers, "ANSWER_LIMIT", 1):
            self.assertEqual(self.answer_it()[0], 200)
            self.read_ask(44)
            self.assertEqual(self.answer_it(number=44, choice="b")[0], 429)
        self.assertEqual(len(self.comments_posted()), 1)

    def test_an_answered_card_waits_under_answered_not_yet_applied(self):
        self.answer_it(choice="no", note="use a narrower role")
        body = self.page()
        self.assertIn("Answered, not yet applied (1)", body)
        card = self.card(body, 40)
        self.assertIn("Answered <b>No</b> - use a narrower role by repo-owner", card)
        self.assertIn("the station has not applied it yet", card)
        self.assertNotIn("<button", card)
        # The next Operators pass reads the label instead of the memory.
        self.store.answers = None
        self.gh.pages["/repos/%s/issues?labels=loom%%3Aanswer-pending&" % REPO] = [self.gh.issues[40]]
        self.gh.pages["/repos/%s/issues?labels=needs%%3A%s&" % (REPO, OWNER)] = [self.gh.issues[44]]
        self.store._snapshot["operator_claims"]["read_at"] = 0
        self.store.refresh()
        card = self.card(self.page(), 40)
        self.assertIn("Answered <b>No</b>", card)
        self.assertIn("the station has not applied it yet", card)


class AskOnDemand(AnswerTest):
    def test_a_card_with_no_ask_yet_reads_it_now(self):
        card = self.card(self.page(), 44)
        self.assertIn("data-ask-read=\"/api/needs-you/ask?repo=%s&amp;number=44\"" % REPO, card)
        code, found = self.read_ask(44)
        self.assertEqual(code, 200, found)
        self.assertIn("which store holds the key", found["ask"])
        self.assertIn('value="a"', found["card"])
        self.assertIn('value="b"', found["card"])
        self.assertIn("which store holds the key", self.card(self.page(), 44))

    def test_the_read_keeps_the_budget_rules(self):
        self.store._last_remaining = 10
        self.store._reset_at = 0
        code, found = self.read_ask(44)
        self.assertEqual(code, 429)
        self.assertIn("below the floor", found["error"])
        self.store._last_remaining = None
        with mock.patch.object(dashboard_github, "OPERATOR_ASK_READS", 1):
            self.assertEqual(self.read_ask(44)[0], 200)
            self.assertEqual(self.read_ask(42)[0], 429)

    def test_an_item_waiting_on_nobody_is_not_read(self):
        self.assertEqual(self.read_ask(99)[0], 404)


if __name__ == "__main__":
    unittest.main()
