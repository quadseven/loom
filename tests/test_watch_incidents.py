"""Route real watch passes to the factory incident named before redaction."""
from unittest import mock

from bus import watch
from tests.test_watch import LANES_REPO, T0, WatchBase


class IncidentRoutingTest(WatchBase):
    def incident(self, number=41):
        self.gh.issues[number] = dict(number=number, title="Factory validation failed",
                                      state="open", labels=["loom:factory"])

    @staticmethod
    def lines(number=41, repo=LANES_REPO):
        ref = "%s#%d" % (repo, number)
        ticket = "%s#%d" % ("example-owner/repo-a", 5)
        return ["%s failed: [factory] %s; factory incident %s"
                % (ticket, wording, ref) for wording in (
                    "base check exited 1 on the unmodified repo",
                    "base check exited 1 on the default branch",
                    "the check exited 126 on the unmodified base")]

    def issue_posts(self):
        return [c for c in self.gh.calls
                if c == ("POST", "/repos/%s/issues" % LANES_REPO)]

    def test_setup_variants_count_on_named_incident_without_a_new_issue(self):
        self.incident()
        self.append(*self.lines())
        self.run_at(T0)
        self.assertEqual([], self.issue_posts())
        self.assertEqual([41], [n for n, _ in self.gh.comments])
        self.assertIn("Seen 3 more times", self.gh.comments[0][1])
        self.assertEqual(1, len(self.saved()["sigs"]))
        body = self.gh.comments[0][1]
        self.assertNotIn(LANES_REPO, body)
        self.assertNotIn("repo-a", body)

    def test_repeat_pass_and_hourly_hits_edit_one_comment(self):
        self.incident()
        self.append(*self.lines())
        self.run_at(T0)
        self.run_at(T0 + 1)
        self.append(*self.lines())
        self.run_at(T0 + 2)
        self.assertEqual(1, len(self.gh.comments))
        self.assertEqual([], self.gh.edits)
        self.run_at(T0 + watch.COMMENT_EVERY)
        self.assertEqual([41], [n for n, _ in self.gh.comments])
        self.assertEqual([1], [n for n, _ in self.gh.edits])
        self.assertEqual([], self.issue_posts())

    def test_closed_incident_keeps_ordinary_watch_deduplication(self):
        self.incident()
        self.gh.issues[41]["state"] = "closed"
        key = "station: " + watch.signature(self.lines()[0])
        self.gh.issues[42] = dict(number=42, title=key, state="open", labels=[watch.LABEL])
        self.append(*self.lines())
        self.run_at(T0)
        self.assertEqual([], self.issue_posts())
        self.assertEqual([42], [n for n, _ in self.gh.comments])

    def test_non_factory_and_pull_request_references_do_not_attach(self):
        for number, fields in enumerate(({"labels": [watch.LABEL]}, {"pull_request": {}}), 41):
            with self.subTest(fields=fields):
                self.incident(number)
                self.gh.issues[number].update(fields)
                self.append(*self.lines(number))
                self.run_at(T0 + number)
                self.assertNotIn(number, [n for n, _ in self.gh.comments])

    def test_ambiguous_references_are_not_attached(self):
        self.incident()
        self.incident(42)
        second = "; factory incident %s#%d" % (LANES_REPO, 42)
        self.append(*(line + second for line in self.lines()))
        self.run_at(T0)
        self.assertEqual(1, len(self.issue_posts()))
        self.assertEqual([], self.gh.comments)

    def test_wrong_repository_is_never_looked_up_or_attached(self):
        self.incident()
        self.append(*self.lines(repo="example-owner/other"))
        self.run_at(T0)
        self.assertEqual(1, len(self.issue_posts()))
        self.assertEqual([], self.gh.comments)
        self.assertFalse(any("/issues/41" in p for _, p in self.gh.calls))

    def test_missing_lookup_is_unknown_and_retries_without_losing_hits(self):
        self.append(*self.lines())
        self.run_at(T0)
        self.assertEqual([], self.issue_posts())
        self.assertEqual([], self.gh.comments)
        self.assertTrue(any("unknown" in line for line in self.said))
        self.incident()
        self.run_at(T0 + 1)
        self.assertEqual([41], [n for n, _ in self.gh.comments])
        self.assertIn("Seen 3 more times", self.gh.comments[0][1])

    def test_pending_burst_survives_lookup_failure_past_the_window(self):
        self.append(*self.lines())
        self.run_at(T0)
        self.incident()
        self.run_at(T0 + watch.WINDOW + 1)
        self.assertEqual([41], [n for n, _ in self.gh.comments])
        self.assertIn("Seen 3 more times", self.gh.comments[0][1])

    def test_unreadable_targets_cannot_starve_the_next_incident(self):
        valid = 41 + watch.INCIDENT_LOOKUPS_MAX
        self.incident(valid)
        for number in range(41, valid + 1):
            self.append(*self.lines(number))
        self.run_at(T0)
        self.run_at(T0 + 1)
        self.assertEqual([valid], [n for n, _ in self.gh.comments])

    def test_unreadable_lookup_is_unknown_and_does_not_file(self):
        self.incident()
        self.append(*self.lines())
        original = self.gh.rest

        def rest(method, path, body=None):
            if method == "GET" and path.endswith("/issues/41"):
                raise OSError("unavailable")
            return original(method, path, body)

        with mock.patch.object(self.gh, "rest", side_effect=rest):
            self.run_at(T0)
        self.assertEqual([], self.issue_posts())
        self.assertEqual([], self.gh.comments)
        self.assertTrue(any("unknown" in line for line in self.said))

    def test_malformed_labels_are_unknown(self):
        self.incident()
        self.gh.issues[41].pop("labels")
        self.append(*self.lines())
        # A response without labels cannot establish that this is a factory issue.
        with mock.patch.object(self.gh, "paginate", return_value=[]):
            self.run_at(T0)
        self.assertEqual([], self.issue_posts())
        self.assertEqual([], self.gh.comments)
        self.assertTrue(any("unknown" in line for line in self.said))

    def test_distinct_incidents_do_not_share_normalized_count(self):
        for number in (41, 42):
            self.incident(number)
            self.append(*self.lines(number))
        self.run_at(T0)
        self.assertEqual([], self.issue_posts())
        self.assertEqual([41, 42], [n for n, _ in self.gh.comments])
        self.assertTrue(all("Seen 3 more times" in body for _, body in self.gh.comments))

    def test_distinct_signatures_share_the_incidents_comment_and_timer(self):
        self.incident()
        self.append(*self.lines())
        other = "another error; factory incident %s#%d" % (LANES_REPO, 41)
        self.append(*([other] * 3))
        self.run_at(T0)
        self.assertEqual([41], [n for n, _ in self.gh.comments])
        self.assertIn("Seen 6 more times", self.gh.comments[0][1])
        self.append(*self.lines())
        self.run_at(T0 + 1)
        self.assertEqual([], self.gh.edits)
        self.run_at(T0 + watch.COMMENT_EVERY)
        self.assertEqual([1], [n for n, _ in self.gh.edits])

    def test_invalid_refs_share_activity_on_their_ordinary_watch_issue(self):
        for number in (41, 42):
            self.incident(number)
            self.gh.issues[number]["state"] = "closed"
            self.append(*self.lines(number))
        self.run_at(T0)
        self.assertEqual(1, len(self.issue_posts()))
        number = next(n for n in self.gh.issues if n not in (41, 42))
        self.append(*self.lines(42))
        self.run_at(T0 + watch.DAY - 1)
        self.run_at(T0 + watch.DAY)
        self.assertEqual("open", self.gh.issues[number]["state"])
        self.assertEqual(1, len(self.saved()["sigs"]))

    def test_write_budget_and_read_only_pass_are_preserved(self):
        for number in range(41, 41 + watch.WRITES_MAX + 1):
            self.incident(number)
            self.append(*self.lines(number))
        self.run_at(T0, write=False)
        self.assertEqual([], self.gh.comments)
        self.run_at(T0 + 1)
        self.assertEqual(watch.WRITES_MAX, len(self.gh.comments))
        self.run_at(T0 + 2)
        self.assertEqual(watch.WRITES_MAX + 1, len(self.gh.comments))
        self.assertEqual([], self.issue_posts())

    def test_watch_silence_never_closes_the_referenced_incident(self):
        self.incident()
        self.append(*self.lines())
        self.run_at(T0)
        self.run_at(T0 + watch.DAY)
        self.assertEqual("open", self.gh.issues[41]["state"])
        self.assertNotIn(("PATCH", "/repos/%s/issues/41" % LANES_REPO), self.gh.calls)
        self.append(*self.lines())
        self.run_at(T0 + watch.DAY + 1)
        self.assertEqual([41], [n for n, _ in self.gh.comments])
        self.assertEqual([1], [n for n, _ in self.gh.edits])

    def test_label_objects_are_supported_and_later_removal_is_respected(self):
        self.incident()
        self.gh.issues[41]["labels"] = [{"name": "loom:factory"}]
        self.append(*self.lines())
        self.run_at(T0)
        self.assertEqual([41], [n for n, _ in self.gh.comments])
        self.gh.issues[41]["labels"] = []
        self.append(*self.lines())
        self.run_at(T0 + watch.COMMENT_EVERY)
        self.assertEqual(1, len(self.gh.comments))
        self.assertEqual([], self.gh.edits)
