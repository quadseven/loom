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


class OperatorsNavTest(unittest.TestCase):
    def test_operators_page_is_in_the_nav(self):
        self.assertIn(("operators", "Operators", "/operators"), shell.VIEWS)
        page = operators.page_html({"operator_claims": {}}, time.time())
        self.assertIn('href="/operators" aria-current="page"', page)
        self.assertIn("Claims have not been read yet", page)


if __name__ == "__main__":
    unittest.main()
