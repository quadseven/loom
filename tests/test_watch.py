from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import station, text, watch  # noqa: E402
from bus.github import Response  # noqa: E402

LANES_REPO = "example-owner/lanes"
T0 = 1_800_000_000.0
LINE_403 = ("[2026-10-13T10:00:01Z] station: could not scan example-owner/repo-a: "
            "GET /repos/example-owner/repo-a/issues?labels=loom:briefed answered 403")
LINE_403_B = ("[2026-10-13T10:02:41Z] station: could not scan example-owner/repo-b: "
              "GET /repos/example-owner/repo-b/issues?labels=loom:briefed answered 403")
# bus/station.py _try_ticket's hold line, as loom#645 and loom#646 saw it.
HOLD_LINE = "station: example-owner/repo-a#506 waits for #102 (failed)"
HOLD_LINE_CROSS = "station: example-owner/repo-b#871 waits for example-owner/repo-a#102 (failed)"


class FakeGH:
    def __init__(self):
        self.issues = {}
        self.comments = []
        self.edits = []
        self.calls = []

    def rest(self, method, path, body=None):
        self.calls.append((method, path))
        if method == "POST" and path == "/repos/%s/issues" % LANES_REPO:
            number = len(self.issues) + 1
            self.issues[number] = dict(body or {}, number=number, state="open")
            return Response(201, {"number": number})
        if method == "POST" and path.endswith("/comments"):
            self.comments.append((int(path.split("/")[-2]), (body or {})["body"]))
            return Response(201, {"id": len(self.comments)})
        if method == "PATCH" and "/issues/comments/" in path:
            ident = int(path.rsplit("/", 1)[1])
            self.edits.append((ident, (body or {})["body"]))
            return Response(200, {"id": ident})
        if method == "GET":
            number = int(path.rsplit("/", 1)[1])
            return Response(200, self.issues.get(number))
        raise AssertionError("unexpected %s %s" % (method, path))

    def paginate(self, path):
        self.calls.append(("GET", path))
        return [i for i in self.issues.values()
                if i["state"] == "open" and watch.LABEL in i["labels"]]


class WatchBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.log = os.path.join(self.dir, "logs", "station.log")
        os.makedirs(os.path.dirname(self.log))
        self.state = os.path.join(self.dir, "state", "station", "watch.json")
        self.gh = FakeGH()
        self.said = []
        # The first scan starts at each log's end; start every test past it.
        self.append("")
        self.run_at(T0 - 3600)

    def append(self, *lines):
        with open(self.log, "a", encoding="utf-8") as fh:
            for line in lines:
                fh.write(line + "\n")

    def run_at(self, now, write=True):
        watch.Watcher(self.gh, LANES_REPO, [("station", self.log)], self.state,
                      now, self.said.append, write=write).run()

    def saved(self):
        with open(self.state, encoding="utf-8") as fh:
            return json.load(fh)


class SignatureTest(unittest.TestCase):
    def test_a_credential_never_reaches_a_signature(self):
        sig = watch.signature("auth failed: Authorization: Bearer abcdef0123456789xyz api_key=hunter2hunter2")
        self.assertNotIn("abcdef0123456789xyz", sig)
        self.assertNotIn("hunter2", sig)

    def test_two_403s_on_different_repos_match(self):
        self.assertEqual(watch.signature(LINE_403), watch.signature(LINE_403_B))
        self.assertIn("403", watch.signature(LINE_403))
        self.assertNotIn("repo-a", watch.signature(LINE_403))

    def test_a_different_status_is_a_different_signature(self):
        self.assertNotEqual(watch.signature(LINE_403),
                            watch.signature(LINE_403.replace("403", "502")))

    def test_ids_numbers_shas_times_and_refs_are_placeholders(self):
        a = ("2026-10-13 09:14:02 brain: push of 3f9c2a1b7e failed for example-owner/one#12 "
             "(PRRT_kwDOAbCd1234, pid 4411) in /var/tmp/run-77")
        b = ("2026-10-14 23:01:59 brain: push of 0d4e8f6a2c99 failed for other-owner/two#9 "
             "(PRRT_kwDOZzZz9999, pid 12) in /var/tmp/run-3")
        self.assertEqual(watch.signature(a), watch.signature(b))
        self.assertEqual(watch.signature(a),
                         "<time> brain: push of <hex> failed for <repo>#<n> (<id>, pid <n>) in <path>")

    def test_which_lines_are_kept(self):
        for line in ("ValueError: bad", "push failed", "Connection refused",
                     "HTTP 502 from the API", "upstream said 404 Not Found"):
            self.assertTrue(watch.keep(line), line)
        for line in ("claimed example-owner/repo#3 for lane codex", "took 403 ms"):
            self.assertFalse(watch.keep(line), line)

    def test_a_hold_on_a_failed_ticket_is_status_not_an_error(self):
        # loom#645 and loom#646: the station says each pass what a held
        # ticket waits for, and a dependency in loom:failed reads "(failed)".
        for line in (HOLD_LINE, HOLD_LINE_CROSS,
                     "station: example-owner/repo-a#7 waits for #3 (failed), #4 (pr)",
                     "[2026-10-17 18:50:01] " + HOLD_LINE):
            self.assertFalse(watch.keep(line), line)
        # Anything more on the line than the hold is judged as usual.
        for line in ("station: example-owner/repo-a#7 waits for #3 (failed) and push failed",
                     "station: could not read example-owner/repo-a#3: HTTP 502 failed",
                     "error: example-owner/repo-a#7 waits for #3 (failed)"):
            self.assertTrue(watch.keep(line), line)


