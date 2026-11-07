"""Eligibility is decided before a verify lane spends a run."""
from unittest import mock

from bus import station
from bus.github import GitHub, GitHubError
from tests.test_bus_station import Base, REPO, SHIPPED_BODY


class VerifyEligibilityTest(Base):
    def setUp(self):
        super().setUp()
        self.fake.add(5, lane="codex", body=SHIPPED_BODY, state="shipped")
        self.result_number = 0
        self.runner = station.Station(GitHub("token", transport=self.fake), self.settings)

    def pool(self):
        with mock.patch.object(self.runner, "_unmeetable_live", return_value=False):
            return self.runner._verify_pool({}, queued_holds=False)[0]

    def missing(self, reason="The live event has not happened yet.", name=None):
        self.result_number += 1
        name = name or "example-verify-%d" % self.result_number
        with mock.patch.object(self.runner, "_read_verify", return_value=({"missing": reason}, "")):
            self.runner._report_verify("codex", "done", REPO, 5, name)

    def test_no_criteria_is_excluded_and_sent_to_rewrite_once(self):
        self.fake.issues[5]["body"] = "No acceptance criteria."
        self.assertEqual(station.verify_due([(REPO, self.fake.issues[5])], {}, 99999), [])
        self.assertEqual(self.pool(), [])
        self.assertIn("loom:triage", self.fake.names(5))
        self.assertNotIn("loom:shipped", self.fake.names(5))
        writes = len(self.fake.paths("PUT"))
        self.assertEqual(self.pool(), [])
        self.assertEqual(len(self.fake.paths("PUT")), writes)

    def test_repeated_missing_reaches_cap_and_hands_off_once(self):
        for _ in range(station.VERIFY_REFUSAL_CAP - 1):
            self.missing()
        self.assertNotIn("needs-brain", self.fake.names(5))
        self.missing()
        self.assertIn("needs-brain", self.fake.names(5))
        self.assertEqual(self.pool(), [])
        self.assertIn("loom:shipped", self.fake.names(5))
        posts = len(self.fake.comments[5])
        self.missing()
        self.assertEqual(len(self.fake.comments[5]), posts)

    def test_distinct_missing_reasons_do_not_add_up_to_the_cap(self):
        for number in range(station.VERIFY_REFUSAL_CAP):
            self.missing("The live event number %d has not happened yet." % number)
        self.assertNotIn("needs-brain", self.fake.names(5))

    def test_confirmed_no_merge_returns_to_factory(self):
        self.assertEqual(self.pool(), [])
        self.assertIn("loom:queued", self.fake.names(5))
        self.assertNotIn("lane:codex", self.fake.names(5))

    def test_unreadable_merge_facts_do_not_return_to_factory(self):
        with mock.patch.object(self.runner, "_merged_pull_facts", return_value=("", [], "status 503")):
            self.assertEqual(self.pool(), [])
        self.assertIn("loom:shipped", self.fake.names(5))
        self.assertEqual(self.fake.paths("PUT"), [])

    def test_late_missing_after_rewrite_does_not_change_labels(self):
        self.fake.issues[5]["labels"] = [{"name": "loom:ticket"}, {"name": "loom:triage"}]
        self.missing()
        self.assertNotIn("loom:awaiting-evidence", self.fake.names(5))

    def test_a_merged_ticket_remains_eligible(self):
        with mock.patch.object(self.runner, "_merged_pull_facts", return_value=("main", [{"number": 9}], "")):
            self.assertEqual([issue["number"] for _, issue in self.pool()], [5])
        self.assertIn("loom:shipped", self.fake.names(5))

    def test_incomplete_history_is_unknown_not_absence(self):
        with mock.patch.object(self.runner, "_merged_pull_facts", return_value=("main", [], "")):
            self.runner._closed_complete[REPO] = False
            self.assertEqual(self.pool(), [])
        self.assertIn("loom:shipped", self.fake.names(5))

    def test_a_proven_ticket_is_not_requeued(self):
        with mock.patch.object(self.runner, "_verify_proof_state", return_value=True), \
                mock.patch.object(self.runner, "_merged_pull_facts") as reads:
            self.assertEqual(self.pool(), [])
        reads.assert_not_called()
        self.assertIn("loom:shipped", self.fake.names(5))

    def test_no_criteria_with_existing_proof_is_not_rewritten(self):
        self.fake.issues[5]["body"] = "No acceptance criteria."
        with mock.patch.object(self.runner, "_verify_proof_state", return_value=True):
            self.assertEqual(self.pool(), [])
        self.assertIn("loom:shipped", self.fake.names(5))

    def test_cooldown_does_not_read_merge_facts(self):
        with mock.patch.object(self.runner, "_merged_pull_facts") as reads:
            found, _ = self.runner._verify_pool({"%s#5" % REPO: self.runner.now}, queued_holds=False)
        self.assertEqual(found, [])
        reads.assert_not_called()

    def test_capped_missing_answers_are_excluded_by_the_pure_filter(self):
        key = "%s#5" % REPO
        issue = self.fake.issues[5]
        reasons = {"not yet observed": station.VERIFY_REFUSAL_CAP - 1}
        self.assertEqual(len(station.verify_due([(REPO, issue)], {}, 99999, missing={key: reasons})), 1)
        reasons["not yet observed"] += 1
        self.assertEqual(station.verify_due([(REPO, issue)], {}, 99999, missing={key: reasons}), [])

    def test_boxes_outside_acceptance_section_are_not_criteria(self):
        self.fake.issues[5]["body"] = "## Notes\n- [ ] A thing to remember\n"
        self.assertEqual(self.pool(), [])
        self.assertIn("loom:triage", self.fake.names(5))

    def test_no_criteria_never_dispatches(self):
        self.fake.issues[5]["body"] = "No acceptance criteria."
        with mock.patch.object(self.runner, "_write_verify_brief") as dispatch:
            self.runner._hand_verifies(["codex"])
        dispatch.assert_not_called()

    def test_malformed_merge_link_fields_do_not_prove_absence(self):
        self.fake.pulls = [{"number": 9, "merged_at": "2029-01-01T00:00:00Z",
                            "merge_commit_sha": "a" * 40, "state": "closed"}]
        self.assertEqual(self.pool(), [])
        self.assertIn("loom:shipped", self.fake.names(5))
        self.assertFalse(self.runner._closed_complete[REPO])

    def test_malformed_merge_revision_does_not_prove_absence(self):
        self.fake.pulls = [{"number": 9, "merged_at": "2029-01-01T00:00:00Z",
                            "merge_commit_sha": None, "state": "closed", "body": "Refs #5",
                            "head": {"ref": "example-change"}}]
        self.assertEqual(self.pool(), [])
        self.assertIn("loom:shipped", self.fake.names(5))

    def test_missing_cap_survives_the_next_station_pass(self):
        for _ in range(station.VERIFY_REFUSAL_CAP):
            self.missing()
        self.runner = station.Station(GitHub("token", transport=self.fake), self.settings)
        self.assertEqual(self.pool(), [])
        self.assertIn("loom:shipped", self.fake.names(5))

    def test_adapter_missing_answers_share_the_cap(self):
        with mock.patch.object(self.runner, "_adapter_can_verify", return_value=True), \
                mock.patch.object(self.runner, "_adapter_human_ask") as ask:
            for _ in range(station.VERIFY_REFUSAL_CAP):
                self.missing()
        self.assertEqual(ask.call_count, station.VERIFY_REFUSAL_CAP - 1)
        self.assertIn("needs-brain", self.fake.names(5))

    def test_clearing_refusals_clears_missing_history_too(self):
        self.missing()
        self.runner._forget_refusals(REPO, 5)
        self.assertEqual(self.runner._verify_missing_ledger(), {})

    def test_health_proof_still_prioritizes_an_eligible_ticket(self):
        second = dict(self.fake.issues[5], number=6, health_proof="absent", shipped_at=2)
        first = dict(self.fake.issues[5], shipped_at=1)
        self.assertEqual([i["number"] for _, i in station.verify_due(
            [(REPO, first), (REPO, second)], {}, 99999)], [6, 5])

    def test_handoff_retries_failed_label_without_duplicate_comment(self):
        for _ in range(station.VERIFY_REFUSAL_CAP - 1):
            self.missing()
        with mock.patch.object(self.runner, "_add_label", side_effect=GitHubError("label failed")):
            with self.assertRaises(GitHubError):
                self.missing()
        self.assertEqual(len(self.fake.comments[5]), 1)
        self.missing()
        self.assertEqual(len(self.fake.comments[5]), 1)
        self.assertIn("needs-brain", self.fake.names(5))

    def test_replayed_result_does_not_spend_another_attempt(self):
        with mock.patch.object(self.runner, "_adapter_can_verify", return_value=True), \
                mock.patch.object(self.runner, "_adapter_human_ask", side_effect=GitHubError("ask failed")):
            for _ in range(station.VERIFY_REFUSAL_CAP):
                with self.assertRaises(GitHubError):
                    self.missing(name="one-verify-run")
        self.assertNotIn("needs-brain", self.fake.names(5))
        record = self.runner._verify_missing_ledger()["%s#5" % REPO]
        self.assertEqual(list(record["reasons"].values()), [1])

    def test_unknown_ticket_body_never_routes(self):
        for body in ({}, 42):
            self.fake.issues[5]["body"] = body
            self.assertEqual(self.pool(), [])
            self.assertEqual(station.verify_due([(REPO, self.fake.issues[5])], {}, 99999), [])
        self.fake.issues[5].pop("body")
        self.assertEqual(self.pool(), [])
        self.assertIn("loom:shipped", self.fake.names(5))
        self.assertEqual(self.fake.paths("PUT"), [])

    def test_explicit_empty_body_goes_to_rewrite(self):
        self.fake.issues[5]["body"] = None
        self.assertEqual(self.pool(), [])
        self.assertIn("loom:triage", self.fake.names(5))

    def test_malformed_next_link_is_unknown(self):
        from bus.github import Response
        response = Response(200, [], None, {"link": '<broken; rel="next"'})
        following, complete = self.runner._closed_next(REPO, response, 0)
        self.assertIsNone(following)
        self.assertFalse(complete)

    def test_full_page_without_link_probes_again(self):
        from bus.github import Response
        response = Response(304, [{}] * 100, None, {})
        following, complete = self.runner._closed_next(REPO, response, 0)
        self.assertTrue(complete)
        self.assertTrue(following.endswith("page=2"))

    def test_prose_live_without_a_merge_returns_to_factory(self):
        self.fake.issues[5]["body"] = "## Acceptance criteria\n- [ ] Live: it works correctly\n"
        self.assertEqual(self.pool(), [])
        self.assertIn("loom:queued", self.fake.names(5))

    def test_missing_merge_routes_before_unmeetable_live_check(self):
        with mock.patch.object(self.runner, "_unmeetable_live", return_value=True) as check:
            self.assertEqual(self.runner._verify_pool({}, queued_holds=False)[0], [])
        check.assert_not_called()
        self.assertIn("loom:queued", self.fake.names(5))

    def test_malformed_head_is_unknown_without_aborting_pool(self):
        self.fake.pulls = [{"number": 9, "merged_at": "2029-01-01T00:00:00Z",
                            "merge_commit_sha": "a" * 40, "state": "closed",
                            "body": "No linked ticket.", "head": ["malformed"]}]
        self.assertEqual(self.pool(), [])
        self.assertIn("loom:shipped", self.fake.names(5))

    def test_malformed_falsey_merge_state_is_unknown(self):
        for value in (False, 0, "", []):
            self.assertEqual(self.runner._closed_page([{"merged_at": value}]), ([], False))

    def test_malformed_proof_history_never_routes(self):
        from bus.github import Response
        self.fake.issues[5]["body"] = "No acceptance criteria."
        original = self.runner.gh.rest

        def read(method, path, *args, **kwargs):
            if method == "GET" and "/issues/5/comments" in path:
                return Response(200, {"message": "incomplete"}, None, {})
            return original(method, path, *args, **kwargs)

        with mock.patch.object(self.runner.gh, "rest", side_effect=read):
            self.assertEqual(self.pool(), [])
        self.assertIn("loom:shipped", self.fake.names(5))
        self.assertEqual(self.fake.paths("PUT"), [])
