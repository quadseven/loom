"""A provider's HTTP 410 for a retired model is transient, not the ticket's work.

The free-nvidia route kept asking a model the provider had retired; each run
ended `[work] agent exited 1` and the ticket climbed a ladder rung on a fault
its brief did not cause (loom#3243).
"""

import unittest

from tests.test_provider_fault_class import classify

RETIRED = (
    '{"type":"error","error":{"name":"AI_APICallError","data":{"message":'
    '"Gone: The model \'vendor/model-x\' has reached its end of life on '
    '2026-10-03","statusCode":410}}}'
)


class ProviderRetiredModelTest(unittest.TestCase):
    def test_a_410_end_of_life_after_edits_is_transient(self):
        fail_class, retry_at, _, reason = classify(1, "working on it\n" + RETIRED)
        self.assertEqual(fail_class, "transient")
        self.assertEqual(retry_at, "")
        self.assertIn("end of life", reason)

    def test_the_words_in_an_earlier_tool_result_do_not_count(self):
        output = "\n".join(["read: Gone, has reached its end of life"] +
                           ["line %d" % n for n in range(6)])
        self.assertEqual(classify(1, output)[0], "work")


if __name__ == "__main__":
    unittest.main()
