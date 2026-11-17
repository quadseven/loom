"""The OpenAI spend ledger and its monthly cap (bus/openai_budget.py, loom#3188).

Every call the openai-mini lane or the decision helper makes is priced from
the response's own token usage and a pinned price table, and both refuse a
call once the calendar month's ledger reaches the cap (90 USD by default).
"""
import calendar
import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

from bus import openai_budget as ob

# 2026-11-15 12:00 UTC, and a moment in the next month.
NOV = calendar.timegm((2026, 11, 15, 12, 0, 0))
DEC = calendar.timegm((2026, 12, 1, 0, 0, 1))
# One dollar per million tokens of each kind keeps the arithmetic plain.
FLAT = {"flat-model": ob.Price(input=1.0, cached=0.5, output=1.0,
                               long_input=4.0, long_cached=2.0, long_output=8.0)}


class Pricing(unittest.TestCase):
    def test_the_pinned_table_names_its_source_and_the_day_it_was_read(self):
        self.assertTrue(ob.PRICES_SOURCE.startswith("https://developers.openai.com/"))
        self.assertRegex(ob.PRICES_READ, r"^\d{4}-\d{2}-\d{2}$")
        luna = ob.PRICES["gpt-6-luna"]
        self.assertEqual((luna.input, luna.cached, luna.output), (0.10, 0.01, 0.50))

    def test_cost_of_a_call_from_its_uncached_cached_and_output_tokens(self):
        usage = ob.Usage(input=100_000, cached=100_000, output=500_000)
        self.assertAlmostEqual(ob.cost("flat-model", usage, FLAT), 0.1 + 0.05 + 0.5)

    def test_the_lane_model_name_with_its_provider_prefix_prices_the_same(self):
        usage = ob.Usage(input=200_000, cached=0, output=1_000_000)
        self.assertAlmostEqual(ob.cost("openai/gpt-6-luna", usage), 0.02 + 0.50)

    def test_a_prompt_past_the_long_context_line_pays_the_long_rates(self):
        usage = ob.Usage(input=ob.LONG_CONTEXT_OVER, cached=1, output=1_000_000)
        expected = (ob.LONG_CONTEXT_OVER * 4.0 + 1 * 2.0) / 1e6 + 8.0
        self.assertAlmostEqual(ob.cost("flat-model", usage, FLAT), expected)

    def test_an_unpriced_model_has_no_cost(self):
        with self.assertRaises(ob.Unpriced):
            ob.cost("gpt-6-astra", ob.Usage(1, 0, 1))

    def test_chat_completions_usage_counts_cached_prompt_tokens_apart(self):
        usage = ob.Usage.from_openai({"prompt_tokens": 1000, "completion_tokens": 50,
                                      "prompt_tokens_details": {"cached_tokens": 400}})
        self.assertEqual(usage, ob.Usage(input=600, cached=400, output=50))

    def test_responses_api_usage_reads_the_same(self):
        usage = ob.Usage.from_openai({"input_tokens": 10, "output_tokens": 7,
                                      "input_tokens_details": {"cached_tokens": 3}})
        self.assertEqual(usage, ob.Usage(input=7, cached=3, output=7))

    def test_opencode_step_tokens_charge_reasoning_and_cache_writes(self):
        # A cache write is billed as input, and reasoning as output: never
        # under the real bill.
        usage = ob.Usage.from_opencode({"input": 100, "output": 20, "reasoning": 30,
                                        "cache": {"read": 400, "write": 5}})
        self.assertEqual(usage, ob.Usage(input=105, cached=400, output=50))

    def test_usage_with_no_token_counts_is_unknown(self):
        for bad in (None, {}, {"prompt_tokens": "many"}, {"prompt_tokens": -1, "completion_tokens": 1}):
            with self.assertRaises(ob.UsageUnknown):
                ob.Usage.from_openai(bad)
        with self.assertRaises(ob.UsageUnknown):
            ob.Usage.from_opencode({})


