"""Open issues free for a second operator (loom#685): `python3 -m bus.free`
and the dashboard's /free page list them from one function, which asks the
ownership rule triage uses (bus.tickets.owned_elsewhere)."""
import io
import os
import sys
import types
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain, free, tickets  # noqa: E402
from dashboard import free_view, server  # noqa: E402
from dashboard.github import FREE_ISSUES_SECONDS, TicketStore, ticket_record  # noqa: E402

REPO = "example-owner/app"
ME = "second-operator"
OTHER = "loom-operator"


def _issue(number, labels=(), assignees=(), body="", sub_issues=0, state="open"):
    return {"number": number, "title": "Issue %d" % number, "state": state,
            "html_url": "https://github.com/%s/issues/%d" % (REPO, number), "body": body,
            "user": {"login": "someone", "type": "User"},
            "labels": [{"name": name} for name in labels],
            "assignees": [{"login": login} for login in assignees],
            "sub_issues_summary": {"total": sub_issues}}


# The fixture repo: one issue of each kind the view must tell apart.
OPEN_ISSUES = [
    _issue(1, ["loom:triage"], assignees=[OTHER]),                 # assigned to the other operator
    _issue(2, ["loom:triage", "claim:" + OTHER]),                   # claimed by the other operator
    _issue(3, ["loom:ticket", "loom:briefed"], body="Part of #40"),  # a briefed Loom ticket
    _issue(4, ["loom:rewritten"]),                                  # rewritten, its ticket #20 open
    _issue(5, ["loom:rewritten"]),                                  # rewritten, its ticket #21 closed
    _issue(6, ["epic"]),                                            # an epic by label
    _issue(7, [], sub_issues=3),                                    # an epic by its sub-issues
    _issue(8, ["loom:triage"]),                                     # free
    _issue(9, []),                                                  # free, no loom label
    _issue(10, ["loom:triage"], assignees=[ME]),                    # free: assigned to me
    _issue(11, ["loom:rewritten"]),                                 # rewritten, ticket #23 merged
    _issue(12, ["loom:triage"]),                                    # "Briefed as #20" comment: open
    _issue(13, ["loom:goal"]),                                      # a goal home
    _issue(14, ["claim:" + ME]),                                    # free: claimed by me
]
OPEN_ISSUES.append(dict(_issue(15), pull_request={}))               # a pull request: left out
TICKETS = [
    _issue(3, ["loom:ticket", "loom:briefed"], body="Part of #40"),
    _issue(20, ["loom:ticket", "loom:running"], body="Part of #40\n\nRewritten from issue 4.\n"),
    _issue(21, ["loom:ticket", "loom:merged"], body="Part of #40\n\nRewritten from issue 5.\n",
           state="closed"),
    _issue(23, ["loom:ticket", "loom:shipped"], body="Part of #40\n\nRefs #11\n"),
]
COMMENTS = {12: [{"body": "Briefed as #20 on the epic."}]}

FREE = {8: "no owner and no Loom ticket", 9: "no owner and no Loom ticket",
        10: "yours (assigned or claim:%s)" % ME, 14: "yours (assigned or claim:%s)" % ME}
CHECK = {5: "Loom ticket #21 is closed; verify and close this issue",
         11: "Loom ticket #23 is merged; verify and close this issue"}
OWNED = {1, 2}


class FakeGitHub:
    def __init__(self):
        self.paths = []

    def paginate(self, path):
        self.paths.append(path)
        if "/comments" in path:
            number = int(path.split("/issues/")[1].split("/")[0])
            return COMMENTS.get(number, [])
        if "labels=loom%3Aticket" in path:
            return [dict(t) for t in TICKETS]
        if "state=open" in path:
            return [dict(i) for i in OPEN_ISSUES]
        raise AssertionError(path)

    def rest(self, method, path, *args, **kwargs):
        self.paths.append(path)
        assert method == "GET", method
        number = int(path.rsplit("/", 1)[1])
        found = next((t for t in TICKETS if t["number"] == number), None)
        return types.SimpleNamespace(data=dict(found) if found else None)


def _by_bucket(entries, bucket):
    return {e["number"]: e["reason"] for e in entries if e["bucket"] == bucket}


