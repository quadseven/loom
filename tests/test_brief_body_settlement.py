"""A verified ask removal settles new stale evidence only for the same body."""
import hashlib
import unittest

from bus import brief_check
from bus.github import GitHub
from tests.test_brief_check import (REPO, TRUSTED, OPERATOR, STALE_REWRITE,
                                    Route, ticket, comment, iso)


class BodySettlementTests(unittest.TestCase):
    def setUp(self):
        self.issue = ticket()
        digest = hashlib.sha256(STALE_REWRITE.encode()).hexdigest()
        self.notes = [comment('<!-- loom:stale-brief comment-99 -->\n'
                              '<!-- loom:stale-body %s -->' % digest,
                              login='loom-station[bot]', bot=True, ago=120)]
        self.event = {'event': 'unlabeled', 'label': {'name': 'needs:human'},
                      'actor': {'login': OPERATOR, 'type': 'User'},
                      'created_at': iso(60)}
        self.path = '/repos/%s/issues/206/events' % REPO
        self.fake = Route({self.path: [self.event],
                           '/repos/%s/issues/124' % REPO:
                               {'state': 'closed', 'state_reason': 'completed'},
                           '/user': {'login': 'loom-station[bot]'}})

    def check(self):
        return brief_check.check(GitHub('t', transport=self.fake), REPO,
                                 self.issue, self.notes, TRUSTED, 'main')

    def test_new_evidence_does_not_reask_unchanged_settled_body(self):
        self.assertIsNone(self.check())
        self.assertFalse(any('/issues/124' in p for p in self.fake.reads))

    def test_changed_body_can_raise_new_evidence(self):
        self.issue['body'] += '\nNew requirement.\n'
        self.assertEqual(self.check().key, 'source-124')

    def test_bot_removal_is_not_an_operator_decision(self):
        self.event['actor'] = {'login': 'loom-station[bot]', 'type': 'Bot'}
        self.assertEqual(self.check().key, 'source-124')

    def test_untrusted_person_removal_does_not_settle(self):
        self.event['actor'] = {'login': 'another-person', 'type': 'User'}
        self.assertEqual(self.check().key, 'source-124')

    def test_removal_before_the_ask_does_not_settle(self):
        self.event['created_at'] = iso(180)
        self.assertEqual(self.check().key, 'source-124')

    def test_a_current_person_hold_is_never_cleared(self):
        self.issue['labels'].append({'name': 'needs:human'})
        self.assertEqual(self.check().key, 'source-124')

    def test_event_read_failure_is_not_a_settlement(self):
        self.fake.fail.add(self.path)
        with self.assertRaises(brief_check.Unread):
            self.check()

    def test_ask_records_the_exact_body_fingerprint(self):
        verdict = brief_check.Verdict(brief_check.STALE, 'source-124', 'done')
        got = brief_check.ask(verdict, self.issue['body'])
        self.assertIn(hashlib.sha256(self.issue['body'].encode()).hexdigest(), got)

    def test_automation_using_a_person_login_does_not_settle(self):
        self.fake.routes['/user'] = {'login': OPERATOR}
        self.assertEqual(self.check().key, 'source-124')

    def test_a_newer_ask_requires_a_new_removal(self):
        self.notes.append(comment(self.notes[0]['body'], login='loom-station[bot]',
                                  bot=True, ago=30, ident=2))
        self.assertEqual(self.check().key, 'source-124')

    def test_a_later_bot_removal_does_not_reuse_an_old_person_event(self):
        self.fake.routes[self.path].append({**self.event, 'created_at': iso(10),
                                           'actor': {'login': 'worker[bot]', 'type': 'Bot'}})
        self.assertEqual(self.check().key, 'source-124')

    def test_legacy_ask_never_causes_an_event_read_or_a_body_waiver(self):
        self.notes[0]['body'] = '<!-- loom:stale-brief comment-99 -->'
        self.assertEqual(self.check().key, 'source-124')
        self.assertFalse(any('/events' in p for p in self.fake.reads))

    def test_station_asks_persist_the_body_for_later_settlement(self):
        import tempfile
        from bus import station
        with tempfile.TemporaryDirectory() as home:
            fake = Route({'/repos/%s/issues/206' % REPO: self.issue,
                          '/repos/%s/issues/206/comments' % REPO: []})
            settings = station.Settings(home, [REPO], TRUSTED, ['example-owner'])
            worker = station.Station(GitHub('t', transport=fake), settings)
            found = brief_check.Verdict(brief_check.STALE, 'source-124', 'already done')
            self.assertTrue(worker._apply_brief_hold(REPO, 206, found, self.issue['body']))
            bodies = [str(payload) for method, path, payload in fake.writes
                      if path.endswith('/comments')]
            self.assertTrue(any(hashlib.sha256(self.issue['body'].encode()).hexdigest()
                                in body for body in bodies))
