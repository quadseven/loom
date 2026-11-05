from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain, handoff, logrotate, station, text, watch  # noqa: E402
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
        if method == "PATCH" and path.startswith("/repos/%s/issues/" % LANES_REPO):
            number = int(path.rsplit("/", 1)[1])
            self.issues[number].update(body or {})
            return Response(200, self.issues[number])
        if method == "GET":
            number = int(path.rsplit("/", 1)[1])
            return Response(200, self.issues.get(number))
        raise AssertionError("unexpected %s %s" % (method, path))

    def paginate(self, path):
        self.calls.append(("GET", path))
        return [i for i in self.issues.values()
                if i["state"] == "open" and watch.LABEL in i["labels"]]


class WatchBase(unittest.TestCase):
    COMPONENT = "station"
    PRIVATE = ()

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
        watch.Watcher(self.gh, LANES_REPO, [(self.COMPONENT, self.log)], self.state,
                      now, self.said.append, write=write, private=self.PRIVATE).run()

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

    def test_a_station_stage_failure_folds_the_stage_name(self):
        a = "[2026-10-20T01:45:00Z] station: run_factory failed: GET /repos/o/r/issues -> 404"
        b = "station: settle_orphan_sources failed: GET /repos/o/r/issues -> 404"
        self.assertEqual(watch.signature(a), watch.signature(b))
        self.assertTrue(watch.signature(a).startswith("station: <stage> failed: GET "))
        self.assertEqual(watch.stage(a), "run_factory")
        self.assertEqual(watch.stage(b), "settle_orphan_sources")
        # Other shapes keep their signatures: a repo before "failed", a
        # stage not at the start, or a line that is not the station's.
        for line in ("station: unwind_reverts example-owner/repo-a failed: GET /x -> 404",
                     "station: could not scan: run_factory failed: boom",
                     "brain: run_factory failed: boom"):
            self.assertIsNone(watch.stage(line), line)
            self.assertNotIn("<stage>", watch.signature(line), line)

    def test_setup_failure_wording_variants_produce_one_signature(self):
        # loom#2158: the log watcher filed #2120 ("on the unmodified repo")
        # and #2122 ("on the default branch") for one factory incident #2061.
        # Both wordings after "base check exited <n>" must normalize to one signature.
        line_unmodified = ("[2026-10-13T10:00:01Z] station: could not scan example-owner/repo-a: "
                           "base check exited 1 on the unmodified repo")
        line_default_branch = ("[2026-10-13T10:00:01Z] station: could not scan example-owner/repo-a: "
                               "base check exited 1 on the default branch")
        self.assertEqual(watch.signature(line_unmodified), watch.signature(line_default_branch))
        sig = watch.signature(line_unmodified)
        self.assertIn("base check exited <n> on the <setup-failure>", sig)
        # Also the "the check exited 125-127 on the unmodified base" variant.
        line_check_exited = ("[2026-10-13T10:00:01Z] station: could not scan example-owner/repo-a: "
                             "the check exited 126 on the unmodified base")
        self.assertEqual(watch.signature(line_check_exited), sig)

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


def _brain_status_lines():
    """The brain's own status output, made by the code that prints it: the
    stage handoff lines (bus/handoff.py stage_lines), the ticket report with
    its REPEATEDLY STUCK marks and state headings, the defer resume lines,
    and the tick record bus/brain.py step prints as JSON (loom#871)."""
    item = ("example-owner/repo-a", 590)
    marks = [handoff.Mark("done", "failed", item, "r1", "lane-machine", "2026-10-18T06:10:04Z"),
             handoff.Mark("defer", "merge", item, "r1", "muse", "2026-10-18T06:12:00Z"),
             handoff.Mark("done", "merge", item, "r1", "lane-machine", "2026-10-18T06:11:00Z"),
             handoff.Mark("defer", "merge", item, "r2", "muse", "2026-10-18T06:13:00Z")]
    stage = handoff.stage_lines(marks)
    history = brain.History(failed=4, fixes=2, tried=["claude"], reasons=["failed 4 times"],
                            rung=None)
    stuck = brain._stuck_marks({264: history}, [])
    ticket = {"number": 264, "title": "Retry a failed push - error path", "created_at":
              "2026-10-17T06:00:00Z", "labels": [{"name": "loom:failed"}, {"name": "lane:claude"}]}
    from datetime import datetime, timezone
    report = brain._ticket_lines([ticket], datetime(2026, 10, 18, 6, tzinfo=timezone.utc), stuck)
    dropped = brain._resume_item_line(marks[0], marks[0], False, None)
    tick = json.dumps({"holder": "lane-machine", "notes": [
        {"step": "merge", "text": "Two PRs failed their checks", "ts": "2026-10-18T21:35:52Z"}],
        "started_at": "2026-10-18T21:32:48Z", "step": "merge", "step_index": 4}, sort_keys=True)
    journal = json.dumps({"body": handoff.JOURNAL_MARKER + "\nTick 2026-10-18, muse\n"
                          "- push failed on #12"})
    return (["  " + x for x in stage] + report + [dropped, tick, journal]
            + ["### Reports owed before unlock", "## Loom brain tick (muse)"])


