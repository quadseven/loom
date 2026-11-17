"""An agy run that ends on the CLI's background-task wait is not the ticket's work.

Two agy runs on 2026-11-11 finished their work and a green full suite, then
the CLI printed `root agent idle; waiting up to 30m0s for 1 background
task(s)`, waited on a task it had left running, and ended `error:
interrupted` with exit 1. The runner parked both as `[work] agent exited 1`,
so each ticket lost a model ladder rung to the CLI (loom#2984).
"""

import unittest

from tests.test_provider_fault_class import classify

WAIT_TAIL = (
    "Parallel unittest total: Ran 6949 tests across 248 modules\n"
    "OK\n"
    "root agent idle; waiting up to 30m0s for 1 background task(s)\n"
    "error: interrupted\n"
)


class AgyBackgroundWaitTest(unittest.TestCase):
    def test_a_run_ending_on_the_background_task_wait_is_transient(self):
        fail_class, retry_at, _, reason = classify(1, "working on it\n" + WAIT_TAIL)
        self.assertEqual(fail_class, "transient")
        self.assertEqual(retry_at, "")
        self.assertIn("background task", reason)

    def test_the_wait_without_edits_is_transient_too(self):
        self.assertEqual(classify(1, WAIT_TAIL, edited="")[0], "transient")

    def test_an_agy_exit_1_without_the_wait_line_stays_work(self):
        fail_class, _, _, reason = classify(1, "tests failed\nerror: interrupted\n")
        self.assertEqual(fail_class, "work")
        self.assertEqual(reason, "")

    def test_the_wait_line_without_the_interruption_stays_work(self):
        output = "root agent idle; waiting up to 30m0s for 1 background task(s)\nFAILED (errors=2)\n"
        self.assertEqual(classify(1, output)[0], "work")

    def test_the_wait_line_read_early_in_the_run_does_not_count(self):
        # Only the last lines the CLI printed are read: a log line the agent
        # read earlier is no ending.
        output = "\n".join([WAIT_TAIL] + ["line %d" % n for n in range(6)])
        self.assertEqual(classify(1, output)[0], "work")


if __name__ == "__main__":
    unittest.main()
