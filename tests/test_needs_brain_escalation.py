"""Test that the station escalates needs-brain tickets that wait 4+ hours."""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import station, tickets  # noqa: E402

REPO = "example-owner/example-repo"
AUTHORS = ["alice-example"]
PR_NUMBER = 1
TICKET_NUMBER = 2
HEAD_SHA = "abc123"
NEEDS_BRAIN_AGE = 4 * 3600
NOW = time.time()


def stamp(hours_ago: float) -> str:
    """An ISO-8601 timestamp `hours_ago` before NOW."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - hours_ago * 3600))


class Response:
    """A minimal stand-in for bus.github.GitHub's rest response."""

    def __init__(self, data):
        self.data = data
        self.status = 200


class FakeGitHub:
    """A tiny stateful GitHub for testing escalations."""

    def __init__(self):
        self.issues = {}
        self.pulls = []
        self.comments = {}
        self.head_commits = {}
        self.events = {}
        self.api = "https://api.github.com"

    @staticmethod
    def _resp(data):
        return Response(data)

    def add_issue(self, repo, number, state="open", labels=(), body="", created_at=None):
        if created_at is None:
            created_at = stamp(0)
        self.issues[f"{repo}#{number}"] = {
            "number": number, "state": state, "labels": list(labels),
            "body": body, "created_at": created_at,
        }
        self.events[(repo, number)] = [{
            "event": "labeled", "label": {"name": "needs-brain"},
            "created_at": created_at,
        }]

    def add_pull(self, repo, number, state="open", head_ref="loom/1", head_sha=None,
                 user="alice-example", created_at=None):
        if head_sha is None:
            head_sha = HEAD_SHA
        if created_at is None:
            created_at = stamp(0)
        self.pulls.append({
            "number": number, "state": state, "created_at": created_at,
            "head": {"ref": head_ref, "sha": head_sha}, "user": {"login": user},
        })
        self.head_commits[head_sha] = {"ref": head_ref, "pr_number": number}

    def get_issue(self, repo, number):
        return self.issues.get(f"{repo}#{number}")

    def get_pull(self, repo, number):
        for pr in self.pulls:
            if pr["number"] == number:
                return pr
        return None

    def paginate(self, path):
        ticket_q = "/repos/%s/issues?labels=loom:ticket&state=open" % REPO
        pulls_q = "/repos/%s/pulls?state=open" % REPO
        if path.startswith(ticket_q):
            return [i for i in self.issues.values()
                    if i.get("state") == "open" and "loom:ticket" in i.get("labels", [])]
        if path.startswith(pulls_q):
            return [pr for pr in self.pulls if pr.get("state") == "open"]
        m = re.match(r"/repos/([^/]+/[^/]+)/issues/(\d+)/comments\?", path)
        if m:
            return list(self.comments.get((m.group(1), int(m.group(2))), []))
        m = re.match(r"/repos/([^/]+/[^/]+)/issues/(\d+)/events", path)
        if m:
            return list(self.events.get((m.group(1), int(m.group(2))), []))
        return []

    def rest(self, method, path, body=None):
        m = re.match(r"/repos/([^/]+/[^/]+)/issues/(\d+)$", path)
        if method == "GET" and m:
            return self._resp(self.get_issue(m.group(1), int(m.group(2))))
        m = re.match(r"/repos/([^/]+/[^/]+)/pulls/(\d+)$", path)
        if method == "GET" and m:
            return self._resp(self.get_pull(m.group(1), int(m.group(2))))
        m = re.match(r"/repos/([^/]+/[^/]+)/issues/(\d+)/comments$", path)
        if method == "GET" and m:
            return self._resp(list(self.comments.get((m.group(1), int(m.group(2))), [])))
        if method == "POST" and m:
            repo, number = m.group(1), int(m.group(2))
            self.comments.setdefault((repo, number), []).append({"body": body.get("body")})
            return self._resp({"id": len(self.comments), "body": body.get("body")})
        m = re.match(r"/repos/([^/]+/[^/]+)/issues/(\d+)/labels$", path)
        if method == "GET" and m:
            issue = self.get_issue(m.group(1), int(m.group(2)))
            return self._resp(list(issue.get("labels", [])) if issue else [])
        if method == "PUT" and m:
            repo, number = m.group(1), int(m.group(2))
            issue = self.get_issue(repo, number)
            if issue:
                issue["labels"] = list(body.get("labels", []))
            return self._resp({"labels": issue.get("labels", [])} if issue else {})
        m = re.match(r"/repos/([^/]+/[^/]+)/pulls\?state=open", path)
        if method == "GET" and m:
            return self._resp([pr for pr in self.pulls if pr.get("state") == "open"])
        return self._resp({"data": None})

    def add_comment(self, repo, number, comment):
        self.comments.setdefault((repo, number), []).append(comment)

    def comment_bodies(self, repo, number):
        return [str(c.get("body", "")) for c in self.comments.get((repo, number), [])]

    def has_comment_marker(self, repo, number, marker):
        return any(marker in body for body in self.comment_bodies(repo, number))

    def human_comment_count(self, repo, number):
        return sum(1 for body in self.comment_bodies(repo, number)
                   if "<!-- loom:human v1 -->" in body)