class BrainStatusTest(unittest.TestCase):
    def test_the_brains_status_output_is_never_kept(self):
        lines = _brain_status_lines()
        # The fixture really carries the words the watcher keys on.
        self.assertTrue(any("stage failed: held by" in x for x in lines), lines)
        self.assertTrue(any("REPEATEDLY STUCK" in x for x in lines), lines)
        self.assertIn("#### failed", lines)
        for line in lines + ["[2026-10-18 06:10:04] " + lines[0]]:
            self.assertFalse(watch.keep(line), line)

    def test_a_real_brain_error_is_still_kept(self):
        for line in ("brain step write failed: GET /repos/x/y -> 502",
                     "brain: push of abc failed for example-owner/repo-a#3",
                     "- stage failed: Traceback in the handoff reader",
                     "error: the journal for #12 failed to post",
                     "{not json} request failed"):
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

    def test_one_error_every_stage_hits_files_one_issue_naming_the_stages(self):
        # 2026-10-20: one unreadable repo's 404 failed every pass stage and
        # filed loom#1054-#1066 and loom#1087, one issue per stage.
        stages = ("run_factory", "publish_lanes", "hand_back")
        for _ in range(3):
            self.append(*("[2026-10-20T01:45:0%dZ] station: %s failed: GET /repos/o/r/issues -> 404"
                          % (i, stage) for i, stage in enumerate(stages)))
        self.run_at(T0)
        posts = [c for c in self.gh.calls if c == ("POST", "/repos/%s/issues" % LANES_REPO)]
        self.assertEqual(len(posts), 1)
        issue = self.gh.issues[1]
        self.assertTrue(issue["title"].startswith("station: station: <stage> failed: "),
                        issue["title"])
        self.assertIn("Stages: hand_back, publish_lanes, run_factory", issue["body"])
        self.assertIn("9 times in the last 30 minutes", issue["body"])
        # A later stage on the same error is counted there and named.
        self.append("station: assign_queued failed: GET /repos/o/r/issues -> 404")
        self.run_at(T0 + 3600)
        self.assertEqual(len(self.gh.issues), 1)
        self.assertEqual(len(self.gh.comments), 1)
        self.assertIn("Stages: assign_queued, hand_back, publish_lanes, run_factory",
                      self.gh.comments[0][1])

    def test_two_errors_from_one_stage_file_two_issues(self):
        self.append(*["station: run_factory failed: GET /repos/o/r/issues answered 403"] * 3)
        self.append(*["station: run_factory failed: GET /repos/o/r/issues answered 502"] * 3)
        self.run_at(T0)
        self.assertEqual(len(self.gh.issues), 2)
        self.assertEqual(sorted("403" in i["title"] for i in self.gh.issues.values()),
                         [False, True])

    def test_the_brains_status_output_files_nothing(self):
        # loom#871-#877: one tick's report filed six issues, one per line.
        for i in range(5):
            self.append(*_brain_status_lines())
            self.run_at(T0 + i * 300)
        self.assertEqual(self.gh.issues, {})
        self.assertEqual(self.saved()["sigs"], {})

    def test_a_retitled_issue_is_counted_when_its_signature_returns(self):
        # The brain rewrites a loom:triage issue: a new title, a new label.
        # The signature coming back a day later is the same root cause, so it
        # counts on that issue instead of filing a twin.
        self.append(LINE_403, LINE_403, LINE_403)
        self.run_at(T0)
        self.assertEqual(len(self.gh.issues), 1)
        self.gh.issues[1].update(title="Scans of a private repo answer 403",
                                 labels=["loom:briefed"])
        # The quiet day passes with no write budget, so the issue is not
        # closed; the signature's record is lost with the state (older state
        # pruned it) and only the issue map remembers where it was filed.
        self.run_at(T0 + watch.DAY + 60, write=False)
        state = self.saved()
        state["sigs"] = {}
        with open(self.state, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        self.append(LINE_403, LINE_403, LINE_403)
        self.run_at(T0 + 2 * watch.DAY)
        self.assertEqual(len(self.gh.issues), 1)
        self.assertEqual([n for n, _ in self.gh.comments], [1])
        self.assertIn("Last seen ", self.gh.comments[0][1])
        self.gh.issues[1]["state"] = "closed"
        self.run_at(T0 + 3 * watch.DAY + 60)
        self.append(LINE_403, LINE_403, LINE_403)
        self.run_at(T0 + 4 * watch.DAY)
        self.assertEqual(len(self.gh.issues), 2)

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

    def test_a_verify_run_the_station_already_handled_is_never_a_signature(self):
        # loom#463: a failed verify run costs its ticket a cooldown (or nothing,
        # when transient); the station says so in its log, which is status.
        for line in (
                "[2026-10-18T22:50:36Z] station: verify run example.repo-3-verify-1 failed [work]: "
                "no verify.json written; loom#3 stays shipped and is tried again after 6 hours",
                "[2026-10-18T22:50:36Z] station: verify run example.repo-3-verify-1 failed [transient]; "
                "loom#3 is due again now",
                "[2026-10-18T22:50:36Z] station: verify run example.repo-3-verify-1 failed; "
                "loom#3 stays shipped"):
            with self.subTest(line=line):
                self.assertFalse(watch.keep(line))
        self.assertTrue(watch.keep("[2026-10-18T22:50:36Z] station: verify failed: boom"))
        self.assertTrue(watch.keep("[2026-10-18T22:50:36Z] brain: verify run x failed; error"
                                   " writing state"))

    def test_a_relayed_ci_log_line_is_never_a_signature(self):
        # loom#880-#904: a brain agent read a CI run's log ("gh run view
        # --log": job, step and a seven-digit timestamp before each line)
        # and the passing tests' names and captured output said "failed" or
        # "error", so about 25 issues were filed for a green run.
        for line in (
                "unittest\tunittest (no test may skip)\t2026-10-18T20:36:20.3598990Z "
                "test_a_failed_proof_is_held (test_bus_brain.ProofTests."
                "test_a_failed_proof_is_held) ... ok",
                "unittest\tunittest (no test may skip)\t2026-10-18T20:36:21.0000001Z "
                "factory: re-briefed failed example-owner/repo-a#3 on codex",
                "unittest unittest (no test may skip) 2026-10-18T20:36:22.1234567Z "
                "station: could not post to the webhook (OSError); next pass",
                "2026-10-18T20:36:23.7654321Z ##[error]Process completed with exit code 1.",
                "[2026-10-18T20:40:00Z] unittest\tunittest\t2026-10-18T20:36:24.0000000Z error"):
            with self.subTest(line=line):
                self.assertFalse(watch.keep(line))
        # The brain's own lines, with its one-second stamp, are judged as usual.
        for line in ("[2026-10-18T20:36:20Z] brain: push failed",
                     "brain: the CI run at 2026-10-18T20:36:20.3598990Z failed",
                     "Error: 2026-10-18T20:36:20.3598990Z run failed"):
            with self.subTest(line=line):
                self.assertTrue(watch.keep(line))

    def test_a_green_ci_log_read_by_the_brain_files_nothing(self):
        for i in range(5):
            self.append("unittest\tunittest (no test may skip)\t2026-10-18T20:36:2%d.3598990Z "
                        "test_watch_error_%d (test_watch.FilingTest.test_watch_error_%d) ... ok"
                        % (i, i, i))
            self.run_at(T0 + i * 60)
        self.assertEqual(self.gh.issues, {})

    def test_a_closed_issue_is_not_counted_or_filed_again(self):
        self.append(LINE_403, LINE_403, LINE_403)
        self.run_at(T0)
        self.gh.issues[1]["state"] = "closed"
        self.append(LINE_403)
        self.run_at(T0 + 3600)
        self.run_at(T0 + 7200)
        self.assertEqual(self.gh.comments, [])
        self.assertEqual(len(self.gh.issues), 1)

    def quiet_issue(self, hours):
        """File an issue for LINE_403, then pass with it last seen hours ago."""
        self.append(LINE_403, LINE_403, LINE_403)
        self.run_at(T0 - hours * 3600)
        self.assertEqual(self.gh.issues[1]["state"], "open")
        self.run_at(T0)

    def test_an_issue_whose_signature_was_last_seen_25_hours_ago_closes_as_not_planned(self):
        self.quiet_issue(25)
        issue = self.gh.issues[1]
        self.assertEqual(issue["state"], "closed")
        self.assertEqual(issue["state_reason"], "not_planned")
        (note,) = self.gh.comments
        self.assertEqual(note[0], 1)
        self.assertIn(watch.time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                          watch.time.gmtime(T0 - 25 * 3600)), note[1])
        self.assertTrue(any(line.startswith("watch: closed quiet") for line in self.said))
        self.run_at(T0 + 600)
        self.assertEqual(len(self.gh.comments), 1)

    def quiet_labelled_issue(self, labels):
        """As quiet_issue(25), with the issue relabelled before the quiet pass,
        as GitHub returns labels (objects with a name)."""
        self.append(LINE_403, LINE_403, LINE_403)
        self.run_at(T0 - 25 * 3600)
        self.gh.issues[1]["labels"] = [{"name": name} for name in labels]
        self.run_at(T0)

    def test_a_quiet_signature_that_shipped_closes_as_completed(self):
        # loom#1934: a signature that became a ticket and shipped is done
        # work, not dropped work. Shadow mode off, as Loom runs since it left
        # shadow; in shadow the hold keeps the issue open.
        self.enterContext(mock.patch.dict(os.environ, {"LOOM_SHADOW": "0"}))
        for shipped in ("loom:shipped", "loom:verified"):
            with self.subTest(label=shipped):
                self.setUp()
                self.quiet_labelled_issue(["loom:ticket", shipped])
                issue = self.gh.issues[1]
                self.assertEqual(issue["state"], "closed")
                self.assertEqual(issue["state_reason"], "completed")
                (note,) = self.gh.comments
                self.assertIn("shipped", note[1])
                self.assertNotIn("not planned", note[1])

    def test_a_quiet_signature_that_is_an_unshipped_ticket_stays_open(self):
        # Shadow off: the ticket stays open by the watcher's own rule, not by
        # the shadow hold.
        self.enterContext(mock.patch.dict(os.environ, {"LOOM_SHADOW": "0"}))
        self.quiet_labelled_issue(["loom:ticket", "loom:running"])
        self.assertEqual(self.gh.issues[1]["state"], "open")
        self.assertEqual(self.gh.comments, [])
        self.assertFalse(any("closed quiet" in line for line in self.said))
        # The ticket closes on its own; the next pass writes nothing to it.
        self.gh.issues[1]["state"] = "closed"
        self.run_at(T0 + 600)
        self.assertEqual(self.gh.comments, [])
        self.assertNotIn("state_reason", self.gh.issues[1])

    def test_a_bare_quiet_signature_still_closes_as_not_planned(self):
        self.enterContext(mock.patch.dict(os.environ, {"LOOM_SHADOW": "0"}))
        self.quiet_labelled_issue(["loom:triage"])
        issue = self.gh.issues[1]
        self.assertEqual(issue["state"], "closed")
        self.assertEqual(issue["state_reason"], "not_planned")
        (note,) = self.gh.comments
        self.assertIn("not planned", note[1])

    def test_an_issue_whose_signature_was_last_seen_23_hours_ago_stays_open(self):
        self.quiet_issue(23)
        self.assertEqual(self.gh.issues[1]["state"], "open")
        self.assertEqual(self.gh.comments, [])
        self.assertFalse(any("closed quiet" in line for line in self.said))

    def test_a_signature_that_returns_after_a_quiet_close_files_a_new_issue(self):
        self.quiet_issue(25)
        self.append(LINE_403, LINE_403_B, LINE_403)
        self.run_at(T0 + 600)
        self.assertEqual(len(self.gh.issues), 2)
        self.assertEqual(self.gh.issues[2]["state"], "open")

    def test_a_quiet_close_waits_while_the_pass_may_not_write(self):
        self.append(LINE_403, LINE_403, LINE_403)
        self.run_at(T0 - 25 * 3600)
        self.run_at(T0, write=False)
        self.assertEqual(self.gh.issues[1]["state"], "open")
        self.run_at(T0 + 600)
        self.assertEqual(self.gh.issues[1]["state"], "closed")

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


