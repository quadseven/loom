"""A private repo's incident alias is derived from the repo's GitHub id, so
every host names the incident the same way and any host can close it
(loom#3573: the shared key the alias once came from never existed, so a
cluster lane could not find and close an incident another host filed)."""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from bus import incident_probe

ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "lanes" / "run_lane.sh"
REPO = "example/repo"
FACTORY = "example/factory"
REPO_ID = 4242

# A gh stand-in: the repo read answers the id (or fails), the incident list
# answers $LISTED when the title asked for is $WANT, and every call is logged.
FAKE_GH = """#!/bin/sh
printf 'gh %s\\n' "$*" >> "$CALLS"
case "$1 $2" in
  "api repos/"*) [ -n "$REPO_ID" ] || exit 1; printf '%s\\n' "$REPO_ID" ;;
  "issue list") [ "$TITLE" = "$WANT" ] && printf '%s\\n' "$LISTED" ;;
esac
exit 0
"""


def functions(*names: str) -> str:
    text = RUNNER.read_text()
    return "".join(re.search(r"^%s\(\) \{.*?^\}\n" % n, text, re.S | re.M).group(0)
                   for n in names)


class IncidentAliasDerived(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(0o755)
        self.calls = self.tmp / "calls"
        self.calls.touch()
        self.log = self.tmp / "lane.log"
        self.log.touch()

    def run_shell(self, body: str, host: str = "host", repo_id=REPO_ID, listed="",
                  want="") -> subprocess.CompletedProcess:
        script = (functions("derived_repo_alias", "incident_subject", "shared_incident_number",
                            "resolve_factory_incidents")
                  + 'repo_is_public() { false; }\n'
                  + 'incident_file() { printf "%s/%s-%s" "$INCIDENT_DIR" '
                    '"$(printf "%s" "$REPO" | tr "/" "_")" "$1"; }\n'
                  + 'incident_ticket() { printf "a private repo ticket"; }\n'
                  + 'sign_tail() { :; }\n'
                  # The shadow guard is not under test here; the alias is.
                  + 'python3() { case "$*" in *bus.shadow*) return 0 ;; esac; '
                    'command python3 "$@"; }\n'
                  + body)
        env = {"PATH": "%s:%s" % (self.tmp / "bin", os.environ.get("PATH", "/usr/bin:/bin")),
               "REPO": REPO, "FACTORY_REPO": FACTORY, "LOOM_ROOT": str(ROOT),
               "INCIDENT_DIR": str(self.tmp / host), "NET_TIMEOUT": "5",
               "INCIDENT_MARKER": incident_probe.INCIDENT_MARKER,
               "LOG": str(self.log), "CALLS": str(self.calls),
               "REPO_ID": "" if repo_id is None else str(repo_id),
               "LISTED": listed, "WANT": want, "PWD": str(self.tmp)}
        return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True,
                              cwd=str(self.tmp))

    def test_two_hosts_name_the_repo_identically_and_as_the_station_does(self):
        a = self.run_shell("incident_subject\n", host="host-a")
        b = self.run_shell("incident_subject\n", host="host-b")
        self.assertEqual(a.returncode, 0, a.stderr)
        self.assertEqual(a.stdout, b.stdout)
        self.assertEqual(a.stdout, "private repo %s" % incident_probe.repo_alias(REPO_ID))
        self.assertIn("private repo %s" % incident_probe.repo_alias(REPO_ID),
                      incident_probe.subjects(REPO, REPO_ID))

    def test_another_repo_gets_another_alias(self):
        a = self.run_shell("incident_subject\n", repo_id=1)
        b = self.run_shell("incident_subject\n", repo_id=2)
        self.assertNotEqual(a.stdout, b.stdout)

    def test_a_pass_on_a_host_with_no_state_file_closes_another_hosts_incident(self):
        title = "Factory incident: private repo %s validation base-check" % (
            incident_probe.repo_alias(REPO_ID))
        result = self.run_shell('mkdir -p "$INCIDENT_DIR"\nresolve_factory_incidents base-check\n',
                                listed="77", want=title)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls.read_text()
        self.assertIn("gh issue list --repo %s --state open" % FACTORY, calls)
        self.assertIn("gh issue close --repo %s 77" % FACTORY, calls)

    def test_no_open_incident_is_logged(self):
        result = self.run_shell('mkdir -p "$INCIDENT_DIR"\nresolve_factory_incidents base-check\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("issue close", self.calls.read_text())
        self.assertIn("found no open base-check incident for this repo", self.log.read_text())

    def test_an_unreadable_repo_id_is_logged_and_looks_nothing_up(self):
        result = self.run_shell('mkdir -p "$INCIDENT_DIR"\nresolve_factory_incidents base-check\n',
                                repo_id=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("issue list", self.calls.read_text())
        log = self.log.read_text()
        self.assertIn("could not derive this private repo's incident alias", log)
        self.assertIn("could not name this repo's base-check incident", log)

    def test_with_no_repo_id_a_filing_falls_back_to_this_hosts_own_alias(self):
        a = self.run_shell("incident_subject\n", host="host-a", repo_id=None)
        b = self.run_shell("incident_subject\n", host="host-b", repo_id=None)
        self.assertRegex(a.stdout, r"^private repo [0-9a-f]{8}$")
        self.assertNotEqual(a.stdout, b.stdout)
        self.assertIn("using this host's own alias", self.log.read_text())


if __name__ == "__main__":
    unittest.main()
