"""No-change outcomes share a bounded budget across build and fix runs."""
import os
from unittest import mock

from bus import factory, station
from bus.github import GitHub
from tests.test_bus_station import Base, REPO, SHIPPED_BODY


class NoChangeBudgetTest(Base):
    def setUp(self):
        super().setUp()
        self.fake.add(12, body=SHIPPED_BODY, state="running")
        self.runner = station.Station(GitHub("token", transport=self.fake), self.settings)
        self.serial = 0
        self.fake.add(77, body="Pull request", state="pr")

    def answer(self, fix=False, name=None):
        self.serial += 1
        name = name or "no-change-%d" % self.serial
        lane = next((n[5:] for n in self.fake.names(12) if n.startswith("lane:")), "codex")
        self.fake.issues[12]["labels"] = [{"name": n} for n in
            ("loom:ticket", "loom:running", "lane:" + lane)]
        self.write(os.path.join(self.home, "logs", lane, name + ".answered"), "Nothing changed.")
        with mock.patch.object(self.runner, "_no_change_plan", create=True,
                               return_value={"lane": "claude", "model": "model-next", "rung": 2}):
            if fix:
                self.runner._nothing_to_fix(REPO, 12, 77, lane, name)
            else:
                self.runner._settle_no_change(REPO, 12, lane, name)
        return name

    def test_no_change_labels_delegate_moves_and_preserve_final_hold(self):
        from bus import ticket_move
        issue = self.fake.issues[12]
        issue["labels"] = [{"name": n} for n in (
            "loom:ticket", "loom:running", "lane:codex", "wait:no-change",
            "claim:alice-example", "claim:other-op")]
        with mock.patch.object(ticket_move, "next_labels", wraps=ticket_move.next_labels) as move:
            self.runner._no_change_labels(REPO, 12, issue, "answered", None, True)
        move.assert_called_once()
        self.assertFalse(move.call_args.kwargs["clear_needs_brain"])
        self.assertNotIn("claim:alice-example", self.fake.names(12))
        self.assertIn("claim:other-op", self.fake.names(12))
        self.assertIn("needs-brain", self.fake.names(12))
        self.assertNotIn("wait:no-change", self.fake.names(12))
        # Replaying the completed label write must keep a newer claim while
        # retaining the no-change hold, just like any other ticket move.
        issue["labels"].append({"name": "claim:alice-example"})
        self.runner._no_change_labels(REPO, 12, issue, "answered", None, True)
        self.assertIn("claim:alice-example", self.fake.names(12))
        self.assertIn("needs-brain", self.fake.names(12))

    def test_lane_reassignment_history_does_not_reset_durable_budget(self):
        self.answer()
        reassigned = "The station gave this queued ticket lane codex-2 (tier medium)."
        self.assertTrue(factory.history._is_brief(reassigned))
        self.runner._routine(REPO, 12, reassigned)
        self.fake.issues[12]["labels"] = [{"name": n} for n in (
            "loom:ticket", "loom:running", "lane:codex-2")]
        self.answer()
        self.assertEqual(self.runner._no_change_data()["%s#12" % REPO]["count"], 2)
        self.assertIn("Model: model-next", self.fake.issues[12]["body"])

    def test_limit_stops_requeue_and_promotes_once(self):
        for _ in range(factory.NO_CHANGE_LIMIT):
            self.answer()
        self.assertNotIn("loom:queued", self.fake.names(12))
        self.assertIn("Model: model-next", self.fake.issues[12]["body"])
        self.answer()
        self.assertIn("needs-brain", self.fake.names(12))

    def test_build_and_fix_share_the_same_budget(self):
        self.answer()
        self.answer(fix=True)
        self.assertIn("Model: model-next", self.fake.issues[12]["body"])
        self.answer(fix=True)
        self.assertIn("needs-brain", self.fake.names(12))

    def test_changed_unproven_boxes_start_a_new_budget(self):
        self.answer()
        self.fake.issues[12]["body"] = SHIPPED_BODY.replace("A test passes", "Another test passes")
        self.answer()
        self.assertIn("loom:queued", self.fake.names(12))
        self.assertNotIn("Model: model-next", self.fake.issues[12]["body"])

    def test_fix_below_limit_keeps_the_existing_pull_request_path(self):
        self.answer(fix=True)
        self.assertIn("loom:pr", self.fake.names(12))
        self.assertNotIn("loom:queued", self.fake.names(12))
        self.assertEqual(len(self.fake.comments[77]), 1)

    def test_replayed_run_does_not_spend_another_attempt(self):
        self.answer(name="same-run")
        self.answer(name="same-run")
        entry = self.runner._no_change_data()["%s#12" % REPO]
        self.assertEqual(entry["count"], 1)
        self.assertNotIn("Model: model-next", self.fake.issues[12]["body"])

    def test_changed_model_header_does_not_reset_boxes(self):
        self.answer()
        self.fake.issues[12]["body"] = "Model: some-model\n" + SHIPPED_BODY
        self.answer()
        self.assertIn("Model: model-next", self.fake.issues[12]["body"])

    def test_committed_run_resets_once_even_when_reporting_replays(self):
        self.answer()
        path = os.path.join(self.home, "logs", "codex", "committed.pushed")
        self.write(path, "pushed")
        self.runner._reset_no_change(REPO, 12, "codex", "committed")
        self.answer()
        self.runner._reset_no_change(REPO, 12, "codex", "committed")
        entry = self.runner._no_change_data()["%s#12" % REPO]
        self.assertEqual(entry["count"], 1)
        self.assertNotIn("Model: model-next", self.fake.issues[12]["body"])

    def test_done_without_pushed_marker_does_not_reset(self):
        self.answer()
        self.runner._reset_no_change(REPO, 12, "codex", "no-commit")
        self.answer()
        self.assertIn("Model: model-next", self.fake.issues[12]["body"])

    def test_busy_ladder_keeps_pending_escalation_without_requeue(self):
        self.answer()
        self.fake.issues[12]["labels"] = [{"name": n} for n in
            ("loom:ticket", "loom:running", "lane:codex")]
        self.write(os.path.join(self.home, "logs", "codex", "pending.answered"), "Nothing changed.")
        with mock.patch.object(self.runner, "_no_change_plan", return_value=None):
            self.runner._settle_no_change(REPO, 12, "codex", "pending")
        self.assertIn("wait:no-change", self.fake.names(12))
        self.assertNotIn("loom:queued", self.fake.names(12))
        self.assertFalse(self.runner._no_change_pending(REPO, 12, "codex", "pending"))
        with mock.patch.object(self.runner, "_no_change_plan", return_value={
                "lane": "claude", "model": "model-next", "rung": 2}):
            self.runner._settle_no_change(REPO, 12, "codex", "pending")
        self.assertIn("loom:briefed", self.fake.names(12))
        self.assertFalse(self.runner._no_change_pending(REPO, 12, "codex", "pending"))

    def test_hold_comment_is_posted_once(self):
        for _ in range(factory.NO_CHANGE_LIMIT):
            self.answer()
        name = self.answer()
        notes = [c for c in self.fake.comments[12] if "Held for the brain" in c["body"]]
        self.assertEqual(len(notes), 1)
        self.runner._settle_no_change(REPO, 12, "claude", name)
        self.assertEqual(len([c for c in self.fake.comments[12]
                             if "Held for the brain" in c["body"]]), 1)

    def test_partial_promotion_replays_the_same_plan(self):
        from bus.github import GitHubError
        for method, suffix in (("PATCH", "/issues/12"), ("PUT", "/issues/12/labels")):
            with self.subTest(method=method):
                self.setUp()
                self.answer()
                original = self.runner.gh.rest
                failed = []

                def write(verb, path, *args, original=original, failed=failed,
                          method=method, suffix=suffix, **kwargs):
                    result = original(verb, path, *args, **kwargs)
                    if verb == method and path.endswith(suffix) and not failed:
                        failed.append(True)
                        raise GitHubError("response lost after write")
                    return result

                with mock.patch.object(self.runner.gh, "rest", side_effect=write):
                    with self.assertRaises(GitHubError):
                        self.answer(name="promoting")
                with mock.patch.object(self.runner, "_no_change_plan", side_effect=AssertionError("replanned")):
                    self.runner._settle_no_change(REPO, 12, "codex", "promoting")
                self.assertIn("Model: model-next", self.fake.issues[12]["body"])
                self.assertIn("lane:claude", self.fake.names(12))
                self.assertEqual(self.runner._no_change_data()["%s#12" % REPO]["count"], 2)

    def test_partial_hold_comment_and_local_save_replay_once(self):
        from bus.github import GitHubError
        for _ in range(factory.NO_CHANGE_LIMIT):
            self.answer()
        original = self.runner.gh.rest
        failed = []

        def write(method, path, *args, **kwargs):
            result = original(method, path, *args, **kwargs)
            if method == "POST" and path.endswith("/issues/12/comments") and not failed:
                failed.append(True)
                raise GitHubError("response lost after comment")
            return result

        with mock.patch.object(self.runner.gh, "rest", side_effect=write):
            with self.assertRaises(GitHubError):
                self.answer(name="held-run")
        self.runner._settle_no_change(REPO, 12, "claude", "held-run")
        self.assertEqual(len([c for c in self.fake.comments[12]
                             if "Held for the brain" in c["body"]]), 1)
        # A failed reported.json write replays the same completed outcome.
        self.runner._settle_no_change(REPO, 12, "claude", "held-run")
        self.assertEqual(self.runner._no_change_data()["%s#12" % REPO]["count"], 3)

    def test_late_lower_rung_answer_does_not_hold_promoted_ticket(self):
        for _ in range(factory.NO_CHANGE_LIMIT):
            self.answer()
        self.runner._settle_no_change(REPO, 12, "codex", "late-lower-run")
        self.assertNotIn("needs-brain", self.fake.names(12))
        self.assertIn("loom:briefed", self.fake.names(12))

    def test_last_rung_holds_without_requeue(self):
        self.answer()
        self.fake.issues[12]["labels"] = [{"name": n} for n in
            ("loom:ticket", "loom:running", "lane:codex")]
        self.write(os.path.join(self.home, "logs", "codex", "last-rung.answered"), "Nothing changed.")
        with mock.patch.object(self.runner, "_no_change_plan", return_value={}):
            self.runner._settle_no_change(REPO, 12, "codex", "last-rung")
        self.assertIn("needs-brain", self.fake.names(12))
        self.assertNotIn("loom:queued", self.fake.names(12))

    def test_failed_run_that_pushed_resets_via_report(self):
        self.answer()
        name = "failed-after-push"
        self.write(os.path.join(self.home, "logs", "codex", name + ".pushed"), "pushed")
        path = os.path.join(self.home, "failed-brief.md")
        self.write(path, "Repo: %s\nIssue: %s#12\nLane: codex\n" % (REPO, REPO))
        with mock.patch.object(self.runner, "_report_failure", return_value=True), \
                mock.patch.object(self.runner, "_never_left_running"), \
                mock.patch.object(self.runner, "_revert_decision_ticks"):
            self.assertTrue(self.runner._report("codex", "failed", path, name))
        self.assertEqual(self.runner._no_change_data()["%s#12" % REPO]["count"], 0)

    def test_case_sensitive_changed_boxes_reset(self):
        self.answer()
        self.fake.issues[12]["body"] = SHIPPED_BODY.replace("A test passes", "a test passes")
        self.answer()
        self.assertEqual(self.runner._no_change_data()["%s#12" % REPO]["count"], 1)

    def test_reset_before_any_budget_cannot_reset_a_newer_outcome(self):
        data = {}
        no_change.reset(data, "ticket", "commit", 1)
        no_change.outcome(data, "ticket", ["box"], "new", 2, 2)
        self.assertFalse(no_change.reset(data, "ticket", "commit", 1))
        no_change.reset(data, "ticket", "older-commit", 1)
        self.assertEqual(data["ticket"]["count"], 1)

    def test_same_promoted_model_on_another_lane_holds(self):
        self.answer()
        self.answer()
        self.fake.issues[12]["labels"] = [{"name": n} for n in
            ("loom:ticket", "loom:running", "lane:codex")]
        self.write(os.path.join(self.home, "logs", "codex", "rerouted.answered"), "Nothing changed.")
        self.runner._settle_no_change(REPO, 12, "codex", "rerouted", "Model: model-next\n")
        self.assertIn("needs-brain", self.fake.names(12))

    def test_corrupt_ledger_fails_without_remote_effects(self):
        for content in ("{broken", "[]", '{"ticket": []}', '{"ticket": {"runs": []}}'):
            with self.subTest(content=content):
                path = os.path.join(self.runner.state, no_change.FILE)
                self.write(path, content)
                with mock.patch.object(self.runner.gh, "rest", wraps=self.runner.gh.rest) as rest:
                    with self.assertRaises(station.StateError):
                        self.runner._settle_no_change(REPO, 12, "codex", "bad-state")
                self.assertFalse([c for c in rest.call_args_list if c.args[0] != "GET"])
                with open(path) as stream:
                    self.assertEqual(stream.read(), content)

    def test_invalid_pending_budget_is_rejected_before_replay_writes(self):
        import json
        for step in (
            {"action": "retry", "done": False},
            {"action": "promote", "done": False, "marker": ""},
            {"action": "ignore", "done": False},
            {"action": "retry", "done": False, "marker": "a" * 16, "plan": {}},
            {"action": "hold", "done": False, "marker": "a" * 16, "plan": {}},
            {"action": "promote", "done": False, "marker": "a" * 16,
             "plan": {"lane": "", "model": "model-next", "rung": 2}},
            {"action": "promote", "done": False, "marker": "a" * 16,
             "plan": {"lane": "claude", "model": "", "rung": 2}},
            {"action": "promote", "done": False, "marker": "a" * 16,
             "plan": {"lane": "claude", "model": "model-next", "rung": 0}},
        ):
            with self.subTest(step=step):
                self.setUp()
                boxes = factory.unproven_criteria(SHIPPED_BODY, "Nothing changed.")
                data = {"%s#12" % REPO: {"key": no_change.key(boxes), "count": 1,
                                        "runs": {"codex/corrupt-run": step}}}
                path = os.path.join(self.runner.state, no_change.FILE)
                self.write(path, json.dumps(data))
                self.write(os.path.join(self.home, "logs", "codex", "corrupt-run.answered"), "Nothing changed.")
                before = self.fake.names(12)
                with mock.patch.object(self.runner.gh, "rest", wraps=self.runner.gh.rest) as rest:
                    with self.assertRaises(station.StateError):
                        self.runner._settle_no_change(REPO, 12, "codex", "corrupt-run")
                self.assertEqual(self.fake.names(12), before)
                self.assertFalse([c for c in rest.call_args_list if c.args[0] != "GET"])
                with open(path) as stream:
                    self.assertEqual(json.load(stream), data)

    def test_proven_boxes_cancel_pending_promotion(self):
        self.answer()
        self.fake.issues[12]["labels"] = [{"name": n} for n in
            ("loom:ticket", "loom:running", "lane:codex")]
        self.write(os.path.join(self.home, "logs", "codex", "pending.answered"), "Nothing changed.")
        with mock.patch.object(self.runner, "_no_change_plan", return_value=None):
            self.runner._settle_no_change(REPO, 12, "codex", "pending")
        self.fake.issues[12]["body"] = SHIPPED_BODY.replace("[ ]", "[x]")
        self.runner._settle_no_change(REPO, 12, "codex", "pending")
        self.assertFalse(self.runner._no_change_pending(REPO, 12, "codex", "pending"))

    def claimed_limit_run(self):
        self.answer()
        self.heartbeat("codex")
        issue = self.fake.issues[12]
        issue["body"] = "Model: " + factory.CLIMB[0][1] + "\n" + SHIPPED_BODY
        issue["labels"] = [{"name": n} for n in (
            "loom:ticket", "loom:running", "lane:claude", "claim:alice-example")]
        self.fake.events[12].append({"event": "labeled", "label": {"name": "claim:alice-example"},
                                    "created_at": "2026-01-01T00:00:00Z"})
        name = "claimed-limit"
        self.write(os.path.join(self.home, "logs", "claude", name + ".answered"), "Nothing changed.")
        return name

    def test_missing_area_blocks_new_and_saved_promotions(self):
        from bus.github import GitHubError
        for saved in (False, True):
            with self.subTest(saved=saved):
                self.setUp()
                name = self.claimed_limit_run()
                if saved:
                    with mock.patch.object(self.runner, "_apply_no_change", side_effect=GitHubError("before write")):
                        with self.assertRaises(GitHubError):
                            self.runner._settle_no_change(REPO, 12, "claude", name)
                self.runner._area_cache[REPO] = ["area:billing"]
                with mock.patch.dict(os.environ, {"LOOM_SHADOW": "0"}):
                    self.runner._settle_no_change(REPO, 12, "claude", name)
                self.assertIn("needs-brain", self.fake.names(12))
                self.assertIn("loom:queued", self.fake.names(12))
                self.assertNotIn("lane:codex", self.fake.names(12))
                self.assertIn("Model: " + factory.CLIMB[0][1], self.fake.issues[12]["body"])
                self.assertEqual(self.runner._no_change_data()["%s#12" % REPO]["count"], 2)
                self.assertTrue(self.runner._no_change_finished(REPO, 12, "claude", name))

    def test_unreadable_area_keeps_new_and_saved_promotions_pending(self):
        from bus.github import GitHubError
        for saved in (False, True):
            with self.subTest(saved=saved):
                self.setUp()
                name = self.claimed_limit_run()
                if saved:
                    with mock.patch.object(self.runner, "_apply_no_change", side_effect=GitHubError("before write")):
                        with self.assertRaises(GitHubError):
                            self.runner._settle_no_change(REPO, 12, "claude", name)
                before = self.fake.names(12)
                with mock.patch.object(self.runner, "_area_labels", side_effect=GitHubError("unreadable labels")), \
                        mock.patch.object(self.runner.gh, "rest", wraps=self.runner.gh.rest) as rest:
                    with self.assertRaises(GitHubError):
                        self.runner._settle_no_change(REPO, 12, "claude", name)
                self.assertEqual(self.fake.names(12), before)
                self.assertFalse([c for c in rest.call_args_list if c.args[0] != "GET"])
                self.assertTrue(self.runner._no_change_pending(REPO, 12, "claude", name))
                self.assertEqual(self.runner._no_change_data()["%s#12" % REPO]["count"], 2)

    def test_area_promotion_gate_preserves_regression_and_revert_exemptions(self):
        for label in ("loom:regression", factory.REVERT_LABEL):
            with self.subTest(label=label):
                self.setUp()
                name = self.claimed_limit_run()
                self.fake.issues[12]["labels"].append({"name": label})
                self.runner._area_cache[REPO] = ["area:billing"]
                self.runner._settle_no_change(REPO, 12, "claude", name)
                self.assertIn("loom:briefed", self.fake.names(12))
                self.assertIn("lane:codex", self.fake.names(12))

    def test_promotion_preserves_area_label_for_the_next_claim(self):
        name = self.claimed_limit_run()
        self.fake.issues[12]["labels"].append({"name": "area:billing"})
        self.runner._settle_no_change(REPO, 12, "claude", name)
        self.assertIn("area:billing", self.fake.names(12))
        self.assertIn("loom:briefed", self.fake.names(12))
        self.assertNotIn("claim:alice-example", self.fake.names(12))

    def test_area_refusal_hold_survives_an_old_failed_no_change_report(self):
        name = self.claimed_limit_run()
        self.runner._area_cache[REPO] = ["area:billing"]
        with mock.patch.dict(os.environ, {"LOOM_SHADOW": "0"}):
            self.assertTrue(self.runner._brief_holds(REPO, self.fake.issues[12], []))
        self.assertIn("needs-brain", self.fake.names(12))
        self.assertIn("loom:queued", self.fake.names(12))
        path = self.failed_claimed_brief(name)
        before = self.fake.names(12)
        with mock.patch.object(self.runner.gh, "rest", wraps=self.runner.gh.rest) as rest:
            self.assertTrue(self.runner._report("claude", "failed", path, name))
        self.assertEqual(self.fake.names(12), before)
        self.assertFalse([c for c in rest.call_args_list if c.args[0] != "GET"])
        self.assertTrue(self.runner._no_change_finished(REPO, 12, "claude", name))

    def test_real_selector_promotes_ended_run_with_own_claim(self):
        name = self.claimed_limit_run()
        self.runner._settle_no_change(REPO, 12, "claude", name)
        self.assertIn("loom:briefed", self.fake.names(12))
        self.assertIn("lane:codex", self.fake.names(12))
        self.assertNotIn("claim:alice-example", self.fake.names(12))
        self.assertFalse(self.runner._no_change_pending(REPO, 12, "claude", name))

    def test_pending_own_claim_promotes_when_real_lane_becomes_available(self):
        name = self.claimed_limit_run()
        with mock.patch.object(self.runner, "_no_change_plan", return_value=None):
            self.runner._settle_no_change(REPO, 12, "claude", name)
        self.assertFalse(self.runner._no_change_pending(REPO, 12, "claude", name))
        self.runner._settle_no_change(REPO, 12, "claude", name)
        self.assertIn("loom:briefed", self.fake.names(12))
        self.assertNotIn("claim:alice-example", self.fake.names(12))
        self.assertEqual(self.runner._no_change_data()["%s#12" % REPO]["count"], 2)

    def test_failed_report_ignores_obsolete_ownership_without_failing_active_run(self):
        for changed in ("newer", "external", "hold", "lane"):
            with self.subTest(changed=changed):
                self.setUp()
                name = self.claimed_limit_run()
                issue = self.fake.issues[12]
                if changed == "newer":
                    self.fake.events[12].append({"event": "labeled", "label": {"name": "claim:alice-example"},
                                                "created_at": "2099-01-01T00:00:00Z"})
                else:
                    issue["labels"].append({"name": {"external": "claim:someone-else",
                        "hold": "needs-brain", "lane": "lane:codex"}[changed]})
                path = self.failed_claimed_brief(name)
                before = self.fake.names(12)
                with mock.patch.object(self.runner.gh, "rest", wraps=self.runner.gh.rest) as rest:
                    self.assertTrue(self.runner._report("claude", "failed", path, name))
                self.assertEqual(self.fake.names(12), before)
                self.assertFalse([c for c in rest.call_args_list if c.args[0] != "GET"])
                self.assertTrue(self.runner._no_change_finished(REPO, 12, "claude", name))

    def failed_claimed_brief(self, name):
        self.write(os.path.join(self.home, "logs", "claude", name + ".log"),
                   "[2026-01-01T00:00:00Z] %s failed: [work] agent made no commit and PR text is current on branch\n" % name)
        path = os.path.join(self.home, "briefs", "claude", "failed", name + ".md")
        self.write(path, "Repo: %s\nIssue: %s#12\nFix-PR: 77\n" % (REPO, REPO))
        return path

    def test_failed_report_unknown_claim_is_retryable_without_remote_writes(self):
        name = self.claimed_limit_run()
        self.fake.events[12] = []
        path = self.failed_claimed_brief(name)
        before = self.fake.names(12)
        with mock.patch.object(self.runner.gh, "rest", wraps=self.runner.gh.rest) as rest:
            with self.assertRaises(station.StateError):
                self.runner._report("claude", "failed", path, name)
        self.assertEqual(self.fake.names(12), before)
        self.assertFalse([c for c in rest.call_args_list if c.args[0] != "GET"])
        self.assertFalse(self.runner._no_change_finished(REPO, 12, "claude", name))
        self.fake.events[12].append({"event": "labeled", "label": {"name": "claim:alice-example"},
                                    "created_at": "2026-01-01T00:00:00Z"})
        self.assertTrue(self.runner._report("claude", "failed", path, name))
        self.assertIn("loom:pr", self.fake.names(12))
        self.assertEqual(self.runner._no_change_data()["%s#12" % REPO]["count"], 2)

    def test_saved_plan_does_not_override_a_newer_claim_on_replay(self):
        from bus.github import GitHubError
        name = self.claimed_limit_run()
        original = self.runner.gh.rest

        def write(method, path, *args, **kwargs):
            result = original(method, path, *args, **kwargs)
            if method == "PATCH" and path.endswith("/issues/12"):
                raise GitHubError("response lost after body write")
            return result

        with mock.patch.object(self.runner.gh, "rest", side_effect=write):
            with self.assertRaises(GitHubError):
                self.runner._settle_no_change(REPO, 12, "claude", name)
        self.fake.events[12].append({"event": "labeled", "label": {"name": "claim:alice-example"},
                                    "created_at": "2099-01-01T00:00:00Z"})
        before = self.fake.names(12)
        with mock.patch.object(self.runner.gh, "rest", wraps=self.runner.gh.rest) as rest:
            self.runner._settle_no_change(REPO, 12, "claude", name)
        self.assertEqual(self.fake.names(12), before)
        self.assertFalse([c for c in rest.call_args_list if c.args[0] != "GET"])

    def test_pending_claimed_run_preserves_newer_claim_external_claim_and_hold(self):
        for changed in ("newer", "external", "hold", "lane"):
            with self.subTest(changed=changed):
                self.setUp()
                name = self.claimed_limit_run()
                with mock.patch.object(self.runner, "_no_change_plan", return_value=None):
                    self.runner._settle_no_change(REPO, 12, "claude", name)
                issue = self.fake.issues[12]
                if changed == "newer":
                    self.fake.events[12].append({"event": "labeled", "label": {"name": "claim:alice-example"},
                                                "created_at": "2099-01-01T00:00:00Z"})
                else:
                    issue["labels"].append({"name": {"external": "claim:someone-else",
                        "hold": "needs-brain", "lane": "lane:codex"}[changed]})
                before = self.fake.names(12)
                with mock.patch.object(self.runner.gh, "rest", wraps=self.runner.gh.rest) as rest:
                    self.runner._settle_no_change(REPO, 12, "claude", name)
                self.assertEqual(self.fake.names(12), before)
                self.assertFalse([c for c in rest.call_args_list if c.args[0] != "GET"])

    def test_real_ladder_plan_promotes_from_the_current_model(self):
        self.heartbeat("codex")
        self.fake.issues[12]["body"] = "Model: " + factory.CLIMB[0][1] + "\n" + SHIPPED_BODY
        self.fake.issues[12]["labels"] = [{"name": n} for n in
            ("loom:ticket", "loom:running", "lane:claude")]
        plan = self.runner._no_change_plan(REPO, self.fake.issues[12])
        self.assertEqual(plan, {"lane": "codex", "model": factory.CLIMB[1][1], "rung": 2})

    def test_changed_boxes_after_promotion_on_another_lane_reset(self):
        self.answer()
        self.answer()
        self.fake.issues[12]["body"] = self.fake.issues[12]["body"].replace("A test passes", "Different test passes")
        self.fake.issues[12]["labels"] = [{"name": n} for n in
            ("loom:ticket", "loom:running", "lane:codex")]
        self.write(os.path.join(self.home, "logs", "codex", "changed.answered"), "Nothing changed.")
        self.runner._settle_no_change(REPO, 12, "codex", "changed", "Model: model-next\n")
        self.assertIn("loom:queued", self.fake.names(12))
        self.assertEqual(self.runner._no_change_data()["%s#12" % REPO]["count"], 1)

    def test_pending_replay_preserves_external_hold_claim_or_settlement(self):
        for label in ("needs-brain", "needs:human", "wait:operator", "claim:someone-else", "loom:merged", "closed"):
            with self.subTest(label=label):
                self.setUp()
                self.answer()
                self.fake.issues[12]["labels"] = [{"name": n} for n in
                    ("loom:ticket", "loom:running", "lane:codex")]
                self.write(os.path.join(self.home, "logs", "codex", "pending.answered"), "Nothing changed.")
                with mock.patch.object(self.runner, "_no_change_plan", return_value=None):
                    self.runner._settle_no_change(REPO, 12, "codex", "pending")
                if label == "closed":
                    self.fake.issues[12]["state"] = "closed"
                else:
                    self.fake.issues[12]["labels"].append({"name": label})
                before = self.fake.names(12)
                with mock.patch.object(self.runner.gh, "rest", wraps=self.runner.gh.rest) as rest:
                    self.runner._settle_no_change(REPO, 12, "codex", "pending")
                self.assertEqual(self.fake.names(12), before)
                self.assertFalse([c for c in rest.call_args_list if c.args[0] != "GET"])
                self.assertFalse(self.runner._no_change_pending(REPO, 12, "codex", "pending"))

    def test_delayed_fix_without_answer_marker_cannot_charge_newer_commit_budget(self):
        name = "old-fix"
        log = os.path.join(self.home, "logs", "codex", name + ".log")
        self.write(log, "[2026-01-01T00:00:00Z] old-fix failed: agent made no commit and PR text is current on branch\n")
        os.utime(log, ns=(1_000_000_000, 1_000_000_000))
        pushed = os.path.join(self.home, "logs", "codex", "new-commit.pushed")
        self.write(pushed, "pushed")
        os.utime(pushed, ns=(2_000_000_000, 2_000_000_000))
        self.runner._reset_no_change(REPO, 12, "codex", "new-commit")
        self.assertFalse(os.path.exists(os.path.join(self.home, "logs", "codex", name + ".answered")))
        brief = os.path.join(self.home, "briefs", "codex", "failed", name + ".md")
        self.write(brief, "Repo: %s\nIssue: %s#12\nFix-PR: 77\n" % (REPO, REPO))
        before = self.fake.names(12)
        self.assertTrue(self.runner._report("codex", "failed", brief, name))
        self.assertEqual(self.fake.names(12), before)
        entry = self.runner._no_change_data()["%s#12" % REPO]
        self.assertEqual(entry["count"], 0)
        self.assertEqual(entry["runs"]["codex/old-fix"]["action"], "ignore")

    def test_real_failing_check_no_commit_reports_share_budget_before_failure_guard(self):
        self.answer()
        for index, lane in enumerate(("codex", "claude")):
            name = "check-fix-%d" % index
            self.fake.issues[12]["labels"] = [{"name": n} for n in
                ("loom:ticket", "loom:running", "lane:" + lane)]
            log = os.path.join(self.home, "logs", lane, name + ".log")
            self.write(log, "[2026-01-01T00:00:00Z] %s failed: [work] fix round made no commit, "
                       "so the checks it was asked to fix still fail on abcdefabcdef, "
                       "the head CI failed them on: lint; nothing pushed\n" % name)
            brief = os.path.join(self.home, "briefs", lane, "failed", name + ".md")
            self.write(brief, "Repo: %s\nIssue: %s#12\nFix-PR: 77\nModel: %s\n" %
                       (REPO, REPO, "model-next" if index else "model-first"))
            self.assertFalse(os.path.exists(os.path.join(self.home, "logs", lane, name + ".answered")))
            with mock.patch.object(self.runner, "_flag_repeated_failure", side_effect=AssertionError("legacy guard")), \
                    mock.patch.object(self.runner, "_no_change_plan", return_value={
                        "lane": "claude", "model": "model-next", "rung": 2}):
                self.assertTrue(self.runner._report(lane, "failed", brief, name))
            self.assertNotIn("loom:queued", self.fake.names(12))
        self.assertIn("needs-brain", self.fake.names(12))
        self.assertEqual(self.runner._no_change_data()["%s#12" % REPO]["count"], 3)

    def test_check_only_no_commit_keeps_existing_failure_policy(self):
        self.fake.issues[12]["body"] = SHIPPED_BODY.replace("[ ] A test passes", "[x] A test passes")
        name = "checks-only"
        self.write(os.path.join(self.home, "logs", "codex", name + ".log"),
                   "[2026-01-01T00:00:00Z] checks-only failed: [work] fix round made no commit; nothing pushed\n")
        with mock.patch.object(self.runner, "_flag_repeated_failure", return_value=True) as guard:
            self.assertTrue(self.runner._report_failure(REPO, 12, "codex", "Fix-PR: 77\n", name))
        guard.assert_called_once()
        self.assertEqual(self.runner._no_change_data(), {})

    def test_old_model_fix_on_promoted_lane_does_not_fail_active_run(self):
        self.answer()
        self.answer()
        self.fake.issues[12]["labels"] = [{"name": n} for n in
            ("loom:ticket", "loom:running", "lane:claude")]
        name = "old-model-fix"
        self.write(os.path.join(self.home, "logs", "claude", name + ".log"),
                   "[2026-01-01T00:00:00Z] old-model-fix failed: [work] agent made no commit and PR text is current on branch\n")
        brief = os.path.join(self.home, "briefs", "claude", "failed", name + ".md")
        self.write(brief, "Repo: %s\nIssue: %s#12\nFix-PR: 77\nModel: model-first\n" % (REPO, REPO))
        before = self.fake.names(12)
        self.assertTrue(self.runner._report("claude", "failed", brief, name))
        self.assertEqual(self.fake.names(12), before)
        entry = self.runner._no_change_data()["%s#12" % REPO]
        self.assertEqual(entry["count"], 2)
        self.assertEqual(entry["runs"]["claude/old-model-fix"], {"action": "ignore", "done": True})

    def test_completed_outcomes_keep_metric_log_once_each(self):
        with mock.patch.object(station, "_say") as say:
            for index in range(factory.NO_CHANGE_LIMIT + 1):
                lane = "claude" if index == factory.NO_CHANGE_LIMIT else "codex"
                name = self.answer()
                self.runner._settle_no_change(REPO, 12, lane, name)
        lines = [call.args[0] for call in say.call_args_list
                 if "a no-change answer left" in call.args[0]]
        self.assertEqual(len(lines), factory.NO_CHANGE_LIMIT + 1)
        for line, action in zip(lines, ("requeued", "promoted", "held"), strict=True):
            self.assertEqual(line, "%s#12: a no-change answer left 1 box(es) unproven, %s" % (REPO, action))


