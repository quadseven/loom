"""A repo's read-only live-evidence adapter proves Live boxes (loom#1770).

bus/live_adapter.py runs the adapter a target repo declares in
.loom/live-evidence.json; bus/live_proof.py reads the templated Live forms
(`log:<source>`, `sql:<SELECT ...>`) it answers. These tests drive the
station's close pass with a fake adapter and with a real `.loom/` script: a
proven box closes the ticket quoting the evidence, a failed read leaves it
open with the reason, and a write-shaped query, a shell argv or a repo the
operator has not opted in runs nothing.
"""
from __future__ import annotations

import json
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import live_adapter, live_proof, proof, station  # noqa: E402
from bus.github import GitHub  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

import test_bus_station as tbs  # noqa: E402
import test_station_live_proof as tslp  # noqa: E402

REPO = tbs.REPO
HOUR = 3600
stamp = tslp.stamp
HEAD = ("Difficulty: easy\n\n## Why\nBecause.\n\n## Acceptance criteria\n"
        "- [ ] A test passes\n")
LOG_BODY = HEAD + "- [ ] Live: after the ship, `log:worker` names `boat leg taken`\n"
# Ticket text under test, never run as SQL: the refusal is what is tested.
SQL_BODY = HEAD + ("- [ ] Live: `sql:SELECT COUNT(*) AS stuck FROM queue WHERE state = 'held'` "  # noqa: S608
                   "answers `stuck=0`\n")
WRITE_SQL_BODY = HEAD + "- [ ] Live: `sql:UPDATE queue SET state = 'done'` answers `ok`\n"  # noqa: S608
ABSENT_BODY = HEAD + ("- [ ] Live: over 24 hours after the ship, `log:worker` never names "
                      "`drowned below the pier`\n")
SCRIPT = """#!/bin/sh
# A fake adapter: prints its arguments and environment, as a log line.
echo "loom-evidence-window-hours: 48"
printf '%s worker: %s for %s since %s token=[%s]\\n' "$FAKE_STAMP" "$3" "$1" "$2" "$GH_TOKEN"
"""
MANIFEST = {"version": 1,
            "log": {"argv": [".loom/live-evidence/log", "{source}", "{since}", "{needle}"],
                    "sources": ["worker"], "timeout": 20},
            "sql": {"argv": [".loom/live-evidence/sql", "{sql}"], "timeout": 20}}


class FakeAdapter:
    """An adapter answer per call, with every call recorded."""

    def __init__(self, log_lines=(), rows=(), hours=None, count=None, error=""):
        self.log_lines, self.rows = list(log_lines), list(rows)
        self.hours, self.count, self.error = hours, count, error
        self.calls = []

    def log(self, source, literal, since):
        self.calls.append(("log", source, literal, since))
        if self.error:
            return live_proof.Answer(None, error=self.error)
        return live_proof.Answer([l for l in self.log_lines if literal in l],
                                 hours=self.hours, count=self.count)

    def sql(self, statement):
        self.calls.append(("sql", statement))
        if self.error:
            return live_proof.Answer(None, error=self.error)
        return live_proof.Answer(list(self.rows))