class FreeIssuesTest(unittest.TestCase):
    def test_the_fixture_repo_lists_exactly_the_free_and_closed_ticket_issues(self):
        gh = FakeGitHub()
        entries = free.collect(gh, [REPO], ME)[REPO]
        self.assertEqual(_by_bucket(entries, free.FREE), FREE)
        self.assertEqual(_by_bucket(entries, free.CHECK), CHECK)
        taken = _by_bucket(entries, free.TAKEN)
        self.assertEqual(set(taken), {1, 2, 3, 4, 6, 7, 12, 13})
        self.assertEqual(taken[1], "held by %s (assigned or claim:%s)" % (OTHER, OTHER))
        self.assertEqual(taken[3], "a Loom ticket")
        self.assertEqual(taken[4], "Loom ticket #20 is open")
        self.assertEqual(taken[6], "an epic")
        self.assertEqual(taken[7], "an epic")
        self.assertEqual(taken[12], "Loom ticket #20 is open")
        self.assertEqual(taken[13], "a goal home")
        self.assertNotIn(15, {e["number"] for e in entries})
        loom = {e["number"]: e["loom"] for e in entries}
        self.assertEqual(loom[8], "loom:triage")
        self.assertEqual(loom[9], "none")

    def test_it_only_reads(self):
        gh = FakeGitHub()
        free.collect(gh, [REPO], ME)
        self.assertFalse(hasattr(gh, "post"))
        # Comments are read only for Loom-labelled issues that still read
        # free or unresolved, and a ticket already read is not read again.
        self.assertEqual(sorted(p for p in gh.paths if "/comments" in p),
                         ["/repos/%s/issues/%d/comments" % (REPO, n) for n in (10, 12, 8)])
        self.assertEqual([p for p in gh.paths if "/comments" not in p and "?" not in p], [])

    def test_with_no_operator_an_issue_anyone_holds_is_taken(self):
        entries = free.collect(FakeGitHub(), [REPO], "")[REPO]
        self.assertEqual(set(_by_bucket(entries, free.FREE)), {8, 9})
        self.assertIn(10, _by_bucket(entries, free.TAKEN))
        self.assertIn(14, _by_bucket(entries, free.TAKEN))

    def test_a_rewritten_issue_settles_by_its_comment(self):
        issue = _issue(30, ["loom:rewritten"])
        gh = FakeGitHub()
        COMMENTS[30] = [{"body": "<!-- loom:triage v1 -->\nRewritten as ticket #20, which refs this issue."}]
        self.addCleanup(COMMENTS.pop, 30)
        with mock.patch.object(gh, "paginate", side_effect=lambda path: (
                [issue] if "state=open" in path else [] if "loom%3Aticket" in path
                else COMMENTS[30])):
            entries = free.collect(gh, [REPO], ME)[REPO]
        self.assertEqual(entries[0]["bucket"], free.TAKEN)
        self.assertEqual(entries[0]["reason"], "Loom ticket #20 is open")

    def test_a_rewritten_issue_with_no_ticket_found_is_listed_to_check(self):
        covers = free.coverage({REPO: []})
        entries = free.free_issues(REPO, [_issue(31, ["loom:rewritten"])], ME, covers)
        self.assertEqual(entries[0]["bucket"], free.CHECK)

    def test_the_command_prints_the_free_issues(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = free.main([REPO, "--operator", ME], gh=FakeGitHub())
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("%s: 14 open issues, 4 free for %s, 2 to check" % (REPO, ME), text)
        self.assertIn("free #8 Issue 8 [no owner and no Loom ticket] (loom: loom:triage)", text)
        self.assertIn("check #5", text)
        self.assertNotIn("#1 ", text)


class SharedOwnershipTest(unittest.TestCase):
    """The view and triage ask one ownership function and agree on the fixture."""

    def test_view_and_triage_agree_on_who_owns_each_issue(self):
        covers = free.coverage({REPO: TICKETS})
        for issue in OPEN_ISSUES:
            if "pull_request" in issue:
                continue
            bucket, reason = free.classify(REPO, issue, ME, covers.get((REPO, issue["number"]), ()))
            held = reason.startswith("held by ")
            self.assertEqual(held, brain._triage_skip(issue, [ME]) == "another operator's",
                             issue["number"])
            self.assertEqual(held, issue["number"] in OWNED, issue["number"])

    def test_both_call_tickets_owned_elsewhere(self):
        real = tickets.owned_elsewhere
        with mock.patch.object(tickets, "owned_elsewhere", side_effect=real) as spy:
            free.classify(REPO, OPEN_ISSUES[0], ME)
            self.assertEqual(spy.call_count, 1)
            brain._triage_skip(OPEN_ISSUES[0], [ME])
            self.assertEqual(spy.call_count, 2)


class FreePageTest(unittest.TestCase):
    def _store(self):
        store = mock.Mock()
        store.open_issues = {REPO: [free_view.open_issue_record(i) for i in OPEN_ISSUES
                                    if "pull_request" not in i]}
        store.response.return_value = {
            "tickets": {REPO: [ticket_record(REPO, t) for t in TICKETS]},
            "issue_comments": [{"body": c["body"], "issue_url": "https://api.github.com/repos/%s/issues/%d"
                                % (REPO, n)} for n, found in COMMENTS.items() for c in found]}
        store.open_issues_read_at = 1760000000
        store.open_issues_error = ""
        return store

    def _page(self, query):
        sent = {}
        handler = mock.Mock()
        handler.send_page.side_effect = lambda body, code=200: sent.update(body=body)
        server._dispatch_free(handler, self._store(), server.urlsplit("/free?" + query))
        return sent["body"]

    def test_the_page_lists_the_same_free_issues_as_the_command(self):
        body = self._page("operator=" + ME)
        self.assertIn("4 free, 2 to check, 8 taken", body)
        for number in FREE:
            self.assertIn(">#%d</a>" % number, body)
        self.assertNotIn(">#1</a>", body)
        self.assertNotIn(">#4</a>", body)
        # #12 is settled by its "Briefed as #20" comment from the feed.
        self.assertNotIn(">#12</a>", body)
        self.assertIn(CHECK[5], body)

    def test_a_query_value_that_is_not_a_login_reads_as_anyone(self):
        body = self._page("operator=%3Cscript%3E")
        self.assertNotIn("<script>", body)
        self.assertIn("free for anyone", body)

    def test_the_nav_links_the_page(self):
        self.assertIn(("free", "Free to take", "/free"),
                      __import__("dashboard.shell", fromlist=["VIEWS"]).VIEWS)


class OpenIssueReadTest(unittest.TestCase):
    def _store(self):
        gh = FakeGitHub()
        return TicketStore([REPO], gh=gh, budget_floor=500), gh

    def test_open_issues_are_read_at_most_once_an_hour(self):
        store, gh = self._store()
        store._read_open_issues(10000.0)
        store._read_open_issues(10000.0 + FREE_ISSUES_SECONDS - 1)
        self.assertEqual(len([p for p in gh.paths if "state=open" in p]), 1)
        self.assertEqual([r["number"] for r in store.open_issues[REPO]], list(range(1, 15)))
        store._read_open_issues(10000.0 + FREE_ISSUES_SECONDS)
        self.assertEqual(len([p for p in gh.paths if "state=open" in p]), 2)

    def test_every_page_of_open_issues_is_read(self):
        pages = {"/repos/%s/issues?state=open&per_page=100" % REPO: (
                     [_issue(1)], '<https://api.example/next>; rel="next"'),
                 "https://api.example/next": ([_issue(2)], "")}
        gh = mock.Mock()
        gh.rest.side_effect = lambda method, path, *a, **k: types.SimpleNamespace(
            data=pages[path][0], status=200, headers={"link": pages[path][1]})
        from bus.github import GitHub
        gh.paginate.side_effect = lambda path: GitHub.paginate(gh, path)
        gh._paginate_from.side_effect = lambda path, first: GitHub._paginate_from(gh, path, first)
        store = TicketStore([REPO], gh=gh, budget_floor=500)
        store._read_open_issues(10000.0)
        self.assertEqual([r["number"] for r in store.open_issues[REPO]], [1, 2])

    def test_nothing_is_read_below_the_budget_floor(self):
        store, gh = self._store()
        store._last_remaining, store._reset_at = 10, 0
        store._read_open_issues(10000.0)
        self.assertEqual(gh.paths, [])
        self.assertIsNone(store.open_issues[REPO])


if __name__ == "__main__":
    unittest.main()
