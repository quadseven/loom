"""bus/pace.py: each subscription's used share against its window's elapsed
share, and the slot the station asks for when one is behind (loom#3630)."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone

from bus import pace

# 2026-11-26T14:40:00Z
NOW = 1795704000.0
WEEK = 10080


def iso(at):
    return datetime.fromtimestamp(at, timezone.utc).isoformat()


def weekly(provider, lanes, used, elapsed_share, **extra):
    """A Usage reading for a weekly window ELAPSED_SHARE (0..1) through."""
    reset = NOW + (1 - elapsed_share) * WEEK * 60
    row = {"provider": provider, "lanes": lanes, "window": "weekly", "used_pct": used,
           "window_minutes": WEEK, "resets_at": iso(reset), "observed_at": iso(NOW - 60),
           "as_of": iso(NOW)}
    row.update(extra)
    return row


CODEX = ["codex"] + ["codex-%d" % n for n in range(2, 17)]


class VerdictTest(unittest.TestCase):
    def test_used_share_against_elapsed_share_with_the_slack(self):
        self.assertEqual(pace.verdict(0, 5, 10), pace.ON)
        self.assertEqual(pace.verdict(0, 50, 10), pace.BEHIND)
        self.assertEqual(pace.verdict(81, 81, 10), pace.ON)
        self.assertEqual(pace.verdict(60, 20, 10), pace.AHEAD)
        self.assertEqual(pace.verdict(10, 20, 10), pace.ON)
        self.assertEqual(pace.verdict(9.9, 20, 10), pace.BEHIND)

    def test_elapsed_share_of_a_window(self):
        reset = NOW + 0.25 * WEEK * 60
        self.assertAlmostEqual(pace.elapsed_pct(reset, WEEK, NOW), 75.0)
        self.assertIsNone(pace.elapsed_pct(NOW - 1, WEEK, NOW))
        self.assertIsNone(pace.elapsed_pct(NOW + 2 * WEEK * 60, WEEK, NOW))
        self.assertIsNone(pace.elapsed_pct(reset, None, NOW))

    def test_elapsed_share_rejects_unrepresentable_window_lengths(self):
        reset = NOW + WEEK * 60
        for minutes in (10 ** 400, 1e308, float("inf")):
            with self.subTest(minutes=minutes):
                self.assertIsNone(pace.elapsed_pct(reset, minutes, NOW))

    def test_slack_setting(self):
        self.assertEqual(pace.slack({}), 10.0)
        self.assertEqual(pace.slack({"LOOM_PACE_SLACK": "5"}), 5.0)
        self.assertEqual(pace.slack({"LOOM_PACE_SLACK": "junk"}), 10.0)
        self.assertEqual(pace.slack({"LOOM_PACE_SLACK": "-3"}), 10.0)


class WantedSlotsTest(unittest.TestCase):
    def test_behind_with_queued_work_opens_one_more_slot(self):
        self.assertEqual(pace.wanted(pace.BEHIND, queued=3, open_slots=4, maximum=16), 5)

    def test_ahead_opens_none(self):
        self.assertEqual(pace.wanted(pace.AHEAD, queued=3, open_slots=4, maximum=16), 4)

    def test_on_pace_opens_none(self):
        self.assertEqual(pace.wanted(pace.ON, queued=3, open_slots=4, maximum=16), 4)

    def test_behind_at_its_maximum_opens_none(self):
        self.assertEqual(pace.wanted(pace.BEHIND, queued=3, open_slots=16, maximum=16), 16)

    def test_behind_with_nothing_queued_opens_none(self):
        self.assertEqual(pace.wanted(pace.BEHIND, queued=0, open_slots=4, maximum=16), 4)


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        self.usage = os.path.join(self.home, "state", "usage")
        os.makedirs(self.usage)

    def beat(self, slot, age=0):
        with open(os.path.join(self.usage, slot + ".json"), "w") as fh:
            json.dump({"lane": slot, "at": NOW - age, "available": True, "reason": ""}, fh)

    def test_codex_behind_with_queued_work_asks_for_a_fifth_slot(self):
        for slot in CODEX[:4]:
            self.beat(slot)
        self.beat("codex-5", age=86400)  # not running in a pod
        rows = [weekly("codex", CODEX, 0, 0.5),
                {"provider": "codex", "lanes": CODEX, "window": "5-hour", "used_pct": 90,
                 "window_minutes": 300, "resets_at": iso(NOW + 60), "observed_at": iso(NOW)}]
        plan = pace.plan(self.home, rows, queued=2, env={}, now=NOW)
        codex = plan["providers"]["codex"]
        self.assertEqual((codex["window"], codex["used"], codex["elapsed"], codex["verdict"]),
                         ("weekly", 0, 50.0, pace.BEHIND))
        self.assertEqual((codex["open"], codex["max"], codex["want"]), (4, 16, 5))

    def test_ahead_asks_for_no_slot(self):
        self.beat("claude")
        plan = pace.plan(self.home, [weekly("claude", ["claude"], 90, 0.5)], queued=5, env={}, now=NOW)
        claude = plan["providers"]["claude"]
        self.assertEqual(claude["verdict"], pace.AHEAD)
        self.assertEqual(claude["want"], claude["open"])

    def test_a_configured_maximum_caps_the_request(self):
        for slot in CODEX[:4]:
            self.beat(slot)
        plan = pace.plan(self.home, [weekly("codex", CODEX, 0, 0.5)], queued=2,
                         env={"LOOM_PACE_MAX_CODEX": "4"}, now=NOW)
        self.assertEqual((plan["providers"]["codex"]["max"], plan["providers"]["codex"]["want"]), (4, 4))

    def test_a_stale_or_reset_reading_gives_no_verdict(self):
        reset_since = weekly("codex", CODEX, 40, 0.5, resets_at=iso(NOW - 60))
        old = weekly("claude", ["claude"], 40, 0.5, observed_at=iso(NOW - 3 * 86400))
        plan = pace.plan(self.home, [reset_since, old], queued=2, env={}, now=NOW)
        self.assertEqual(plan["providers"], {})

    def test_a_reading_without_a_window_length_or_figure_is_skipped(self):
        rows = [{"provider": "zai", "lanes": ["zai"], "window": "all", "used_pct": None},
                weekly("codex", CODEX, 10, 0.5, window_minutes=None)]
        self.assertEqual(pace.plan(self.home, rows, queued=1, env={}, now=NOW)["providers"], {})

    def test_the_plan_is_written_and_read_back(self):
        for slot in CODEX[:4]:
            self.beat(slot)
        plan = pace.plan(self.home, [weekly("codex", CODEX, 3, 0.4)], queued=1, env={}, now=NOW)
        pace.write(self.home, plan)
        self.assertEqual(pace.read(self.home), plan)

    def test_a_missing_plan_reads_as_empty(self):
        self.assertEqual(pace.read(self.home), {})


class QueuedTest(unittest.TestCase):
    def test_only_tickets_waiting_for_a_free_lane_count(self):
        key = lambda n: "%s#%d" % ("example/app", n)  # noqa: E731
        waits = {key(1): {"reason": "no free lane"},
                 key(2): {"reason": "no free lane takes model gpt-x"},
                 key(3): {"reason": "waits for its blocker"},
                 key(4): "junk"}
        self.assertEqual(pace.queued(waits), 2)
        self.assertEqual(pace.queued(None), 0)


if __name__ == "__main__":
    unittest.main()
