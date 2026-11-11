"""Merged interactive branches reconcile through the public station pass."""

from unittest import mock

from bus import station
from bus.github import GitHub
from tests.test_bus_station import AUTHORS, BODY_WITH_CRITERIA, Base, REPO, reply


class MergedNonLaneBranch(Base):
    def merged_pull(self):
        return {
            "number": 99,
            "state": "closed",
            "merged_at": "2030-01-02T00:00:00Z",
            "merge_commit_sha": "a" * 40,
            "user": {"login": AUTHORS[0]},
            "body": (BODY_WITH_CRITERIA.replace("- [ ]", "- [x]")
                     + "\nRefs %s#5\n" % REPO),
            "head": {"ref": "muse/5-x", "repo": {"full_name": REPO}},
            "base": {"ref": "main"},
        }

    def reconcile(self):
        station.Station(GitHub("token", transport=self.fake),
                        self.settings).reconcile_pull_requests()

    def test_trusted_merged_branch_delivers_queued_ticket_and_releases_lane(self):
        self.fake.add(5, state="queued", body=BODY_WITH_CRITERIA)
        self.fake.pulls = [self.merged_pull()]
        self.reconcile()
        self.assertIn("loom:merged", self.fake.names(5))
        self.assertNotIn("lane:codex", self.fake.names(5))

    def test_trusted_merge_delivers_remaining_active_ticket_states(self):
        for state in ("briefed", "running", "pr", "answered", "failed"):
            with self.subTest(state=state):
                self.fake.add(5, state=state, body=BODY_WITH_CRITERIA)
                self.fake.pulls = [self.merged_pull()]
                self.reconcile()
                self.assertIn("loom:merged", self.fake.names(5))
                self.assertNotIn("loom:" + state, self.fake.names(5))
                self.assertNotIn("lane:codex", self.fake.names(5))

    def test_non_lane_merge_is_logged_once_with_commit(self):
        for state in ("running", "pr"):
            with self.subTest(state=state):
                self.fake.add(5, state=state, body=BODY_WITH_CRITERIA)
                self.fake.pulls = [self.merged_pull()]
                with mock.patch("bus.station._say") as say:
                    self.reconcile()
                    self.reconcile()
                lines = [str(call.args[0]) for call in say.call_args_list
                         if "merged from a non-lane branch" in str(call.args[0])]
                self.assertEqual(len(lines), 1)
                self.assertIn("#99", lines[0])
                self.assertIn("a" * 40, lines[0])

    def test_untrusted_merge_does_not_deliver_an_active_ticket(self):
        for state in ("queued", "running", "failed"):
            with self.subTest(state=state):
                self.fake.add(5, state=state, body=BODY_WITH_CRITERIA)
                pr = self.merged_pull()
                pr["user"] = {"login": "untrusted-example"}
                self.fake.pulls = [pr]
                self.reconcile()
                self.assertIn("loom:" + state, self.fake.names(5))
                self.assertIn("lane:codex", self.fake.names(5))

    def test_cross_repository_refs_do_not_deliver_ticket(self):
        self.fake.add(5, state="running", body=BODY_WITH_CRITERIA)
        pr = self.merged_pull()
        pr["body"] = pr["body"].replace(REPO, "other-owner/other-repo")
        self.fake.pulls = [pr]
        self.reconcile()
        self.assertIn("loom:running", self.fake.names(5))

    def test_fork_merge_does_not_deliver_ticket(self):
        self.fake.add(5, state="failed", body=BODY_WITH_CRITERIA)
        pr = self.merged_pull()
        pr["head"]["repo"]["full_name"] = "other-owner/other-repo"
        self.fake.pulls = [pr]
        self.reconcile()
        self.assertIn("loom:failed", self.fake.names(5))

    def test_merge_before_requeue_does_not_deliver_ticket(self):
        self.fake.add(5, state="running", body=BODY_WITH_CRITERIA)
        self.fake.events[5].append({"event": "labeled",
                                   "label": {"name": "loom:queued"},
                                   "created_at": "2030-01-03T00:00:00Z"})
        self.fake.pulls = [self.merged_pull()]
        self.reconcile()
        self.assertIn("loom:running", self.fake.names(5))
        self.assertIn("lane:codex", self.fake.names(5))

    def test_unbuilt_criteria_stay_queued_instead_of_becoming_merged(self):
        self.fake.add(5, state="running", body=BODY_WITH_CRITERIA)
        pr = self.merged_pull()
        pr["body"] = pr["body"].replace("- [x]", "- [ ]")
        self.fake.pulls = [pr]
        self.reconcile()
        self.assertIn("loom:queued", self.fake.names(5))
        self.assertNotIn("loom:merged", self.fake.names(5))

    def test_merge_does_not_clear_person_or_blocked_holds(self):
        self.fake.add(5, state="running", body=BODY_WITH_CRITERIA)
        self.fake.issues[5]["labels"].extend(
            [{"name": "needs:human"}, {"name": "blocked"}])
        self.fake.pulls = [self.merged_pull()]
        self.reconcile()
        self.assertIn("needs:human", self.fake.names(5))
        self.assertIn("blocked", self.fake.names(5))

    def test_feature_branch_merge_does_not_deliver_ticket(self):
        for state in ("queued", "running", "failed", "pr"):
            with self.subTest(state=state):
                self.fake.add(5, state=state, body=BODY_WITH_CRITERIA)
                pr = self.merged_pull()
                pr["base"] = {"ref": "feature/stack"}
                self.fake.pulls = [pr]
                self.reconcile()
                self.assertIn("loom:" + state, self.fake.names(5))
                self.assertIn("lane:codex", self.fake.names(5))

    def test_missing_base_does_not_deliver_ticket(self):
        self.fake.add(5, state="running", body=BODY_WITH_CRITERIA)
        pr = self.merged_pull()
        del pr["base"]
        self.fake.pulls = [pr]
        self.reconcile()
        self.assertIn("loom:running", self.fake.names(5))

    def test_non_main_default_branch_uses_cached_repository_metadata(self):
        self.fake.add(5, state="running", body=BODY_WITH_CRITERIA)
        pr = self.merged_pull()
        pr["base"] = {"ref": "release"}
        self.fake.pulls = [pr]
        reads = []

        def transport(method, url, headers, body):
            if method == "GET" and url.endswith("/repos/" + REPO):
                reads.append(url)
                return reply(200, {"full_name": REPO, "default_branch": "release"})
            return self.fake(method, url, headers, body)

        station.Station(GitHub("token", transport=transport),
                        self.settings).reconcile_pull_requests()
        self.assertIn("loom:merged", self.fake.names(5))
        self.assertEqual(len(reads), 1)

    def test_unknown_default_branch_does_not_deliver_ticket(self):
        self.fake.add(5, state="failed", body=BODY_WITH_CRITERIA)
        self.fake.pulls = [self.merged_pull()]

        def transport(method, url, headers, body):
            if method == "GET" and url.endswith("/repos/" + REPO):
                return reply(200, {"full_name": REPO})
            return self.fake(method, url, headers, body)

        station.Station(GitHub("token", transport=transport),
                        self.settings).reconcile_pull_requests()
        self.assertIn("loom:failed", self.fake.names(5))

    def test_timeline_merge_is_revalidated_against_real_pull(self):
        for case in ("feature base", "missing base", "foreign source", "untrusted pull",
                     "foreign head", "changed refs", "not merged", "valid"):
            with self.subTest(case=case):
                self.fake.add(5, state="pr", body=BODY_WITH_CRITERIA)
                pr = self.merged_pull()
                source_repo = REPO
                if case == "feature base":
                    pr["base"]["ref"] = "feature/stack"
                elif case == "missing base":
                    del pr["base"]
                elif case == "foreign source":
                    source_repo = "other-owner/other-repo"
                elif case == "untrusted pull":
                    pr["user"]["login"] = "untrusted-example"
                elif case == "foreign head":
                    pr["head"]["repo"]["full_name"] = "other-owner/other-repo"
                elif case == "changed refs":
                    pr["body"] = "Refs #6"
                elif case == "not merged":
                    pr["merged_at"] = None
                self.fake.timelines = {5: [{"event": "cross-referenced", "source": {"issue": {
                    "number": 99, "state": "closed", "user": {"login": AUTHORS[0]},
                    "repository_url": "https://api.github.com/repos/" + source_repo,
                    "pull_request": {"merged_at": "2030-01-02T00:00:00Z"},
                    "body": "Refs %s#5" % REPO}}}]}

                def transport(method, url, headers, body):
                    if method == "GET" and url.endswith("/pulls/99"):
                        return reply(200, pr)
                    return self.fake(method, url, headers, body)

                self.fake.pulls = []
                station.Station(GitHub("token", transport=transport),
                                self.settings).reconcile_pull_requests()
                if case == "valid":
                    self.assertIn("loom:merged", self.fake.names(5))
                else:
                    self.assertNotIn("loom:merged", self.fake.names(5))

    def test_non_lane_pr_state_rejects_untrusted_or_missing_author(self):
        for author in ("untrusted-example", None):
            with self.subTest(author=author):
                self.fake.add(5, state="pr", body=BODY_WITH_CRITERIA)
                pr = self.merged_pull()
                pr["user"] = {"login": author} if author else {}
                self.fake.pulls = [pr]
                self.reconcile()
                self.assertNotIn("loom:merged", self.fake.names(5))
                self.assertIn("lane:codex", self.fake.names(5))

    def test_non_lane_pr_state_requires_same_repository_head(self):
        for head_repo in ("other-owner/other-repo", None):
            with self.subTest(head_repo=head_repo):
                self.fake.add(5, state="pr", body=BODY_WITH_CRITERIA)
                pr = self.merged_pull()
                pr["head"]["repo"] = {"full_name": head_repo} if head_repo else {}
                self.fake.pulls = [pr]
                self.reconcile()
                self.assertNotIn("loom:merged", self.fake.names(5))
                self.assertIn("lane:codex", self.fake.names(5))
