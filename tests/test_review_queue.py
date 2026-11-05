"""Operator review queue decisions preserve the existing merge authority."""
import unittest
from bus import answer, operator_hold


class ReviewChoices(unittest.TestCase):
    def test_held_pull_request_offers_send_back(self):
        offered = answer.options(operator_hold.ask(18, ["deployment review"]))
        self.assertEqual(offered.choices, ("Merge", "Hold", "Send back"))

    def test_send_back_supersedes_an_older_merge_verdict(self):
        ask = {"id": 1, "body": answer.HUMAN_MARKER + "\n" + operator_hold.ask(18, ["review"]),
               "user": {"login": "station-bot"}, "created_at": "2030-01-01T10:00:00Z"}
        def said(choice, number):
            return {"id": number, "body": answer.comment_body("operator-one", choice),
                    "user": {"login": "station-bot"}, "created_at": "2030-01-01T11:00:00Z"}
        result = answer.pr_verdict([ask, said("Merge", 2), said("Send back", 3)], 18,
                                   ["station-bot"], ["operator-one"])
        self.assertFalse(answer.is_merge(result))
        self.assertEqual(result.choice, "Send back")


class QueueRender(unittest.TestCase):
    def test_card_uses_the_api_pr_link_and_rejects_executable_schemes(self):
        from dashboard import review_queue
        entry = {'repo': 'example-org/example-repo', 'number': 7,
                 'comment_full': operator_hold.ask(18, ['review']), 'title': 'Review change'}
        pr = {'number': 18, 'state': 'open', 'head': 'a' * 40,
              'html_url': 'https://example.com/pull/18'}
        text = review_queue.card(entry, {'pr': pr}, 'off')
        self.assertIn('href="https://example.com/pull/18"', text)
        pr['html_url'] = 'javascript:alert(1)'
        self.assertNotIn('href=', review_queue.card(entry, {'pr': pr}, 'off'))

    def test_queue_exposes_hold_findings_criteria_and_unknown_proof(self):
        from dashboard import review_queue
        repo = "example-org/example-repo"
        entry = {"repo": repo, "number": 7, "title": "Deploy parser", "body_ask": "",
                 "comment_full": operator_hold.ask(18, ["review deployment"]),
                 "labels": ["loom:human"], "body": "## Acceptance criteria\n- [x] Unit contract\n- [ ] Live check"}
        detail = {"pr": {"number": 18, "title": "Parser change", "state": "open", "head": "a" * 40,
                         "tier": "deploy-config", "threads": [{"text": "Check rollback", "resolved": False}],
                         "reviews": [], "checks": []}, "errors": {}}
        rendered = review_queue.render([entry], {repo + "#7": detail}, mode="off")
        for text in ("deploy-config", "review deployment", "Check rollback", "Unit contract", "Live check", "Claimed", "Pending"):
            self.assertIn(text, rendered)
        self.assertNotIn("Verified", rendered)

    def test_missing_pr_detail_keeps_the_held_item_visible_without_actions(self):
        from dashboard import review_queue
        entry = {"repo": "example-org/example-repo", "number": 7,
                 "comment_full": operator_hold.ask(18, ["review"])}
        rendered = review_queue.render([entry], {}, mode="token")
        self.assertIn("Details unavailable. Try again in five minutes.", rendered)
        self.assertNotIn("data-answer", rendered)