class FilingTest(WatchBase):
    def test_three_hits_in_thirty_minutes_file_one_issue_and_a_fourth_adds_nothing(self):
        self.append(LINE_403)
        self.run_at(T0)
        self.append(LINE_403_B)
        self.run_at(T0 + 600)
        self.assertEqual(self.gh.issues, {})
        self.append(LINE_403)
        self.run_at(T0 + 1200)
        self.assertEqual(len(self.gh.issues), 1)
        issue = self.gh.issues[1]
        self.assertEqual(issue["title"], "station: " + watch.signature(LINE_403))
        self.assertEqual(issue["labels"], ["loom:triage"])
        self.assertIn("3 times in the last 30 minutes", issue["body"])
        self.assertIn("repo-b", LINE_403_B)
        self.assertNotIn("repo-a", issue["body"])
        self.append(LINE_403_B)
        self.run_at(T0 + 1260)
        self.assertEqual(len(self.gh.issues), 1)
        self.assertEqual(self.gh.comments, [])

    def test_a_hold_on_a_failed_ticket_files_nothing(self):
        for i in range(5):
            self.append(HOLD_LINE, HOLD_LINE_CROSS)
            self.run_at(T0 + i * 300)
        self.assertEqual(self.gh.issues, {})

    def test_hits_spread_wider_than_the_window_file_nothing(self):
        for i in range(3):
            self.append(LINE_403)
            self.run_at(T0 + i * 1000)
        self.assertEqual(self.gh.issues, {})

    def test_a_signature_seen_yesterday_files_nothing(self):
        self.append(LINE_403)
        self.run_at(T0 - 20 * 3600)
        self.append(LINE_403, LINE_403_B, LINE_403)
        self.run_at(T0)
        self.assertEqual(self.gh.issues, {})

    def test_a_signature_quiet_for_a_day_is_new_again(self):
        self.append(LINE_403)
        self.run_at(T0 - 25 * 3600)
        self.append(LINE_403, LINE_403_B, LINE_403)
        self.run_at(T0)
        self.assertEqual(len(self.gh.issues), 1)

    def test_an_open_issue_gets_one_count_comment_an_hour(self):
        self.append(LINE_403, LINE_403, LINE_403)
        self.run_at(T0)
        self.append(LINE_403, LINE_403)
        self.run_at(T0 + 1800)
        self.assertEqual(self.gh.comments, [])
        self.run_at(T0 + 3600)
        self.assertEqual(len(self.gh.comments), 1)
        number, body = self.gh.comments[0]
        self.assertEqual(number, 1)
        self.assertIn("Seen 2 more times", body)
        self.run_at(T0 + 3 * 3600)
        self.assertEqual(len(self.gh.comments), 1)
        self.assertEqual(len(self.gh.issues), 1)

    def test_later_counts_edit_the_one_count_comment_in_place(self):
        # loom#467, 2026-10-18: 26 "Seen N more times" comments, one email each.
        self.append(LINE_403, LINE_403, LINE_403)
        self.run_at(T0)
        self.append(LINE_403, LINE_403)
        self.run_at(T0 + 3600)
        self.append(LINE_403)
        self.run_at(T0 + 2 * 3600)
        self.append(LINE_403)
        self.run_at(T0 + 3 * 3600)
        self.assertEqual(len(self.gh.comments), 1)
        self.assertEqual([ident for ident, _ in self.gh.edits], [1, 1])
        self.assertIn("Seen 1 more times", self.gh.edits[-1][1])

    def test_a_lane_failure_classed_transient_or_limit_is_never_a_signature(self):
        for line in ("[2026-10-18T22:50:36Z] example.repo-3 failed: [transient] agent exited 124",
                     "[2026-10-18T22:50:36Z] example.repo-3 failed: [limit] agent exited 3: limit"):
            with self.subTest(line=line):
                self.assertFalse(watch.keep(line))
        self.assertTrue(watch.keep("[2026-10-18T22:50:36Z] example.repo-3 failed: [work] tests failed"))

    def test_a_closed_issue_is_not_counted_or_filed_again(self):
        self.append(LINE_403, LINE_403, LINE_403)
        self.run_at(T0)
        self.gh.issues[1]["state"] = "closed"
        self.append(LINE_403)
        self.run_at(T0 + 3600)
        self.run_at(T0 + 7200)
        self.assertEqual(self.gh.comments, [])
        self.assertEqual(len(self.gh.issues), 1)

    def test_an_open_issue_with_the_title_is_counted_not_filed_twice(self):
        title = "station: " + watch.signature(LINE_403)
        self.gh.issues[1] = {"number": 1, "title": title, "state": "open",
                             "labels": ["loom:triage"]}
        self.append(LINE_403, LINE_403, LINE_403)
        self.run_at(T0)
        self.assertEqual(len(self.gh.issues), 1)
        self.assertEqual([n for n, _ in self.gh.comments], [1])

    def test_a_low_budget_scans_and_files_on_a_later_pass(self):
        self.append(LINE_403, LINE_403, LINE_403)
        self.run_at(T0, write=False)
        self.assertEqual(self.gh.issues, {})
        self.run_at(T0 + 3600)
        self.assertEqual(len(self.gh.issues), 1)

    def test_a_failed_filing_does_not_skip_another_due_signature(self):
        other = "Connection refused"
        self.append(LINE_403, LINE_403, LINE_403, other, other, other)
        rest = self.gh.rest
        failed = False

        def fail_first_issue(method, path, body=None):
            nonlocal failed
            if method == "POST" and path == "/repos/%s/issues" % LANES_REPO and not failed:
                failed = True
                raise OSError("temporary failure")
            return rest(method, path, body)

        with mock.patch.object(self.gh, "rest", side_effect=fail_first_issue):
            self.run_at(T0)

        self.assertTrue(failed)
        self.assertEqual([issue["title"] for issue in self.gh.issues.values()],
                         ["station: " + watch.signature(other)])
        self.assertEqual(self.saved()["sigs"]["station: " + watch.signature(LINE_403)]["due"],
                         "file")

    def test_the_watchers_own_lines_never_count(self):
        line = "station: watch: filed %s#1 for a repeated station line: error" % LANES_REPO
        self.append(line, line, line)
        self.run_at(T0)
        self.assertEqual(self.gh.issues, {})

    def test_a_traceback_counts_once_as_its_exception_line(self):
        tb = ["Traceback (most recent call last):",
              '  File "x.py", line 3, in <module>',
              "    raise ConnectionError('refused')",
              "ConnectionError: sandbox transport closed"]
        self.append(*(tb * 3))
        self.run_at(T0)
        self.assertEqual([i["title"] for i in self.gh.issues.values()],
                         ["station: ConnectionError: sandbox transport closed"])

    def test_scanning_logs_an_hourly_summary(self):
        self.said.clear()
        self.run_at(T0 - 600)
        self.assertEqual(self.said, [])
        self.run_at(T0)
        self.assertEqual(len(self.said), 1)
        self.assertIn("watch: scanned 1 logs", self.said[0])


