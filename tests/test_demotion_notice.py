"""Demotions notify once and remain visible in report digests."""
import json
import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_bus_station import Base
from bus import scorecard, station
from bus.github import GitHub


class DemotionNoticeTest(Base):
    def runner(self, now=2000000000):
        self.settings.factory_on = True
        return station.Station(GitHub("token", transport=self.fake), self.settings, now=now)

    def seed(self, runner):
        card = scorecard.Scorecard(runner.state, runner.now)
        card.data["demoted"] = {"codex": {
            "at": runner.now, "why": "3 bad-work findings", "kind": "bad-outcomes"}}
        card.data["refreshed"] = runner.now
        card.save()

    def test_notice_sent_once_across_passes_and_restart(self):
        runner = self.runner()
        self.seed(runner)
        with mock.patch.object(station.Station, "_digest_send", return_value=True) as send:
            runner.score_lanes()
            runner.score_lanes()
            self.runner(runner.now + 60).score_lanes()
        self.assertEqual(send.call_count, 1)
        line = send.call_args.args[0]
        self.assertIn("needs-a-person", line)
        self.assertIn("codex", line)
        self.assertIn("3 bad-work findings", line)
        self.assertIn("python3 -m bus.scorecard clear codex", line)

    def test_failed_notice_is_retried_without_waiting_for_score_refresh(self):
        runner = self.runner()
        self.seed(runner)
        with mock.patch.object(station.Station, "_digest_send", side_effect=[False, True]) as send:
            runner.score_lanes()
            self.runner(runner.now + 60).score_lanes()
        self.assertEqual(send.call_count, 2)

    def test_pending_notice_goes_out_when_the_refresh_carries_over(self):
        runner = self.runner()
        self.seed(runner)
        card = scorecard.Scorecard(runner.state, runner.now)
        card.data["refreshed"] = 0
        card.save()
        def carried(*args):
            raise station.TargetReached("other read carried over at request target")
        with mock.patch.object(station.Station, "_refresh_scorecard", carried), \
                mock.patch.object(station.Station, "_digest_send", return_value=True) as send:
            with self.assertRaises(station.TargetReached):
                runner.score_lanes()
            with self.assertRaises(station.TargetReached):
                self.runner(runner.now + 60).score_lanes()
        self.assertEqual(send.call_count, 1)
        self.assertIn("python3 -m bus.scorecard clear codex", send.call_args.args[0])

    def test_refresh_posts_new_demotion_and_clear_allows_one_new_notice(self):
        runner = self.runner()
        def refresh(card, *args):
            card.data["records"] = {str(n): {
                "lane": "codex", "model": "test", "at": runner.now,
                "review": "bad-work", "merged": True} for n in range(3)}
            card.demote()
            card.data["refreshed"] = runner.now
        with mock.patch.object(scorecard.Scorecard, "refresh", refresh), \
                mock.patch.object(station.audit, "held", return_value={}), \
                mock.patch.object(station.Station, "_digest_send", return_value=True) as send:
            runner.score_lanes()
            self.assertEqual(send.call_count, 1)
            card = scorecard.Scorecard(runner.state, runner.now)
            card.clear("codex")
            card.save()
            runner.score_lanes()
            self.assertEqual(send.call_count, 1)
            # A separate later demotion carries no old delivery marker.
            card.data["demoted"]["codex"] = {"at": runner.now + 60, "why": "new findings"}
            card.save()
            runner.score_lanes()
            runner.score_lanes()
            self.assertEqual(send.call_count, 2)

    def test_factory_off_never_sends_pending_notice(self):
        runner = self.runner()
        self.seed(runner)
        runner.s.factory_on = False
        with mock.patch.object(runner, "_digest_send") as send:
            runner.score_lanes()
        send.assert_not_called()

    def test_unreadable_scorecard_is_visible_without_overwriting_or_clear_command(self):
        runner = self.runner()
        path = Path(runner.state) / scorecard.FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("broken")
        lines = scorecard.Scorecard(runner.state, runner.now).demotion_lines()
        self.assertIn("cannot be read", str(lines))
        self.assertNotIn("clear", str(lines))
        self.assertEqual(path.read_text(), "broken")

    def test_model_notice_quotes_exact_clear_argument(self):
        import shlex
        runner = self.runner()
        card = scorecard.Scorecard(runner.state, runner.now)
        model = "model:provider/model name"
        card.data["demoted"][model] = {"at": runner.now, "why": "3 bad findings"}
        line = card.demotion_lines()[model]
        command = line.split("`", 2)[1]
        self.assertEqual(shlex.split(command), ["python3", "-m", "bus.scorecard", "clear", model])

    def test_clear_during_delivery_is_not_resurrected(self):
        runner = self.runner()
        self.seed(runner)
        def delivered(_):
            current = scorecard.Scorecard(runner.state, runner.now + 1)
            current.clear("codex")
            current.save()
            return True
        with mock.patch.object(runner, "_digest_send", side_effect=delivered):
            runner.score_lanes()
        self.assertEqual(scorecard.Scorecard(runner.state, runner.now).demoted(), {})

    def test_replacement_during_delivery_remains_new_and_unnotified(self):
        runner = self.runner()
        self.seed(runner)
        def delivered(_):
            current = scorecard.Scorecard(runner.state, runner.now + 1)
            current.data["demoted"]["codex"] = {"at": runner.now + 1, "why": "new findings"}
            current.save()
            return True
        with mock.patch.object(runner, "_digest_send", side_effect=delivered):
            runner.score_lanes()
        current = scorecard.Scorecard(runner.state, runner.now + 2)
        self.assertEqual(current.demoted(), {"codex": "new findings"})
        self.assertIn("codex", current.demotion_lines(pending_only=True))

    def test_corrupt_receipts_refuse_delivery_without_erasing_the_ledger(self):
        runner = self.runner()
        self.seed(runner)
        path = Path(runner.state) / scorecard.NOTICE_FILE
        path.write_text("broken")
        with mock.patch.object(runner, "_digest_send") as send:
            with self.assertRaisesRegex(ValueError, "notice receipts cannot be read"):
                runner.score_lanes()
        send.assert_not_called()
        self.assertEqual(path.read_text(), "broken")

    def digest(self, runner, slot):
        path = os.path.join(runner.state, "reports.json")
        Path(path).write_text(json.dumps({"_digest_slot": "prior", "_digest_since": runner.now - 60}))
        with mock.patch.object(runner, "_due_digest", return_value=(slot, "morning")), \
                mock.patch.object(runner, "_digest_lines", return_value=([], 0, 0)), \
                mock.patch.object(runner, "_digest_backlog_lines", return_value=[]), \
                mock.patch.object(runner, "_digest_factory_line", return_value=""), \
                mock.patch.object(runner, "_digest_shadow_lines", return_value=[]), \
                mock.patch.object(runner, "_digest_send", return_value=True) as send:
            runner.send_report_digest()
        return send.call_args.args[0]

    def test_next_digest_and_every_overdue_digest_include_uncleared_demotion(self):
        runner = self.runner()
        self.seed(runner)
        self.assertIn("3 bad-work findings", self.digest(runner, "first"))
        for days in [1, 2, 7]:
            later = self.runner(runner.now + days * 86400 + 1)
            self.assertIn("python3 -m bus.scorecard clear codex", self.digest(later, str(days)))
        card = scorecard.Scorecard(runner.state, runner.now)
        card.clear("codex")
        card.save()
        self.assertNotIn("3 bad-work findings", self.digest(runner, "cleared"))
