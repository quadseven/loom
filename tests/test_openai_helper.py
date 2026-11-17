"""The OpenAI decision helper (bus/openai_helper.py, loom#3188): routing,
triage and duplicate verdicts from a small model, each call priced into the
shared ledger and refused at the monthly cap. No test reaches the network:
every call goes to a fake transport."""
import calendar
import json
import tempfile
import unittest

from bus import openai_budget as ob
from bus import openai_helper as oh

NOV = calendar.timegm((2026, 11, 15, 12, 0, 0))
DEC = calendar.timegm((2026, 12, 1, 0, 0, 1))
KEY = "sk-test-not-a-real-key"


class FakeTransport:
    """Answers each request with the next canned reply and keeps the request."""

    def __init__(self, *replies, status=200, usage=None):
        self.replies = list(replies)
        self.status = status
        self.usage = {"prompt_tokens": 1000, "completion_tokens": 100} if usage is None else usage
        self.requests = []

    def __call__(self, url, headers, body, timeout):
        self.requests.append({"url": url, "headers": headers, "body": json.loads(body)})
        content = self.replies.pop(0) if self.replies else "{}"
        answer = {"choices": [{"message": {"content": content}}]}
        if self.usage is not False:
            answer["usage"] = self.usage
        return self.status, answer


class HelperCase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()

    def helper(self, transport, now=NOV, env=None):
        return oh.Helper(self.home, KEY, env=env or {}, clock=lambda: now, transport=transport)

    def spend_to(self, usd, now=NOV):
        # gpt-6-luna output is 0.50 USD a million tokens.
        ob.record(self.home, "lane", "gpt-6-luna", ob.Usage(0, 0, int(usd * 2_000_000)), now)


class Flag(HelperCase):
    def test_the_helper_is_off_unless_the_operator_turns_it_on(self):
        self.assertFalse(oh.enabled({}))
        self.assertFalse(oh.enabled({"LOOM_OPENAI_HELPER": "yes"}))
        self.assertTrue(oh.enabled({"LOOM_OPENAI_HELPER": "1"}))


class Calls(HelperCase):
    def test_route_returns_one_of_the_offered_lanes(self):
        fake = FakeTransport('{"lane": "codex", "reason": "needs tests"}')
        lane = self.helper(fake).route("Fix the flaky test", ["spark", "codex"])
        self.assertEqual(lane, "codex")
        [req] = fake.requests
        self.assertEqual(req["url"], oh.CHAT_URL)
        self.assertEqual(req["body"]["model"], ob.HELPER_MODEL)
        self.assertEqual(req["headers"]["Authorization"], "Bearer " + KEY)
        self.assertIn("spark", json.dumps(req["body"]["messages"]))

    def test_a_lane_that_was_not_offered_is_no_answer(self):
        fake = FakeTransport('{"lane": "anthropic"}')
        self.assertIsNone(self.helper(fake).route("x", ["spark", "codex"]))

    def test_triage_returns_a_difficulty_and_readiness(self):
        fake = FakeTransport('{"difficulty": "easy", "ready": true, "reason": "one file"}')
        verdict = self.helper(fake).triage("Rename a constant")
        self.assertEqual(verdict, {"difficulty": "easy", "ready": True, "reason": "one file"})

    def test_triage_with_an_unknown_difficulty_is_an_error(self):
        fake = FakeTransport('{"difficulty": "trivial", "ready": true}')
        with self.assertRaises(oh.HelperError):
            self.helper(fake).triage("x")

    def test_duplicate_names_a_candidate_or_none(self):
        fake = FakeTransport('{"duplicate_of": 12, "reason": "same bug"}',
                             '{"duplicate_of": 99}', '{"duplicate_of": null}')
        h = self.helper(fake)
        candidates = [(12, "Crash on empty input"), (13, "Docs typo")]
        self.assertEqual(h.duplicate("Crash when input is empty", candidates),
                         {"duplicate_of": 12, "reason": "same bug"})
        # A number outside the candidates is not a verdict.
        self.assertEqual(h.duplicate("x", candidates)["duplicate_of"], None)
        self.assertEqual(h.duplicate("x", candidates)["duplicate_of"], None)

    def test_duplicate_with_no_candidates_makes_no_call(self):
        fake = FakeTransport()
        self.assertEqual(self.helper(fake).duplicate("x", [])["duplicate_of"], None)
        self.assertEqual(fake.requests, [])

    def test_every_call_is_priced_into_the_ledger(self):
        fake = FakeTransport('{"difficulty": "easy", "ready": true}',
                             usage={"prompt_tokens": 2000, "completion_tokens": 400,
                                    "prompt_tokens_details": {"cached_tokens": 1000}})
        self.helper(fake).triage("x")
        [line] = [json.loads(x) for x in open(ob.ledger_path(self.home))]
        self.assertEqual(line["source"], "helper")
        self.assertAlmostEqual(line["cost_usd"], (1000 * 0.10 + 1000 * 0.01 + 400 * 0.50) / 1e6)
        self.assertNotIn(KEY, json.dumps(line))

    def test_an_answer_without_usage_is_charged_the_pessimistic_amount(self):
        fake = FakeTransport('{"difficulty": "easy", "ready": true}', usage=False)
        self.helper(fake).triage("x")
        self.assertAlmostEqual(ob.month_spend(self.home, NOV).usd, ob.UNKNOWN_CALL_USD)

    def test_an_http_error_is_a_helper_error_that_never_carries_the_key(self):
        fake = FakeTransport(status=401)
        with self.assertRaises(oh.HelperError) as caught:
            self.helper(fake).triage("x")
        self.assertIn("401", str(caught.exception))
        self.assertNotIn(KEY, str(caught.exception))