class ExampleTextTest(WatchBase):
    def test_example_lines_pass_the_written_text_check(self):
        private = ("[2026-10-13T10:00:01Z] error: push by example-user@example.org from "
                   "/Users/example-user/src/x on build-box.internal (192.0.2.7) "
                   "token=abc123secretvalue cc @example-user")
        self.append(private, private, private)
        self.run_at(T0)
        issue = self.gh.issues[1]
        text.check(issue["title"] + "\n" + issue["body"])
        for leak in ("example-user", "build-box", "192.0.2.7", "abc123secretvalue"):
            self.assertNotIn(leak, issue["title"] + issue["body"])

    def test_an_example_that_fails_the_check_is_withheld(self):
        bad = "push failed; this fixes example-owner/repo#3 says @%s" % text.REVIEW_BOT_SHORT_NAME
        self.assertEqual(watch._checked("this fixes #3"), watch.WITHHELD)
        self.append(bad, bad, bad)
        self.run_at(T0)
        body = self.gh.issues[1]["body"]
        text.check(body)
        self.assertNotIn("@" + text.REVIEW_BOT_SHORT_NAME, body)


class OffsetTest(WatchBase):
    def test_the_first_scan_starts_at_the_end_of_each_log(self):
        os.remove(self.state)
        self.append(LINE_403, LINE_403, LINE_403)
        self.run_at(T0)
        self.assertEqual(self.gh.issues, {})
        self.assertEqual(self.saved()["logs"]["station"]["offset"], os.path.getsize(self.log))

    def test_a_rotated_log_restarts_the_offset(self):
        self.append("x" * 500)
        self.run_at(T0)
        self.assertEqual(self.saved()["logs"]["station"]["offset"], os.path.getsize(self.log))
        os.rename(self.log, self.log + ".1")
        self.append(LINE_403, LINE_403, LINE_403)
        self.run_at(T0 + 60)
        self.assertEqual(len(self.gh.issues), 1)
        self.assertEqual(self.saved()["logs"]["station"]["offset"], os.path.getsize(self.log))

    def test_a_truncated_log_restarts_the_offset(self):
        self.append("x" * 500)
        self.run_at(T0)
        with open(self.log, "w", encoding="utf-8") as fh:
            fh.write((LINE_403 + "\n") * 3)
        self.run_at(T0 + 60)
        self.assertEqual(len(self.gh.issues), 1)

    def test_a_partial_last_line_waits_for_its_end(self):
        with open(self.log, "a", encoding="utf-8") as fh:
            fh.write("push fai")
        self.run_at(T0)
        before = self.saved()["logs"]["station"]["offset"]
        with open(self.log, "a", encoding="utf-8") as fh:
            fh.write("led\n")
        self.run_at(T0 + 60)
        self.assertEqual(self.saved()["logs"]["station"]["offset"], os.path.getsize(self.log))
        self.assertGreater(os.path.getsize(self.log), before)
        self.assertIn("station: push failed", self.saved()["sigs"])

    def test_a_log_that_appears_later_is_read_from_its_start(self):
        late = os.path.join(self.dir, "logs", "claude-service.log")
        logs = [("station", self.log), ("claude", late)]
        watch.Watcher(self.gh, LANES_REPO, logs, self.state, T0, self.said.append).run()
        with open(late, "w", encoding="utf-8") as fh:
            fh.write((LINE_403 + "\n") * 3)
        watch.Watcher(self.gh, LANES_REPO, logs, self.state, T0 + 60, self.said.append).run()
        self.assertEqual([i["title"][:7] for i in self.gh.issues.values()], ["claude:"])