# loom#1265: the spark brain rung's opencode printed its sandbox refusal in
# colour, and the watcher filed it with the escape codes in and the denied
# path cut off. These are the lines opencode 2.x run prints for a file tool
# its external_directory rule denies: the failed tool, then the error.
ESC = "\x1b"
REFUSAL_LINE = (
    ESC + "[91m" + ESC + "[1mError: " + ESC + "[0mThe user has specified a rule which prevents "
    "you from using this specific tool call. Here are some of the relevant rules "
    '[{"permission":"*","action":"allow","pattern":"*"},'
    '{"permission":"external_directory","pattern":"*","action":"ask"},'
    '{"permission":"external_directory","pattern":"/Users/example-user/.local/share/opencode/'
    'tool-output/*","action":"allow"},'
    '{"permission":"external_directory","pattern":"*","action":"deny"}]')
TOOL_FAILED_LINE = (ESC + "[0m\u2717 " + ESC + "[0mRead /Users/example-user/.config/gh/hosts.yml failed")
# The issue's own text shows the escapes in caret notation.
CARET_LINE = REFUSAL_LINE.replace(ESC, "^[")
TICK_START = "[2026-10-21T10:00:00Z] brain: tick start (lane spark, model spark/example-model, scope live)"
TICK_END = "[2026-10-21T10:20:00Z] brain: tick end (exit %d)"
REFUSAL_TITLE = "brain/spark: sandbox refused Read ~/.config/gh/hosts.yml"


