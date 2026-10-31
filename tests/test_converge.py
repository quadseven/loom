"""Fix round failures compared by signature (bus/converge.py)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bus import converge  # noqa: E402
from bus.tickets import FIX_MARKER  # noqa: E402

REPO = "example-owner/example-repo"


def fix_body(*sections):
    return "%s\nPull request: %s#7, branch loom/12-fix\n\n%s\n" % (
        FIX_MARKER, REPO, "\n\n".join(sections))


def finding(path, line, rule):
    return ("### Thread PRRT_old%d (comment 5) at %s:%s\n\n"
            "    grug-tribe: medium | _robustness_ | `%s` | quick win\n"
            "    \n    **What Elder sees**" % (line, path, line, rule))


def thread(ident, path, line, rule):
    return {"id": ident, "path": path, "line": line,
            "comments": {"nodes": [{"body": "medium | _robustness_ | `%s` | quick win\n\nmore"
                                            % rule}]}}


class Persistent(unittest.TestCase):
    def test_a_check_that_fails_again_is_persistent_whatever_its_run(self):
        body = fix_body("### Check: pasto-api CI verdict\n\n    failed",
                        "### Check: Step 1b - Run unit + integration tests (pytest)\n\n    failed")
        found = converge.persistent(body, ["Step 1b - Run unit + integration tests (pytest)",
                                           "pasto-api CI verdict"], [])
        self.assertEqual(found, ["check Step 1b - Run unit + integration tests (pytest)",
                                 "check pasto-api CI verdict"])

    def test_a_check_that_now_passes_is_progress(self):
        body = fix_body("### Check: lint\n\n    failed")
        self.assertEqual(converge.persistent(body, ["unittest"], []), [])

    def test_the_review_check_is_judged_by_its_findings_not_its_name(self):
        body = fix_body("### Check: Grug - Elder\n\n    findings")
        self.assertEqual(converge.persistent(body, ["Grug - Elder"], [],
                                             ignore=["Grug - Elder"]), [])

    def test_the_same_rule_a_few_lines_lower_under_a_new_thread_id_is_persistent(self):
        body = fix_body(finding("web/staged.js", 54, "unvalidated-external-input"),
                        finding("web/staged.js", 59, "silent-exception-swallow"))
        now = [thread("PRRT_a", "web/staged.js", 59, "unvalidated-external-input"),
               thread("PRRT_b", "web/staged.js", 64, "silent-exception-swallow")]
        self.assertEqual(converge.persistent(body, [], now),
                         ["finding unvalidated-external-input at web/staged.js:59",
                          "finding silent-exception-swallow at web/staged.js:64"])

    def test_a_finding_moved_more_than_the_slack_or_to_another_rule_is_new(self):
        body = fix_body(finding("web/staged.js", 54, "resource-leak"))
        far = converge.LINE_SLACK + 55
        for now in ([thread("PRRT_a", "web/staged.js", far, "resource-leak")],
                    [thread("PRRT_a", "web/staged.js", 54, "unvalidated-external-input")],
                    [thread("PRRT_a", "web/other.js", 54, "resource-leak")]):
            with self.subTest(now=now):
                self.assertEqual(converge.persistent(body, [], now), [])

    def test_the_same_thread_id_still_open_is_persistent(self):
        body = fix_body(finding("templates/privacy.html", 168, "partial-delivery"))
        now = [thread("PRRT_old168", "templates/privacy.html", 200, "something-else")]
        self.assertEqual(len(converge.persistent(body, [], now)), 1)

    def test_no_earlier_round_is_never_stuck(self):
        self.assertEqual(converge.persistent(None, ["lint"], []), [])


class LastFixBody(unittest.TestCase):
    def test_the_newest_trusted_round_for_the_pull_request(self):
        other = fix_body("### Check: a").replace("#7,", "#8,")
        mine = fix_body("### Check: b")
        stranger = fix_body("### Check: c")
        comments = [{"user": {"login": "Alice-Example"}, "body": mine},
                    {"user": {"login": "alice-example"}, "body": other},
                    {"user": {"login": "someone"}, "body": stranger}]
        self.assertEqual(converge.last_fix_body(comments, ["alice-example"], (REPO, 7)), mine)

    def test_summary_names_at_most_five(self):
        found = ["check %d" % n for n in range(7)]
        self.assertEqual(converge.summary(found),
                         "check 0; check 1; check 2; check 3; check 4; 2 more")


if __name__ == "__main__":
    unittest.main()
