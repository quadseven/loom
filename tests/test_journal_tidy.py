"""A tick journal reads cleanly on GitHub (loom#3321): handoff markers are
bare lines at the end of the tick, never empty list bullets, and the goal
home's own issues and pull requests named in words become #N links."""
from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import handoff, journal, journal_check  # noqa: E402
from bus.github import GitHub  # noqa: E402

REPO = "example-owner/example-repo"
M = journal.JOURNAL_MARKER
DONE = "<!-- loom:done v1 stage=failed item=%s#2941 holder=lane-machine -->" % REPO
DEFER = ('<!-- loom:defer v1 stage=failed item=%s#2451 rev=failed-c6 holder=lane-machine '
         'note="needs a bot-side brief" -->' % REPO)


class Fake:
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, headers, body):
        path = url.split("api.github.com", 1)[-1]
        self.calls.append((method, path, json.loads(body) if body else None))
        if method == "GET" and path == "/user":
            return 200, {}, json.dumps({"login": "me"}).encode()
        if method == "GET":
            return 200, {}, b"[]"
        return 201, {}, b"{}"

    def written(self):
        [(_, _, body)] = [c for c in self.calls if c[0] in ("PATCH", "POST")]
        return body["body"]


def posted(*lines):
    fake = Fake()
    text = "\n".join([M, "Tick 2030-01-01T09:00:00Z, lane-machine"] + list(lines))
    journal.post(GitHub("t", transport=fake), REPO, 7, text)
    return fake.written()


class JournalTidyTests(unittest.TestCase):
    def test_handoff_markers_are_bare_lines_after_the_list(self):
        body = posted("- Failed: one ticket re-briefed.", "- " + DONE, "* " + DEFER,
                      "- Moved: merged 0, briefed 1")
        lines = body.splitlines()
        self.assertNotIn("- " + DONE, lines)
        self.assertNotIn("* " + DEFER, lines)
        self.assertEqual(lines[-2:], [DONE, DEFER])
        self.assertEqual(lines[-3], "")
        self.assertIn("- Failed: one ticket re-briefed.\n- Moved: merged 0, briefed 1", body)

    def test_moved_markers_are_still_read_as_handoffs(self):
        body = posted("- " + DONE, "- " + DEFER, "- Moved: briefed 1")
        found = handoff.marks([{"body": body, "at": "2030-01-01T09:00:00Z"}])
        self.assertEqual([(m.kind, m.item) for m in found],
                         [("done", (REPO, 2941)), ("defer", (REPO, 2451))])

    def test_goal_home_numbers_in_words_become_links(self):
        body = posted("- Failed: issue 1728 restarted; 2350's pull request 2970 closed; "
                      "PR 12 merged.", "- Moved: briefed 1")
        self.assertIn("issue #1728 restarted", body)
        self.assertIn("pull request #2970 closed", body)
        self.assertIn("PR #12 merged", body)

    def test_other_repos_code_and_comments_are_left_alone(self):
        body = posted("- Shipped: issue 5 of another repo; `issue 9` stays code.",
                      "- " + DONE, "- Moved: briefed 1")
        self.assertIn("issue 5 of another repo", body)
        self.assertIn("`issue 9`", body)
        self.assertIn(DONE, body)

    def test_a_linked_stuck_list_still_reads_as_repeated(self):
        # The previous tick was posted with its numbers linked; the new draft
        # names the same stuck issue in words.
        previous = journal.tidy("\n".join([M, "Tick 2030-01-01T08:00:00Z, lane-machine",
                                           "- Waiting: issue 7 blocked", "- Moved: merged 0"]))
        self.assertIn("issue #7", previous)
        checked = journal_check.annotate("\n".join([M, "Tick 2030-01-01T09:00:00Z, lane-machine",
                                                    "- Waiting: issue 7 blocked",
                                                    "- Moved: merged 0"]), previous)
        self.assertIn("repeated unchanged stuck list", checked)


if __name__ == "__main__":
    unittest.main()