class CleanLineTest(unittest.TestCase):
    def test_redact_preserves_ordinary_text_and_pipes(self):
        line = "ordinary words | x remain unchanged"
        self.assertEqual(watch.redact(line), line)

    def test_ticket_ids_are_redacted_in_signatures_and_examples(self):
        for ticket in ("example.private-thing-253", "example.private-thing-253-r2"):
            line = ticket + " failed: worker crashed"
            self.assertNotIn("private-thing", watch.signature(line))
            self.assertIn("<repo> failed: worker crashed", watch.signature(line))
            self.assertNotIn("private-thing", watch.redact(line))
            self.assertIn("<repo> failed: worker crashed", watch.redact(line))

    def test_escape_codes_are_stripped_raw_or_in_caret_notation(self):
        for line in (REFUSAL_LINE, CARET_LINE):
            out = watch.clean(line)
            self.assertNotIn(ESC, out)
            self.assertNotIn("^[", out)
            self.assertTrue(out.startswith("Error: The user has specified a rule"), out)
        self.assertEqual(watch.clean(TOOL_FAILED_LINE),
                         "\u2717 Read /Users/example-user/.config/gh/hosts.yml failed")

    def test_a_coloured_error_has_the_plain_lines_signature(self):
        plain = "Error: push failed for 3 repos"
        coloured = ESC + "[91m" + ESC + "[1mError: " + ESC + "[0mpush failed for 3 repos"
        self.assertEqual(watch.signature(coloured), watch.signature(plain))
        self.assertEqual(watch.signature(coloured), "Error: push failed for <n> repos")

    def test_a_long_example_keeps_its_end(self):
        line = "error: " + "word " * 600 + "and the cause comes last: tail-marker"
        out = watch.redact(line)
        self.assertTrue(out.startswith("error: word word"), out[:20])
        self.assertTrue(out.endswith("the cause comes last: tail-marker"), out[-40:])
        self.assertLessEqual(len(out), watch.EXAMPLE_MAX)