class Cap(HelperCase):
    def test_the_helper_refuses_at_the_cap_and_sends_nothing(self):
        self.spend_to(90.0)
        fake = FakeTransport('{"difficulty": "easy", "ready": true}')
        with self.assertRaises(oh.Refused) as caught:
            self.helper(fake).triage("x")
        self.assertIn("cap", str(caught.exception))
        self.assertEqual(fake.requests, [])

    def test_a_call_whose_worst_case_would_cross_the_cap_is_refused(self):
        self.spend_to(89.9999)
        fake = FakeTransport('{"difficulty": "easy", "ready": true}')
        with self.assertRaises(oh.Refused):
            self.helper(fake).triage("x" * 4000)
        self.assertEqual(fake.requests, [])

    def test_the_next_month_the_helper_answers_again(self):
        self.spend_to(95.0)
        fake = FakeTransport('{"difficulty": "hard", "ready": false}')
        self.assertEqual(self.helper(fake, now=DEC).triage("x")["difficulty"], "hard")

    def test_a_lowered_cap_holds_the_helper_too(self):
        self.spend_to(10.0)
        fake = FakeTransport()
        with self.assertRaises(oh.Refused):
            self.helper(fake, env={"LOOM_OPENAI_CAP_USD": "10"}).triage("x")


class Advice(HelperCase):
    def test_advice_names_difficulty_lane_and_duplicate(self):
        fake = FakeTransport('{"difficulty": "easy", "ready": true, "reason": "small"}',
                             '{"lane": "spark", "reason": "local"}',
                             '{"duplicate_of": 7, "reason": "same ask"}')
        body = oh.advise(self.helper(fake), "Fix a typo", "In the README.", [(7, "README typo")],
                         {"easy": ("spark", "codex")})
        self.assertTrue(body.startswith(oh.ADVICE_MARKER))
        self.assertIn("Difficulty: easy", body)
        self.assertIn("Lane: spark", body)
        self.assertIn("Possible duplicate of #7", body)

    def test_advice_never_offers_a_review(self):
        self.assertNotIn("review", " ".join(oh.TASKS).lower())


if __name__ == "__main__":
    unittest.main()
