"""Behavioral regressions from the stacked-lane PR review."""

from contextlib import ExitStack
from types import SimpleNamespace
from unittest import TestCase, mock

from bus import factory
from bus.github import GitHubError
from test_bus_factory import FakeGitHub, ME, NOW, REPO, STRANGER, iso

class Client:
    def __init__(self, stacks):
        self.stacks, self.calls = stacks, []
    def paginate(self, path):
        repo = path.split("/repos/", 1)[1].split("/pulls", 1)[0]
        return [{"number": 7, "state": "open", "user": {"login": "example-bot"},
                 "head": {"ref": "loom/base", "repo": {"full_name": repo}}}]
    def rest(self, method, path, body=None):
        self.calls.append((method, path, body))
        return SimpleNamespace(data=self.stacks if method == "GET" else {})

class StackReview(TestCase):
    def test_existing_stack_is_extended(self):
        client = Client([{"number": 42, "pull_requests": [7, 8]}])
        factory.link_stack(client, "example/alpha", 8, 9)
        self.assertEqual(client.calls[-1], ("POST", "/repos/example/alpha/stacks/42/add", {"pull_requests": [9]}))
    def test_same_pr_number_links_in_both_repositories(self):
        client = Client([])
        handler = factory.Factory(client, ["example/alpha", "example/beta"], 2_000_000_000, trusted=["example-bot"])
        for repo in ("example/alpha", "example/beta"):
            handler._link_stack_for(repo, {"number": 8, "body": "Builds on #7"})
        self.assertEqual([p for method, p, _ in client.calls if method == "POST"],
                         ["/repos/example/alpha/stacks", "/repos/example/beta/stacks"])


