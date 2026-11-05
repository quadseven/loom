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
        self.assertIn("Details unavailable", rendered)
        self.assertNotIn("data-answer", rendered)


class ReviewReads(unittest.TestCase):
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
    def test_changed_head_refuses_before_an_answer_can_be_written(self):
        from dashboard import answers
        from types import SimpleNamespace
        entry = {'repo': 'example-org/example-repo', 'number': 7,
                 'comment_full': operator_hold.ask(18, ['review']), 'body_ask': ''}
        comments = [{'body': answer.HUMAN_MARKER + '\n' + entry['comment_full']}]
        gh = SimpleNamespace(rest=lambda *args: SimpleNamespace(data={
            'state': 'open', 'head': {'sha': 'b' * 40}}))
        with self.assertRaises(answers.Refused):
            answers.review_target(gh, entry, comments, 18, 'a' * 40)

    def test_a_newer_non_pr_ask_refuses_a_stale_review_card(self):
        from dashboard import answers
        entry = {'repo': 'example-org/example-repo', 'number': 7,
                 'comment_full': operator_hold.ask(18, ['review']), 'body_ask': ''}
        comments = [{'body': answer.HUMAN_MARKER + '\nPick another task.'}]
        with self.assertRaises(answers.Refused):
            answers.review_target(None, entry, comments, 18, 'a' * 40)


class BoundVerdict(unittest.TestCase):
    def test_old_head_bound_approval_cannot_authorize_a_new_head(self):
        ask = {'body': answer.HUMAN_MARKER + '\n' + operator_hold.ask(18, ['review']),
               'user': {'login': 'station-bot'}}
        given = {'body': answer.comment_body('operator-one', 'Merge', 'Review target: #18 ' + 'a' * 40),
                 'user': {'login': 'station-bot'}}
        self.assertIsNone(answer.pr_verdict([ask, given], 18, ['station-bot'], ['operator-one'], head='b' * 40))