class NoChangeWaitingReportTest(Base):
    """A promote step with no plan is reported once; the station pass retries it."""

    def setUp(self):
        super().setUp()
        self.fake.add(12, body=SHIPPED_BODY, state="running")
        self.runner = station.Station(GitHub("token", transport=self.fake), self.settings)
        self.runner.now = 1_000_000.0
        self.fake.add(77, body="Pull request", state="pr")
        self.name = "waiting-run"
        self.brief = os.path.join(self.home, "waiting-brief.md")
        self.write(self.brief, "Repo: %s\nIssue: %s#12\nLane: codex\n" % (REPO, REPO))
        # Spend the retries below the limit so the next answer promotes.
        with mock.patch.object(self.runner, "_no_change_plan", return_value=None):
            for index in range(factory.NO_CHANGE_LIMIT - 1):
                name = "spent-%d" % index
                self.write(os.path.join(self.home, "logs", "codex", name + ".answered"), "Nothing changed.")
                self.relabel_running()
                self.runner._settle_no_change(REPO, 12, "codex", name)
        self.write(os.path.join(self.home, "logs", "codex", self.name + ".answered"), "Nothing changed.")

    def relabel_running(self):
        self.fake.issues[12]["labels"] = [{"name": n} for n in
            ("loom:ticket", "loom:running", "lane:codex")]

    def report(self, plan):
        self.relabel_running()
        with mock.patch.object(self.runner, "_no_change_plan", return_value=plan):
            return self.runner._report("codex", "done", self.brief, self.name)

    def comment_reads(self, call):
        with mock.patch.object(self.runner.gh, "rest", wraps=self.runner.gh.rest) as rest:
            call()
        return [c for c in rest.call_args_list
                if c.args[0] == "GET" and "/comments" in str(c.args[1])]

    def test_second_report_pass_does_not_repeat_the_report(self):
        self.assertTrue(self.report(None))
        self.assertIn("wait:no-change", self.fake.names(12))
        reads = self.comment_reads(lambda: self.assertTrue(self.report(None)))
        self.assertEqual(reads, [])

    def test_later_pass_with_a_plan_promotes_the_ticket(self):
        self.assertTrue(self.report(None))
        self.relabel_running()
        self.runner.now += station.NO_CHANGE_RETRY_EVERY
        plan = {"lane": "claude", "model": "model-next", "rung": 2}
        with mock.patch.object(self.runner, "_no_change_plan", return_value=plan):
            self.runner.retry_waiting_no_change()
        self.assertIn("loom:briefed", self.fake.names(12))
        self.assertIn("Model: model-next", self.fake.issues[12]["body"])
        self.assertNotIn("wait:no-change", self.fake.names(12))
        step = self.runner._no_change_data()["%s#12" % REPO]["runs"]["codex/" + self.name]
        self.assertTrue(step["done"])
        with mock.patch.object(self.runner, "_no_change_plan", return_value=plan) as planner:
            self.runner.retry_waiting_no_change()
        planner.assert_not_called()

    def test_wait_label_stays_while_no_plan_exists(self):
        self.assertTrue(self.report(None))
        with mock.patch.object(self.runner, "_no_change_plan", return_value=None):
            self.runner.retry_waiting_no_change()
        self.assertIn("wait:no-change", self.fake.names(12))
        self.assertNotIn("loom:briefed", self.fake.names(12))
        step = self.runner._no_change_data()["%s#12" % REPO]["runs"]["codex/" + self.name]
        self.assertFalse(step["done"])
        self.assertTrue(step["waiting"])

    def reads_for_ticket(self, call):
        with mock.patch.object(self.runner.gh, "rest", wraps=self.runner.gh.rest) as rest:
            call()
        return [c for c in rest.call_args_list
                if c.args[0] == "GET" and "/issues/12" in str(c.args[1])]

    def test_retry_is_throttled_per_waiting_step(self):
        self.assertTrue(self.report(None))
        self.runner.now += station.NO_CHANGE_RETRY_EVERY - 1
        with mock.patch.object(self.runner, "_no_change_plan", return_value=None) as planner:
            reads = self.reads_for_ticket(self.runner.report_outcomes)
        self.assertEqual(reads, [])
        planner.assert_not_called()

    def test_retry_after_the_window_promotes_given_a_plan(self):
        self.assertTrue(self.report(None))
        self.relabel_running()
        self.runner.now += station.NO_CHANGE_RETRY_EVERY + 1
        plan = {"lane": "claude", "model": "model-next", "rung": 2}
        with mock.patch.object(self.runner, "_no_change_plan", return_value=plan):
            self.runner.report_outcomes()
        self.assertIn("loom:briefed", self.fake.names(12))

    def test_failed_retry_restarts_the_window(self):
        self.assertTrue(self.report(None))
        self.runner.now += station.NO_CHANGE_RETRY_EVERY + 1
        with mock.patch.object(self.runner, "_no_change_plan", return_value=None):
            self.runner.report_outcomes()
        self.runner.now += 1
        with mock.patch.object(self.runner, "_no_change_plan", return_value=None) as planner:
            self.runner.report_outcomes()
        planner.assert_not_called()

    def test_retry_hands_the_briefs_model_back_to_the_budget(self):
        self.write(self.brief, "Repo: %s\nIssue: %s#12\nLane: codex\nModel: model-x\n" % (REPO, REPO))
        self.assertTrue(self.report(None))
        self.relabel_running()
        self.runner.now += station.NO_CHANGE_RETRY_EVERY + 1
        with mock.patch.object(self.runner, "_no_change_budget",
                               wraps=self.runner._no_change_budget) as budget:
            with mock.patch.object(self.runner, "_no_change_plan", return_value=None):
                self.runner.retry_waiting_no_change()
        self.assertEqual(budget.call_args.args[-1], "model-x")

    def test_retry_that_raises_restarts_the_window(self):
        self.assertTrue(self.report(None))
        self.runner.now += station.NO_CHANGE_RETRY_EVERY + 1
        with mock.patch.object(self.runner, "_no_change_planning_issue",
                               side_effect=station.StateError("cannot establish claim timing")):
            self.runner.report_outcomes()
        self.runner.now += 1
        with mock.patch.object(self.runner, "_no_change_plan", return_value=None) as planner:
            reads = self.reads_for_ticket(self.runner.report_outcomes)
        self.assertEqual(reads, [])
        planner.assert_not_called()

    def wait_second_ticket(self):
        self.fake.add(13, body=SHIPPED_BODY, state="running")
        labels = [{"name": n} for n in ("loom:ticket", "loom:running", "lane:codex")]
        with mock.patch.object(self.runner, "_no_change_plan", return_value=None):
            for name in ("spent-13", "waiting-13"):
                self.fake.issues[13]["labels"] = list(labels)
                self.write(os.path.join(self.home, "logs", "codex", name + ".answered"), "Nothing changed.")
                self.runner._settle_no_change(REPO, 13, "codex", name)

    def test_one_pass_keeps_the_first_promotion_and_stamps_the_second_step(self):
        self.assertTrue(self.report(None))
        self.wait_second_ticket()
        self.relabel_running()
        self.fake.issues[13]["labels"] = [{"name": n} for n in ("loom:ticket", "loom:running", "lane:codex")]
        self.runner.now += station.NO_CHANGE_RETRY_EVERY + 1
        plan = {"lane": "claude", "model": "model-next", "rung": 2}
        real = self.runner._no_change_planning_issue

        def planning(repo, issue, lane, name):
            if int(issue["number"]) == 13:
                raise station.StateError("cannot establish claim timing")
            return real(repo, issue, lane, name)

        with mock.patch.object(self.runner, "_no_change_planning_issue", side_effect=planning), \
                mock.patch.object(self.runner, "_no_change_plan", return_value=plan):
            self.runner.report_outcomes()
        runs = self.runner._no_change_data()
        first = runs["%s#12" % REPO]["runs"]["codex/" + self.name]
        second = runs["%s#13" % REPO]["runs"]["codex/waiting-13"]
        self.assertTrue(first["done"])
        self.assertEqual(first["plan"], plan)
        self.assertTrue(second["waiting"])
        self.assertEqual(second["tried_at"], self.runner.now)

    def test_fix_path_waiting_step_promotes_on_a_later_pass(self):
        self.relabel_running()
        with mock.patch.object(self.runner, "_no_change_plan", return_value=None):
            self.runner._nothing_to_fix(REPO, 12, 77, "codex", self.name)
        self.assertIn("wait:no-change", self.fake.names(12))
        step = self.runner._no_change_data()["%s#12" % REPO]["runs"]["codex/" + self.name]
        self.assertEqual(step["fix"], 77)
        self.relabel_running()
        self.runner.now += station.NO_CHANGE_RETRY_EVERY + 1
        plan = {"lane": "claude", "model": "model-next", "rung": 2}
        with mock.patch.object(self.runner, "_no_change_plan", return_value=plan):
            self.runner.retry_waiting_no_change()
        self.assertIn("loom:pr", self.fake.names(12))
        self.assertNotIn("wait:no-change", self.fake.names(12))
        step = self.runner._no_change_data()["%s#12" % REPO]["runs"]["codex/" + self.name]
        self.assertTrue(step["done"])
        for key in ("waiting", "fix", "tried_at", "model"):
            self.assertNotIn(key, step)


from bus import no_change
from tests.test_bus_factory import Base as FactoryBase, FIX_ASKED, NO_CHANGE, ME, iso


class FactoryBoxBudgetTest(FactoryBase):
    def test_box_budget_prevents_legacy_check_hold_and_second_climb(self):
        self.gh.ticket(5, "pr", lane="claude")
        self.gh.issues[5]["body"] += "\n## Acceptance criteria\n- [ ] A test passes\n"
        self.gh.pull(50, 5, resolved=False, green=False)
        for n, text in enumerate((FIX_ASKED, NO_CHANGE, FIX_ASKED, NO_CHANGE, no_change.MARKER)):
            self.gh.comments[5].append({"body": text, "user": ME, "created_at": iso(20000 + n)})
        with mock.patch.object(factory.Factory, "_climb_or_stop", side_effect=AssertionError("second climb")):
            self.go()
        self.assertEqual(self.humans, [])
        self.assertEqual(len(self.fixed), 1)