class Ledger(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()

    def record(self, usd_tokens, now=NOV, source="lane"):
        # Output tokens at one dollar a million: the tokens are the micro-dollars.
        return ob.record(self.home, source, "flat-model", ob.Usage(0, 0, usd_tokens),
                         now, slot="openai-mini", ticket="T1", prices=FLAT)

    def test_each_call_is_one_ledger_line_with_its_tokens_and_cost(self):
        line = self.record(2_000_000)
        self.assertEqual(line["cost_usd"], 2.0)
        self.assertEqual(line["month"], "2026-11")
        [stored] = [json.loads(x) for x in open(ob.ledger_path(self.home))]
        self.assertEqual(stored, line)
        self.assertEqual((stored["output_tokens"], stored["source"]), (2_000_000, "lane"))

    def test_month_spend_sums_the_calendar_month_only(self):
        self.record(30_000_000, now=calendar.timegm((2026, 10, 31, 23, 59, 59)))
        self.record(5_000_000)
        self.record(1_500_000, source="helper")
        spend = ob.month_spend(self.home, NOV)
        self.assertAlmostEqual(spend.usd, 6.5)
        self.assertEqual(spend.calls, 2)

    def test_the_cap_refuses_at_ninety_dollars_with_a_fake_clock_and_price(self):
        self.record(89_990_000)
        self.assertIsNone(ob.refusal(self.home, {}, NOV, "flat-model", FLAT))
        self.record(10_000)
        why = ob.refusal(self.home, {}, NOV, "flat-model", FLAT)
        self.assertIn("90.00", why)
        self.assertIn("2026-11", why)

    def test_the_month_rolls_over_and_the_cap_lifts(self):
        self.record(95_000_000)
        self.assertIsNotNone(ob.refusal(self.home, {}, NOV, "flat-model", FLAT))
        self.assertIsNone(ob.refusal(self.home, {}, DEC, "flat-model", FLAT))
        self.assertEqual(ob.month_spend(self.home, DEC).usd, 0.0)

    def test_a_configured_cap_may_lower_the_ceiling_never_raise_it(self):
        self.record(50_000_000)
        self.assertIsNotNone(ob.refusal(self.home, {"LOOM_OPENAI_CAP_USD": "40"}, NOV, "flat-model", FLAT))
        self.record(45_000_000)
        self.assertIsNotNone(ob.refusal(self.home, {"LOOM_OPENAI_CAP_USD": "500"}, NOV, "flat-model", FLAT))
        self.assertEqual(ob.cap_usd({"LOOM_OPENAI_CAP_USD": "500"}), ob.CAP_USD)

    def test_a_cap_that_does_not_parse_refuses_every_call(self):
        why = ob.refusal(self.home, {"LOOM_OPENAI_CAP_USD": "lots"}, NOV, "flat-model", FLAT)
        self.assertIn("LOOM_OPENAI_CAP_USD", why)

    def test_an_unpriced_model_is_refused(self):
        why = ob.refusal(self.home, {}, NOV, "gpt-6-astra")
        self.assertIn("no pinned price", why)

    def test_a_worst_case_call_that_would_cross_the_cap_is_refused(self):
        self.record(89_000_000)
        self.assertIsNone(ob.refusal(self.home, {}, NOV, "flat-model", FLAT, worst_usd=0.5))
        self.assertIsNotNone(ob.refusal(self.home, {}, NOV, "flat-model", FLAT, worst_usd=1.5))

    def test_a_call_of_unknown_usage_is_charged_the_pessimistic_amount(self):
        line = ob.record_unknown(self.home, "lane", "flat-model", NOV, slot="s", ticket="T")
        self.assertEqual(line["cost_usd"], ob.UNKNOWN_CALL_USD)
        self.assertTrue(line["estimated"])
        self.assertAlmostEqual(ob.month_spend(self.home, NOV).usd, ob.UNKNOWN_CALL_USD)

    def test_a_torn_or_foreign_line_is_skipped_not_fatal(self):
        os.makedirs(os.path.dirname(ob.ledger_path(self.home)))
        with open(ob.ledger_path(self.home), "w") as fh:
            fh.write('{"month": "2026-11", "cost_usd": 1.25}\nnot json\n{"month": "2026-11"\n'
                     '{"month": "2026-11", "cost_usd": "x"}\n')
        self.assertAlmostEqual(ob.month_spend(self.home, NOV).usd, 1.25)

    def test_status_line_names_the_month_spend_cap_and_calls(self):
        self.record(12_340_000)
        line = ob.month_spend(self.home, NOV).line(ob.CAP_USD)
        self.assertEqual(line, "OpenAI spend 2026-11: $12.34 of the $90.00 cap, 1 call")


class Events(unittest.TestCase):
    """The lane's agent (opencode) prints one step_finish event per model
    call; each becomes one ledger line."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.events = os.path.join(self.home, "events")

    def write(self, *events):
        with open(self.events, "w") as fh:
            for e in events:
                fh.write((e if isinstance(e, str) else json.dumps(e)) + "\n")

    def test_each_step_finish_is_priced_and_recorded(self):
        self.write({"type": "step_start"},
                   {"type": "step_finish", "part": {"tokens": {"input": 200_000, "output": 0,
                                                               "reasoning": 0, "cache": {"read": 0}}}},
                   "a provider error line",
                   {"type": "step_finish", "part": {"tokens": {"input": 0, "output": 1_000_000,
                                                               "reasoning": 0, "cache": {"read": 0}}}})
        lines = ob.record_events(self.home, self.events, "openai/gpt-6-luna", "openai-mini", "T7", NOV)
        self.assertEqual([round(x["cost_usd"], 6) for x in lines], [0.02, 0.50])
        self.assertAlmostEqual(ob.month_spend(self.home, NOV).usd, 0.52)

    def test_a_step_without_tokens_is_charged_as_unknown(self):
        self.write({"type": "step_finish", "part": {}})
        [line] = ob.record_events(self.home, self.events, "openai/gpt-6-luna", "openai-mini", "T7", NOV)
        self.assertTrue(line["estimated"])

    def test_a_missing_events_file_records_nothing(self):
        self.assertEqual(ob.record_events(self.home, self.events + ".none", "openai/gpt-6-luna",
                                          "openai-mini", "T7", NOV), [])


class Cli(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()

    def run_cli(self, *argv, env=None):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"LOOM_HOME": self.home, **(env or {})}), redirect_stdout(out):
            code = ob.main(list(argv))
        return code, out.getvalue()

    def test_check_passes_under_the_cap_and_fails_with_the_reason_over_it(self):
        self.assertEqual(self.run_cli("check", "--model", "openai/gpt-6-luna")[0], 0)
        ob.record(self.home, "lane", "gpt-6-luna", ob.Usage(900_000_000, 0, 0), time.time())
        code, out = self.run_cli("check", "--model", "openai/gpt-6-luna")
        self.assertEqual(code, 1)
        self.assertIn("cap", out)

    def test_check_refuses_an_unpriced_model(self):
        code, out = self.run_cli("check", "--model", "openai/gpt-6-astra")
        self.assertEqual(code, 1)
        self.assertIn("no pinned price", out)

    def test_record_events_and_month(self):
        events = os.path.join(self.home, "ev")
        with open(events, "w") as fh:
            fh.write(json.dumps({"type": "step_finish", "part": {"tokens": {"output": 400_000}}}) + "\n")
        code, _ = self.run_cli("record-events", "--events", events, "--model", "openai/gpt-6-luna",
                               "--slot", "openai-mini", "--ticket", "T9")
        self.assertEqual(code, 0)
        code, out = self.run_cli("month")
        self.assertEqual(code, 0)
        self.assertIn("$0.20 of the $90.00 cap, 1 call", out)


if __name__ == "__main__":
    unittest.main()
