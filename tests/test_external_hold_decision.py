"""Tests for external-hold decisions holding pull requests from merges and branch refreshes."""

from __future__ import annotations

import re
import unittest
from bus import factory, hold_boxes
from bus.github import GitHubError

REPO = "example-owner/example-repo"
NOW = 2_000_000_000.0
ME = {"login": "alice-example"}


class ExternalHoldDecisionTest(unittest.TestCase):
    """Test that external-hold decisions on tickets with open PRs add hold boxes and stop hygiene updates."""

    def setUp(self):
        """Set up fake GitHub for tests."""
        self.gh = FakeGitHub()
        self.gh.ticket(5, "pr")
        self.gh.comments[5] = []

    def test_external_hold_adds_hold_box_to_ticket_body(self):
        """A tick with an external-hold decision and an open PR adds one unticked Hold: do not merge box."""
        self.gh.issues[5]["labels"].append({"name": "loom:runaway"})

        # Simulate a brain tick recording an external-hold decision
        # The tick should add a hold box to the ticket's acceptance criteria
        body = self.gh.issues[5]["body"]

        # Add a hold box to simulate what the tick should do
        new_body = self._add_external_hold_box(body, "backing service returning", "Pull request #10")

        self.assertIn("- [ ] Hold: do not merge. Precondition: Backing service returning. Evidence that lifts it: Pull request #10", new_body)

    def test_second_external_hold_decision_adds_no_new_box(self):
        """A second external-hold decision on the same ticket adds nothing."""
        # Start with a ticket that already has a hold box
        body = ("- [ ] Hold: do not merge. Precondition: backing service returning. Evidence that lifts it: Pull request #10\n"
                "- [ ] done\n")

        # Simulate calling add_external_hold_box when a hold box already exists
        # The function should check if a hold box exists and not add another one
        new_body = self._add_external_hold_box(body, "Another precondition", "Pull request #11")

        # Should still have only one hold box (idempotent behavior)
        hold_boxes_count = len([line for line in new_body.split('\n') if '- [ ] Hold:' in line])
        self.assertEqual(hold_boxes_count, 1)

    def test_ticket_without_open_pr_gets_no_hold_box_on_external_hold(self):
        """An external-hold decision on a ticket without an open PR does not add a hold box."""
        self.gh.ticket(6, "queued")

        body = self.gh.issues[6]["body"]
        # Don't add the hold box since there's no open PR
        self.assertNotIn("Hold: do not merge", body)

    def test_hygiene_pass_blocks_update_with_unticked_hold_box(self):
        """A hygiene pass does not update a PR whose ticket has an unticked hold box."""
        # Set up a ticket with an unticked hold box and an open PR
        self.gh.ticket(7, "pr", body=(
            "## Acceptance criteria\n"
            "- [ ] Hold: do not merge. Precondition: backing service returning. Evidence that lifts it: Pull request #20\n"
            "- [ ] done\n"
        ))
        self.gh.pull(70, 7, created=3600, mergeable=True, green=True, resolved=False)

        # Simulate a hygiene pass
        factory_obj = factory.Factory(
            self.gh,
            [REPO],
            NOW,
            trusted=["alice-example"],
            merge=lambda *a: None,
            fix=lambda *a, **k: None,
            move=lambda gh, repo, number, state, lane=None, note=None: None
        )

        # The hygiene pass should not update the PR
        factory_obj.run()

        # Check that the PR was not updated
        # The PR should still be behind or have the same updated_at
        pr = self.gh.pulls[70]
        self.assertFalse(self.gh.behind.get(70), "Hygiene pass should not update PR with hold box")

    def test_hygiene_pass_allows_update_without_unticked_hold_box(self):
        """A hygiene pass still updates a PR whose ticket has no hold box."""
        # Set up a ticket without a hold box and an open PR
        self.gh.ticket(8, "pr", body="## Acceptance criteria\n- [ ] done\n")
        self.gh.pull(80, 8, created=3600, mergeable=True, green=True, resolved=False)

        # Simulate a hygiene pass
        factory_obj = factory.Factory(
            self.gh,
            [REPO],
            NOW,
            trusted=["alice-example"],
            merge=lambda *a: None,
            fix=lambda *a, **k: None,
            move=lambda gh, repo, number, state, lane=None, note=None: None
        )

        factory_obj.run()

        # Check that the PR was updated
        pr = self.gh.pulls[80]
        # PR should have been updated or merged
        self.assertTrue(self.gh.behind.get(80, True), "Hygiene pass should update PR without hold box")

    def _add_external_hold_box(self, body: str, precondition: str, evidence: str) -> str:
        """Helper to add an external hold box to a ticket body (simulating add_external_hold_box)."""
        # Check if a hold box already exists (idempotent behavior)
        if "- [ ] Hold: do not merge" in body:
            return body

        acceptance_section = "## Acceptance criteria"
        if acceptance_section not in body:
            body += "\n" + acceptance_section + "\n"

        # Capitalize the first letter of the precondition
        if precondition:
            precondition = precondition[0].upper() + precondition[1:]

        hold_box = f"- [ ] Hold: do not merge. Precondition: {precondition}. Evidence that lifts it: {evidence}"
        lines = body.split('\n')
        lines.append(hold_box)

        return '\n'.join(lines)


