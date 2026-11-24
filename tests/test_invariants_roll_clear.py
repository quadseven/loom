from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import unittest
from unittest import mock
from urllib.parse import parse_qs

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import invariants, station  # noqa: E402
from bus.github import GitHubError, Response  # noqa: E402
from bus.invariants import Check, EpicShape, Facts, GoalShape, Ref, Run  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

FACTORY_REPO = "example-owner/factory"
TARGET_REPO = "example-owner/target"
GOAL = 30
AUTHOR = "trusted-example"
T0 = 1_800_000_000.0
DAY_S = 24 * 3600


def stamp(at):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))


def ticket(number, state="open"):
    return {"number": number, "state": state, "labels": [{"name": "loom:ticket"}],
            "body": "Why."}


def epic(number, state="open", title=None):
    t = title or "Epic: part %d" % number
    return {"number": number, "state": state, "title": t,
            "labels": [{"name": "epic"}], "body": "Done when: x.\n\nPart of #%d\n" % GOAL}


class FakeGH:
    def __init__(self):
        self.rate = {}
        self.issues = {}
        self.comments = {}
        self.goal_issues = {
            FACTORY_REPO: [{"number": GOAL, "title": "Loom keeps goals moving",
                            "labels": [{"name": "loom:goal"}],
                            "sub_issues_summary": {"total": 40}}],
            TARGET_REPO: [{"number": GOAL, "title": "Target goal",
                           "labels": [{"name": "loom:goal"}],
                           "sub_issues_summary": {"total": 40}}],
        }
        self.pulls = {}
        self.pull_state = {}
        self.check_runs = {}
        self.scheduled_runs = {}
        self.private = set()
        self.calls = []
        self.writes = []
        self.search_items = []
        self.subs = {}
        self.tickets = {}
        self.refuse_subs = set()

    def paginate(self, path):
        self.calls.append(("GET", path))
        m = re.match(r"/repos/([^/]+/[^/]+)/issues/(\d+)/comments\?", path)
        if m:
            return list(self.comments.get((m.group(1), int(m.group(2))), []))
        m = re.match(r"/repos/([^/]+/[^/]+)/issues\?labels=loom:goal&", path)
        if m:
            return list(self.goal_issues.get(m.group(1), []))
        m = re.match(r"/repos/([^/]+/[^/]+)/issues\?labels=loom:invariant&state=(all|open)", path)
        if m:
            return [i for i in self.issues.values()
                    if m.group(2) == "all" or i["state"] == "open"]
        m = re.match(r"/repos/([^/]+/[^/]+)/issues\?labels=loom:ticket&state=open", path)
        if m:
            return list(self.tickets.get(m.group(1), []))
        m = re.match(r"/repos/([^/]+/[^/]+)/issues\?labels=loom:queued&state=open", path)
        if m:
            return list(self.tickets.get(m.group(1), []))
        m = re.match(r"/repos/([^/]+/[^/]+)/issues/(\d+)/sub_issues\?", path)
        if m:
            return list(self.subs.get((m.group(1), int(m.group(2))), []))
        if re.match(r"/repos/[^/]+/[^/]+/issues\?labels=epic&", path):
            return []
        raise AssertionError("unexpected paginate %s" % path)

    def rest(self, method, path, body=None):
        self.calls.append((method, path))
        if method != "GET":
            self.writes.append((method, path, body))
        if method == "POST" and path == "/repos/%s/issues" % FACTORY_REPO:
            number = 100 + len(self.issues)
            self.issues[number] = dict(body, number=number, id=9000 + number, state="open",
                                       closed_at=None)
            return Response(201, {"number": number, "id": 9000 + number})
        if method == "GET" and path.startswith("/search/issues?"):
            return Response(200, {"items": self.search_items})
        m = re.fullmatch(r"/repos/([^/]+/[^/]+)/actions/runs\?(.+)", path)
        if method == "GET" and m:
            runs = self.scheduled_runs.get(m.group(1), [])
            if isinstance(runs, Exception):
                raise runs
            query = parse_qs(m.group(2))
            assert query["event"] == ["schedule"] and query["status"] == ["completed"]
            size = int(query["per_page"][0])
            start = size * (int(query["page"][0]) - 1)
            return Response(200, {"total_count": len(runs),
                                  "workflow_runs": runs[start:start + size]})
        m = re.fullmatch(r"/repos/([^/]+/[^/]+)/issues/(\d+)/sub_issues", path)
        if method == "POST" and m and int(m.group(2)) in self.refuse_subs:
            raise GitHubError("Validation Failed: sub-issue limit reached", 422)
        if method == "POST" and m:
            child = next((i for i in self.issues.values() if i.get("id") == body["sub_issue_id"]),
                         None)
            if child is not None:
                self.subs.setdefault((m.group(1), int(m.group(2))), []).append(
                    dict(child, labels=[x if isinstance(x, dict) else {"name": x}
                                        for x in child.get("labels", [])]))
            return Response(201, {})
        m = re.fullmatch(r"/repos/[^/]+/[^/]+/issues/(\d+)", path)
        if m and method == "GET":
            issue = self.issues.get(int(m.group(1)))
            if issue is None:
                raise GitHubError("Not Found", 404)
            return Response(200, dict(issue, created_at=issue.get("created_at", stamp(T0)),
                                      labels=[x if isinstance(x, dict) else {"name": x}
                                              for x in issue.get("labels", [])]))
        if m and method == "PATCH":
            self.issues[int(m.group(1))].update(body)
            return Response(200, {})
        m = re.fullmatch(r"/repos/([^/]+/[^/]+)/issues/comments/(\d+)", path)
        if method == "PATCH" and m:
            for notes in self.comments.values():
                for note in notes:
                    if note.get("id") == int(m.group(1)):
                        note["body"] = body["body"]
            return Response(200, {})
        if method == "POST" and path.endswith("/comments"):
            m = re.fullmatch(r"/repos/([^/]+/[^/]+)/issues/(\d+)/comments", path)
            if m:
                self.next_comment = getattr(self, "next_comment", 500) + 1
                self.comments.setdefault((m.group(1), int(m.group(2))), []).append(
                    {"id": self.next_comment, "body": body["body"], "created_at": stamp(T0 + 10),
                     "user": {"login": AUTHOR}})
            return Response(201, {"id": getattr(self, "next_comment", 1)})
        m = re.fullmatch(r"/repos/([^/]+/[^/]+)/pulls/(\d+)", path)
        if m and method == "GET":
            pull = self.pull_state.get((m.group(1), int(m.group(2))))
            if pull is None:
                raise GitHubError("Not Found", 404)
            return Response(200, dict(pull, number=int(m.group(2))))
        if m and method == "PATCH":
            self.pull_state.setdefault((m.group(1), int(m.group(2))), {}).update(body)
            return Response(200, {})
        m = re.fullmatch(r"/repos/([^/]+/[^/]+)/pulls\?state=open&per_page=100", path)
        if m:
            return Response(200, self.pulls.get(m.group(1), []))
        m = re.match(r"/repos/[^/]+/[^/]+/commits/([0-9a-f]+)/check-runs\?", path)
        if m:
            return Response(200, {"check_runs": self.check_runs.get(m.group(1), [])})
        m = re.fullmatch(r"/repos/([^/]+/[^/]+)", path)
        if m:
            return Response(200, {"private": m.group(1) in self.private})
        raise AssertionError("unexpected %s %s" % (method, path))

    def filed(self):
        return [i for i in self.issues.values()]

    def graphql(self, query):
        self.calls.append(("POST", "/graphql"))
        return {"repository": {"pullRequest": {"reviewThreads": {
            "totalCount": 0, "nodes": []}}}}

    def comment(self, number, at, body, repo=FACTORY_REPO, author=AUTHOR):
        self.comments.setdefault((repo, number), []).append(
            {"body": body, "created_at": stamp(at), "user": {"login": author}})


class RollClearTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = tmp.name
        self.gh = FakeGH()
        self.said = []
        self.settings = station.Settings(self.home, [FACTORY_REPO, TARGET_REPO], [AUTHOR], ["example-owner"],
                                         invariant_goal="%s#%d" % (FACTORY_REPO, GOAL), self_repo=FACTORY_REPO)

    def run_at(self, now):
        st = station.Station(self.gh, self.settings, now=now)
        orig = station._say
        station._say = self.said.append
        try:
            st.check_invariants()
        finally:
            station._say = orig
        return st

    def write(self, path, text, at=None):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        if at is not None:
            os.utime(path, (at, at))

    def breaches(self):
        return [i for i in self.gh.filed() if invariants.LABEL in i.get("labels", [])]

    def test_epic_title_shared_tracking_issue_rewrites_to_none_when_shapes_read(self):
        """When shapes are read and epic-title-shared breach is gone for a target repo,
        the tracking issue's list is rewritten to 'none' on that pass."""
        # Set up two epics with the same title in TARGET_REPO (breach)
        self.gh.subs[(TARGET_REPO, GOAL)] = [
            dict(epic(40), title="Epic: shared title"),
            dict(epic(41), title="Epic: shared  title"),  # extra space, same normalized title
            epic(42)
        ]
        self.run_at(T0)
        filed = self.gh.filed()
        self.assertEqual(len(filed), 1)
        self.assertEqual(filed[0]["title"], "Invariant: epic-title-shared in %s" % TARGET_REPO)
        self.assertIn("Open epics with one title", filed[0]["body"])
        self.assertIn("issue 40", filed[0]["body"])
        self.assertIn("issue 41", filed[0]["body"])
        issue_number = filed[0]["number"]

        # Fix: make titles distinct in TARGET_REPO
        self.gh.subs[(TARGET_REPO, GOAL)] = [
            dict(epic(40), title="Epic: first batch"),
            dict(epic(41), title="Epic: second batch"),
            epic(42)
        ]
        # Run again with shapes read - the tracking issue should be rewritten to "none"
        def other_breaches(facts, say):
            self.assertIsNotNone(facts.shapes)
            shape = next(shape for shape in facts.shapes if shape.goal.key ==
                         "%s#%d" % (TARGET_REPO, GOAL))
            self.assertEqual([item.title for item in shape.epics],
                             ["Epic: first batch", "Epic: second batch", "Epic: part 42"])
            return [invariants.Breach("budget-probe", "budget-probe-%d" % n,
                                      "Budget probe %d" % n, (), T0)
                    for n in range(invariants.WRITES_MAX)]

        with mock.patch.object(invariants, "check", side_effect=other_breaches):
            self.run_at(T0 + invariants.CHECK_EVERY)
        # The issue should still exist but its body should now say "none"
        updated = self.gh.issues[issue_number]
        self.assertEqual(updated["state"], "open")
        self.assertIn(invariants.ROLL_NONE, updated["body"], "Tracking issue should list no violation now")

    def test_epic_title_shared_tracking_issue_closes_after_clear_wait(self):
        """Once the tracking issue has said 'none' for CLEAR_AFTER, it closes."""
        # Set up breach in TARGET_REPO
        self.gh.subs[(TARGET_REPO, GOAL)] = [
            dict(epic(40), title="Epic: shared title"),
            dict(epic(41), title="Epic: shared  title"),
        ]
        self.run_at(T0)
        filed = self.gh.filed()
        issue_number = filed[0]["number"]

        # Fix titles in TARGET_REPO
        self.gh.subs[(TARGET_REPO, GOAL)] = [
            dict(epic(40), title="Epic: first"),
            dict(epic(41), title="Epic: second"),
        ]
        # First clear pass: rewrites to "none"
        self.run_at(T0 + invariants.CHECK_EVERY)
        updated = self.gh.issues[issue_number]
        self.assertIn(invariants.ROLL_NONE, updated["body"])
        self.assertEqual(updated["state"], "open")

        # After CLEAR_AFTER: closes
        self.run_at(T0 + invariants.CHECK_EVERY + invariants.CLEAR_AFTER)
        self.assertEqual(self.gh.issues[issue_number]["state"], "closed", self.said)
        notes = self.gh.comments[(FACTORY_REPO, issue_number)]
        self.assertTrue(any(invariants.CLEARED_TEXT in note["body"] for note in notes), notes)

    def test_epic_title_shared_tracking_issue_stays_open_when_shapes_not_read(self):
        """When shapes are NOT read (facts.shapes is None), the tracking issue
        is left as-is and not rewritten to 'none'."""
        # Set up breach in TARGET_REPO
        self.gh.subs[(TARGET_REPO, GOAL)] = [
            dict(epic(40), title="Epic: shared title"),
            dict(epic(41), title="Epic: shared  title"),
        ]
        self.run_at(T0)
        filed = self.gh.filed()
        issue_number = filed[0]["number"]
        original_body = filed[0]["body"]

        # Fix titles in TARGET_REPO
        self.gh.subs[(TARGET_REPO, GOAL)] = [
            dict(epic(40), title="Epic: first"),
            dict(epic(41), title="Epic: second"),
        ]

        # Simulate shapes not being read by mocking _invariant_facts to return shapes=None
        with mock.patch.object(station.Station, "_invariant_facts") as mock_facts:
            facts = invariants.Facts(now=T0 + invariants.CHECK_EVERY)
            facts.shapes = None  # Not read
            facts.unread = {}  # No unread repos for epic-title-shared
            mock_facts.return_value = facts
            self.run_at(T0 + invariants.CHECK_EVERY)

        # The tracking issue should NOT be rewritten (shapes not read)
        updated = self.gh.issues[issue_number]
        self.assertEqual(updated["body"], original_body, "Tracking issue unchanged when shapes not read")
        self.assertEqual(updated["state"], "open", "Tracking issue stays open when shapes not read")
        self.assertNotIn(invariants.ROLL_NONE, updated["body"])


if __name__ == "__main__":
    unittest.main()