class StationAdapterProofTest(tbs.Base):
    """The station closes a shipped ticket on its adapter's evidence."""
    NUMBER = 1

    def setUp(self):
        super().setUp()
        self.fake.private = False
        self.now = float(int(time.time()))

    ship = tslp.StationLiveProofTest.ship
    status_text = tslp.StationLiveProofTest.status_text
    plain_comments = tslp.StationLiveProofTest.plain_comments

    def adapter_pass(self, fake):
        st = station.Station(GitHub("token", transport=self.fake), self.settings, now=self.now)
        sources = live_proof.Sources(gh=st.gh, source_log=fake.log, sql=fake.sql)
        with mock.patch.object(station.Station, "_proof_sources", return_value=sources):
            st.close_done_tickets()
        return st

    def note(self):
        [note] = [c for c in self.plain_comments() if "Every acceptance criterion has evidence" in c]
        return note

    def test_a_log_source_box_closes_quoting_the_adapter_lines(self):
        self.ship(LOG_BODY, hours_ago=2)
        fake = FakeAdapter(["%s worker: boat leg taken at the dock" % stamp(self.now - HOUR),
                            "%s worker: boat leg taken before" % stamp(self.now - 5 * HOUR)])
        self.adapter_pass(fake)
        issue = self.fake.issues[self.NUMBER]
        self.assertEqual((issue["state"], issue.get("state_reason")), ("closed", "completed"))
        self.assertEqual(fake.calls, [("log", "worker", "boat leg taken", self.shipped_at)])
        note = self.note()
        self.assertIn("named 1 times in the `worker` log since the deploy", note)
        self.assertIn("> %s worker: boat leg taken at the dock" % stamp(self.now - HOUR), note)
        # The line stamped before the ship is never its evidence.
        self.assertNotIn("boat leg taken before", note)
        self.assertIn("- [x] Live: after the ship", issue["body"])
        [record] = [proof.read(c) for c in self.plain_comments() if proof.PROOF_MARKER in c]
        self.assertTrue(any("live-evidence adapter" in c["evidence"] for c in record["criteria"]))

    def test_a_count_the_adapter_reports_is_evidence_without_a_line(self):
        self.ship(LOG_BODY, hours_ago=2)
        self.adapter_pass(FakeAdapter([], count=7))
        self.assertEqual(self.fake.issues[self.NUMBER]["state"], "closed")
        self.assertIn("`boat leg taken` named 7 times in the `worker` log", self.note())

    def test_a_sql_box_closes_on_the_rows_it_answers(self):
        self.ship(SQL_BODY, hours_ago=2)
        fake = FakeAdapter(rows=["stuck=0"])
        self.adapter_pass(fake)
        self.assertEqual(self.fake.issues[self.NUMBER]["state"], "closed")
        self.assertEqual(fake.calls, [
            ("sql", "SELECT COUNT(*) AS stuck FROM queue WHERE state = 'held'")])
        self.assertIn("the query answered 1 rows, found `stuck=0`", self.note())

    def test_a_failing_read_stays_open_with_the_reason(self):
        self.ship(SQL_BODY, hours_ago=2)
        self.adapter_pass(FakeAdapter(error="the adapter exited 2"))
        issue = self.fake.issues[self.NUMBER]
        self.assertEqual(issue["state"], "open")
        self.assertNotIn("loom:verified", self.fake.names(self.NUMBER))
        text = self.status_text()
        self.assertIn("Live proof missing:", text)
        self.assertIn("the repo's SQL adapter gave no answer (the adapter exited 2)", text)
        self.assertIn("next check %s" % stamp(self.now + live_proof.RETRY), text)

    def test_a_write_query_is_refused_and_never_reaches_the_adapter(self):
        self.ship(WRITE_SQL_BODY, hours_ago=2)
        fake = FakeAdapter(rows=["ok"])
        self.adapter_pass(fake)
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.fake.issues[self.NUMBER]["state"], "open")

    def test_an_absence_closes_only_over_a_full_window(self):
        self.ship(ABSENT_BODY, hours_ago=30)
        short = FakeAdapter([], hours=10, count=0)
        self.adapter_pass(short)
        self.assertEqual(self.fake.issues[self.NUMBER]["state"], "open")
        self.assertIn("read 10.0 hours, short of the 24.0 needed", self.status_text())
        state = os.path.join(self.home, "state", "station", station.LIVE_PROOF_FILE)
        os.remove(state)
        self.adapter_pass(FakeAdapter([], hours=30, count=0))
        self.assertEqual(self.fake.issues[self.NUMBER]["state"], "closed")
        self.assertIn("`drowned below the pier` absent from the `worker` log", self.note())

    def test_a_present_absent_string_keeps_the_ticket_open(self):
        self.ship(ABSENT_BODY, hours_ago=30)
        self.adapter_pass(FakeAdapter(["%s worker: drowned below the pier" % stamp(self.now - HOUR)],
                                      hours=30))
        self.assertEqual(self.fake.issues[self.NUMBER]["state"], "open")
        self.assertIn("names `drowned below the pier` 1 times", self.status_text())


