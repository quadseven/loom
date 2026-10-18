"""The Operators page (loom#786): every open issue and pull request another
operator holds, grouped by login, with its age and linked pull request, read
on the refresh pass at low request cost."""
import os
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from dashboard import operators, shell
from bus.github import GitHubError
from dashboard.github import OPERATOR_CLAIMS_SECONDS, TicketStore

REPO = "example-owner/app"
LOOM = "loom-bot"
OTHER = "other-operator"
THIRD = "third-operator"
SHA = "a" * 40


def _stamp(hours_ago):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _issue(number, title, labels=(), assignees=(), pull_request=False, hours=30):
    out = {"number": number, "title": title, "state": "open", "body": "Part of #30\n",
           "html_url": "https://github.com/%s/%s/%d" % (REPO, "pull" if pull_request else "issues", number),
           "created_at": _stamp(hours), "updated_at": _stamp(1), "user": {"login": LOOM},
           "labels": [{"name": name} for name in labels],
           "assignees": [{"login": login} for login in assignees]}
    if pull_request:
        out["pull_request"] = {"url": "x"}
    return out


def _pull(number, body, merged=False, state="open", assignees=(), labels=()):
    return {"number": number, "title": "PR %d" % number, "state": state, "body": body,
            "html_url": "https://github.com/%s/pull/%d" % (REPO, number),
            "created_at": _stamp(5), "updated_at": _stamp(1),
            "merged_at": _stamp(2) if merged else None, "user": {"login": OTHER},
            "head": {"ref": "work-%d" % number, "sha": SHA if state == "open" else "b" * 40},
            "base": {"ref": "main", "repo": {"full_name": REPO}},
            "assignees": [{"login": login} for login in assignees],
            "labels": [{"name": name} for name in labels]}


class FakeGitHub:
    def __init__(self, pages, rest):
        self.pages, self.rest_data, self.calls = pages, rest, []
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
            data = self.rest_data.get(path)
            headers = {}
        return Response()


def _fixture():
    claimed_issue = _issue(5, "Shared cache warmup", ["claim:" + OTHER], [OTHER], hours=50)
    claimed_pr = _issue(7, "Land the cache warmup", ["claim:" + OTHER], [OTHER], pull_request=True)
    adoption = _issue(9, "Land or supersede #7", ["loom:ticket", "loom:queued", "claim:" + OTHER])
    assigned_ticket = _issue(10, "A ticket the third operator took", ["loom:ticket", "loom:briefed"], [THIRD])
    own_ticket = _issue(11, "This Loom's own", ["loom:ticket", "loom:running", "claim:" + LOOM], [LOOM])
    pages = {
        "/repos/%s/labels" % REPO: [{"name": "claim:" + LOOM}, {"name": "claim:" + OTHER}, {"name": "bug"}],
        "/repos/%s/issues?labels=claim%%3A%s&" % (REPO, OTHER): [claimed_issue, claimed_pr, adoption],
        "/repos/%s/issues?labels=claim%%3A%s&" % (REPO, LOOM): [own_ticket],
        "/repos/%s/issues?labels=loom%%3Aticket" % REPO: [adoption, assigned_ticket, own_ticket],
        "/repos/%s/pulls?" % REPO: [_pull(7, "Refs #5", assignees=[OTHER], labels=["claim:" + OTHER]),
                                    _pull(6, "An earlier try. Refs #5", merged=True, state="closed"),
                                    _pull(12, "Refs #11", assignees=[LOOM])],
    }
    rest = {"/repos/%s/commits/%s/check-runs?per_page=100" % (REPO, SHA): {"check_runs": [
        {"name": "unittest", "status": "completed", "conclusion": "success"},
        {"name": "Grug - Elder", "status": "completed", "conclusion": "neutral"}]}}
    return FakeGitHub(pages, rest)


ENV = {key: "" for key in ("LOOM_IMPROVEMENT_HOMES", "LOOM_STATUS_REPO", "LOOM_METRICS_GOAL",
                           "LOOM_BACKLOG_SOURCE", "LOOM_TRUSTED_BRIEFERS")}


