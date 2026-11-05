"""A provider's own fault on one request is transient, not the ticket's work.

opencode-go's provider answered two runs with a non-retryable 400
`[prompt_rendering_failed]` while about 30 other runs on the same model
finished the same day (2026-10-25). The runner parked both as
`[work] agent exited 1`, so each ticket climbed a model ladder rung on a fault
its brief did not cause (loom#1749).
"""

import subprocess
import unittest
from pathlib import Path

from tests.test_run_lane import BRIEF, LaneRunnerTest

ROOT = Path(__file__).resolve().parents[1]
AGENT_CLI = ROOT / "lanes" / "agent_cli.sh"

RENDER_FAULT = (
    '{"type":"error","error":{"name":"APIError","data":{"message":'
    '"400 [prompt_rendering_failed] The request could not be rendered into a '
    'prompt for this model","statusCode":400,"isRetryable":false}}}'
)


def classify(status, output, edited="edited"):
    """FAIL_CLASS, RETRY_AT, OUT_OF_CREDIT and FAIL_REASON for one run."""
    script = (
        '. "$1"; tail="$(printf "%s\\n" "$3" | tail -n 5)"; '
        'classify_failure "$2" "$3" "$4" "$tail"; '
        'printf "%s\\n%s\\n%s\\n%s\\n" "$FAIL_CLASS" "$RETRY_AT" "$OUT_OF_CREDIT" "${FAIL_REASON:-}"'
    )
    out = subprocess.run(
        ["/bin/bash", "-c", script, "classify", str(AGENT_CLI), str(status), output, edited],
        capture_output=True, text=True, check=True, timeout=30,
    ).stdout
    fail_class, retry_at, out_of_credit, reason = out.split("\n")[:4]
    return fail_class, retry_at, out_of_credit, reason


class ProviderFaultClassTest(unittest.TestCase):
    def test_a_prompt_rendering_failure_after_edits_is_transient(self):
        fail_class, retry_at, _, reason = classify(1, "working on it\n" + RENDER_FAULT)
        self.assertEqual(fail_class, "transient")
        self.assertEqual(retry_at, "")
        self.assertIn("prompt_rendering_failed", reason)

    def test_a_prompt_rendering_failure_is_no_limit(self):
        fail_class, retry_at, out_of_credit, _ = classify(1, RENDER_FAULT)
        self.assertNotEqual(fail_class, "limit")
        self.assertEqual(retry_at, "")
        self.assertEqual(out_of_credit, "")

    def test_a_plain_exit_1_after_edits_stays_work(self):
        fail_class, retry_at, _, reason = classify(1, "tests failed\nerror: assertion")
        self.assertEqual(fail_class, "work")
        self.assertEqual(retry_at, "")
        self.assertEqual(reason, "")

    def test_the_words_in_an_earlier_tool_result_do_not_count(self):
        # A ticket about this fault, read by the agent, is no fault: only the
        # last lines the CLI printed are read.
        output = "\n".join(["read the brief: prompt_rendering_failed is transient"] +
                           ["line %d" % n for n in range(6)])
        self.assertEqual(classify(1, output)[0], "work")


class ProviderFaultRunTest(LaneRunnerTest):
    """A whole lane run on the fake agent, parked by the real runner."""

    def test_a_prompt_rendering_failure_parks_transient_naming_the_error(self):
        self.drop_brief("T042-fix-the-thing.md", BRIEF)
        result = self.run_lane(FAKE_AGENT="say", FAKE_OUT=RENDER_FAULT,
                               FAKE_EDIT="1", FAKE_EXIT="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        reason = self.failure_line()
        self.assertTrue(reason.startswith("[transient] agent exited 1: "), reason)
        self.assertIn("prompt_rendering_failed", reason)
        beat = self.beat()
        self.assertTrue(beat["available"], beat)
        self.assertNotIn("limit", beat.get("reason") or "")

    def test_a_plain_exit_1_still_parks_as_work(self):
        self.drop_brief("T042-fix-the-thing.md", BRIEF)
        self.run_lane(FAKE_AGENT="say", FAKE_OUT="tests failed",
                      FAKE_EDIT="1", FAKE_EXIT="1")
        self.assertEqual(self.failure_line(), "[work] agent exited 1")


def load_tests(loader, tests, pattern):
    # Only this module's own runs: LaneRunnerTest's tests run in theirs.
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(ProviderFaultClassTest))
    for name in ProviderFaultRunTest.__dict__:
        if name.startswith("test_"):
            suite.addTest(ProviderFaultRunTest(name))
    return suite


if __name__ == "__main__":
    unittest.main()
