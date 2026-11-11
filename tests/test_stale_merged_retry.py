"""An incomplete merge cannot stand in for a retry's pull request."""
from copy import deepcopy
from unittest import mock

from bus import station, tickets
from bus.github import GitHub
from test_bus_station import Base, REPO
from shadow_env import setUpModule, tearDownModule  # noqa: F401


class StaleMergedRetryTest(Base):
    def incomplete_merge(self):
        self.fake.add(12, state="pr", body=(
            "## Acceptance criteria\n- [ ] First works.\n- [ ] Second works.\n"))
        old = {"number": 99, "state": "closed", "merged_at": "2030-01-02T00:00:00Z",
               "user": {"login": "alice-example"},
               "html_url": "https://example.com/pull/99",
               "repository_url": "https://api.github.com/repos/" + REPO,
               "body": "Refs %s#12\n## Acceptance criteria\n- [x] First works.\n" % REPO,
               "head": {"ref": "loom/12-fix-the-thing", "repo": {"full_name": REPO}}}
        runner = self.reader()
        runner._reconcile_pr_ticket(REPO, old, {12: self.fake.issues[12]})
        self.assertIn("loom:queued", self.fake.names(12))
        self.assertTrue(any("left ticket criteria unbuilt" in c["body"]
                            for c in self.fake.comments[12]))
        return old

    def reader(self):
        return station.Station(GitHub("token", transport=self.fake), self.settings,
                               now=4102444800.0)

    def report(self, pulls):
        self.fake.pulls = pulls
        self.fake.issues[12]["labels"] = [{"name": n} for n in
                                          ("loom:ticket", "loom:running", "lane:codex")]
        reader = self.reader()
        with mock.patch.object(reader, "_answered", return_value=False), \
                mock.patch.object(reader, "_done_without_pr", return_value=False) as no_pr:
            result = reader._report_done(REPO, 12, "codex",
                                        "Branch: loom/12-fix-the-thing\n", "/unused", "retry")
        return result, no_pr

    def test_retry_ignores_rejected_merge_and_accepts_its_new_open_pull(self):
        old = self.incomplete_merge()
        result, no_pr = self.report([old])
        self.assertFalse(result)
        no_pr.assert_called_once()
        self.assertNotIn("loom:pr", self.fake.names(12))
        self.assertFalse(any("opened a pull request" in c["body"]
                             for c in self.fake.comments[12]))
        self.assertTrue(any("already rejected for unfinished work" in c["body"]
                            for c in self.fake.comments[12]))
        new = dict(old, number=100, state="open", merged_at=None,
                   html_url="https://example.com/pull/100")
        result, no_pr = self.report([old, new])
        self.assertTrue(result)
        no_pr.assert_not_called()
        self.assertIn("loom:pr", self.fake.names(12))
        self.assertTrue(any("https://example.com/pull/100" in c["body"]
                            for c in self.fake.comments[12]))

    def reconcile(self, old, source, rejected=True):
        self.fake.issues[12]["labels"] = [{"name": n} for n in ("loom:ticket", "loom:pr")]
        events = [{"event": "labeled", "label": {"name": "loom:pr"},
                   "created_at": "2030-01-05T00:00:00Z"}]
        closed = []
        if source == "timeline":
            self.fake.pulls = [old]
            pull = deepcopy(old)
            pull["pull_request"] = {"merged_at": pull.pop("merged_at")}
            events.append({"event": "cross-referenced", "source": {"issue": pull}})
        else:
            closed = [old]
        self.fake.timelines = {12: events}
        if not rejected:
            self.fake.comments[12] = []
        reader = self.reader()
        reader._clear_pull_gone(REPO, 12, self.fake.issues[12], [], closed)

    def test_closed_list_reconciliation_preserves_the_rejection(self):
        old = self.incomplete_merge()
        self.reconcile(old, "closed")
        self.assertIn("loom:queued", self.fake.names(12))
        self.assertNotIn("loom:merged", self.fake.names(12))
        self.reconcile(old, "closed", rejected=False)
        self.assertIn("loom:merged", self.fake.names(12))

    def test_timeline_reconciliation_preserves_the_rejection(self):
        old = self.incomplete_merge()
        self.reconcile(old, "timeline")
        self.assertIn("loom:queued", self.fake.names(12))
        self.assertNotIn("loom:merged", self.fake.names(12))
        self.reconcile(old, "timeline", rejected=False)
        self.assertIn("loom:merged", self.fake.names(12))

    def test_rejected_merge_cannot_deliver_after_criteria_text_changes(self):
        old = self.incomplete_merge()
        self.fake.issues[12]["labels"] = [{"name": n} for n in ("loom:ticket", "loom:running")]
        # A later edit of the old PR's boxes does not undo its recorded rejection.
        old["body"] += "- [x] Second works.\n"
        self.reader()._reconcile_pr_ticket(REPO, old, {12: self.fake.issues[12]})
        self.assertIn("loom:running", self.fake.names(12))
        self.assertNotIn("loom:merged", self.fake.names(12))

    def test_aged_retry_without_a_current_pull_follows_the_real_failure_path(self):
        old = self.incomplete_merge()
        self.fake.pulls = [old]
        self.fake.issues[12]["labels"] = [{"name": n} for n in
                                          ("loom:ticket", "loom:running", "lane:codex")]
        path = self.inbox("codex", "done", "retry.md")
        self.write(path, "Branch: loom/12-fix-the-thing\n")
        reader = self.reader()  # This clock is well past the brief's real mtime.
        with mock.patch.object(reader, "_answered", return_value=False):
            self.assertTrue(reader._report_done(REPO, 12, "codex",
                                               "Branch: loom/12-fix-the-thing\n", path, "retry"))
        self.assertIn("loom:failed", self.fake.names(12))
        self.assertNotIn("loom:pr", self.fake.names(12))

    def test_refuted_merge_is_rejected_but_pending_verification_is_not(self):
        old = self.incomplete_merge()
        comment = {"id": 501, "user": {"login": "alice-example"},
                   "body": tickets.REFUTED_NOTE % (99, "the second check still fails")}
        self.fake.comments[12] = [comment]
        result, no_pr = self.report([old])
        self.assertFalse(result)
        no_pr.assert_called_once()
        self.fake.comments[12] = [dict(comment, body=tickets.MERGED_FIRST_NOTE % (
            99, old["merged_at"], "a" * 12))]
        result, no_pr = self.report([old])
        self.assertTrue(result)
        no_pr.assert_not_called()
        self.assertIn("loom:pr", self.fake.names(12))

    def test_a_foreign_open_pull_with_the_rejected_number_still_keeps_the_ticket(self):
        old = self.incomplete_merge()
        self.fake.issues[12]["labels"] = [{"name": n} for n in ("loom:ticket", "loom:pr")]
        local = dict(old, pull_request={"merged_at": old["merged_at"]})
        foreign = dict(old, state="open", pull_request={},
                       repository_url="https://api.github.com/repos/example-owner/other-repo")
        self.fake.timelines = {12: [
            {"event": "labeled", "label": {"name": "loom:pr"},
             "created_at": "2030-01-05T00:00:00Z"},
            {"event": "cross-referenced", "source": {"issue": local}},
            {"event": "cross-referenced", "source": {"issue": foreign}}]}
        self.reader()._clear_pull_gone(REPO, 12, self.fake.issues[12], [], [old])
        self.assertIn("loom:pr", self.fake.names(12))
        self.assertNotIn("loom:merged", self.fake.names(12))
