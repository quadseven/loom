"""The blocking-comment convention (bus/comment_findings.py, loom#2988)."""
from __future__ import annotations

import unittest

from bus import comment_findings

from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

REPO = "example-owner/example-repo"
SHA = "abc1234def5678abc1234def5678abc1234def56"
AUTHORS = ["example-brain"]
OTHER_SHA = "0123456789abcdef0123456789abcdef01234567"


def on_pr(*shas):
    """A commit reader for a pull request carrying `shas`."""
    return lambda: list(shas)


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

    def test_a_marker_quoted_later_in_a_comment_is_not_a_finding(self):
        # The marker form opens the comment. A trusted builder quoting the
        # marker, say to explain the convention, raises no finding.
        quoted = comment("example-brain", "To block, open a comment with "
                         "`<!-- loom:finding v1 -->`.", "2030-01-01T13:00:00Z", 1)
        self.assertEqual(comment_findings.open_findings([quoted], AUTHORS), [])

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
        answer = comment("example-brain", "Declined: the retry case is tested at "
                         "tests/test_retry.py:40. " + url(1), "2030-01-01T13:30:00Z", 2)
        self.assertEqual(comment_findings.open_findings([finding, answer], AUTHORS), [])

    def test_a_fix_naming_its_commit_answers_the_finding(self):
        finding = comment("example-brain", "Blocking: retry fails on %s." % SHA[:9],
                          "2030-01-01T13:00:00Z", 1)
        answer = comment("example-brain", "Fixed in %s: the retry test covers it. %s"
                         % (SHA[:10], url(1)), "2030-01-01T13:30:00Z", 2)
        self.assertEqual(comment_findings.open_findings(
            [finding, answer], AUTHORS, on_pr(OTHER_SHA, SHA)), [])

    def test_a_fix_naming_a_commit_the_pull_request_lacks_does_not_answer(self):
        # Resolution-shaped text is not a fix: the named commit must be one of
        # the pull request's own, so any hex token does not clear a finding.
        finding = comment("example-brain", "Blocking: retry fails on %s." % SHA[:9],
                          "2030-01-01T13:00:00Z", 1)
        answer = comment("example-brain", "Fixed in deadbeef1: trust me. " + url(1),
                         "2030-01-01T13:30:00Z", 2)
        for label, reader in (("not on the pull request", on_pr(SHA, OTHER_SHA)),
                              ("no reader", None)):
            with self.subTest(commits=label):
                found = comment_findings.open_findings([finding, answer], AUTHORS, reader)
                self.assertEqual([c["id"] for c in found], [1])

    def test_a_decline_needs_evidence_beside_its_reason(self):
        finding = comment("example-brain", "Blocking: retry fails on %s." % SHA[:9],
                          "2030-01-01T13:00:00Z", 1)
        cases = (
            ("no evidence", "Declined: this is fine as it is. " + url(1), False),
            ("a commit not on the pull request",
             "Declined: covered by deadbeef1 already. " + url(1), False),
            ("a file line", "Declined: covered at bus/retry.py:12 already. " + url(1), True),
            ("another link", "Declined: tracked in https://example.com/notes/7 instead. "
             + url(1), True),
            ("a pull request commit", "Declined: covered by %s already. %s"
             % (OTHER_SHA[:12], url(1)), True))
        for label, body, clears in cases:
            with self.subTest(decline=label):
                found = comment_findings.open_findings(
                    [finding, comment("example-brain", body, "2030-01-01T13:30:00Z", 2)],
                    AUTHORS, on_pr(OTHER_SHA))
                self.assertEqual([c["id"] for c in found], [] if clears else [1])

    def test_commits_are_read_once_and_only_for_an_answer_naming_one(self):
        reads = []

        def reader():
            reads.append(1)
            return [SHA]

        finding = comment("example-brain", "Blocking: retry fails on %s." % SHA[:9],
                          "2030-01-01T13:00:00Z", 1)
        comment_findings.open_findings([finding], AUTHORS, reader)
        self.assertEqual(reads, [])
        fixes = [comment("example-brain", "Fixed in %s. %s" % (SHA[:10], url(1)),
                         "2030-01-01T13:%d0:00Z" % n, n) for n in (2, 3)]
        comment_findings.open_findings([finding] + fixes, AUTHORS, reader)
        self.assertEqual(reads, [1])

    def test_an_unreadable_commit_list_reaches_the_caller(self):
        def reader():
            raise OSError("boom")

        finding = comment("example-brain", "Blocking: retry fails on %s." % SHA[:9],
                          "2030-01-01T13:00:00Z", 1)
        fix = comment("example-brain", "Fixed in %s. %s" % (SHA[:10], url(1)),
                      "2030-01-01T13:30:00Z", 2)
        with self.assertRaises(OSError):
            comment_findings.open_findings([finding, fix], AUTHORS, reader)

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
                    AUTHORS, on_pr(SHA))
                self.assertIn(1, [c["id"] for c in found])

    def test_an_answer_needs_the_link_the_time_and_a_trusted_author(self):
        finding = comment("example-brain", "Blocking: retry fails on %s." % SHA[:9],
                          "2030-01-01T13:00:00Z", 1)
        declined = "Declined: the retry test covers it at tests/test_retry.py:40. "
        for label, answer in (
                ("unlinked", comment("example-brain", "Fixed in %s." % SHA[:10],
                                     "2030-01-01T13:30:00Z", 2)),
                ("older", comment("example-brain", declined + url(1),
                                  "2030-01-01T12:30:00Z", 2)),
                ("untrusted", comment("example-stranger", declined + url(1),
                                      "2030-01-01T13:30:00Z", 2)),
                ("untimed", comment("example-brain", declined + url(1), "", 2))):
            with self.subTest(answer=label):
                found = comment_findings.open_findings([finding, answer], AUTHORS, on_pr(SHA))
                self.assertEqual([c["id"] for c in found], [1])

    def test_a_link_to_a_longer_comment_id_does_not_answer_a_shorter_one(self):
        finding = comment("example-brain", "Blocking: retry fails on %s." % SHA[:9],
                          "2030-01-01T13:00:00Z", 1)
        answer = comment("example-brain", "Declined: covered at tests/test_retry.py:40. "
                         + url(12),
                         "2030-01-01T13:30:00Z", 2)
        self.assertEqual([c["id"] for c in comment_findings.open_findings(
            [finding, answer], AUTHORS)], [1])

    def test_the_blocker_names_the_author_and_the_link(self):
        finding = comment("example-brain", "Blocking: retry fails on %s." % SHA[:9],
                          "2030-01-01T13:00:00Z", 1)
        self.assertEqual(comment_findings.blocker([finding]),
                         "unanswered blocking comment from example-brain: " + url(1))


if __name__ == "__main__":
    unittest.main()