class StationManifestTest(tbs.Base):
    """The station reads the adapter from the repo's default branch and runs
    it only for a repo the operator opted in."""

    def setUp(self):
        super().setUp()
        self.files = {live_adapter.MANIFEST: json.dumps(MANIFEST),
                      ".loom/live-evidence/log": SCRIPT, ".loom/live-evidence/sql": SCRIPT}

    def station(self, repos):
        self.settings.live_evidence_repos = list(repos)
        st = station.Station(GitHub("token", transport=self.fake), self.settings, now=time.time())
        st._contents_text = lambda repo, path: self.files.get(path)
        return st

    def test_an_opted_in_repo_runs_its_script_with_whole_arguments_and_a_bare_env(self):
        sources = self.station([REPO])._proof_sources(REPO)
        needle = "a; rm -rf / `x`"
        with mock.patch.dict(os.environ, {"GH_TOKEN": "ghp_shouldnotpass", "FAKE_STAMP": "x"}):
            answer = sources.source_log("worker", needle, 1_900_000_000)
        self.assertEqual(answer.error, "")
        self.assertEqual(answer.hours, 48)
        [line] = answer.lines
        self.assertIn("%s for worker since 2030-03-17T17:46:40Z" % needle, line)
        self.assertIn("token=[]", line)

    def test_a_repo_not_opted_in_gets_no_adapter_and_reads_nothing(self):
        st = self.station([])
        st._contents_text = mock.Mock(side_effect=AssertionError("read a manifest"))
        sources = st._proof_sources(REPO)
        self.assertIsNone(sources.source_log)
        self.assertIsNone(sources.sql)

    def test_a_manifest_that_does_not_parse_gives_no_adapter(self):
        self.files[live_adapter.MANIFEST] = "{not json"
        self.assertIsNone(self.station([REPO])._proof_sources(REPO).source_log)


class ManifestTest(unittest.TestCase):

    def parse(self, manifest, files=None):
        files = {".loom/live-evidence/log": SCRIPT, ".loom/live-evidence/sql": SCRIPT,
                 **(files or {})}
        return live_adapter.parse(json.dumps(manifest), files.get)

    def with_log(self, argv, **extra):
        return dict({"version": 1, "log": dict({"argv": argv, "sources": ["worker"]}, **extra)})

    def test_the_example_manifest_parses(self):
        adapter, why = self.parse(MANIFEST)
        self.assertEqual(why, "")
        self.assertEqual(adapter.sources, ("worker",))
        self.assertEqual(set(adapter.scripts), {".loom/live-evidence/log", ".loom/live-evidence/sql"})

    def test_shell_shaped_and_write_shaped_argv_are_refused(self):
        refused = {
            "shell": ["sh", "-c", "{needle}", "{source}"],
            "absolute path": ["/bin/sh", "{source}", "{needle}"],
            "interpreter": ["python3", "x", "{source}", "{needle}"],
            "placeholder in a literal": [".loom/live-evidence/log", "--match={needle}",
                                         "{source}"],
            "ticket picks the verb": ["archive-logs", "{needle}", "{source}"],
            "kubectl write": ["kubectl", "delete", "{source}", "{needle}"],
            "exec": ["kubectl", "get", "exec", "{source}", "{needle}"],
            "escape .loom": [".loom/../bin/sh", "{source}", "{needle}"],
            "no needle": [".loom/live-evidence/log", "{source}"],
            "unknown placeholder": [".loom/live-evidence/log", "{source}", "{needle}", "{sql}"],
        }
        for name, argv in refused.items():
            with self.subTest(name):
                adapter, why = self.parse(self.with_log(argv))
                self.assertIsNone(adapter)
                self.assertTrue(why)

    def test_a_timeout_past_the_cap_and_a_missing_script_are_refused(self):
        self.assertIsNone(self.parse(self.with_log(
            [".loom/live-evidence/log", "{source}", "{needle}"], timeout=9999))[0])
        adapter, why = live_adapter.parse(json.dumps(self.with_log(
            [".loom/live-evidence/gone", "{source}", "{needle}"])), {}.get)
        self.assertIsNone(adapter)
        self.assertIn("not on the default branch", why)

    def test_a_bare_read_command_parses(self):
        adapter, why = self.parse(self.with_log(["archive-logs", "report", "{source}", "{since}",
                                                 "{needle}"]))
        self.assertEqual(why, "")
        self.assertEqual(adapter.scripts, {})