class ReviewReads(unittest.TestCase):
    def test_review_bot_identity_handles_graphql_bot_and_rest_spellings(self):
        from dashboard.review_queue import Details
        from unittest import mock
        class GH:
            def graphql(self, query, variables):
                self.query = query
                nodes = [{'isResolved': False, 'comments': {'nodes': [{'body': 'finding',
                          'author': {'login': login, '__typename': kind}}]}}
                         for login, kind in [('grug-tribe', 'Bot'), ('grug-tribe', 'User'),
                                             ('other-reviewer', 'Bot')]]
                return {'repository': {'pullRequest': {'reviewThreads': {
                    'nodes': nodes, 'pageInfo': {'hasNextPage': False}}}}}
        gh = GH()
        with mock.patch.dict('os.environ', {'LOOM_REVIEW_BOT': 'grug-tribe'}):
            rows = Details(gh)._threads('example-org/example-repo', 18)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['comments']['nodes'][0]['author']['__typename'], 'Bot')
            reviews = Details._elder_reviews([
                {'user': {'login': 'grug-tribe[bot]'}, 'body': 'Elder findings'},
                {'user': {'login': 'grug-tribe'}, 'body': 'forged'},
                {'user': {'login': 'other-reviewer[bot]'}, 'body': 'unrelated'}])
        self.assertEqual([row['body'] for row in reviews], ['Elder findings'])
        self.assertIn('__typename', gh.query)

    def test_reader_keeps_complete_finding_and_head_bound_actions(self):
        from dashboard.review_queue import Details, render
        from types import SimpleNamespace
        from unittest import mock
        finding = 'First paragraph.\n\n' + ('Detail ' * 90) + 'FINAL REQUIRED STEP <script>'
        class GH:
            def rest(self, method, path):
                if '/issues/' in path:
                    return SimpleNamespace(data={'body': '## Acceptance criteria\n- [ ] Check rollback'})
                if '/check-runs?' in path:
                    return SimpleNamespace(data={'total_count': 0, 'check_runs': []})
                return SimpleNamespace(data={'state': 'open', 'head': {'sha': 'a' * 40}, 'labels': []})
            def paginate(self, path):
                return [{'filename': 'docs/guide.md'}] if path.endswith('/files') else []
            def graphql(self, query, variables):
                return {'repository': {'pullRequest': {'reviewThreads': {
                    'nodes': [{'isResolved': False, 'comments': {'nodes': [{'body': finding,
                        'author': {'login': 'grug-tribe[bot]'}}]}}],
                    'pageInfo': {'hasNextPage': False, 'endCursor': None}}}}}
        entry = {'repo': 'example-org/example-repo', 'number': 7,
                 'comment_full': operator_hold.ask(18, ['review']), 'body_ask': ''}
        with mock.patch.dict('os.environ', {'LOOM_REVIEW_BOT': 'grug-tribe[bot]'}):
            details = Details(GH()).get(entry)
        self.assertEqual(details['errors'], {})
        html = render([entry], {entry['repo'] + '#7': details}, mode='token')
        self.assertIn('FINAL REQUIRED STEP &lt;script&gt;', html)
        self.assertNotIn('<script>', html)
        self.assertIn('data-review-head="' + 'a' * 40 + '"', html)
        self.assertIn('No matching trusted proof record', html)

    def test_every_thread_page_is_read_without_showing_other_commenters(self):
        from dashboard.review_queue import Details
        class GH:
            def __init__(self):
                self.cursors = []
            def graphql(self, query, variables):
                self.cursors.append(variables['cursor'])
                first = variables['cursor'] is None
                node = {'isResolved': False, 'comments': {'nodes': [
                    {'author': {'login': 'grug-tribe[bot]'}, 'body': 'finding', 'createdAt': ''}]}}
                return {'repository': {'pullRequest': {'reviewThreads': {
                    'nodes': [node], 'pageInfo': {'hasNextPage': first, 'endCursor': 'next' if first else None}}}}}
        gh = GH()
        self.assertEqual(len(Details(gh)._threads('example-org/example-repo', 18)), 2)
        self.assertEqual(gh.cursors, [None, 'next'])

    def test_check_runs_use_the_rest_response_envelope_and_all_pages(self):
        from dashboard.review_queue import Details
        from types import SimpleNamespace
        class GH:
            def rest(self, method, path):
                page = 2 if 'page=2' in path else 1
                return SimpleNamespace(data={'total_count': 101, 'check_runs': [
                    {'name': 'check-%d' % i, 'conclusion': 'success'}
                    for i in (range(100) if page == 1 else [100])]})
        self.assertEqual(len(Details(GH())._checks('/repos/example-org/example-repo', 'a' * 40)), 101)


class ReviewRoute(unittest.TestCase):
    def test_actual_page_uses_the_held_queue_and_existing_answer_auth(self):
        from unittest import mock
        from types import SimpleNamespace
        from dashboard import server
        repo = 'example-org/example-repo'
        entry = {'repo': repo, 'number': 7, 'comment_full': operator_hold.ask(18, ['review']),
                 'title': 'Held change'}
        state = SimpleNamespace(repos=[repo], operator_items=[], gh=None,
                                response=lambda: {'operator_claims': {}})
        with mock.patch.object(server.needs_you_view, 'render', return_value=([entry], '')):
            text = server._review_queue(state)
        self.assertIn('Review queue', text)
        self.assertIn('Held change', text)
        self.assertIn('Details unavailable', text)


