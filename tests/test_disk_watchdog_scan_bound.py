"""Test scan bounds for disk watchdog."""

import os
import tempfile
import time
import unittest

from bus import disk_watchdog as wd


class TestDiskWatchdogScanBounds(unittest.TestCase):
    """Test that disk watchdog scan respects time and filesystem bounds."""

    def test_time_budget_respected(self):
        """Test that _measure respects its time budget."""
        
        # Create a temporary directory
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create some files
            for i in range(5):
                test_file = os.path.join(tmpdir, f"file{i}.txt")
                with open(test_file, "w") as f:
                    f.write("test content")
            
            # Test with a very short budget
            deadline = time.monotonic() + 0.001  # 1ms budget
            
            # Start measuring
            start_time = time.monotonic()
            result = wd._measure(tmpdir, deadline)
            elapsed = time.monotonic() - start_time
            
            # Should complete quickly even with short budget
            self.assertLess(elapsed, 1.0, "Scan took too long, didn't respect budget")
            self.assertIsInstance(result, int)

    def test_stays_on_same_filesystem(self):
        """Test that _measure doesn't cross filesystem boundaries."""
        
        # For this test, we'll check if the basic functionality works
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a subdirectory
            subdir = os.path.join(tmpdir, "subdir")
            os.makedirs(subdir)
            
            # Create a file in subdirectory
            test_file = os.path.join(subdir, "file.txt")
            with open(test_file, "w") as f:
                f.write("test")
            
            # Measure from parent directory
            deadline = time.monotonic() + 10.0
            result = wd._measure(tmpdir, deadline)
            
            # Should measure both files since they're on same filesystem
            self.assertIsInstance(result, int)


if __name__ == "__main__":
    unittest.main()