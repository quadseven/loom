"""Shared fixtures for the invariant tests: the fake GitHub, the station base
class, and the small record builders. Not a test module itself."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain, invariants, metrics, station  # noqa: E402
from bus.github import GitHubError, Response  # noqa: E402
from bus.invariants import Check, Event, Facts, GateHold, Journal, Posted, Ref, Run, SilentRun, Wait  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401 - shadow off: these tests close

REPO = "example-owner/factory"
OTHER = "example-owner/hidden"
GOAL = 30
AUTHOR = "trusted-example"
T0 = 1_800_000_000.0
DAY_S = 24 * 3600


def stamp(at):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))


def journal(at, holder="muse", step="- Step status: failed (GitHub answered 502)"):
    return "\n".join(["<!-- loom:journal v1 -->", "Tick %s, %s" % (stamp(at), holder), step,
                      "- Shipped: none", "- Cost: 40 requests, 6 minutes"])


def ticket(number, state="open"):
    return {"number": number, "state": state, "labels": [{"name": "loom:ticket"}],
            "body": "Why."}


def epic(number, state="open"):
    return {"number": number, "state": state, "title": "Epic: part %d" % number,
            "labels": [{"name": "epic"}], "body": "Done when: x.\n\nPart of #%d\n" % GOAL}


class FakeGH:
    """The GitHub reads and writes the invariant check makes, on one repo."""

    def __init__(self):
        self.rate = {}
        self.issues = {}
        self.comments = {}
        self.goal_issues = {REPO: [{"number": GOAL, "title": "Loom keeps goals moving",
                                    "labels": [{"name": "loom:goal"}],
                                    "sub_issues_summary": {"total": 40}}]}
        self.pulls = {}
        # (repo, number) -> a pull request as GET .../pulls/N answers it.
        self.pull_state = {}
        self.check_runs = {}
        self.private = {OTHER}
        self.calls = []
        self.writes = []
        self.search_items = []
        # The open review threads the review bot left on a pull request.
        self.threads = []
        # (repo, issue number) -> its sub-issues, for the goal walk.
        self.subs = {}
        # repo -> its open loom:ticket issues.
        self.tickets = {}
        # Issues GitHub refuses a further sub-issue under (HTTP 422).
        self.refuse_subs = set()
        # Repos whose open pull request listing answers an error.
        self.pull_errors = set()

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
        # The goal walk (goal, epics, tickets).
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
        if method == "POST" and path == "/repos/%s/issues" % REPO:
            number = 100 + len(self.issues)
            self.issues[number] = dict(body, number=number, id=9000 + number, state="open",
                                       closed_at=None)
            return Response(201, {"number": number, "id": 9000 + number})
        if method == "GET" and path.startswith("/search/issues?"):
            return Response(200, {"items": self.search_items})
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
            # As GitHub answers: label objects, and when the issue was opened.
            return Response(200, dict(issue, created_at=issue.get("created_at", stamp(T0)),
                                      labels=[x if isinstance(x, dict) else {"name": x}
                                              for x in issue.get("labels", [])]))
        if m and method == "PATCH":
            self.issues[int(m.group(1))].update(body)
            return Response(200, {})
        m = re.fullmatch(r"/repos/[^/]+/[^/]+/issues/comments/(\d+)", path)
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
        m = re.fullmatch(r"/repos/([^/]+/[^/]+)/pulls\?state=open&per_page=100", path)
        if m and m.group(1) in self.pull_errors:
            raise GitHubError("Bad Gateway", 502)
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
        return {"repository": {"pullRequest": {"reviewThreads": {"nodes": self.threads}}}}

    def comment(self, number, at, body, repo=REPO, author=AUTHOR):
        self.comments.setdefault((repo, number), []).append(
            {"body": body, "created_at": stamp(at), "user": {"login": author}})


class StationBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = tmp.name
        self.gh = FakeGH()
        self.said = []
        self.settings = station.Settings(self.home, [REPO, OTHER], [AUTHOR], ["example-owner"],
                                         invariant_goal="%s#%d" % (REPO, GOAL), self_repo=REPO)

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

    def lane_ticket(self, number, labels=("loom:ticket", "loom:pr"), state="open"):
        """The issue that carries a lane pull request."""
        self.gh.issues[number] = {"number": number, "state": state, "body": "Why.",
                                  "labels": [{"name": n} for n in labels]}

    def lane_pull(self, number, ticket, opened, labels=(), author=AUTHOR):
        """An open, non-draft lane pull request opened at `opened`, on the
        branch its ticket number names."""
        self.gh.pulls.setdefault(REPO, []).append(
            {"number": number, "created_at": stamp(opened),
             "head": {"ref": "loom/%d-thing" % ticket, "sha": "%x" % number},
             "labels": [{"name": n} for n in labels], "user": {"login": author}})

    def bot_threads(self, count=1):
        """`count` unresolved review threads the review bot opened."""
        self.gh.threads = [{"id": "T%d" % i, "comments": {"nodes": [
            {"author": {"login": "grug-tribe[bot]"}, "body": "naming %d" % i}]}}
            for i in range(count)]

    def breaches(self):
        """The invariant issues this pass filed."""
        return [i for i in self.gh.filed() if invariants.LABEL in i.get("labels", [])]