class RunnerTest(unittest.TestCase):

    def runner(self, script=SCRIPT, timeout=20, popen=None):
        manifest = dict(MANIFEST)
        manifest["log"] = dict(MANIFEST["log"], timeout=timeout)
        adapter, why = live_adapter.parse(json.dumps(manifest), {
            ".loom/live-evidence/log": script, ".loom/live-evidence/sql": script}.get)
        self.assertEqual(why, "")
        kwargs = {"popen": popen} if popen else {}
        return live_adapter.Runner(adapter, env={"PATH": "/usr/bin:/bin"}, **kwargs)

    def test_a_non_select_query_is_refused_before_anything_runs(self):
        popen = mock.Mock(side_effect=AssertionError("ran"))
        runner = self.runner(popen=popen)
        for statement in ("UPDATE t SET a = 1", "SELECT 1; DROP TABLE t",
                          "SELECT * INTO OUTFILE '/tmp/x' FROM t", "DELETE FROM t"):
            with self.subTest(statement):
                answer = runner.query(statement)
                self.assertIsNone(answer.lines)
                self.assertTrue(answer.error.startswith("refused:"))
        popen.assert_not_called()

    def test_select_and_show_run(self):
        runner = self.runner("#!/bin/sh\necho \"row: $1\"\n")
        self.assertEqual(runner.query("SELECT a FROM t").lines, ["row: SELECT a FROM t"])
        self.assertEqual(runner.query("SHOW TABLES").lines, ["row: SHOW TABLES"])

    def test_an_unserved_source_is_no_answer(self):
        answer = self.runner().log("database", "boat leg taken", 0)
        self.assertIsNone(answer.lines)
        self.assertIn("serves no `database` log", answer.error)

    def test_a_failed_exit_a_timeout_and_too_much_output_are_no_answer(self):
        self.assertIn("exited 3", self.runner("#!/bin/sh\nexit 3\n").log(
            "worker", "boat leg taken", 0).error)
        started = time.time()
        self.assertIn("timed out after 1 seconds", self.runner(
            "#!/bin/sh\nsleep 30\n", timeout=1).log("worker", "boat leg taken", 0).error)
        self.assertLess(time.time() - started, 15)
        self.assertIn("more than", self.runner("#!/bin/sh\nyes boat\n").log(
            "worker", "boat leg taken", 0).error)

    def test_exit_one_is_nothing_matched_and_a_count_is_read(self):
        answer = self.runner("#!/bin/sh\necho 'loom-evidence-count: 0'\nexit 1\n").log(
            "worker", "boat leg taken", 0)
        self.assertEqual((answer.lines, answer.count, answer.error), ([], 0, ""))


class StepOfTest(unittest.TestCase):

    def test_templated_forms_name_adapter_steps(self):
        step, why = live_proof.step_of("Live: `log:worker` names `boat leg taken`")
        self.assertEqual((step.kind, step.source, step.target, why),
                         (live_proof.LOG, "worker", "boat leg taken", ""))
        step, _ = live_proof.step_of("Live: `sql:SHOW TABLES` answers `queue`")
        self.assertEqual((step.kind, step.target, step.expects),
                         (live_proof.QUERY, "SHOW TABLES", ("queue",)))
        step, why = live_proof.step_of("Live: `sql:DELETE FROM queue` answers `0`")
        self.assertIsNone(step)
        self.assertIn("not one read-only SELECT or SHOW", why)

    def test_a_template_span_is_not_a_string_to_find(self):
        from bus import evidence
        self.assertEqual(evidence.needles("`log:worker` names `boat leg taken`"),
                         ["boat leg taken"])


if __name__ == "__main__":
    unittest.main()
