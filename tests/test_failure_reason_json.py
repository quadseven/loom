"""A parked run's failure reason is the lane's own failure line, and the
sticky comment helper keeps the GitHub response out of the run log."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import station  # noqa: E402

RUNNER = Path(__file__).resolve().parent.parent / "lanes" / "run_lane.sh"
LANE_LINE = "[2030-01-01T00:00:00Z] T failed: [factory] setup failed: base check exited 1"
JSON_LINE = ('{"id":1,"body":"... setup failed: base check exited 1 ...",'
             '"reactions":{"url":"https://example.invalid/r"}}')


class FailureReasonJsonTest(unittest.TestCase):
    def test_the_lane_failure_line_wins_over_a_later_json_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = station.Settings(tmp, ["example-owner/repo"], [], [],
                                        disk_free_min_gb=0)
            runner = station.Station(mock.Mock(), settings)
            log = Path(tmp, "logs", "codex", "T.log")
            log.parent.mkdir(parents=True)
            log.write_text(LANE_LINE + "\n" + JSON_LINE + "\n")
            reason = runner._failure_reason("codex", "T")
        self.assertNotIn("reactions", reason)
        self.assertEqual(reason, "[factory] setup failed: base check exited 1")


class StickyCommentLogTest(unittest.TestCase):
    def test_the_api_response_is_not_appended_to_the_run_log(self):
        function = re.search(r"^upsert_sticky_comment\(\) \{\n.*?^\}\n",
                             RUNNER.read_text(), re.S | re.M).group(0)
        for existing in ("", "42"):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as tmp:
                log = Path(tmp, "run.log")
                log.touch()
                gh = Path(tmp, "bin", "gh")
                gh.parent.mkdir()
                gh.write_text(
                    "#!/bin/bash\n"
                    "case \"$*\" in\n"
                    "  *--paginate*) printf '%s\\n' \"$FAKE_ID\" ;;\n"
                    "  *-X*) printf '%s\\n' '" + JSON_LINE + "' ;;\n"
                    "esac\n")
                gh.chmod(0o755)
                script = (
                    "set -uo pipefail\n"
                    "python3() { cat > /dev/null; echo new body; }\n"
                    "NET_TIMEOUT=10 LOOM_ROOT=. LOG=\"$1\"\n"
                    + function
                    + "echo hi | upsert_sticky_comment example-owner/repo 7 '<!-- m -->' once\n")
                env = dict(os.environ, PATH=str(gh.parent) + ":" + os.environ["PATH"],
                           FAKE_ID=existing)
                result = subprocess.run(["/bin/bash", "-c", script, "bash", str(log)],
                                        capture_output=True, text=True, timeout=60,
                                        env=env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("reactions", log.read_text())


if __name__ == "__main__":
    unittest.main()