class OperatorsStoreTest(unittest.TestCase):
    def store(self, authors=LOOM):
        patcher = mock.patch.dict(os.environ, dict(ENV, LOOM_BRIEF_AUTHORS=authors))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.gh = _fixture()
        store = TicketStore([REPO], gh=self.gh, budget_floor=500)
        store._waits_refreshed_at = float("inf")
        return store

    def claim_reads(self):
        return [path for path in self.gh.calls if "/labels" in path or "labels=claim" in path]

    def test_page_groups_other_operators_work_by_login_with_linked_pull_requests(self):
        store = self.store()
        store.refresh()
        body = operators.render(store.response()["operator_claims"], time.time())
        self.assertIn("Loom leaves these alone", body)
        # Grouped by login: the claimed issue, PR and adoption ticket under
        # one, the ticket assigned to a third login under another.
        groups = body.split('<details class="tickets-group"')[1:]
        self.assertEqual(len(groups), 2)
        self.assertIn(OTHER, groups[0])
        self.assertIn("Shared cache warmup", groups[0])
        self.assertIn("Land the cache warmup", groups[0])
        self.assertIn(THIRD, groups[1])
        self.assertIn("A ticket the third operator took", groups[1])
        # This Loom's own claim and pull request are not listed.
        self.assertNotIn("app#11", body)
        self.assertNotIn("#12", body)
        # The claimed issue links its open and merged pull requests, the open
        # one with its checks (neutral is not a pass).
        self.assertIn("#7 open, 1 neutral, 1 passed", body)
        self.assertIn("#6 merged", body)
        self.assertIn("Grug - Elder (neutral)", body)
        self.assertIn(">2d<", body)

    def test_claimed_land_or_supersede_tickets_have_their_own_section(self):
        store = self.store()
        store.refresh()
        body = operators.render(store.response()["operator_claims"], time.time())
        section = body.split('<section class="operators-adopted">')[1].split("</section>")[0]
        self.assertIn("Land or supersede #7", section)
        # It links the pull request its title names.
        self.assertIn("#7 open, 1 neutral, 1 passed", section)
        self.assertNotIn("Shared cache warmup", section)

    def test_reads_one_list_per_foreign_claim_label_and_only_every_interval(self):
        store = self.store()
        store.refresh()
        self.assertEqual(self.claim_reads(), [
            "/repos/%s/labels?per_page=100" % REPO,
            "/repos/%s/issues?labels=claim%%3A%s&state=open&per_page=100" % (REPO, OTHER)])
        self.gh.calls.clear()
        store.refresh()
        self.assertEqual(self.claim_reads(), [])
        self.assertNotIn("/check-runs", " ".join(self.gh.calls))
        store._snapshot["operator_claims"]["read_at"] -= OPERATOR_CLAIMS_SECONDS
        store.refresh()
        self.assertEqual(len(self.claim_reads()), 2)

    def test_below_the_budget_floor_reads_nothing_and_keeps_the_last_claims(self):
        store = self.store()
        store.refresh()
        kept = store.response()["operator_claims"]
        store._snapshot["operator_claims"]["read_at"] -= OPERATOR_CLAIMS_SECONDS
        store._last_remaining, store._reset_at = 10, 2000000000
        self.gh.calls.clear()
        store.refresh()
        self.assertEqual(self.claim_reads(), [])
        self.assertEqual(store.response()["operator_claims"]["items"], kept["items"])

    def test_a_failed_claim_read_keeps_the_last_items_says_why_and_retries_next_pass(self):
        store = self.store()
        store.refresh()
        store._snapshot["operator_claims"]["read_at"] -= OPERATOR_CLAIMS_SECONDS
        labels = "/repos/%s/labels" % REPO
        listed = self.gh.pages.pop(labels)
        original = self.gh.paginate

        def failing(path):
            if path.startswith(labels):
                raise GitHubError("label list failed")
            return original(path)
        self.gh.paginate = failing
        store.refresh()
        claims = store.response()["operator_claims"]
        body = operators.render(claims, time.time())
        self.assertIn("label list failed", body)
        # The claimed non-ticket issue only the claim list finds is still shown.
        self.assertIn("Shared cache warmup", body)
        self.assertTrue(claims["retry"])
        self.gh.paginate, self.gh.pages[labels] = original, listed
        self.gh.calls.clear()
        store.refresh()
        self.assertEqual(len(self.claim_reads()), 2)
        self.assertFalse(store.response()["operator_claims"]["retry"])

    def test_without_brief_authors_every_claim_label_is_listed_and_the_page_says_why(self):
        store = self.store(authors="")
        store.refresh()
        self.assertIn("/repos/%s/issues?labels=claim%%3A%s&state=open&per_page=100" % (REPO, LOOM),
                      self.claim_reads())
        body = operators.render(store.response()["operator_claims"], time.time(), env={})
        self.assertIn("LOOM_BRIEF_AUTHORS is not set", body)
        self.assertIn("app#11", body)
        # Assignees alone are not judged without the logins.
        self.assertNotIn(THIRD, body)


