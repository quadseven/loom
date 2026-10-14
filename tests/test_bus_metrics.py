from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timezone

from bus import metrics, station
from bus.github import GitHubError, Response

REPO = "example-owner/example-repo"
NOW = datetime(2030, 1, 2, 17, tzinfo=timezone.utc).timestamp()


def iso(at):
    return datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def label(name):
    return {"name": name}


class Fake:
    def __init__(self):
        self.goal = {"number": 1, "labels": [label("loom:goal")]}
        self.ticket = {"number": 2, "labels": [label(x) for x in
                       ("loom:ticket", "loom:merged", "loom:verified")],
                       "created_at": iso(NOW - 10000), "state": "closed"}
        self.issues = [self.goal, self.ticket]
        self.pr = {"number": 3, "head": {"ref": "loom/2-fix", "sha": "abc"},
                   "state": "closed", "created_at": iso(NOW - 9000),
                   "merged_at": iso(NOW - 1000), "body": "A feature"}
        self.pulls = [self.pr]
        self.comments = {1: [{"body": metrics.REPORT + "\nReport",
                              "created_at": iso(NOW - 1000),
                              "user": {"login": "trusted-bot"}}], 2: []}
        self.events = {2: [{"event": "labeled", "label": label("loom:briefed"),
                            "created_at": iso(NOW - 8000)},
                           {"event": "labeled", "label": label("loom:merged"),
                            "created_at": iso(NOW - 1000)}]}
        self.checks = [{"name": "Review"}]
        self.files = [{"filename": "tests/test_feature.py", "status": "modified"}]
        self.posts = []

    def paginate(self, path):
        if path.endswith("/issues?state=all&per_page=100"):
            return self.issues
        if path.endswith("/pulls?state=all&per_page=100"):
            return self.pulls
        if "/issues/" in path and path.endswith("/comments"):
            return self.comments[int(path.rsplit("/", 2)[1])]
        if "/issues/" in path and path.endswith("/events"):
            return self.events.get(int(path.rsplit("/", 2)[1]), [])
        if path.endswith("/pulls/3/files"):
            return self.files
        raise AssertionError(path)

    def rest(self, method, path, body=None):
        if method == "GET" and "/check-runs" in path:
            # GitHub's paging: 30 a page unless per_page says otherwise.
            query = dict(kv.split("=") for kv in path.partition("?")[2].split("&") if "=" in kv)
            size, page = int(query.get("per_page", 30)), int(query.get("page", 1))
            batch = self.checks[(page - 1) * size:page * size]
            return type("Response", (), {"data": {"check_runs": batch}})()
        if method == "POST" and path.endswith("/issues/1/comments"):
            self.posts.append(body["body"])
            self.comments[1].append({"body": body["body"], "created_at": iso(NOW),
                                     "user": {"login": "trusted-bot"}})
            return type("Response", (), {"data": {"id": len(self.posts)}})()
        raise AssertionError((method, path))


class MetricsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        self.gh = Fake()
        path = os.path.join(self.home, "briefs", "codex", "done")
        os.makedirs(path)
        run = os.path.join(path, "run.md")
        with open(run, "w") as fh:
            fh.write("Issue: %s#2\nModel: model-x\n" % REPO)
        os.utime(run, (NOW - 1000, NOW - 1000))
        path = os.path.join(self.home, "state", "station")
        os.makedirs(path)
        with open(os.path.join(path, "slot-samples.jsonl"), "w") as fh:
            fh.write(json.dumps({"at": NOW - 1000, "idle": False}) + "\n")

    def numbers(self):
        return metrics.Collector(self.gh, [REPO], self.home, NOW, "Review").collect()

    def test_all_nine_pass_on_a_complete_day(self):
        result = self.numbers()
        self.assertEqual(len(result), 9)
        self.assertTrue(all(n.passed for n in result), [n.line() for n in result])

    def test_each_target_can_fail(self):
        changes = [
            lambda: self.gh.pr.update(state="open", created_at=iso(NOW - 30000), merged_at=None),
            lambda: self.gh.ticket["labels"].remove(label("loom:verified")),
            lambda: self._idle_slot(),
            lambda: self._two_failures(),
            lambda: self.gh.checks.clear(),
            lambda: self.gh.issues.append({"number": 4, "labels": [label("needs-operator")],
                                           "created_at": iso(NOW - 1000)}),
            lambda: self.gh.comments[1].clear(),
            lambda: self._past_rung(),
            lambda: self._generic_file(),
        ]
        for index, change in enumerate(changes):
            with self.subTest(number=index + 1):
                self.setUp()
                change()
                if index == 5:
                    self.gh.issues.append({"number": 5, "labels": [label("needs-operator")],
                                           "created_at": iso(NOW - 1000)})
                self.assertFalse(self.numbers()[index].passed)

    def test_a_review_check_past_the_first_page_still_counts(self):
        self.gh.checks = [{"name": "unittest (%d)" % n} for n in range(120)] + [{"name": "Review"}]
        self.assertTrue(metrics.Collector(self.gh, [REPO], self.home, NOW, "Review").passes(REPO, "abc"))
        self.gh.checks = [{"name": "unittest (%d)" % n} for n in range(120)]
        self.assertFalse(metrics.Collector(self.gh, [REPO], self.home, NOW, "Review").passes(REPO, "abc"))

    def test_check_runs_reply_without_a_body_counts_as_no_review(self):
        self.gh.rest = lambda method, path, body=None: Response(200, None)
        self.assertFalse(metrics.Collector(self.gh, [REPO], self.home, NOW, "Review").passes(REPO, "abc"))

    def test_a_torn_slot_sample_line_is_skipped(self):
        path = os.path.join(self.home, "state", "station", "slot-samples.jsonl")
        with open(path, "w") as fh:
            fh.write(json.dumps({"at": NOW - 1000, "idle": False}) + "\n")
            fh.write('{"at": %d, "id' % (NOW - 500))
        with contextlib.redirect_stderr(io.StringIO()) as err:
            samples = metrics.Collector(self.gh, [REPO], self.home, NOW, "Review").slot_samples()
        self.assertEqual((0, 1), samples)
        self.assertIn("unreadable slot sample", err.getvalue())

    def test_a_hung_git_ls_files_raises_os_error(self):
        timeout = subprocess.TimeoutExpired(["git"], 60)
        with mock.patch.object(metrics.subprocess, "run", side_effect=timeout):
            with self.assertRaises(OSError):
                metrics.Collector(self.gh, [REPO], self.home, NOW, "Review").generic_files()

    def _idle_slot(self):
        path = os.path.join(self.home, "state", "station", "slot-samples.jsonl")
        with open(path, "w") as fh:
            fh.write(json.dumps({"at": NOW - 1000, "idle": True}) + "\n")

    def _two_failures(self):
        folder = os.path.join(self.home, "briefs", "codex", "failed")
        os.makedirs(folder)
        for n in (1, 2):
            path = os.path.join(folder, "%d.md" % n)
            with open(path, "w") as fh:
                fh.write("Issue: %s#2\nModel: model-x\n" % REPO)
            os.utime(path, (NOW - 1000, NOW - 1000))

    def _tag_failures(self, *kinds):
        folder = os.path.join(self.home, "logs", "codex")
        os.makedirs(folder, exist_ok=True)
        for n, kind in enumerate(kinds, 1):
            if kind:
                with open(os.path.join(folder, "%d.log" % n), "w") as fh:
                    fh.write("[t] %d failed: [work] first\n[t] %d failed: [%s] last\n" % (n, n, kind))

    def escalation(self):
        return next(n for n in self.numbers() if n.name == "Escalation")

    def test_two_limit_failures_on_one_model_do_not_count(self):
        self._two_failures()
        self._tag_failures("limit", "limit")
        number = self.escalation()
        self.assertIn("0 tickets failed twice", number.value)
        self.assertIn("2 failures left out", number.value)
        self.assertTrue(number.passed)

    def test_two_work_failures_on_one_model_count(self):
        self._two_failures()
        self._tag_failures("work", "work")
        number = self.escalation()
        self.assertIn("1 tickets failed twice", number.value)
        self.assertIn("0 failures left out", number.value)
        self.assertFalse(number.passed)

    def test_a_failure_with_no_tag_line_counts_as_work(self):
        self._two_failures()
        self._tag_failures(None, "work")
        self.assertIn("1 tickets failed twice", self.escalation().value)

    def test_a_limit_and_a_work_failure_do_not_make_a_repeat(self):
        self._two_failures()
        self._tag_failures("transient", "work")
        number = self.escalation()
        self.assertIn("0 tickets failed twice", number.value)
        self.assertIn("1 failures left out", number.value)

    def _past_rung(self):
        self.gh.ticket["state"] = "open"
        self.gh.ticket["labels"].append(label("loom:failed"))
        self.gh.comments[2].append({"body": "The factory tried every rung of the model ladder.",
                                    "created_at": iso(NOW - 90000)})

    def _generic_file(self):
        self.gh.issues.append({"number": 8, "labels": [], "title": "unused"})
        # The repository scan is covered separately with a patched result.
        original = metrics.Collector.generic_files
        metrics.Collector.generic_files = lambda collector: ["source.py"]
        self.addCleanup(setattr, metrics.Collector, "generic_files", original)

    def test_daily_comment_streak_and_discord_retry(self):
        self.gh.comments[1].append({"body": metrics.MARKER +
                                    "\n2030-01-01: 9/9 pass; streak 1 day(s)",
                                    "user": {"login": "trusted-bot"}})
        settings = station.Settings(self.home, [REPO], ["trusted-bot"], [], digest_hour=0,
                                    metrics_goal=REPO + "#1", webhook_ssm="fake",
                                    review_check="Review")
        sent = []
        st = station.Station(self.gh, settings, now=NOW,
                             read_secret=lambda name, region: "https://example.com/webhook",
                             post=lambda url, body: sent.append(body["content"]))
        st.post_metrics()
        st.post_metrics()
        self.assertEqual(len(self.gh.posts), 1)
        self.assertEqual(len(sent), 1)
        self.assertIn("9/9 pass; streak 2 day(s)", self.gh.posts[0])
        self.assertIn("projected 7-day date 2030-01-07", self.gh.posts[0])
        self.assertIn("9/9 pass; streak 2/7", sent[0])

    def test_collection_failure_alerts_once_and_retries_daily_comment(self):
        settings = station.Settings(self.home, [REPO], ["trusted-bot"], [], digest_hour=0,
                                    metrics_goal=REPO + "#1", webhook_ssm="fake",
                                    review_check="Review")
        sent = []
        st = station.Station(self.gh, settings, now=NOW,
                             read_secret=lambda name, region: "https://example.com/webhook",
                             post=lambda url, body: sent.append(body["content"]))
        paginate = self.gh.paginate
        failing = True

        def interrupted(path):
            if failing and path.endswith("/pulls?state=all&per_page=100"):
                raise GitHubError("temporary API failure")
            return paginate(path)

        self.gh.paginate = interrupted
        st.post_metrics()
        st.post_metrics()
        self.assertEqual(self.gh.posts, [])
        self.assertEqual(sent, ["Loom metrics 2030-01-02: collection failed; retrying."])
        failing = False
        st.post_metrics()
        self.assertEqual(len(self.gh.posts), 1)
        self.assertEqual(len(sent), 2)
        self.assertIn("9/9 pass", sent[1])

    def test_regression_note_and_fix_without_test_fail_quality(self):
        self.gh.pr["body"] = "Fixes a bug"
        self.gh.files = [{"filename": "bus/brain.py", "status": "modified"}]
        self.gh.issues.append({"number": 4, "labels": [label("loom:urgent")],
                               "created_at": iso(NOW - 10000), "state": "open"})
        self.gh.comments[4] = [{"body": "Regression in a shipped change",
                                "created_at": iso(NOW - 9000)}]
        result = self.numbers()[4]
        self.assertFalse(result.passed)
        self.assertIn("1 unfixed regressions", result.value)
        self.assertIn("1 fix PRs without tests", result.value)

    def test_untrusted_metrics_comment_does_not_suppress_daily_post(self):
        self.gh.comments[1].append({"body": metrics.MARKER +
                                    "\n2030-01-02: 9/9 pass; streak 7 day(s)",
                                    "user": {"login": "untrusted"}})
        settings = station.Settings(self.home, [REPO], ["trusted-bot"], [], digest_hour=0,
                                    metrics_goal=REPO + "#1", review_check="Review")
        station.Station(self.gh, settings, now=NOW).post_metrics()
        self.assertEqual(len(self.gh.posts), 1)

    def test_waits_for_digest_hour_and_retries_discord_without_reposting(self):
        settings = station.Settings(self.home, [REPO], ["trusted-bot"], [], digest_hour=23,
                                    metrics_goal=REPO + "#1", review_check="Review",
                                    webhook_ssm="fake")
        sent = []

        def post(url, body):
            sent.append(body["content"])
            if len(sent) == 1:
                raise OSError("temporary failure")

        st = station.Station(self.gh, settings, now=NOW,
                             read_secret=lambda name, region: "https://example.com/webhook",
                             post=post)
        st.post_metrics()
        self.assertEqual(self.gh.posts, [])
        st.s.digest_hour = 0
        st.post_metrics()
        st.post_metrics()
        self.assertEqual(len(self.gh.posts), 1)
        self.assertEqual(len(sent), 2)


if __name__ == "__main__":
    unittest.main()
