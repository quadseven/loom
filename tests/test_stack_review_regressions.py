"""Behavioral regressions from the stacked-lane PR review."""

from contextlib import ExitStack
from types import SimpleNamespace
from unittest import TestCase, mock

from bus import factory
from bus.github import GitHubError
from test_bus_factory import FakeGitHub, ME, NOW, REPO, STRANGER, iso
from shadow_env import setUpModule, tearDownModule  # noqa: F401

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

    def run_pull(self, note_error=None, guarded=False):
        patch = [{"filename": "bus/a.py", "patch": "@@ -1 +1 @@\n-old\n+new"}]
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(self.handler, "_reclass"))
            stack.enter_context(mock.patch.object(self.handler, "_status_note", side_effect=note_error))
            stack.enter_context(mock.patch.object(self.handler, "_review_state", return_value="reviewed"))
            stack.enter_context(mock.patch.object(factory.hunk_overlap, "pull_files", return_value=patch))
            judge = stack.enter_context(mock.patch.object(self.handler, "_judge"))
            if guarded:
                self.guard_completed = self.handler._guarded(
                    self.handler.pull_request, REPO, self.gh.pulls[52], self.gh.issues)
            else:
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

    def test_advisory_stack_failure_and_note_read_failure_still_judge(self):
        self.gh.pulls[51]["state"] = "closed"
        with mock.patch.object(self.handler, "_link_stack_for", side_effect=GitHubError("stack unavailable")), \
                mock.patch.object(self.handler, "_comments", side_effect=[GitHubError("note read unavailable"), []]):
            self.assertEqual(self.run_pull(guarded=True), 1)
        self.assertTrue(self.guard_completed)

    def test_advisory_stack_failure_and_note_write_failure_still_judge(self):
        self.gh.pulls[51]["state"] = "closed"
        with mock.patch.object(self.handler, "_link_stack_for", side_effect=GitHubError("stack unavailable")):
            self.assertEqual(self.run_pull(note_error=GitHubError("note write unavailable"), guarded=True), 1)
        self.assertTrue(self.guard_completed)


class StackLanding(TestCase):
    """Exercise the real factory landing boundary and its real merge backstop."""

    # AI-REVIEW 2026-11-11 [gpt-6-luna/subscription] 2956-AC4:
    # Helper ordering alone does not prove the real factory refuses an upper merge.

    def land(self, number, ticket, base_ref, parent=None):
        import json
        from test_bus_brain import Fake, SensitiveResumeTests, reply
        from bus import brain
        from bus.github import GitHub

        fixture = SensitiveResumeTests()
        issue = {"number": ticket, "state": "open", "body": "",
                 "labels": [{"name": "loom:ticket"}, {"name": "loom:pr"}]}
        body = "Refs %s#%d\n## Acceptance criteria\n- [x] built\n" % (REPO, ticket)
        pr_reply, ready_reply = fixture._merge_replies({"pr": {
            "number": number, "body": body, "stack": None,
            "head": {"ref": "loom/%d-work" % ticket, "sha": "checked",
                     "repo": {"full_name": REPO}},
            "base": {"ref": base_ref, "repo": {"full_name": REPO, "default_branch": "main"}}},
            "details": {"headRepository": {"nameWithOwner": REPO}}})
        full = json.loads(pr_reply[2])
        fake = Fake(pr_reply, ready_reply, reply(200, issue), reply(200, {"behind_by": 0}),
                    reply(200, {"merged": True}), reply(204), reply(200, []),
                    reply(200, issue), reply(200, {}))
        calls = []

        def transport(method, url, headers, payload):
            calls.append((method, url, json.loads(payload) if payload else None))
            if parent is not None and "/pulls?state=open&head=" in url:
                return reply(200, [parent] if parent["state"] == "open" else [])
            if parent is not None and "/pulls?state=closed&head=" in url:
                return reply(200, [parent] if parent["state"] == "closed" else [])
            return fake(method, url, headers, payload)

        handler = factory.Factory(GitHub("fixture-token", transport=transport), [REPO], NOW)
        self.assertIs(handler.merge, brain.merge)
        # Lock, behind, scope and the existing brain fixture's risk-tier gate
        # are neutral here: this tests stack order, not unrelated risk policy.
        # Readiness, the real stack backstop, merge PUT and settlement execute.
        import os
        with mock.patch.object(handler, "_lock", return_value=None), \
                mock.patch.object(handler, "_behind", return_value=False), \
                mock.patch.object(handler, "_scope_holds", return_value=False), \
                mock.patch.object(brain, "_refuse_risk_tier"), \
                mock.patch.dict(os.environ, {"LOOM_BRIEF_AUTHORS": "", "LOOM_TRUSTED_BRIEFERS": ""}):
            handler._land(REPO, number, full, issue, [])
        return calls, handler

    @staticmethod
    def merge_puts(calls):
        return [url for method, url, _ in calls
                if method == "PUT" and (url.endswith("/merge") or "/merge-async" in url)]

    def test_upper_cannot_land_while_parent_is_pending_or_failed(self):
        for checks in ("PENDING", "FAILURE"):
            with self.subTest(parent_checks=checks):
                parent = {"number": 7, "state": "open", "head": {"ref": "loom/9-work"},
                          "statusCheckRollup": {"state": checks}}
                calls, handler = self.land(8, 10, "loom/9-work", parent)
                self.assertEqual(self.merge_puts(calls), [])
                self.assertEqual(handler.actions, 0)
                self.assertNotIn(REPO, handler.landed)

    def test_factory_lands_lower_then_upper_after_the_parent_is_merged(self):
        parent = {"number": 7, "state": "open", "head": {"ref": "loom/9-work"}}
        blocked, _ = self.land(8, 10, "loom/9-work", parent)
        self.assertEqual(self.merge_puts(blocked), [])
        lower, lower_handler = self.land(7, 9, "main")
        self.assertIn(REPO, lower_handler.landed)
        parent.update(state="closed", merged_at="2030-01-01T00:00:00Z")
        upper, upper_handler = self.land(8, 10, "loom/9-work", parent)
        self.assertIn(REPO, upper_handler.landed)
        self.assertEqual([url.rsplit("/pulls/", 1)[1] for url in self.merge_puts(lower + upper)],
                         ["7/merge", "8/merge"])

    def test_an_unstacked_control_reaches_the_real_merge_put(self):
        calls, handler = self.land(8, 10, "main")
        self.assertEqual(len(self.merge_puts(calls)), 1)
        self.assertIn(REPO, handler.landed)
