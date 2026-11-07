from __future__ import annotations

import calendar
import base64
import contextlib
import fcntl
import gzip
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from bus import __main__ as cli  # noqa: E402
from bus import answer, audit, brain, factory, fixrounds, invariants, proof, spend, station, status, tickets  # noqa: E402
from bus import lock, rebuttal, urgency  # noqa: E402
from bus import quiet as quiet_rule  # noqa: E402
from bus import github as github_module  # noqa: E402
from bus import watch as watch_module  # noqa: E402
from bus import labels as labels_module  # noqa: E402
from bus.github import AppToken, GitHub, GitHubError, RateLimited, TargetReached  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401
from bus import handoff  # noqa: E402

REPO = "example-owner/example-repo"
# A goal report this old is past its next reading time and grace whatever the
# time of day (brain.next_report_due is at most about 15 hours out).
REPORT_OVERDUE_AGE = 24 * 3600
AUTHORS = ["alice-example", "bob-example"]
OWNERS = ["example-owner"]
CLOSED_PULLS = "/repos/%s/pulls?state=closed&sort=updated&direction=desc&per_page=100"
BODY = "Difficulty: hard\nModel: model-x\n\n## Why\nBecause.\n\n## What\nDo it.\n"
# BODY with the criterion a dispatchable ticket carries (loom#2162): the
# station holds a build ticket naming no acceptance criteria for the brain,
# so a ticket a pass goes on to claim or brief names one. Classes whose
# passes dispatch their tickets set the fake's default_body to this; tests
# that want the criteria-free body pass BODY itself.
BODY_WITH_CRITERIA = (BODY + "\n## Acceptance criteria\n"
                      "- [ ] A test shows the thing is fixed.\n")


def reply(status=200, body=None, headers=None):
    raw = b"" if body is None else json.dumps(body).encode()
    return status, headers or {}, raw


def labeled(label, actor):
    return {"event": "labeled", "label": {"name": label}, "actor": {"login": actor}}


