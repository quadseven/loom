"""The blocking-comment convention (bus/comment_findings.py, loom#2988)."""
from __future__ import annotations

import unittest

from bus import comment_findings

from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

REPO = "example-owner/example-repo"
SHA = "abc1234def5678abc1234def5678abc1234def56"
AUTHORS = ["example-brain"]
HEAD_SHA = "abc1234def5678abc1234def5678abc1234def56"


def comment(login, body, at, ident):
    return {"id": ident, "user": {"login": login}, "body": body, "created_at": at,
            "html_url": "https://github.com/%s/pull/8#issuecomment-%d" % (REPO, ident)}


def url(ident):
    return "https://github.com/%s/pull/8#issuecomment-%d" % (REPO, ident)


class OpenFindingsTests(unittest.TestCase):

    def test_the_legacy_blocking_line_naming_a_commit_is_a_finding(self):
        found = comment_findings.open_findings(
            [comment("example-brain", "Blocking: retry fails on %s." % SHA[:9],
                     "2030-01-01T13:00:00Z", 1)], AUTHORS)
        self.assertEqual([c["id"] for c in found], [1])

    def test_a_blocking_line_without_a_commit_is_not_a_finding(self):
        found = comment_findings.open_findings(
            [comment("example-brain", "Blocking: retry fails.", "2030-01-01T13:00:00Z", 1)],
            AUTHORS)
        self.assertEqual(found, [])

    def test_the_marker_form_is_a_finding_without_a_commit(self):
        found = comment_findings.open_findings(
            [comment("example-brain", "<!-- loom:finding v1 -->\nRetry fails.",
                     "2030-01-01T13:00:00Z", 1)], AUTHORS)
        self.assertEqual([c["id"] for c in found], [1])

    def test_luna_verdicts_and_other_machine_markers_are_not_legacy_findings(self):
        verdict = comment("example-brain", "<!-- loom:luna-review v1 sha=%s -->\n"
                          "Blocking findings:\n- x" % SHA, "2030-01-01T13:00:00Z", 1)
        self.assertEqual(comment_findings.open_findings([verdict], AUTHORS), [])

    def test_only_a_trusted_api_author_counts(self):
        stranger = comment("example-stranger", "Blocking: retry fails on %s. -- example-brain"
                           % SHA[:9], "2030-01-01T13:00:00Z", 1)
        self.assertEqual(comment_findings.open_findings([stranger], AUTHORS), [])
        self.assertEqual(comment_findings.open_findings([stranger], []), [])

    def test_a_linked_later_trusted_comment_answers_the_finding(self):
        finding = comment("example-brain", "Blocking: retry fails on %s." % SHA[:9],
                          "2030-01-01T13:00:00Z", 1)
        answer = comment("example-brain", "Declined: test_retry_handles_empty_input covers it. " + url(1),
                         "2030-01-01T13:30:00Z", 2)
        self.assertEqual(comment_findings.open_findings([finding, answer], AUTHORS), [])

    def test_a_fix_naming_its_commit_answers_the_finding(self):
        finding = comment("example-brain", "Blocking: retry fails on %s." % SHA[:9],
                          "2030-01-01T13:00:00Z", 1)
        answer = comment("example-brain", "Fixed in %s: the retry test covers it. %s"
                         % (SHA[:10], url(1)), "2030-01-01T13:30:00Z", 2)
        self.assertEqual(comment_findings.open_findings([finding, answer], AUTHORS), [])

    def test_a_link_without_a_resolution_does_not_answer_the_finding(self):
        # A link alone is not a resolution: an acknowledgement, a repeated
        # objection, a fix with no commit or a bare decline leaves it open.
        finding = comment("example-brain", "Blocking: retry fails on %s." % SHA[:9],
                          "2030-01-01T13:00:00Z", 1)
        for body in ("Still broken, investigating: " + url(1),
                     "Acknowledged, looking at it. " + url(1),
                     "Fixed. " + url(1),
                     "Not fixed in %s yet. %s" % (SHA[:10], url(1)),
                     "Declined. " + url(1),
                     "Blocking: retry still fails on %s. %s" % (SHA[:9], url(1))):
            with self.subTest(body=body):
                found = comment_findings.open_findings(
                    [finding, comment("example-brain", body, "2030-01-01T13:30:00Z", 2)],
                    AUTHORS)
                self.assertIn(1, [c["id"] for c in found])

    def test_an_answer_needs_the_link_the_time_and_a_trusted_author(self):
        finding = comment("example-brain", "Blocking: retry fails on %s." % SHA[:9],
                          "2030-01-01T13:00:00Z", 1)
        declined = "Declined: the retry test covers it. "
        for label, answer in (
                ("unlinked", comment("example-brain", "Fixed in %s." % SHA[:10],
                                     "2030-01-01T13:30:00Z", 2)),
                ("older", comment("example-brain", declined + url(1),
                                  "2030-01-01T12:30:00Z", 2)),
                ("untrusted", comment("example-stranger", declined + url(1),
                                      "2030-01-01T13:30:00Z", 2)),
                ("untimed", comment("example-brain", declined + url(1), "", 2))):
            with self.subTest(answer=label):
                found = comment_findings.open_findings([finding, answer], AUTHORS)
                self.assertEqual([c["id"] for c in found], [1])

    def test_a_link_to_a_longer_comment_id_does_not_answer_a_shorter_one(self):
        finding = comment("example-brain", "Blocking: retry fails on %s." % SHA[:9],
                          "2030-01-01T13:00:00Z", 1)
        answer = comment("example-brain", "Declined: the retry test covers it. " + url(12),
                         "2030-01-01T13:30:00Z", 2)
        self.assertEqual([c["id"] for c in comment_findings.open_findings(
            [finding, answer], AUTHORS)], [1])

    def test_the_blocker_names_the_author_and_the_link(self):
        finding = comment("example-brain", "Blocking: retry fails on %s." % SHA[:9],
                          "2030-01-01T13:00:00Z", 1)
        self.assertEqual(comment_findings.blocker([finding]),
                         "unanswered blocking comment from example-brain: " + url(1))

    def test_fixed_in_fake_sha_does_not_clear_finding(self):
        finding = comment("example-brain", "Blocking: retry fails on %s." % SHA[:9],
                          "2030-01-01T13:00:00Z", 1)
        answer = comment("example-brain", "Fixed in deadbeef1: the retry test covers it. " + url(1),
                          "2030-01-01T13:30:00Z", 2)
        found = comment_findings.open_findings([finding, answer], AUTHORS, head_sha=HEAD_SHA)
        self.assertEqual([c["id"] for c in found], [1])

    def test_declined_without_evidence_does_not_clear_finding(self):
        finding = comment("example-brain", "Blocking: retry fails on %s." % SHA[:9],
                          "2030-01-01T13:00:00Z", 1)
        answer = comment("example-brain", "Declined: this is fine, no issue. " + url(1),
                          "2030-01-01T13:30:00Z", 2)
        found = comment_findings.open_findings([finding, answer], AUTHORS)
        self.assertEqual([c["id"] for c in found], [1])

    def test_declined_with_file_line_clears_finding(self):
        finding = comment("example-brain", "Blocking: retry fails on %s." % SHA[:9],
                          "2030-01-01T13:00:00Z", 1)
        answer = comment("example-brain", "Declined: the retry case is handled at test_retry.py:42. " + url(1),
                          "2030-01-01T13:30:00Z", 2)
        found = comment_findings.open_findings([finding, answer], AUTHORS)
        self.assertEqual(found, [])

    def test_declined_with_test_name_clears_finding(self):
        finding = comment("example-brain", "Blocking: retry fails on %s." % SHA[:9],
                          "2030-01-01T13:00:00Z", 1)
        answer = comment("example-brain", "Declined: test_retry_handles_empty_input covers it. " + url(1),
                          "2030-01-01T13:30:00Z", 2)
        found = comment_findings.open_findings([finding, answer], AUTHORS)
        self.assertEqual(found, [])

    def test_declined_with_link_clears_finding(self):
        finding = comment("example-brain", "Blocking: retry fails on %s." % SHA[:9],
                          "2030-01-01T13:00:00Z", 1)
        answer = comment("example-brain", "Declined: see https://example.com/design-doc for reasoning. " + url(1),
                          "2030-01-01T13:30:00Z", 2)
        found = comment_findings.open_findings([finding, answer], AUTHORS)
        self.assertEqual(found, [])

    def test_fixed_in_real_sha_clears_finding(self):
        finding = comment("example-brain", "Blocking: retry fails on %s." % SHA[:9],
                          "2030-01-01T13:00:00Z", 1)
        answer = comment("example-brain", "Fixed in %s: the retry test covers it. %s"
                          % (HEAD_SHA[:10], url(1)), "2030-01-01T13:30:00Z", 2)
        found = comment_findings.open_findings([finding, answer], AUTHORS, head_sha=HEAD_SHA)
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