class LongLineTest(WatchBase):
    def test_a_filed_example_keeps_the_whole_line(self):
        line = ("[2026-10-13T10:00:01Z] error: " + "word " * 120
                + "and the cause comes last: tail-marker")
        self.append(line, line, line)
        self.run_at(T0)
        self.assertEqual(len(self.gh.issues), 1)
        self.assertIn("the cause comes last: tail-marker", self.gh.issues[1]["body"])


class RefusalTest(unittest.TestCase):
    def test_each_lane_clis_refusal_is_recognised(self):
        cases = [
            (watch.clean(REFUSAL_LINE), watch.clean(TOOL_FAILED_LINE),
             ("opencode", "Read", "~/.config/gh/hosts.yml", "external_directory * deny")),
            ("Claude requested permissions to write to /Users/example-user/notes.txt, but you "
             "haven't granted it yet.", "", ("claude", "Write", "~/notes.txt", None)),
            ("Permission to use Bash with command cat /etc/hosts has been denied.", "",
             ("claude", "Bash", None, None)),
            ("patch rejected: writing outside of the project; rejected by user approval settings",
             "", ("codex", "patch", None, "writing outside of the project")),
            ('Tool execution for "WebFetch" denied by policy.', "",
             ("gemini", "WebFetch", None, None)),
        ]
        for line, prev, (cli, tool, path, rule) in cases:
            found = watch.refusal(line, prev)
            self.assertIsNotNone(found, line)
            self.assertEqual(found["cli"], cli, line)
            self.assertEqual(found["tool"], tool, line)
            if path is not None:
                self.assertEqual(found["path"], path, line)
            if rule is not None:
                self.assertEqual(found["rule"], rule, line)
            text.check(found["path"] + " " + found["rule"])

    def test_an_ordinary_error_is_no_refusal(self):
        for line in ("Error: push failed", "Connection refused", "HTTP 403 from the API",
                     "permission denied (publickey)"):
            self.assertIsNone(watch.refusal(line, ""), line)


