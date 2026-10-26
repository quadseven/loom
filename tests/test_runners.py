"""bus/runners.py: lane runs on cluster runners (quadseven/loom#1824). The
station dispatches a runner slot's brief to the lane workflow once, follows
the run, and writes the run's sealed report back into the loom home where a
local runner writes its files. A fake GitHub answers; the reports are sealed
by lanes/k8s/lane_job.py itself, so the two halves are tested together."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bus import runners, station  # noqa: E402
from bus.github import GitHubError, RateLimited, Response  # noqa: E402

REPO = "owner/loom"
APP = "loom-station[bot]"
TICKET = "owner.app-7"
BRIEF = ("# Ticket\n\nRepo: owner/app\nBranch: loom/owner.app-7\n"
         "Lane: opencode-go\n\nDo the thing.\n")


def load_job():
    spec = importlib.util.spec_from_file_location("lane_job", ROOT / "lanes" / "k8s" / "lane_job.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeGitHub:
    """Answers the Actions endpoints the station uses; records each call."""

    def __init__(self):
        self.calls = []
        self.dispatch_error = None
        self.runs = {}          # run id -> run dict
        self.artifacts = {}     # run id -> [artifact]
        self.zips = {}          # artifact id -> bytes
        self.next_run = 900

    def rest(self, method, path, body=None, etag=None, ok=()):
        self.calls.append((method, path, body))
        if method == "POST" and path.endswith("/dispatches"):
            if self.dispatch_error is not None:
                raise self.dispatch_error
            self.next_run += 1
            title = "lane %s %s" % (body["inputs"]["slot"], body["inputs"]["run_id"])
            self.runs[self.next_run] = {"id": self.next_run, "status": "queued",
                                        "display_title": title, "head_branch": "main",
                                        "path": ".github/workflows/lane.yml",
                                        "event": "workflow_dispatch",
                                        "triggering_actor": {"login": APP}}
            return Response(200, {"workflow_run_id": self.next_run})
        if method == "POST" and path.endswith("/cancel"):
            return Response(202, {})
        parts = path.split("/")
        if path.endswith("/artifacts"):
            return Response(200, {"artifacts": self.artifacts.get(int(parts[-2]), [])})
        if path.endswith("/zip"):
            return Response(302, None, headers={"location": "https://blob.example.com/%s" % parts[-2]})
        if "/actions/runs/" in path:
            return Response(200, self.runs[int(parts[-1])])
        raise AssertionError("unexpected request %s %s" % (method, path))

    def posts(self, suffix):
        return [c for c in self.calls if c[0] == "POST" and c[1].endswith(suffix)]


class RunnersTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home = self.root / "loom"
        self.inbox = self.home / "briefs" / "opencode-go-2"
        self.inbox.mkdir(parents=True)
        self.gh = FakeGitHub()
        self.said = []
        self.public = {"owner/app": True}
        self.now = 1_800_000_000.0
        self.env = {"LOOM_RUNNER_SLOTS": "opencode-go-2", "LOOM_OPENCODE_GO_KEY_SSM": "/x/oc",
                    "LOOM_OPENCODE_GO_2_MODEL": "opencode-go/beam-free",
                    "LOOM_GITHUB_APP_KEY_SSM": "/station/app", "LOOM_LANES_GITHUB_APP_ID": "42",
                    "LOOM_REPO_OWNERS": "owner", "HOME": "/nowhere"}

    def tearDown(self):
        self._tmp.cleanup()

    def runner(self, now=None, actor=APP):
        return runners.Runners(self.gh, str(self.home), REPO, ["opencode-go-2"], self.env,
                               lambda repo: self.public.get(repo, False), self.said.append,
                               actor=actor, now=self.now if now is None else now, fetch=self.fetch)

    def fetch(self, url):
        return self.gh.zips[int(url.rsplit("/", 1)[1])]

    def drop_brief(self, text=BRIEF, name=TICKET):
        (self.inbox / (name + ".md")).write_text(text)

    def failure_line(self, name=TICKET):
        return (self.home / "logs" / "opencode-go-2" / (name + ".log")).read_text().splitlines()[-1]

    def beat(self):
        return json.loads((self.home / "state" / "usage" / "opencode-go-2.json").read_text())

    # -- configuration -------------------------------------------------------
    def test_no_slot_is_a_runner_slot_by_default(self):
        self.assertEqual(runners.runner_slots({}), [])
        self.assertEqual(runners.runner_slots({"LOOM_RUNNER_SLOTS": "codex claude-9 zen, free-kilo"}),
                         ["zen", "free-kilo"])

    def test_a_runner_slot_takes_public_repos_only(self):
        with mock.patch.dict(os.environ, {"LOOM_RUNNER_SLOTS": "opencode-go-2"}):
            self.assertTrue(station.needs_public_repo("opencode-go-2", "opencode-go/beam-free"))
            self.assertFalse(station.needs_public_repo("opencode-go-3", "opencode-go/beam-free"))
        with mock.patch.dict(os.environ, {"LOOM_RUNNER_SLOTS": ""}):
            self.assertFalse(station.needs_public_repo("opencode-go-2", "opencode-go/beam-free"))

    def test_the_settings_sent_are_the_slots_and_the_lanes_app_never_the_stations(self):
        settings = runners.lane_settings("opencode-go-2", self.env)
        self.assertEqual(settings, {"LOOM_GITHUB_APP_ID": "42", "LOOM_OPENCODE_GO_2_MODEL": "opencode-go/beam-free",
                                    "LOOM_OPENCODE_GO_KEY_SSM": "/x/oc", "LOOM_REPO_OWNERS": "owner"})
        with self.assertRaises(ValueError):
            runners.parse_settings("opencode-go-2", "LOOM_OPENCODE_GO_OPENCODE=/tmp/evil\n")
        with self.assertRaises(ValueError):
            runners.parse_settings("opencode-go-2", "LOOM_SANDBOX=seatbelt\n")

    def test_the_installer_gives_the_station_its_runner_slots_settings(self):
        env = dict(self.env, LOOM_ZEN_KEY_SSM="/x/zen", LOOM_LANES_GITHUB_APP_KEY_SSM="/x/lanes")
        self.assertEqual(runners.station_env(env), {
            "LOOM_LANES_GITHUB_APP_ID": "42", "LOOM_LANES_GITHUB_APP_KEY_SSM": "/x/lanes",
            "LOOM_OPENCODE_GO_KEY_SSM": "/x/oc", "LOOM_RUNNER_SLOTS": "opencode-go-2"})

    # -- dispatch ------------------------------------------------------------
    def test_a_brief_is_dispatched_once_with_its_inputs(self):
        self.drop_brief()
        self.runner().tend()
        posts = self.gh.posts("/dispatches")
        self.assertEqual(len(posts), 1)
        method, path, body = posts[0]
        self.assertEqual(path, "/repos/owner/loom/actions/workflows/lane.yml/dispatches")
        self.assertEqual(body["ref"], "main")
        inputs = body["inputs"]
        self.assertEqual(inputs["slot"], "opencode-go-2")
        self.assertEqual(inputs["ticket"], TICKET)
        self.assertEqual(inputs["brief"], BRIEF)
        self.assertRegex(inputs["run_id"], r"^[0-9a-f]{32}$")
        self.assertTrue(inputs["report_key"].startswith("-----BEGIN PUBLIC KEY-----"))
        self.assertIn("LOOM_OPENCODE_GO_KEY_SSM=/x/oc\n", inputs["settings"])
        self.assertNotIn("/station/app", inputs["settings"])
        # Still waiting: the brief stays in the inbox, the lane reads as alive.
        self.assertTrue((self.inbox / (TICKET + ".md")).exists())
        self.assertTrue((self.home / "state" / "usage" / "opencode-go-2.alive").exists())
        for _ in range(3):
            self.runner(now=self.now + 60).tend()
        self.assertEqual(len(self.gh.posts("/dispatches")), 1)

    def test_a_dispatch_whose_answer_is_lost_is_never_sent_again(self):
        self.drop_brief()
        self.gh.dispatch_error = GitHubError("request failed: timeout", 0, "")
        self.runner().tend()
        self.gh.dispatch_error = None
        # Parked so another lane takes the ticket; the same brief put back
        # in the inbox is still not sent again.
        self.assertTrue((self.inbox / "failed" / (TICKET + ".md")).exists())
        self.assertIn("failed: [transient] the runner dispatch got no answer", self.failure_line())
        self.drop_brief()
        self.runner(now=self.now + 120).tend()
        self.assertEqual(len(self.gh.posts("/dispatches")), 1)

    def test_runner_slots_need_the_station_app(self):
        self.drop_brief()
        self.runner(actor=None).tend()
        self.assertEqual(self.gh.calls, [])

    def test_a_budget_refusal_sends_the_brief_on_a_later_pass(self):
        self.drop_brief()
        self.gh.dispatch_error = RateLimited("budget", 403, "", 0)
        with self.assertRaises(RateLimited):
            self.runner().tend()
        self.gh.dispatch_error = None
        self.runner(now=self.now + 60).tend()
        self.assertEqual(len(self.gh.posts("/dispatches")), 2)

    def test_an_unreadable_ledger_stops_every_dispatch(self):
        self.drop_brief()
        ledger = self.home / "state" / "runners" / "runs.json"
        ledger.parent.mkdir(parents=True)
        ledger.write_text("{not json")
        with self.assertRaises(runners.ReportError):
            self.runner().tend()
        self.assertEqual(self.gh.posts("/dispatches"), [])

    def test_a_brief_for_a_private_repo_is_not_dispatched(self):
        self.public["owner/app"] = False
        self.drop_brief()
        self.runner().tend()
        self.assertEqual(self.gh.posts("/dispatches"), [])
        self.assertTrue((self.inbox / "failed" / (TICKET + ".md")).exists())
        self.assertIn("failed: [transient] runner slots take public repos only", self.failure_line())

    def test_a_brief_past_the_input_limit_stays_with_a_local_lane(self):
        self.drop_brief(BRIEF + "x" * runners.INPUT_LIMIT)
        self.runner().tend()
        self.assertEqual(self.gh.posts("/dispatches"), [])
        self.assertTrue((self.inbox / "failed" / (TICKET + ".md")).exists())
        self.assertIn("failed: [transient] the brief is too large to dispatch", self.failure_line())

    # -- following runs ------------------------------------------------------
    def dispatched(self):
        self.drop_brief()
        self.runner().tend()
        inputs = self.gh.posts("/dispatches")[0][2]["inputs"]
        self.mac_key = inputs["report_mac_key"]
        return self.gh.next_run, inputs["run_id"]

    def seal(self, run_id, files, beat=None, mac_key=None):
        """A report artifact as the lane job seals it: `files` written under a
        job's loom home, then lane_job.py report."""
        job = load_job()
        jobdir = self.root / "job"
        shutil.rmtree(jobdir, ignore_errors=True)
        home = jobdir / "home"
        for rel, text in files.items():
            (home / rel).parent.mkdir(parents=True, exist_ok=True)
            (home / rel).write_text(text)
        if beat is not None:
            (home / "state" / "usage").mkdir(parents=True, exist_ok=True)
            (home / "state" / "usage" / "opencode-go-2.json").write_text(json.dumps(beat))
        (jobdir / "inputs.json").write_text(json.dumps({"slot": "opencode-go-2", "ticket": TICKET,
                                                        "run_id": run_id}))
        key = self.home / "state" / "runners" / "report-key.pem.pub"
        shutil.copy(key, jobdir / "report-key.pem")
        (jobdir / "report-mac-key").write_text(mac_key or self.mac_key)
        with mock.patch.dict(os.environ, {"LOOM_JOB_DIR": str(jobdir)}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(job.main(["lane_job.py", "report"]), 0)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for name in ("key.enc", "report.enc", "report.mac"):
                zf.write(jobdir / "report" / name, name)
        return buf.getvalue()

    def finish(self, run, run_id, artifact, conclusion="success"):
        self.gh.runs[run].update({"status": "completed", "conclusion": conclusion})
        if artifact is not None:
            self.gh.artifacts[run] = [{"id": 77, "name": "lane-report-%s" % run_id, "expired": False}]
            self.gh.zips[77] = artifact

    def test_a_finished_run_writes_its_report_where_a_local_runner_writes(self):
        run, run_id = self.dispatched()
        artifact = self.seal(run_id, {
            "briefs/opencode-go-2/done/%s.md" % TICKET: BRIEF,
            "logs/opencode-go-2/%s.log" % TICKET: "[t] %s done: https://github.com/owner/app/pull/9\n" % TICKET,
            "logs/opencode-go-2/%s.result" % TICKET: "pr\n",
            "logs/opencode-go-2/%s/opencode/db.sqlite" % TICKET: "transcript",
            "logs/opencode-go-2/other.log": "not this run",
        }, beat={"lane": "opencode-go-2", "at": 1, "available": True, "reason": ""})
        self.finish(run, run_id, artifact)
        self.runner(now=self.now + 600).tend()
        self.assertFalse((self.inbox / (TICKET + ".md")).exists())
        self.assertEqual((self.inbox / "done" / (TICKET + ".md")).read_text(), BRIEF)
        logs = self.home / "logs" / "opencode-go-2"
        self.assertIn("done: https://github.com/owner/app/pull/9", (logs / (TICKET + ".log")).read_text())
        self.assertEqual((logs / (TICKET + ".result")).read_text(), "pr\n")
        self.assertEqual((logs / TICKET / "opencode" / "db.sqlite").read_text(), "transcript")
        self.assertFalse((logs / "other.log").exists())
        beat = self.beat()
        self.assertTrue(beat["available"])
        self.assertEqual(beat["at"], int(self.now + 600))
        self.assertFalse((self.home / "state" / "usage" / "opencode-go-2.alive").exists())

    def test_a_held_runner_parks_the_brief_and_holds_the_slot(self):
        run, run_id = self.dispatched()
        artifact = self.seal(run_id, {}, beat={"lane": "opencode-go-2", "at": 1, "available": False,
                                               "reason": "egress not narrowed: probe accepts connections"})
        self.finish(run, run_id, artifact, "failure")
        self.runner(now=self.now + 600).tend()
        self.assertTrue((self.inbox / "failed" / (TICKET + ".md")).exists())
        self.assertIn("failed: [transient] runner: egress not narrowed", self.failure_line())
        beat = self.beat()
        self.assertFalse(beat["available"])
        self.assertEqual(beat["retry_at"], int(self.now + 600 + runners.HOLD_SECONDS))
        self.assertIn("egress not narrowed", beat["reason"])
        # A new brief waits while the slot is held, then goes.
        self.drop_brief(name="owner.app-8")
        self.runner(now=self.now + 700).tend()
        self.assertEqual(len(self.gh.posts("/dispatches")), 1)
        self.runner(now=self.now + 601 + runners.HOLD_SECONDS).tend()
        self.assertEqual(len(self.gh.posts("/dispatches")), 2)
        self.assertTrue(self.beat()["available"])

    def test_a_run_that_ended_with_no_report_parks_the_brief(self):
        run, run_id = self.dispatched()
        self.finish(run, run_id, None, "cancelled")
        self.runner(now=self.now + 600).tend()
        self.assertTrue((self.inbox / "failed" / (TICKET + ".md")).exists())
        self.assertIn("failed: [transient] the runner run ended cancelled with no report", self.failure_line())

    def test_a_report_naming_another_file_is_refused_whole(self):
        run, run_id = self.dispatched()
        artifact = self.seal(run_id, {"briefs/opencode-go-2/done/%s.md" % TICKET: BRIEF})
        # Swap in a tampered report: the same seal over a tar with a stray path.
        files = {"briefs/opencode-go-2/done/%s.md" % TICKET: b"x", "state/station/reported.json": b"{}"}
        with mock.patch.object(runners.Runners, "open_report", return_value=files):
            self.finish(run, run_id, artifact)
            self.runner(now=self.now + 600).tend()
        self.assertFalse((self.home / "state" / "station" / "reported.json").exists())
        self.assertTrue((self.inbox / "failed" / (TICKET + ".md")).exists())
        self.assertIn("could not read the runner's report: the report names a file outside this run",
                      self.failure_line())

    def test_a_run_no_runner_takes_is_cancelled_and_parked(self):
        run, _ = self.dispatched()
        self.runner(now=self.now + 60).tend()
        self.assertEqual(self.gh.posts("/cancel"), [])
        self.runner(now=self.now + runners.QUEUE_MAX + 1).tend()
        self.assertEqual(len(self.gh.posts("/cancel")), 1)
        self.assertTrue((self.inbox / "failed" / (TICKET + ".md")).exists())
        self.assertIn("failed: [transient] no cluster runner took the run", self.failure_line())
        self.assertFalse(self.beat()["available"])

    def test_a_dispatch_with_no_run_id_is_parked_not_searched_for(self):
        self.drop_brief()
        real = self.gh.rest

        def no_id(method, path, body=None, etag=None, ok=()):
            resp = real(method, path, body, etag, ok)
            return Response(204, None) if path.endswith("/dispatches") else resp
        self.gh.rest = no_id
        self.runner().tend()
        self.assertTrue((self.inbox / "failed" / (TICKET + ".md")).exists())
        self.assertIn("failed: [transient] GitHub returned no run id", self.failure_line())
        self.runner(now=self.now + 60).tend()
        self.assertEqual(len(self.gh.posts("/dispatches")), 1)
        self.assertFalse([c for c in self.gh.calls if c[0] == "GET"])

    def test_a_run_from_elsewhere_is_not_believed(self):
        for field, value in (("head_branch", "feature"), ("path", ".github/workflows/other.yml"),
                             ("event", "pull_request"), ("triggering_actor", {"login": "someone"})):
            self.setUp()
            run, run_id = self.dispatched()
            artifact = self.seal(run_id, {"briefs/opencode-go-2/done/%s.md" % TICKET: BRIEF})
            self.finish(run, run_id, artifact)
            self.gh.runs[run][field] = value
            self.runner(now=self.now + 600).tend()
            self.assertTrue((self.inbox / "failed" / (TICKET + ".md")).exists(), field)
            self.assertIn("is not this station's", self.failure_line())
            self.assertFalse([c for c in self.gh.calls if c[1].endswith("/artifacts")], field)
            self.tearDown()
        self.setUp()

    def test_a_report_sealed_without_the_runs_mac_key_is_refused(self):
        # Anyone can encrypt to the station's public key; only the job holds
        # the run's MAC key.
        run, run_id = self.dispatched()
        artifact = self.seal(run_id, {"briefs/opencode-go-2/done/%s.md" % TICKET: BRIEF},
                             mac_key="f" * 64)
        self.finish(run, run_id, artifact)
        self.runner(now=self.now + 600).tend()
        self.assertFalse((self.inbox / "done" / (TICKET + ".md")).exists())
        self.assertIn("the report's MAC does not match", self.failure_line())

    def test_a_malformed_mac_is_refused_without_an_exception(self):
        run, run_id = self.dispatched()
        artifact = self.seal(run_id, {"briefs/opencode-go-2/done/%s.md" % TICKET: BRIEF})
        r = self.runner(now=self.now + 600)
        for mac, why in (("\u00e9".encode("utf-8") * 32, "is not 64 hex digits"),
                         (b"ab" * 31, "is not 64 hex digits"),
                         (b"AB" * 32, "is not 64 hex digits")):
            buf = io.BytesIO()
            with zipfile.ZipFile(io.BytesIO(artifact)) as src, zipfile.ZipFile(buf, "w") as dst:
                for name in ("key.enc", "report.enc"):
                    dst.writestr(name, src.read(name))
                dst.writestr("report.mac", mac)
            with self.assertRaises(runners.ReportError) as caught:
                r.open_report(buf.getvalue(), run_id, self.mac_key)
            self.assertIn(why, str(caught.exception))

    def test_anything_a_malformed_report_raises_parks_the_brief(self):
        run, run_id = self.dispatched()
        self.finish(run, run_id, b"not a zip")
        with mock.patch.object(runners.Runners, "_open_report", side_effect=TypeError("odd")):
            self.runner(now=self.now + 600).tend()
        self.assertIn("could not read the runner's report: the report could not be opened (TypeError: odd)",
                      self.failure_line())

    def test_a_report_that_unpacks_past_the_bound_is_refused(self):
        run, run_id = self.dispatched()
        artifact = self.seal(run_id, {"briefs/opencode-go-2/done/%s.md" % TICKET: BRIEF,
                                      "logs/opencode-go-2/%s.log" % TICKET: "x" * 5000})
        self.finish(run, run_id, artifact)
        with mock.patch.object(runners, "UNPACKED_MAX", 4000):
            self.runner(now=self.now + 600).tend()
        self.assertFalse((self.home / "logs" / "opencode-go-2" / (TICKET + ".log")).read_text().startswith("x"))
        self.assertIn("the report unpacks past 4000 bytes", self.failure_line())

    def test_an_oversized_zip_member_is_refused_before_it_is_read(self):
        run, run_id = self.dispatched()
        artifact = self.seal(run_id, {"logs/opencode-go-2/%s.log" % TICKET: os.urandom(4000).hex()})
        self.finish(run, run_id, artifact)
        with mock.patch.object(runners, "ARTIFACT_MAX", 1000):
            self.runner(now=self.now + 600).tend()
        # The declared size refuses it, before a byte of it is read.
        self.assertIn("report.enc is larger than 1000 bytes", self.failure_line())


class StationHookTest(unittest.TestCase):
    def test_no_runner_slot_means_no_request(self):
        st = station.Station.__new__(station.Station)
        st.gh = FakeGitHub()
        with mock.patch.dict(os.environ, {"LOOM_RUNNER_SLOTS": ""}):
            st.tend_runners()
        self.assertEqual(st.gh.calls, [])


if __name__ == "__main__":
    unittest.main()
