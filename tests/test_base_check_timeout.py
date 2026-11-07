"""test_base_check_timeout.py

Simple test to verify that exit 124 is excluded from base-verdict caching.

This test verifies the fix for the issue where a base check that times out (exit 124)
was being cached as a red base for all subsequent lane runs.
"""

import os
import unittest
from pathlib import Path


class BaseCheckTimeoutTest(unittest.TestCase):
    """Test that exit 124 is excluded from base-verdict caching like 143, 125, 126, 127."""

    def test_base_check_timeout_excluded_from_caching(self):
        """Test that exit 124 is excluded from base-verdict caching.

        This verifies the fix in lanes/run_lane.sh where the condition
        was updated to exclude 124 from being cached.
        """
        run_lane_path = Path(__file__).parent.parent / "lanes" / "run_lane.sh"

        with open(run_lane_path, "r") as f:
            content = f.read()

        # Find the base-verdict write condition
        # Look for the pattern: base_verdict_write "$verdict_key" "$BASELINE_EXIT"
        lines = content.split('\n')

        # Look for the key condition that writes to base-verdicts
        base_verdict_write_line = None
        for i, line in enumerate(lines):
            if 'base_verdict_write' in line:
                base_verdict_write_line = line
                # Also check the condition on the previous line
                if i > 0:
                    condition_line = lines[i-1]
                    self.assertIn('if [ "$cached_verdict" -eq 0 ] && [ -z "$changed" ] &&',
                                condition_line,
                                "Should have the base-verdict write condition")

        # Verify the condition excludes 124 like it does for 143, 125, 126, 127
        self.assertIn(
            '[ "$BASELINE_EXIT" -ne 143 ] && [ "$BASELINE_EXIT" -ne 125 ] &&',
            content,
            "The base-verdict write condition should exclude 143 and 125"
        )

        # Check that 124 is now also excluded (this is the fix)
        self.assertIn(
            '[ "$BASELINE_EXIT" -ne 126 ] && [ "$BASELINE_EXIT" -ne 127 ] && [ "$BASELINE_EXIT" -ne 124 ]',
            content,
            "The base-verdict write condition should now also exclude 124 (this is the fix)"
        )

    def test_fix_applied_to_correct_line(self):
        """Test that the fix was applied to the correct line in run_lane.sh.

        This test specifically verifies that the fix in lanes/run_lane.sh
        excludes exit code 124 from being stored in the base-verdict cache.
        """
        run_lane_path = Path(__file__).parent.parent / "lanes" / "run_lane.sh"
        with open(run_lane_path, "r") as f:
            content = f.read()

        # Find the specific line that excludes exit codes from being cached
        # The fix should exclude 124 like 143, 125, 126, 127 are excluded
        self.assertIn(
            '[ "$BASELINE_EXIT" -ne 143 ] && [ "$BASELINE_EXIT" -ne 125 ] &&',
            content,
            "Should exclude exit codes 143 and 125 from caching"
        )

        # The fix adds 124 to the exclusion list
        self.assertIn(
            '[ "$BASELINE_EXIT" -ne 126 ] && [ "$BASELINE_EXIT" -ne 127 ] && [ "$BASELINE_EXIT" -ne 124 ]',
            content,
            "Should exclude exit code 124 from caching (this is the fix)"
        )


if __name__ == "__main__":
    unittest.main()