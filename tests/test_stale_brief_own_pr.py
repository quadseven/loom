"""bus/brief_check.py: a ticket requeued for unticked boxes is not held as
stale by its own merged pull request.

The station requeues a ticket when its lane's merged pull request leaves
boxes unticked (tickets.UNBUILT_NOTE). Later, merged_linked must not flag
that same pull request as stale: it was already judged by the requeue.
Also, a pull request from the ticket's own loom/<N>- branch is the lane's
own work and must not be treated as stale evidence.
"""
from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bus import brief_check, status, tickets  # noqa: E402
from bus.github import GitHub  # noqa: E402

REPO = "example-owner/example-app"
OTHER = "example-owner/example-infra"
NOW = 2_000_000_000.0
OPERATOR = "the-operator"
TRUSTED = [OPERATOR, "loom-station[bot]", "loom-brain[bot]"]


def iso(ago):
    return datetime.fromtimestamp(NOW - ago, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def comment(body, login=OPERATOR, ago=60, ident=1, bot=False):
    return {"id": ident, "body": body, "created_at": iso(ago),
            "user": {"login": login, "type": "Bot" if bot else "User"}}


class Route:
    """GitHub by path: GETs from `routes` (longest prefix first), a missing
    path is a 404, a path in `fail` a 422; writes are recorded."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.fail = set()
        self.writes = []
        self.reads = []

    def __call__(self, method, url, headers, body):
        path = url.split("api.github.com")[-1]
        if method != "GET":
            self.writes.append((method, path, json.loads(body) if body else None))
            return 200, {}, b"{}"
        self.reads.append(path)
        for prefix in sorted(self.routes, key=len, reverse=True):
            if path.startswith(prefix):
                if prefix in self.fail:
                    return 422, {}, b'{"message": "unprocessable"}'
                return 200, {}, json.dumps(self.routes[prefix]).encode()
        return 404, {}, b'{"message": "Not Found"}'


def ticket(body="", number=3017, labels=("loom:ticket", "loom:briefed", "lane:codex")):
    return {"number": number, "state": "open", "body": body, "created_at": iso(86400),
            "user": {"login": "loom-brain[bot]"}, "labels": [{"name": n} for n in labels]}


class MergedLinkedOwnPR(unittest.TestCase):
    """merged_linked returns None for the ticket's own requeued or loom/ PR."""

    def setUp(self):
        self.fake = Route({
            "/repos/%s/issues/3017/timeline" % REPO: [],
        })

    def _merged_linked(self, issue, comments=()):
        gh = GitHub("t", transport=self.fake)
        return brief_check.merged_linked(gh, REPO, issue, list(comments), TRUSTED)

    def test_unbuilt_requeue_line_suppresses_stale_for_that_pr(self):
        # Ticket was requeued because PR 42 left criteria unbuilt.
        unbuilt_line = tickets.UNBUILT_NOTE % (42, "criterion 1, criterion 2")
        body = ("Difficulty: medium\n\n## Why\nTest\n\n## What\n- Fix it\n\n"
                "## Acceptance criteria\n- [ ] A test passes\n")
        # The status section at the end of the body has the unbuilt note.
        issue = ticket(body=body + "\n\n" + status.BODY_BEGIN + "\n---\n\n"
                        + status.BODY_HEAD + "\n\n- 2026-11-11T15:46:17Z " + unbuilt_line + "\n"
                        + status.BODY_END, number=3017)

        # PR 42 is linked on the ticket and merged after filing.
        self.fake.routes["/repos/%s/pulls/42" % REPO] = {"merged_at": iso(3600), "title": "Lane work"}
        said = [comment("Shipping it in %s#42" % REPO)]
        got = self._merged_linked(issue, comments=said)
        self.assertIsNone(got, "PR 42 is in unbuilt requeue line; should not be stale")

    def test_loom_branch_pr_suppresses_stale(self):
        # No unbuilt requeue line, but the PR's head branch is loom/3017-...
        body = ("Difficulty: medium\n\n## Why\nTest\n\n## What\n- Fix it\n\n"
                "## Acceptance criteria\n- [ ] A test passes\n")
        issue = ticket(body=body, number=3017)

        # PR 99 has head branch loom/3017-some-slug
        self.fake.routes["/repos/%s/pulls/99" % REPO] = {
            "merged_at": iso(3600), "title": "Lane work",
            "head": {"ref": "loom/3017-some-slug", "sha": "abc123"}}
        said = [comment("Shipping it in %s#99" % REPO)]
        got = self._merged_linked(issue, comments=said)
        self.assertIsNone(got, "PR from ticket's own loom/ branch should not be stale")

    def test_loom_branch_with_attempt_suffix_suppresses_stale(self):
        # PR from loom/3017-slug-r2 (retry attempt)
        body = ("Difficulty: medium\n\n## Why\nTest\n\n## What\n- Fix it\n\n"
                "## Acceptance criteria\n- [ ] A test passes\n")
        issue = ticket(body=body, number=3017)

        self.fake.routes["/repos/%s/pulls/100" % REPO] = {
            "merged_at": iso(3600), "title": "Lane retry",
            "head": {"ref": "loom/3017-some-slug-r2", "sha": "def456"}}
        said = [comment("Shipping it in %s#100" % REPO)]
        got = self._merged_linked(issue, comments=said)
        self.assertIsNone(got, "PR from ticket's own loom/ branch with -rN should not be stale")

    def test_other_branch_pr_still_stale(self):
        # PR from a different branch (not loom/3017-...) is still stale.
        body = ("Difficulty: medium\n\n## Why\nTest\n\n## What\n- Fix it\n\n"
                "## Acceptance criteria\n- [ ] A test passes\n")
        issue = ticket(body=body, number=3017)

        self.fake.routes["/repos/%s/pulls/55" % REPO] = {
            "merged_at": iso(3600), "title": "Other work",
            "head": {"ref": "feature/other-branch", "sha": "xyz789"}}
        said = [comment("Shipping it in %s#55" % REPO)]
        got = self._merged_linked(issue, comments=said)
        self.assertIsNotNone(got, "PR from another branch should be stale")
        self.assertEqual(got.kind, brief_check.STALE)
        self.assertEqual(got.key, "pr-%s#55" % REPO)

    def test_other_repo_pr_still_stale(self):
        # PR in another repo of the same owner, from a non-loom branch.
        body = ("Difficulty: medium\n\n## Why\nTest\n\n## What\n- Fix it\n\n"
                "## Acceptance criteria\n- [ ] A test passes\n")
        issue = ticket(body=body, number=3017)

        self.fake.routes["/repos/%s/pulls/77" % OTHER] = {
            "merged_at": iso(3600), "title": "Cross-repo work",
            "head": {"ref": "main", "sha": "abc123"}}
        said = [comment("Shipping it in %s#77" % OTHER)]
        got = self._merged_linked(issue, comments=said)
        self.assertIsNotNone(got, "PR from another repo should be stale")
        self.assertEqual(got.kind, brief_check.STALE)
        self.assertEqual(got.key, "pr-%s#77" % OTHER)

    def test_unbuilt_requeue_for_different_pr_does_not_suppress(self):
        # Ticket has unbuilt requeue for PR 42, but PR 55 is also linked and merged.
        unbuilt_line = tickets.UNBUILT_NOTE % (42, "criterion 1")
        body = ("Difficulty: medium\n\n## Why\nTest\n\n## What\n- Fix it\n\n"
                "## Acceptance criteria\n- [ ] A test passes\n")
        issue = ticket(body=body + "\n\n" + status.BODY_BEGIN + "\n---\n\n"
                        + status.BODY_HEAD + "\n\n- 2026-11-11T15:46:17Z " + unbuilt_line + "\n"
                        + status.BODY_END, number=3017)

        # PR 55 is linked and merged, but not in the unbuilt requeue line.
        self.fake.routes["/repos/%s/pulls/55" % REPO] = {
            "merged_at": iso(3600), "title": "Other work",
            "head": {"ref": "feature/other", "sha": "xyz789"}}
        said = [comment("Also done in %s#55" % REPO)]
        got = self._merged_linked(issue, comments=said)
        self.assertIsNotNone(got, "PR 55 not in unbuilt line should be stale")
        self.assertEqual(got.key, "pr-%s#55" % REPO)

    def test_merged_before_filing_is_not_stale(self):
        # PR merged before ticket was filed is context, not stale.
        body = ("Difficulty: medium\n\n## Why\nTest\n\n## What\n- Fix it\n\n"
                "## Acceptance criteria\n- [ ] A test passes\n")
        issue = ticket(body=body, number=3017)

        self.fake.routes["/repos/%s/pulls/88" % REPO] = {
            "merged_at": iso(90000), "title": "Old work",
            "head": {"ref": "feature/old", "sha": "old123"}}
        said = [comment("Context: %s#88" % REPO)]
        got = self._merged_linked(issue, comments=said)
        self.assertIsNone(got, "PR merged before filing should not be stale")


if __name__ == "__main__":
    unittest.main()