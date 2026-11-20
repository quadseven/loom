"""A final check that fails only on sandbox refusals is an environment fault."""
import unittest

from bus.validation_refusal import only_sandbox_refusals

REFUSED = ("ERROR: test_runner (suite.Runner.test_runner)\n"
           "Traceback (most recent call last):\n"
           "  File \"x.py\", line 1, in run\n"
           "PermissionError: [Errno 1] Operation not permitted: '/venv/bin/python'\n")
ASSERTION = ("FAIL: test_sum (suite.Math.test_sum)\n"
             "Traceback (most recent call last):\n"
             "AssertionError: 1 != 2\n")


class ValidationRefusalClass(unittest.TestCase):
    def test_only_operation_not_permitted_errors_are_a_sandbox_refusal(self):
        self.assertTrue(only_sandbox_refusals(REFUSED * 3))

    def test_an_assertion_failure_stays_work(self):
        self.assertFalse(only_sandbox_refusals(ASSERTION))
        self.assertFalse(only_sandbox_refusals(REFUSED + ASSERTION))

    def test_another_permission_error_or_empty_output_stays_work(self):
        other = REFUSED.replace("Operation not permitted", "Permission denied")
        self.assertFalse(only_sandbox_refusals(other))
        self.assertFalse(only_sandbox_refusals("ok\n"))


class ValidationRefusalWiring(unittest.TestCase):
    def test_the_lane_parks_a_refusal_as_an_environment_fault(self):
        from pathlib import Path
        body = (Path(__file__).resolve().parent.parent / "lanes" / "run_lane.sh").read_text()
        self.assertIn("bus.validation_refusal", body)
        self.assertIn("validation environment unavailable: sandbox refusal", body)


if __name__ == "__main__":
    unittest.main()
