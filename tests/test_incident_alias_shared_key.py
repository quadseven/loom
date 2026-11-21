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
    def subject(self, host_dir: Path, ssm_dir: Path, repo="example/repo") -> subprocess.CompletedProcess:
        script = (functions("incident_alias_key", "incident_subject")
                  + 'repo_is_public() { false; }\nincident_subject\n')
        env = {"PATH": "/usr/bin:/bin", "REPO": repo, "INCIDENT_DIR": str(host_dir),
               "LOOM_SSM_DIR": str(ssm_dir), "NET_TIMEOUT": "5"}
        return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)

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

    def test_with_no_key_each_host_keeps_its_own_random_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            a = self.subject(tmp / "host-a", tmp / "none")
            b = self.subject(tmp / "host-b", tmp / "none")
            self.assertRegex(a.stdout, r"^private repo [0-9a-f]{8}$")
            self.assertNotEqual(a.stdout, b.stdout)


if __name__ == "__main__":
    unittest.main()
