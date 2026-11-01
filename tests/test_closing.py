"""A close or reopen says why in the thread first (loom#2444)."""

import unittest
from unittest import mock

from shadow_env import setUpModule, tearDownModule  # noqa: F401

from bus import closing, shadow
from bus.github import GitHubError, ShadowHeld


class FakeGitHub:
    """Records writes; a POST or PATCH raises when told to."""

    def __init__(self, issue=None, refuse=()):
        self.issue = issue if issue is not None else {"body": "", "labels": []}
        self.refuse = refuse
        self.writes = []

    def rest(self, method, path, body=None):
        if method == "GET":
            return mock.Mock(data=self.issue)
        if method in self.refuse:
            raise GitHubError("refused", 500)
        self.writes.append((method, path, body))
        return mock.Mock(data={})


COMMENTS = "/repos/o/r/issues/7/comments"
ISSUE = "/repos/o/r/issues/7"


class Close(unittest.TestCase):
    def test_the_reason_is_posted_before_the_close(self):
        gh = FakeGitHub()
        closing.close(gh, "o/r", 7, "The work moved to #9.", "not_planned")
        self.assertEqual(gh.writes, [
            ("POST", COMMENTS, {"body": "The work moved to #9."}),
            ("PATCH", ISSUE, {"state": "closed", "state_reason": "not_planned"})])

    def test_a_pull_request_closes_on_its_own_path_with_no_state_reason(self):
        gh = FakeGitHub()
        closing.close_pull(gh, "o/r", 7, "Superseded by #9.")
        self.assertEqual(gh.writes, [
            ("POST", COMMENTS, {"body": "Superseded by #9."}),
            ("PATCH", "/repos/o/r/pulls/7", {"state": "closed"})])

    def test_no_visible_reason_refuses_and_writes_nothing(self):
        for empty in ("", "   \n", None, "<!-- loom:marker v1 -->", "<!-- a -->\n <!-- b -->"):
            gh = FakeGitHub()
            with self.assertRaises(ValueError):
                closing.close(gh, "o/r", 7, empty, "completed")
            with self.assertRaises(ValueError):
                closing.reopen(gh, "o/r", 7, empty)
            with self.assertRaises(ValueError):
                closing.close_pull(gh, "o/r", 7, empty)
            self.assertEqual(gh.writes, [])

    def test_a_failed_comment_leaves_the_state_alone(self):
        gh = FakeGitHub(refuse=("POST",))
        with self.assertRaises(GitHubError):
            closing.close(gh, "o/r", 7, "Done.", "completed")
        with self.assertRaises(GitHubError):
            closing.reopen(gh, "o/r", 7, "Broken again.")
        self.assertEqual(gh.writes, [])

    def test_a_reason_already_in_the_thread_is_not_posted_twice(self):
        gh = FakeGitHub()
        closing.close(gh, "o/r", 7, "Said before.", "completed", said=True)
        self.assertEqual(gh.writes, [
            ("PATCH", ISSUE, {"state": "closed", "state_reason": "completed"})])

    def test_said_does_not_excuse_an_empty_reason(self):
        gh = FakeGitHub()
        with self.assertRaises(ValueError):
            closing.close(gh, "o/r", 7, "", "completed", said=True)
        self.assertEqual(gh.writes, [])

    def test_unticked_criteria_refuse_before_any_comment(self):
        gh = FakeGitHub({"body": "## Acceptance criteria\n\n- [ ] not yet\n", "labels": []})
        with self.assertRaises(ValueError):
            closing.close(gh, "o/r", 7, "Done.", "completed")
        self.assertEqual(gh.writes, [])

    def test_a_shadow_held_close_says_nothing(self):
        gh = FakeGitHub({"body": "", "labels": [{"name": "loom:ticket"}]})
        with mock.patch.object(shadow, "active", return_value=True), \
                mock.patch.object(shadow, "record_held",
                                  return_value={"new": True, "action": "close"}):
            with self.assertRaises(ShadowHeld):
                closing.close(gh, "o/r", 7, "Done.", "not_planned")
        self.assertEqual(gh.writes, [])


class ARefusedChange(unittest.TestCase):
    """A retry must not say it twice, and a close must not lose its reason."""

    def run_refused(self, status):
        gh = FakeGitHub()
        gh.rest = mock.Mock(side_effect=lambda method, path, body=None: self.answer(
            gh, method, path, body, status))
        with self.assertRaises(GitHubError):
            closing.close(gh, "o/r", 7, "Done.", "not_planned")
        return gh.writes

    @staticmethod
    def answer(gh, method, path, body, status):
        gh.writes.append((method, path, body))
        if method == "PATCH":
            raise GitHubError("no", status)
        return mock.Mock(data={"id": 55})

    def test_a_refusal_takes_the_comment_back(self):
        self.assertEqual(self.run_refused(403)[-1],
                         ("DELETE", "/repos/o/r/issues/comments/55", None))

    def test_an_answer_that_may_hide_a_landed_close_keeps_it(self):
        for status in (0, 502):
            self.assertEqual(self.run_refused(status)[-1][0], "PATCH")


class Reopen(unittest.TestCase):
    def test_the_reason_is_posted_before_the_reopen(self):
        gh = FakeGitHub()
        closing.reopen(gh, "o/r", 7, "The audit failed its proof.")
        self.assertEqual(gh.writes, [
            ("POST", COMMENTS, {"body": "The audit failed its proof."}),
            ("PATCH", ISSUE, {"state": "open"})])


if __name__ == "__main__":
    unittest.main()