class AgentRefusalTest(WatchBase):
    COMPONENT = "brain"

    def tick(self, now, refusals=1, exit_code=0, start=True):
        lines = [TICK_START] if start else []
        for _ in range(refusals):
            lines += [TOOL_FAILED_LINE, REFUSAL_LINE]
        self.append(*(lines + [TICK_END % exit_code]))
        self.run_at(now)

    def test_a_refusal_the_run_recovered_from_files_nothing(self):
        # Three refusals in one tick that ended well: the sandbox did its job.
        self.tick(T0, refusals=3, exit_code=0)
        self.assertEqual(self.gh.issues, {})
        state = self.saved()
        self.assertEqual(state["sigs"], {})
        self.assertEqual(state["refusals"][REFUSAL_TITLE]["total"], 3)

    def test_loom_1265s_lines_file_nothing(self):
        # The lines loom#1265 was filed for, three times in 30 minutes.
        for i in range(3):
            self.append(CARET_LINE)
            self.run_at(T0 + 600 * i)
        self.assertEqual(self.gh.issues, {})

    def test_a_refusal_in_a_failed_run_files_one_issue_naming_the_path(self):
        self.tick(T0, refusals=1, exit_code=1)
        self.assertEqual(len(self.gh.issues), 1)
        issue = self.gh.issues[1]
        self.assertEqual(issue["title"], REFUSAL_TITLE)
        body = issue["body"]
        text.check(issue["title"] + "\n" + body)
        for want in ("~/.config/gh/hosts.yml", "external_directory * deny", "opencode",
                     "Read", "exit 1"):
            self.assertIn(want, body)
        self.assertNotIn("example-user", issue["title"] + body)
        self.assertNotIn(ESC, body)
        # The same refusal breaking later runs updates that one issue: one
        # count comment, then edited in place.
        self.tick(T0 + 3700, refusals=1, exit_code=1)
        self.tick(T0 + 7400, refusals=1, exit_code=1)
        self.assertEqual(len(self.gh.issues), 1)
        self.assertEqual(len(self.gh.comments), 1)
        self.assertEqual(len(self.gh.edits), 1)
        self.assertIn("~/.config/gh/hosts.yml", self.gh.edits[0][1])

    def test_a_refusal_repeated_in_one_run_files_one_issue(self):
        self.tick(T0, refusals=watch.REFUSAL_REPEAT - 1, exit_code=0)
        self.assertEqual(self.gh.issues, {})
        self.tick(T0 + 600, refusals=watch.REFUSAL_REPEAT, exit_code=0)
        self.assertEqual(len(self.gh.issues), 1)
        self.assertEqual(self.gh.issues[1]["title"], REFUSAL_TITLE)
        self.assertIn("%d times in one run" % watch.REFUSAL_REPEAT, self.gh.issues[1]["body"])

    def test_a_refusal_across_runs_in_a_day_files_one_issue(self):
        for i in range(watch.REFUSAL_RUNS - 1):
            self.tick(T0 + 3600 * i, refusals=1, exit_code=0)
        self.assertEqual(self.gh.issues, {})
        self.tick(T0 + 3600 * watch.REFUSAL_RUNS, refusals=1, exit_code=0)
        self.assertEqual(len(self.gh.issues), 1)
        self.assertIn("in %d runs" % watch.REFUSAL_RUNS, self.gh.issues[1]["body"])
        self.tick(T0 + 3600 * (watch.REFUSAL_RUNS + 1), refusals=1, exit_code=0)
        self.assertEqual(len(self.gh.issues), 1)

    def test_the_failed_tool_line_before_a_refusal_is_no_signature(self):
        self.tick(T0, refusals=1)
        self.tick(T0 + 60, refusals=1)
        self.tick(T0 + 120, refusals=1)
        self.assertEqual(self.saved()["sigs"], {})

    def test_a_lane_run_that_made_no_change_after_a_refusal_files_one_issue(self):
        self.append("[2026-10-21T10:00:00Z] T042 start, lane spark",
                    TOOL_FAILED_LINE, REFUSAL_LINE,
                    "[2026-10-21T10:10:00Z] T042 failed: [work] agent made no commit")
        self.run_at(T0)
        self.assertEqual(len(self.gh.issues), 1)
        self.assertIn("no change", self.gh.issues[1]["body"])

    def test_run_marks(self):
        self.assertEqual(watch.run_mark(TICK_START), ("start", "spark"))
        self.assertEqual(watch.run_mark(TICK_END % 0), ("end", "ok"))
        self.assertEqual(watch.run_mark(TICK_END % 124), ("end", "failed (exit 124)"))
        for line, outcome in (("T042 done", "ok"), ("T042 done, answer posted", "ok"),
                              ("T042 done, answered: nothing to change", "no change"),
                              ("T042 failed: [transient] agent exited 124",
                               "ended early ([transient])"),
                              ("T042 failed: [work] tests failed", "failed ([work])")):
            self.assertEqual(watch.run_mark("[2026-10-21T10:10:00Z] " + line), ("end", outcome))
        self.assertIsNone(watch.run_mark("station: reported o/r#5 failed: [work] x"))

    def test_a_run_cut_short_by_a_limit_is_not_hurt_by_its_refusal(self):
        self.append("[2026-10-21T10:00:00Z] T042 start, lane spark",
                    TOOL_FAILED_LINE, REFUSAL_LINE,
                    "[2026-10-21T10:10:00Z] T042 failed: [limit] usage limit reached")
        self.run_at(T0)
        self.assertEqual(self.gh.issues, {})