class TrustAndOverlap(TestCase):
    def setUp(self):
        factory._OVERLAP_MEMO.clear()
        self.gh = FakeGitHub()
        self.gh.head_date = iso(0)
        for ticket, pr in ((8, 51), (9, 52)):
            self.gh.ticket(ticket, "pr")
            self.gh.pull(pr, ticket)
        self.handler = factory.Factory(self.gh, [REPO], NOW, trusted=[ME["login"]])
        self.issue = self.gh.issues[9]

    def run_pull(self):
        patch = [{"filename": "bus/a.py", "patch": "@@ -1 +1 @@\n-old\n+new"}]
        with ExitStack() as stack:
            for name in ("_reclass", "_status_note"):
                stack.enter_context(mock.patch.object(self.handler, name))
            stack.enter_context(mock.patch.object(self.handler, "_review_state", return_value="reviewed"))
            stack.enter_context(mock.patch.object(factory.hunk_overlap, "pull_files", return_value=patch))
            judge = stack.enter_context(mock.patch.object(self.handler, "_judge"))
            self.handler.pull_request(REPO, self.gh.pulls[52], self.gh.issues)
            return judge.call_count

    def test_untrusted_draft_and_unlinked_peers_cannot_hold(self):
        for kind in ("untrusted", "draft", "unlinked", "fork"):
            with self.subTest(kind=kind):
                older = self.gh.pulls[51]
                older["user"] = STRANGER if kind == "untrusted" else ME
                older["draft"] = kind == "draft"
                older["head"]["repo"] = {"full_name": "example/fork" if kind == "fork" else REPO}
                self.gh.issues[8]["labels"] = [{"name": "loom:queued" if kind == "unlinked" else "loom:pr"}]
                self.assertEqual(self.run_pull(), 1)
                self.assertNotIn("wait:gate", factory._labels(self.issue))

    def test_fork_with_trunk_named_head_cannot_be_stack_base(self):
        self.gh.pulls[51]["head"].update(ref="main", repo={"full_name": "example/fork"})
        with mock.patch.object(factory, "link_stack") as link:
            self.handler._link_stack_for(REPO, self.gh.pulls[52])
        link.assert_not_called()

    def test_overlap_wait_is_released_when_peer_closes_or_lands(self):
        for merged in (False, True):
            with self.subTest(merged=merged):
                self.gh.pulls[51].update(state="open", merged=False)
                self.handler._open_pulls.clear()
                self.assertEqual(self.run_pull(), 0)
                self.assertIn("wait:gate", factory._labels(self.issue))
                self.handler.known_waits[REPO + "#9"] = "overlaps pull request #51"
                self.gh.pulls[51].update(state="closed", merged=merged)
                self.handler._open_pulls.clear()
                self.assertEqual(self.run_pull(), 1)
                self.assertNotIn("wait:gate", factory._labels(self.issue))

    def test_old_failed_head_does_not_hold_but_fresh_or_unknown_failure_does(self):
        for age, blocks in ((factory.STUCK_AGE + 1, False), (factory.STUCK_AGE - 1, True), (None, True)):
            with self.subTest(age=age):
                self.handler._overlap_health.clear()
                self.issue["labels"] = [{"name": "loom:ticket"}, {"name": "loom:pr"}]
                self.gh.runs["sha51"] = [{"name": "unittest", "status": "completed", "conclusion": "failure",
                                           "completed_at": iso(age) if age is not None else None}]
                self.assertEqual(self.run_pull(), 0 if blocks else 1)

    def test_failed_advisory_stack_call_still_reaches_judgment(self):
        self.gh.pulls[51]["state"] = "closed"
        with mock.patch.object(self.handler, "_link_stack_for", side_effect=GitHubError("unavailable")):
            self.assertEqual(self.run_pull(), 1)

    def test_conflict_uses_stable_head_clock_and_unknown_clocks_stay_explicit(self):
        self.gh.pulls[51]["mergeable"] = False
        for age, blocks in ((factory.STUCK_AGE + 1, False), (1, True), (-1, True), (None, True)):
            with self.subTest(age=age):
                self.handler._overlap_health.clear()
                self.issue["labels"] = [{"name": "loom:ticket"}, {"name": "loom:pr"}]
                self.gh.head_date = iso(age) if age is not None else None
                self.assertEqual(self.run_pull(), 0 if blocks else 1)
                if age is None or age < 0:
                    self.assertIn("health clock unavailable", self.handler.waits[REPO + "#9"])

    def test_missing_or_future_failure_clock_uses_a_readable_head_clock(self):
        for clock in (None, iso(-1)):
            with self.subTest(clock=clock):
                self.handler._overlap_health.clear()
                self.gh.runs["sha51"] = [{"name": "unittest", "status": "completed",
                                           "conclusion": "failure", "completed_at": clock}]
                self.gh.head_date = iso(factory.STUCK_AGE + 1)
                self.assertEqual(self.run_pull(), 1)

    def test_builds_on_prose_does_not_reorder_independent_pulls(self):
        upper = {"number": 52, "body": "This builds on #51 in prose.", "base": {"ref": "main"}}
        self.assertIsNone(factory._stack_parent(upper, {}, {51, 52}))
        upper["body"] = "builds on #51"
        self.assertEqual(factory._stack_parent(upper, {}, {51, 52}), 51)

    def test_each_peer_health_is_read_once_per_head_and_repository_per_pass(self):
        with mock.patch.object(self.handler, "_check_runs", wraps=self.handler._check_runs) as checks:
            self.run_pull()
            self.run_pull()
        self.assertEqual(checks.call_count, 1)

    def test_next_repo_pass_refreshes_same_head_after_check_recovery(self):
        patch = [{"filename": "bus/a.py", "patch": "@@ -1 +1 @@\n-old\n+new"}]
        self.gh.runs["sha51"] = [{"name": "unittest", "status": "completed", "conclusion": "failure",
                                   "completed_at": iso(factory.STUCK_AGE + 1)}]
        def only_pulls(step, repo, item, *rest):
            if getattr(step, "__name__", "") == "pull_request":
                step(repo, item, *rest)
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(self.handler, "_guarded", side_effect=only_pulls))
            for name in ("_reclass", "_status_note"):
                stack.enter_context(mock.patch.object(self.handler, name))
            stack.enter_context(mock.patch.object(self.handler, "_review_state", return_value="reviewed"))
            stack.enter_context(mock.patch.object(factory.hunk_overlap, "pull_files", return_value=patch))
            judge = stack.enter_context(mock.patch.object(self.handler, "_judge"))
            checks = stack.enter_context(mock.patch.object(self.handler, "_check_runs", wraps=self.handler._check_runs))
            self.handler._repo_pass(REPO, self.gh.issues)
            self.assertIn(52, [call.args[1] for call in judge.call_args_list])
            judge.reset_mock()
            self.gh.runs["sha51"][0]["conclusion"] = "success"
            self.handler._repo_pass(REPO, self.gh.issues)
            self.assertNotIn(52, [call.args[1] for call in judge.call_args_list])
            self.assertEqual(checks.call_count, 2)
