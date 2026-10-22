"""The close-completeness gate at the transport layer.

conducted#209 was closed as completed with 6 unchecked acceptance boxes five
seconds after its PR merged; the GitHub close-completeness guard reopened it
seconds later (the same close -> reopen cycle as conducted#171 the same day).
The brain's close() already refuses that, but whatever closed the ticket
bypassed it. This gate lives in GitHub.rest(), the one choke point every
programmatic issue close passes through, so no code path -- brain, lane
machine, station, or future code -- can close an issue as completed while its
acceptance criteria are unchecked. Escape hatches mirror the GitHub guard:
state_reason not_planned, force-close/wontfix labels, and issues with no
criteria at all are allowed through.
"""
from __future__ import annotations

import json
import unittest

from bus.github import GitHub, GitHubError
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401


def reply(status=200, body=None):
    return status, {}, b"" if body is None else json.dumps(body).encode()


class Fake:
    """A transport answering replies in order, recording every call."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, json.loads(body) if body else None))
        return self.replies.pop(0)


def issue(body, labels=()):
    return {"number": 209, "state": "open", "labels": [{"name": name} for name in labels],
            "body": body}


UNCHECKED_BODY = ("## Acceptance criteria\n"
                  "- [ ] After level-up, check if train meets evolution conditions\n"
                  "- [x] Display 'What? [TRAIN] is evolving!' dialogue\n")


class CloseGateTests(unittest.TestCase):
    def test_completed_close_with_unchecked_boxes_is_refused_before_sending(self):
        fake = Fake(reply(200, issue(UNCHECKED_BODY)))
        gh = GitHub("t", transport=fake)
        with self.assertRaisesRegex(ValueError, "unchecked acceptance criterion"):
            gh.rest("PATCH", "/repos/quadseven/conducted/issues/209",
                    {"state": "closed"})
        self.assertEqual([c[0] for c in fake.calls], ["GET"])

    def test_completed_close_with_all_boxes_checked_goes_through(self):
        checked = UNCHECKED_BODY.replace("- [ ]", "- [x]")
        fake = Fake(reply(200, issue(checked)), reply(200, {}))
        gh = GitHub("t", transport=fake)
        gh.rest("PATCH", "/repos/quadseven/conducted/issues/209",
                {"state": "closed", "state_reason": "completed"})
        self.assertEqual([c[0] for c in fake.calls], ["GET", "PATCH"])

    def test_not_planned_close_needs_no_criteria_check(self):
        fake = Fake(reply(200, {}))
        gh = GitHub("t", transport=fake)
        gh.rest("PATCH", "/repos/quadseven/conducted/issues/209",
                {"state": "closed", "state_reason": "not_planned"})
        self.assertEqual([c[0] for c in fake.calls], ["PATCH"])

    def test_force_close_label_is_an_escape_hatch(self):
        for label in ("force-close", "wontfix"):
            fake = Fake(reply(200, issue(UNCHECKED_BODY, labels=[label])),
                        reply(200, {}))
            gh = GitHub("t", transport=fake)
            gh.rest("PATCH", "/repos/quadseven/conducted/issues/209",
                    {"state": "closed"})
            self.assertEqual([c[0] for c in fake.calls], ["GET", "PATCH"])

    def test_issue_with_no_criteria_closes(self):
        fake = Fake(reply(200, issue("Just a question, no checkboxes.")),
                    reply(200, {}))
        gh = GitHub("t", transport=fake)
        gh.rest("PATCH", "/repos/quadseven/conducted/issues/209",
                {"state": "closed"})
        self.assertEqual([c[0] for c in fake.calls], ["GET", "PATCH"])

    def test_pull_request_payloads_are_not_gated(self):
        pr = issue(UNCHECKED_BODY)
        pr["pull_request"] = {"url": "https://api.github.com/repos/q/c/pulls/1"}
        fake = Fake(reply(200, pr), reply(200, {}))
        gh = GitHub("t", transport=fake)
        gh.rest("PATCH", "/repos/quadseven/conducted/issues/209",
                {"state": "closed"})
        self.assertEqual([c[0] for c in fake.calls], ["GET", "PATCH"])

    def test_failed_issue_read_refuses_the_close(self):
        # loom#656: the verification GET failing must not let the close
        # through. The error rises out of rest() and the PATCH is never sent.
        fake = Fake(reply(500, {"message": "boom"}))
        gh = GitHub("t", transport=fake)
        with self.assertRaises(GitHubError):
            gh.rest("PATCH", "/repos/quadseven/conducted/issues/209",
                    {"state": "closed"})
        self.assertEqual([c[0] for c in fake.calls], ["GET"])

    def test_unreadable_issue_body_refuses_the_close(self):
        # loom#656: a 2xx read with an empty or non-JSON answer used to become
        # {} -- no labels, no criteria -- and the close went through unchecked.
        for raw in (b"", b"<html>not json</html>", b"[]"):
            fake = Fake((200, {}, raw), reply(200, {}))
            gh = GitHub("t", transport=fake)
            with self.assertRaisesRegex(GitHubError, "cannot read"):
                gh.rest("PATCH", "/repos/quadseven/conducted/issues/209",
                        {"state": "closed"})
            self.assertEqual([c[0] for c in fake.calls], ["GET"])


if __name__ == "__main__":
    unittest.main()