class PrivateRefusalPathTest(WatchBase):
    COMPONENT = "brain"
    PRIVATE = ("secret-repo",)

    def test_a_private_repo_name_in_a_denied_path_is_hidden(self):
        tool = TOOL_FAILED_LINE.replace(".config/gh/hosts.yml", "loom/work/secret-repo/a.py")
        self.append(TICK_START, tool, REFUSAL_LINE, TICK_END % 1)
        self.run_at(T0)
        issue = self.gh.issues[1]
        self.assertEqual(issue["title"], "brain/spark: sandbox refused Read ~/loom/work/<repo>/a.py")
        self.assertNotIn("secret-repo", issue["title"] + issue["body"])


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

    def test_a_log_rotated_away_keeps_being_read_once_its_writer_makes_a_new_one(self):
        self.append("x" * 500)
        self.run_at(T0)
        logrotate.rotate(os.path.dirname(self.log), 100, 2, T0 + 30, lambda _: None,
                         writers=lambda _: False)
        self.assertFalse(os.path.exists(self.log))
        self.run_at(T0 + 60)
        self.append(LINE_403, LINE_403, LINE_403)
        self.run_at(T0 + 120)
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

    def test_the_station_hides_its_goal_repos_names_in_a_denied_path(self):
        tool = TOOL_FAILED_LINE.replace(".config/gh/hosts.yml", "loom/work/repo-a/a.py")
        self.append(TICK_START, tool, REFUSAL_LINE, TICK_END % 1)
        st = self.station(LANES_REPO)
        with mock.patch.object(st, "_deferred", return_value=False):
            st.watch_logs()
        title = self.gh.issues[1]["title"]
        self.assertIn("~/loom/work/<repo>/a.py", title)
        self.assertNotIn("repo-a", title + self.gh.issues[1]["body"])

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
