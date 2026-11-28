"""bus/remote_checks.py and .github/workflows/remote-check.yml."""
from __future__ import annotations

import io
import json
import re
import tempfile
import threading
import time
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from bus import remote_checks
from bus.github import Response

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "remote-check.yml"
SHA = "a" * 40


class Request(unittest.TestCase):
    def test_a_good_request_is_kept_and_a_bad_one_refused(self):
        good = {"repo": "example/repo", "sha": SHA, "interpreter": "python3",
                "submodules": "no", "setup": "", "check": "python3 -m unittest"}
        self.assertEqual(remote_checks.check_request(good)["repo"], "example/repo")
        for bad in ({**good, "repo": "example/repo; rm -rf /"}, {**good, "sha": "main"},
                    {**good, "check": ""}, {**good, "check": "echo \x1b[0m"},
                    {**good, "submodules": "maybe"}, {**good, "interpreter": "a b"}):
            with self.assertRaises(remote_checks.RemoteCheckError):
                remote_checks.check_request(bad)

    def test_a_result_keeps_only_an_exit_code_and_plain_test_names(self):
        raw = json.dumps({"exit": 1, "failing": ["tests.a.B.test_c", "has space", "x" * 300,
                                                 "tests.a.B.test_c", 5]}).encode()
        self.assertEqual(remote_checks.parse_result(raw),
                         {"exit": 1, "failing": ["tests.a.B.test_c"]})
        for bad in (b"[]", b'{"exit": "0"}', b'{"exit": true}', b'{"exit": 256}', b"nope"):
            with self.assertRaises(remote_checks.RemoteCheckError):
                remote_checks.parse_result(bad)

    def test_an_artifact_without_the_result_file_is_refused(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("other.txt", "x")
        with self.assertRaises(remote_checks.RemoteCheckError):
            remote_checks.open_artifact(buf.getvalue())


class Dispatch(unittest.TestCase):
    def test_a_non_public_repo_is_refused_without_a_dispatch(self):
        calls = []

        class GH:
            def rest(self, *args, **kw):
                calls.append(args)
                return Response(200, {})

        with tempfile.TemporaryDirectory() as home:
            rid = remote_checks.submit(home, "example/repo", SHA, "", "no", "", "true")
            remote_checks.RemoteChecks(GH(), home, "example/loom", lambda r: False, lambda m: None,
                                       actor="loom[bot]").tend()
            self.assertEqual(calls, [])
            self.assertEqual(remote_checks.read_result(home, rid)["state"], "error")


class Ticker(unittest.TestCase):
    def test_a_request_filed_with_no_pass_running_is_dispatched_and_written_back_on_ticks(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("result.json", json.dumps({"exit": 0, "failing": []}))
        state = {"done": False}
        run = {"head_branch": "main", "path": ".github/workflows/remote-check.yml@refs/heads/main",
               "event": "workflow_dispatch", "triggering_actor": {"login": "loom[bot]"}}

        class GH:
            def rest(self, method, path, body=None, ok=None):
                if method == "POST":
                    return Response(200, {"workflow_run_id": 7})
                if path.endswith("/runs/7"):
                    return Response(200, dict(run, status="completed" if state["done"] else "queued",
                                              conclusion="success"))
                if path.endswith("/artifacts"):
                    return Response(200, {"artifacts": [{"id": 9, "name": "remote-check-result-" + rid}]})
                return Response(302, {}, headers={"location": "https://x/zip"})

        with tempfile.TemporaryDirectory() as home:
            rid = remote_checks.submit(home, "example/repo", SHA, "", "no", "", "true")
            make = lambda: remote_checks.RemoteChecks(
                GH(), home, "example/loom", lambda r: True, lambda m: None,
                actor="loom[bot]", fetch=lambda url: buf.getvalue())
            ticker = remote_checks.Ticker(make, lambda m: None)
            ticker.tick()                      # no pass step runs: only the ticker
            self.assertIsNone(remote_checks.read_result(home, rid))
            state["done"] = True
            ticker.tick()
            self.assertEqual(remote_checks.read_result(home, rid)["state"], "done")

    def test_a_tending_that_finds_another_running_does_nothing(self):
        calls = []

        class GH:
            def rest(self, *a, **kw):
                calls.append(a)
                return Response(200, {})

        with tempfile.TemporaryDirectory() as home:
            remote_checks.submit(home, "example/repo", SHA, "", "no", "", "true")
            import fcntl
            with open(Path(home) / "state" / "remote-checks" / ".tend.lock", "a+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                remote_checks.RemoteChecks(GH(), home, "example/loom", lambda r: True,
                                           lambda m: None, actor="loom[bot]").tend()
            self.assertEqual(calls, [])


class TickerLifecycle(unittest.TestCase):
    def test_the_loop_ticks_until_stopped_and_then_ends(self):
        ticks = []

        class Fake:
            def tend(self):
                ticks.append(1)

        ticker = remote_checks.Ticker(Fake, lambda m: None, interval=0.01)
        ticker.start()
        deadline = time.monotonic() + 5
        while not ticks and time.monotonic() < deadline:
            time.sleep(0.01)
        ticker.stop()
        ticker.join(timeout=5)
        self.assertTrue(ticks)
        self.assertFalse(ticker.is_alive())

    def test_a_rate_limited_tick_says_so_and_does_not_raise(self):
        said = []

        class Limited:
            def tend(self):
                raise remote_checks.RateLimited("limited", 429, "", 0)

        remote_checks.Ticker(Limited, said.append).tick()
        self.assertEqual(len(said), 1)
        self.assertIn("rate limit", said[0])

    def test_a_station_without_a_real_client_starts_no_ticker(self):
        from bus import station
        with tempfile.TemporaryDirectory() as home:
            Path(home, "state", "remote-checks", "req").mkdir(parents=True)
            fake = types.SimpleNamespace(gh=object(), s=types.SimpleNamespace(home=home, self_repo="o/r"))
            station.Station.start_remote_check_ticker(fake)
            self.assertFalse(hasattr(fake, "_remote_ticker"))
            bare = types.SimpleNamespace(s=types.SimpleNamespace(home=home, self_repo="o/r"))
            station.Station.start_remote_check_ticker(bare)
            self.assertFalse(hasattr(bare, "_remote_ticker"))


class StationPass(unittest.TestCase):
    def test_a_check_filed_during_a_claim_step_reaches_the_lane_before_the_step_ends(self):
        from bus import github, station

        dispatched = threading.Event()
        completed = threading.Event()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("result.json", json.dumps({"exit": 0, "failing": []}))
        artifact = buf.getvalue()

        class GitHub:
            def __init__(self, auth=None):
                self.request_id = None

            def app_login(self):
                return "loom[bot]"

            def rest(self, method, path, body=None, ok=None):
                if method == "POST" and path.endswith("/workflows/remote-check.yml/dispatches"):
                    self.request_id = body["inputs"]["run_id"]
                    dispatched.set()
                    return Response(200, {"workflow_run_id": 7})
                if path.endswith("/runs/7"):
                    return Response(200, {
                        "head_branch": "main", "path": ".github/workflows/remote-check.yml",
                        "event": "workflow_dispatch", "triggering_actor": {"login": "loom[bot]"},
                        "status": "completed" if completed.is_set() else "queued",
                        "conclusion": "success",
                    })
                if path.endswith("/artifacts"):
                    name = "remote-check-result-" + self.request_id
                    return Response(200, {"artifacts": [{"id": 9, "name": name}]})
                if path.endswith("/artifacts/9/zip"):
                    return Response(302, {}, headers={"location": "https://example.com/result"})
                if path == "/repos/example/repo":
                    return Response(200, {"private": False})
                raise AssertionError((method, path))

        class EndClaimStep(Exception):
            pass

        ticker_type = getattr(remote_checks, "Ticker", None)
        tickers = []

        def fast_ticker(make, say):
            # Let the real thread schedule its ticks without a minute-long wait.
            ticker = ticker_type(make, say, interval=0.01)
            tickers.append(ticker)
            return ticker

        with tempfile.TemporaryDirectory() as home:
            Path(home, "state", "remote-checks", "req").mkdir(parents=True)
            st = object.__new__(station.Station)
            st.s = station.Settings(home=home, repos=[], authors=[], owners=[], self_repo="example/loom")
            st.gh = GitHub()
            st.capture_pod_logs = st.sample_operator_budget = lambda: None
            st._budget_exhausted = lambda: False
            st.check_repo_access = lambda: True
            st.ask_operator_login = st._publish_lanes_when_old = st.sync_labels = lambda: None
            st._end_pass_locally = lambda: None

            def claim_step():
                rid = remote_checks.submit(home, "example/repo", SHA, "", "no", "", "true")
                self.assertTrue(dispatched.wait(5), "the request waited for the claim step to finish")
                completed.set()
                deadline = time.monotonic() + 5
                result = remote_checks.read_result(home, rid)
                while result is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                    result = remote_checks.read_result(home, rid)
                self.assertEqual(result, {"state": "done", "exit": 0, "failing": [], "sha": SHA},
                                 "the result waited for the claim step to finish")
                raise EndClaimStep

            st.finish_claims = claim_step
            with (mock.patch.object(station, "GitHub", GitHub),
                  mock.patch.object(github, "auth_from_env"),
                  mock.patch.object(github.urllib.request, "urlopen", side_effect=lambda *a, **kw: io.BytesIO(artifact)),
                  mock.patch.object(remote_checks, "Ticker", fast_ticker, create=True)):
                try:
                    with self.assertRaises(EndClaimStep):
                        st.run_pass()
                finally:
                    for ticker in tickers:
                        ticker.stop()
                        ticker.join(timeout=5)


class Workflow(unittest.TestCase):
    text = WORKFLOW.read_text()

    def test_it_runs_on_a_hosted_runner_on_main_only_with_no_secrets(self):
        self.assertIn("runs-on: ubuntu-latest", self.text)
        self.assertIn("github.ref == 'refs/heads/main'", self.text)
        self.assertIn("workflow_dispatch:", self.text)
        self.assertNotIn("secrets.", self.text)
        self.assertIn("persist-credentials: false", self.text)
        self.assertRegex(self.text, r"(?m)^permissions:\n  contents: read\n")

    def test_no_input_is_interpolated_into_a_script(self):
        for block in re.findall(r"run: \|?\n?((?:\s{10}.*\n)+)", self.text):
            self.assertNotIn("${{", block)

    def test_uv_is_installed_before_the_check_as_on_the_lane_image(self):
        # A hosted runner has no uv; contracts that call uvx failed at once without it.
        uv = self.text.index("astral-sh/setup-uv@")
        self.assertLess(uv, self.text.index("- name: Setup and check"))
        self.assertRegex(self.text, r"astral-sh/setup-uv@[0-9a-f]{40} ")

    def test_it_takes_the_inputs_the_station_sends(self):
        for name in ("repo", "sha", "run_id", "interpreter", "submodules", "setup", "check"):
            self.assertRegex(self.text, r"(?m)^      %s:$" % name)


if __name__ == "__main__":
    unittest.main()