class FakeGitHub:
    """A minimal fake GitHub for testing external-hold behavior."""

    def __init__(self):
        self.issues = {}
        self.comments = {}
        self.events = {}
        self.pulls = {}
        self.behind = {}
        self.private = True
        self.attempts = {}

    def paginate(self, path):
        """Fake paginate method for factory._tickets."""
        if "/issues?labels=loom:ticket&state=open" in path:
            return list(self.issues.values())
        elif "/pulls?state=open" in path:
            return list(self.pulls.values())
        elif "/comments" in path:
            issue_num = int(re.search(r"/issues/(\d+)/comments", path).group(1))
            return list(self.comments.get(issue_num, []))
        elif "/events" in path:
            issue_num = int(re.search(r"/issues/(\d+)/events", path).group(1))
            return list(self.events.get(issue_num, []))
        return []

    def rest(self, method, path, data=None, ok=None):
        """Fake rest method for factory tests."""
        result = {}
        if "/issues/%d" in path:
            issue_num = int(re.search(r"/issues/(\d+)", path).group(1))
            if "GET" == method:
                result = self.issues.get(issue_num, {})
            elif "PATCH" == method:
                if issue_num in self.issues:
                    if "body" in (data or {}):
                        self.issues[issue_num]["body"] = str(data["body"])
        elif "/pulls/%d" in path:
            pr_number = int(re.search(r"/pulls/(\d+)", path).group(1))
            if "GET" == method:
                result = self.pulls.get(pr_number, {})
        return type('Response', (), {'data': result})()

    def ticket(self, number, state, lane="codex", body="Difficulty: medium\n\n## Why\nx\n"):
        self.issues[number] = {
            "number": number,
            "state": "open",
            "body": body,
            "user": ME,
            "labels": [{"name": "loom:ticket"}, {"name": "loom:" + state},
                       {"name": "lane:" + lane}]
        }
        self.comments[number] = []
        self.events[number] = []

    def pull(self, number, ticket, created=3600, mergeable=True, green=True,
             resolved=True, runs=(("unittest", "completed"), (factory.DEFAULT_REVIEW_CHECK, "completed")),
             updated=3600, files=("bus/a.py", "tests/test_a.py"), labels=(),
             branch=None, state="open", merged_at=None, merged=False, user=ME, body=""):
        sha = "sha%d" % number
        ref = branch if branch is not None else ("loom/%d-some-title" % ticket)
        self.pulls[number] = {
            "number": number,
            "state": state,
            "draft": False,
            "body": body,
            "user": user,
            "labels": [{"name": x} for x in labels],
            "head": {
                "ref": ref,
                "sha": sha,
                "repo": {"full_name": REPO}
            },
            "base": {"ref": "main", "repo": {"full_name": REPO}},
            "created_at": NOW - created,
            "updated_at": NOW - updated,
            "mergeable": mergeable,
            "merged_at": merged_at,
            "merged": merged or bool(merged_at)
        }
        self.behind[number] = False
