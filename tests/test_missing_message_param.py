"""A provider 400 naming a missing message parameter is transient, not work.

The openrouter lane's Cohere model answers some requests with a non-retryable
HTTP 400 that is the provider's own fault: `[Cohere] missing required
parameter: 'messages[60].content'`. Eight run logs ended on it from
2026-10-31, and five were parked `[work] agent exited 1`, each costing a
ladder rung (loom#2754).
"""

import unittest

from tests.test_provider_fault_class import classify

PARAM_FAULT = (
    '{"type":"error","error":{"name":"AI_APICallError","data":{"message":'
    '"[Cohere] missing required parameter: \'messages[60].content\'",'
    '"statusCode":400,"isRetryable":false}}}'
)


class MissingMessageParamTest(unittest.TestCase):
    def test_an_edited_run_that_ends_on_the_fault_is_transient(self):
        fail_class, retry_at, _, reason = classify(1, "working on it\n" + PARAM_FAULT, "edited")
        self.assertEqual(fail_class, "transient")
        self.assertEqual(retry_at, "")
        self.assertIn("provider fault", reason)
        self.assertIn("missing required parameter", reason)

    def test_an_unedited_run_that_ends_on_the_fault_names_the_provider_fault(self):
        fail_class, retry_at, out_of_credit, reason = classify(1, PARAM_FAULT, "")
        self.assertEqual(fail_class, "transient")
        self.assertEqual(retry_at, "")
        self.assertEqual(out_of_credit, "")
        self.assertIn("provider fault", reason)

    def test_the_fault_text_early_in_the_run_does_not_count(self):
        # Only the tail decides: a run that read the text and then failed its
        # tests is still work.
        output = PARAM_FAULT + "\n" + "\n".join("line %d" % n for n in range(8)) + \
            "\ntests failed\nerror: assertion"
        fail_class, _, _, reason = classify(1, output, "edited")
        self.assertEqual(fail_class, "work")
        self.assertEqual(reason, "")

    def test_a_run_that_exited_0_is_not_reclassed(self):
        fail_class, _, _, reason = classify(0, PARAM_FAULT, "edited")
        self.assertNotEqual(reason[:14], "provider fault")


if __name__ == "__main__":
    unittest.main()
