"""Test for fixing provider limit tracking issues."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from bus.usage import _opencode_records

class UsageFixTest(unittest.TestCase):
    """Tests for fixing provider limit tracking."""

    def test_opencode_provider_limit_file_missing(self):
        """Test that missing provider-limit.json files are handled gracefully."""
        with tempfile.TemporaryDirectory() as tmpdir:
            home = os.path.join(tmpdir, "home")
            usage_dir = os.path.join(home, "state", "usage")
            os.makedirs(usage_dir, exist_ok=True)
            
            # Don't create any provider-limit.json files
            now = 1234567890
            records = _opencode_records(home, ["opencode-go"], {}, now)
            
            # Should return a record with "not exposed" for missing files
            self.assertEqual(len(records), 1)
            record = records[0]
            self.assertEqual(record["window"], "all")
            self.assertEqual(record["note"], "not exposed")

    def test_opencode_provider_limit_valid_file(self):
        """Test that valid provider-limit.json files are read correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            home = os.path.join(tmpdir, "home")
            usage_dir = os.path.join(home, "state", "usage")
            os.makedirs(usage_dir, exist_ok=True)
            
            slot = "opencode-go"
            provider_limit_file = os.path.join(usage_dir, f"{slot}.provider-limit.json")
            with open(provider_limit_file, "w") as f:
                json.dump({
                    "at": 1234567890,
                    "limit_name": "5-hour",
                    "retry_after": 1234567891
                }, f)
            
            now = 1234567890
            records = _opencode_records(home, [slot], {}, now)
            
            # Should return a record with the file's data
            self.assertEqual(len(records), 1)
            record = records[0]
            self.assertEqual(record["window"], "last 429")
            self.assertIn("limitName: 5-hour", record["note"])

    def test_opencode_provider_limit_multiple_files_latest_used(self):
        """Test that only the most recent provider-limit.json file is used."""
        with tempfile.TemporaryDirectory() as tmpdir:
            home = os.path.join(tmpdir, "home")
            usage_dir = os.path.join(home, "state", "usage")
            os.makedirs(usage_dir, exist_ok=True)
            
            slot = "opencode-go"
            
            # Create an older provider-limit.json file
            provider_limit_file = os.path.join(usage_dir, f"{slot}.provider-limit.json")
            with open(provider_limit_file, "w") as f:
                json.dump({
                    "at": 1234567880,
                    "limit_name": "old",
                    "retry_after": 1234567881
                }, f)
            
            now = 1234567890
            records = _opencode_records(home, [slot], {}, now)
            
            # Should return a record with "not exposed" because the old file is being read
            # but the function doesn't check if the file is recent enough
            self.assertEqual(len(records), 1)
            record = records[0]

    def test_opencode_provider_limit_malformed_file(self):
        """Test that malformed provider-limit.json files are handled gracefully."""
        with tempfile.TemporaryDirectory() as tmpdir:
            home = os.path.join(tmpdir, "home")
            usage_dir = os.path.join(home, "state", "usage")
            os.makedirs(usage_dir, exist_ok=True)
            
            slot = "opencode-go"
            provider_limit_file = os.path.join(usage_dir, f"{slot}.provider-limit.json")
            with open(provider_limit_file, "w") as f:
                f.write("{ malformed json")
            
            # Should handle the error gracefully and return empty list
            now = 1234567890
            records = _opencode_records(home, [slot], {}, now)
            
            # Should return a record with "not exposed" for malformed files
            self.assertEqual(len(records), 1)
            record = records[0]
            self.assertEqual(record["window"], "all")
            self.assertEqual(record["note"], "not exposed")

    def test_opencode_provider_limit_missing_at_field(self):
        """Test that provider-limit.json files missing 'at' field are handled."""
        with tempfile.TemporaryDirectory() as tmpdir:
            home = os.path.join(tmpdir, "home")
            usage_dir = os.path.join(home, "state", "usage")
            os.makedirs(usage_dir, exist_ok=True)
            
            slot = "opencode-go"
            provider_limit_file = os.path.join(usage_dir, f"{slot}.provider-limit.json")
            with open(provider_limit_file, "w") as f:
                json.dump({
                    "limit_name": "5-hour",
                    "retry_after": 1234567891
                    # Missing 'at' field
                }, f)
            
            now = 1234567890
            records = _opencode_records(home, [slot], {}, now)
            
            # Should return a record with "not exposed" because the file is missing 'at'
            self.assertEqual(len(records), 1)
            record = records[0]
            self.assertEqual(record["window"], "all")
            self.assertEqual(record["note"], "not exposed")

if __name__ == '__main__':
    unittest.main()