def _needs_fixture():
    """The claims fixture plus needs:<login> items (loom#810): the claimed
    issue also waits on THIRD, an issue waits on this Loom's own login, and a
    pull request with no comment waits on THIRD."""
    gh = _fixture()
    claimed_issue = _issue(5, "Shared cache warmup", ["claim:" + OTHER, "needs:" + THIRD], [OTHER], hours=50)
    claimed_issue["comments"] = 3
    own_ask = _issue(20, "Mint the app key", ["needs:" + LOOM], hours=80)
    own_ask["comments"] = 1
    waiting_pr = _issue(21, "Flip the ruleset", ["needs:" + THIRD], pull_request=True, hours=3)
    waiting_pr["comments"] = 0
    labels = "/repos/%s/labels" % REPO
    gh.pages[labels] = gh.pages[labels] + [{"name": "needs:" + THIRD}, {"name": "needs:" + LOOM},
                                           {"name": "needs-operator"}]
    claims = "/repos/%s/issues?labels=claim%%3A%s&" % (REPO, OTHER)
    gh.pages[claims] = [claimed_issue] + gh.pages[claims][1:]
    gh.pages["/repos/%s/issues?labels=needs%%3A%s&" % (REPO, THIRD)] = [claimed_issue, waiting_pr]
    gh.pages["/repos/%s/issues?labels=needs%%3A%s&" % (REPO, LOOM)] = [own_ask]
    gh.rest_data["/repos/%s/issues/5/comments?per_page=1&page=3" % REPO] = [{
        "body": "<!-- ccqw:handoff v1 -->\n## Exact ask: run `gh variable set FLAG --body true`\n\nMore detail."}]
    gh.rest_data["/repos/%s/issues/20/comments?per_page=1&page=1" % REPO] = [{
        "body": "Create the App and store its key in the parameter store."}]
    return gh


