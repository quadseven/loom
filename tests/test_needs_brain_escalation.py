"""Test that the station escalates needs-brain tickets that wait 4+ hours."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import station, tickets  # noqa: E402

REPO = "example-owner/example-repo"
AUTHORS = ["alice-example"]
PR_NUMBER = 1
TICKET_NUMBER = 2
HEAD_SHA = "abc123"
FOUR_HOURS_AGO = 4 * 3600
PR_STALLED = 6 * 3600


class FakeGitHub:
    """A tiny stateful issues API for testing escalations."""

    def __init__(self):
        self.issues = {}
        self.pulls = []
        self.comments = {}
        self.calls = []
        self.posts = []
        self.writes = []
        self.head_commits = {}
        self.lane_prs = []
        self.add_issue(
            repo=REPO, number=TICKET_NUMBER, state="open",
            labels=["loom:ticket", "loom:pr", "needs-brain"],
            body=f"Refs {REPO}#{TICKET_NUMBER}"
        )
        self.add_pull(
            repo=REPO, number=PR_NUMBER, state="open",
            head_ref="loom/1", head_sha=HEAD_SHA, user="alice-example"
        )
        self.pr_ticket_map = {f"{REPO}#{PR_NUMBER}": TICKET_NUMBER}
        self.head_commits[HEAD_SHA] = {"ref": "loom/1", "pr_number": PR_NUMBER}

    def add_issue(self, repo, number, state="open", labels=(), body=""):
        self.issues[f"{repo}#{number}"] = {
            "number": number, "state": state, "labels": labels,
            "body": body, "created_at": "2026-01-01T00:00:00Z"
        }
        self.open_issues = [
            (repo, int(i["number"]))
            for repo, issues in self.issues.items()
            for i in issues if i.get("state") == "open"
        ]

    def add_pull(self, repo, number, state="open", head_ref="loom/1", head_sha=None, user="alice-example"):
        if head_sha is None:
            head_sha = "abc123"
        self.pulls.append({
            "number": number, "state": state, "created_at": "2026-01-01T00:00:00Z",
            "head": {"ref": head_ref, "sha": head_sha}, "user": {"login": user}
        })
        self.lane_prs.append((repo, number, head_sha))

    def get_issue(self, repo, number):
        return self.issues.get(f"{repo}#{number}")

    def get_pull(self, repo, number):
        for pr in self.pulls:
            if pr["repo"] == repo and pr["number"] == number:
                return pr
        return None

    def paginate(self, path):
        self.calls.append(("GET", path))
        m = re.match(r"/repos/([^/]+/[^/]+)/issues/(\d+)/comments\?", path)
        if m:
            return list(self.comments.get((m.group(1), int(m.group(2))), []))
        m = re.match(r"/repos/([^/]+/[^/]+)/issues\?labels=loom:ticket&state=open", path)
        if m:
            issues = []
            for repo, issues_dict in self.issues.items():
                for issue in issues_dict:
                    if issue.get("state") == "open" and "loom:ticket" in issue.get("labels", []):
                        issues.append(issue)
            return issues
        m = re.match(r"/repos/([^/]+/[^/]+)/issues\?labels=loom:pr&state=open", path)
        if m:
            issues = []
            for repo, issues_dict in self.issues.items():
                for issue in issues_dict:
                    if issue.get("state") == "open" and "loom:pr" in issue.get("labels", []):
                        issues.append(issue)
            return issues
        return []

    def rest(self, method, path, body=None):
        self.calls.append((method, path))
        if method == "GET":
            m = re.match(r"/repos/([^/]+/[^/]+)/issues/(\d+)", path)
            if m:
                repo, number = m.group(1), int(m.group(2))
                return self.get_issue(repo, number)
            m = re.match(r"/repos/([^/]+/[^/]+)/pulls/(\d+)", path)
            if m:
                repo, number = m.group(1), int(m.group(2))
                return self.get_pull(repo, number)
            m = re.match(r"/repos/([^/]+/[^/]+)/issues/(\d+)/comments", path)
            if m:
                repo, number = m.group(1), int(m.group(2))
                return self.comments.get((repo, number), [])
            m = re.match(r"/repos/([^/]+/[^/]+)/issues/(\d+)/labels", path)
            if m:
                repo, number = m.group(1), int(m.group(2))
                issue = self.get_issue(repo, number)
                if issue:
                    return issue.get("labels", [])
        if method == "POST":
            if path.endswith("/comments"):
                comment_id = len(self.comments) + 1
                self.comments.append({
                    (path.rsplit("/", 1)[0], comment_id): body
                })
                return {"id": comment_id, "body": body}
            if path.endswith("/labels"):
                m = re.match(r"/repos/([^/]+/[^/]+)/issues/(\d+)/labels", path)
                if m:
                    repo, number = m.group(1), int(m.group(2))
                    self.add_label(repo, number, body.get("name"))
            if path.endswith("/commits"):
                m = re.match(r"/repos/([^/+]+)/([^/]+)/commits/(.+)", path)
                if m:
                    repo, _owner, sha = m.group(1), m.group(2), m.group(3)
                    self.head_commits[sha] = self.head_commits.get(sha, {})
                    return {"sha": sha, "commit": self.head_commits[sha]}
            if path.endswith("/merge"):
                return {"merged": True, "merge_commit_sha": "def456"}

        return {"data": None}

    def add_label(self, repo, number, label):
        if f"{repo}#{number}" not in self.issues:
            return
        issue = self.issues[f"{repo}#{number}"]
        if isinstance(issue.get("labels"), list):
            issue["labels"].append(label)
        else:
            issue["labels"] = [label]

    def add_comment(self, repo, number, comment):
        if (repo, number) not in self.comments:
            self.comments[(repo, number)] = []
        self.comments[(repo, number)].append(comment)

    def has_comment_marker(self, repo, number, marker):
        if (repo, number) not in self.comments:
            return False
        for comment in self.comments[(repo, number)]:
            if marker in str(comment.get("body", "")):
                return True
        return False


class TestNeedsBrainEscalation(unittest.TestCase):
    """Test that the station escalates tickets that wait on brain for 4+ hours."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.gh = FakeGitHub()
        self.station = station.Station(
            settings=station.Settings(
                home=self.temp_dir,
                repos=[REPO],
                authors=AUTHORS,
                plan_check="Grug - Chief",
                factory_on=False
            ),
            gh=self.gh,
            now=time.time() - FOUR_HOURS_AGO
        )
        os.makedirs(os.path.join(self.temp_dir, "state"), exist_ok=True)

    def tearDown(self):
        if hasattr(self, 'temp_dir'):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_ticket_under_4_hours_no_change(self):
        """A ticket under 4 hours with needs-brain is left alone."""
        # Ticket is open for only 3 hours, so it should not be escalated
        self.station.now = time.time() - (3 * 3600)
        self.station.escalate_needs_brain_tickets()
        issue = self.gh.get_issue(REPO, TICKET_NUMBER)
        self.assertEqual(issue["labels"], ["loom:ticket", "loom:pr", "needs-brain"])
        self.assertFalse(self.gh.has_comment_marker(REPO, TICKET_NUMBER, "<!-- loom:human v1 -->"))

    def test_no_open_lane_pr_no_change(self):
        """A ticket with no open lane PR is left alone."""
        # Remove the lane PR
        for pr in self.gh.pulls:
            if pr["number"] == PR_NUMBER:
                pr["state"] = "closed"
        self.gh.escalated_prs = set()
        self.station.escalate_needs_brain_tickets()
        issue = self.gh.get_issue(REPO, TICKET_NUMBER)
        self.assertEqual(issue["labels"], ["loom:ticket", "loom:pr", "needs-brain"])
        self.assertFalse(self.gh.has_comment_marker(REPO, TICKET_NUMBER, "<!-- loom:human v1 -->"))

    def test_pr_already_escalated_no_duplicate(self):
        """A second pass on the same PR head posts no comment and changes no label."""
        # First pass should escalate
        self.station.escalate_needs_brain_tickets()
        issue = self.gh.get_issue(REPO, TICKET_NUMBER)
        self.assertIn("loom:human", issue["labels"])
        self.assertTrue(self.gh.has_comment_marker(REPO, TICKET_NUMBER, "<!-- loom:human v1 -->"))

        # Second pass should not escalate again
        self.station.escalate_needs_brain_tickets()
        issue = self.gh.get_issue(REPO, TICKET_NUMBER)
        self.assertIn("loom:human", issue["labels"])
        self.assertTrue(self.gh.has_comment_marker(REPO, TICKET_NUMBER, "<!-- loom:human v1 -->"))

    def test_different_pr_head_escalates(self):
        """A new head commit may escalate again."""
        # First PR head is already escalated
        self.station.escalate_needs_brain_tickets()
        issue = self.gh.get_issue(REPO, TICKET_NUMBER)
        self.assertIn("loom:human", issue["labels"])

        # New PR head should be escalated
        new_sha = "def456"
        for pr in self.gh.pulls:
            if pr["number"] == PR_NUMBER:
                pr["head"]["sha"] = new_sha
        self.station.escalate_needs_brain_tickets()
        issue = self.gh.get_issue(REPO, TICKET_NUMBER)
        self.assertIn("loom:human", issue["labels"])
        self.assertTrue(self.gh.has_comment_marker(REPO, TICKET_NUMBER, "<!-- loom:human v1 -->"))

    def test_multiple_tickets_one_escalates(self):
        """Multiple tickets with needs-brain, only one is open for 4+ hours."""
        # Add a second ticket that is also 4+ hours old
        TICKET_NUMBER_2 = 3
        self.gh.add_issue(
            repo=REPO, number=TICKET_NUMBER_2, state="open",
            labels=["loom:ticket", "loom:pr", "needs-brain"],
            body=f"Refs {REPO}#{TICKET_NUMBER_2}"
        )
        self.gh.add_pull(
            repo=REPO, number=PR_NUMBER + 1, state="open",
            head_ref="loom/2", head_sha="ghi789", user="alice-example"
        )

        self.station.escalate_needs_brain_tickets()
        issue1 = self.gh.get_issue(REPO, TICKET_NUMBER)
        issue2 = self.gh.get_issue(REPO, TICKET_NUMBER_2)
        self.assertIn("loom:human", issue1["labels"])
        self.assertIn("loom:human", issue2["labels"])
        self.assertTrue(self.gh.has_comment_marker(REPO, TICKET_NUMBER, "<!-- loom:human v1 -->"))
        self.assertTrue(self.gh.has_comment_marker(REPO, TICKET_NUMBER_2, "<!-- loom:human v1 -->"))


if __name__ == "__main__":
    unittest.main()
