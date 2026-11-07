"""A provider 400 on a missing message parameter is transient, not the ticket's work.

The openrouter lane's model (cohere/north-mini-code:free) answers some requests with
an HTTP 400 that is the provider's own fault: 'missing required parameter: messages[X].content'
(statusCode 400, isRetryable false). This test verifies that classify_failure treats it as
transient with a FAIL_REASON naming the provider fault.
"""

import subprocess
import unittest
from pathlib import Path

from tests.test_run_lane import BRIEF, LaneRunnerTest

ROOT = Path(__file__).resolve().parents[1]
AGENT_CLI = ROOT / "lanes" / "agent_cli.sh"


MESSAGE_PARAM_FAULT = (
    'AI_APICallError: [Cohere] missing required parameter: messages[60].content '
    '(statusCode 400, isRetryable false)'
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


class MissingMessageParamTest(unittest.TestCase):
    def test_missing_message_param_after_edits_is_transient(self):
        fail_class, retry_at, _, reason = classify(1, "working on it\n" + MESSAGE_PARAM_FAULT)
        self.assertEqual(fail_class, "transient")
        self.assertEqual(retry_at, "")
        self.assertIn("provider could not render the prompt", reason)
        self.assertIn("missing required parameter: messages[", reason)

    def test_missing_message_param_unedited_is_transient(self):
        fail_class, retry_at, _, reason = classify(1, MESSAGE_PARAM_FAULT, "unedited")
        self.assertEqual(fail_class, "transient")
        self.assertEqual(retry_at, "")
        self.assertIn("provider could not render the prompt", reason)
        self.assertIn("missing required parameter: messages[", reason)

    def test_missing_message_param_is_no_limit(self):
        fail_class, retry_at, out_of_credit, _ = classify(1, MESSAGE_PARAM_FAULT)
        self.assertNotEqual(fail_class, "limit")
        self.assertEqual(retry_at, "")
        self.assertEqual(out_of_credit, "")

    def test_plain_exit_1_after_edits_stays_work(self):
        fail_class, retry_at, _, reason = classify(1, "tests failed\nerror: assertion")
        self.assertEqual(fail_class, "work")
        self.assertEqual(retry_at, "")
        self.assertEqual(reason, "")

    def test_the_words_in_an_earlier_tool_result_do_not_count(self):
        output = "\n".join(["read the brief: missing message parameter is transient"] +
                           ["line %d" % n for n in range(6)])
        self.assertEqual(classify(1, output)[0], "work")


class MissingMessageParamRunTest(LaneRunnerTest):
    """A whole lane run on the fake agent, parked by the real runner."""

    def test_missing_message_param_parks_transient_naming_the_error(self):
        self.drop_brief("T042-fix-the-thing.md", BRIEF)
        result = self.run_lane(FAKE_AGENT="say", FAKE_OUT=MESSAGE_PARAM_FAULT,
                               FAKE_EDIT="1", FAKE_EXIT="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        reason = self.failure_line()
        self.assertTrue(reason.startswith("[transient] agent exited 1: "), reason)
        self.assertIn("provider could not render the prompt", reason)
        self.assertIn("missing required parameter: messages[", reason)
        beat = self.beat()
        self.assertTrue(beat["available"], beat)
        self.assertNotIn("limit", beat.get("reason") or "")

    def test_plain_exit_1_still_parks_as_work(self):
        self.drop_brief("T042-fix-the-thing.md", BRIEF)
        self.run_lane(FAKE_AGENT="say", FAKE_OUT="tests failed",
                      FAKE_EDIT="1", FAKE_EXIT="1")
        self.assertEqual(self.failure_line(), "[work] agent exited 1")


if __name__ == "__main__":
    unittest.main()
