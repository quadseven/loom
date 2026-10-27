"""lanes/k8s/lane_job.py, the steps of one lane run on a cluster runner
(.github/workflows/lane.yml, quadseven/loom#1824): inputs read from the event
file, settings checked, SSM values copied into files under HOME without
printing them, egress proved before any key, run_lane.sh run once with no
Actions token or AWS session, and the report sealed. A copy of the script
beside a fake run_lane.sh records the run."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bus import runners  # noqa: E402

BRIEF = "Repo: owner/app\nBranch: loom/owner.app-7\n\nDo the thing.\n"
PUB = "-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----\n"
RUN_ID = "0123456789abcdef0123456789abcdef"
MAC_KEY = "ab" * 32

FAKE_RUNNER = """#!/usr/bin/env bash
env | sort > "$LOOM_HOME/../runner.env"
echo "runner output that must stay out of the job log"
slot="$1"; brief="$(ls "$LOOM_HOME/briefs/$slot"/*.md)"; t="$(basename "$brief" .md)"
mkdir -p "$LOOM_HOME/briefs/$slot/done" "$LOOM_HOME/logs/$slot" "$LOOM_HOME/state/usage"
mv "$brief" "$LOOM_HOME/briefs/$slot/done/"
echo "[t] $t done" > "$LOOM_HOME/logs/$slot/$t.log"
echo '{"lane": "'"$slot"'", "at": 1, "available": true, "reason": ""}' > "$LOOM_HOME/state/usage/$slot.json"
"""

SIGNED = ("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20261025/us-east-2/ssm/aws4_request, "
          "SignedHeaders=content-type;host;x-amz-date;x-amz-security-token;x-amz-target, "
          "Signature=da23e471ad758ba3397eaaef0812d8d3010bb6f33663eb5e9e329e7a758f04f1")



class LaneJobTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "loom"
        (self.root / "lanes" / "k8s").mkdir(parents=True)
        shutil.copy(ROOT / "lanes" / "k8s" / "lane_job.py", self.root / "lanes" / "k8s")
        shutil.copy(ROOT / "lanes" / "agent_sandbox.sh", self.root / "lanes")
        shutil.copytree(ROOT / "bus", self.root / "bus",
                        ignore=shutil.ignore_patterns("__pycache__"))
        runner = self.root / "lanes" / "run_lane.sh"
        runner.write_text(FAKE_RUNNER)
        runner.chmod(0o755)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.job = self.tmp / "home" / "loom-job"
        self.event = self.tmp / "event.json"
        self.inputs(settings="LOOM_OPENCODE_GO_KEY_SSM=/x/oc\nLOOM_GITHUB_APP_KEY_SSM=/x/app\n"
                             "LOOM_REPO_OWNERS=owner\nLOOM_AWS_REGION=us-test-1\n")

    def tearDown(self):
        self._tmp.cleanup()

    def inputs(self, **over):
        inputs = {"slot": "opencode-go-2", "ticket": "owner.app-7", "brief": BRIEF,
                  "run_id": RUN_ID, "settings": "", "report_key": PUB, "report_mac_key": MAC_KEY}
        inputs.update(over)
        self.event.write_text(json.dumps({"inputs": inputs}))

    def step(self, name, **env):
        full = {"PATH": "%s:%s" % (self.bin, os.environ["PATH"]), "HOME": str(self.tmp / "home"),
                "LOOM_JOB_DIR": str(self.job), "LOOM_JOB_EVENT": str(self.event),
                "GITHUB_ACTIONS": "true", **env}
        return subprocess.run([sys.executable, "-B", str(self.root / "lanes" / "k8s" / "lane_job.py"), name],
                              env=full, capture_output=True, text=True, timeout=60)

    def test_prepare_reads_the_event_and_writes_the_checked_settings(self):
        result = self.step("prepare")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.job / "brief.md").read_text(), BRIEF)
        self.assertEqual(json.loads((self.job / "inputs.json").read_text()),
                         {"slot": "opencode-go-2", "ticket": "owner.app-7", "run_id": RUN_ID})
        self.assertIn("LOOM_OPENCODE_GO_KEY_SSM=/x/oc\n", (self.job / "lane.env").read_text())
        self.assertEqual(stat.S_IMODE((self.job / "lane.env").stat().st_mode), 0o600)
        self.assertNotIn("Do the thing", result.stdout + result.stderr)
        # The run's MAC key is masked in the log before anything else.
        self.assertIn("::add-mask::%s" % MAC_KEY, result.stdout)
        self.assertEqual((self.job / "report-mac-key").read_text(), MAC_KEY)

    def test_prepare_refuses_a_setting_a_slot_may_not_take(self):
        # LOOM_<SLOT>_OPENCODE names the binary a slot runs: never from inputs.
        self.inputs(settings="LOOM_OPENCODE_GO_2_OPENCODE=/tmp/evil\n")
        result = self.step("prepare")
        self.assertEqual(result.returncode, 1)
        self.assertIn("::error::", result.stderr)
        self.assertFalse((self.job / "lane.env").exists())

    def test_prepare_refuses_bad_names(self):
        for over in ({"slot": "codex"}, {"ticket": "../x"}, {"run_id": "1"}, {"report_key": "x"},
                     {"report_mac_key": "short"}):
            self.inputs(**over)
            self.assertEqual(self.step("prepare").returncode, 1, over)

    def test_a_gateway_lane_is_moved_onto_the_cluster_gateway(self):
        self.inputs(slot="spark-2", settings="LOOM_SPARK_2_URL=http://spark.example.internal:8000/v1\n"
                                             "LOOM_SPARK_MODEL=spark/coder\n")
        result = self.step("prepare", LOOM_RUNNER_GATEWAY_URL="http://gateway.example:8080")
        self.assertEqual(result.returncode, 0, result.stderr)
        env = (self.job / "lane.env").read_text()
        self.assertIn("LOOM_SPARK_2_URL=http://gateway.example:8080/v1\n", env)
        self.inputs(slot="free-kilo", settings="LOOM_FREE_KILO_MODEL=free/kilo\n")
        self.assertEqual(self.step("prepare", LOOM_RUNNER_GATEWAY_URL="http://gateway.example:8080").returncode, 0)
        self.assertIn("LOOM_FREE_GATEWAY_URL=http://gateway.example:8080\n", (self.job / "lane.env").read_text())

    def load(self):
        spec = importlib.util.spec_from_file_location("lane_job", self.root / "lanes" / "k8s" / "lane_job.py")
        job = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(job)
        return job

    def ssm_step(self, missing=()):
        """The ssm step in process, with SSM answered by a fake endpoint.
        Returns (exit code, printed text, requests)."""
        job = self.load()
        asked = []

        class Answer(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def urlopen(req, timeout=None):
            name = json.loads(req.data)["Name"]
            asked.append((req.full_url, dict(req.header_items()), name))
            if name in missing:
                raise urllib.error.HTTPError(req.full_url, 400, "ParameterNotFound", {}, None)
            return Answer(json.dumps({"Parameter": {"Value": "value-of" + name}}).encode())

        out, err = io.StringIO(), io.StringIO()
        env = {"LOOM_JOB_DIR": str(self.job), "AWS_ACCESS_KEY_ID": "AKIDEXAMPLE",
               "AWS_SECRET_ACCESS_KEY": "secret", "AWS_SESSION_TOKEN": "tok",
               "AWS_REGION": "us-test-1", "GITHUB_ACTIONS": "true"}
        with mock.patch.dict(os.environ, env), mock.patch.object(job.urllib.request, "urlopen", urlopen), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = job.main(["lane_job.py", "ssm"])
        return code, out.getvalue() + err.getvalue(), asked

    def test_ssm_copies_each_value_into_files_and_prints_no_name_or_value(self):
        self.assertEqual(self.step("prepare").returncode, 0)
        code, printed, asked = self.ssm_step()
        self.assertEqual(code, 0, printed)
        ssm = self.job / "ssm"
        self.assertEqual((ssm / "x" / "oc").read_text(), "value-of/x/oc\n")
        deny = ssm / "infra" / "leak-scan" / "deny-list"
        self.assertEqual(deny.read_text(), "value-of/infra/leak-scan/deny-list\n")
        self.assertEqual((self.job / "app-key.pem").read_text(), "value-of/x/app\n")
        for path in (ssm / "x" / "oc", deny, self.job / "app-key.pem"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        masked = "".join(l for l in printed.splitlines(True) if not l.startswith("::add-mask::"))
        self.assertNotIn("/x/", masked)
        self.assertNotIn("value-of", masked)
        self.assertIn("::add-mask::value-of/x/oc", printed)
        url, headers, _ = asked[0]
        self.assertEqual(url, "https://ssm.us-test-1.amazonaws.com/")
        self.assertEqual(headers["X-amz-target"], "AmazonSSM.GetParameter")
        self.assertEqual(headers["X-amz-security-token"], "tok")
        self.assertTrue(headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/"))

    def test_the_request_is_signed_as_aws_signs_it(self):
        # Pinned from botocore's SigV4Auth for the same request (checked when
        # this was written): the image has no AWS CLI, so the job signs.
        job = self.load()
        body = json.dumps({"Name": "/a/b", "WithDecryption": True}).encode()
        headers = job.sigv4_headers(body, "us-east-2", "AKIDEXAMPLE",
                                    "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY", "tok", "20261025T120000Z")
        self.assertEqual(headers["authorization"], SIGNED)

    def test_an_unreadable_parameter_fails_naming_only_its_setting(self):
        self.inputs(settings="LOOM_OPENCODE_GO_KEY_SSM=/missing/oc\n")
        self.assertEqual(self.step("prepare").returncode, 0)
        code, printed, _ = self.ssm_step(missing=("/missing/oc",))
        self.assertEqual(code, 1)
        self.assertIn("LOOM_OPENCODE_GO_KEY_SSM", printed)
        self.assertNotIn("/missing/", printed)

    def test_failed_egress_runs_nothing_and_says_why_only_in_the_report(self):
        self.assertEqual(self.step("prepare").returncode, 0)
        bwrap = self.bin / "bwrap"
        bwrap.write_text("#!/bin/sh\nexit 0\n")
        bwrap.chmod(0o755)
        result = self.step("egress", LOOM_EGRESS_DENY_PROBES="", LOOM_EGRESS_ALLOW_PROBE="")
        self.assertEqual(result.returncode, 1)
        self.assertIn("egress proof failed", result.stderr)
        beat = json.loads((self.job / "home" / "state" / "usage" / "opencode-go-2.json").read_text())
        self.assertFalse(beat["available"])
        self.assertIn("bwrap narrows no network", beat["reason"])

    def test_run_gives_run_lane_no_actions_token_or_aws_session(self):
        self.assertEqual(self.step("prepare").returncode, 0)
        (self.job / "app-key.pem").write_text("pem")
        result = self.step("run", ACTIONS_ID_TOKEN_REQUEST_TOKEN="idtok", GITHUB_TOKEN="ghtok",
                           AWS_ACCESS_KEY_ID="AKIAFAKE", AWS_SESSION_TOKEN="sess",
                           LOOM_EGRESS_DENY_PROBES="192.0.2.1:443", LOOM_EGRESS_ALLOW_PROBE="example.com:443")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("runner output", result.stdout + result.stderr)
        env = dict(line.split("=", 1) for line in (self.job / "runner.env").read_text().splitlines() if "=" in line)
        for name in ("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "GITHUB_TOKEN", "AWS_ACCESS_KEY_ID",
                     "AWS_SESSION_TOKEN", "LOOM_GITHUB_APP_KEY_SSM"):
            self.assertNotIn(name, env)
        self.assertEqual(env["LOOM_SANDBOX"], "bwrap")
        self.assertEqual(env["LOOM_DROP_TREES"], "1")
        self.assertEqual(env["LOOM_CLUSTER_RUNNER"], "1")
        self.assertEqual(env["TMPDIR"], str(self.job / "tmp"))
        self.assertEqual(env["LOOM_SSM_DIR"], str(self.job / "ssm"))
        self.assertEqual(env["LOOM_GITHUB_APP_KEY_FILE"], str(self.job / "app-key.pem"))
        self.assertEqual(env["LOOM_EGRESS_DENY_PROBES"], "192.0.2.1:443")
        self.assertEqual(env["LOOM_OPENCODE_GO_KEY_SSM"], "/x/oc")
        self.assertIn("runner output", (self.job / "home" / "logs" / "opencode-go-2" /
                                        "owner.app-7.service.log").read_text())
        self.assertTrue((self.job / "home" / "briefs" / "opencode-go-2" / "done" / "owner.app-7.md").exists())

    def test_the_report_holds_exactly_this_runs_files(self):
        self.assertEqual(self.step("prepare").returncode, 0)
        self.assertEqual(self.step("run").returncode, 0)
        home = self.job / "home"
        (home / "logs" / "opencode-go-2" / "owner.app-70.log").write_text("another ticket")
        (home / "logs" / "opencode-go-2" / "owner.app-7.answered").write_text("answer")
        (home / "state" / "usage" / "opencode-go-2.alive").write_text("1")
        load = __import__("importlib.util").util
        spec = load.spec_from_file_location("lane_job", self.root / "lanes" / "k8s" / "lane_job.py")
        job = load.module_from_spec(spec)
        spec.loader.exec_module(job)
        self.assertEqual(job.report_files(str(home), "opencode-go-2", "owner.app-7"), [
            "briefs/opencode-go-2/done/owner.app-7.md",
            "logs/opencode-go-2/owner.app-7.answered", "logs/opencode-go-2/owner.app-7.log",
            "logs/opencode-go-2/owner.app-7.service.log", "state/usage/opencode-go-2.json"])
        r = runners.Runners(None, str(self.tmp / "station"), "o/r", [], {}, lambda r: True, print)
        for rel in job.report_files(str(home), "opencode-go-2", "owner.app-7"):
            self.assertTrue(r.allowed_path("opencode-go-2", "owner.app-7", rel), rel)


    def real_key(self):
        """A real report public key in the job dir (openssl, as the station makes it)."""
        priv = self.tmp / "station.pem"
        subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048",
                        "-out", str(priv)], check=True, capture_output=True)
        pub = subprocess.run(["openssl", "pkey", "-in", str(priv), "-pubout"], check=True,
                             capture_output=True, text=True).stdout
        (self.job / "report-key.pem").write_text(pub)

    def test_sealing_drops_the_runs_mac_key(self):
        self.assertEqual(self.step("prepare").returncode, 0)
        self.assertEqual(self.step("run").returncode, 0)
        self.real_key()
        result = self.step("report")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.job / "report" / "report.mac").exists())
        self.assertFalse((self.job / "report-mac-key").exists())

    def test_a_seal_that_fails_names_its_step(self):
        self.assertEqual(self.step("prepare").returncode, 0)
        self.assertEqual(self.step("run").returncode, 0)
        result = self.step("report")   # report-key.pem is not a usable key
        self.assertEqual(result.returncode, 1)
        self.assertIn("could not seal the report: seal the key to the station's report key failed "
                      "(openssl exit", result.stderr)
        self.assertFalse((self.job / "report-mac-key").exists())


class SsmDirAwsTest(unittest.TestCase):
    """lanes/k8s/ssm-dir-bin/aws, the AWS CLI stand-in the leak scanner reads
    the deny-list through: a value is read only from inside LOOM_SSM_DIR."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.ssm = self.tmp / "ssm"
        (self.ssm / "infra").mkdir(parents=True)
        (self.ssm / "infra" / "deny").write_text("terms\n")
        (self.tmp / "outside").write_text("not a parameter\n")

    def tearDown(self):
        self._tmp.cleanup()

    def get(self, name):
        return subprocess.run(["bash", str(ROOT / "lanes" / "k8s" / "ssm-dir-bin" / "aws"), "ssm",
                               "get-parameter", "--name", name, "--with-decryption",
                               "--query", "Parameter.Value", "--output", "text"],
                              env={"PATH": os.environ["PATH"], "LOOM_SSM_DIR": str(self.ssm)},
                              capture_output=True, text=True, timeout=30)

    def test_a_copied_value_is_served(self):
        got = self.get("/infra/deny")
        self.assertEqual((got.returncode, got.stdout), (0, "terms\n"), got.stderr)

    def test_a_link_out_of_the_folder_is_refused(self):
        (self.ssm / "infra" / "link").symlink_to(self.tmp / "outside")
        (self.ssm / "away").symlink_to(self.tmp, target_is_directory=True)
        for name in ("/infra/link", "/away/outside", "/../outside", "/infra/../../outside"):
            got = self.get(name)
            self.assertEqual(got.returncode, 2, name)
            self.assertNotIn("not a parameter", got.stdout, name)


if __name__ == "__main__":
    unittest.main()