class FakeGitHub:
    """A tiny stateful issues API. Records every call as (method, path)."""

    def __init__(self):
        # The body add() gives a ticket that names none: BODY, the
        # criteria-free ticket only a reconcile or an answer settles. A
        # class whose passes go on to claim or brief the tickets it adds
        # sets this to BODY_WITH_CRITERIA in its setUp (loom#2162: a pass
        # holds a build ticket naming no acceptance criteria).
        self.default_body = BODY
        self.issues = {}
        self.events = {}
        self.comments = {}
        self.editors = {}
        self.pulls = []
        self.search_items = []
        # Pull requests read as issues: number -> the live issue object.
        self.pull_issues = {}
        self.calls = []
        self.version = 1
        self.lanes_issues = []
        self.status_comments = []
        self.created = []
        self.edits = []
        self.posts = []
        self.factory_incidents = []
        self.asks = []
        self.humans = []
        self.digest_issues = []
        self.pages = []
        self.page_error = False
        self.page_reply = None
        self.goals = []
        self.sub_issues = {}
        self.goal_error = False
        self.private = True
        self.threads = {}
        self.reply_fails = set()
        self.replies = []
        self.resolved = []
        self.open_issues = []
        self.bot = AUTHORS[0]
        # Issues of other repos, read by an After: line: (repo, number) -> issue.
        self.foreign = {}
        # Check runs per commit, files per pull request, the commits the
        # default branch contains, and a status every check-runs read answers.
        self.check_runs = {}
        self.pull_files = {}
        self.carried = set()
        self.check_runs_status = 200
        self.head_files = {}
        # Commit sha -> the files it changed, for a "Fixed in <sha>" answer.
        self.commit_files = {}
        # (thread id, cursor) -> the page of replies before that cursor.
        self.older_replies = {}
        # Strings a code search of the repo finds nothing for (bus/unmeetable.py);
        # every other search finds one file.
        self.code_absent = set()
        self.code_searches = []
        # The default branch's files the code search index skips (over
        # 384 KB): path -> text, read through the git trees and blobs API.
        self.big_files = {}
        self.tree_status = 200
        # The brain lock's commit message (bus/lock.py), or None for no lock.
        self.lock_message = None
        self.lock_error = False

    def add_foreign(self, repo, number, labels=("loom:pr",), state="open", reason=None):
        """An issue in another repo, as an After: line that names one reads it."""
        self.foreign[(repo, number)] = {
            "number": number, "title": "Elsewhere", "state": state, "state_reason": reason,
            "body": "", "user": {"login": "alice-example"},
            "labels": [{"name": "loom:ticket"}] + [{"name": x} for x in labels]}

    def add_goal(self, number, journals=(), title="Goal"):
        """journals: (login, age in seconds) pairs, posted as journal comments,
        or (login, age, holder) for a journal whose Tick line names its holder."""
        goal = {"number": number, "title": title, "state": "open",
                "labels": [{"name": "loom:goal"}]}
        self.goals.append(goal)
        self.issues[number] = dict(goal)
        self.comments[number] = []
        for login, age, *holder in journals:
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - age))
            tick = "Tick %s, %s" % (stamp, holder[0]) if holder else "Tick"
            self.comments[number].append({"body": "<!-- loom:journal v1 -->\n" + tick,
                                          "user": {"login": login}, "created_at": stamp})

    def _list_asks(self, headers, label):
        found = [a for a in self.asks if label in [x["name"] for x in a["labels"]]]
        etag = '"a%d"' % len(found)
        if headers.get("If-None-Match") == etag:
            return reply(304, None, {"ETag": etag})
        return reply(200, found, {"ETag": etag})

    def add_ask(self, number, title="Need a decision", author="alice-example",
                labeller="alice-example", urgent=False, body="", created="2030-01-01T00:00:00Z",
                label="needs-operator"):
        labels = [{"name": label}] + ([{"name": "loom:urgent"}] if urgent else [])
        self.asks.append({"number": number, "title": title, "user": {"login": author},
                          "labels": labels, "created_at": created, "body": body,
                          "html_url": "https://github.com/%s/issues/%d" % (REPO, number)})
        self.events[number] = [labeled(label, labeller)]

    def add_human(self, number, title="Buy the domain", ask="Buy it.", author="alice-example",
                created="2030-01-01T00:00:00Z", extra=()):
        """An open loom:human issue with the brain's ask comment by author."""
        self.humans.append({"number": number, "title": title, "user": {"login": author},
                            "labels": [{"name": "loom:human"}] + [{"name": x} for x in extra],
                            "created_at": created,
                            "html_url": "https://github.com/%s/issues/%d" % (REPO, number)})
        self.comments[number] = [{"body": "<!-- loom:human v1 -->\n%s" % ask,
                                  "user": {"login": author}}] if ask else []

    def add_open_issue(self, number, title="Open issue", body="It is slow.",
                       author="alice-example", created="2030-01-01T00:00:00Z"):
        """An open issue with no Loom labels, eligible for triage."""
        issue = {"number": number, "id": 1000 + number, "title": title, "body": body,
                 "state": "open", "user": {"login": author}, "created_at": created,
                 "labels": []}
        self.open_issues.append(issue)
        self.issues[number] = dict(issue)
        self.comments[number] = []
        self.events[number] = []

    def add(self, number, title="Fix the thing", lane="codex", body=None,
            author="alice-example", created="2030-01-01T00:00:00Z",
            events=None, editor=None, state="briefed", ghost_edit=False):
        if body is None:
            body = self.default_body
        self.issues[number] = {
            "number": number, "title": title, "body": body, "state": "open",
            "user": {"login": author}, "created_at": created,
            "updated_at": created,
            "labels": [{"name": "loom:ticket"}, {"name": "loom:" + state},
                       {"name": "lane:" + lane}],
        }
        self.events[number] = events if events is not None else [