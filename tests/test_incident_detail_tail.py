"""Tests for check_error_tail: extracting error lines from check output for incident details."""

import re
import subprocess
import tempfile
import unittest
from pathlib import Path


RUNNER = Path(__file__).resolve().parent.parent / "lanes" / "run_lane.sh"


def extract_function(name):
    """Extract a bash function by name from run_lane.sh."""
    text = RUNNER.read_text()
    # Match function definition possibly preceded by comments/whitespace
    pattern = r"(?:^|[\n;])" + re.escape(name) + r"\s*\(\)\s*\{.*?^\}\n"
    match = re.search(pattern, text, re.S | re.M)
    if not match:
        raise ValueError("Function %s not found in %s" % (name, RUNNER))
    return match.group(0)


def run_bash_function(func_text, func_name, *args, env=None):
    """Run a bash function with the given arguments."""
    script = func_text + f'\n{func_name} "$@"\n'
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        input="\n".join(args) if args else "",
        env=env or {},
    )
    return result.stdout.strip(), result.stderr.strip(), result.returncode


class TestIncidentDetailTail(unittest.TestCase):
    """Tests for the check_error_tail function."""

    def setUp(self):
        self.check_error_tail_func = extract_function("check_error_tail")

    def test_pyo3_and_make_error_lines(self):
        """Test that PyO3 version error and make error lines are captured."""
        # Create a log file with check output containing both error types
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            log_path = f.name
            f.write("""some build output
downloading cpython-3.15.2
error: the configured Python interpreter version (3.15) is newer than PyO3's maximum supported version (3.14)
while building ddtrace==3.19.9
make: *** [webhook-test] Error 1
more output""")

        try:
            # We need to simulate the log file being read by the function
            # The function reads from $LOG, so we'll set LOG env var
            env = {"LOG": log_path}
            # The function expects a log offset (byte position)
            # We want to read from the beginning, so offset = 0
            stdout, stderr, rc = run_bash_function(
                self.check_error_tail_func, "check_error_tail", "0", env=env
            )
            self.assertEqual(rc, 0, f"Function failed: {stderr}")
            # Should contain both error lines
            self.assertIn("the configured Python interpreter version (3.15) is newer than PyO3", stdout)
            self.assertIn("make: *** [webhook-test] Error 1", stdout)
            # Should have the check: line prefix in the detail (but the function only returns the tail)
            # The detail builder will prepend "check: ... (exit N)\n"
        finally:
            Path(log_path).unlink(missing_ok=True)

    def test_more_than_8_matching_lines_yields_last_8(self):
        """Test that more than 8 matching lines yields only the last 8."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            log_path = f.name
            # Write 10 error lines
            lines = [f"error: line {i}" for i in range(1, 11)]
            f.write("\n".join(lines))

        try:
            env = {"LOG": log_path}
            stdout, stderr, rc = run_bash_function(
                self.check_error_tail_func, "check_error_tail", "0", env=env
            )
            self.assertEqual(rc, 0, f"Function failed: {stderr}")
            # Should have only last 8 lines (lines 3-10)
            output_lines = stdout.split("\n")
            self.assertEqual(len(output_lines), 8)
            self.assertEqual(output_lines[0], "error: line 3")
            self.assertEqual(output_lines[-1], "error: line 10")
        finally:
            Path(log_path).unlink(missing_ok=True)

    def test_each_line_cut_to_200_chars(self):
        """Test that each matching line is cut to 200 characters."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            log_path = f.name
            # Write an error line longer than 200 chars
            long_line = "error: " + "x" * 250
            f.write(long_line)

        try:
            env = {"LOG": log_path}
            stdout, stderr, rc = run_bash_function(
                self.check_error_tail_func, "check_error_tail", "0", env=env
            )
            self.assertEqual(rc, 0, f"Function failed: {stderr}")
            self.assertEqual(len(stdout), 200)
        finally:
            Path(log_path).unlink(missing_ok=True)

    def test_no_matching_lines_yields_last_3_nonempty(self):
        """Test that output with no matching lines yields last 3 non-empty lines."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            log_path = f.name
            f.write("""line 1
line 2

line 3
line 4

line 5
""")

        try:
            env = {"LOG": log_path}
            stdout, stderr, rc = run_bash_function(
                self.check_error_tail_func, "check_error_tail", "0", env=env
            )
            self.assertEqual(rc, 0, f"Function failed: {stderr}")
            output_lines = stdout.split("\n")
            self.assertEqual(len(output_lines), 3)
            self.assertEqual(output_lines[0], "line 3")
            self.assertEqual(output_lines[1], "line 4")
            self.assertEqual(output_lines[2], "line 5")
        finally:
            Path(log_path).unlink(missing_ok=True)

    def test_case_insensitive_error_matching(self):
        """Test that Error (capital E) and FAILED are also matched."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            log_path = f.name
            f.write("""Error: something went wrong
FAILED test_something
warning: this is not matched
make: *** [target] Error 2""")

        try:
            env = {"LOG": log_path}
            stdout, stderr, rc = run_bash_function(
                self.check_error_tail_func, "check_error_tail", "0", env=env
            )
            self.assertEqual(rc, 0, f"Function failed: {stderr}")
            self.assertIn("Error: something went wrong", stdout)
            self.assertIn("FAILED test_something", stdout)
            self.assertIn("make: *** [target] Error 2", stdout)
            self.assertNotIn("warning: this is not matched", stdout)
        finally:
            Path(log_path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()