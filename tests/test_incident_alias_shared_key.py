"""A private repo's incident alias, derived from a shared key, is the same on
every host, so any host names the incident the same way and can close it."""
from __future__ import annotations

import re
import subprocess
import tempfile
import unittest
from pathlib import Path

RUNNER = Path(__file__).resolve().parent.parent / "lanes" / "run_lane.sh"


def functions(*names: str) -> str:
    text = RUNNER.read_text()
    return "".join(re.search(r"^%s\(\) \{.*?^\}\n" % n, text, re.S | re.M).group(0)
                   for n in names)


class IncidentAliasSharedKey(unittest.TestCase):
    def subject(self, host_dir: Path, ssm_dir: Path, repo="example/repo", *,
                trace=False, shared_only=False) -> subprocess.CompletedProcess:
        script = ('set -o pipefail\n. "$1"\nshift\n' + functions("incident_subject")
                  + 'repo_is_public() { false; }\n')
        if trace:
            script += 'python3() { [ -z "${KEY+x}" ] || return 90; command python3 "$@"; }\n'
        script += 'incident_subject "$@"\n'
        env = {"PATH": "/usr/bin:/bin", "REPO": repo, "INCIDENT_DIR": str(host_dir),
               "LOOM_SSM_DIR": str(ssm_dir), "NET_TIMEOUT": "5"}
        args = ["bash", "-xc" if trace else "-c", script, "bash",
                str(RUNNER.with_name("agent_sandbox.sh"))]
        if shared_only:
            args.append("shared-only")
        return subprocess.run(args, env=env, capture_output=True, text=True)

    def write_key(self, ssm: Path, value: str):
        param = ssm / "loom" / "incident-alias-key"
        param.parent.mkdir(parents=True, exist_ok=True)
        param.write_text(value)

    def test_two_hosts_with_one_key_name_the_same_repo_identically(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            self.write_key(tmp / "ssm", "shared-test-key")
            a = self.subject(tmp / "host-a", tmp / "ssm")
            b = self.subject(tmp / "host-b", tmp / "ssm")
            self.assertEqual(a.returncode, 0, a.stderr)
            self.assertEqual(a.stdout, b.stdout)
            self.assertRegex(a.stdout, r"^private repo [0-9a-f]{8}$")

    def test_another_repo_gets_another_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            self.write_key(tmp / "ssm", "shared-test-key")
            a = self.subject(tmp / "h", tmp / "ssm", "example/one")
            b = self.subject(tmp / "h", tmp / "ssm", "example/two")
            self.assertNotEqual(a.stdout, b.stdout)

    def test_signing_keeps_the_key_out_of_the_process_environment_and_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            self.write_key(tmp / "ssm", "shared-test-key\n\n")
            result = self.subject(tmp / "host", tmp / "ssm", trace=True)
            self.assertNotIn("shared-test-key", result.stderr)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "private repo 9111c25b")

    def test_with_no_key_each_host_keeps_its_own_random_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            a = self.subject(tmp / "host-a", tmp / "none")
            b = self.subject(tmp / "host-b", tmp / "none")
            self.assertRegex(a.stdout, r"^private repo [0-9a-f]{8}$")
            self.assertNotEqual(a.stdout, b.stdout)

    def test_cross_host_lookup_requires_a_shared_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            host = tmp / "host"
            result = self.subject(host, tmp / "ssm", shared_only=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")
            self.assertFalse(host.exists())


if __name__ == "__main__":
    unittest.main()