class TestNeedsBrainEscalation(unittest.TestCase):
    """Test that the station escalates needs-brain tickets that wait 4+ hours."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.gh = FakeGitHub()
        self.gh.add_issue(
            repo=REPO, number=TICKET_NUMBER, state="open",
            labels=["loom:ticket", "loom:pr", "needs-brain"],
            body="Refs %s#%d" % (REPO, TICKET_NUMBER), created_at=stamp(4))
        self.gh.add_pull(
            repo=REPO, number=PR_NUMBER, state="open",
            head_ref="loom/%d" % TICKET_NUMBER, head_sha=HEAD_SHA)
        self.station = station.Station(
            settings=station.Settings(
                home=self.temp_dir, repos=[REPO], authors=AUTHORS,
                owners=(), plan_check="Grug - Chief", factory_on=False),
            gh=self.gh,
            now=NOW)
        os.makedirs(os.path.join(self.temp_dir, "state"), exist_ok=True)

    def tearDown(self):
        if hasattr(self, 'temp_dir'):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_ticket_4_plus_hours_escalates_once(self):
        """A ticket with an open lane PR that waited 4+ hours gets loom:human
        and exactly one escalation comment naming the PR and the ask."""
        self.station.escalate_needs_brain_tickets()
        issue = self.gh.get_issue(REPO, TICKET_NUMBER)
        self.assertIn("loom:human", issue["labels"])
        self.assertEqual(self.gh.human_comment_count(REPO, TICKET_NUMBER), 1)
        self.assertTrue(self.gh.has_comment_marker(
            REPO, TICKET_NUMBER, "<!-- loom:human v1 -->"))
        body = "\n".join(self.gh.comment_bodies(REPO, TICKET_NUMBER))
        self.assertIn("%s#%d" % (REPO, PR_NUMBER), body)
        self.assertIn("python3 -m bus.brain merge %s#%d" % (REPO, PR_NUMBER), body)  # leak-guard-allow: command format under test
        self.assertIn("python3 -m bus.brain fix %s#%d" % (REPO, PR_NUMBER), body)  # leak-guard-allow: command format under test

    def test_age_uses_needs_brain_label_event(self):
        """An old ticket with a recent needs-brain label has not waited four hours."""
        self.gh.events[(REPO, TICKET_NUMBER)] = [{
            "event": "labeled", "label": {"name": "needs-brain"},
            "created_at": stamp(2),
        }]
        self.station.escalate_needs_brain_tickets()
        issue = self.gh.get_issue(REPO, TICKET_NUMBER)
        self.assertNotIn("loom:human", issue["labels"])
        self.assertEqual(self.gh.human_comment_count(REPO, TICKET_NUMBER), 0)

    def test_removed_needs_brain_label_is_not_escalated(self):
        """A later label removal ends the needs-brain wait period."""
        self.gh.events[(REPO, TICKET_NUMBER)].append({
            "event": "unlabeled", "label": {"name": "needs-brain"},
            "created_at": stamp(1),
        })
        self.station.escalate_needs_brain_tickets()
        issue = self.gh.get_issue(REPO, TICKET_NUMBER)
        self.assertNotIn("loom:human", issue["labels"])
        self.assertEqual(self.gh.human_comment_count(REPO, TICKET_NUMBER), 0)

    def test_ticket_under_4_hours_no_change(self):
        """A ticket under 4 hours is left alone."""
        self.gh.add_issue(
            repo=REPO, number=TICKET_NUMBER, state="open",
            labels=["loom:ticket", "loom:pr", "needs-brain"],
            body="Refs %s#%d" % (REPO, TICKET_NUMBER), created_at=stamp(3))
        self.station.escalate_needs_brain_tickets()
        issue = self.gh.get_issue(REPO, TICKET_NUMBER)
        self.assertEqual(issue["labels"], ["loom:ticket", "loom:pr", "needs-brain"])
        self.assertFalse(self.gh.has_comment_marker(
            REPO, TICKET_NUMBER, "<!-- loom:human v1 -->"))

    def test_no_open_lane_pr_no_change(self):
        """A ticket with no open lane PR is left alone."""
        for pr in self.gh.pulls:
            if pr["number"] == PR_NUMBER:
                pr["state"] = "closed"
        self.station.escalate_needs_brain_tickets()
        issue = self.gh.get_issue(REPO, TICKET_NUMBER)
        self.assertEqual(issue["labels"], ["loom:ticket", "loom:pr", "needs-brain"])
        self.assertFalse(self.gh.has_comment_marker(
            REPO, TICKET_NUMBER, "<!-- loom:human v1 -->"))

    def test_pr_already_escalated_no_duplicate(self):
        """A second pass on the same PR head posts no comment and changes no label."""
        self.station.escalate_needs_brain_tickets()
        issue = self.gh.get_issue(REPO, TICKET_NUMBER)
        self.assertIn("loom:human", issue["labels"])
        self.assertEqual(self.gh.human_comment_count(REPO, TICKET_NUMBER), 1)

        self.station.escalate_needs_brain_tickets()
        issue = self.gh.get_issue(REPO, TICKET_NUMBER)
        self.assertEqual(issue["labels"].count("loom:human"), 1)
        self.assertEqual(self.gh.human_comment_count(REPO, TICKET_NUMBER), 1)

        self.station.escalate_needs_brain_tickets()
        issue = self.gh.get_issue(REPO, TICKET_NUMBER)
        self.assertIn("loom:human", issue["labels"])
        self.assertEqual(self.gh.human_comment_count(REPO, TICKET_NUMBER), 1)

    def test_different_pr_head_escalates(self):
        """A new head commit may escalate once more."""
        self.station.escalate_needs_brain_tickets()
        issue = self.gh.get_issue(REPO, TICKET_NUMBER)
        self.assertIn("loom:human", issue["labels"])
        first = self.gh.human_comment_count(REPO, TICKET_NUMBER)
        self.assertEqual(first, 1)

        new_sha = "def456"
        for pr in self.gh.pulls:
            if pr["number"] == PR_NUMBER:
                pr["head"]["sha"] = new_sha
        self.station.escalate_needs_brain_tickets()
        issue = self.gh.get_issue(REPO, TICKET_NUMBER)
        self.assertIn("loom:human", issue["labels"])
        self.assertEqual(self.gh.human_comment_count(REPO, TICKET_NUMBER), 2)

    def test_multiple_tickets_only_the_due_one_escalates(self):
        """Of several needs-brain tickets, only the one that waited 4+ hours
        is escalated."""
        self.gh.add_issue(
            repo=REPO, number=3, state="open",
            labels=["loom:ticket", "loom:pr", "needs-brain"],
            body="Refs %s#3" % REPO, created_at=stamp(1))
        self.gh.add_pull(
            repo=REPO, number=2, state="open",
            head_ref="loom/3", head_sha="ghi789")

        self.station.escalate_needs_brain_tickets()
        issue1 = self.gh.get_issue(REPO, TICKET_NUMBER)
        issue2 = self.gh.get_issue(REPO, 3)
        self.assertIn("loom:human", issue1["labels"])
        self.assertNotIn("loom:human", issue2["labels"])
        self.assertEqual(self.gh.human_comment_count(REPO, TICKET_NUMBER), 1)
        self.assertEqual(self.gh.human_comment_count(REPO, 3), 0)


if __name__ == "__main__":
    unittest.main()
