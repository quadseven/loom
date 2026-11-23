"""bus/remote_checks.py and .github/workflows/remote-check.yml."""
from __future__ import annotations

import io
import json
import re
import tempfile
import unittest
import zipfile
from pathlib import Path

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
    def test_a_request_filed_mid_pass_is_dispatched_and_written_back_on_ticks(self):
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