class NeedsTest(unittest.TestCase):
    """needs:<login> items on the Operators page and the Floor (loom#810)."""

    def store(self):
        patcher = mock.patch.dict(os.environ, dict(ENV, LOOM_BRIEF_AUTHORS=LOOM))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.gh = _needs_fixture()
        store = TicketStore([REPO], gh=self.gh, budget_floor=500)
        store._waits_refreshed_at = float("inf")
        return store

    def needs_reads(self):
        return [path for path in self.gh.calls if "labels=needs%3A" in path or "/comments?per_page=1" in path]

    def group(self, body, login):
        return body.split('<details class="tickets-group" id="person-%s"' % login)[1].split("</details>")[0]

    def test_needs_items_are_grouped_by_login_with_age_and_the_latest_comments_ask(self):
        store = self.store()
        store.refresh()
        body = operators.render(store.response()["operator_claims"], time.time())
        third = self.group(body, THIRD)
        self.assertIn("Waiting on %s" % THIRD, third)
        self.assertIn("Shared cache warmup", third)
        self.assertIn("Flip the ruleset", third)
        # The ask is the first worded line of the latest comment, markers dropped.
        self.assertIn("Exact ask: run `gh variable set FLAG --body true`", third)
        self.assertNotIn("More detail", third)
        self.assertNotIn("ccqw:handoff", third)
        self.assertIn("no comment names the ask", third)
        self.assertIn(">2d<", third)
        # A needs label naming this Loom's own login is listed too: that
        # person still has to act.
        own = self.group(body, LOOM)
        self.assertIn("Mint the app key", own)
        self.assertIn("Create the App and store its key in the parameter store.", own)
        self.assertIn(">3d<", own)

    def test_an_item_claimed_by_one_login_and_waiting_on_another_is_a_row_in_each_group(self):
        store = self.store()
        store.refresh()
        body = operators.render(store.response()["operator_claims"], time.time())
        other, third = self.group(body, OTHER), self.group(body, THIRD)
        waiting = third.split("Waiting on %s" % THIRD)[1]
        self.assertIn("Shared cache warmup", waiting)
        # THIRD holds another ticket; #5 is not among its claimed rows.
        self.assertNotIn("app#5<", third.split("Claimed by %s" % THIRD)[1])
        self.assertIn("Shared cache warmup", other)
        self.assertNotIn("Waiting on %s" % OTHER, other)
        self.assertEqual(body.count("app#5<"), 2)

    def test_the_floor_counts_needs_items_per_person_and_links_to_their_group(self):
        from dashboard import server
        from dashboard.github import _floor_context
        store = self.store()
        store.refresh()
        claims = store.response()["operator_claims"]

        class State:
            context = _floor_context([], [], [], [])
            data = {}

            def __init__(self, source):
                self.source = source

            def response(self):
                return {"tickets": {}, "operator_items": [], "operator_claims": self.source}
        floor = server._floor(State(claims))
        card = floor.split('<section class="card waiting-on-card">')[1].split("</section>")[0]
        self.assertIn('<a href="/operators#person-%s">%s</a> <strong class="count">2</strong>' % (THIRD, THIRD), card)
        self.assertIn('<a href="/operators#person-%s">%s</a> <strong class="count">1</strong>' % (LOOM, LOOM), card)
        self.assertLess(card.index(THIRD), card.index(LOOM))
        empty = server._floor(State({"items": [], "needs": [], "read_at": time.time()}))
        self.assertNotIn("Waiting on a person", empty)
        self.assertNotIn("Waiting on a person", server._floor(State({})))

    def test_needs_reads_ride_the_interval_reuse_unchanged_asks_and_stop_below_the_floor(self):
        store = self.store()
        store.refresh()
        self.assertEqual(sorted(self.needs_reads()), sorted([
            "/repos/%s/issues?labels=needs%%3A%s&state=open&per_page=100" % (REPO, THIRD),
            "/repos/%s/issues?labels=needs%%3A%s&state=open&per_page=100" % (REPO, LOOM),
            "/repos/%s/issues/5/comments?per_page=1&page=3" % REPO,
            "/repos/%s/issues/20/comments?per_page=1&page=1" % REPO]))
        # Inside the interval: nothing is read again.
        self.gh.calls.clear()
        store.refresh()
        self.assertEqual(self.needs_reads(), [])
        # After the interval: one list per needs label again, and the
        # unchanged items keep their asks without a comment read.
        store._snapshot["operator_claims"]["read_at"] -= OPERATOR_CLAIMS_SECONDS
        self.gh.calls.clear()
        store.refresh()
        self.assertEqual(len(self.needs_reads()), 2)
        self.assertNotIn("/comments?per_page=1", " ".join(self.gh.calls))
        kept = store.response()["operator_claims"]
        self.assertIn("Exact ask", operators.render(kept, time.time()))
        # Below the budget floor: nothing is read, the last result stays shown.
        store._snapshot["operator_claims"]["read_at"] -= OPERATOR_CLAIMS_SECONDS
        store._last_remaining, store._reset_at = 10, 2000000000
        self.gh.calls.clear()
        store.refresh()
        self.assertEqual(self.needs_reads(), [])
        self.assertEqual(store.response()["operator_claims"]["needs"], kept["needs"])

    def test_a_changed_item_reads_its_ask_again(self):
        store = self.store()
        store.refresh()
        store._snapshot["operator_claims"]["read_at"] -= OPERATOR_CLAIMS_SECONDS
        key = "/repos/%s/issues?labels=needs%%3A%s&" % (REPO, LOOM)
        changed = dict(self.gh.pages[key][0], updated_at=_stamp(0), comments=2)
        self.gh.pages[key] = [changed]
        self.gh.rest_data["/repos/%s/issues/20/comments?per_page=1&page=2" % REPO] = [{"body": "Done, key stored."}]
        self.gh.calls.clear()
        store.refresh()
        self.assertIn("/repos/%s/issues/20/comments?per_page=1&page=2" % REPO, self.gh.calls)
        self.assertIn("Done, key stored.", operators.render(store.response()["operator_claims"], time.time()))


class AskLineTest(unittest.TestCase):
    def test_first_worded_line_without_markers_clipped(self):
        self.assertEqual(operators.ask_line("<!-- x v1 -->\n\n> **Ask:** run it\nmore"), "**Ask:** run it")
        self.assertEqual(operators.ask_line(""), "")
        self.assertEqual(operators.ask_line(None), "")
        long = operators.ask_line("x" * 500)
        self.assertEqual(len(long), operators.ASK_CHARS)
        self.assertTrue(long.endswith("..."))

    def test_needs_labels_name_logins_only(self):
        self.assertEqual(operators.needs_labels(["needs:Alice", "needs-operator", "needs:", "needs:bad login", 3]),
                         ["needs:Alice"])
        self.assertEqual(operators.needs_of(["needs:Alice", "needs:alice", "claim:bob"]), ["alice"])


class OperatorsNavTest(unittest.TestCase):
    def test_operators_page_is_in_the_nav(self):
        self.assertIn(("operators", "Operators", "/operators"), shell.VIEWS)
        page = operators.page_html({"operator_claims": {}}, time.time())
        self.assertIn('href="/operators" aria-current="page"', page)
        self.assertIn("Claims have not been read yet", page)


if __name__ == "__main__":
    unittest.main()
