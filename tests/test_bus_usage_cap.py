"""bus/usage_cap.py: a claude slot keeps to a share of its weekly allowance
(loom#3660), by the Go plan's stop and pace rule (bus/model_pick.py)."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from bus import model_pick, usage_cap

NOW = 1792324800.0
DAY = 86400.0


def meter(percent, reset, at=NOW, window="weekly"):
    return {"at": at, "source": "Claude CLI", "windows": [
        {"window": "5-hour", "window_minutes": 300, "unit": "percent", "used_pct": 10.0,
         "resets_at": NOW + 3600, "at": at},
        {"window": window, "window_minutes": 10080, "unit": "percent", "used_pct": percent,
         "resets_at": reset, "at": at}]}


def allowed(stop, slack, reset, now=NOW):
    gone = 1 - (reset - now) / model_pick.PACED["weekly"]
    return stop * gone + slack


class CheckTest(unittest.TestCase):
    def test_usage_at_the_stop_holds_until_the_weekly_reset(self):
        got = usage_cap.check(meter(94.0, NOW + 8 * 3600), NOW, 90.0, 5.0)
        self.assertEqual(got.until, NOW + 8 * 3600)
        self.assertIn("weekly usage 94% is at its 90% stop", got.note)

    def test_usage_ahead_of_pace_holds_until_the_pace_catches_up(self):
        reset = NOW + 5 * DAY
        percent = allowed(90.0, 5.0, reset) + 10
        got = usage_cap.check(meter(percent, reset), NOW, 90.0, 5.0)
        self.assertIn("ahead of its", got.note)
        self.assertGreater(got.until, NOW + model_pick.RETRY_NOTHING)
        self.assertLess(got.until, reset)
        # At the time it names, the pace allows that usage; a minute before, not.
        self.assertIsNone(usage_cap.check(meter(percent, reset, at=got.until), got.until + 1,
                                          90.0, 5.0).until)
        self.assertIsNotNone(usage_cap.check(meter(percent, reset), got.until - 60,
                                             90.0, 5.0).until)

    def test_usage_under_pace_runs_and_names_the_verdict(self):
        reset = NOW + 2 * DAY
        got = usage_cap.check(meter(allowed(90.0, 5.0, reset) - 5, reset), NOW, 90.0, 5.0)
        self.assertIsNone(got.until)
        self.assertIn("is within its", got.note)
        self.assertIn("to a 90% stop", got.note)

    def test_a_lower_stop_holds_where_the_default_runs(self):
        reset = NOW + DAY
        record = meter(70.0, reset)
        self.assertIsNone(usage_cap.check(record, NOW, 90.0, 5.0).until)
        held = usage_cap.check(record, NOW, 60.0, 5.0)
        self.assertEqual(held.until, reset)
        self.assertIn("at its 60% stop", held.note)

    def test_the_slack_moves_the_pace(self):
        reset = NOW + 4 * DAY
        percent = allowed(90.0, 5.0, reset) + 3
        self.assertIsNotNone(usage_cap.check(meter(percent, reset), NOW, 90.0, 5.0).until)
        self.assertIsNone(usage_cap.check(meter(percent, reset), NOW, 90.0, 10.0).until)

    def test_no_reading_lets_the_run_go_and_record_one(self):
        for record in (None, {}, {"windows": "x"}, meter(50.0, NOW + DAY, window="5-hour"),
                       meter("x", NOW + DAY), meter(50.0, None)):
            with self.subTest(record=record):
                got = usage_cap.check(record, NOW, 90.0, 5.0)
                self.assertIsNone(got.until)
                self.assertIn("this run records one", got.note)

    def test_a_reading_from_a_window_that_already_reset_does_not_hold(self):
        got = usage_cap.check(meter(99.0, NOW - 60, at=NOW - 2 * DAY), NOW, 90.0, 5.0)
        self.assertIsNone(got.until)

    def test_an_old_reading_still_holds_as_a_floor_and_never_past_the_reset(self):
        # Use within a window never falls, so an hour-old 94% is at least 94%.
        reset = NOW + 3 * 3600
        got = usage_cap.check(meter(94.0, reset, at=NOW - 3600), NOW, 90.0, 5.0)
        self.assertEqual(got.until, reset)
        self.assertIn("read 60 min ago", got.note)
        # Past the reset the same reading holds nothing: the lane is never
        # wedged by a meter no run has refreshed.
        self.assertIsNone(usage_cap.check(meter(94.0, reset, at=NOW - 3600), reset + 1,
                                          90.0, 5.0).until)

    def test_a_setting_that_is_not_a_number_holds_the_slot(self):
        got = usage_cap.check(meter(10.0, NOW + DAY), NOW, None, 5.0)
        self.assertEqual(got.until, NOW + model_pick.RETRY_NOTHING)
        self.assertIn("not a number", got.note)


class SettingTest(unittest.TestCase):
    def test_unset_is_the_default_and_a_bad_value_is_none(self):
        self.assertEqual(usage_cap.setting("", 90.0, 1, 100), 90.0)
        self.assertEqual(usage_cap.setting(" 60 ", 90.0, 1, 100), 60.0)
        for bad in ("6O", "0", "101", "-5", "nan%"):
            with self.subTest(bad=bad):
                self.assertIsNone(usage_cap.setting(bad, 90.0, 1, 100))


class MainTest(unittest.TestCase):
    def run_main(self, record, stop="", slack=""):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "claude.meter.json"
            if record is not None:
                path.write_text(json.dumps(record))
            out = io.StringIO()
            with redirect_stdout(out):
                code = usage_cap.main(["check", str(path), stop, slack], now=NOW)
        return code, out.getvalue().strip()

    def test_a_hold_prints_its_time_and_reason_and_exits_3(self):
        code, out = self.run_main(meter(94.0, NOW + 3600))
        self.assertEqual(code, 3)
        self.assertEqual(out, "%d\tweekly usage 94%% is at its 90%% stop" % (NOW + 3600))

    def test_a_free_slot_prints_the_verdict_and_exits_0(self):
        code, out = self.run_main(None)
        self.assertEqual((code, out), (0, "no current weekly reading; this run records one"))

    def test_the_stop_setting_reaches_the_check(self):
        self.assertEqual(self.run_main(meter(70.0, NOW + DAY))[0], 0)
        self.assertEqual(self.run_main(meter(70.0, NOW + DAY), stop="60")[0], 3)

    def test_wrong_arguments_exit_2(self):
        self.assertEqual(usage_cap.main(["check"], now=NOW), 2)


if __name__ == "__main__":
    unittest.main()