class StationTest(WatchBase):
    def station(self, status_repo):
        settings = station.Settings(self.dir, ["example-owner/repo-a"], [], [],
                                    status_repo=status_repo, watched=[("station", self.log)])
        st = station.Station(self.gh, settings, now=T0)
        st.state = os.path.dirname(self.state)
        return st

    def test_the_station_pass_files_in_the_lanes_repo(self):
        self.append(LINE_403, LINE_403_B, LINE_403)
        st = self.station(LANES_REPO)
        with mock.patch.object(st, "_deferred", return_value=False):
            st.watch_logs()
        self.assertEqual(len(self.gh.issues), 1)

    def test_a_low_budget_still_moves_the_offsets(self):
        self.append(LINE_403, LINE_403_B, LINE_403)
        st = self.station(LANES_REPO)
        with mock.patch.object(st, "_deferred", return_value=True):
            st.watch_logs()
        self.assertEqual(self.gh.issues, {})
        self.assertEqual(self.saved()["logs"]["station"]["offset"], os.path.getsize(self.log))

    def test_no_lanes_repo_watches_nothing(self):
        self.append(LINE_403, LINE_403_B, LINE_403)
        before = self.saved()
        self.station("").watch_logs()
        self.assertEqual(self.saved(), before)


class ComponentsTest(unittest.TestCase):
    def test_default_is_station_brain_and_each_lane_service_log(self):
        names = dict(watch.components("/loom"))
        self.assertEqual(names["station"], "/loom/logs/station.log")
        self.assertEqual(names["brain"], "/loom/logs/brain.log")
        self.assertEqual(names["claude"], "/loom/logs/claude-service.log")
        self.assertEqual(names["anthropic-2"], "/loom/logs/anthropic-2-service.log")

    def test_loom_watch_lines_replace_the_default(self):
        spec = "# comment\nstation=logs/station.log\n\nother = /var/log/other.log\n"
        self.assertEqual(watch.components("/loom", spec),
                         [("station", "/loom/logs/station.log"), ("other", "/var/log/other.log")])
        with self.assertRaises(ValueError):
            watch.components("/loom", "no equals sign")

    def test_settings_read_loom_watch(self):
        s = station.Settings.from_env({"LOOM_HOME": "/loom", "LOOM_WATCH": "a=/x.log"})
        self.assertEqual(s.watched, [("a", "/x.log")])
        s = station.Settings.from_env({"LOOM_HOME": "/loom", "LOOM_WATCH": "bad"})
        self.assertEqual(s.watched, watch.components("/loom"))


if __name__ == "__main__":
    unittest.main()
