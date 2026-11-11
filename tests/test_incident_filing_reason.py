"""lanes/run_lane.sh: a failed factory incident filing says why.

`file_factory_incident` printed only "factory incident filing failed", so the
ticket's hold note and the station's held line could not tell a failed
`gh issue create` from a failed private-repo alias write. It now names the
step and the first line of its error, with local paths removed, and the
station still reads the result as "no incident" (the ticket is released
after the hold).
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bus import station  # noqa: E402

RUNNER = ROOT / "lanes" / "run_lane.sh"
FAILED = "factory incident filing failed"
# A home folder in an error line, built so the source holds no user path.
HOME_DIR = "/" + "/".join(["Users", "someone"])


def functions(*names: str) -> str:
    text = RUNNER.read_text()
    found = []
    for name in names:
        match = re.search(r"^%s\(\) \{.*?^\}\n" % re.escape(name), text, re.S | re.M)
        if match is None:
            raise AssertionError("lanes/run_lane.sh has no %s()" % name)
        found.append(match.group(0))
    return "\n".join(found)


# The filing's collaborators, stubbed: a public repo, a clean leak scan, no
# open incident, and a `gh` that fails `issue create` the way $GH_ERROR says.
STUBS = r'''
repo_is_public() { [ "${REPO_PRIVATE:-}" = false ]; }
incident_ticket() { printf 'T042'; }
leak_scan() { return 0; }
open_issue() { :; }
upsert_sticky_comment() { :; }
gh() {
  if [ "$1 $2" = "issue create" ]; then printf '%b' "$GH_ERROR" >&2; return 1; fi
  return 0
}
'''


class IncidentFilingReason(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.log = self.tmp / "lane.log"
        self.log.touch()

    def tearDown(self):
        self._tmp.cleanup()

    def file_incident(self, incident_dir: Path, gh_error: str = "",
                      private: bool = False) -> str:
        script = (functions("incident_subject", "incident_text_clean", "incident_file",
                            "incident_filing_failed",
                            "file_factory_incident")
                  + STUBS + 'file_factory_incident environment "python: 3.12"\n')
        env = {"PATH": "/usr/bin:/bin", "LOG": str(self.log), "TMPDIR": str(self.tmp),
               "INCIDENT_DIR": str(incident_dir), "REPO": "example/repo",
               "FACTORY_REPO": "quadseven/loom", "GH_ERROR": gh_error,
               "INCIDENT_MARKER": "<!-- loom:factory-incident v1 -->",
               "INCIDENT_WITHHELD": "withheld", "INCIDENT_REPEAT_MARKER": "<!-- r -->"}
        if not private:
            env["REPO_PRIVATE"] = "false"
        result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_a_failed_issue_create_names_the_step_and_its_first_error_line(self):
        reason = self.file_incident(
            self.tmp / "incidents",
            gh_error="HTTP 403: Resource not accessible by integration (open "
                     "%s/.loom/state/token.json)\\nsecond line\\n" % HOME_DIR)
        self.assertTrue(reason.startswith(FAILED + " (issue create: "), reason)
        self.assertIn("HTTP 403: Resource not accessible by integration", reason)
        self.assertNotIn("second line", reason)
        self.assertNotIn(HOME_DIR, reason)
        self.assertNotIn(".loom/state", reason)
        # The whole error still goes to the lane log.
        self.assertIn("second line", self.log.read_text())

    def test_a_failed_private_repo_alias_write_names_that_step(self):
        blocked = self.tmp / "incidents"
        blocked.write_text("a file where the incident folder should be\n")
        reason = self.file_incident(blocked, private=True)
        self.assertTrue(reason.startswith(FAILED + " (alias write: "), reason)
        self.assertNotIn(str(self.tmp), reason)

    def test_the_station_still_reads_a_detailed_failure_as_no_incident(self):
        reason = self.file_incident(
            self.tmp / "incidents",
            gh_error="could not add label to %s#%d: not found\\n" % ("quadseven/loom", 12))
        self.assertTrue(reason.startswith(FAILED + " (issue create: "), reason)
        bare = "base check exited 124; " + reason
        self.assertIsNone(station.HELD_FOR_RE.search(bare))


if __name__ == "__main__":
    unittest.main()
