"""Report deadlines leave time for work and retain unposted goal reports."""
import io
import json
import sys
from contextlib import redirect_stdout
from unittest import mock
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bus import brain, journal
from bus.github import GitHub
from test_tick_clock import held_lock, clock
from test_bus import REPO as LOCK_REPO
from test_journal_tidy import Fake

T = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
REPO = 'example/project'
OWED = ['%s#%d' % (REPO, n) for n in (1, 2, 3)]


class ReportBudgetTests(unittest.TestCase):
    def test_three_reports_stop_at_cap_and_offer_non_report_work(self):
        record = {'started_at': T.strftime('%Y-%m-%dT%H:%M:%SZ'),
                  'tick_minutes': 7, 'holder': 'codex'}
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'reports.json'
            first = brain.report_plan(state, record, OWED, T)
            self.assertEqual(first['report'], OWED[0])
            self.assertEqual(first['seconds_left'], 60)
            after = brain.report_plan(state, record, [], T + timedelta(seconds=60),
                                      done=OWED[0])
            self.assertIsNone(after['report'])
            self.assertEqual(after['deferred'], 2)
            self.assertEqual(after['work'][0]['step'], 'verify')
            self.assertGreater(after['work'][0]['seconds'], 0)
            self.assertEqual(json.loads(state.read_text())['pending'], OWED[1:])

    def record(self, start=T):
        return {'started_at': start.strftime('%Y-%m-%dT%H:%M:%SZ'),
                'tick_minutes': 7, 'holder': 'muse'}

    def test_all_four_work_steps_run_after_cap_before_stop(self):
        _, gh = held_lock()
        brain.tick_step(gh, LOCK_REPO, 'lock', holder='muse', now=T, tick_minutes=7)
        brain.tick_step(gh, LOCK_REPO, 'status', holder='muse', now=T)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'reports.json'
            brain.report_plan(path, self.record(), OWED, T)
            now = T + timedelta(seconds=60)
            plan = brain.report_plan(path, self.record(), [], now, done=OWED[0])
            self.assertEqual([slot['step'] for slot in plan['work']],
                             ['verify', 'brief', 'triage', 'sweep'])
            for slot in plan['work']:
                self.assertGreater(slot['seconds'], 0)
                saved = brain.tick_step(gh, LOCK_REPO, slot['step'], 'work attempted',
                                        holder='muse', now=now, sleep=lambda _: None)
                self.assertNotIn(slot['step'], brain.pending_steps(saved))
                now = datetime.fromisoformat(slot['until'])
                self.assertLess(now, T + timedelta(minutes=7))
            self.assertEqual(saved['sweep'], 'ok')

    def test_deferred_reports_post_on_following_tick_in_queue_order(self):
        fake = Fake()
        gh = GitHub('test-token', transport=fake)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'reports.json'
            plan = brain.report_plan(path, self.record(), OWED, T)
            report = journal.REPORT_MARKER + '\nWork advanced.\nNeeds you: nothing'
            journal.post(gh, REPO, 1, report, now=T)
            plan = brain.report_plan(path, self.record(), [], T + timedelta(seconds=60),
                                     done=plan['report'])
            self.assertIsNone(plan['report'])
            self.assertEqual(plan['pending'], OWED[1:])
            later = T + timedelta(minutes=30)
            # A fresh tick and a partial status read must not erase the queue.
            plan = brain.report_plan(path, self.record(later), [], later)
            for number in (2, 3):
                self.assertEqual(plan['report'], OWED[number - 1])
                journal.post(gh, REPO, number, report, now=later)
                later += timedelta(seconds=10)
                plan = brain.report_plan(path, self.record(T + timedelta(minutes=30)),
                                         [], later, done=plan['report'])
            self.assertEqual(plan['pending'], [])
            posted = [call[1] for call in fake.calls if call[0] == 'POST']
            self.assertEqual(posted, ['/repos/%s/issues/%d/comments' % (REPO, n)
                                      for n in (1, 2, 3)])

    def test_unacknowledged_report_survives_restart_and_cap_does_not_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'reports.json'
            first = brain.report_plan(path, self.record(), OWED, T)
            again = brain.report_plan(path, self.record(), OWED, T + timedelta(seconds=50))
            self.assertEqual(again['report_until'], first['report_until'])
            self.assertEqual(again['seconds_left'], 10)
            later = T + timedelta(minutes=30)
            retry = brain.report_plan(path, self.record(later), [], later)
            self.assertEqual(retry['pending'], OWED)
            self.assertEqual(retry['report'], OWED[0])

    def test_deferred_count_is_preserved_in_posted_tick_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'reports.json'
            brain.report_plan(path, self.record(), OWED, T)
            plan = brain.report_plan(path, self.record(), [], T + timedelta(seconds=60),
                                     done=OWED[0])
            fake = Fake()
            text = journal.JOURNAL_MARKER + '\n' + plan['journal_line']
            journal.post(GitHub('test-token', transport=fake), REPO, 7, text,
                         holder='muse', now=T + timedelta(minutes=2))
            self.assertIn('- Reports deferred: 2', fake.written())

    def test_command_reads_real_tick_and_prints_the_schedule(self):
        _, gh = held_lock()
        brain.tick_step(gh, LOCK_REPO, 'lock', holder='muse', now=T, tick_minutes=7)
        with tempfile.TemporaryDirectory() as directory:
            args = brain.parser().parse_args(['report-plan', '--state',
                    str(Path(directory) / 'reports.json'), '--holder', 'muse',
                    '--lock-repo', LOCK_REPO, '--owed', *OWED])
            out = io.StringIO()
            with mock.patch.object(brain, 'datetime', clock(T)), redirect_stdout(out):
                brain.COMMANDS[args.command](gh, args)
            result = json.loads(out.getvalue())
            self.assertEqual(result['report'], OWED[0])
            self.assertEqual(result['seconds_left'], 60)


if __name__ == '__main__':
    unittest.main()
