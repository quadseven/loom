"""close_facts: the done close's sentence about a ticket's merged pull
requests, built from the check runs the station read, never a fixed claim."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import verify_data  # noqa: E402

SHA = "0123456789ab" + "c" * 28


def run(name, conclusion, completed_at="2030-01-02T00:10:00Z", status="completed"):
    return {"name": name, "status": status, "conclusion": conclusion,
            "completed_at": completed_at}


def pull(runs, number=7, on_branch=True):
    return {"number": number, "merge_commit_sha": SHA, "merged_at": "2030-01-02T00:00:00Z",
            "check_runs": runs, "on_branch": on_branch}


class CloseFactsTest(unittest.TestCase):
    def test_a_neutral_review_check_is_named_and_nothing_is_green(self):
        text = verify_data.close_facts("main", [pull([
            run("Grug - Elder", "neutral"), run("Grug - Guard", "success"),
            run("unittest", "success")])])
        self.assertIn("Grug - Elder neutral", text)
        self.assertNotIn("green", text)
        self.assertIn("PR #7 merged 2030-01-02T00:00:00Z as 0123456789ab", text)
        self.assertIn("main contains it: yes", text)

    def test_every_run_success_or_skipped_is_green(self):
        text = verify_data.close_facts("main", [pull([
            run("Grug - Elder", "success"), run("docs", "skipped"),
            run("unittest", "success")])])
        self.assertIn("green", text)
        self.assertIn("3 runs", text)

    def test_no_merged_pull_request_never_says_merged(self):
        text = verify_data.close_facts("main", []).lower()
        self.assertIn("no merged pull request", text)
        self.assertNotIn("merged", text.replace("no merged pull request", ""))
        self.assertNotIn("green", text)

    def test_a_repeated_name_uses_its_newest_run(self):
        text = verify_data.close_facts("main", [pull([
            run("Grug - Elder", "neutral", completed_at="2030-01-02T00:10:00Z"),
            run("Grug - Elder", "success", completed_at="2030-01-02T00:20:00Z"),
            run("unittest", "success")])])
        self.assertIn("green", text)
        self.assertNotIn("neutral", text)
        older_last = verify_data.close_facts("main", [pull([
            run("Grug - Elder", "success", completed_at="2030-01-02T00:10:00Z"),
            run("Grug - Elder", "failure", completed_at="2030-01-02T00:20:00Z")])])
        self.assertIn("Grug - Elder failure", older_last)
        self.assertNotIn("green", older_last)

    def test_a_run_still_in_progress_is_named_not_green(self):
        text = verify_data.close_facts("main", [pull([
            run("unittest", None, completed_at=None, status="in_progress"),
            run("Grug - Elder", "success")])])
        self.assertIn("unittest in progress", text)
        self.assertNotIn("green", text)

    def test_no_check_runs_is_not_green(self):
        text = verify_data.close_facts("main", [pull([], on_branch=None)])
        self.assertIn("no check runs", text)
        self.assertIn("main contains it: unknown", text)
        self.assertNotIn("green", text)

    def test_a_read_error_says_the_checks_could_not_be_read(self):
        text = verify_data.close_facts("main", [], error="status 502")
        self.assertIn("could not be read (status 502)", text)
        self.assertNotIn("green", text)
        self.assertNotIn("no merged pull request", text.lower())

    def test_the_text_is_ascii_and_names_the_branch_state(self):
        text = verify_data.close_facts("", [pull([run("caf\u00e9 @bot", "success")],
                                                 on_branch=False)])
        text.encode("ascii")
        self.assertNotIn("@", text)
        self.assertIn("the default branch contains it: no", text)


if __name__ == "__main__":
    unittest.main()
