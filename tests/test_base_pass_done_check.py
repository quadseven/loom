"""A vacuous base-test result asks the brain for a done-check."""

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class BasePassDoneCheckTest(unittest.TestCase):
    def test_lane_distinguishes_only_base_passes_outside_fix_rounds(self):
        script = (ROOT / "lanes" / "run_lane.sh").read_text()
        start = script.index("final_base_tests() {")
        body = script[start:script.index("\n}\n", start)]
        self.assertIn("every changed test passes on base", body)
        self.assertIn('[ -z "${FIX_PR:-}" ]', body)
        self.assertIn("timed out on base:\\|could not run on base:", body)

    def test_station_holds_for_done_check_without_a_fix_round(self):
        source = (ROOT / "bus" / "station.py").read_text()
        start = source.index("def _report_failure(")
        body = source[start:source.index("\n    def _retry_factory_check", start)]
        self.assertIn("every changed test passes on base", body)
        self.assertIn('self._add_label(repo, number, "needs-brain")', body)
        self.assertIn("Brain: please do a done-check.", body)
        self.assertIn("if done_check and not fix_pr:", body)
        self.assertIn('self._relabel(repo, number, "loom:failed")', body)


if __name__ == "__main__":
    unittest.main()
