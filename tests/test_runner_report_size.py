"""A cluster run report whose files are large: lane_job.py report cuts a log
past runners.REPORT_FILE_MAX to its tail, and Runners._untar names the
largest member when a report still unpacks past UNPACKED_MAX."""
from __future__ import annotations

import io
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bus import runners  # noqa: E402
from tests import test_runners  # noqa: E402

TICKET, BRIEF = test_runners.TICKET, test_runners.BRIEF

LOG = "logs/opencode-go-2/%s.log" % TICKET


class ReportSizeTest(test_runners.RunnersTest):
    def test_a_log_past_the_file_cap_is_packed_as_its_tail_with_its_size_named(self):
        run, run_id = self.dispatched()
        text = "".join("line %04d\n" % i for i in range(500))
        with mock.patch.object(runners, "REPORT_FILE_MAX", 1000):
            artifact = self.seal(run_id, {"briefs/opencode-go-2/done/%s.md" % TICKET: BRIEF, LOG: text})
        with mock.patch.object(runners, "UNPACKED_MAX", 4000):
            files = self.runner().open_report(artifact, run_id, self.mac_key)
        got = files[LOG].decode()
        first, rest = got.split("\n", 1)
        self.assertIn(str(len(text)), first)
        self.assertEqual(rest, text[-1000:])
        self.assertEqual(files["briefs/opencode-go-2/done/%s.md" % TICKET].decode(), BRIEF)

    def test_a_log_under_the_file_cap_is_packed_whole(self):
        run, run_id = self.dispatched()
        with mock.patch.object(runners, "REPORT_FILE_MAX", 1000):
            artifact = self.seal(run_id, {LOG: "short\n"})
        self.assertEqual(self.runner().open_report(artifact, run_id, self.mac_key)[LOG], b"short\n")

    def test_untar_past_the_limit_names_the_largest_member_and_its_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "r.tgz")
            with tarfile.open(path, "w:gz") as tar:
                for name, n in (("logs/s/a.log", 300), ("logs/s/big.log", 3000), ("logs/s/b.log", 500)):
                    info = tarfile.TarInfo(name)
                    info.size = n
                    tar.addfile(info, io.BytesIO(b"x" * n))
            with mock.patch.object(runners, "UNPACKED_MAX", 3500):
                with self.assertRaises(runners.ReportError) as caught:
                    self.runner()._untar(path)
        msg = str(caught.exception)
        self.assertIn("the report unpacks past 3500 bytes", msg)
        self.assertIn("largest file: logs/s/big.log, 3000 bytes", msg)


# Do not rerun the inherited suite here.
for _name in [n for n in dir(test_runners.RunnersTest) if n.startswith("test_")]:
    if _name not in ReportSizeTest.__dict__:
        setattr(ReportSizeTest, _name, None)

if __name__ == "__main__":
    unittest.main()
