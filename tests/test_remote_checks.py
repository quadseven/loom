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

    def test_it_takes_the_inputs_the_station_sends(self):
        for name in ("repo", "sha", "run_id", "interpreter", "submodules", "setup", "check"):
            self.assertRegex(self.text, r"(?m)^      %s:$" % name)


if __name__ == "__main__":
    unittest.main()
