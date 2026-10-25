from __future__ import annotations

import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import bug_refs  # noqa: E402

REPO = "example-owner/example-repo"
# Built, not written out: a public repo's leak scan flags the written shape.
OTHER = "other-owner/other-repo"
OTHER_3 = OTHER + "#" + "3"
NOW = dt.datetime(2026, 10, 25, 12, 0, tzinfo=dt.timezone.utc)
OLD = "2026-09-01T00:00:00Z"
YESTERDAY = "2026-10-24T12:00:00Z"


def fake_lookup(issues):
    """A lookup over {(repo, number): issue}; any other number raises, the
    way gh fails on a missing issue."""
    def lookup(repo, number):
        if (repo, number) not in issues:
            raise RuntimeError("HTTP 404")
        return issues[(repo, number)]
    return lookup


ISSUES = {
    (REPO, 7): {"number": 7, "state": "open", "created_at": OLD},
    (REPO, 8): {"number": 8, "state": "closed", "created_at": OLD},
    (REPO, 9): {"number": 9, "state": "closed", "created_at": YESTERDAY},
    (REPO, 10): {"number": 10, "state": "open", "created_at": OLD,
                 "pull_request": {"url": "x"}},
    (OTHER, 3): {"number": 3, "state": "open", "created_at": OLD},
}


class SectionTest(unittest.TestCase):
    def test_reads_each_spelling_and_drops_bullets(self):
        for heading in ("Bugs discovered", "Bugs found", "Found bugs", "discovered bugs"):
            summary = "Did a thing\n\n## %s\n- one, #1\n\n* two, #2\n## Next\nx" % heading
            self.assertEqual(bug_refs.section(summary), ["one, #1", "two, #2"], heading)

    def test_none_and_no_section_are_empty(self):
        self.assertEqual(bug_refs.section("## Bugs discovered\nnone\n"), [])
        self.assertEqual(bug_refs.section("## Operator steps before merge\nnone\n"), [])


class CheckTest(unittest.TestCase):
    def check(self, *lines):
        return bug_refs.check(list(lines), REPO, fake_lookup(ISSUES), NOW)

    def test_a_line_citing_an_open_issue_in_the_repo_is_kept(self):
        kept, rejected = self.check("The parser drops a tab, #7")
        self.assertEqual((kept, rejected), (["The parser drops a tab, #7"], []))

    def test_a_cross_repo_citation_is_looked_up_in_that_repo(self):
        kept, rejected = self.check("Shared lib bug, " + OTHER_3)
        self.assertEqual(len(kept), 1, rejected)

    def test_an_issue_the_lane_filed_and_closed_recently_is_kept(self):
        kept, _ = self.check("Fixed upstream already, #9")
        self.assertEqual(len(kept), 1)

    def test_rejections(self):
        cases = {
            "The cache never expires": "cites no issue",
            "Missing one, #404": "could not be looked up",
            "A pull request, #10": "pull request, not an issue",
            "Long closed, #8": "closed and was not filed",
            "Non ascii \u2014 here, #7": "plain ASCII",
        }
        for line, why in cases.items():
            kept, rejected = self.check(line)
            self.assertEqual(kept, [], line)
            self.assertEqual(len(rejected), 1, line)
            self.assertIn(why, rejected[0].why, line)

    def test_a_lookup_failure_rejects_the_line(self):
        def broken(repo, number):
            raise OSError("network down")
        kept, rejected = bug_refs.check(["Real bug, #7"], REPO, broken, NOW)
        self.assertEqual(kept, [])
        self.assertIn("could not be looked up", rejected[0].why)

    def test_every_citation_on_a_line_must_check_out(self):
        kept, rejected = self.check("Two bugs, #7 and #8")
        self.assertEqual(kept, [])
        self.assertEqual(rejected[0].position, 1)

    def test_an_issue_with_no_number_does_not_exist(self):
        kept, rejected = bug_refs.check(["Bug, #7"], REPO, lambda r, n: {}, NOW)
        self.assertEqual(kept, [])
        self.assertIn("does not exist", rejected[0].why)


class RenderTest(unittest.TestCase):
    def test_a_public_body_carries_no_other_repos_name(self):
        line = "Shared lib bug, %s, and %s#7" % (OTHER_3, REPO)
        out = bug_refs.render([line], REPO, public=True)
        self.assertEqual(out, "- Shared lib bug, issue 3 of another repo, and #7")
        self.assertNotIn("other-repo", out)

    def test_a_private_body_keeps_the_citation(self):
        out = bug_refs.render(["Bug, " + OTHER_3], REPO, public=False)
        self.assertEqual(out, "- Bug, " + OTHER_3)

    def test_refusal_asks_to_file_or_drop_and_never_repeats_the_line(self):
        reason = bug_refs.refusal([bug_refs.Rejected(2, "the line cites no issue")])
        self.assertIn("line 2: the line cites no issue", reason)
        self.assertIn("file the issue and cite it as #N, or drop the line", reason)


if __name__ == "__main__":
    unittest.main()