class ReviewTarget(unittest.TestCase):
    def setUp(self):
        from unittest import mock
        patcher = mock.patch.dict('os.environ', {'LOOM_BRIEF_AUTHORS': 'station-bot'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_latest_trusted_ask_can_precede_the_last_comment_page(self):
        from dashboard import answers
        from types import SimpleNamespace
        from unittest import mock
        trusted = {'body': answer.HUMAN_MARKER + '\n' + operator_hold.ask(18, ['review']),
                   'user': {'login': 'station-bot'}}
        outsider = {'body': answer.HUMAN_MARKER + '\nPick another task.',
                    'user': {'login': 'outsider'}}
        class GH:
            def rest(self, method, path):
                if '/pulls/' in path:
                    return SimpleNamespace(data={'state': 'open', 'head': {'sha': 'a' * 40}})
                return SimpleNamespace(data=[outsider] if 'page=2' in path else
                                       [trusted] + [{'body': 'ordinary reply'}] * 99)
        entry = {'repo': 'example-org/example-repo', 'number': 7,
                 'comment_full': operator_hold.ask(18, ['review']), 'body_ask': ''}
        comments = answers._comments(GH(), entry['repo'], 7, 100)
        self.assertEqual(len(comments), 101)
        with mock.patch.dict('os.environ', {'LOOM_BRIEF_AUTHORS': 'station-bot'}):
            self.assertEqual(answers.review_target(GH(), entry, comments, 18, 'a' * 40),
                             'Review target: #18 ' + 'a' * 40)

    def test_changed_head_refuses_before_an_answer_can_be_written(self):
        from dashboard import answers
        from types import SimpleNamespace
        entry = {'repo': 'example-org/example-repo', 'number': 7,
                 'comment_full': operator_hold.ask(18, ['review']), 'body_ask': ''}
        comments = [{'body': answer.HUMAN_MARKER + '\n' + entry['comment_full'],
                     'user': {'login': 'station-bot'}}]
        gh = SimpleNamespace(rest=lambda *args: SimpleNamespace(data={
            'state': 'open', 'head': {'sha': 'b' * 40}}))
        with self.assertRaises(answers.Refused):
            answers.review_target(gh, entry, comments, 18, 'a' * 40)

    def test_a_newer_non_pr_ask_refuses_a_stale_review_card(self):
        from dashboard import answers
        entry = {'repo': 'example-org/example-repo', 'number': 7,
                 'comment_full': operator_hold.ask(18, ['review']), 'body_ask': ''}
        comments = [{'body': answer.HUMAN_MARKER + '\nPick another task.',
                     'user': {'login': 'station-bot'}}]
        with self.assertRaises(answers.Refused):
            answers.review_target(None, entry, comments, 18, 'a' * 40)


class BoundVerdict(unittest.TestCase):
    def test_newer_trusted_non_review_ask_supersedes_each_old_review_decision(self):
        for choice in ('Merge', 'Hold', 'Send back'):
            with self.subTest(choice=choice):
                comments = [
                    {'body': answer.HUMAN_MARKER + '\n' + operator_hold.ask(18, ['review']),
                     'user': {'login': 'station-bot'}},
                    {'body': answer.comment_body('operator-one', choice, 'Review target: #18 ' + 'a' * 40),
                     'user': {'login': 'station-bot'}},
                    {'body': answer.HUMAN_MARKER + '\nChoose another task.',
                     'user': {'login': 'station-bot'}}]
                self.assertIsNone(answer.pr_verdict(comments, 18, ['station-bot'],
                                                   ['operator-one'], head='a' * 40))

    def test_old_head_bound_approval_cannot_authorize_a_new_head(self):
        ask = {'body': answer.HUMAN_MARKER + '\n' + operator_hold.ask(18, ['review']),
               'user': {'login': 'station-bot'}}
        given = {'body': answer.comment_body('operator-one', 'Merge', 'Review target: #18 ' + 'a' * 40),
                 'user': {'login': 'station-bot'}}
        self.assertIsNone(answer.pr_verdict([ask, given], 18, ['station-bot'], ['operator-one'], head='b' * 40))


class NoAutomaticReviewDecision(unittest.TestCase):
    def test_held_actions_never_become_reversible_defaults(self):
        for choice in ('Merge', 'Hold', 'Send back'):
            with self.subTest(choice=choice), self.assertRaises(ValueError):
                answer.decision_default(operator_hold.ask(18, ['review']) +
                                        '\nReversible: yes\nDefault: ' + choice)
