"""Host timeouts in unchanged tests are not lane work failures."""
import unittest

from bus.validation_timeout import only_untouched_test_timeouts


class ValidationTimeoutClass(unittest.TestCase):
    def test_only_timeout_errors_in_untouched_test_files_are_transient(self):
        output = """ERROR: test_case (test_example.Example.test_case)
Traceback (most recent call last):
  File \"test_example.py\", line 1, in test_case
    subprocess.run(command, timeout=60)
subprocess.TimeoutExpired: Command '['bash']' timed out after 60 seconds
"""
        self.assertTrue(only_untouched_test_timeouts(output, {"bus/brain.py"}))

    def test_changed_test_file_is_not_classified_as_transient(self):
        output = """ERROR: test_case (test_example.Example.test_case)
Traceback (most recent call last):
subprocess.TimeoutExpired: Command '['bash']' timed out after 60 seconds
"""
        self.assertFalse(only_untouched_test_timeouts(output, {"tests/test_example.py"}))

    def test_mixed_timeout_and_assertion_failures_are_not_transient(self):
        output = """ERROR: test_case (test_example.Example.test_case)
subprocess.TimeoutExpired: Command '['bash']' timed out after 60 seconds
FAIL: test_other (test_example.Example.test_other)
AssertionError: mismatch
"""
        self.assertFalse(only_untouched_test_timeouts(output, set()))


if __name__ == "__main__":
    unittest.main()
