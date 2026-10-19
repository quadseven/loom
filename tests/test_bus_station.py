from __future__ import annotations

import calendar
import base64
import contextlib
import fcntl
import hashlib
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import __main__ as cli  # noqa: E402
from bus import audit, brain, factory, invariants, proof, spend, station, status, tickets  # noqa: E402
from bus import github as github_module  # noqa: E402
from bus import watch as watch_module  # noqa: E402
from bus.github import AppToken, GitHub, GitHubError  # noqa: E402

REPO = "example-owner/example-repo"
AUTHORS = ["alice-example", "bob-example"]
OWNERS = ["example-owner"]
CLOSED_PULLS = "/repos/%s/pulls?state=closed&sort=updated&direction=desc&per_page=100"
BODY = "Difficulty: hard\nModel: model-x\n\n## Why\nBecause.\n\n## What\nDo it.\n"


def reply(status=200, body=None, headers=None):
    raw = b"" if body is None else json.dumps(body).encode()
    return status, headers or {}, raw


def labeled(label, actor):
    return {"event": "labeled", "label": {"name": label}, "actor": {"login": actor}}


class FakeGitHub:
    """A tiny stateful issues API. Records every call as (method, path)."""

    def __init__(self):
        self.issues = {}
        self.events = {}
        self.comments = {}
        self.editors = {}
        self.pulls = []
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

    def add_foreign(self, repo, number, labels=("loom:pr",), state="open", reason=None):
        """An issue in another repo, as an After: line that names one reads it."""
        self.foreign[(repo, number)] = {
            "number": number, "title": "Elsewhere", "state": state, "state_reason": reason,
            "body": "", "user": {"login": "alice-example"},
            "labels": [{"name": "loom:ticket"}] + [{"name": x} for x in labels]}

    def add_goal(self, number, journals=(), title="Goal"):
        """journals: (login, age in seconds) pairs, posted as journal comments."""
        goal = {"number": number, "title": title, "state": "open",
                "labels": [{"name": "loom:goal"}]}
        self.goals.append(goal)
        self.issues[number] = dict(goal)
        self.comments[number] = [
            {"body": "<!-- loom:journal v1 -->\nTick", "user": {"login": login},
             "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - age))}
            for login, age in journals]

    def _list_asks(self, headers):
        etag = '"a%d"' % len(self.asks)
        if headers.get("If-None-Match") == etag:
            return reply(304, None, {"ETag": etag})
        return reply(200, self.asks, {"ETag": etag})

    def add_ask(self, number, title="Need a decision", author="alice-example",
                labeller="alice-example", urgent=False, body="", created="2030-01-01T00:00:00Z"):
        labels = [{"name": "needs-operator"}] + ([{"name": "loom:urgent"}] if urgent else [])
        self.asks.append({"number": number, "title": title, "user": {"login": author},
                          "labels": labels, "created_at": created, "body": body,
                          "html_url": "https://github.com/%s/issues/%d" % (REPO, number)})
        self.events[number] = [labeled("needs-operator", labeller)]

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

    def add(self, number, title="Fix the thing", lane="codex", body=BODY,
            author="alice-example", created="2030-01-01T00:00:00Z",
            events=None, editor=None, state="briefed", ghost_edit=False):
        self.issues[number] = {
            "number": number, "title": title, "body": body, "state": "open",
            "user": {"login": author}, "created_at": created,
            "updated_at": created,
            "labels": [{"name": "loom:ticket"}, {"name": "loom:" + state},
                       {"name": "lane:" + lane}],
        }
        self.events[number] = events if events is not None else [
            labeled("loom:briefed", "alice-example"), labeled("lane:" + lane, "bob-example")]
        self.comments[number] = []
        # A body edited by an account since deleted: GitHub reports the edit
        # time but no editor.
        self.editors[number] = "<ghost>" if ghost_edit else editor
        self.version += 1

    def names(self, number):
        return [lab["name"] for lab in self.issues[number]["labels"]]

    def paths(self, method=None):
        return [p for m, p in self.calls if method in (None, m)]

    def __call__(self, method, url, headers, body):
        parts = urlsplit(url)
        path = parts.path + ("?" + parts.query if parts.query else "")
        internal_validation_read = (parts.path.startswith("/repos/%s/contents/" % REPO)
                                    or "labels=loom:ticket" in parts.query
                                    or "labels=loom:goal" in parts.query)
        if not internal_validation_read:
            self.calls.append((method, path))
        data = json.loads(body) if body else None
        if method == "POST" and parts.path == "/graphql":
            return self._graphql(data)
        if method == "GET" and parts.path == "/repos/%s" % REPO:
            return reply(200, {"full_name": REPO, "private": self.private,
                               "default_branch": "main"})
        if method == "GET" and parts.path.startswith("/repos/%s/contents/" % REPO):
            if parts.path.endswith("/.github/workflows"):
                return reply(200, [])
            return reply(404, {"message": "Not Found"})
        other = re.match(r"/repos/([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)/issues/(\d+)$", parts.path)
        if other and other.group(1) != REPO:
            found = self.foreign.get((other.group(1), int(other.group(2))))
            return reply(200, found) if found else reply(404, {"message": "Not Found"})
        if (method == "GET" and parts.path == "/repos/%s/issues" % station.FACTORY_REPO
                and ("labels=loom%3Afactory" in parts.query or "labels=loom:factory" in parts.query)):
            return reply(200, self.factory_incidents)
        rest = re.match(r"/repos/%s/(.*)" % REPO, parts.path).group(1)
        if rest == "issues/900/comments" and method == "POST":
            return self._post_page(data)
        if rest == "issues" or re.match(r"issues/comments/\d+$", rest) \
                or (rest == "issues/1/comments" and self.lanes_issues):
            return self._issues_and_status(method, rest, parts.query, headers, data)
        gone = re.match(r"issues/(\d+)/labels/(.+)$", rest)
        if gone and method == "DELETE":
            n = int(gone.group(1))
            self.issues[n]["labels"] = [lab for lab in self.issues[n]["labels"]
                                        if lab["name"] != unquote(gone.group(2))]
            return reply(200, [])
        m = re.match(r"issues/(\d+)(?:/(\w+))?$", rest)
        if m:
            return self._issue_call(method, int(m.group(1)), m.group(2), data)
        if method == "POST" and rest == "labels":
            self.made_labels = getattr(self, "made_labels", []) + [data["name"]]
            return reply(201, data)
        if method == "GET" and rest == "pulls":
            return reply(200, self.pulls)
        if method == "GET" and rest.startswith("compare/"):
            base = rest[len("compare/"):].split("...", 1)[0]
            return reply(200, {"status": "ahead" if base in self.carried else "behind"})
        runs = re.match(r"commits/([0-9a-f]{40})/check-runs$", rest)
        if method == "GET" and runs:
            if self.check_runs_status != 200:
                return reply(self.check_runs_status, {"message": "refused"})
            found = self.check_runs.get(runs.group(1), [])
            return reply(200, {"total_count": len(found), "check_runs": found})
        files = re.match(r"pulls/(\d+)/files$", rest)
        if method == "GET" and files:
            return reply(200, self.pull_files.get(int(files.group(1)), []))
        if method == "GET" and rest == "git/ref/heads/loom-lock/brain":
            return reply(404, {"message": "Not Found"})
        raise AssertionError("unexpected call %s %s" % (method, path))

    def _post_page(self, data):
        if self.page_error:
            return reply(502, {"message": "down"})
        self.pages.append(data["body"])
        n = len(self.pages)
        made = {"id": n, "html_url": "https://github.com/%s/issues/900#issuecomment-%d" % (REPO, n)}
        return reply(201, self.page_reply if self.page_reply is not None else made)

    def _issues_and_status(self, method, rest, query, headers, data):
        if method == "GET" and rest == "issues":
            return self._get_issues(query, headers)
        if method == "POST" and rest == "issues" and data["labels"] == ["loom:digest"]:
            self.created.append(data)
            self.digest_issues = [{"number": 900, "labels": [{"name": "loom:digest"}]}]
            return reply(201, {"number": 900})
        if method == "POST" and rest == "issues":
            if (data.get("title") == "Add the repository validation contract"
                    or data.get("title", "").startswith(("Land or supersede #", "Epic: "))):
                number = max(self.issues.keys(), default=0) + 1
                made = {"number": number, "id": 1000 + number, "title": data["title"],
                        "body": data["body"], "state": "open",
                        "labels": [{"name": label} for label in data["labels"]],
                        "user": {"login": "alice-example"},
                        "html_url": "https://github.com/%s/issues/%d" % (REPO, number)}
                self.issues[number] = made
                self.events[number] = []
                self.comments[number] = []
                self.created.append(data)
                return reply(201, made)
            self.created.append(data)
            self.lanes_issues = [{"number": 1, "labels": [{"name": "loom:lanes"}]}]
            return reply(201, {"number": 1})
        if method == "PATCH":
            self.edits.append(data["body"])
            # GitHub answers a comment edit with the comment, naming its issue.
            return reply(200, getattr(self, "edit_answer",
                                      {"issue_url": "https://api.github.com/repos/%s/issues/1" % REPO}))
        if method == "GET":
            return reply(200, self.status_comments)
        self.status_comments.append({"id": 77, "body": data["body"]})
        self.posts.append(data["body"])
        return reply(201, {"id": 77})

    def _get_issues(self, query, headers):
        if "labels=loom:audit-failed&" in query:
            return reply(200, [i for i in self.issues.values()
                               if "loom:audit-failed" in self.names(i["number"])])
        if "labels=loom%3Apr" in query or "labels=loom:pr&" in query:
            return reply(200, [i for i in self.issues.values() if i["state"] == "open"
                               and "loom:pr" in self.names(i["number"])])
        if "labels=loom%3Amerged" in query or "labels=loom:merged&" in query:
            return reply(200, [i for i in self.issues.values() if i["state"] == "open"
                               and "loom:merged" in self.names(i["number"])])
        if "labels=loom:runaway" in query:
            return reply(200, [i for i in self.issues.values() if i["state"] == "open"
                               and "loom:runaway" in self.names(i["number"])])
        if "labels=loom:invariant" in query:
            return reply(200, [i for i in self.issues.values() if i["state"] == "open"
                               and "loom:invariant" in self.names(i["number"])])
        if "labels=loom%3Ashipped" in query or "labels=loom:shipped&" in query:
            return reply(200, [i for i in self.issues.values() if i["state"] == "open"
                               and "loom:shipped" in self.names(i["number"])])
        if "labels=loom%3Aticket" in query or "labels=loom:ticket&" in query:
            return reply(200, [i for i in self.issues.values() if i["state"] == "open"
                               and "loom:ticket" in self.names(i["number"])])
        if "needs-operator" in query:
            return self._list_asks(headers)
        if "loom:human" in query:
            return reply(200, self.humans)
        if "loom:goal" in query:
            return self._get_goals()
        if "labels=epic&" in query:
            return reply(200, list(getattr(self, "epics", [])))
        if "loom:lanes" in query:
            return reply(200, self.lanes_issues)
        if "loom:digest" in query:
            return reply(200, self.digest_issues)
        if "sort=created&direction=asc" in query:
            return reply(200, self.open_issues)
        return self._list_briefed(headers)

    def _get_goals(self):
        if self.goal_error:
            return reply(500, {"message": "down"})
        if self.goals:
            return reply(200, self.goals)
        found = [dict(i, html_url="https://github.com/%s/issues/%d" % (REPO, i["number"]))
                 for i in self.issues.values() if i["state"] == "open"
                 and "loom:goal" in self.names(i["number"])]
        return reply(200, found)

    def _graphql(self, data):
        query, variables = data["query"], data.get("variables") or {}
        if "addPullRequestReviewThreadReply" in query:
            if variables["id"] in self.reply_fails:
                return reply(502, {"message": "bad gateway"})
            self.replies.append((variables["id"], variables["body"]))
            return reply(200, {"data": {"addPullRequestReviewThreadReply": {"comment": {"id": "c"}}}})
        if "resolveReviewThread" in query:
            self.resolved.append(variables["id"])
            return reply(200, {"data": {"resolveReviewThread": {"thread": {"isResolved": True}}}})
        if "PullRequestReviewThread" in query:
            return reply(200, {"data": {"node": self.threads.get(variables["id"])}})
        editor = self.editors[data["variables"]["number"]]
        # GitHub's GraphQL names an App by its slug, typed Bot, without "[bot]".
        actor = ({"__typename": "Bot", "login": editor[:-len("[bot]")]}
                 if editor and editor.endswith("[bot]") else {"__typename": "User", "login": editor})
        return reply(200, {"data": {"repository": {"issue": {
            "editor": actor if editor and editor != "<ghost>" else None,
            "lastEditedAt": "2030-01-01T00:05:00Z" if editor else None}}}})

    def _list_briefed(self, headers):
        etag = '"v%d"' % self.version
        if headers.get("If-None-Match") == etag:
            return reply(304, None, {"ETag": etag})
        listed = [dict(i, repository_url="https://api.github.com/repos/" + REPO)
                  for i in self.issues.values() if i["state"] == "open"
                  and any(lab["name"] == "loom:briefed" for lab in i["labels"])]
        return reply(200, listed, {"ETag": etag})

    def _update_labels(self, method, n, data):
        # POST adds labels; PUT replaces them. Both update events.
        old = set(self.names(n))
        if method == "POST":
            new = list(self.names(n))
            for label in data["labels"]:
                if label not in old:
                    new.append(label)
        else:
            new = list(data["labels"])
        self.events.setdefault(n, [])
        self.events[n] += [labeled(x, "alice-example") for x in new if x not in old]
        self.issues[n]["labels"] = [{"name": x} for x in new]
        self.version += 1
        return reply(201 if method == "POST" else 200, [])

    def _get_issue(self, n, sub):
        if sub == "sub_issues":
            return reply(200, self.sub_issues.get(n, []))
        found = {None: self.issues, "events": self.events, "comments": self.comments}
        if sub in found:
            if sub is None and n not in found[sub]:
                return reply(404, {"message": "Not Found"})
            return reply(200, found[sub][n])
        return None

    def _patch_issue(self, n, data):
        for key in ("body", "state", "state_reason"):
            if key in data:
                self.issues[n][key] = data[key]
        return reply(200, {})

    def _issue_call(self, method, n, sub, data):
        if method == "GET":
            result = self._get_issue(n, sub)
            if result is not None:
                return result
        if method == "POST" and sub == "comments":
            self.comments[n].append({"body": data["body"], "user": {"login": self.bot}})
            return reply(201, {})
        if method == "PATCH" and sub is None:
            return self._patch_issue(n, data)
        if method == "POST" and sub == "sub_issues":
            self.sub_issues.setdefault(n, []).append(self.issues.get(data["sub_issue_id"] - 1000, {}))
            return reply(201, {})
        if method in ("POST", "PUT") and sub == "labels":
            return self._update_labels(method, n, data)
        if method == "POST" and sub == "assignees":
            have = self.issues[n].setdefault("assignees", [])
            have += [{"login": x} for x in data["assignees"]]
            return reply(201, self.issues[n])
        raise AssertionError("unexpected issue call %s %s/%s" % (method, n, sub))


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = os.path.join(self._tmp.name, "loom")
        self.fake = FakeGitHub()
        self.settings = station.Settings(self.home, [REPO], list(AUTHORS), list(OWNERS))
        self.heartbeat("codex")

    def heartbeat(self, lane, available=True, age=0, limited_models=None):
        d = os.path.join(self.home, "state", "usage")
        os.makedirs(d, exist_ok=True)
        beat = {"lane": lane, "at": int(time.time()) - age, "available": available, "reason": ""}
        if limited_models is not None:
            beat["limited_models"] = limited_models
        with open(os.path.join(d, lane + ".json"), "w") as fh:
            json.dump(beat, fh)

    def inbox(self, lane="codex", *sub):
        return os.path.join(self.home, "briefs", lane, *sub)

    def run_pass(self):
        gh = GitHub("token", transport=self.fake)
        self.assertEqual(station.run(self.settings, gh), 0)
        return gh

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)

    def listing(self, lane="codex"):
        try:
            return sorted(os.listdir(self.inbox(lane)))
        except OSError:
            return []


class StationReadBudgetTest(Base):
    def reader(self):
        return station.Station(GitHub("token", transport=self.fake), self.settings)

    def test_unchanged_ticket_reads_no_comments_or_events_on_second_pass(self):
        self.fake.add(12, state="shipped")
        first = self.reader()
        issue = first._tickets_open(REPO)[0]
        first._proven(REPO, 12)
        first._shipped_stamp(REPO, issue)
        first.end_pass()
        self.fake.calls.clear()
        second = self.reader()
        issue = second._tickets_open(REPO)[0]
        second._proven(REPO, 12)
        second._shipped_stamp(REPO, issue)
        paths = self.fake.paths("GET")
        self.assertFalse([p for p in paths if "/issues/12/comments" in p
                          or "/issues/12/events" in p])

    def test_changed_ticket_comments_are_read_once_per_pass(self):
        self.fake.add(12, state="shipped")
        first = self.reader()
        first._tickets_open(REPO)
        first._proven(REPO, 12)
        first.end_pass()
        self.fake.issues[12]["updated_at"] = "2030-01-02T00:00:00Z"
        self.fake.version += 1
        self.fake.calls.clear()
        second = self.reader()
        second._tickets_open(REPO)
        second._proven(REPO, 12)
        second._proven(REPO, 12)
        paths = self.fake.paths("GET")
        self.assertEqual(len([p for p in paths if "/issues/12/comments" in p]), 1)

    def test_a_comment_edit_on_another_issue_keeps_the_ticket_read(self):
        # The station edits its handoff packet and lane status every pass; that
        # must not throw away every other ticket's cached reads (loom#722).
        self.fake.add(12, state="shipped")
        first = self.reader()
        first._tickets_open(REPO)
        first._proven(REPO, 12)
        first.gh.rest("PATCH", "/repos/%s/issues/comments/901" % REPO, {"body": "packet"})
        first.end_pass()
        self.fake.calls.clear()
        second = self.reader()
        second._tickets_open(REPO)
        second._proven(REPO, 12)
        self.assertFalse([p for p in self.fake.paths("GET") if "/issues/12/comments" in p])

    def test_a_comment_edit_that_names_no_issue_still_drops_every_read(self):
        self.fake.add(12, state="shipped")
        self.fake.edit_answer = {}
        first = self.reader()
        first._tickets_open(REPO)
        first._proven(REPO, 12)
        first.gh.rest("PATCH", "/repos/%s/issues/comments/901" % REPO, {"body": "packet"})
        first.end_pass()
        self.fake.calls.clear()
        second = self.reader()
        second._tickets_open(REPO)
        second._proven(REPO, 12)
        self.assertTrue([p for p in self.fake.paths("GET") if "/issues/12/comments" in p])

    def test_comment_post_invalidates_the_ticket_read(self):
        self.fake.add(12, state="shipped")
        reader = self.reader()
        reader._tickets_open(REPO)
        reader._proven(REPO, 12)
        reader.gh.rest("POST", "/repos/%s/issues/12/comments" % REPO, {"body": "new proof"})
        reader._proven(REPO, 12)
        self.assertEqual(len([p for p in self.fake.paths("GET")
                              if "/issues/12/comments" in p]), 2)


class ReconcilePullRequestTest(Base):
    def test_trusted_same_repo_open_pr_promotes_queued_ticket(self):
        self.fake.add(12, state="queued")
        self.fake.pulls = [{"number": 99, "state": "open", "user": {"login": "alice-example"},
                            "body": "Refs %s#12" % REPO,
                            "head": {"ref": "loom/12-fix", "repo": {"full_name": REPO}}}]
        station.Station(GitHub("token", transport=self.fake), self.settings).reconcile_pull_requests()
        self.assertIn("loom:pr", self.fake.names(12))
        self.assertNotIn("loom:queued", self.fake.names(12))
        self.assertFalse(any("Linked pull request" in c["body"] for c in self.fake.comments[12]))

    def test_a_fix_round_for_the_open_pull_request_stays_briefed(self):
        # zippie#269, 2026-10-18: a brain fix round moved the ticket from pr
        # to briefed for a lane to fix pull request 99; this pass read the open
        # pull request as a new link, moved it back to pr and commented, and
        # the brain re-briefed it, every 3 to 6 minutes.
        self.fake.add(12, state="briefed", lane="codex")
        self.fake.comments[12] = [{"id": 501, "user": {"login": "alice-example"},
                                   "body": "%s\nPull request: %s#99, branch loom/12-fix\n\nFix the test."
                                           % (tickets.FIX_MARKER, REPO),
                                   "created_at": "2030-01-01T00:00:00Z",
                                   "updated_at": "2030-01-01T00:00:00Z"}]
        self.fake.pulls = [{"number": 99, "state": "open", "user": {"login": "alice-example"},
                            "body": "Refs %s#12" % REPO,
                            "head": {"ref": "loom/12-fix", "repo": {"full_name": REPO}}}]
        station.Station(GitHub("token", transport=self.fake), self.settings).reconcile_pull_requests()
        self.assertIn("loom:briefed", self.fake.names(12))
        self.assertNotIn("loom:pr", self.fake.names(12))
        self.assertEqual(len(self.fake.comments[12]), 1)

    def test_a_briefed_ticket_whose_fix_names_another_pull_request_is_linked(self):
        self.fake.add(12, state="briefed", lane="codex")
        self.fake.comments[12] = [{"id": 501, "user": {"login": "alice-example"},
                                   "body": "%s\nPull request: %s#98, branch loom/12-old\n\nFix."
                                           % (tickets.FIX_MARKER, REPO),
                                   "created_at": "2030-01-01T00:00:00Z",
                                   "updated_at": "2030-01-01T00:00:00Z"}]
        self.fake.pulls = [{"number": 99, "state": "open", "user": {"login": "alice-example"},
                            "body": "Refs %s#12" % REPO,
                            "head": {"ref": "loom/12-fix", "repo": {"full_name": REPO}}}]
        station.Station(GitHub("token", transport=self.fake), self.settings).reconcile_pull_requests()
        self.assertIn("loom:pr", self.fake.names(12))

    def contributor_pr(self, author="dana-example", state="open", merged_at=None):
        pr = {"number": 99, "state": state, "user": {"login": author},
              "body": "Why.\n\nRefs #12\n",
              "head": {"ref": "dana/12-fix", "repo": {"full_name": REPO}}}
        if merged_at:
            pr["merged_at"] = merged_at
        return pr

    def test_a_contributor_owned_ticket_moves_to_pr_then_merged(self):
        # loom#688: a ticket a second contributor claimed (assigned, or a
        # claim:<login> label) follows that contributor's pull request.
        for claim in ("assignee", "label"):
            with self.subTest(claim=claim):
                self.fake.add(12, state="queued")
                if claim == "assignee":
                    self.fake.issues[12]["assignees"] = [{"login": "dana-example"}]
                else:
                    self.fake.issues[12]["labels"].append({"name": "claim:dana-example"})
                self.fake.pulls = [self.contributor_pr()]
                runner = station.Station(GitHub("token", transport=self.fake), self.settings)
                runner.reconcile_pull_requests()
                self.assertIn("loom:pr", self.fake.names(12))
                self.assertNotIn("loom:queued", self.fake.names(12))
                self.fake.pulls = [self.contributor_pr(state="closed",
                                                       merged_at="2030-01-02T00:00:00Z")]
                station.Station(GitHub("token", transport=self.fake),
                                self.settings).reconcile_pull_requests()
                self.assertIn("loom:merged", self.fake.names(12))
                self.assertNotIn("loom:pr", self.fake.names(12))

    def test_a_pull_request_by_someone_who_does_not_own_the_ticket_moves_nothing(self):
        self.fake.add(12, state="queued")
        self.fake.issues[12]["assignees"] = [{"login": "dana-example"}]
        for author in ("stranger", "erin-example"):
            self.fake.pulls = [self.contributor_pr(author=author)]
            station.Station(GitHub("token", transport=self.fake), self.settings).reconcile_pull_requests()
            self.assertIn("loom:queued", self.fake.names(12))
        # A ticket a stranger filed is not trusted, even for its owner.
        self.fake.add(13, state="queued", author="stranger")
        self.fake.issues[13]["assignees"] = [{"login": "dana-example"}]
        pr = self.contributor_pr()
        pr["body"] = "Refs #13\n"
        self.fake.pulls = [pr]
        station.Station(GitHub("token", transport=self.fake), self.settings).reconcile_pull_requests()
        self.assertIn("loom:queued", self.fake.names(13))

    def test_untrusted_or_fork_pr_does_not_promote_ticket(self):
        for author, head_repo in (("stranger", REPO), ("alice-example", "fork-owner/example-repo")):
            with self.subTest(author=author, head_repo=head_repo):
                self.fake.add(12, state="queued")
                self.fake.pulls = [{"number": 99, "state": "open", "user": {"login": author},
                                    "body": "Refs %s#12" % REPO,
                                    "head": {"ref": "loom/12-fix",
                                             "repo": {"full_name": head_repo}}}]
                station.Station(GitHub("token", transport=self.fake), self.settings).reconcile_pull_requests()
                self.assertIn("loom:queued", self.fake.names(12))
                self.assertNotIn("loom:pr", self.fake.names(12))

    def test_briefed_ticket_promotes_but_running_ticket_is_untouched(self):
        pr = {"number": 99, "state": "open", "user": {"login": "alice-example"},
              "body": "Refs %s#12" % REPO,
              "head": {"ref": "loom/12-fix", "repo": {"full_name": REPO}}}
        self.fake.add(12, state="briefed")
        self.fake.pulls = [pr]
        station.Station(GitHub("token", transport=self.fake), self.settings).reconcile_pull_requests()
        self.assertIn("loom:pr", self.fake.names(12))
        self.fake.add(13, state="running")
        pr["body"] = "Refs %s#13" % REPO
        pr["head"]["ref"] = "loom/13-fix"
        station.Station(GitHub("token", transport=self.fake), self.settings).reconcile_pull_requests()
        self.assertIn("loom:running", self.fake.names(13))
        self.assertNotIn("loom:pr", self.fake.names(13))

    def test_merged_pr_repairs_failed_ticket_label_once(self):
        self.fake.add(12, state="pr")
        # The /pulls list endpoint returns merged_at but omits merged.
        self.fake.pulls = [{"number": 99, "state": "closed",
                            "merged_at": "2030-01-02T00:00:00Z",
                            "body": "Refs %s#12" % REPO,
                            "head": {"ref": "loom/12-fix-the-thing"}}]
        failed = [False]

        def transport(method, url, headers, body):
            if method == "PUT" and url.endswith("/issues/12/labels") and not failed[0]:
                failed[0] = True
                raise station.GitHubError("temporary failure", 500)
            return self.fake(method, url, headers, body)

        gh = GitHub("token", transport=transport)
        runner = station.Station(gh, self.settings)
        runner.reconcile_pull_requests()
        self.assertIn("loom:pr", self.fake.names(12))
        runner.reconcile_pull_requests()
        self.assertIn("loom:merged", self.fake.names(12))
        self.assertNotIn("loom:shipped", self.fake.names(12))
        self.assertNotIn("loom:verified", self.fake.names(12))
        puts = len([call for call in self.fake.calls if call[0] == "PUT"])
        runner.reconcile_pull_requests()
        self.assertEqual(puts, len([call for call in self.fake.calls if call[0] == "PUT"]))

    def test_a_failed_issues_fetch_is_logged_and_the_pass_survives(self):
        self.fake.add(12, state="pr")
        next_repo = "second-owner/second-repo"
        next_ticket = {"number": 13, "labels": [{"name": "loom:ticket"},
                                                 {"name": "loom:pr"}]}
        next_pr = {"number": 100, "state": "closed", "merged_at": "2030-01-02T00:00:00Z",
                   "body": "Refs %s#13" % next_repo, "head": {"ref": "loom/13-fix"}}
        writes = []
        requested = []

        def transport(method, url, headers, body):
            path = urlsplit(url).path
            requested.append(path)
            if path == "/repos/%s/issues" % REPO:
                raise station.GitHubError("server error", 500)
            if path == "/repos/%s/issues" % next_repo:
                return reply(200, [next_ticket])
            if path == "/repos/%s/pulls" % next_repo:
                return reply(200, [next_pr])
            if method == "PUT" and path == "/repos/%s/issues/13/labels" % next_repo:
                writes.append(json.loads(body)["labels"])
                return reply(200, [])
            return self.fake(method, url, headers, body)

        settings = station.Settings(self.home, [REPO, next_repo], list(AUTHORS), list(OWNERS))
        runner = station.Station(GitHub("token", transport=transport), settings)
        with mock.patch("bus.station._say") as say:
            runner.reconcile_pull_requests()
        say.assert_called_once()
        self.assertIn(REPO, say.call_args.args[0])
        self.assertIn("loom:pr", self.fake.names(12))
        self.assertNotIn("/repos/%s/pulls" % REPO, requested)
        self.assertEqual(writes, [["loom:ticket", "loom:merged"]])

    def test_a_failed_pulls_fetch_is_logged_and_the_pass_survives(self):
        self.fake.add(12, state="pr")

        def transport(method, url, headers, body):
            if "/pulls?" in url:
                raise station.GitHubError("server error", 500)
            return self.fake(method, url, headers, body)

        runner = station.Station(GitHub("token", transport=transport), self.settings)
        runner.reconcile_pull_requests()
        self.assertIn("loom:pr", self.fake.names(12))

    def test_closed_unmerged_pr_gets_distinct_disposition(self):
        self.fake.add(12, state="pr")
        self.fake.pulls = [{"number": 99, "state": "closed",
                            "merged_at": None,
                            "body": "Refs %s#12" % REPO,
                            "head": {"ref": "loom/12-fix-the-thing"}}]
        station.Station(GitHub("token", transport=self.fake), self.settings).reconcile_pull_requests()
        self.assertIn("loom:closed-unmerged", self.fake.names(12))
        self.assertNotIn("loom:merged", self.fake.names(12))

    def test_same_repo_short_refs_links_the_merged_pr(self):
        self.fake.add(12, state="pr")
        self.fake.pulls = [{"number": 99, "state": "closed",
                            "merged_at": "2030-01-02T00:00:00Z",
                            "body": "Why it matters.\n\nRefs #12\n",
                            "head": {"ref": "fix/stray-files"}}]
        station.Station(GitHub("token", transport=self.fake), self.settings).reconcile_pull_requests()
        self.assertIn("loom:merged", self.fake.names(12))

    def test_a_merged_pr_delivers_the_ticket_while_a_duplicate_is_open(self):
        # A hand-made PR merged the ticket's change while a lane's PR for the
        # same ticket was still taking fix rounds.
        self.fake.add(12, state="pr")
        self.fake.pulls = [{"number": 98, "state": "open", "body": "Refs %s#12" % REPO,
                            "head": {"ref": "loom/12-fix-the-thing-r2"}},
                           {"number": 99, "state": "closed",
                            "merged_at": "2030-01-02T00:00:00Z",
                            "body": "Refs #12", "head": {"ref": "fix/stray-files"}}]
        station.Station(GitHub("token", transport=self.fake), self.settings).reconcile_pull_requests()
        self.assertIn("loom:merged", self.fake.names(12))
        self.assertNotIn("loom:pr", self.fake.names(12))

    def test_old_merge_does_not_deliver_a_requeued_regression(self):
        self.fake.add(12, state="pr")
        self.fake.issues[12]["labels"].append({"name": "loom:regression"})
        self.fake.events[12].append({"event": "labeled", "label": {"name": "loom:briefed"},
                                     "created_at": "2030-01-03T00:00:00Z"})
        self.fake.pulls = [{"number": 98, "state": "open", "body": "Refs #12",
                            "head": {"ref": "loom/12-fix-r2"}},
                           {"number": 97, "state": "closed",
                            "merged_at": "2030-01-02T00:00:00Z", "body": "Refs #12",
                            "head": {"ref": "loom/12-fix"}}]
        station.Station(GitHub("token", transport=self.fake), self.settings).reconcile_pull_requests()
        self.assertIn("loom:pr", self.fake.names(12))
        self.assertNotIn("loom:merged", self.fake.names(12))

    def test_merge_after_regression_requeue_delivers_ticket(self):
        self.fake.add(12, state="pr")
        self.fake.issues[12]["labels"].append({"name": "loom:regression"})
        self.fake.events[12].append({"event": "labeled", "label": {"name": "loom:briefed"},
                                     "created_at": "2030-01-02T00:00:00Z"})
        self.fake.pulls = [{"number": 99, "state": "closed",
                            "merged_at": "2030-01-03T00:00:00Z", "body": "Refs #12"}]
        station.Station(GitHub("token", transport=self.fake), self.settings).reconcile_pull_requests()
        self.assertIn("loom:merged", self.fake.names(12))

    def test_a_closed_unmerged_pr_leaves_a_ticket_with_an_open_pr_alone(self):
        self.fake.add(12, state="pr")
        self.fake.pulls = [{"number": 98, "state": "open", "body": "Refs %s#12" % REPO,
                            "head": {"ref": "loom/12-fix-the-thing-r2"}},
                           {"number": 97, "state": "closed", "merged_at": None,
                            "body": "Refs %s#12" % REPO, "head": {"ref": "loom/12-fix-the-thing"}}]
        station.Station(GitHub("token", transport=self.fake), self.settings).reconcile_pull_requests()
        self.assertIn("loom:pr", self.fake.names(12))
        self.assertNotIn("loom:closed-unmerged", self.fake.names(12))

    def merged_pr(self, number=12, author="alice-example", head_repo=REPO):
        return {"number": 99, "state": "closed", "merged_at": "2030-01-02T00:00:00Z",
                "user": {"login": author}, "body": "Why.\n\nRefs #%d\n" % number,
                "head": {"ref": "fix/%d" % number, "repo": {"full_name": head_repo}}}

    def test_a_pull_request_merged_while_the_ticket_waits_moves_it_to_merged(self):
        # loom#784: loom#633 sat at briefed after a fix round, its pull
        # request merged, and the ticket kept climbing the ladder.
        for state in ("briefed", "queued"):
            with self.subTest(state=state):
                self.fake.add(12, state=state)
                self.fake.pulls = [self.merged_pr()]
                station.Station(GitHub("token", transport=self.fake),
                                self.settings).reconcile_pull_requests()
                self.assertIn("loom:merged", self.fake.names(12))
                self.assertNotIn("loom:" + state, self.fake.names(12))
                self.assertIn("Pull request #99 merged while this ticket was at loom:" + state,
                              self.fake.comments[12][-1]["body"])

    def test_a_merged_pull_request_that_may_not_deliver_moves_nothing(self):
        cases = {
            "untrusted author": self.merged_pr(author="stranger"),
            "fork": self.merged_pr(head_repo="fork-owner/example-repo"),
            "requeued after the merge": self.merged_pr(),
            "another ticket": self.merged_pr(number=13),
        }
        for name, pr in cases.items():
            with self.subTest(name):
                self.fake.add(12, state="briefed")
                if name == "requeued after the merge":
                    self.fake.events[12].append(
                        {"event": "labeled", "label": {"name": "loom:briefed"},
                         "actor": {"login": "alice-example"},
                         "created_at": "2030-01-03T00:00:00Z"})
                self.fake.pulls = [pr]
                station.Station(GitHub("token", transport=self.fake),
                                self.settings).reconcile_pull_requests()
                self.assertIn("loom:briefed", self.fake.names(12))
                self.assertNotIn("loom:merged", self.fake.names(12))
                self.assertEqual(self.fake.comments[12], [])

    def test_a_contributors_merged_pull_request_delivers_the_queued_ticket_they_own(self):
        self.fake.add(12, state="queued")
        self.fake.issues[12]["labels"].append({"name": "claim:dana-example"})
        self.fake.pulls = [self.contributor_pr(state="closed", merged_at="2030-01-02T00:00:00Z")]
        station.Station(GitHub("token", transport=self.fake), self.settings).reconcile_pull_requests()
        self.assertIn("loom:merged", self.fake.names(12))
        self.assertNotIn("loom:queued", self.fake.names(12))
        # The same pull request by someone who does not own the ticket.
        self.fake.add(13, state="queued")
        pr = self.contributor_pr(author="erin-example", state="closed",
                                 merged_at="2030-01-02T00:00:00Z")
        pr["body"] = "Refs #13\n"
        self.fake.pulls = [pr]
        station.Station(GitHub("token", transport=self.fake), self.settings).reconcile_pull_requests()
        self.assertIn("loom:queued", self.fake.names(13))

    def test_a_refs_line_for_another_repo_does_not_link(self):
        self.fake.add(12, state="pr")
        self.fake.pulls = [{"number": 99, "state": "closed",
                            "merged_at": "2030-01-02T00:00:00Z",
                            "body": "Refs other-owner/other-repo#12",
                            "head": {"ref": "fix/elsewhere"}}]
        station.Station(GitHub("token", transport=self.fake), self.settings).reconcile_pull_requests()
        self.assertIn("loom:pr", self.fake.names(12))


class AdoptUntrackedPullRequestTest(Base):
    def setUp(self):
        super().setUp()
        self.fake.add_goal(30)

    def pull(self, number, author="alice-example", head_repo=REPO, ref="fix/stray-files",
             body="", draft=False, created="2030-01-01T00:00:00Z", title="Tidy the docs"):
        self.fake.comments.setdefault(number, [])
        pr = {"number": number, "state": "open", "title": title, "user": {"login": author},
              "body": body, "draft": draft, "created_at": created,
              "head": {"ref": ref, "repo": {"full_name": head_repo}},
              "base": {"ref": "main", "repo": {"full_name": REPO}}}
        self.fake.pulls.append(pr)
        return pr

    def adopt(self, now=None):
        station.Station(GitHub("token", transport=self.fake), self.settings,
                        now=now).adopt_untracked_pulls()

    def comment_reads(self, number):
        return self.fake.paths("GET").count("/repos/%s/issues/%d/comments?per_page=100"
                                            % (REPO, number))

    def adoptions(self):
        return [c for c in self.fake.created if c["title"].startswith("Land or supersede #")]

    def markers(self, number):
        return [c["body"] for c in self.fake.comments[number]
                if station.ADOPTED_RE.search(c["body"])]

    def test_an_untracked_pull_request_gets_one_ticket_and_one_marker(self):
        self.fake.add_open_issue(5, title="A plain issue")
        self.pull(99, body="Why it matters.\n\nRefs #5\n")
        self.run_pass()
        made = self.adoptions()
        self.assertEqual([c["title"] for c in made], ["Land or supersede #99: Tidy the docs"])
        self.assertEqual(made[0]["labels"], ["loom:ticket", "loom:queued"])
        body = made[0]["body"]
        self.assertTrue(body.startswith("Difficulty: medium\n\n## Why\n"))
        for section in ("## What", "## Acceptance criteria", "## Size", "## Out of scope",
                        "## Definition of done"):
            self.assertIn(section, body)
        # Filed under the goal's intake epic, which the station opened.
        intake = [c for c in self.fake.created if c["title"] == brain.INTAKE_TITLE]
        self.assertEqual(len(intake), 1)
        self.assertRegex(intake[0]["body"], r"(?m)^Part of #30$")
        epic_number = next(n for n, i in self.fake.issues.items()
                           if i.get("title") == brain.INTAKE_TITLE)
        self.assertRegex(body, r"(?m)^Part of #%d$" % epic_number)
        self.assertNotRegex(body, r"(?m)^Part of #30$")
        self.assertNotRegex(body, r"(?m)^Refs ")
        ticket = max(self.fake.issues)
        self.assertEqual(self.markers(99), [station.adoption_comment(REPO, ticket)])
        self.assertIn("<!-- loom:adopted v1 ticket=%s#%d -->" % (REPO, ticket),
                      self.markers(99)[0])
        # The next pass scans the repo again (an idle repo waits IDLE_SCAN).
        os.remove(os.path.join(self.home, "state", "station", "scans.json"))
        pulls_read = self.fake.paths("GET").count("/repos/%s/pulls?state=open&per_page=100" % REPO)
        self.run_pass()
        self.assertGreater(self.fake.paths("GET").count(
            "/repos/%s/pulls?state=open&per_page=100" % REPO), pulls_read)
        self.assertEqual(len(self.adoptions()), 1)
        self.assertEqual(len(self.markers(99)), 1)

    def test_fork_untrusted_draft_and_linked_pull_requests_are_not_adopted(self):
        self.fake.add(12, state="queued")
        cases = (("fork", dict(head_repo="fork-owner/example-repo")),
                 ("untrusted", dict(author="stranger")),
                 ("draft", dict(draft=True)),
                 ("full refs", dict(body="Refs %s#12" % REPO)),
                 ("short refs", dict(body="Why.\n\nRefs #12\n")),
                 ("loom branch", dict(ref="loom/12-fix-the-thing")))
        for name, kwargs in cases:
            with self.subTest(name):
                self.fake.pulls = []
                self.pull(99, **kwargs)
                self.adopt()
                self.assertEqual(self.adoptions(), [])
                self.assertEqual(self.fake.comments[99], [])

    def test_one_adoption_a_pass_per_repo_oldest_pull_request_first(self):
        self.pull(98, created="2030-01-05T00:00:00Z", title="Newer")
        self.pull(97, created="2030-01-02T00:00:00Z", title="Older")
        self.adopt()
        self.assertEqual([c["title"] for c in self.adoptions()],
                         ["Land or supersede #97: Older"])
        self.assertEqual(self.fake.comments[98], [])
        self.adopt()
        self.assertEqual([c["title"] for c in self.adoptions()],
                         ["Land or supersede #97: Older", "Land or supersede #98: Newer"])
        self.adopt()
        self.assertEqual(len(self.adoptions()), 2)

    def test_a_trusted_marker_comment_stops_adoption_and_an_untrusted_one_does_not(self):
        marker = station.adoption_comment(REPO, 7)
        self.pull(98, created="2030-01-01T00:00:00Z", title="Marked by a trusted author")
        self.pull(99, created="2030-01-02T00:00:00Z", title="Marked by a stranger")
        self.fake.comments[98] = [{"body": marker, "user": {"login": "bob-example"}}]
        self.fake.comments[99] = [{"body": marker, "user": {"login": "stranger"}}]
        self.adopt()
        self.assertEqual([c["title"] for c in self.adoptions()],
                         ["Land or supersede #99: Marked by a stranger"])
        self.assertEqual(len(self.fake.comments[98]), 1)

    def test_an_adopted_pull_requests_comments_are_read_once(self):
        self.pull(97, created="2030-01-01T00:00:00Z", title="Adopted before")
        self.fake.comments[97] = [{"body": station.adoption_comment(REPO, 7),
                                   "user": {"login": "alice-example"}}]
        self.pull(98, created="2030-01-02T00:00:00Z", title="Untracked")
        self.adopt()
        self.assertEqual([c["title"] for c in self.adoptions()],
                         ["Land or supersede #98: Untracked"])
        self.assertEqual((self.comment_reads(97), self.comment_reads(98)), (1, 1))
        self.adopt()
        self.adopt()
        self.assertEqual((self.comment_reads(97), self.comment_reads(98)), (1, 1))
        self.assertEqual(len(self.adoptions()), 1)

    def test_a_refused_brief_waits_before_reading_or_briefing_again(self):
        self.pull(99)
        start = time.time()
        with mock.patch.object(brain, "brief",
                               side_effect=ValueError("goal already has 8 tickets in flight")) as made, \
                mock.patch("bus.station._say") as say:
            self.adopt(now=start)
            self.adopt(now=start + 60)
            self.adopt(now=start + station.ADOPT_RETRY - 1)
            self.assertEqual((made.call_count, self.comment_reads(99)), (1, 1))
            self.adopt(now=start + station.ADOPT_RETRY + 1)
            self.assertEqual((made.call_count, self.comment_reads(99)), (2, 2))
        refused = [c.args[0] for c in say.call_args_list if "in flight" in c.args[0]]
        self.assertEqual(len(refused), 1)
        self.assertEqual(self.fake.comments[99], [])

    def test_a_closing_keyword_in_the_title_files_the_ticket_without_it(self):
        # Built here, so this file never holds a closing keyword next to an issue number.
        self.pull(99, title="%s #3 for good" % "Fixes")
        self.adopt()
        self.assertEqual([c["title"] for c in self.adoptions()], ["Land or supersede #99"])

    def test_a_repo_without_a_goal_home_files_nothing_and_logs_once(self):
        self.fake.goals = []
        del self.fake.issues[30]
        self.pull(99)
        with mock.patch("bus.station._say") as say:
            self.adopt()
            self.adopt()
        refused = [c.args[0] for c in say.call_args_list if "no goal home" in c.args[0]]
        self.assertEqual(len(refused), 1)
        self.assertEqual(self.adoptions(), [])
        self.assertEqual(self.fake.comments[99], [])

    def test_a_failed_pulls_fetch_is_logged_and_the_pass_survives(self):
        self.pull(99)

        def transport(method, url, headers, body):
            if "/pulls?" in url:
                raise station.GitHubError("server error", 500)
            return self.fake(method, url, headers, body)

        runner = station.Station(GitHub("token", transport=transport), self.settings)
        with mock.patch("bus.station._say") as say:
            runner.adopt_untracked_pulls()
        say.assert_called_once()
        self.assertIn("could not adopt pull requests for %s" % REPO, say.call_args.args[0])
        self.assertEqual(self.adoptions(), [])


DEPLOYED = "d" * 40
DEPLOYED_AT = calendar.timegm((2030, 1, 3, 12, 0, 0))


class ShipDeployedTest(Base):
    """Loom's own merged tickets move to loom:shipped once the station has
    deployed a revision that carries their pull request's merge commit."""

    def setUp(self):
        super().setUp()
        self.settings.self_repo = REPO
        self.carried = set()

    def record_deploy(self, revision=DEPLOYED, at=DEPLOYED_AT):
        self.write(os.path.join(self.home, "state", "station", "deployed.json"),
                   json.dumps({"revision": revision, "at": at}))

    def merged(self, number, pr, merged_at="2030-01-02T00:00:00Z", carried=True):
        """A merged ticket and its merged pull request, whose merge commit the
        deployed revision carries when carried is true."""
        self.fake.add(number, state="merged")
        sha = "%040x" % pr
        self.fake.pulls.append({"number": pr, "state": "closed", "merged_at": merged_at,
                                "merge_commit_sha": sha, "body": "Refs %s#%d" % (REPO, number),
                                "head": {"ref": "loom/%d-fix" % number}})
        if carried:
            self.carried.add(sha)

    def ancestor(self, commit, revision):
        return revision == DEPLOYED and commit in self.carried

    def ship(self, transport=None):
        gh = GitHub("token", transport=transport or self.fake)
        station.Station(gh, self.settings, ancestor=self.ancestor).ship_deployed()

    def test_a_ticket_the_deployed_revision_carries_ships_with_the_revision_in_its_note(self):
        self.merged(12, 99)
        self.record_deploy()
        with mock.patch("bus.station._say") as say:
            self.ship()
        self.assertIn("loom:shipped", self.fake.names(12))
        self.assertNotIn("loom:merged", self.fake.names(12))
        note = self.fake.comments[12][-1]["body"]
        self.assertIn(DEPLOYED[:12], note)
        self.assertIn("2030-01-03 12:00:00Z", note)
        self.assertIn("#99", note)
        say.assert_called_once_with("shipped %s#12: deployed %s carries #99" % (REPO, DEPLOYED[:12]))

    def test_a_ticket_whose_commit_is_not_yet_deployed_stays_merged(self):
        self.merged(12, 99, carried=False)
        self.merged(13, 100)
        self.record_deploy()
        self.ship()
        self.assertIn("loom:merged", self.fake.names(12))
        self.assertNotIn("loom:shipped", self.fake.names(12))
        self.assertEqual(self.fake.comments[12], [])
        self.assertIn("loom:shipped", self.fake.names(13))

    def test_a_ticket_whose_prs_are_all_hand_reverted_does_not_ship(self):
        self.merged(12, 99)
        rsha = "%040x" % 777
        self.fake.pulls.append({"number": 101, "state": "closed",
                                "merged_at": "2030-01-02T01:00:00Z",
                                "merge_commit_sha": rsha, "title": 'Revert "Change"',
                                "body": "git revert %s\n\n## Why\nIt broke the build.\n"
                                        % ("%040x" % 99),
                                "head": {"ref": "revert-101"}, "base": {"ref": "main"}})
        self.carried.add(rsha)
        self.record_deploy()
        with mock.patch("bus.station._say") as say:
            self.ship()
        self.assertIn("loom:merged", self.fake.names(12))
        self.assertNotIn("loom:shipped", self.fake.names(12))
        say.assert_not_called()

    def test_without_a_successful_deploy_nothing_moves(self):
        self.merged(12, 99)
        self.ship()
        self.assertIn("loom:merged", self.fake.names(12))
        self.assertEqual(self.fake.calls, [])
        # A record that is not a deploy (a torn write) moves nothing either.
        self.record_deploy(revision="not-a-commit")
        self.ship()
        self.assertIn("loom:merged", self.fake.names(12))
        self.assertEqual(self.fake.calls, [])

    def test_a_ticket_in_another_repo_is_never_moved(self):
        other = "second-owner/second-repo"
        ticket = {"number": 13, "state": "open",
                  "labels": [{"name": "loom:ticket"}, {"name": "loom:merged"}]}
        pr = {"number": 100, "state": "closed", "merged_at": "2030-01-02T00:00:00Z",
              "merge_commit_sha": "%040x" % 100, "body": "Refs %s#13" % other,
              "head": {"ref": "loom/13-fix"}}
        self.carried.add(pr["merge_commit_sha"])
        touched = []

        def transport(method, url, headers, body):
            path = urlsplit(url).path
            if path.startswith("/repos/%s/" % other):
                touched.append((method, path))
                return reply(200, [pr] if "/pulls" in path else [ticket])
            return self.fake(method, url, headers, body)

        self.settings.repos = [REPO, other]
        self.record_deploy()
        self.ship(transport)
        self.assertEqual(touched, [])
        # With no repo of its own to ship, the station moves nothing at all.
        self.settings.self_repo = ""
        self.merged(12, 99)
        self.ship(transport)
        self.assertIn("loom:merged", self.fake.names(12))
        self.assertEqual(touched, [])

    def test_at_most_twenty_move_a_pass_oldest_merge_first(self):
        for n in range(22):
            self.merged(100 + n, 500 + n, merged_at="2030-01-02T00:%02d:00Z" % (59 - n))
        self.record_deploy()
        self.ship()
        left = [n for n in range(100, 122) if "loom:merged" in self.fake.names(n)]
        self.assertEqual(left, [100, 101])
        self.ship()
        self.assertTrue(all("loom:shipped" in self.fake.names(n) for n in range(100, 122)))

    def test_a_pass_ships_after_the_deploy(self):
        self.merged(12, 99)
        self.record_deploy()
        gh = GitHub("token", transport=self.fake)
        self.assertEqual(station.run(self.settings, gh, ancestor=self.ancestor), 0)
        self.assertIn("loom:shipped", self.fake.names(12))

    def test_self_repo_defaults_to_the_lanes_repo(self):
        env = {"LOOM_HOME": self.home, "LOOM_REPOS": REPO,
               "LOOM_STATUS_REPO": "example-owner/lanes"}
        self.assertEqual(station.Settings.from_env(env).self_repo, "example-owner/lanes")
        env["LOOM_SELF_REPO"] = REPO
        self.assertEqual(station.Settings.from_env(env).self_repo, REPO)

    def test_the_default_ancestry_check_reads_this_checkout(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        head = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"],
                              capture_output=True, text=True)
        if head.returncode != 0:
            self.skipTest("not a git checkout")
        sha = head.stdout.strip()
        self.assertTrue(station._is_ancestor(sha, sha))
        self.assertFalse(station._is_ancestor("0" * 40, sha))


class ShipRuleTest(Base):
    """A repo nothing deploys ships at merge (LOOM_SHIP_ON_MERGE); a repo
    with its own deploy workflow ships when that workflow succeeds on the
    merge commit (LOOM_SHIP_WORKFLOWS)."""

    WORKFLOW = "deploy.yml"

    def setUp(self):
        super().setUp()
        self.runs = []
        self.carried = set()
        self.compares = []
        self.queries = []

    def merged(self, number, pr, merged_at="2030-01-02T00:00:00Z"):
        self.fake.add(number, state="merged")
        sha = "%040x" % pr
        self.fake.pulls.append({"number": pr, "state": "closed", "merged_at": merged_at,
                                "merge_commit_sha": sha, "title": "Fix the thing",
                                "body": "Refs %s#%d" % (REPO, number),
                                "head": {"ref": "loom/%d-fix" % number}})
        return sha

    def deploy_run(self, sha, conclusion="success", branch="main",
                   created="2030-01-02T01:00:00Z", ident=1, event="push", head_repo=REPO):
        self.runs.append({"id": ident, "head_sha": sha, "status": "completed",
                          "conclusion": conclusion, "head_branch": branch,
                          "created_at": created, "event": event,
                          "head_repository": {"full_name": head_repo}})

    def transport(self, method, url, headers, body):
        parts = urlsplit(url)
        if parts.path == "/repos/%s/actions/workflows/%s/runs" % (REPO, self.WORKFLOW):
            self.queries.append(parse_qs(parts.query))
            # GitHub filters on status alone here, so the station's own
            # check of event, branch and head repository is what is tested.
            return reply(200, {"workflow_runs": [r for r in self.runs
                                                 if r["conclusion"] == "success"]})
        compare = re.match(r"/repos/%s/compare/([0-9a-f]+)\.\.\.([0-9a-f]+)$" % REPO,
                           parts.path)
        if compare:
            self.compares.append(compare.groups())
            base, head = compare.groups()
            return reply(200, {"status": "ahead" if (base, head) in self.carried
                               else "diverged"})
        if method == "GET" and parts.path == "/repos/%s" % REPO:
            return reply(200, {"full_name": REPO, "default_branch": "main"})
        return self.fake(method, url, headers, body)

    def ship(self):
        gh = GitHub("token", transport=self.transport)
        st = station.Station(gh, self.settings)
        st.ship_merged()
        st.ship_workflows()

    def test_a_ship_on_merge_repo_ships_its_merged_tickets(self):
        self.settings.ship_on_merge = [REPO]
        self.merged(12, 99)
        self.ship()
        self.assertIn("loom:shipped", self.fake.names(12))
        self.assertNotIn("loom:merged", self.fake.names(12))

    def test_an_unlisted_repo_does_not_ship_on_merge(self):
        self.merged(12, 99)
        self.ship()
        self.assertIn("loom:merged", self.fake.names(12))
        self.assertNotIn("loom:shipped", self.fake.names(12))

    def test_a_workflow_repo_ships_on_a_successful_run_at_the_merge_commit(self):
        self.settings.ship_workflows = {REPO: self.WORKFLOW}
        sha = self.merged(12, 99)
        self.deploy_run(sha)
        self.ship()
        self.assertIn("loom:shipped", self.fake.names(12))

    def test_a_failed_run_or_another_commit_does_not_ship(self):
        self.settings.ship_workflows = {REPO: self.WORKFLOW}
        sha = self.merged(12, 99)
        self.merged(13, 100)
        self.deploy_run(sha, conclusion="failure", ident=1)
        self.deploy_run("f" * 40, ident=2)
        self.ship()
        self.assertIn("loom:merged", self.fake.names(12))
        self.assertIn("loom:merged", self.fake.names(13))

    def test_runs_are_asked_for_as_successful_pushes_to_the_default_branch(self):
        self.settings.ship_workflows = {REPO: self.WORKFLOW}
        self.merged(12, 99)
        self.ship()
        self.assertEqual(self.queries, [{"status": ["success"], "event": ["push"],
                                         "branch": ["main"], "per_page": ["30"]}])

    def test_a_pull_request_or_fork_run_at_the_merge_commit_does_not_ship(self):
        # A pull request run, or one whose head repository is a fork, can sit
        # at the merge commit's SHA without deploying anything.
        self.settings.ship_workflows = {REPO: self.WORKFLOW}
        for number, run in ((12, {"event": "pull_request"}),
                            (13, {"head_repo": "someone-else/example-repo"}),
                            (14, {"branch": "feature"})):
            with self.subTest(run=run):
                sha = self.merged(number, 90 + number)
                self.deploy_run(sha, ident=number, **run)
        self.ship()
        for number in (12, 13, 14):
            self.assertIn("loom:merged", self.fake.names(number))
        self.assertEqual(self.compares, [])

    def test_a_fork_run_never_stands_in_for_the_newest_deploy(self):
        self.settings.ship_workflows = {REPO: self.WORKFLOW}
        sha = self.merged(12, 99)
        self.deploy_run("f" * 40, created="2030-01-02T03:00:00Z",
                        head_repo="someone-else/example-repo")
        self.carried.add((sha, "f" * 40))
        self.ship()
        self.assertIn("loom:merged", self.fake.names(12))

    def test_a_merge_the_newest_deploy_carries_ships(self):
        # A merge outside the workflow's path filter starts no run of its
        # own; it ships with the next deploy whose revision carries it.
        self.settings.ship_workflows = {REPO: self.WORKFLOW}
        sha = self.merged(12, 99)
        self.deploy_run("f" * 40, created="2030-01-02T03:00:00Z")
        self.carried.add((sha, "f" * 40))
        self.ship()
        self.assertIn("loom:shipped", self.fake.names(12))

    def test_a_deploy_older_than_the_merge_is_not_asked(self):
        self.settings.ship_workflows = {REPO: self.WORKFLOW}
        self.merged(12, 99, merged_at="2030-01-02T05:00:00Z")
        self.deploy_run("f" * 40, created="2030-01-02T03:00:00Z")
        self.ship()
        self.assertIn("loom:merged", self.fake.names(12))
        self.assertEqual(self.compares, [])

    def test_ship_rules_come_from_the_environment(self):
        env = {"LOOM_HOME": self.home, "LOOM_REPOS": REPO,
               "LOOM_SHIP_ON_MERGE": REPO + " example-owner/second",
               "LOOM_SHIP_WORKFLOWS": "%s=deploy.k8s.yml not-a-pair example-owner/x=run.sh" % REPO}
        with mock.patch("bus.station._say"):
            settings = station.Settings.from_env(env)
        self.assertEqual(settings.ship_on_merge, [REPO, "example-owner/second"])
        self.assertEqual(settings.ship_workflows, {REPO: "deploy.k8s.yml"})


DEPLOY = "example-owner/deploy"
PUBLISHED = "e" * 40
PIN_MERGED = "2030-01-03T12:00:00Z"
PIN_MERGED_AT = calendar.timegm((2030, 1, 3, 12, 0, 0))


class ShipPinTest(Base):
    """A merged pin pull request in a deployment repo ships the merged
    tickets of the source revision its published-from comment names."""

    def setUp(self):
        super().setUp()
        self.settings.pin_ship_repos = [DEPLOY]
        self.carried = set()
        self.pins = []
        self.files = {}
        self.reads = []

    def merged(self, number, pr, merged_at="2030-01-02T00:00:00Z", carried=True,
               title="Fix the thing", body=None):
        self.fake.add(number, state="merged", title=title)
        sha = "%040x" % pr
        self.fake.pulls.append({"number": pr, "state": "closed", "merged_at": merged_at,
                                "merge_commit_sha": sha, "title": title,
                                "body": "Refs %s#%d" % (REPO, number) if body is None else body,
                                "head": {"ref": "loom/%d-fix" % number}})
        if carried:
            self.carried.add(sha)
        return sha

    def revert(self, pr, of, title, carried=True, merged_at="2030-01-02T06:00:00Z"):
        """A merged pull request GitHub's revert button opened for pull request of."""
        sha = "%040x" % pr
        self.fake.pulls.append({"number": pr, "state": "closed", "merged_at": merged_at,
                                "merge_commit_sha": sha, "title": 'Revert "%s"' % title,
                                "body": "Reverts %s#%d" % (REPO, of),
                                "head": {"ref": "revert-%d" % of}})
        if carried:
            self.carried.add(sha)

    def pin(self, number=7, merged_at=PIN_MERGED, revision=PUBLISHED, name="example-repo"):
        self.pins.append({"number": number, "state": "closed", "merged_at": merged_at,
                          "merge_commit_sha": "f" * 40, "head": {"ref": "pin"}})
        patch = ("@@ -4,3 +4,3 @@ images:\n"
                 "-  # Published from %s #98, merge %s, build run 1.\n"
                 "+  # Published from %s #99, merge %s, build run 2.\n"
                 "   - name: registry.example/example-repo\n"
                 % (name, "a" * 10, name, revision[:10]))
        self.files[number] = [{"filename": "deploy/kustomization.yaml", "patch": patch}]

    def transport(self, method, url, headers, body):
        parts = urlsplit(url)
        path = parts.path
        if path.startswith("/repos/%s/" % DEPLOY):
            self.reads.append(path)
            if path == "/repos/%s/pulls" % DEPLOY:
                return reply(200, self.pins)
            files = re.match(r"/repos/%s/pulls/(\d+)/files$" % DEPLOY, path)
            if files:
                return reply(200, self.files.get(int(files.group(1)), []))
            raise AssertionError("unexpected call %s %s" % (method, url))
        commit = re.match(r"/repos/%s/commits/([0-9a-f]+)$" % REPO, path)
        if commit:
            if PUBLISHED.startswith(commit.group(1)):
                return reply(200, {"sha": PUBLISHED})
            return reply(404, {"message": "Not Found"})
        compare = re.match(r"/repos/%s/compare/([0-9a-f]+)\.\.\.([0-9a-f]+)$" % REPO, path)
        if compare:
            base, head = compare.groups()
            status = "ahead" if head == PUBLISHED and base in self.carried else "diverged"
            return reply(200, {"status": "identical" if base == head else status})
        return self.fake(method, url, headers, body)

    def ship(self, now=PIN_MERGED_AT + 600):
        gh = GitHub("token", transport=self.transport)
        station.Station(gh, self.settings, now=now).ship_pins()

    def test_a_merged_pin_ships_the_tickets_its_source_revision_carries(self):
        self.merged(12, 99)
        self.merged(13, 100, carried=False)
        self.pin()
        with mock.patch("bus.station._say") as say:
            self.ship()
        self.assertIn("loom:shipped", self.fake.names(12))
        self.assertNotIn("loom:merged", self.fake.names(12))
        self.assertIn("loom:merged", self.fake.names(13))
        self.assertNotIn("loom:shipped", self.fake.names(13))
        self.assertEqual(self.fake.comments[13], [])
        say.assert_called_once_with("shipped %s#12: pin %s#7 carries #99" % (REPO, DEPLOY))

    def test_the_note_names_the_pin_pull_request_the_revision_and_the_merge_time(self):
        self.merged(12, 99)
        self.pin()
        self.ship()
        note = self.fake.comments[12][-1]["body"]
        self.assertIn("%s#7" % DEPLOY, note)
        self.assertIn(PUBLISHED[:12], note)
        self.assertIn("2030-01-03 12:00:00Z", note)
        self.assertIn("#99", note)

    def test_a_ticket_a_carried_revert_undid_stays_merged(self):
        self.merged(12, 99)
        self.revert(101, of=99, title="Fix the thing")
        self.merged(14, 102, title="Fix another thing")
        self.revert(103, of=102, title="Fix another thing", carried=False)
        self.pin()
        self.ship()
        self.assertIn("loom:merged", self.fake.names(12))
        self.assertNotIn("loom:shipped", self.fake.names(12))
        self.assertEqual(self.fake.comments[12], [])
        # A revert the published revision does not carry undoes nothing yet.
        self.assertIn("loom:shipped", self.fake.names(14))

    def test_a_revert_named_only_by_its_title_also_holds_the_ticket(self):
        self.merged(12, 99, title="Fix the thing")
        self.fake.pulls.append({"number": 101, "state": "closed",
                                "merged_at": "2030-01-02T06:00:00Z",
                                "merge_commit_sha": "%040x" % 101,
                                "title": 'Revert "Fix the thing"', "body": "",
                                "head": {"ref": "undo"}})
        self.carried.add("%040x" % 101)
        self.pin()
        self.ship()
        self.assertIn("loom:merged", self.fake.names(12))

    def test_a_merged_pull_request_that_publishes_nothing_moves_nothing(self):
        self.merged(12, 99)
        self.pins.append({"number": 8, "state": "closed", "merged_at": PIN_MERGED})
        self.files[8] = [{"filename": "README.md", "patch": "+# Published notes\n"}]
        self.ship()
        self.assertIn("loom:merged", self.fake.names(12))
        self.assertEqual(self.fake.comments[12], [])

    def test_an_unwatched_source_or_unknown_commit_moves_nothing(self):
        self.merged(12, 99)
        self.pin(number=7, name="other-repo")
        self.pin(number=8, revision="0" * 40)
        self.ship()
        self.assertIn("loom:merged", self.fake.names(12))
        self.assertEqual(self.fake.comments[12], [])

    def test_an_old_or_unmerged_pull_request_is_never_read(self):
        self.merged(12, 99)
        self.pin(number=7, merged_at="2029-12-01T00:00:00Z")
        self.pins.append({"number": 8, "state": "closed", "merged_at": None})
        self.ship()
        self.assertIn("loom:merged", self.fake.names(12))
        self.assertEqual(self.reads, ["/repos/%s/pulls" % DEPLOY])

    def test_a_settled_pin_is_read_once_and_a_fresh_one_until_it_settles(self):
        self.merged(12, 99)
        self.pin()
        files = "/repos/%s/pulls/7/files" % DEPLOY
        self.ship(now=PIN_MERGED_AT + 600)
        self.assertIn("loom:shipped", self.fake.names(12))
        # Within the settle time a ticket reconciled late still ships.
        self.merged(13, 100)
        self.ship(now=PIN_MERGED_AT + 1200)
        self.assertIn("loom:shipped", self.fake.names(13))
        self.ship(now=PIN_MERGED_AT + station.PIN_SHIP_SETTLE + 1)
        self.ship(now=PIN_MERGED_AT + station.PIN_SHIP_SETTLE + 60)
        self.assertEqual(self.reads.count(files), 3)

    def test_at_most_twenty_move_a_pass_and_the_pin_is_read_again(self):
        for n in range(22):
            self.merged(100 + n, 500 + n, merged_at="2030-01-02T00:%02d:00Z" % n)
        self.pin()
        self.ship(now=PIN_MERGED_AT + 2 * station.PIN_SHIP_SETTLE)
        left = [n for n in range(100, 122) if "loom:merged" in self.fake.names(n)]
        self.assertEqual(left, [120, 121])
        self.ship(now=PIN_MERGED_AT + 2 * station.PIN_SHIP_SETTLE + 60)
        self.assertTrue(all("loom:shipped" in self.fake.names(n) for n in range(100, 122)))

    def test_without_deployment_repos_nothing_is_read(self):
        self.settings.pin_ship_repos = []
        self.merged(12, 99)
        self.pin()
        self.ship()
        self.assertEqual(self.reads, [])
        self.assertEqual(self.fake.calls, [])

    def test_a_pass_ships_merged_pins(self):
        self.merged(12, 99)
        self.pin()
        gh = GitHub("token", transport=self.transport)
        self.assertEqual(station.run(self.settings, gh, now=PIN_MERGED_AT + 600), 0)
        self.assertIn("loom:shipped", self.fake.names(12))

    def test_deployment_repos_come_from_the_environment(self):
        env = {"LOOM_HOME": self.home, "LOOM_REPOS": REPO,
               "LOOM_PIN_SHIP_REPOS": DEPLOY + " example-owner/second"}
        self.assertEqual(station.Settings.from_env(env).pin_ship_repos,
                         [DEPLOY, "example-owner/second"])

    def test_the_published_from_line_is_read_from_added_lines_only(self):
        patch = ("-  # Published from example-repo #1, merge 1111111111, build run 1.\n"
                 "+  # Published from example-repo #857 and #858, merge 67151ececc, build run 3.\n"
                 "+# Published from example-owner/example-repo, merge abcdef1234.\n")
        self.assertEqual([m.groups() for m in station.PUBLISHED_RE.finditer(patch)],
                         [("example-repo", "67151ececc"),
                          ("example-owner/example-repo", "abcdef1234")])


class BudgetTest(Base):
    """The station's GitHub budget (T218): conditional list reads, a floor
    under which a pass only claims and reports, and idle repos scanned every
    IDLE_SCAN seconds."""

    PR_LIST = "/repos/%s/issues?labels=loom:pr&state=open&per_page=100" % REPO

    def passes(self, transport=None, now=None, gh=None):
        gh = gh or GitHub("token", transport=transport or self.fake)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(station.run(self.settings, gh, now=now or time.time()), 0)
        return gh, err.getvalue()

    def gets(self, fragment):
        return [p for p in self.fake.paths("GET") if fragment in p]

    def test_a_repeated_list_read_sends_the_etag_and_a_304_is_unchanged(self):
        self.fake.add(12, state="pr")
        sent = []

        def transport(method, url, headers, body):
            # The fake gives the loom:pr list an ETag that moves with the data.
            if method == "GET" and url.endswith(self.PR_LIST):
                sent.append(headers.get("If-None-Match"))
                etag = '"pr%d"' % self.fake.version
                if headers.get("If-None-Match") == etag:
                    self.fake.calls.append((method, self.PR_LIST))
                    return reply(304, None, {"ETag": etag})
                status, _, raw = self.fake(method, url, headers, body)
                return status, {"ETag": etag}, raw
            return self.fake(method, url, headers, body)

        self.passes(transport)
        self.assertEqual(sent[0], None)
        # The ticket's pull request merged; the loom:pr list did not change.
        self.fake.pulls = [{"number": 99, "state": "closed", "merged_at": "2030-01-02T00:00:00Z",
                            "body": "Refs %s#12" % REPO, "head": {"ref": "loom/12-x"}}]
        gh, err = self.passes(transport)
        self.assertEqual(sent[2], '"pr%d"' % (self.fake.version - 1))
        self.assertGreaterEqual(gh.not_modified, 1)
        # The 304's kept list still named the ticket, so it was reconciled.
        self.assertIn("loom:merged", self.fake.names(12))
        self.assertRegex(err, r"requests: \d+, \d+ counted \(.*issues list \d+.*\), "
                              r"\d+ answered 304 \(not counted\)")

    def low(self, remaining=500):
        reset = int(time.time()) + 1800
        headers = {"X-RateLimit-Remaining": str(remaining), "X-RateLimit-Limit": "5000",
                   "X-RateLimit-Reset": str(reset), "X-RateLimit-Resource": "core"}

        def transport(method, url, headers_in, body):
            status, _, raw = self.fake(method, url, headers_in, body)
            return status, headers, raw
        return transport, reset

    def test_below_the_floor_a_pass_claims_and_defers_the_rest_saying_so_once(self):
        self.settings.status_repo = REPO
        self.fake.add(12)
        transport, reset = self.low()
        gh = GitHub("token", transport=transport)
        # The last pass's responses said the budget is low.
        gh.rate["core"] = (500, 5000, reset)
        _, err = self.passes(gh=gh)
        self.assertEqual(self.listing(), ["example-owner.example-repo-12.md"])
        self.assertEqual(self.gets("loom:pr"), [])
        self.assertEqual(self.gets("loom:lanes"), [])
        self.assertEqual(err.count("budget low"), 1)
        self.assertIn("500 of 5000 left", err)
        # The next pass knows from the station's state, and does not say it again.
        _, err = self.passes(transport)
        self.assertEqual(self.gets("loom:pr"), [])
        self.assertEqual(self.gets("loom:lanes"), [])
        self.assertNotIn("budget low", err)

    def test_below_the_floor_the_daily_metrics_line_still_goes_out(self):
        # 2026-10-18: the floor deferred every step after triage on every
        # pass, post_metrics among them, and no metrics line posted that day.
        transport, reset = self.low()
        gh = GitHub("token", transport=transport)
        gh.rate["core"] = (500, 5000, reset)
        with mock.patch.object(station.Station, "post_metrics", autospec=True) as posted:
            _, err = self.passes(gh=gh)
        self.assertIn("budget low", err)
        self.assertEqual(posted.call_count, 1)

    def test_above_the_floor_nothing_is_deferred(self):
        self.settings.status_repo = REPO
        transport, _ = self.low(remaining=4000)
        _, err = self.passes(transport)
        self.assertEqual(len(self.gets("loom:pr")), 2)
        # One lookup finds the status issue; the other reads the remote
        # lane heartbeat from that issue's comments.
        self.assertEqual(len(self.gets("loom:lanes")), 2)
        self.assertNotIn("budget low", err)
        self.assertIn("budget 4000 of 5000 left", err)

    def test_on_a_github_app_the_budget_line_names_the_app_budget(self):
        from bus.github import AppToken
        self.settings.status_repo = REPO
        transport, _ = self.low(remaining=4000)
        bearers = []

        def app_transport(method, url, headers_in, body):
            if method == "POST" and "/app/installations/" in url:
                answer = {"token": "ghs_" + "x" * 396, "expires_at": "2099-01-01T00:00:00Z"}
                return 201, {}, json.dumps(answer).encode()
            if method == "GET" and url.endswith("/app"):
                return 200, {}, json.dumps({"slug": "loom-app"}).encode()
            bearers.append(headers_in["Authorization"])
            return transport(method, url, headers_in, body)
        app = AppToken("1", "2", lambda: "key", sign=lambda data, key: b"sig")
        gh, err = self.passes(gh=GitHub(app, transport=app_transport))
        self.assertEqual(app.minted, 1)
        self.assertEqual(set(bearers), {"Bearer ghs_" + "x" * 396})
        self.assertIn("app token 1", err)
        self.assertIn("GitHub App budget 4000 of 5000 left", err)

    def test_a_refused_app_token_is_logged_with_its_reason_and_sends_nothing_else(self):
        from bus.github import AppToken
        self.settings.status_repo = REPO
        sent = []

        def refusing(method, url, headers_in, body):
            sent.append((method, url.rsplit("/", 1)[-1]))
            return 401, {}, json.dumps({"message": "Bad credentials"}).encode()
        app = AppToken("1", "2", lambda: "key", sign=lambda data, key: b"sig")
        _, err = self.passes(gh=GitHub(app, transport=refusing))
        self.assertIn("could not mint the GitHub App installation token", err)
        self.assertIn("(Bad credentials)", err)
        # Every request was the App's own login read or a mint: none went out
        # with a personal token.
        self.assertEqual(set(sent), {("GET", "app"), ("POST", "access_tokens")})

    def test_an_idle_repo_is_scanned_once_per_ten_minutes(self):
        start = time.time()
        self.passes(now=start)
        self.assertEqual(len(self.gets("loom:briefed")), 1)
        # A ticket briefed a minute later waits for the next scan of the repo.
        self.fake.add(12)
        self.passes(now=start + 60)
        self.assertEqual(len(self.gets("loom:briefed")), 1)
        self.assertEqual(len(self.gets("loom:pr")), 2)
        self.assertEqual(self.listing(), [])
        self.heartbeat("codex", age=-station.IDLE_SCAN)
        self.passes(now=start + station.IDLE_SCAN)
        self.assertEqual(len(self.gets("loom:briefed")), 2)
        self.assertEqual(self.listing(), ["example-owner.example-repo-12.md"])

    def test_a_repo_with_a_ticket_in_flight_is_scanned_every_pass(self):
        start = time.time()
        self.fake.add(12, state="pr")
        for n in range(3):
            self.passes(now=start + 60 * n)
        self.assertEqual(len(self.gets("loom:briefed")), 3)
        self.assertEqual(len(self.gets("loom:pr")), 6)


READY_BODY = ("## Why\nThe page query is slow for every visitor.\n## What\nwhat\n"
              "## Acceptance criteria\n- [ ] done\n- [ ] tested\n- [ ] live\n"
              "## Size\nS\n## Out of scope\n- none")


class TriageTest(Base):
    """The station triages backlog goal homes once an hour."""

    def setUp(self):
        super().setUp()
        self.start = time.time()

    def run_at(self, offset=0):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(station.run(
                self.settings, GitHub("token", transport=self.fake),
                now=self.start + offset), 0)
        return err.getvalue()

    def triage_reads(self):
        return [p for p in self.fake.paths("GET")
                if "sort=created&direction=asc" in p]

    def test_a_backlog_goal_is_triaged_once_an_hour(self):
        self.fake.add_goal(30, title="Backlog: every open issue in %s" % REPO)
        self.fake.add_open_issue(100, body=READY_BODY)
        self.fake.add_open_issue(101, body="It is slow.")
        err = self.run_at()
        self.assertEqual(len(self.triage_reads()), 1)
        self.assertIn("loom:queued", self.fake.names(100))
        self.assertIn("loom:triage", self.fake.names(101))
        self.assertIn("triage %s#30: 1 queued, 1 triage, 0 held" % REPO, err)
        # A second pass in the same hour does not triage again.
        self.run_at(60)
        self.assertEqual(len(self.triage_reads()), 1)

    def test_a_backlog_goal_is_triaged_again_after_an_hour(self):
        self.fake.add_goal(30, title="Backlog: every open issue in %s" % REPO)
        self.fake.add_open_issue(100, body=READY_BODY)
        self.run_at()
        self.assertEqual(len(self.triage_reads()), 1)
        self.run_at(station.TRIAGE_INTERVAL + 1)
        self.assertEqual(len(self.triage_reads()), 2)

    def test_a_backlog_goal_is_not_triaged_again_across_a_restart(self):
        self.fake.add_goal(30, title="Backlog: every open issue in %s" % REPO)
        self.fake.add_open_issue(100, body=READY_BODY)
        path = os.path.join(self.home, "state", "station", "triage.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump({"%s#30" % REPO: self.start - 100}, fh)
        self.run_at()
        self.assertEqual(len(self.triage_reads()), 0)

    def add_labelled_backlog(self, number=30):
        """A backlog home that carries loom:backlog and no "Backlog:" prefix."""
        self.fake.add_goal(number, title="Goal: Loom or a person works every open %s issue" % REPO)
        for issue in (self.fake.goals[-1], self.fake.issues[number]):
            issue["labels"] = [{"name": "loom:goal"}, {"name": "loom:backlog"}]

    def test_a_goal_labelled_backlog_is_the_backlog_home_whatever_its_title(self):
        self.add_labelled_backlog(30)
        self.fake.add_goal(31, title="Goal: ship the thing")
        self.fake.add_open_issue(100, body=READY_BODY)
        st = station.Station(GitHub("token", transport=self.fake), self.settings,
                             now=self.start)
        with mock.patch.object(brain, "triage", wraps=brain.triage) as triage:
            st.triage_backlogs()
        # Only the backlog home: other goal homes are triaged only in a repo
        # that has none.
        self.assertEqual([c.args[2] for c in triage.call_args_list], [30])

    def test_the_backlog_label_orders_adoption_invariant_and_handoff_homes(self):
        self.add_labelled_backlog(30)
        self.fake.add_goal(31, title="Goal: ship the thing")
        self.settings.self_repo = REPO
        self.settings.status_repo = REPO
        self.settings.invariant_goal = ""
        self.settings.metrics_goal = ""
        st = station.Station(GitHub("token", transport=self.fake), self.settings,
                             now=self.start)
        self.assertEqual(st._adoption_goal(REPO), 31)
        self.assertEqual(st._invariant_goal(), (REPO, 31))
        self.assertEqual(st._handoff_home(), (REPO, 31))

    def test_a_goal_without_backlog_home_is_triaged(self):
        self.fake.add_goal(31, title="Ship the thing")
        self.fake.add_open_issue(102, body="It is slow.")
        self.run_at()
        self.assertEqual(len(self.triage_reads()), 1)
        self.assertIn("loom:triage", self.fake.names(102))

    def test_a_goal_without_backlog_home_is_triaged_and_rewritten(self):
        self.fake.add_goal(31, title="Ship the thing")
        self.fake.add_open_issue(102, body=READY_BODY)
        st = station.Station(GitHub("token", transport=self.fake), self.settings,
                             now=self.start)
        with mock.patch.object(brain, "triage", wraps=brain.triage) as triage:
            st.triage_backlogs()
        triage.assert_called_once_with(st.gh, REPO, 31, brain.TRIAGE_LIMIT,
                                       comment_limit=5)
        self.assertIn("loom:queued", self.fake.names(102))
        triage_json = os.path.join(self.home, "state", "station", "triage.json")
        with open(triage_json) as fh:
            self.assertIn("%s#31" % REPO, json.load(fh))
        with mock.patch.object(st, "_rewrite_goal", return_value=0) as rewrite_goal:
            st.s.rewrite_cmd = "agent"
            st.rewrite_triaged()
        self.assertEqual(rewrite_goal.call_args.args[0:3], ("%s#31" % REPO, REPO, 31))
        rewrite_json = os.path.join(self.home, "state", "station", "rewrite.json")
        with open(rewrite_json) as fh:
            self.assertIn("%s#31" % REPO, json.load(fh))

    def test_fallback_goal_waits_at_three_queued_and_caps_not_ready_comments(self):
        self.fake.add_goal(31, title="Ship the thing")
        for number in range(100, 106):
            self.fake.add_open_issue(number, body="It is slow.", created="2030-01-%02dT00:00:00Z" % (number - 90))
        self.fake.add(90, state="queued")
        self.fake.add(91, state="queued")
        self.fake.add(92, state="queued")
        st = station.Station(GitHub("token", transport=self.fake), self.settings,
                             now=self.start)
        original_paginate = st.gh.paginate
        with mock.patch.object(st.gh, "paginate", side_effect=lambda path: (
                [{}, {}, {}] if "labels=loom:queued" in path else original_paginate(path))):
            st.triage_backlogs()
        self.assertEqual([p for p in self.fake.paths("POST") if p.endswith("/comments")], [])
        self.fake.issues[92]["labels"] = [{"name": "loom:ticket"}]
        state_path = os.path.join(self.home, "state", "station", "triage.json")
        with open(state_path) as fh:
            state = json.load(fh)
        state["_scanned"] = self.start - station.TRIAGE_INTERVAL - 1
        with open(state_path, "w") as fh:
            json.dump(state, fh)
        with mock.patch.object(st.gh, "paginate", side_effect=lambda path: (
                [] if "labels=loom:queued" in path else original_paginate(path))):
            st.triage_backlogs()
        comments = [p for p in self.fake.paths("POST") if p.endswith("/comments")]
        self.assertEqual(len(comments), 5)

    def test_below_the_budget_floor_triage_is_deferred(self):
        self.fake.add_goal(30, title="Backlog: every open issue in %s" % REPO)
        self.fake.add_open_issue(103, body=READY_BODY)
        path = os.path.join(self.home, "state", "station", "budget.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        reset = int(self.start) + 1800
        with open(path, "w") as fh:
            json.dump({"rates": {"core": [500, 5000, reset]}}, fh)
        self.run_at()
        self.assertEqual(len(self.triage_reads()), 0)


class HandoffFake:
    """The reads one handoff packet pass makes, and the comment writes."""

    def __init__(self):
        self.goal = {"number": 30, "title": "Loom goal", "state": "open",
                     "labels": [{"name": "loom:goal"}], "comments": 0,
                     "updated_at": "2030-01-01T00:00:00Z"}
        self.comments = []
        self.writes = []
        self.comment_reads = 0
        self.rules_refs = []
        self.post_no_id = False
        self.muse = base64.b64encode(b"# Muse\n\n## Rules\n\n- Write ASCII only.\n").decode()

    def touch(self, at):
        self.goal["comments"] = len(self.comments)
        self.goal["updated_at"] = at

    def journal(self, holder, at, *lines):
        self.comments.append({"id": len(self.comments) + 1, "user": {"login": AUTHORS[0]},
                              "created_at": at, "body": "\n".join(
                                  [station.JOURNAL_MARKER, "Tick %s, %s" % (at, holder)]
                                  + list(lines))})
        self.touch(at)

    def rest(self, method, path, data=None, ok=()):
        if method == "GET" and path.endswith("/git/ref/heads/loom-lock/brain"):
            return mock.Mock(status=404, data=None)
        if method == "GET" and path.startswith("/repos/%s/contents/docs/MUSE.md?ref=" % REPO):
            self.rules_refs.append(path.rsplit("=", 1)[1])
            return mock.Mock(status=200, data={"content": self.muse})
        if method == "POST" and path == "/repos/%s/issues/30/comments" % REPO:
            self.writes.append((method, path))
            self.comments.append({"id": 900 + len(self.writes), "user": {"login": AUTHORS[0]},
                                  "created_at": "2030-01-01T00:00:00Z", "body": data["body"]})
            self.touch("2030-01-01T12:00:%02dZ" % len(self.writes))
            return mock.Mock(status=201, data={} if self.post_no_id else self.comments[-1])
        if method == "PATCH" and path.startswith("/repos/%s/issues/comments/" % REPO):
            self.writes.append((method, path))
            found = next(c for c in self.comments if c["id"] == int(path.rsplit("/", 1)[1]))
            found["body"] = data["body"]
            return mock.Mock(status=200, data=found)
        raise AssertionError("unexpected %s %s" % (method, path))

    def paginate(self, path):
        if path.startswith("/repos/%s/issues?labels=loom:goal" % REPO):
            return [self.goal]
        if path.startswith("/repos/%s/issues/30/comments" % REPO):
            self.comment_reads += 1
            return [dict(c) for c in self.comments]
        raise AssertionError(path)


class HandoffPacketTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.gh = HandoffFake()
        self.now = calendar.timegm(time.strptime("2030-01-01T12:00:00Z", "%Y-%m-%dT%H:%M:%SZ"))
        self.settings = station.Settings(self.temp.name, [REPO], AUTHORS, OWNERS, status_repo=REPO)

    def run_pass(self, minutes=0):
        station.Station(self.gh, self.settings, now=self.now + 60 * minutes,
                        head=lambda: "c" * 40).publish_handoff()

    def tick_ended(self, at):
        with open(os.path.join(self.temp.name, "state", "brain.json"), "w") as fh:
            json.dump({"at": at, "outcome": "ok", "exit": 0}, fh)

    def packets(self):
        return [c for c in self.gh.comments if c["body"].startswith("<!-- loom:handoff v1 -->")]

    def test_a_pass_posts_one_packet_and_later_passes_edit_it_in_place(self):
        self.gh.journal("muse", "2030-01-01T11:00:00Z", "- Decided: merged one",
                        "<!-- loom:defer v1 stage=merge item=%s#7 rev=pr-abc holder=muse -->" % REPO)
        self.run_pass()
        self.assertEqual([("POST", "/repos/%s/issues/30/comments" % REPO)], self.gh.writes)
        body = self.packets()[0]["body"]
        self.assertIn("2030-01-01T11:00:00Z muse, model unknown, on %s#30: 0 done, 1 deferred. "
                      "Decided: merged one." % REPO, body)
        self.assertIn("- merge %s#7, deferred by muse at 2030-01-01T11:00:00Z." % REPO, body)
        self.assertIn("## Rules (docs/MUSE.md at %s)\n- Write ASCII only." % ("c" * 40), body)
        # Nothing changed: no write at all on a later pass.
        self.run_pass(minutes=11)
        self.assertEqual(1, len(self.gh.writes))
        # A brain ticks: the same comment is edited, never a second one posted.
        self.gh.journal("lane-machine", "2030-01-01T12:12:30Z", "- Decided: merged it",
                        "<!-- loom:done v1 stage=merge item=%s#7 holder=lane-machine -->" % REPO)
        self.tick_ended(self.now + 60 * 12)
        self.run_pass(minutes=12)
        self.assertEqual(("PATCH", "/repos/%s/issues/comments/901" % REPO), self.gh.writes[-1])
        self.assertEqual(1, len(self.packets()))
        self.assertEqual(1, sum(1 for method, _ in self.gh.writes if method == "POST"))
        body = self.packets()[0]["body"]
        self.assertIn("lane-machine, model unknown, on %s#30: 1 done, 0 deferred" % REPO, body)
        self.assertIn("## Open defers (0, 0 from brains no longer ticking)\n- none", body)

    def test_a_pass_within_ten_minutes_and_no_new_tick_reads_no_comments(self):
        self.gh.journal("muse", "2030-01-01T11:00:00Z", "- Decided: merged one")
        self.run_pass()
        reads = self.gh.comment_reads
        self.gh.journal("muse", "2030-01-01T12:05:00Z", "- Decided: briefed two")
        self.run_pass(minutes=9)
        self.assertEqual(reads, self.gh.comment_reads)
        self.assertNotIn("briefed two", self.packets()[0]["body"])

    def test_a_brain_tick_that_just_ended_rebuilds_the_packet_at_once(self):
        os.makedirs(os.path.join(self.temp.name, "state"), exist_ok=True)
        self.tick_ended(self.now - 600)
        self.run_pass()
        self.gh.journal("lane-machine", "2030-01-01T12:01:00Z", "- Decided: briefed two")
        self.tick_ended(self.now + 60)
        self.run_pass(minutes=1)
        self.assertIn("Decided: briefed two.", self.packets()[0]["body"])
        self.assertEqual(1, len(self.packets()))

    def test_an_unchanged_goal_home_is_not_read_again(self):
        self.gh.journal("muse", "2030-01-01T11:00:00Z", "- Decided: merged one",
                        "<!-- loom:defer v1 stage=merge item=%s#7 rev=pr-abc holder=muse -->" % REPO)
        self.run_pass()
        # The post itself changed the home: the next build reads it once more.
        self.run_pass(minutes=11)
        reads = self.gh.comment_reads
        # Two hours on, the defer turns into open work: a new text, no read.
        self.run_pass(minutes=125)
        self.assertEqual(reads, self.gh.comment_reads)
        self.assertEqual(("PATCH", "/repos/%s/issues/comments/901" % REPO), self.gh.writes[-1])
        self.assertIn("Open work for any brain: muse last ticked at 2030-01-01T11:00:00Z",
                      self.packets()[0]["body"])

    def test_a_kept_journal_by_a_login_no_longer_allowed_leaves_the_packet(self):
        self.gh.comments.append({"id": 1, "user": {"login": AUTHORS[1]},
                                 "created_at": "2030-01-01T11:00:00Z", "body": "\n".join(
                                     [station.JOURNAL_MARKER, "Tick 2030-01-01T11:00:00Z, muse",
                                      "- Decided: from the second author"])})
        self.gh.touch("2030-01-01T11:00:00Z")
        self.run_pass()
        self.assertIn("from the second author", self.packets()[0]["body"])
        # The post changed the home; this build reads it once more and keeps it.
        self.run_pass(minutes=11)
        reads = self.gh.comment_reads
        self.settings.authors = [AUTHORS[0]]
        self.run_pass(minutes=12)
        self.assertEqual(reads + 1, self.gh.comment_reads)
        self.assertNotIn("from the second author", self.packets()[0]["body"])
        self.assertEqual(1, len(self.packets()))

    def test_kept_journals_are_filtered_by_the_allowed_logins(self):
        st = station.Station(self.gh, self.settings, now=self.now, head=lambda: "c" * 40)
        entry = {"stamp": ["2030-01-01T00:00:00Z", 0], "packet": None,
                 "journals": [{"body": "%s\nTick 2030-01-01T11:00:00Z, muse" % station.JOURNAL_MARKER,
                               "at": "2030-01-01T11:00:00Z", "login": "stranger"}]}
        goals, _, _ = st._handoff_goals(REPO, {"%s#30" % REPO: entry})
        self.assertEqual([], goals[0].journals)

    def test_a_name_that_is_not_owner_slash_name_is_never_shown_or_asked_about(self):
        st = station.Station(self.gh, self.settings, now=self.now, head=lambda: "c" * 40)
        for name in ("not-a-repo", "a/b/c", "", "owner/name with space", None):
            with self.subTest(name=name):
                # The fake refuses any other read, so a public check would raise.
                self.assertFalse(st._shown(name, REPO))
        self.assertTrue(st._shown(REPO, REPO))

    def test_the_station_default_reads_the_rules_at_the_checkout_head(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        head = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{40}", head):
            self.skipTest("not a git checkout")
        st = station.Station(self.gh, self.settings, now=self.now)
        self.assertIs(station._checkout_head, st.head)
        st.publish_handoff()
        self.assertEqual([head], self.gh.rules_refs)
        self.assertIn("## Rules (docs/MUSE.md at %s)" % head, self.packets()[0]["body"])

    def test_a_post_answered_without_an_id_never_leads_to_a_second_packet(self):
        self.gh.journal("muse", "2030-01-01T11:00:00Z", "- Decided: merged one")
        self.gh.post_no_id = True
        self.run_pass()
        self.gh.post_no_id = False
        self.gh.journal("lane-machine", "2030-01-01T12:12:00Z", "- Decided: briefed two")
        self.run_pass(minutes=12)
        # The post changed the home, so the next build reads it again and
        # finds the packet it posted: an edit, not a second post.
        self.assertEqual(["POST", "PATCH"], [method for method, _ in self.gh.writes])
        self.assertEqual(1, len(self.packets()))
        self.assertIn("briefed two", self.packets()[0]["body"])

    def test_a_marked_comment_from_a_stranger_is_not_the_packet(self):
        self.gh.comments.append({"id": 5, "user": {"login": "stranger"},
                                 "created_at": "2030-01-01T00:00:00Z",
                                 "body": "<!-- loom:handoff v1 -->\nfake"})
        self.run_pass()
        self.assertEqual([("POST", "/repos/%s/issues/30/comments" % REPO)], self.gh.writes)

    def test_the_packet_waits_for_one_loom_goal_home(self):
        self.gh.goal["title"] = "Backlog: every open issue"
        self.run_pass()
        self.assertEqual([], self.gh.writes)


STORED_PACKET = {
    "ticks": [{"id": 3, "holder": "lane-machine", "lane": "claude", "model": "opus",
               "started": "2030-01-01T11:30:00Z", "ended": "2030-01-01T11:50:00Z",
               "outcome": "ok", "decided": ["shipped one"],
               "left_open": ["merge %s#7: waiting on CI" % REPO], "handed_off": []}],
    "commitments": [{"id": 1, "tick_id": 3, "holder": "lane-machine", "item": "%s#7" % REPO,
                     "stage": "merge", "key": "", "what": "merge: waiting on CI",
                     "due": "2030-01-01T13:50:00Z", "state": "open"}],
    "rules": [{"source": "docs/MUSE.md", "source_ref": "d" * 40, "text": "- Write ASCII only.",
               "loaded_at": "2030-01-01T11:00:00Z"},
              {"source": "%s#30" % REPO, "source_ref": "comment 12", "text": "## Rules\n- one",
               "loaded_at": "2030-01-01T11:00:00Z"}]}


class StoredHandoffPacketTest(unittest.TestCase):
    """The packet pass reads the dashboard's brain memory when it answers, and
    falls back to the goal-home journals when it does not."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.gh = HandoffFake()
        self.now = calendar.timegm(time.strptime("2030-01-01T12:00:00Z", "%Y-%m-%dT%H:%M:%SZ"))
        self.settings = station.Settings(
            self.temp.name, [REPO], AUTHORS, OWNERS, status_repo=REPO,
            transcript_url="https://dashboard.example.com/api/transcripts",
            transcript_token_ssm="/loom/upload-token")
        self.fetched = []
        self.answer = STORED_PACKET
        self.gh.journal("muse", "2030-01-01T11:00:00Z", "- Decided: from the goal home",
                        "<!-- loom:defer v1 stage=merge item=%s#7 rev=pr-abc holder=muse -->" % REPO)

    def fetch(self, url, token):
        self.fetched.append((url, token))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def run_pass(self, minutes=0):
        station.Station(self.gh, self.settings, now=self.now + 60 * minutes,
                        head=lambda: "c" * 40, read_secret=lambda name, region: "tok",
                        fetch=self.fetch).publish_handoff()

    def packets(self):
        return [c for c in self.gh.comments if c["body"].startswith(station.handoff.HANDOFF_MARKER)]

    def test_the_packet_renders_from_the_endpoint_when_it_answers(self):
        self.run_pass()
        self.assertEqual([("https://dashboard.example.com/api/brain/handoff", "tok")], self.fetched)
        body = self.packets()[0]["body"]
        self.assertIn("## Last 5 ticks\n- 2030-01-01T11:30:00Z lane-machine on claude, model opus, "
                      "ended 2030-01-01T11:50:00Z: ok. 1 decided, 1 left open, 0 handed off.", body)
        self.assertIn("## Open commitments (1)\n- merge %s#7, from lane-machine's tick 3, "
                      "due 2030-01-01T13:50:00Z." % REPO, body)
        self.assertIn("## Rules digest\n- docs/MUSE.md at %s\n- %s#30 at comment 12"
                      % ("d" * 40, REPO), body)
        self.assertIn("## Rules (docs/MUSE.md at %s)\n- Write ASCII only." % ("c" * 40), body)
        self.assertNotIn("from the goal home", body)

    def test_the_packet_falls_back_to_the_goal_homes_when_the_endpoint_does_not_answer(self):
        for answer in (OSError("the handoff read answered 401"),
                       OSError("the handoff read failed to connect"),
                       {"ticks": [], "commitments": [], "rules": []},
                       {"ticks": "not a list"}):
            with self.subTest(answer=answer):
                self.answer = answer
                self.gh.comments = [c for c in self.gh.comments
                                    if not c["body"].startswith(station.handoff.HANDOFF_MARKER)]
                with mock.patch("sys.stderr", new_callable=io.StringIO) as out:
                    self.run_pass(minutes=len(self.fetched) * 11)
                body = self.packets()[0]["body"]
                self.assertIn("Decided: from the goal home.", body)
                self.assertIn("## Open defers (1, 0 from brains no longer ticking)", body)
                self.assertNotIn("## Open commitments", body)
                self.assertIn("handoff packet: built from the goal homes", out.getvalue())

    def test_no_dashboard_settings_build_from_the_goal_homes_without_a_fetch(self):
        self.settings.transcript_url = ""
        self.run_pass()
        self.assertEqual([], self.fetched)
        self.assertIn("Decided: from the goal home.", self.packets()[0]["body"])


class ReviewFake:
    def __init__(self):
        self.api = "https://api.github.com"
        self.issue = {"number": 12, "state": "open", "body": "", "labels": [
            {"name": "loom:ticket"}, {"name": "loom:pr"}, {"name": "lane:codex"}]}
        self.pr = {"number": 7, "body": "Refs %s#12" % REPO,
                   "head": {"ref": "loom/12-fix", "sha": "a" * 40}}
        self.check_runs = [{"status": "completed", "conclusion": "success"}]
        self.statuses = []
        self.comments = []
        self.writes = []

    def rest(self, method, path, data=None):
        # Writes are only accepted at the exact ticket paths; any other PUT or POST
        # falls through to the AssertionError below, so a wrong path fails the test.
        if method == "GET":
            response = self._get(path)
            if response is not None:
                return response
        if method == "PUT" and path == "/repos/%s/issues/12/labels" % REPO:
            return self._put_labels(data)
        if method == "POST" and path == "/repos/%s/issues/12/comments" % REPO:
            return self._post_comment(data)
        if method == "PATCH" and path.startswith("/repos/%s/issues/comments/" % REPO):
            return self._patch_comment(int(path.rsplit("/", 1)[1]), data)
        raise AssertionError("unexpected %s %s" % (method, path))

    def _put_labels(self, data):
        self.issue["labels"] = [{"name": x} for x in data["labels"]]
        self.writes.append(("PUT", "/repos/%s/issues/12/labels" % REPO, data))
        return mock.Mock(data=[])

    def _post_comment(self, data):
        self.comments.append({"id": len(self.comments) + 1, "body": data["body"],
                              "user": {"login": "alice-example"}})
        return mock.Mock(data={})

    def _patch_comment(self, comment_id, data):
        comment = next(c for c in self.comments if c.get("id") == comment_id)
        comment["body"] = data["body"]
        return mock.Mock(data={})

    def _get(self, path):
        if path.startswith("/repos/%s/issues?labels=loom:pr" % REPO):
            return mock.Mock(data=[dict(self.issue)])
        if path.startswith("/repos/%s/pulls?" % REPO):
            return mock.Mock(data=[self.pr])
        if path == "/repos/%s/pulls/7" % REPO:
            return mock.Mock(data=self.pr)
        if path.endswith("/check-runs?per_page=100"):
            return mock.Mock(data={"check_runs": self.check_runs})
        if path.endswith("/status"):
            return mock.Mock(data={"statuses": self.statuses})
        if path == "/repos/%s/issues/12" % REPO:
            return mock.Mock(data=self.issue)
        return None

    def paginate(self, path):
        if path == "/repos/%s/issues/12/comments" % REPO:
            return self.comments
        raise AssertionError(path)


class HandBackTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.gh = ReviewFake()
        self.station = station.Station(self.gh, station.Settings(self.temp.name, [REPO], AUTHORS, OWNERS))

    def run_hand_back(self, checks=None, threads=None):
        with mock.patch("bus.brain._failing_checks", return_value=checks or []), \
             mock.patch("bus.brain._open_threads", return_value=threads or []):
            self.station.hand_back()

    def test_review_fake_rejects_writes_to_other_ticket_paths(self):
        for method, suffix, data in (("PUT", "labels", {"labels": []}),
                                     ("POST", "comments", {"body": "reply"})):
            with self.subTest(method=method):
                with self.assertRaises(AssertionError):
                    self.gh.rest(method, "/repos/%s/issues/13/%s" % (REPO, suffix), data)

    def test_failed_check_is_sent_back_to_its_lane(self):
        self.run_hand_back(checks=[("lint", "last log line")])
        self.assertIn("loom:briefed", [x["name"] for x in self.gh.issue["labels"]])
        self.assertIn("last log line", self.gh.comments[0]["body"])

    def test_a_huge_findings_list_is_truncated_below_the_comment_limit(self):
        self.run_hand_back(checks=[("lint", "x" * 200000)])
        body = self.gh.comments[0]["body"]
        self.assertLess(len(body), 65536)
        self.assertIn("loom:fix-head " + "a" * 40, body)

    def test_unresolved_review_bot_thread_is_sent_back(self):
        thread = {"id": "PRRT_fake", "path": "a.py", "line": 4,
                  "comments": {"nodes": [{"databaseId": 9, "body": "change this",
                                             "author": {"login": "grug-tribe[bot]"}}]}}
        self.run_hand_back(threads=[thread])
        self.assertIn("PRRT_fake", self.gh.comments[0]["body"])
        self.assertIn("loom:briefed", [x["name"] for x in self.gh.issue["labels"]])

    def test_unresolved_thread_not_started_by_the_review_bot_is_ignored(self):
        thread = {"id": "PRRT_user", "path": "a.py", "line": 4,
                  "comments": {"nodes": [{"databaseId": 9, "body": "ignore this",
                                             "author": {"login": "someone-else"}}]}}
        self.run_hand_back(threads=[thread])
        self.assertEqual(self.gh.comments[-1]["body"], "<!-- loom:ready-head %s -->" % ("a" * 40))
        self.assertIn("loom:ready", [x["name"] for x in self.gh.issue["labels"]])

    def test_fix_pending_for_current_head_is_not_handed_back_twice(self):
        self.gh.issue["labels"].append({"name": "loom:running"})
        self.gh.comments.append({"user": {"login": "alice-example"},
                                 "body": tickets.FIX_MARKER + "\n<!-- loom:fix-head " + "a" * 40 + " -->"})
        self.run_hand_back(checks=[("lint", "failed")])
        self.assertEqual(len(self.gh.comments), 1)
        self.assertIn("loom:pr", [x["name"] for x in self.gh.issue["labels"]])

    def test_pending_brain_fix_is_queued_before_automatic_hand_back(self):
        self.gh.comments.append({"user": {"login": AUTHORS[0]},
                                 "body": tickets.FIX_MARKER + "\n" +
                                         brain.BRAIN_FIX_MARKER + "\nManual review finding"})
        self.run_hand_back(checks=[("lint", "automatic check finding")])
        self.assertEqual(len(self.gh.comments), 1)
        self.assertIn("loom:briefed", [x["name"] for x in self.gh.issue["labels"]])
        self.assertNotIn("loom:pr", [x["name"] for x in self.gh.issue["labels"]])

    def test_fix_round_cap_adds_needs_brain(self):
        self.gh.comments.extend({"user": {"login": "alice-example"},
                                 "body": tickets.FIX_MARKER + "\n<!-- loom:fix-head %s -->" % (str(i) * 40)}
                                for i in range(1, 4))
        with mock.patch.dict(os.environ, {"LOOM_FIX_ROUNDS": "3"}):
            self.run_hand_back(checks=[("lint", "failed")])
        self.assertIn("needs-brain", [x["name"] for x in self.gh.issue["labels"]])
        self.assertEqual(len(self.gh.comments), 3)

    def test_markerless_fix_comments_do_not_consume_rounds(self):
        self.gh.comments.extend({"user": {"login": "alice-example"}, "body": tickets.FIX_MARKER}
                                for _ in range(3))
        with mock.patch.dict(os.environ, {"LOOM_FIX_ROUNDS": "3"}):
            self.run_hand_back(checks=[("lint", "failed")])
        self.assertNotIn("needs-brain", [x["name"] for x in self.gh.issue["labels"]])
        self.assertEqual(len(self.gh.comments), 4)

    def test_clean_pr_gets_ready_and_a_new_head_removes_it_while_checks_run(self):
        self.run_hand_back()
        self.assertIn("loom:ready", [x["name"] for x in self.gh.issue["labels"]])
        self.gh.pr["head"]["sha"] = "b" * 40
        self.gh.check_runs = [{"status": "in_progress"}]
        self.run_hand_back()
        self.assertNotIn("loom:ready", [x["name"] for x in self.gh.issue["labels"]])

    def test_each_new_clean_head_edits_the_one_ready_marker(self):
        self.run_hand_back()
        self.gh.pr["head"]["sha"] = "b" * 40
        self.run_hand_back()
        self.assertEqual([c["body"] for c in self.gh.comments],
                         ["<!-- loom:ready-head %s -->" % ("b" * 40)])
        self.assertIn("loom:ready", [x["name"] for x in self.gh.issue["labels"]])


class CorruptStateTest(Base):
    def test_a_corrupt_ledger_stops_the_pass_instead_of_reporting_again(self):
        self.fake.add(12, state="running")
        self.write(self.inbox("codex", "done", "example-owner.example-repo-12.md"),
                   "Repo: %s\nBranch: loom/12-fix-the-thing\nIssue: %s#12\n" % (REPO, REPO))
        self.write(os.path.join(self.home, "state", "station", "reported.json"), "{not json")
        gh = GitHub("token", transport=self.fake)
        self.assertEqual(station.run(self.settings, gh), 1)
        self.assertEqual(self.fake.paths("POST"), [])
        self.assertEqual(self.fake.paths("PUT"), [])

    def test_a_half_written_heartbeat_only_idles_that_lane(self):
        d = os.path.join(self.home, "state", "usage")
        self.write(os.path.join(d, "codex.json"), '{"lane": "co')
        # Reconciliation, PR review polling, the goal scan, the hand-revert
        # scan's merged and shipped lists, and the invariant check's read of
        # open pull requests (their plan checks) still run, and so does
        # reconciliation's read of the open tickets (loom#784).
        self.assertEqual(self.run_pass().requests, 8)


class TicketsTest(unittest.TestCase):
    def test_slug_branch_and_names(self):
        self.assertEqual(tickets.slug("  Fix: the  Thing!! (v2) "), "fix-the-thing-v2")
        self.assertLessEqual(len(tickets.slug("a" * 30 + " " + "b" * 30)), 40)
        self.assertFalse(tickets.slug("a" * 39 + " bbb").endswith("-"))
        self.assertEqual(tickets.branch_for(7, "Fix it", 1), "loom/7-fix-it")
        self.assertEqual(tickets.branch_for(7, "Fix it", 3), "loom/7-fix-it-r3")
        self.assertEqual(tickets.ticket_name(REPO, 7, 1), "example-owner.example-repo-7")
        self.assertEqual(tickets.ticket_name(REPO, 7, 2), "example-owner.example-repo-7-r2")

    def test_ticket_name_obeys_the_runners_rule(self):
        runner = re.compile(r"^[A-Za-z0-9._-]+$")
        for attempt in (1, 4):
            self.assertRegex(tickets.ticket_name("a.b_c/d-e", 12, attempt), runner)
        with self.assertRaises(ValueError):
            tickets.ticket_name("no-slash", 1)

    def test_parse_splits_headers_and_rejects_what_the_runner_would(self):
        headers, rest = tickets.parse(BODY.replace("\n", "\r\n"))
        self.assertEqual(headers, {"Difficulty": "hard", "Model": "model-x"})
        self.assertTrue(rest.startswith("## Why"))
        self.assertEqual(tickets.parse("## Why\nx")[0], {})
        for bad in ("Model: bad model", "Model: -x", "Base: -x", "Base: a b",
                    "Difficulty: huge", "Model: a\nModel: b",
                    "Model: ok\n\n## Why\nBase: other\n"):
            with self.assertRaises(ValueError, msg=bad):
                tickets.parse(bad)

    def test_render_brief(self):
        issue = {"number": 12, "title": "Fix the thing", "body": BODY + "Base is fine\n"}
        text = tickets.render_brief(REPO, issue, "codex", 2)
        self.assertEqual(text, (
            "# Brief: example-owner.example-repo-12-r2\n\n"
            "Repo: example-owner/example-repo\n"
            "Branch: loom/12-fix-the-thing-r2\n"
            "Lane: codex\n"
            "Issue: example-owner/example-repo#12\n"
            "Difficulty: hard\n"
            "Model: model-x\n\n"
            "# T12: Fix the thing\n\n"
            "## Why\nBecause.\n\n## What\nDo it.\nBase is fine\n"))

    def test_contractless_brief_lists_pr_workflow_commands_and_contract_brief_does_not(self):
        issue = {"number": 12, "title": "Fix the thing", "body": BODY}
        checks = [(".github/workflows/test.yml, step Lint", "ruff check ."),
                  (".github/workflows/test.yml, step Tests", "python -m unittest")]
        text = tickets.render_brief(REPO, issue, "codex", ci_checks=checks,
                                    has_contract=False)
        self.assertIn("## Checks CI runs", text)
        self.assertIn("`ruff check .` (from .github/workflows/test.yml, step Lint)", text)
        self.assertIn("`python -m unittest` (from .github/workflows/test.yml, step Tests)", text)
        contracted = tickets.render_brief(REPO, issue, "codex", ci_checks=checks,
                                          has_contract=True)
        self.assertNotIn("Checks CI runs", contracted)


class ValidationContractTest(Base):
    def test_reads_run_lines_only_from_pull_request_workflows(self):
        worker = station.Station(GitHub("token", transport=self.fake), self.settings)
        contents = {
            "/repos/%s/contents/.loom/validation.conf" % REPO: None,
            "/repos/%s/contents/.github/workflows" % REPO: [
                {"path": ".github/workflows/test.yml"},
                {"path": ".github/workflows/nightly.yml"}],
            "/repos/%s/contents/.github/workflows/test.yml" % REPO:
                "name: Test\non:\n  pull_request:\njobs:\n  test:\n    steps:\n"
                "      - name: Unit tests\n        run: python -m unittest\n",
            "/repos/%s/contents/.github/workflows/nightly.yml" % REPO:
                "name: Nightly\non:\n  schedule:\n    - cron: '0 0 * * *'\njobs:\n"
                "  test:\n    steps:\n      - run: ignored\n",
        }
        def response(method, path, **kwargs):
            data = contents[path]
            if isinstance(data, str):
                data = {"content": base64.b64encode(data.encode()).decode()}
            return type("Response", (), {"data": data})()
        worker.gh.rest = mock.Mock(side_effect=response)
        has_contract, checks = worker._validation_checks(REPO)
        self.assertFalse(has_contract)
        self.assertEqual(checks, [(".github/workflows/test.yml, step Unit tests",
                                  "python -m unittest")])

    def test_missing_contract_files_one_ticket_and_skips_an_open_duplicate(self):
        goal = {"number": 2, "title": "Backlog: reusable repository work"}
        self.gh = GitHub("token", transport=self.fake)
        worker = station.Station(self.gh, self.settings)
        worker._goals = [(REPO, goal)]
        worker._contents_text = mock.Mock(return_value=None)
        worker.gh.rest = mock.Mock(return_value=type("Response", (), {"data": []})())
        with mock.patch.object(brain, "brief_intake") as file_ticket:
            worker.file_validation_contracts()
        self.assertEqual(file_ticket.call_count, 1)
        # Filed under the goal's intake epic (brief_intake finds it).
        self.assertEqual(file_ticket.call_args.args[2], 2)
        self.assertIn(".loom/validation.conf", file_ticket.call_args.args[4])

        worker.gh.rest = mock.Mock(return_value=type("Response", (), {"data": [
            {"number": 3, "title": "Add the repository validation contract"}
        ]})())
        with mock.patch.object(brain, "brief_intake") as file_ticket:
            worker.file_validation_contracts()
        file_ticket.assert_not_called()


class TrustTest(unittest.TestCase):
    def setUp(self):
        self.issue = {"user": {"login": "alice-example"},
                      "repository_url": "https://api.github.com/repos/" + REPO}
        self.events = [labeled("loom:briefed", "alice-example"),
                       labeled("lane:codex", "bob-example")]

    def check(self, **over):
        args = dict(issue=self.issue, events=self.events, editor_login=None,
                    authors=AUTHORS, owners=OWNERS)
        args.update(over)
        return tickets.trusted(**args)

    def test_a_clean_ticket_is_trusted(self):
        self.assertEqual(self.check(), (True, "ok"))
        self.assertTrue(self.check(editor_login="Bob-Example")[0])

    def test_author_outside_the_list_is_refused(self):
        self.issue["user"] = {"login": "stranger"}
        self.assertFalse(self.check()[0])

    def test_latest_briefed_label_actor_outside_the_list_is_refused(self):
        self.events.append(labeled("loom:briefed", "stranger"))
        self.assertFalse(self.check()[0])
        self.assertFalse(self.check(events=[labeled("lane:codex", "alice-example")])[0])

    def test_latest_lane_label_actor_outside_the_list_is_refused(self):
        self.events.append(labeled("lane:agy", "stranger"))
        self.assertFalse(self.check()[0])
        self.assertFalse(self.check(events=self.events[:1])[0])

    def test_an_earlier_untrusted_actor_is_superseded_by_a_later_trusted_one(self):
        self.events.insert(0, labeled("loom:briefed", "stranger"))
        self.assertTrue(self.check()[0])

    def test_body_editor_outside_the_list_is_refused(self):
        self.assertFalse(self.check(editor_login="stranger")[0])

    def test_repo_owner_outside_the_list_is_refused(self):
        self.assertFalse(self.check(owners=["someone-else"])[0])
        self.assertFalse(self.check(issue={"user": {"login": "alice-example"}})[0])

    def test_empty_author_list_trusts_nothing(self):
        self.assertFalse(self.check(authors=[])[0])


class ClaimTest(Base):
    def test_a_mismatched_model_moves_to_a_compatible_lane_before_claim(self):
        self.heartbeat("opencode-go-3")
        self.heartbeat("claude")
        body = BODY.replace("Model: model-x", "Model: claude-sonnet-5-5")
        self.fake.add(12, lane="opencode-go-3", body=body)
        self.run_pass()
        self.assertEqual(self.listing("opencode-go-3"), [])
        self.assertEqual(self.listing("claude"), ["example-owner.example-repo-12.md"])
        self.assertIn("lane:claude", self.fake.names(12))
        self.assertIn("Claimed for lane claude", self.fake.comments[12][-1]["body"])

    def test_a_mismatched_model_waits_queued_without_a_free_compatible_lane(self):
        self.heartbeat("opencode-go-3")
        body = BODY.replace("Model: model-x", "Model: claude-sonnet-5-5")
        self.fake.add(12, lane="opencode-go-3", body=body)
        self.run_pass()
        self.assertEqual(self.listing("opencode-go-3"), [])
        self.assertIn("loom:queued", self.fake.names(12))
        self.assertIn("wait:lane", self.fake.names(12))
        self.assertFalse(any("claude-sonnet-5-5" in c["body"] for c in self.fake.comments[12]))
        self.assertEqual(self.fake.issues[12]["body"], body)

    def test_a_mismatched_model_is_queued_even_when_no_lane_is_free(self):
        body = BODY.replace("Model: model-x", "Model: claude-sonnet-5-5")
        self.fake.add(12, lane="opencode-go-3", body=body)
        self.write(self.inbox("codex", "other.md"), "busy\n")
        self.run_pass()
        self.assertIn("loom:queued", self.fake.names(12))
        # A wait label, not a comment: each comment emails the operator.
        self.assertIn("wait:lane", self.fake.names(12))
        self.assertFalse(any("waits for a free compatible lane" in c["body"]
                             for c in self.fake.comments[12]))

    def test_a_trusted_ticket_becomes_a_brief_and_is_claimed(self):
        self.fake.add(12)
        self.run_pass()
        name = "example-owner.example-repo-12"
        self.assertEqual(self.listing(), [name + ".md"])
        with open(self.inbox("codex", name + ".md")) as fh:
            text = fh.read()
        self.assertEqual(text, tickets.render_brief(REPO, self.fake.issues[12], "codex", 1))
        self.assertIn("Issue: %s#12" % REPO, text)
        self.assertEqual(self.fake.names(12), ["loom:ticket", "lane:codex", "loom:running",
                                               "claim:alice-example"])
        puts = [p for p in self.fake.paths("PUT")]
        self.assertEqual(puts, ["/repos/%s/issues/12/labels" % REPO])
        sha = __import__("hashlib").sha256(BODY.encode()).hexdigest()[:12]
        # The claim is an event on the ticket's one status comment (loom#866).
        self.assertEqual(len(self.fake.comments[12]), 1)
        self.assertTrue(status.is_status(self.fake.comments[12][0]["body"]))
        self.assertEqual([c["body"] for c in status.expand(self.fake.comments[12], AUTHORS)],
                         ["Claimed for lane codex (body sha256 %s)" % sha])

    def test_a_ticket_pinned_to_a_model_limited_on_its_lane_is_not_claimed(self):
        # The lane is up (its ladder has another model), but not for this
        # ticket's Model: until the limit lifts; a claim was parked at once and
        # re-claimed every pass (24 claims in 90 minutes, 2026-10-18).
        self.heartbeat("codex", limited_models={"model-x": int(time.time()) + 3600})
        self.fake.add(12)
        self.run_pass()
        self.assertEqual(self.listing(), [])
        self.assertNotIn("loom:running", self.fake.names(12))
        self.assertFalse([c for c in self.fake.comments[12] if c["body"].startswith("Claimed")])
        self.assertIn("wait:model-limit", self.fake.names(12))
        self.assertEqual(self.fake.comments[12], [])

    def test_a_claim_clears_the_wait_label(self):
        self.fake.add(12)
        self.fake.issues[12]["labels"].append({"name": "wait:model-limit"})
        self.run_pass()
        self.assertIn("loom:running", self.fake.names(12))
        self.assertFalse([n for n in self.fake.names(12) if n.startswith("wait:")])
        self.assertEqual(self.fake.paths("PUT"), ["/repos/%s/issues/12/labels" % REPO])

    def test_a_limit_on_another_model_or_one_that_lifted_does_not_hold_the_claim(self):
        self.heartbeat("codex", limited_models={"model-y": int(time.time()) + 3600,
                                                "model-x": int(time.time()) - 60})
        self.fake.add(12)
        self.run_pass()
        self.assertEqual(self.listing(), ["example-owner.example-repo-12.md"])

    def test_a_ticket_held_by_not_before_waits(self):
        self.fake.add(12, body="Not before: 2099-01-01T00:00:00Z\n" + BODY)
        self.run_pass()
        self.assertEqual(self.listing(), [])
        self.assertIn("loom:briefed", self.fake.names(12))

    def test_a_ticket_past_its_not_before_is_claimed_without_the_line(self):
        self.fake.add(12, body="Not before: 2000-01-01T00:00:00Z\n" + BODY)
        self.run_pass()
        name = "example-owner.example-repo-12"
        self.assertEqual(self.listing(), [name + ".md"])
        with open(self.inbox("codex", name + ".md")) as fh:
            self.assertNotIn("Not before", fh.read())

    def test_a_malformed_not_before_is_refused(self):
        self.fake.add(12, body="Not before: tomorrow\n" + BODY)
        self.run_pass()
        self.assertEqual(self.listing(), [])

    def test_a_ticket_waits_for_the_tickets_its_after_line_names(self):
        self.settings.factory_on = True  # the pass that gives the queued ticket a lane
        self.fake.add(5, title="Land the package", state="pr")
        self.fake.add(12, state="queued", body="After: #5\n" + BODY)
        self.run_pass()
        self.assertIn("loom:briefed", self.fake.names(12))
        self.run_pass()  # its dependency has not merged, so no lane takes it
        self.assertEqual(self.listing(), [])
        self.assertIn("loom:briefed", self.fake.names(12))
        # The dependency merges: the next pass claims the ticket.
        self.fake.issues[5]["labels"] = [{"name": "loom:ticket"}, {"name": "loom:merged"},
                                          {"name": "lane:codex"}]
        self.run_pass()
        self.assertEqual(self.listing(), ["example-owner.example-repo-12.md"])
        self.assertIn("loom:running", self.fake.names(12))

    def test_a_ticket_already_briefed_waits_too(self):
        self.fake.add(5, title="Land the package", state="pr")
        self.fake.add(12, body="After: #5\n" + BODY)
        self.run_pass()
        self.assertEqual(self.listing(), [])
        # One labels write says why it waits; no comment, and the next pass
        # writes nothing, the label already being there.
        self.assertEqual(self.fake.paths("PUT"), ["/repos/%s/issues/12/labels" % REPO])
        self.assertIn("wait:after", self.fake.names(12))
        self.assertEqual(self.fake.comments[12], [])
        self.run_pass()
        self.assertEqual(self.fake.paths("PUT"), ["/repos/%s/issues/12/labels" % REPO])

    def test_a_dependency_of_another_repo_is_read_from_there(self):
        self.fake.add_foreign("other-owner/other-repo", 5, labels=("loom:merged",))
        self.fake.add(12, body="After: other-owner/other-repo#5\n" + BODY)
        self.run_pass()
        self.assertIn("/repos/other-owner/other-repo/issues/5", self.fake.paths())
        self.assertEqual(self.listing(), ["example-owner.example-repo-12.md"])

    def test_a_dependency_closed_not_planned_releases_the_hold(self):
        self.fake.add(5, title="Dropped", state="queued")
        self.fake.issues[5]["state"] = "closed"
        self.fake.issues[5]["state_reason"] = "not_planned"
        self.fake.add(12, body="After: #5\n" + BODY)
        self.run_pass()
        self.assertEqual(self.listing(), ["example-owner.example-repo-12.md"])
        said = [c["body"] for c in self.fake.comments[12]]
        self.assertEqual(len(said), 2, said)
        self.assertIn("<!-- loom:after-release 5 -->", said[0])
        self.assertIn("#5 was closed as not planned", said[0])

    def test_the_release_is_said_once_however_many_passes_run(self):
        self.fake.add(5, title="Dropped")
        self.fake.issues[5]["state"] = "closed"
        self.fake.issues[5]["state_reason"] = "not_planned"
        self.fake.add(12, body="After: #5\n" + BODY)
        self.run_pass()
        self.fake.issues[12]["labels"] = [{"name": "loom:ticket"}, {"name": "loom:briefed"},
                                          {"name": "lane:codex"}]
        self.run_pass()
        self.assertEqual(sum("loom:after-release" in str(c["body"])
                             for c in self.fake.comments[12]), 1)

    def test_an_after_line_that_cannot_be_read_holds_the_ticket(self):
        self.fake.add(12, body="After: #404\n" + BODY)
        self.run_pass()
        self.assertEqual(self.listing(), [])
        self.assertIn("loom:briefed", self.fake.names(12))

    def test_the_oldest_trusted_ticket_wins_and_untrusted_ones_are_skipped(self):
        self.fake.add(5, author="stranger", created="2030-01-01T00:00:00Z")
        self.fake.add(9, created="2030-01-03T00:00:00Z")
        self.fake.add(7, created="2030-01-02T00:00:00Z")
        self.run_pass()
        self.assertEqual(self.listing(), ["example-owner.example-repo-7.md"])
        self.assertIn("loom:briefed", self.fake.names(5))

    def test_a_regression_ticket_is_claimed_before_older_ordinary_ones(self):
        self.fake.add(7, created="2030-01-01T00:00:00Z")
        self.fake.add(9, created="2030-01-03T00:00:00Z")
        self.fake.issues[9]["labels"].append({"name": "loom:regression"})
        self.run_pass()
        self.assertEqual(self.listing(), ["example-owner.example-repo-9.md"])
        self.assertIn("loom:briefed", self.fake.names(7))

    def test_a_p0_ticket_is_claimed_before_older_ones_without_a_priority(self):
        self.fake.add(7, created="2030-01-01T00:00:00Z")
        self.fake.add(8, created="2030-01-02T00:00:00Z", body="Priority: P1\n" + BODY)
        self.fake.add(9, created="2030-01-03T00:00:00Z", body="Priority: P0\n" + BODY)
        self.run_pass()
        self.assertEqual(self.listing(), ["example-owner.example-repo-9.md"])

    def test_a_regression_is_claimed_before_a_p0_ticket(self):
        self.fake.add(7, created="2030-01-01T00:00:00Z", body="Priority: P0\n" + BODY)
        self.fake.add(9, created="2030-01-03T00:00:00Z", body="Priority: P3\n" + BODY)
        self.fake.issues[9]["labels"].append({"name": "loom:regression"})
        self.run_pass()
        self.assertEqual(self.listing(), ["example-owner.example-repo-9.md"])

    def test_a_revert_ticket_is_claimed_before_a_regression_and_older_ones(self):
        self.fake.add(7, created="2030-01-01T00:00:00Z")
        self.fake.add(8, created="2030-01-02T00:00:00Z")
        self.fake.issues[8]["labels"].append({"name": "loom:regression"})
        self.fake.add(9, created="2030-01-03T00:00:00Z")
        self.fake.issues[9]["labels"].append({"name": "loom:revert"})
        self.run_pass()
        self.assertEqual(self.listing(), ["example-owner.example-repo-9.md"])

    def test_a_retry_after_failure_gets_a_fresh_branch(self):
        self.fake.add(12)
        self.fake.comments[12].append({"body": "Claimed for lane codex (body sha256 0123456789ab)"})
        self.fake.comments[12].append({"body": "unrelated"})
        self.run_pass()
        with open(self.inbox("codex", "example-owner.example-repo-12-r2.md")) as fh:
            self.assertIn("Branch: loom/12-fix-the-thing-r2\n", fh.read())

    def test_a_ticket_whose_newest_brief_is_still_in_a_lane_inbox_is_not_claimed_again(self):
        self.fake.add(12)
        self.fake.comments[12].append({"body": "Claimed for lane claude (body sha256 0123456789ab)"})
        # The first claim's run has not ended: its brief waits or runs on claude.
        self.write(self.inbox("claude", "example-owner.example-repo-12.md"), "running\n")
        self.run_pass()
        self.assertEqual(self.listing(), [])
        self.assertEqual(self.fake.paths("PUT"), [])
        self.assertEqual(len(self.fake.comments[12]), 1)
        self.assertIn("loom:briefed", self.fake.names(12))
        # Once that run has ended, the next pass claims it.
        os.makedirs(self.inbox("claude", "failed"))
        os.replace(self.inbox("claude", "example-owner.example-repo-12.md"),
                   self.inbox("claude", "failed", "example-owner.example-repo-12.md"))
        self.run_pass()
        self.assertEqual(self.listing(), ["example-owner.example-repo-12-r2.md"])

    def test_a_stale_brief_in_a_down_lanes_inbox_is_returned_and_the_ticket_claimed(self):
        # 2026-10-18: a brief sat two days in the inbox of a
        # lane whose key had expired, and no other lane could claim the ticket.
        self.fake.add(12)
        self.fake.comments[12].append({"body": "Claimed for lane claude (body sha256 0123456789ab)"})
        stale = self.inbox("claude", "example-owner.example-repo-12.md")
        self.write(stale, "never run\n")
        old = time.time() - 2 * station.DEAD_BRIEF_AGE
        os.utime(stale, (old, old))
        self.run_pass()
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.exists(self.inbox("claude", "returned", "example-owner.example-repo-12.md")))
        self.assertEqual(self.listing(), ["example-owner.example-repo-12-r2.md"])

    def test_a_stale_brief_in_a_live_lanes_inbox_still_blocks(self):
        self.fake.add(12)
        self.fake.comments[12].append({"body": "Claimed for lane codex (body sha256 0123456789ab)"})
        stale = self.inbox("codex", "example-owner.example-repo-12.md")
        self.write(stale, "running\n")
        old = time.time() - 2 * station.DEAD_BRIEF_AGE
        os.utime(stale, (old, old))
        self.run_pass()
        self.assertTrue(os.path.exists(stale))
        self.assertIn("loom:briefed", self.fake.names(12))

    def test_a_second_pass_after_a_304_also_polls_for_pr_reviews(self):
        self.fake.add(12, author="stranger")
        self.run_pass()
        first = len(self.fake.calls)
        gh = self.run_pass()
        # Reconciliation reads open PRs to discover linked queued tickets,
        # even when the repo has no loom:pr ticket. PR review and hand-revert
        # scans add their usual reads. It also reads the open tickets, to
        # find a queued or briefed one a merged pull request delivers
        # (loom#784; a conditional read the verify runs share).
        self.assertEqual(len(self.fake.calls), first + 6)
        self.assertEqual(gh.requests, 8)
        self.assertEqual(gh.not_modified, 1)
        self.assertEqual(self.fake.calls[-1][1].split("?")[0], "/repos/%s/issues" % REPO)

    def test_a_304_reuses_the_stored_list(self):
        self.fake.add(12)
        self.heartbeat("codex", available=False)
        self.run_pass()
        self.assertEqual(self.listing(), [])
        self.heartbeat("codex")
        before = len(self.fake.calls)
        self.run_pass()
        # The list came back 304; the stored copy still got the ticket claimed.
        self.assertEqual(self.fake.calls[before][0], "GET")
        self.assertEqual(self.listing(), ["example-owner.example-repo-12.md"])

    def test_pull_requests_in_the_list_are_ignored(self):
        self.fake.add(12)
        self.fake.issues[12]["pull_request"] = {"url": "x"}
        self.run_pass()
        self.assertEqual(self.listing(), [])

    def test_a_busy_lane_gets_nothing(self):
        self.fake.add(12)
        self.write(self.inbox("codex", "other.md"), "x")
        self.run_pass()
        self.assertEqual(self.listing(), ["other.md"])
        self.assertEqual(self.fake.paths("PUT"), [])

    def test_a_lane_with_a_leftover_claiming_file_is_busy(self):
        self.fake.add(12)
        self.write(self.inbox("codex", "unrelated.claiming"), "no issue line\n")
        self.run_pass()
        self.assertEqual(self.fake.paths("PUT"), [])

    def test_a_stale_unavailable_or_missing_heartbeat_gets_nothing(self):
        self.fake.add(12)
        self.heartbeat("codex", age=station.HEARTBEAT_MAX_AGE + 30)
        self.run_pass()
        self.heartbeat("codex", available=False)
        self.run_pass()
        os.remove(os.path.join(self.home, "state", "usage", "codex.json"))
        self.run_pass()
        self.assertEqual(self.listing(), [])
        self.assertEqual(self.fake.paths("PUT"), [])

    def test_a_half_done_claim_is_finished_by_the_next_pass(self):
        self.fake.add(12)
        name = "example-owner.example-repo-12"

        class Crash(Exception):
            pass
        real = self.fake.__call__

        def crashing(method, url, headers, body):
            if method == "PUT":
                raise Crash()
            return real(method, url, headers, body)
        try:
            station.run(self.settings, GitHub("t", transport=crashing))
        except Crash:
            pass
        self.assertEqual(self.listing(), [name + ".claiming"])
        self.assertIn("loom:briefed", self.fake.names(12))
        # Still briefed: the next pass claims it, once.
        self.run_pass()
        self.assertEqual(self.listing(), [name + ".md"])
        self.assertEqual(self.fake.names(12), ["loom:ticket", "lane:codex", "loom:running",
                                               "claim:alice-example"])
        self.assertEqual(len(self.fake.comments[12]), 1)

    def test_a_claiming_file_whose_ticket_is_running_is_just_renamed(self):
        self.fake.add(12, state="running")
        name = "example-owner.example-repo-12"
        self.write(self.inbox("codex", name + ".claiming"),
                   tickets.render_brief(REPO, self.fake.issues[12], "codex", 1))
        self.run_pass()
        self.assertEqual(self.listing(), [name + ".md"])
        self.assertEqual(self.fake.paths("PUT"), [])
        self.assertEqual(self.fake.comments[12], [])

    def test_a_ticket_the_loom_app_last_edited_is_still_claimed(self):
        self.settings.authors = list(self.settings.authors) + ["loom-app[bot]"]
        self.fake.add(12, editor="loom-app[bot]")
        self.run_pass()
        self.assertTrue(any(path.endswith("/issues/12/labels") for path in self.fake.paths("PUT")))

    def test_a_lane_that_beat_during_the_pass_takes_a_queued_ticket_after_the_claims(self):
        # The pass began five minutes ago, and every lane beat again since:
        # its claims run first, then the free spark lane takes the queued
        # ticket (loom#717).
        self.settings.factory_on = True
        self.heartbeat("spark")
        self.fake.add(12)
        self.fake.add(13, body="Difficulty: medium\n\n## Why\nx\n", state="queued")
        self.fake.issues[13]["labels"] = [{"name": "loom:ticket"}, {"name": "loom:queued"}]
        st = station.Station(GitHub("token", transport=self.fake), self.settings,
                             now=time.time() - 300)
        os.makedirs(st.state, exist_ok=True)
        st.claim_new()
        st.assign_queued()
        self.assertEqual(self.listing(), ["example-owner.example-repo-12.md"])
        self.assertIn("loom:briefed", self.fake.names(13))
        self.assertIn("lane:spark", self.fake.names(13))

    def test_a_beat_stamped_ahead_of_the_wall_clock_is_not_fresh(self):
        self.heartbeat("spark", age=-120)
        st = station.Station(GitHub("token", transport=self.fake), self.settings)
        self.assertFalse(st._heartbeat_ok("spark"))
        self.assertTrue(st._heartbeat_ok("codex"))

    def test_a_half_done_claim_is_dropped_when_the_ticket_is_no_longer_trusted(self):
        self.fake.add(12, editor="stranger")
        name = "example-owner.example-repo-12"
        self.write(self.inbox("codex", name + ".claiming"),
                   tickets.render_brief(REPO, self.fake.issues[12], "codex", 1))
        self.run_pass()
        self.assertEqual(self.listing(), [])
        self.assertEqual(self.fake.paths("PUT"), [])


FIX_BODY = ("<!-- loom:fix v1 -->\nPull request: %s#99, branch loom/12-fix-the-thing\n\n"
            "The lint check fails.\nBranch: loom/evil\n\n### Check: lint\n\n    E501\n" % REPO)


class FixTest(Base):
    def comment(self, number, body, author="alice-example", edited=False):
        at = "2030-01-02T00:00:00Z"
        self.fake.comments[number].append({
            "id": len(self.fake.comments[number]) + 1,
            "body": body, "user": {"login": author}, "created_at": at,
            "updated_at": "2030-01-03T00:00:00Z" if edited else at})

    def claimed_brief(self, name="example-owner.example-repo-12-r2"):
        self.assertEqual(self.listing(), [name + ".md"])
        with open(self.inbox("codex", name + ".md")) as fh:
            return fh.read()

    def test_a_trusted_fix_after_the_last_claim_renders_a_fix_brief(self):
        self.fake.add(12)
        self.comment(12, "Claimed for lane codex (body sha256 0123456789ab)", author="station-example")
        self.comment(12, "Lane codex opened a pull request: https://example.com/pull/99")
        self.comment(12, FIX_BODY)
        self.comment(12, "drive-by advice", author="stranger")
        self.run_pass()
        text = self.claimed_brief()
        self.assertIn("\nBranch: loom/12-fix-the-thing\nLane: codex\nIssue: %s#12\nFix-PR: 99\n" % REPO, text)
        fix = text.split("\n## Fix\n", 1)[1]
        self.assertIn("pull request #99 on branch loom/12-fix-the-thing", fix)
        self.assertIn("### Check: lint\n\n    E501", fix)
        # A header-looking line in the findings is quoted, never a header.
        self.assertIn("\n    Branch: loom/evil\n", fix)
        self.assertNotIn("drive-by", text)
        self.assertEqual(self.fake.names(12), ["loom:ticket", "lane:codex", "loom:running",
                                               "claim:alice-example"])

    def assert_plain_brief(self):
        text = self.claimed_brief()
        self.assertNotIn("Fix-PR", text)
        self.assertNotIn("## Fix", text)
        self.assertIn("Branch: loom/12-fix-the-thing-r2\n", text)

    def test_a_fix_comment_from_a_stranger_is_ignored(self):
        self.fake.add(12)
        self.comment(12, "Claimed for lane codex (body sha256 0123456789ab)")
        self.comment(12, FIX_BODY, author="stranger")
        self.run_pass()
        self.assert_plain_brief()

    def test_a_fix_comment_older_than_the_last_claim_is_ignored(self):
        self.fake.add(12)
        self.comment(12, FIX_BODY)
        self.comment(12, "Claimed for lane codex (body sha256 0123456789ab)", author="station-example")
        self.run_pass()
        self.assert_plain_brief()

    def test_a_fix_followed_by_a_later_trusted_comment_still_renders_a_fix(self):
        self.fake.add(12)
        self.comment(12, "Claimed for lane codex (body sha256 0123456789ab)")
        self.comment(12, FIX_BODY)
        self.comment(12, "Start over on a fresh branch.", author="bob-example")
        self.run_pass()
        text = self.claimed_brief()
        self.assertIn("Fix-PR: 99\n", text)
        self.assertIn("\n## Fix\n", text)

    def refused_fix(self, body, edited=False):
        self.fake.add(12)
        self.comment(12, "Claimed for lane codex (body sha256 0123456789ab)")
        self.comment(12, body, edited=edited)
        self.run_pass()
        self.assertEqual(self.listing(), [])
        self.assertEqual(self.fake.paths("PUT"), [])
        self.assertIn("loom:briefed", self.fake.names(12))

    def test_an_edited_fix_comment_is_refused(self):
        self.refused_fix(FIX_BODY, edited=True)

    def test_a_fix_comment_for_another_repo_is_refused(self):
        self.refused_fix(FIX_BODY.replace(REPO + "#99", "example-owner/other#99"))

    def test_a_fix_comment_for_a_branch_the_runner_refuses_is_refused(self):
        self.refused_fix(FIX_BODY.replace("branch loom/12-fix-the-thing", "branch main"))

    def test_a_fix_comment_with_an_invalid_finding_id_is_refused(self):
        body = FIX_BODY + "\n### Thread X9 (comment 1) at test.py:10\nFinding\nDoes not apply\n"
        self.refused_fix(body)

    def test_a_failure_while_refusing_is_not_reported_as_a_bad_finding(self):
        self.fake.add(12)
        self.comment(12, "Claimed for lane codex (body sha256 0123456789ab)")
        self.comment(12, FIX_BODY + "\n### Thread F1 (comment 1) at test.py:10\nFinding\n")
        reasons = []

        def refuse(st, repo, issue, reason):
            reasons.append(reason)
            if "without dispositions" in reason:
                raise ValueError("refusing failed")

        with mock.patch.object(station.Station, "_refuse", refuse):
            with self.assertRaisesRegex(ValueError, "refusing failed"):
                station.Station(GitHub("token", transport=self.fake), self.settings).run_pass()
        self.assertEqual(reasons, ["pre-roll findings without dispositions: F1"])

    def test_a_finished_fix_reports_pushed_fixes(self):
        self.fake.add(12, state="running")
        fix = tickets.parse_fix(REPO, FIX_BODY)
        name = tickets.ticket_name(REPO, 12, 2)
        self.write(self.inbox("codex", "done", name + ".md"),
                   tickets.render_brief(REPO, self.fake.issues[12], "codex", 2, fix))
        self.fake.pulls = [{"html_url": "https://example.com/pull/99"}]
        self.run_pass()
        self.assertEqual(self.fake.names(12), ["loom:ticket", "lane:codex", "loom:pr"])
        self.assertEqual([c["body"] for c in self.fake.comments[12]],
                         ["Lane codex pushed fixes to a pull request: https://example.com/pull/99"])
        pulls = [p for p in self.fake.paths("GET") if "/pulls" in p]
        self.assertEqual(pulls, ["/repos/%s/pulls?head=example-owner:loom/12-fix-the-thing&state=all" % REPO,
                                 "/repos/%s/pulls?state=open&per_page=100" % REPO,
                                 CLOSED_PULLS % REPO])

    def failed_fix(self, log_lines, fix_body=FIX_BODY):
        self.fake.add(12, state="running")
        self.fake.comments[99] = []
        fix = tickets.parse_fix(REPO, fix_body) if fix_body else None
        name = tickets.ticket_name(REPO, 12, 2)
        self.write(self.inbox("codex", "failed", name + ".md"),
                   tickets.render_brief(REPO, self.fake.issues[12], "codex", 2, fix))
        self.write(os.path.join(self.home, "logs", "codex", name + ".log"), "\n".join(log_lines) + "\n")
        self.run_pass()

    def test_a_fix_with_nothing_to_change_asks_for_a_new_review_not_a_failure(self):
        self.failed_fix([
            "[2030-01-01T00:00:00Z] example-owner.example-repo-12-r2 start, lane codex",
            "I didn't change any code: commit abc1234 already fixed this finding.",
            "The thread is stale; /Users/example/x.py measures 5.",
            "rsync(1): error: opencode: (l)stat: No such file or directory",
            "[2030-01-01T00:01:00Z] example-owner.example-repo-12-r2 failed: agent made no commit on loom/12-fix-the-thing"])
        self.assertEqual(self.fake.names(12), ["loom:ticket", "lane:codex", "loom:pr"])
        said, again = [c["body"] for c in self.fake.comments[99]]
        self.assertTrue(said.startswith("Lane codex made no change for this fix. It said:\n\n> I didn't"))
        self.assertIn("> The thread is stale; <path> measures 5.", said)
        self.assertNotIn("rsync", said)
        self.assertNotIn("start, lane", said)
        self.assertEqual(again, station.REVIEW_AGAIN)
        self.assertIn("asked to review it again", self.fake.comments[12][-1]["body"])

    def test_a_fix_with_nothing_to_change_is_recognised_under_its_failure_class(self):
        self.failed_fix([
            "[2030-01-01T00:01:00Z] x failed: [work] agent made no commit on loom/12-fix-the-thing"])
        self.assertEqual(self.fake.names(12), ["loom:ticket", "lane:codex", "loom:pr"])
        self.assertEqual(self.fake.comments[99][-1]["body"], station.REVIEW_AGAIN)
    def test_a_fix_whose_branch_moved_goes_back_to_its_pull_request_not_a_failure(self):
        self.failed_fix([
            "[2030-01-01T00:01:00Z] example-owner.example-repo-12-r2 failed: branch moved: "
            "loom/12-fix-the-thing is at 0123456789ab on the remote, not ba9876543210 as when "
            "the run started"])
        self.assertEqual(self.fake.names(12), ["loom:ticket", "lane:codex", "loom:pr"])
        # Nothing failed, so nothing is posted: the new head's review says the rest.
        self.assertFalse(any("did not push" in c["body"] or "failed" in c["body"]
                             for c in self.fake.comments[12][-1:]
                             if not c["body"].startswith("Claimed")))
        self.assertEqual(self.fake.comments[99], [])

    def test_a_lane_explanation_is_redacted_of_credentials_before_it_is_posted(self):
        self.failed_fix([
            "I checked it. api_key=abc123 and Authorization: Bearer abcdefghijklmnop1234",
            "also ghp_" + "a1B2c3D4e5F6g7H8i9J0 leaked, plus " + "x" * 40,
            "[2030-01-01T00:01:00Z] x failed: agent made no commit on loom/12-fix-the-thing"])
        said = self.fake.comments[99][0]["body"]
        self.assertIn("I checked it.", said)
        for secret in ("abc123", "abcdefghijklmnop1234", "ghp_", "x" * 32):
            self.assertNotIn(secret, said)
        self.assertIn("<redacted>", said)

    def test_a_plain_brief_with_no_commit_still_fails(self):
        self.failed_fix([
            "[2030-01-01T00:01:00Z] x failed: agent made no commit on loom/12-fix-the-thing"],
            fix_body=None)
        self.assertIn("loom:failed", self.fake.names(12))
        self.assertEqual(self.fake.comments[99], [])


PLAIN = "Difficulty: hard\n\n## Why\nBecause.\n\n## What\nDo it.\n"
EASY = PLAIN.replace("hard", "easy")


class RetryTest(Base):
    """A limit or transient failure moves the ticket to another lane; a work
    failure, a fix round or a ticket out of retries does not."""

    def failed(self, lane="codex", reason="[transient] agent exited 1", body=PLAIN,
               attempt=1, fix=None, number=12):
        self.fake.add(number, lane=lane, body=body, state="running")
        for _ in range(attempt):
            self.fake.comments[number].append(
                {"body": "Claimed for lane %s (body sha256 0123456789ab)" % lane})
        name = tickets.ticket_name(REPO, number, attempt)
        self.write(self.inbox(lane, "failed", name + ".md"),
                   tickets.render_brief(REPO, self.fake.issues[number], lane, attempt, fix))
        self.write(os.path.join(self.home, "logs", lane, name + ".log"),
                   "[t] %s start, lane %s\n[t] %s failed: %s\n" % (name, lane, name, reason))
        return name

    def bodies(self, number=12):
        return [c["body"] for c in status.expand(self.fake.comments[number], AUTHORS)]

    def test_a_transient_failure_moves_to_another_provider_on_a_fresh_branch(self):
        # codex-2 shares codex's provider (and so its model): never the retry.
        self.heartbeat("codex-2")
        self.heartbeat("anthropic")
        self.failed()
        self.run_pass()
        retry = [b for b in self.bodies() if b.startswith("Automatic retry")]
        self.assertEqual(retry, ["Automatic retry 1 of 2 (transient): moved from lane codex to "
                                 "lane anthropic. Lane codex failed: [transient] agent exited 1"])
        # The same pass claims it for anthropic as a new attempt.
        self.assertEqual(self.fake.names(12), ["loom:ticket", "lane:anthropic", "loom:running",
                                               "claim:alice-example"])
        name = "example-owner.example-repo-12-r2"
        self.assertEqual(self.listing("anthropic"), [name + ".md"])
        with open(self.inbox("anthropic", name + ".md")) as fh:
            text = fh.read()
        self.assertIn("Branch: loom/12-fix-the-thing-r2\n", text)
        self.assertIn("Lane: anthropic\n", text)
        self.assertNotIn("loom:failed", self.fake.names(12))

    def test_runner_model_refusal_is_reported_as_transient(self):
        self.heartbeat("anthropic")
        body = BODY.replace("Model: model-x", "Model: claude-sonnet-5-5")
        self.failed(lane="opencode-go", body=body,
                    reason="[transient] model claude-sonnet-5-5 is not an opencode-go model")
        self.run_pass()
        self.assertIn("lane:anthropic", self.fake.names(12))
        self.assertIn("Automatic retry 1 of 2 (transient)", self.bodies()[-2])

    def test_a_retry_is_reported_once(self):
        self.heartbeat("anthropic")
        self.failed()
        self.run_pass()
        self.run_pass()
        self.assertEqual(len([b for b in self.bodies() if b.startswith("Automatic retry")]), 1)

    def test_a_spark_that_timed_out_is_not_retried_on_another_spark(self):
        for lane in ("spark", "spark-2", "spark-4", "codex"):
            self.heartbeat(lane)
        self.failed(lane="spark-3", reason="[transient] agent exited 124")
        self.run_pass()
        names = self.fake.names(12)
        self.assertFalse([n for n in names if n.startswith("lane:spark")], names)
        self.assertIn("lane:codex", names)

    def test_a_failure_on_one_opencode_go_slot_is_not_retried_on_another(self):
        for lane in ("opencode-go", "opencode-go-3", "anthropic"):
            self.heartbeat(lane)
        self.failed(lane="opencode-go-2", reason="[transient] agent exited 1")
        self.run_pass()
        self.assertIn("moved from lane opencode-go-2 to lane anthropic.", self.bodies()[-2])

    def test_a_limit_failure_skips_the_providers_other_slots(self):
        for lane in ("anthropic", "anthropic-3", "codex"):
            self.heartbeat(lane)
        self.failed(lane="anthropic-2", reason="[limit] agent exited 1: provider limit reached")
        self.run_pass()
        self.assertIn("moved from lane anthropic-2 to lane codex.", self.bodies()[-2])
        self.assertIn("lane:codex", self.fake.names(12))

    def test_a_provider_limit_from_the_runner_uses_the_bounded_retry(self):
        self.heartbeat("codex-2")
        self.heartbeat("anthropic")
        self.failed(reason="[limit] agent exited 1: provider limit reached")
        self.run_pass()
        self.assertIn("lane:anthropic", self.fake.names(12))
        self.assertIn("Automatic retry 1 of 2 (limit): moved from lane codex to lane anthropic.",
                      self.bodies()[-2])

    def test_an_unavailable_lane_is_skipped(self):
        self.heartbeat("codex-2", available=False)
        self.heartbeat("anthropic", age=3600)
        self.heartbeat("opencode-go")
        self.failed()
        self.run_pass()
        self.assertIn("lane:opencode-go", self.fake.names(12))

    def test_an_idle_lane_wins_over_a_busy_one(self):
        self.heartbeat("codex-2")
        self.heartbeat("anthropic")
        self.write(self.inbox("codex-2", "other.md"), "busy\n")
        self.failed()
        self.run_pass()
        self.assertIn("moved from lane codex to lane anthropic.",
                      [b for b in self.bodies() if b.startswith("Automatic")][0])

    def test_the_tickets_model_limits_the_lanes(self):
        # Model: model-x is not a claude model, so no anthropic slot takes it.
        self.heartbeat("anthropic")
        self.failed(body=BODY)
        self.run_pass()
        self.assertEqual(self.fake.names(12), ["loom:ticket", "lane:codex", "loom:failed"])
        self.assertIn("Not retried: no other eligible lane is available.", self.bodies()[-1])

    def test_a_ticket_body_that_does_not_parse_is_not_retried(self):
        # Model: given twice with two values, so no lane's eligibility can be judged.
        self.heartbeat("anthropic")
        self.failed(body=BODY)
        self.fake.issues[12]["body"] = "Model: claude-sonnet-5-5\nModel: claude-opus-5-5\n\nWork."
        self.run_pass()
        self.assertEqual(self.fake.names(12), ["loom:ticket", "lane:codex", "loom:failed"])
        self.assertIn("Not retried: no other eligible lane is available.", self.bodies()[-1])

    def test_a_limit_with_no_other_lane_to_retry_on_is_not_a_failure(self):
        # On 2026-10-18 such a limit went to loom:failed and the factory
        # climbed the ticket a rung of the model ladder for it.
        self.failed(body=BODY, reason="[limit] agent exited 3: provider limit reached; "
                                      "lane codex waits until 2030-01-02T00:00:00Z")
        self.run_pass()
        self.assertNotIn("loom:failed", self.fake.names(12))
        self.assertIn("lane:codex", self.fake.names(12))
        self.assertEqual([b for b in self.bodies() if b.startswith("Lane codex")], [])

    def test_free_lanes_take_only_easy_tickets_on_public_repos(self):
        self.heartbeat("zen")
        self.failed(body=PLAIN)
        self.run_pass()
        self.assertIn("loom:failed", self.fake.names(12))
        self.failed(body=EASY, number=13)
        self.run_pass()
        self.assertIn("loom:failed", self.fake.names(13))  # the repo is private
        self.fake.private = False
        self.failed(body=EASY, number=14)
        self.run_pass()
        self.assertIn("lane:zen", self.fake.names(14))

    def test_the_retry_cap_ends_in_failed(self):
        self.heartbeat("codex-2")
        self.heartbeat("anthropic")
        self.failed()
        self.fake.comments[12][:0] = [
            {"body": "Automatic retry 1 of 2 (transient): moved from lane opencode-go to lane "
                     "anthropic. Lane opencode-go failed: [transient] agent exited 1"},
            {"body": "Automatic retry 2 of 2 (limit): moved from lane anthropic to lane codex. "
                     "Lane anthropic failed: [limit] agent exited 1"},
        ]
        self.run_pass()
        self.assertEqual(self.fake.names(12), ["loom:ticket", "lane:codex", "loom:failed"])
        self.assertEqual(self.bodies()[-1], "Lane codex failed: [transient] agent exited 1. "
                                            "Not retried: 2 of 2 automatic retries used.")
        self.assertEqual(self.listing("codex-2"), [])

    def test_the_cap_comes_from_the_settings(self):
        self.heartbeat("codex-2")
        self.settings.retry_max = 0
        self.failed()
        self.run_pass()
        self.assertIn("loom:failed", self.fake.names(12))

    def test_lanes_an_earlier_retry_left_are_not_chosen_again(self):
        self.heartbeat("codex-2")
        self.heartbeat("anthropic")
        self.failed()
        self.fake.comments[12][:0] = [
            {"body": "Automatic retry 1 of 2 (transient): moved from lane codex-2 to lane "
                     "codex. Lane codex-2 failed: [transient] agent exited 1"}]
        self.run_pass()
        self.assertIn("Automatic retry 2 of 2 (transient): moved from lane codex to lane "
                      "anthropic.", self.bodies()[-2])

    def test_a_failure_the_brain_saw_starts_a_new_count(self):
        self.heartbeat("anthropic")
        self.failed()
        self.fake.comments[12][:0] = [
            {"body": "Automatic retry 1 of 2 (transient): moved from lane a to lane b. x"},
            {"body": "Automatic retry 2 of 2 (transient): moved from lane b to lane anthropic. x"},
            {"body": "Lane anthropic failed: [work] agent made no commit on loom/12-x"},
        ]
        self.run_pass()
        self.assertIn("lane:anthropic", self.fake.names(12))

    def test_a_retry_whose_relabel_failed_is_finished_without_a_second_comment(self):
        self.heartbeat("codex-2")
        self.heartbeat("anthropic")
        self.failed()
        self.fake.comments[12].append(
            {"body": "Automatic retry 1 of 2 (transient): moved from lane codex to lane "
                     "anthropic. Lane codex failed: [transient] agent exited 1"})
        self.run_pass()
        self.assertIn("lane:anthropic", self.fake.names(12))
        self.assertEqual(len([b for b in self.bodies() if b.startswith("Automatic")]), 1)

    def test_a_work_failure_waits_for_the_brain(self):
        self.heartbeat("codex-2")
        self.failed(reason="[work] agent made no commit on loom/12-fix-the-thing")
        self.run_pass()
        self.assertEqual(self.fake.names(12), ["loom:ticket", "lane:codex", "loom:failed"])
        self.assertEqual(self.bodies()[-1],
                         "Lane codex failed: [work] agent made no commit on loom/12-fix-the-thing")
        self.assertEqual(self.listing("codex-2"), [])

    def test_three_recent_attempts_with_no_earlier_failure_flag_the_ticket(self):
        self.failed(body=BODY, reason="[work] agent made no commit on loom/12-fix-the-thing")
        at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 600))
        self.fake.comments[12][:] = [
            {"body": "Claimed for lane %s (body sha256 0123456789ab)" % lane, "created_at": at, "user": {"login": "alice-example"}}
            for lane in ("codex", "codex-2", "codex")]
        self.run_pass()
        self.assertIn("loom:runaway", self.fake.names(12))
        table = self.bodies()[-1]
        self.assertIn("Runaway: three attempts within six hours.", table)
        self.assertEqual(table.count("\n| %s |" % at), 3)
        self.assertIn("| codex-2 | model-x | - |", table)
        self.assertIn("| codex | model-x | [work] agent made no commit", table)

    def test_attempts_that_ended_transient_or_limit_do_not_count_toward_runaway(self):
        self.failed(body=BODY, reason="[work] agent made no commit on loom/12-fix-the-thing")
        at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 600))
        who = {"created_at": at, "user": {"login": "alice-example"}}
        claim = "Claimed for lane %s (body sha256 0123456789ab)"
        self.fake.comments[12][:] = [
            dict({"body": claim % "codex"}, **who),
            dict({"body": "Lane codex failed: [transient] agent exited 1"}, **who),
            dict({"body": claim % "codex-2"}, **who),
            dict({"body": "Lane codex-2 failed: [limit] agent exited 1: provider limit reached"}, **who),
            dict({"body": claim % "codex"}, **who)]
        self.run_pass()
        self.assertNotIn("loom:runaway", self.fake.names(12))

    def test_a_runaway_comment_says_how_many_attempts_it_left_out(self):
        self.failed(body=BODY, reason="[work] agent made no commit on loom/12-fix-the-thing")
        at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 600))
        who = {"created_at": at, "user": {"login": "alice-example"}}
        claim = "Claimed for lane %s (body sha256 0123456789ab)"
        self.fake.comments[12][:] = [
            dict({"body": claim % "codex"}, **who),
            dict({"body": "Lane codex failed: [transient] agent exited 1"}, **who),
            dict({"body": claim % "codex"}, **who),
            dict({"body": "Lane codex failed: [work] agent made no commit on loom/12-x"}, **who),
            dict({"body": claim % "codex-2"}, **who),
            dict({"body": "Lane codex-2 failed: [work] agent made no commit on loom/12-y"}, **who),
            dict({"body": claim % "codex"}, **who)]
        self.run_pass()
        self.assertIn("loom:runaway", self.fake.names(12))
        self.assertIn("Not counted as work attempts: 1 that ended [transient], [limit] or "
                      "[factory].", self.bodies()[-1])

    def test_the_attempt_table_names_the_model_each_attempt_ran(self):
        # No Model: line: each claim ran its lane's configured model, a slot's
        # own setting first, never the issue's "unspecified" or a fixed map.
        self.failed(body=PLAIN, reason="[work] agent made no commit on loom/12-fix-the-thing")
        times = [time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - s)) for s in (900, 600, 300)]
        self.fake.comments[12][:] = [
            {"body": "Claimed for lane %s (body sha256 0123456789ab)" % lane, "created_at": at,
             "user": {"login": "alice-example"}}
            for lane, at in zip(("codex", "codex-2", "codex-3"), times)]
        env = {k: v for k, v in os.environ.items() if not re.fullmatch(r"LOOM_\w+_MODEL", k)}
        env.update(LOOM_CODEX_MODEL="model-codex", LOOM_CODEX_2_MODEL="model-slot")
        with mock.patch.dict(os.environ, env, clear=True):
            self.run_pass()
        table = self.bodies()[-1]
        self.assertIn("| %s | codex | model-codex |" % times[0], table)
        self.assertIn("| %s | codex-2 | model-slot |" % times[1], table)
        self.assertIn("| %s | codex-3 | model-codex |" % times[2], table)
        self.assertNotIn("unspecified", table)

    def test_the_same_failure_reason_twice_in_a_row_flags_the_ticket(self):
        reason = "[work] could not commit the agent's changes"
        self.failed(body=BODY, reason=reason)
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 8 * 3600))
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60))
        self.fake.comments[12][:] = [
            {"body": "Claimed for lane codex (body sha256 0123456789ab)", "created_at": old, "user": {"login": "alice-example"}},
            {"body": "Lane codex failed: " + reason, "created_at": old, "user": {"login": "alice-example"}},
            {"body": "Claimed for lane codex-2 (body sha256 0123456789ab)", "created_at": now, "user": {"login": "alice-example"}}]
        self.run_pass()
        self.assertIn("loom:runaway", self.fake.names(12))
        self.assertNotIn("loom:failed", self.fake.names(12))
        table = self.bodies()[-1]
        self.assertIn("Runaway: repeated failure reason.", table)
        self.assertIn("| %s | codex | model-x | %s |" % (old, reason), table)
        self.assertIn("| %s | codex-2 | model-x | %s |" % (now, reason), table)

    def test_a_strangers_comments_do_not_flag_the_ticket(self):
        reason = "[work] could not commit the agent's changes"
        self.failed(body=BODY, reason=reason)
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 8 * 3600))
        self.fake.comments[12][:] = [
            {"body": "Claimed for lane codex (body sha256 0123456789ab)", "created_at": old},
            {"body": "Lane codex failed: " + reason, "created_at": old},
            {"body": "Claimed for lane codex-2 (body sha256 0123456789ab)", "created_at": old}]
        self.run_pass()
        self.assertNotIn("loom:runaway", self.fake.names(12))

    def test_a_different_failure_reason_does_not_flag_the_ticket(self):
        self.failed(body=BODY, reason="[work] agent made no commit on loom/12-fix-the-thing")
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 8 * 3600))
        self.fake.comments[12][:] = [
            {"body": "Claimed for lane codex (body sha256 0123456789ab)", "created_at": old, "user": {"login": "alice-example"}},
            {"body": "Lane codex failed: [work] could not commit the agent's changes", "created_at": old, "user": {"login": "alice-example"}},
            {"body": "Claimed for lane codex-2 (body sha256 0123456789ab)", "created_at": old, "user": {"login": "alice-example"}}]
        self.run_pass()
        self.assertNotIn("loom:runaway", self.fake.names(12))

    def test_a_runaway_ticket_is_not_claimed(self):
        self.fake.add(12)
        self.fake.issues[12]["labels"].append({"name": "loom:runaway"})
        self.run_pass()
        self.assertEqual(self.listing(), [])
        self.assertEqual(self.fake.comments[12], [])
        self.assertNotIn("loom:running", self.fake.names(12))

    def test_a_reason_without_a_class_is_a_work_failure(self):
        self.heartbeat("codex-2")
        self.failed(reason="agent exited 1")
        self.run_pass()
        self.assertIn("loom:failed", self.fake.names(12))

    def test_a_failed_fix_brief_returns_to_pr_without_a_new_attempt(self):
        self.heartbeat("codex-2")
        fix = tickets.parse_fix(REPO, FIX_BODY)
        self.failed(reason="[transient] agent exited 1", attempt=2, fix=fix)
        self.run_pass()
        self.assertEqual(self.fake.names(12), ["loom:ticket", "lane:codex", "loom:pr"])
        self.assertEqual(self.bodies()[-1], "Lane codex failed: [transient] agent exited 1. "
                         "This was a fix round on pull request #99; the ticket is back at "
                         "loom:pr and the pull request is unchanged.")
        self.assertEqual(self.listing("codex-2"), [])

    def test_a_fix_brief_that_fails_its_work_goes_to_failed_so_the_factory_climbs(self):
        self.heartbeat("codex-2")
        fix = tickets.parse_fix(REPO, FIX_BODY)
        self.failed(reason="[work] validation failed: the check exited 2", attempt=2, fix=fix)
        self.run_pass()
        self.assertEqual(self.fake.names(12), ["loom:ticket", "lane:codex", "loom:failed"])
        self.assertEqual(self.bodies()[-1], "Lane codex failed: [work] validation failed: the "
                         "check exited 2. This was a fix round on pull request #99; the ticket "
                         "is back at loom:failed and the pull request is unchanged.")
        self.assertEqual(self.listing("codex-2"), [])

    def test_retry_chain_and_model_rules(self):
        self.assertEqual(station.failure_class("[limit] x"), "limit")
        self.assertEqual(station.failure_class("limit x"), "work")
        self.assertTrue(station.lane_takes_model("anthropic-3", "claude-sonnet-5-5"))
        self.assertFalse(station.lane_takes_model("codex-2", "claude-sonnet-5-5"))
        self.assertTrue(station.lane_takes_model("codex", "gpt-x"))
        self.assertFalse(station.lane_takes_model("zen", "opencode/paid"))
        self.assertTrue(station.needs_public_repo("agy", ""))
        self.assertFalse(station.needs_public_repo("opencode-go", "opencode-go/kimi"))
        self.assertTrue(station.needs_public_repo("opencode-go", "opencode-go/muse-spark-contributor"))


class SilentLaneTest(Base):
    """A running ticket on a lane with no heartbeat for 30 minutes (loom#538):
    the station reports each lane's silence to the factory and keeps the lanes
    that held a running ticket for the invariant check."""

    def station(self):
        return station.Station(GitHub("token", transport=self.fake), self.settings,
                               now=time.time())

    def test_a_lane_without_a_live_heartbeat_reports_how_long_it_has_been_silent(self):
        self.heartbeat("codex", age=31 * 60)
        self.heartbeat("codex-2", age=60)
        self.heartbeat("opencode-go", available=False, age=60)
        silent = self.station()._silent_lanes()
        self.assertAlmostEqual(silent["codex"], 31 * 60, delta=5)
        self.assertAlmostEqual(silent["opencode-go"], 60, delta=5)
        self.assertNotIn("codex-2", silent)
        # A lane that never reported has no silence to measure.
        self.assertNotIn("claude", silent)

    def test_the_lanes_found_holding_a_running_ticket_become_an_invariant_fact(self):
        st = self.station()
        st._record_silent({"codex": [(REPO, 12)]}, {"codex": 1860.0})
        (run,) = st._invariant_silent()
        self.assertEqual((run.lane, run.silent_for, run.at), ("codex", 1860.0, st.now))
        self.assertEqual(len(run.tickets), 1)
        (breach,) = invariants.silent_lane(invariants.Facts(st.now, silent_runs=[run]))
        self.assertIn("codex", breach.title)

    def test_nothing_found_records_nothing(self):
        st = self.station()
        st._record_silent({}, {})
        self.assertEqual(st._invariant_silent(), [])


class DeadLaneRunawayTest(Base):
    """A runaway held because one dead lane kept failing it (loom#565): every
    attempt since its last brief ran on a lane with an open runs-and-none-done
    invariant. The station releases the hold and briefs the ticket to a free
    lane of its tier, naming the lane fault; this is not a failure, so the
    ladder does not move."""
    MEDIUM = "Difficulty: medium\n\n## Why\nBecause.\n\n## What\nDo it.\n"

    def bodies(self, number=12):
        return [c["body"] for c in status.expand(self.fake.comments[number], AUTHORS)]

    def setUp(self):
        super().setUp()
        self.fake.add_goal(40)
        self.settings.invariant_goal = "%s#40" % REPO

    def key(self, lane):
        return "lane-no-success/%s" % hashlib.sha256(lane.encode()).hexdigest()[:12]

    def file_invariant(self, lane, number=50):
        self.fake.issues[number] = {
            "number": number,
            "title": "Lane %s: 7 runs in 24 hours and none done" % lane,
            "body": "<!-- loom:invariant v1 key=%s -->\n"
                    "The station found one of the factory's own invariants broken.\n"
                    "\nPart of #40\n" % self.key(lane),
            "state": "open", "user": {"login": "alice-example"},
            "created_at": "2030-01-01T00:00:00Z",
            "labels": [{"name": "loom:triage"}, {"name": "loom:invariant"}],
        }
        self.fake.comments[number] = []

    def hold(self, lane="codex-2", claims=("codex-2", "codex-2"), brief=True, number=12):
        self.fake.add(number, lane=lane, body=self.MEDIUM, state="briefed")
        self.fake.issues[number]["labels"].append({"name": "loom:runaway"})
        at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
        comments = []
        if brief:
            comments.append({
                "body": "The station gave this queued ticket lane %s (tier medium)." % lane,
                "created_at": at, "user": {"login": "alice-example"}})
        for claimed in claims:
            comments.append({
                "body": "Claimed for lane %s (body sha256 0123456789ab)" % claimed,
                "created_at": at, "user": {"login": "alice-example"}})
        comments.append({"body": "Runaway: three attempts within six hours.",
                         "created_at": at, "user": {"login": "alice-example"}})
        self.fake.comments[number] = comments

    def test_a_runaway_on_one_dead_lane_is_rebriefed_to_a_free_lane_of_its_tier(self):
        self.file_invariant("codex-2")
        self.hold(lane="codex-2")
        self.run_pass()
        names = self.fake.names(12)
        self.assertNotIn("loom:runaway", names)
        self.assertNotIn("lane:codex-2", names)
        self.assertIn("lane:codex", names)
        bodies = self.bodies()
        released = [b for b in bodies if b.startswith("<!-- loom:runaway-dead-lane v1")]
        self.assertEqual(len(released), 1)
        self.assertIn("Runaway hold released", released[0])
        self.assertIn("codex-2", released[0])
        self.assertIn("codex", released[0])
        self.assertFalse(any(b.startswith("Lane ") and "failed:" in b for b in bodies))

    def test_a_runaway_with_attempts_on_two_lanes_keeps_its_hold(self):
        self.file_invariant("codex-2")
        self.hold(lane="codex-2", claims=("codex-2", "codex"), brief=False)
        self.run_pass()
        self.assertIn("loom:runaway", self.fake.names(12))
        self.assertEqual(
            [b for b in self.bodies() if b.startswith("<!-- loom:runaway-dead-lane v1")], [])

    def test_a_runaway_on_a_dead_lane_with_no_free_lane_stays_held_and_says_why(self):
        self.file_invariant("codex-2")
        self.hold(lane="codex-2")
        self.heartbeat("codex", available=False)
        self.run_pass()
        names = self.fake.names(12)
        self.assertIn("loom:runaway", names)
        self.assertIn("lane:codex-2", names)
        bodies = self.bodies()
        held = [b for b in bodies if b.startswith("<!-- loom:runaway-dead-lane v1")]
        self.assertEqual(len(held), 1)
        self.assertIn("no lane of its difficulty tier is free", held[0])
        self.assertIn("codex-2", held[0])
        # The next pass does not comment again.
        self.run_pass()
        self.assertEqual(
            len([b for b in self.bodies() if b.startswith("<!-- loom:runaway-dead-lane v1")]), 1)

    def test_attempts_before_the_last_brief_do_not_count(self):
        # Two earlier claims on another lane, then a re-brief and two claims
        # on the dead lane: only the claims since the last brief count.
        self.file_invariant("codex-2")
        self.fake.add(12, lane="codex-2", body=self.MEDIUM, state="briefed")
        self.fake.issues[12]["labels"].append({"name": "loom:runaway"})
        at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
        who = {"created_at": at, "user": {"login": "alice-example"}}
        self.fake.comments[12] = [
            dict({"body": "The station gave this queued ticket lane spark (tier medium)."}, **who),
            dict({"body": "Claimed for lane spark (body sha256 0123456789ab)"}, **who),
            dict({"body": "Claimed for lane spark (body sha256 0123456789ab)"}, **who),
            dict({"body": "Not a failure (lane spark is at a limit): the factory moved this "
                           "ticket to lane codex-2, in its difficulty tier."}, **who),
            dict({"body": "Claimed for lane codex-2 (body sha256 0123456789ab)"}, **who),
            dict({"body": "Claimed for lane codex-2 (body sha256 0123456789ab)"}, **who),
            dict({"body": "Runaway: three attempts within six hours."}, **who),
        ]
        self.run_pass()
        names = self.fake.names(12)
        self.assertNotIn("loom:runaway", names)
        self.assertIn("lane:codex", names)

    def test_no_open_invariant_for_the_lane_keeps_the_hold(self):
        self.hold(lane="codex-2")
        self.run_pass()
        self.assertIn("loom:runaway", self.fake.names(12))
        self.assertEqual(
            [b for b in self.bodies() if b.startswith("<!-- loom:runaway-dead-lane v1")], [])


class ExpiredKeyTest(Base):
    """An [auth] brief is the provider refusing the key, not the lane's work:
    the ticket goes back where it was, no retry is spent on it, and however
    many keys expire it never flags runaway (loom#744)."""

    REASON = "[auth] agent exited 1: API key expired: renew /prod/loom/opencode-go/key"

    def failed(self, lane="opencode-go", number=12, attempt=1, reason=None):
        reason = reason or self.REASON
        self.fake.add(number, lane=lane, body=PLAIN, state="running")
        for _ in range(attempt):
            self.fake.comments[number].append(
                {"body": "Claimed for lane %s (body sha256 0123456789ab)" % lane})
        name = tickets.ticket_name(REPO, number, attempt)
        self.write(self.inbox(lane, "failed", name + ".md"),
                   tickets.render_brief(REPO, self.fake.issues[number], lane, attempt, None))
        self.write(os.path.join(self.home, "logs", lane, name + ".log"),
                   "[t] %s start, lane %s\n[t] %s failed: %s\n" % (name, lane, name, reason))
        return name

    def bodies(self, number=12):
        return [c["body"] for c in status.expand(self.fake.comments[number], AUTHORS)]

    def test_auth_is_a_failure_class_not_work(self):
        self.assertEqual(station.failure_class(self.REASON), "auth")

    def test_an_expired_key_is_not_a_failure_and_spends_no_retry(self):
        self.failed()
        self.run_pass()
        # Back to briefed, where the factory moves it to a lane whose key works.
        self.assertEqual(self.fake.names(12), ["loom:ticket", "lane:opencode-go", "loom:briefed"])
        self.assertFalse(any(b.startswith("Automatic retry") for b in self.bodies()))
        self.assertNotIn("loom:failed", self.fake.names(12))
        # No comment: the runner already filed the operator one ask for the key.
        self.assertFalse(any("could not authenticate" in b for b in self.bodies()))

    def test_expired_key_failures_never_flag_runaway_or_spend_an_attempt(self):
        at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 600))
        self.failed(attempt=3)
        self.fake.comments[12][:] = [
            {"body": "Claimed for lane %s (body sha256 0123456789ab)" % lane,
             "created_at": at, "user": {"login": "alice-example"}}
            for lane in ("opencode-go", "opencode-go-2", "opencode-go-3")]
        before = factory.attempts(self.fake.comments[12])
        self.run_pass()
        self.assertNotIn("loom:runaway", self.fake.names(12))
        self.assertNotIn("loom:failed", self.fake.names(12))
        self.assertEqual(factory.attempts(self.fake.comments[12]), before)


class FactoryIncidentTest(Base):
    """A [factory] brief is not a work failure: the ticket stays running on
    its lane and keeps its rung, and a closed incident re-queues it."""

    def failed(self, lane="muse", number=12, attempt=1, reason=None):
        reason = reason or ("[factory] setup failed: validation environment unavailable: "
                            "the check exited 127 on the unmodified base; "
                            "factory incident %s#99" % REPO)
        self.fake.add(number, lane=lane, body=PLAIN, state="running")
        for _ in range(attempt):
            self.fake.comments[number].append(
                {"body": "Claimed for lane %s (body sha256 0123456789ab)" % lane})
        name = tickets.ticket_name(REPO, number, attempt)
        self.write(self.inbox(lane, "failed", name + ".md"),
                   tickets.render_brief(REPO, self.fake.issues[number], lane, attempt, None))
        self.write(os.path.join(self.home, "logs", lane, name + ".log"),
                   "[t] %s start, lane %s\n[t] %s failed: %s\n" % (name, lane, name, reason))
        return name

    def bodies(self, number=12):
        return [c["body"] for c in status.expand(self.fake.comments[number], AUTHORS)]

    def incident(self, closed_at="2030-01-01T00:10:00Z"):
        self.fake.factory_incidents = [{
            "number": 99,
            "title": "Factory incident: %s validation environment" % REPO,
            "state": "closed", "closed_at": closed_at,
            "body": ("<!-- loom:factory-incident v1 -->\n\nRepo: %s\nKind: environment\n"
                     "Ticket: %s#12\n\ncheck: make check (exit 127)\n" % (REPO, REPO)),
            "labels": [{"name": "loom:factory"}]}]

    def test_factory_is_a_failure_class_not_work(self):
        self.assertEqual(station.failure_class("[factory] setup failed: x"), "factory")
        self.assertEqual(station.failure_class("[work] agent made no commit"), "work")

    def test_a_factory_failure_holds_the_ticket_and_keeps_its_rung(self):
        self.heartbeat("muse")
        self.failed()
        self.run_pass()
        self.assertEqual(set(self.fake.names(12)),
                         {"loom:ticket", "lane:muse", "loom:running", "wait:incident"})
        self.assertFalse(any(b.startswith("Automatic retry") for b in self.bodies()))
        hold = [b for b in self.bodies() if "held" in b]
        self.assertEqual(len(hold), 1)
        self.assertIn("keeps its rung", hold[0])
        self.assertIn("factory incident %s#99" % REPO, hold[0])

    def test_factory_failures_never_flag_runaway(self):
        self.heartbeat("muse")
        at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 600))
        self.failed(attempt=3)
        self.fake.comments[12][:] = [
            {"body": "Claimed for lane muse (body sha256 0123456789ab)",
             "created_at": at, "user": {"login": "alice-example"}}
            for _ in range(3)]
        self.run_pass()
        self.assertNotIn("loom:runaway", self.fake.names(12))
        self.assertNotIn("loom:failed", self.fake.names(12))

    def test_a_closed_factory_incident_requeues_its_ticket(self):
        self.heartbeat("muse")
        self.failed()
        self.run_pass()
        self.incident()
        with mock.patch.object(station, "FACTORY_REPO", REPO):
            self.run_pass()
        self.assertEqual(self.fake.names(12), ["loom:ticket", "lane:muse", "loom:queued"])
        self.assertTrue(any("back in the queue" in b for b in self.bodies()))

    def test_a_closed_factory_incident_clears_the_runaway_hold_of_its_ticket(self):
        self.heartbeat("muse")
        self.failed()
        self.run_pass()
        self.fake.issues[12]["labels"].append({"name": "loom:runaway"})
        self.incident()
        with mock.patch.object(station, "FACTORY_REPO", REPO):
            self.run_pass()
        self.assertEqual(self.fake.names(12), ["loom:ticket", "lane:muse", "loom:queued"])
        self.assertTrue(any("its loom:runaway hold is cleared" in b for b in self.bodies()))

    def test_a_claim_newer_than_the_incident_close_blocks_the_requeue(self):
        self.heartbeat("muse")
        self.failed()
        self.run_pass()
        self.incident(closed_at="2030-01-01T00:10:00Z")
        self.fake.comments[12].append(
            {"body": "Claimed for lane muse (body sha256 0123456789ab)",
             "created_at": "2030-01-01T00:20:00Z", "user": {"login": "alice-example"}})
        with mock.patch.object(station, "FACTORY_REPO", REPO):
            self.run_pass()
        self.assertIn("loom:running", self.fake.names(12))
        self.assertFalse(any("back in the queue" in b for b in self.bodies()))

    def test_a_failed_incident_read_says_so_and_keeps_the_hold(self):
        self.heartbeat("muse")
        self.failed()
        self.run_pass()
        self.incident()
        with mock.patch.object(station, "FACTORY_REPO", REPO), \
                mock.patch.object(station.Station, "_closed_incidents",
                                  side_effect=station.GitHubError("boom", 502)), \
                mock.patch("bus.station._say") as say:
            self.run_pass()
        said = [c.args[0] for c in say.call_args_list]
        self.assertIn("factory release: could not read the closed incidents: boom", said)
        self.assertIn("loom:running", self.fake.names(12))
        self.assertFalse(any("back in the queue" in b for b in self.bodies()))
        with mock.patch.object(station, "FACTORY_REPO", REPO):
            self.run_pass()
        self.assertIn("loom:queued", self.fake.names(12))

    def test_a_failed_ticket_read_says_so_and_keeps_the_hold(self):
        self.heartbeat("muse")
        self.failed()
        self.run_pass()
        self.incident()
        for path in ("/repos/%s/issues/12" % REPO, "/repos/%s/issues/12/comments" % REPO):
            def transport(method, url, headers, body, path=path):
                if method == "GET" and urlsplit(url).path == path:
                    raise station.GitHubError("boom", 502)
                return self.fake(method, url, headers, body)
            runner = station.Station(GitHub("token", transport=transport), self.settings)
            with mock.patch.object(station, "FACTORY_REPO", REPO), \
                    mock.patch("bus.station._say") as say:
                runner.release_factory_held()
            said = [c.args[0] for c in say.call_args_list]
            self.assertIn("factory release: could not read %s#12: boom" % REPO, said)
            self.assertIn("loom:running", self.fake.names(12))
        with mock.patch.object(station, "FACTORY_REPO", REPO):
            self.run_pass()
        self.assertIn("loom:queued", self.fake.names(12))


class PruneTest(Base):
    def tree(self, name, age):
        path = os.path.join(self.home, "worktrees", name)
        os.makedirs(os.path.join(path, ".git"))
        self.write(os.path.join(path, "a.py"), "x\n")
        old = time.time() - age
        os.utime(path, (old, old))
        return path

    def test_a_finished_lane_tree_is_removed_and_a_live_or_fresh_one_kept(self):
        done = self.tree("example.repo-1", station.TREE_KEEP + 60)
        running = self.tree("example.repo-2", station.TREE_KEEP + 60)
        fresh = self.tree("example.repo-3", 60)
        self.write(self.inbox("codex", "example.repo-2.md"), "brief\n")
        self.run_pass()
        self.assertFalse(os.path.exists(done))
        self.assertTrue(os.path.exists(running))
        self.assertTrue(os.path.exists(fresh))

    def test_an_unreadable_inbox_stops_the_prune(self):
        done = self.tree("example.repo-1", station.TREE_KEEP + 60)
        unreadable = self.inbox("claude")
        real = os.listdir

        def listdir(path="."):
            # Raised directly, not by chmod, which a root runner ignores.
            if path == unreadable:
                raise PermissionError(13, "Permission denied", path)
            return real(path)

        with mock.patch.object(station.os, "listdir", listdir):
            self.run_pass()
        self.assertTrue(os.path.exists(done))

    def test_a_link_in_the_worktrees_folder_is_removed_without_following_it(self):
        outside = os.path.join(self._tmp.name, "keep-me")
        os.makedirs(outside)
        self.write(os.path.join(outside, "f"), "x\n")
        root = os.path.join(self.home, "worktrees")
        os.makedirs(root)
        link = os.path.join(root, "example.repo-9")
        os.symlink(outside, link)
        old = time.time() - station.TREE_KEEP - 60
        os.utime(link, (old, old), follow_symlinks=False)
        self.run_pass()
        self.assertFalse(os.path.lexists(link))
        self.assertTrue(os.path.exists(os.path.join(outside, "f")))
class LogRetentionTest(Base):
    """Lane logs past LOOM_LOG_KEEP_DAYS are removed, and the loom home's
    size and free disk are reported (loom#643)."""
    DAY = 24 * 3600

    def aged(self, path, days, text="x\n"):
        self.write(path, text)
        old = time.time() - days * self.DAY
        os.utime(path, (old, old))
        return path

    def lane_log(self, slot, name, days):
        return self.aged(os.path.join(self.home, "logs", slot, name), days)

    def ticket_dir(self, slot, name, days, inner_days=None):
        d = os.path.join(self.home, "logs", slot, name)
        self.aged(os.path.join(d, ".claude", "projects", "p", "s.jsonl"),
                  days if inner_days is None else inner_days)
        old = time.time() - days * self.DAY
        for sub in (os.path.join(d, ".claude", "projects", "p"), os.path.join(d, ".claude", "projects"),
                    os.path.join(d, ".claude"), d):
            os.utime(sub, (old, old))
        return d

    def tend(self, free=100 * 1024 ** 3):
        usage = mock.Mock(free=free, total=500 * 1024 ** 3, used=0)
        with mock.patch.object(station.shutil, "disk_usage", return_value=usage), \
                mock.patch("bus.station._say") as say:
            self.reader().tend_disk()
        return [c.args[0] for c in say.call_args_list]

    def reader(self):
        return station.Station(GitHub("token", transport=self.fake), self.settings)

    def test_a_pass_removes_lane_logs_past_retention_and_keeps_the_rest(self):
        old_log = self.lane_log("codex", "example.repo-1.log", 8)
        old_result = self.lane_log("opencode-go-2", "example.repo-1.validation.jsonl", 8)
        old_dir = self.ticket_dir("anthropic", "example.repo-1", 8)
        fresh = self.lane_log("codex", "example.repo-2.log", 1)
        station_log = self.aged(os.path.join(self.home, "logs", "station.log"), 30)
        events = self.aged(os.path.join(self.home, "logs", "events.jsonl"), 30)
        self.run_pass()
        self.assertFalse(os.path.exists(old_log))
        self.assertFalse(os.path.exists(old_result))
        self.assertFalse(os.path.exists(old_dir))
        self.assertTrue(os.path.exists(fresh))
        # Only the lanes' own folders are pruned; the station's logs stay.
        self.assertTrue(os.path.exists(station_log))
        self.assertTrue(os.path.exists(events))

    def test_a_ticket_folder_with_a_fresh_file_inside_is_kept(self):
        d = self.ticket_dir("anthropic", "example.repo-1", 8, inner_days=1)
        self.tend()
        self.assertTrue(os.path.exists(d))

    def test_a_waiting_or_running_briefs_logs_are_kept_however_old(self):
        log = self.lane_log("codex", "example.repo-3.log", 30)
        record = self.lane_log("codex", "example.repo-3.validation.jsonl", 30)
        self.write(self.inbox("claude", "example.repo-3.md"), "brief\n")
        self.tend()
        self.assertTrue(os.path.exists(log))
        self.assertTrue(os.path.exists(record))

    def test_a_transcript_not_yet_uploaded_is_kept_until_the_dashboard_has_it(self):
        self.settings.transcript_url = "https://dashboard.example.com/api/transcripts"
        self.settings.transcript_token_ssm = "/loom/token"
        for name in ("example.repo-4", "example.repo-5", "example.repo-6", "example.repo-7"):
            self.write(self.inbox("codex", "done", name + ".md"), "brief\n")
            self.lane_log("codex", name + ".log", 30)
        self.write(os.path.join(self.home, "state", "station", "transcripts.json"),
                   json.dumps({"codex/example.repo-4": "sent", "codex/example.repo-5": 2,
                               "codex/example.repo-7": "gave up"}))
        self.tend()
        logs = os.path.join(self.home, "logs", "codex")
        self.assertEqual(sorted(os.listdir(logs)), ["example.repo-5.log", "example.repo-6.log"])

    def test_uploads_on_without_a_ledger_remove_nothing(self):
        self.settings.transcript_url = "https://dashboard.example.com/api/transcripts"
        self.settings.transcript_token_ssm = "/loom/token"
        log = self.lane_log("codex", "example.repo-8.log", 30)
        self.tend()
        self.assertTrue(os.path.exists(log))

    def test_an_unreadable_inbox_removes_no_log(self):
        log = self.lane_log("codex", "example.repo-1.log", 30)
        unreadable = self.inbox("claude")
        os.makedirs(unreadable)
        real = os.listdir

        def listdir(path="."):
            if path == unreadable:
                raise PermissionError(13, "Permission denied", path)
            return real(path)

        with mock.patch.object(station.os, "listdir", listdir):
            said = self.tend()
        self.assertTrue(os.path.exists(log))
        self.assertTrue(any("no lane log is removed" in s for s in said), said)

    def test_retention_zero_keeps_every_log(self):
        self.settings.log_keep_days = 0
        log = self.lane_log("codex", "example.repo-1.log", 400)
        self.tend()
        self.assertTrue(os.path.exists(log))

    def test_a_link_in_a_lane_log_folder_is_removed_without_following_it(self):
        outside = os.path.join(self._tmp.name, "keep-me")
        self.write(os.path.join(outside, "f"), "x\n")
        folder = os.path.join(self.home, "logs", "codex")
        os.makedirs(folder)
        link = os.path.join(folder, "example.repo-9")
        os.symlink(outside, link)
        old = time.time() - 30 * self.DAY
        os.utime(link, (old, old), follow_symlinks=False)
        self.tend()
        self.assertFalse(os.path.lexists(link))
        self.assertTrue(os.path.exists(os.path.join(outside, "f")))

    def test_the_size_of_the_loom_home_is_said_once_an_hour(self):
        self.write(os.path.join(self.home, "run", "codex-check", "big"), "x" * 300000)
        self.lane_log("codex", "example.repo-2.log", 1)
        said = self.tend()
        line = [s for s in said if s.startswith("loom home holds ")]
        self.assertEqual(len(line), 1, said)
        self.assertIn("run ", line[0])
        self.assertIn("logs ", line[0])
        self.assertIn("100.0 GB free on its disk", line[0])
        self.assertFalse(any(s.startswith("loom home holds ") for s in self.tend()))

    def test_low_free_disk_warns_every_pass(self):
        for _ in range(2):
            said = self.tend(free=3 * 1024 ** 3)
            self.assertTrue(any(s.startswith("warning: only 3.0 GB free on the loom home's disk")
                                for s in said), said)
        self.assertFalse(any(s.startswith("warning:") for s in self.tend()))

    def test_the_warning_never_reads_as_an_error_to_the_log_watcher(self):
        said = self.tend(free=1024 ** 3)
        warning = [s for s in said if s.startswith("warning:")]
        self.assertTrue(warning)
        self.assertFalse(watch_module.KEEP_RE.search(warning[0]))

    def test_settings_read_the_retention_and_the_free_disk_floor(self):
        self.assertEqual(station.Settings.from_env({}).log_keep_days, 7)
        self.assertEqual(station.Settings.from_env({}).disk_free_min_gb, 10)
        self.assertEqual(station.Settings.from_env({"LOOM_LOG_KEEP_DAYS": "14"}).log_keep_days, 14)
        self.assertEqual(station.Settings.from_env({"LOOM_LOG_KEEP_DAYS": "0"}).log_keep_days, 0)
        with mock.patch("bus.station._say"):
            self.assertEqual(station.Settings.from_env({"LOOM_LOG_KEEP_DAYS": "x"}).log_keep_days, 7)
            self.assertEqual(station.Settings.from_env({"LOOM_DISK_FREE_MIN_GB": "-1"}).disk_free_min_gb, 10)
        self.assertEqual(station.Settings.from_env({"LOOM_DISK_FREE_MIN_GB": "2.5"}).disk_free_min_gb, 2.5)


BOT = {"__typename": "Bot", "login": "grug-tribe"}
THREAD_FIX = FIX_BODY + "\n### Thread PRRT_abcd1 (comment 5) at a.py:3\n\n    medium finding\n"


class AnswerTest(Base):
    """A fix brief's answers to findings (lanes/run_lane.sh keep_answers)."""

    def thread(self, tid="PRRT_abcd1", pr=99, severity="medium", author=BOT):
        self.fake.threads[tid] = {
            "isResolved": False,
            "pullRequest": {"number": pr, "repository": {"nameWithOwner": REPO}},
            "comments": {"nodes": [{"author": author,
                                    "body": "%s | _correctness_ | `null-deref`\n\nx" % severity}]}}

    def finish(self, answers, model="claude-opus-5-5", kind="done", fix_body=THREAD_FIX):
        body = BODY.replace("Model: model-x", "Model: " + model)
        self.fake.add(12, state="running", body=body)
        self.fake.comments[99] = []
        name = tickets.ticket_name(REPO, 12, 2)
        brief = tickets.render_brief(REPO, self.fake.issues[12], "codex", 2,
                                     tickets.parse_fix(REPO, fix_body))
        self.write(self.inbox("codex", kind, name + ".md"), brief)
        for tid, text in answers.items():
            self.write(self.inbox("codex", "answers", name, tid + ".md"), text)
        if kind == "failed":
            self.write(os.path.join(self.home, "logs", "codex", name + ".log"),
                       "[2030-01-01T00:01:00Z] x failed: agent made no commit on loom/12-fix-the-thing\n")
        self.fake.pulls = [{"html_url": "https://example.com/pull/99"}]
        self.run_pass()
        return name

    def test_the_brief_tells_the_agent_how_to_answer(self):
        brief = tickets.render_brief(REPO, {"number": 12, "title": "t", "body": BODY}, "codex", 2,
                                     tickets.parse_fix(REPO, THREAD_FIX))
        self.assertIn(".loom-answers/<thread id>.md", brief)

    def test_a_strong_models_answer_to_a_medium_bot_finding_is_posted_and_resolved(self):
        self.thread()
        name = self.finish({"PRRT_abcd1": "The guard on a.py:2 returns before x is read."})
        self.assertEqual(len(self.fake.replies), 1)
        tid, body = self.fake.replies[0]
        self.assertEqual(tid, "PRRT_abcd1")
        self.assertIn("Lane codex answered this finding instead of changing the code", body)
        self.assertIn("> The guard on a.py:2 returns before x is read.", body)
        self.assertEqual(self.fake.resolved, ["PRRT_abcd1"])
        self.assertFalse(os.path.exists(self.inbox("codex", "answers", name)))

    def test_an_answer_on_a_no_change_round_is_posted_too(self):
        self.thread()
        self.finish({"PRRT_abcd1": "Already guarded at a.py:2."}, kind="failed")
        self.assertEqual(self.fake.resolved, ["PRRT_abcd1"])
        self.assertIn("loom:pr", self.fake.names(12))

    def test_a_model_off_the_ladder_answers_but_does_not_resolve(self):
        self.thread()
        self.finish({"PRRT_abcd1": "Not valid."}, model="model-x")
        self.assertEqual(len(self.fake.replies), 1)
        self.assertEqual(self.fake.resolved, [])

    def test_a_high_finding_is_answered_but_stays_open(self):
        self.thread(severity="high")
        self.finish({"PRRT_abcd1": "Not valid."})
        self.assertEqual((len(self.fake.replies), self.fake.resolved), (1, []))

    def test_a_persons_thread_is_answered_but_stays_open(self):
        self.thread(author={"__typename": "User", "login": "grug-tribe"})
        self.finish({"PRRT_abcd1": "Not valid."})
        self.assertEqual((len(self.fake.replies), self.fake.resolved), (1, []))

    def test_a_thread_the_brief_did_not_ask_about_or_on_another_pull_request_is_ignored(self):
        self.thread("PRRT_other9")
        self.thread(pr=98)
        self.finish({"PRRT_other9": "Not valid.", "PRRT_abcd1": "Not valid."})
        self.assertEqual((self.fake.replies, self.fake.resolved), ([], []))

    def test_an_answer_is_redacted_of_credentials_and_local_paths(self):
        self.thread()
        self.finish({"PRRT_abcd1": "token ghp_" + "a1B2c3D4e5F6g7H8i9J0 in /Users/example/x.py"})
        body = self.fake.replies[0][1]
        self.assertNotIn("ghp_", body)
        self.assertNotIn("/Users/", body)
        self.assertIn("<redacted>", body)

    def test_one_failed_reply_neither_stops_the_others_nor_repeats_on_the_next_pass(self):
        self.thread("PRRT_abcd1")
        self.thread("PRRT_abcd2")
        self.fake.reply_fails.add("PRRT_abcd1")
        fix = THREAD_FIX + "\n### Thread PRRT_abcd2 (comment 6) at a.py:4\n\n    medium finding\n"
        name = self.finish({"PRRT_abcd1": "Not valid.", "PRRT_abcd2": "Not valid either."},
                           fix_body=fix)
        self.assertEqual([t for t, _ in self.fake.replies], ["PRRT_abcd2"])
        self.assertFalse(os.path.exists(self.inbox("codex", "answers", name)))
        self.assertIn("loom:pr", self.fake.names(12))
        self.run_pass()
        self.assertEqual([t for t, _ in self.fake.replies], ["PRRT_abcd2"])

    def test_an_answer_file_that_is_a_link_is_not_read(self):
        self.thread()
        outside = os.path.join(self._tmp.name, "outside.md")
        self.write(outside, "secret words\n")
        name = tickets.ticket_name(REPO, 12, 2)
        folder = self.inbox("codex", "answers", name)
        os.makedirs(folder)
        os.symlink(outside, os.path.join(folder, "PRRT_abcd1.md"))
        self.finish({})
        self.assertEqual((self.fake.replies, self.fake.resolved), ([], []))

    def test_an_answer_that_fails_the_text_checks_is_not_posted(self):
        self.thread()
        self.finish({"PRRT_abcd1": "Fixes #12 already."})
        self.assertEqual((self.fake.replies, self.fake.resolved), ([], []))

    def answered(self, answers, title="Settle the review findings left from PR #99"):
        """A run with no pull request that the runner reported answered."""
        body = BODY.replace("Model: model-x", "Model: claude-opus-5-5")
        self.fake.add(12, title=title, state="answered", body=body)
        name = tickets.ticket_name(REPO, 12, 1)
        self.write(self.inbox("codex", "done", name + ".md"),
                   tickets.render_brief(REPO, self.fake.issues[12], "codex"))
        for tid, text in answers.items():
            self.write(self.inbox("codex", "answers", name, tid + ".md"), text)
        self.write(os.path.join(self.home, "logs", "codex", name + ".answered"), "")
        self.run_pass()
        return name

    def test_an_answered_runs_thread_answers_go_on_the_pull_request_the_ticket_names(self):
        self.thread()
        name = self.answered({"PRRT_abcd1": "The finding no longer holds: a.py:2 guards it."})
        self.assertEqual([t for t, _ in self.fake.replies], ["PRRT_abcd1"])
        self.assertIn("> The finding no longer holds", self.fake.replies[0][1])
        self.assertEqual(self.fake.resolved, ["PRRT_abcd1"])
        self.assertFalse(os.path.exists(self.inbox("codex", "answers", name)))
        # The runner already labelled and commented; no pull request is looked for.
        self.assertIn("loom:answered", self.fake.names(12))
        self.assertEqual(self.fake.comments[12], [])
        self.assertFalse(any("/pulls?head=" in path for path in self.fake.paths("GET")))

    def test_an_answered_line_the_agent_printed_in_the_log_is_not_an_answer(self):
        self.fake.add(12, state="running")
        name = tickets.ticket_name(REPO, 12, 1)
        self.write(self.inbox("codex", "done", name + ".md"),
                   tickets.render_brief(REPO, self.fake.issues[12], "codex"))
        self.write(os.path.join(self.home, "logs", "codex", name + ".log"),
                   "[2030-01-01T00:01:00Z] %s done, answered: nothing to change\n" % name)
        self.fake.pulls = [{"html_url": "https://example.com/pull/99"}]
        self.run_pass()
        self.assertIn("loom:pr", self.fake.names(12))

    def test_an_answered_run_answers_no_thread_on_a_pull_request_it_does_not_name(self):
        self.thread(pr=98)
        name = self.answered({"PRRT_abcd1": "Does not apply."})
        self.assertEqual((self.fake.replies, self.fake.resolved), ([], []))
        self.assertFalse(os.path.exists(self.inbox("codex", "answers", name)))


class RefusalTest(Base):
    def refused(self, **kw):
        self.fake.add(12, **kw)
        self.run_pass()
        self.assertEqual(self.listing(), [])
        self.assertEqual(self.fake.paths("PUT"), [])
        self.assertIn("loom:briefed", self.fake.names(12))

    def test_each_trust_rule_blocks_a_claim(self):
        self.refused(author="stranger")

    def test_briefed_label_applied_by_a_stranger(self):
        self.refused(events=[labeled("loom:briefed", "stranger"),
                             labeled("lane:codex", "alice-example")])

    def test_lane_label_applied_by_a_stranger(self):
        self.refused(events=[labeled("loom:briefed", "alice-example"),
                             labeled("lane:codex", "stranger")])

    def test_body_edited_by_a_stranger(self):
        self.refused(editor="stranger")

    def test_body_edited_by_a_deleted_account(self):
        self.refused(ghost_edit=True)

    def test_repo_owner_not_listed(self):
        self.settings.owners = ["someone-else"]
        self.refused()

    def test_an_unusable_header_is_refused(self):
        self.refused(body="Model: has spaces\n\n## Why\nx\n")

    def test_a_refusal_is_logged_once_per_issue_and_body(self):
        self.fake.add(12, author="stranger")
        lines = []
        real = station._say
        station._say = lines.append
        self.addCleanup(setattr, station, "_say", real)
        self.run_pass()
        self.run_pass()
        self.assertEqual(len([m for m in lines if "not claiming" in m]), 1)
        self.fake.issues[12]["body"] = "changed"
        self.fake.issues[12]["updated_at"] = "2030-02-02T00:00:00Z"
        self.fake.version += 1
        self.run_pass()
        self.assertEqual(len([m for m in lines if "not claiming" in m]), 2)


class OutcomeTest(Base):
    def finished(self, kind, number=12, attempt=1):
        self.fake.add(number, state="running")
        name = tickets.ticket_name(REPO, number, attempt)
        self.write(self.inbox("codex", kind, name + ".md"),
                   tickets.render_brief(REPO, self.fake.issues[number], "codex", attempt))
        return name

    def test_done_is_reported_once_with_the_pull_request_link(self):
        self.finished("done")
        self.fake.pulls = [{"html_url": "https://example.com/pull/99"}]
        self.run_pass()
        self.run_pass()
        self.assertEqual(self.fake.names(12), ["loom:ticket", "lane:codex", "loom:pr"])
        self.assertEqual(len(self.fake.comments[12]), 1)
        self.assertIn("https://example.com/pull/99", self.fake.comments[12][0]["body"])
        pulls = [p for p in self.fake.paths("GET") if "/pulls" in p]
        # Reconciliation reads the open pull requests (shared with PR
        # reviews) and one page of recently closed ones, never all of them.
        self.assertEqual(pulls, ["/repos/%s/pulls?head=example-owner:loom/12-fix-the-thing&state=all" % REPO]
                         + ["/repos/%s/pulls?state=open&per_page=100" % REPO, CLOSED_PULLS % REPO] * 2)

    def test_done_without_a_pull_request_is_retried_not_reported(self):
        self.finished("done")
        self.run_pass()
        self.assertEqual(self.fake.comments[12], [])
        self.assertIn("loom:running", self.fake.names(12))

    def test_answered_done_is_acknowledged_without_a_pull_request_lookup(self):
        self.fake.add(12, state="running")
        self.fake.issues[12]["body"] = "Deliverable: answer\n" + BODY
        name = tickets.ticket_name(REPO, 12, 1)
        self.write(self.inbox("codex", "done", name + ".md"),
                   tickets.render_brief(REPO, self.fake.issues[12], "codex"))
        self.fake.issues[12]["labels"] = [
            {"name": label} for label in ("loom:ticket", "lane:codex", "loom:answered")]
        self.run_pass()
        self.run_pass()
        self.assertEqual(self.fake.names(12), ["loom:ticket", "lane:codex", "loom:answered"])
        self.assertEqual(self.fake.comments[12], [])
        self.assertFalse(any("/pulls?head=" in path for path in self.fake.paths("GET")))

    def test_failed_is_reported_once_with_paths_scrubbed(self):
        name = self.finished("failed")
        self.write(os.path.join(self.home, "logs", "codex", name + ".log"),
                   "[t] %s failed: agent exited 1\n"
                   "[t] %s failed: could not open /home/example/secret/file.txt (in /tmp/x) now\n"
                   % (name, name))
        self.run_pass()
        self.run_pass()
        self.assertEqual(self.fake.names(12), ["loom:ticket", "lane:codex", "loom:failed"])
        self.assertEqual(len(self.fake.comments[12]), 1)
        body = self.fake.comments[12][0]["body"]
        self.assertIn("could not open <path> (in <path> now", body)
        self.assertNotIn("example", body)
        self.assertNotIn("agent exited 1", body)

    def limited(self, lane="spark", fix_body=None):
        self.fake.add(12, state="running", lane=lane,
                      body=BODY.replace("Model: model-x", "Model: spark/test")
                      if lane == "spark" else BODY)
        self.fake.comments[99] = []
        fix = tickets.parse_fix(REPO, fix_body) if fix_body else None
        name = tickets.ticket_name(REPO, 12, 1)
        self.write(self.inbox(lane, "failed", name + ".md"),
                   tickets.render_brief(REPO, self.fake.issues[12], lane, 1, fix))
        self.write(os.path.join(self.home, "logs", lane, name + ".log"),
                   "[t] %s start, lane %s\n[t] %s failed: [limit] limit: the spark endpoint refused "
                   "the connection (agent exited 1)\n" % (name, lane, name, ))
        self.run_pass()
        self.run_pass()

    def test_a_limit_is_not_a_failure_and_the_ticket_goes_back_to_briefed(self):
        self.limited()
        # A wait label instead of a comment: a comment a bounce emailed the
        # operator dozens of times an hour (2026-10-18).
        self.assertEqual(self.fake.names(12),
                         ["loom:ticket", "lane:spark", "loom:briefed", "wait:model-limit"])
        self.assertEqual(self.fake.comments[12], [])

    def test_a_limited_fix_brief_goes_back_to_its_pull_request(self):
        self.limited(fix_body=FIX_BODY)
        self.assertEqual(self.fake.names(12), ["loom:ticket", "lane:spark", "loom:pr"])

    def test_limited_lanes_are_those_with_a_fresh_limit_heartbeat(self):
        d = os.path.join(self.home, "state", "usage")
        for lane, available, reason, age in (
                ("spark", False, "limit: the spark endpoint answered 503; unavailable until x", 0),
                ("zen", False, "opencode not on PATH", 0),
                ("zai", False, "limit: old", station.HEARTBEAT_MAX_AGE + 60),
                ("codex", True, "limit: nonsense", 0)):
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, lane + ".json"), "w") as fh:
                json.dump({"lane": lane, "at": int(time.time()) - age,
                           "available": available, "reason": reason}, fh)
        st = station.Station(GitHub("token", transport=self.fake), self.settings)
        self.assertEqual(st.limited_lanes(), ["spark"])

    def test_failed_without_a_log_line_says_so(self):
        self.finished("failed")
        self.run_pass()
        self.assertIn("no reason was recorded", self.fake.comments[12][0]["body"])

    def test_reports_survive_a_restart_of_the_station(self):
        self.finished("done")
        self.fake.pulls = [{"html_url": "https://example.com/pull/99"}]
        self.run_pass()
        with open(os.path.join(self.home, "state", "station", "reported.json")) as fh:
            self.assertEqual(len(json.load(fh)), 1)

    def test_a_local_brief_without_an_issue_line_is_ignored(self):
        self.write(self.inbox("codex", "done", "T001-local.md"), "# Brief: T001-local\n\nRepo: x/y\n")
        self.run_pass()
        self.assertEqual(self.fake.paths("PUT"), [])
        self.assertEqual(self.fake.paths("POST"), [])

    def test_scrub(self):
        self.assertEqual(station.scrub("push to /a/b/c failed; branch loom/12-x ok"),
                         "push to <path> failed; branch loom/12-x ok")
        self.assertEqual(station.scrub("x" * 1000), "x" * station.REASON_MAX)


class LockAndCliTest(Base):
    def test_a_second_pass_exits_at_once_while_the_lock_is_held(self):
        self.fake.add(12)
        os.makedirs(os.path.join(self.home, "state"), exist_ok=True)
        fd = os.open(os.path.join(self.home, "state", "station.lock"), os.O_RDWR | os.O_CREAT)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        gh = self.run_pass()
        self.assertEqual(gh.requests, 0)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.listing(), [])

    def test_the_lock_is_released_after_a_pass(self):
        self.fake.add(12, author="stranger")
        self.run_pass()
        self.run_pass()
        self.assertEqual(len(self.fake.paths("GET")) > 1, True)

    def test_empty_repos_does_nothing_and_exits_zero(self):
        self.settings.repos = []
        self.assertEqual(station.run(self.settings, GitHub("t", transport=self.fake)), 0)
        self.assertEqual(self.fake.calls, [])

    def test_settings_come_from_the_environment(self):
        s = station.Settings.from_env({"LOOM_HOME": "/h", "LOOM_REPOS": "a/b  c/d",
                                       "LOOM_BRIEF_AUTHORS": "x y", "LOOM_REPO_OWNERS": "a"})
        self.assertEqual((s.home, s.repos, s.authors, s.owners),
                         ("/h", ["a/b", "c/d"], ["x", "y"], ["a"]))
        self.assertEqual(station.Settings.from_env({}).repos, [])
        self.assertEqual(station.Settings.from_env({}).retry_max, 2)
        self.assertEqual(station.Settings.from_env({"LOOM_RETRY_MAX": "5"}).retry_max, 5)
        self.assertEqual(station.Settings.from_env({"LOOM_RETRY_MAX": "x"}).retry_max, 2)
        configured = station.Settings.from_env({
            "LOOM_EVIDENCE_CMDS": '{"example-owner/example-repo":"printf safe"}'})
        self.assertEqual(configured.evidence_cmds, {REPO: "printf safe"})
        self.assertEqual(station.Settings.from_env({"LOOM_EVIDENCE_CMDS": "invalid"}).evidence_cmds, {})

    def test_the_station_command_runs_a_pass(self):
        old = {k: os.environ.get(k) for k in ("LOOM_HOME", "LOOM_REPOS")}
        os.environ["LOOM_HOME"] = self.home
        os.environ["LOOM_REPOS"] = ""
        try:
            self.assertEqual(cli.main(["station"]), 0)
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


NOON = time.mktime((2030, 5, 5, 12, 0, 0, 0, 0, -1))
EARLY = time.mktime((2030, 5, 5, 7, 0, 0, 0, 0, -1))
URL = "https://discord.example.com/api/webhooks/SECRET"


class StatusTest(Base):
    def setUp(self):
        super().setUp()
        self.settings.status_repo = REPO

    def go(self, later=0):
        gh = GitHub("token", transport=self.fake)
        station.Station(gh, self.settings, now=time.time() + later).run_pass()

    def test_published_on_first_run_and_issue_created(self):
        self.go()
        self.assertEqual(len(self.fake.created), 1)
        self.assertEqual(self.fake.created[0]["labels"], ["loom:lanes"])
        body = self.fake.posts[0]
        self.assertTrue(body.startswith("<!-- loom:lanes v1 -->\nUpdated "))
        self.assertIn("| codex | yes |", body)
        self.assertIn("| agy | no | - |", body)

    def test_existing_issue_is_reused(self):
        self.fake.lanes_issues = [{"number": 1}]
        self.go()
        self.assertEqual(self.fake.created, [])
        self.assertEqual(len(self.fake.posts), 1)

    def test_not_republished_when_nothing_changed(self):
        self.go()
        self.fake.calls.clear()
        self.go(30)
        self.assertEqual([c for c in self.fake.calls if "lanes" in c[1] or "comments" in c[1]], [])

    def test_republished_on_change_by_editing_the_comment(self):
        self.go()
        self.heartbeat("codex", available=False)
        self.go(30)
        self.assertEqual(len(self.fake.edits), 1)
        self.assertIn("| codex | no |", self.fake.edits[0])

    def test_republished_after_an_hour(self):
        self.go()
        self.heartbeat("codex", age=0)
        self.go(3601 - 0)
        self.assertEqual(len(self.fake.edits), 1)

    def test_never_contains_a_reason(self):
        d = os.path.join(self.home, "state", "usage")
        self.write(os.path.join(d, "zen.json"), json.dumps({
            "lane": "zen", "at": int(time.time()), "available": False,
            "reason": "/private/path secret-reason"}))
        self.go()
        self.assertNotIn("secret-reason", self.fake.posts[0])
        self.assertNotIn("/private", self.fake.posts[0])


class SpendTest(Base):
    # 2030-05-05 12:00 UTC; the budget was counted at 2030-05-01 and expires
    # at 2030-05-10 00:00 UTC, 4.5 days on.
    NOW = calendar.timegm((2030, 5, 5, 12, 0, 0))

    def setUp(self):
        super().setUp()
        self.settings.status_repo = REPO
        self.settings.anthropic = spend.Budget.from_env({
            "LOOM_ANTHROPIC_BUDGET_USD": "100", "LOOM_ANTHROPIC_BUDGET_SINCE": "2030-05-01",
            "LOOM_ANTHROPIC_BUDGET_EXPIRES": "2030-05-10"})

    def runs(self, *rows):
        """(hours before NOW, slot, cost or None) per run."""
        path = os.path.join(self.home, "state", "usage", "spend.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as fh:
            for hours, slot, cost in rows:
                fh.write(json.dumps({"at": int(self.NOW - hours * 3600), "slot": slot,
                                     "ticket": "t", "model": "claude-test",
                                     "cost_usd": cost}) + "\n")

    def station(self, now=None):
        return station.Station(GitHub("token", transport=self.fake), self.settings,
                               now=self.NOW if now is None else now)

    def test_the_status_line_shows_spent_remaining_burn_and_projection(self):
        self.runs((24 * 6, "anthropic", 40.0),   # before the budget was counted
                  (30, "anthropic-2", 20.0),     # since, but not in the last 24h
                  (20, "anthropic-3", 6.0),
                  (2, "anthropic", 3.5),
                  (1, "anthropic-4", None))      # no result: unknown, not zero
        with open(os.path.join(self.home, "state", "usage", "spend.jsonl"), "a") as fh:
            fh.write("{torn\n")
        self.station().run_pass()
        body = self.fake.posts[0]
        # today: 3.5; since 05-01: 29.5; remaining 70.5; burn 9.5/day;
        # projected: 70.5 - 9.5 * 4.5 = 27.75.
        self.assertIn("\n\nAnthropic spend: today $3.50; spent since 2030-05-01 $29.50; "
                      "remaining $70.50 of $100.00; burn $9.50/day (last 24h); "
                      "projected at expiry 2030-05-10 $27.75; "
                      "1 run of unknown cost not counted", body)
        self.assertNotIn("floor", body)

    def test_without_a_budget_only_the_spend_is_shown(self):
        self.settings.anthropic = spend.Budget()
        self.runs((2, "anthropic", 1.25), (50, "anthropic", 2.0))
        self.station().run_pass()
        line = self.fake.posts[0].splitlines()[-1]
        self.assertEqual(line, "Anthropic spend: today $1.25; spent in all $3.25")

    def test_no_line_without_a_budget_or_a_run(self):
        self.settings.anthropic = spend.Budget()
        self.station().run_pass()
        self.assertNotIn("Anthropic", self.fake.posts[0])

    def test_a_new_run_republishes_on_the_next_pass(self):
        self.runs((2, "anthropic", 1.0))
        self.station().run_pass()
        self.runs((0, "anthropic-2", 2.0))
        self.station(self.NOW + 60).run_pass()
        self.assertEqual(len(self.fake.edits), 1)
        self.assertIn("today $3.00", self.fake.edits[0])

    def test_credit_under_the_floor_stops_the_anthropic_slots(self):
        self.runs((5, "anthropic", 96.0))
        for lane in ("anthropic", "anthropic-4"):
            self.heartbeat(lane)
        # Heartbeats are stamped with the real clock; with no since date the
        # run above counts whenever it was.
        st = self.station(time.time())
        st.s.anthropic.since = None
        self.assertFalse(st._heartbeat_ok("anthropic"))
        self.assertFalse(st._heartbeat_ok("anthropic-4"))
        self.assertTrue(st._heartbeat_ok("codex"))
        self.fake.add(12, lane="anthropic-4",
                      body=BODY.replace("Model: model-x", "Model: claude-sonnet-5-5"))
        st.run_pass()
        self.assertEqual(self.listing("anthropic-4"), [])
        self.assertIn("Under the $5.00 floor: the anthropic slots take no work.",
                      self.fake.posts[0])
        self.assertIn("| anthropic-4 | no |", self.fake.posts[0])
        # Above the floor, the same heartbeat takes work again.
        self.settings.anthropic.floor = 3.0
        self.station(time.time()).run_pass()
        self.assertEqual(self.listing("anthropic-4"), ["example-owner.example-repo-12.md"])

    def test_budget_settings_come_from_the_environment(self):
        s = station.Settings.from_env({"LOOM_ANTHROPIC_BUDGET_USD": "197",
                                       "LOOM_ANTHROPIC_BUDGET_EXPIRES": "2026-10-21T00:00Z",
                                       "LOOM_ANTHROPIC_FLOOR_USD": "7.5"})
        self.assertEqual((s.anthropic.usd, s.anthropic.floor), (197.0, 7.5))
        self.assertEqual(s.anthropic.expires, calendar.timegm((2026, 10, 21, 0, 0, 0)))
        bad = station.Settings.from_env({"LOOM_ANTHROPIC_BUDGET_USD": "lots"})
        self.assertIsNone(bad.anthropic.usd)


class AsksTest(Base):
    def setUp(self):
        super().setUp()
        self.settings.webhook_ssm = "/example/webhook"
        self.sent = []
        self.fail_post = False
        self.read = []

    def go(self, now=NOON):
        def post(url, payload):
            if self.fail_post:
                raise OSError("down")
            self.sent.append((url, payload))

        def secret(name, region):
            self.read.append((name, region))
            return URL
        gh = GitHub("token", transport=self.fake)
        station.Station(gh, self.settings, now=now, read_secret=secret, post=post).run_pass()

    def test_untrusted_asks_are_ignored(self):
        self.fake.add_ask(5, author="stranger", urgent=True)
        self.fake.add_ask(6, labeller="stranger", urgent=True)
        self.go()
        self.assertEqual(self.sent, [])

    def test_urgent_is_sent_once(self):
        self.fake.add_ask(5, title="Pick one", urgent=True)
        self.go(EARLY)
        self.go(EARLY + 60)
        self.assertEqual(len(self.sent), 1)
        url, payload = self.sent[0]
        self.assertEqual(url, URL)
        self.assertEqual(payload["content"],
                         "Urgent ask: Pick one https://github.com/%s/issues/5" % REPO)
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertEqual(self.read[0], ("/example/webhook", "us-east-1"))

    def test_a_failed_trust_check_holds_every_send_until_the_next_pass(self):
        from unittest import mock
        from bus.github import GitHubError
        self.fake.add_ask(5, title="Pick one", urgent=True)
        with mock.patch.object(station.Station, "_ask_trusted",
                               side_effect=GitHubError("boom", 502)):
            self.go(EARLY)
        self.assertEqual(self.sent, [])
        self.go(EARLY + 60)
        self.assertEqual(len(self.sent), 1)

    def test_the_urgent_ledger_forgets_asks_that_closed(self):
        self.fake.add_ask(5, title="Pick one", urgent=True)
        self.go(EARLY)
        ledger = os.path.join(self.home, "state", "station", "asks.json")
        with open(ledger) as fh:
            self.assertEqual(json.load(fh)["urgent"], ["%s#5" % REPO])
        self.fake.asks.clear()
        self.fake.version += 1
        self.go(EARLY + 60)
        with open(ledger) as fh:
            self.assertEqual(json.load(fh)["urgent"], [])

    def test_digest_once_per_day_after_the_hour_only(self):
        self.fake.add_ask(5, title="Slow one")
        self.go(EARLY)
        self.assertEqual(self.sent, [])
        self.go(NOON)
        self.go(NOON + 600)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][1]["content"].splitlines()[1],
                         "- Slow one https://github.com/%s/issues/5" % REPO)
        self.go(NOON + 86400)
        self.assertEqual(len(self.sent), 2)

    def test_no_digest_without_asks(self):
        self.go()
        self.assertEqual(self.sent, [])

    def test_mention_is_the_only_allowed_one(self):
        self.settings.mention = "123456"
        self.fake.add_ask(5, urgent=True)
        self.go()
        payload = self.sent[0][1]
        self.assertTrue(payload["content"].startswith("<@123456> "))
        self.assertEqual(payload["allowed_mentions"], {"parse": [], "users": ["123456"]})

    def test_nothing_sent_without_the_webhook_setting(self):
        self.settings.webhook_ssm = ""
        self.fake.add_ask(5, urgent=True)
        self.go()
        self.assertEqual((self.sent, self.read), ([], []))

    def test_a_failed_post_is_retried_next_pass(self):
        self.fake.add_ask(5, urgent=True)
        self.fail_post = True
        self.go()
        self.assertEqual(self.sent, [])
        self.fail_post = False
        self.go(NOON + 60)
        self.assertEqual(len(self.sent), 2)  # the urgent ask and the digest
        self.go(NOON + 120)
        self.assertEqual(len(self.sent), 2)

    # The audit's size distribution: 56 open asks, 53 of them trusted, with
    # titles long enough that only 14 fit in one message.
    def add_many(self):
        self.fake.add_goal(30)
        self.fake.goals[0].update(title="Ship the factory")
        for n in range(100, 156):
            title = "Grant the %s permission the lane needs %s" % (
                "x" * (20 + (n * 7) % 30), "y" * (n % 9))
            self.fake.add_ask(n, title=title, body="Part of #30." if n % 2 else "",
                              created="2030-01-%02dT00:00:00Z" % (1 + (n - 100) // 2),
                              author="stranger" if n in (101, 120, 140) else "alice-example")
        return [n for n in range(100, 156) if n not in (101, 120, 140)]

    def reachable(self, content):
        """Ask numbers the notification shows or links to."""
        found = {int(n) for n in re.findall(r"/issues/(\d+)$", content, re.M)}
        for i in map(int, re.findall(r"issues/900#issuecomment-(\d+)", content)):
            found |= {int(n) for n in re.findall(r"^- #(\d+) ", self.fake.pages[i - 1], re.M)}
        return found

    def ledger(self):
        with open(os.path.join(self.home, "state", "station", "asks.json")) as fh:
            return json.load(fh)

    def test_every_trusted_ask_is_reachable_from_one_notification(self):
        trusted = self.add_many()
        self.assertEqual(len(trusted), 53)
        self.go()
        self.assertEqual(len(self.sent), 1)
        content = self.sent[0][1]["content"]
        self.assertLessEqual(len(content), station.DISCORD_MAX)
        self.assertIn("- and ", content)
        self.assertEqual(self.reachable(content), set(trusted))
        # The newest asks are the ones shown inline.
        self.assertIn("/issues/155\n", content)
        self.assertIn("/issues/154\n", content)
        self.assertNotIn("/issues/100\n", content)
        page = self.fake.pages[0]
        self.assertTrue(page.startswith(station.DIGEST_MARKER))
        self.assertIn("## Ship the factory (#30)", page)
        self.assertIn("## Not filed under a goal", page)
        self.assertLess(page.index("- #103 "), page.index("## Not filed"))
        self.assertGreater(page.index("- #102 "), page.index("## Not filed"))
        self.assertEqual(self.fake.created[0]["labels"], ["loom:digest"])

    def test_the_ledger_records_what_was_covered(self):
        trusted = self.add_many()
        self.go()
        ledger = self.ledger()
        self.assertEqual(sorted(ledger["covered"]),
                         sorted("%s#%d" % (REPO, n) for n in trusted))
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{12}", v) for v in ledger["covered"].values()))
        self.assertEqual(ledger["pages"], {REPO: "https://github.com/%s/issues/900#issuecomment-1" % REPO})
        self.assertNotIn("pending", ledger)

    def test_new_and_changed_asks_lead_the_next_digest(self):
        self.add_many()
        self.go()
        self.fake.add_ask(10, title="A fresh ask", created="2029-01-01T00:00:00Z")
        self.fake.asks[0]["body"] = "Edited."  # ask 100, the oldest
        self.go(NOON + 86400)
        lines = self.sent[1][1]["content"].splitlines()
        self.assertEqual(lines[0], "Asks waiting for you: 54, 2 new since the last digest")
        self.assertEqual(sorted(line.rsplit("/", 1)[1] for line in lines[1:3]), ["10", "100"])
        self.assertEqual(len(self.fake.pages), 2)

    def test_a_retry_sends_the_same_digest(self):
        self.add_many()
        self.fail_post = True
        self.go()
        self.assertEqual(self.sent, [])
        self.assertEqual(len(self.fake.pages), 1)
        self.assertIn("pending", self.ledger())
        self.fake.add_ask(10, title="Arrived after the first try")
        self.fail_post = False
        self.go(NOON + 60)
        self.assertEqual(len(self.fake.pages), 1)  # the page is not posted twice
        content = self.sent[0][1]["content"]
        self.assertNotIn("Arrived after", content)
        self.assertNotIn("%s#10" % REPO, self.ledger()["covered"])
        self.go(NOON + 120)
        self.assertEqual(len(self.sent), 1)

    def test_a_failed_page_post_holds_the_digest_not_urgent_asks(self):
        trusted = self.add_many()
        self.fake.add_ask(9, title="Breaking now", urgent=True)
        self.fake.page_error = True
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            self.go()
        self.assertEqual([p["content"] for _, p in self.sent],
                         ["Urgent ask: Breaking now https://github.com/%s/issues/9" % REPO])
        self.assertIn("daily digest pending: could not post its page", out.getvalue())
        self.assertIn("pending", self.ledger())
        self.fake.page_error = False
        self.go(NOON + 60)
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.reachable(self.sent[1][1]["content"]), set(trusted) | {9})

    def test_a_non_object_page_reply_holds_the_digest(self):
        self.add_many()
        self.fake.page_reply = []
        self.go()
        self.assertEqual(self.sent, [])
        self.assertIn("pending", self.ledger())
        self.fake.page_reply = None
        self.go(NOON + 60)
        self.assertEqual(len(self.sent), 1)

    def test_a_failed_trust_check_of_an_old_ask_does_not_hold_an_urgent_one(self):
        from unittest import mock
        from bus.github import GitHubError
        self.fake.add_ask(5, title="Old one")
        self.fake.add_ask(6, title="Breaking now", urgent=True)
        real = station.Station._ask_trusted

        def flaky(st, repo, issue):
            if issue["number"] == 5:
                raise GitHubError("boom", 502)
            return real(st, repo, issue)
        with mock.patch.object(station.Station, "_ask_trusted", flaky):
            self.go()
        self.assertEqual([p["content"] for _, p in self.sent],
                         ["Urgent ask: Breaking now https://github.com/%s/issues/6" % REPO])
        self.go(NOON + 60)
        self.assertEqual(len(self.sent), 2)

    def test_a_small_digest_needs_no_page(self):
        self.fake.add_ask(5, title="Slow one")
        self.go()
        self.assertEqual(self.fake.pages, [])
        self.assertEqual(self.ledger()["pages"], {})

    def test_a_failed_goal_read_holds_the_digest_until_the_next_pass(self):
        trusted = self.add_many()
        ledger_path = os.path.join(self.home, "state", "station", "asks.json")
        os.makedirs(os.path.dirname(ledger_path), exist_ok=True)
        with open(ledger_path, "w") as fh:
            json.dump({"digest": "2030-05-04"}, fh)
        self.fake.goal_error = True
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            self.go()
        self.assertEqual(self.sent, [])
        # No plan is fixed on a failed read, so the next pass plans afresh.
        self.assertTrue(os.path.exists(ledger_path))
        self.assertNotIn("pending", self.ledger())
        self.assertIn("could not read the goal homes for the digest", out.getvalue())
        self.fake.goal_error = False
        self.go(NOON + 60)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.reachable(self.sent[0][1]["content"]), set(trusted))

    def test_a_goal_with_an_odd_number_does_not_stop_the_digest(self):
        trusted = self.add_many()
        self.fake.goals.append({"number": "abc", "title": "Odd"})
        self.go()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.reachable(self.sent[0][1]["content"]), set(trusted))
        self.assertIn("Ship the factory (#30)", self.fake.pages[0])


class HumanDigestTest(AsksTest):
    """loom:human issues wait in the daily digest, never one message each."""

    def contents(self):
        return [p["content"] for _, p in self.sent]

    def test_human_issues_are_listed_in_the_digest_with_their_ask(self):
        self.fake.add_human(7, title="Buy the domain", ask="Buy example.com.\nMore detail.")
        self.fake.add_human(8, title="Sign the form", ask="",
                            created="2030-02-01T00:00:00Z")
        self.go(NOON)
        self.assertEqual(self.contents(), [
            "Waiting on you (2):\n%s\n"
            "- Sign the form https://github.com/%s/issues/8\n"
            "- Buy the domain https://github.com/%s/issues/7\n"
            "  Buy example.com." % (REPO, REPO, REPO)])
        self.assertEqual(self.sent[0][1]["flags"], station.SUPPRESS_EMBEDS)

    def test_a_human_issue_is_never_forwarded_on_its_own(self):
        self.fake.add_human(7)
        self.go(EARLY)
        self.go(EARLY + 60)
        self.assertEqual(self.sent, [])
        # Not even when it also carries an urgent ask's labels.
        self.fake.add_human(9, extra=("needs-operator", "loom:urgent"))
        self.fake.asks.append(self.fake.humans[-1])
        self.fake.events[9] = [labeled("needs-operator", "alice-example")]
        self.go(EARLY + 120)
        self.assertEqual(self.sent, [])
        self.go(NOON)
        self.go(NOON + 600)
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.contents()[0].startswith("Waiting on you (2):"))

    def test_the_list_shows_the_5_newest_and_the_total(self):
        for n in range(100, 140):
            self.fake.add_human(n, title="Item %d" % n,
                                created="2030-01-01T00:00:%02dZ" % (n - 100))
        self.go(NOON)
        text = "\n".join(self.contents())
        self.assertIn("Waiting on you (the 5 newest of 40):", text)
        listed = re.findall(r"- Item (\d+) ", text)
        self.assertEqual(listed, [str(n) for n in range(139, 134, -1)])
        self.assertTrue(all(len(c) + 30 <= station.DISCORD_MAX for c in self.contents()))
        self.assertEqual(self.fake.paths("GET").count(
            "/repos/%s/issues/100/comments?per_page=100" % REPO), 0)
        comment_reads = [p for p in self.fake.paths("GET") if "/comments?" in p]
        self.assertEqual(len(comment_reads), station.HUMAN_SHOWN)
        self.assertTrue(all(p.endswith("?per_page=100") for p in comment_reads))

    def test_clipped_human_text_does_not_escape_its_ellipsis(self):
        title = "T" * (station.HUMAN_TITLE_MAX - 4) + "\\" + "tail"
        ask = "A" * (station.HUMAN_ASK_MAX - 4) + "\\" + "tail"
        self.fake.add_human(7, title=title, ask=ask)
        self.go(NOON)
        content = self.contents()[0]
        self.assertIn("- %s... https://" % ("T" * (station.HUMAN_TITLE_MAX - 4)), content)
        self.assertIn("  %s..." % ("A" * (station.HUMAN_ASK_MAX - 4)), content)
        self.assertNotIn("\\...", content)

    def test_the_list_follows_the_asks_in_the_same_digest(self):
        self.settings.mention = "123456"
        self.fake.add_ask(5, title="Slow one")
        self.fake.add_human(7, title="Buy the domain")
        self.go(NOON)
        self.assertEqual(len(self.sent), 1)
        lines = self.contents()[0].splitlines()
        self.assertEqual(lines[:2], ["<@123456> " + station.DIGEST_HEAD +
                                     " 1, 1 new since the last digest",
                                     "- Slow one https://github.com/%s/issues/5" % REPO])
        self.assertEqual(lines[3], "Waiting on you (1):")

    def test_the_list_follows_a_paged_ask_digest(self):
        self.add_many()
        self.fake.add_human(7, title="Buy the domain")
        self.go(NOON)
        text = "\n".join(self.contents())
        self.assertIn("Every ask, by goal:", text)
        self.assertIn("Waiting on you (1):", text)
        self.assertIn("- Buy the domain https://github.com/%s/issues/7" % REPO, text)
        self.assertEqual(len(self.fake.pages), 1)
        self.assertEqual(self.ledger()["digest"], "2030-05-05")

    def test_an_ask_by_a_stranger_is_not_shown(self):
        self.fake.add_human(7, title="Buy the domain", ask="Send money to me.", author="stranger")
        self.go(NOON)
        self.assertNotIn("Send money", self.contents()[0])

    def test_an_unreadable_list_holds_the_digest_but_not_urgent_asks(self):
        from unittest import mock
        from bus.github import GitHubError
        self.fake.add_ask(5, title="Pick one", urgent=True)
        self.fake.add_ask(6, title="Slow one")
        self.fake.add_human(7)
        with mock.patch.object(station.Station, "_gather_human", return_value=None):
            self.go(NOON)
        self.assertEqual(self.contents(), ["Urgent ask: Pick one https://github.com/%s/issues/5" % REPO])
        with mock.patch.object(station.Station, "_human_ask", side_effect=GitHubError("boom", 502)):
            self.go(NOON + 60)
        self.assertEqual(len(self.sent), 1)
        self.go(NOON + 120)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("Waiting on you (1):", self.contents()[1])

    def test_the_section_groups_by_repo_newest_first(self):
        other = "example-owner/other-repo"
        found = [(other, {"number": 3, "title": "C", "html_url": "u3"}),
                 (REPO, {"number": 2, "title": "B", "html_url": "u2"}),
                 (other, {"number": 1, "title": "A", "html_url": "u1"})]
        st = station.Station(GitHub("token", transport=self.fake), self.settings, now=NOON)
        from unittest import mock
        with mock.patch.object(station.Station, "_human_ask", return_value="Ask."):
            text = st._human_text(found)
        self.assertEqual(text.splitlines(), [
            "Waiting on you (3):", other, "- C u3", "  Ask.", "- A u1", "  Ask.",
            REPO, "- B u2", "  Ask."])


class ReportDigestTest(Base):
    """Two digests a day (operator, 2026-10-14) instead of every report."""

    def setUp(self):
        super().setUp()
        self.settings.webhook_ssm = "/example/webhook"
        self.settings.report_digest = ((10, 0), (21, 0))
        self.settings.authors = list(AUTHORS)
        self.sent = []
        day = time.localtime()
        self.base = time.mktime((day.tm_year, day.tm_mon, day.tm_mday, 0, 0, 0, 0, 0, -1))

    def at(self, hour, minute=0, days=0):
        gh = GitHub("token", transport=self.fake)
        station.Station(gh, self.settings, now=self.base + days * 86400 + hour * 3600 + minute * 60,
                        read_secret=lambda n, r: URL,
                        post=lambda url, payload: self.sent.append(payload)).run_pass()

    def goal(self, number, title):
        self.fake.add(number, title=title, state="goal")
        self.fake.issues[number]["labels"] = [{"name": "loom:goal"}]

    def report(self, number, ident, first):
        self.fake.comments[number].append({"id": ident, "body": station.REPORT_MARKER + "\n" + first + "\nmore",
            "user": {"login": AUTHORS[0]},
            "html_url": "https://github.com/%s/issues/%d#issuecomment-%d" % (REPO, number, ident)})

    def test_one_digest_at_each_time_lists_movers_and_counts_quiet_goals(self):
        self.goal(40, "Goal forty")
        self.goal(41, "Goal forty-one")
        self.goal(42, "Goal forty-two")
        self.at(9, 0)            # switching on before 10:00 sends nothing
        self.at(9, 30)
        self.report(40, 100, "2 merged, 1 verified since the last report.")
        self.report(41, 101, "Nothing moved since the last report.")
        self.report(41, 102, "Nothing moved since the last report.")
        self.at(9, 59)
        self.assertEqual(self.sent, [])
        self.at(10, 1)
        self.at(10, 30)          # the same time is never sent twice
        self.assertEqual(len(self.sent), 1)
        text = self.sent[0]["content"]
        self.assertIn("Loom morning digest", text)
        self.assertIn("Moving, 1 goal:", text)
        self.assertIn("**Goal forty** 2 merged, 1 verified since the last report.", text)
        self.assertIn("Quiet: 2 goals, nothing moved.", text)
        self.assertNotIn("forty-one", text)
        self.at(21, 5)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("Loom evening digest", self.sent[1]["content"])
        self.assertIn("No goal moved since the last digest.", self.sent[1]["content"])

    def test_a_goal_with_only_work_in_flight_counts_as_quiet(self):
        # 2026-10-18: every goal reported "N in flight since the last report",
        # so the digest listed all 18 goals as moving.
        self.goal(40, "Goal forty")
        self.goal(41, "Goal forty-one")
        self.at(9, 30)
        self.report(40, 100, "1 merged, 13 in flight since the last report.")
        self.report(41, 101, "10 in flight since the last report.")
        self.at(10, 1)
        text = self.sent[0]["content"]
        self.assertIn("Moving, 1 goal:", text)
        self.assertIn("Quiet: 1 goal, nothing moved.", text)
        self.assertNotIn("forty-one", text)

    def test_each_report_is_not_forwarded_on_its_own(self):
        self.goal(40, "Goal forty")
        self.at(10, 1)           # first pass: records the time, sends nothing
        for minute in range(2, 50, 6):
            self.report(40, 200 + minute, "1 merged since the last report.")
            self.at(10, minute)
        self.assertEqual(self.sent, [])

    def test_the_digest_carries_the_latest_daily_factory_numbers(self):
        self.goal(40, "Goal forty")
        self.settings.metrics_goal = "%s#40" % REPO
        self.fake.comments[40].append({"id": 90, "body": "<!-- loom:metrics v1 -->\n2026-10-13: 3/9 pass; streak 0 day(s)",
                                       "user": {"login": AUTHORS[0]}})
        self.fake.comments[40].append({"id": 91, "body": "<!-- loom:metrics v1 -->\n2026-10-14: 4/9 pass; streak 0 day(s)",
                                       "user": {"login": AUTHORS[0]}})
        gh = GitHub("token", transport=self.fake)
        st = station.Station(gh, self.settings, now=self.base + 10 * 3600, read_secret=lambda n, r: URL,
                             post=lambda url, payload: None)
        self.assertEqual(st._digest_factory_line(), "Factory 2026-10-14: 4/9 numbers pass; streak 0 of 7.")
        self.settings.metrics_goal = ""
        self.assertEqual(st._digest_factory_line(), "")

    def test_digest_times_parse_and_turn_off(self):
        self.assertEqual(station.parse_digest_times("10:00, 21:00"), ((10, 0), (21, 0)))
        self.assertEqual(station.parse_digest_times("25:00 9:5 x"), ())
        self.assertEqual(station.Settings.from_env({"LOOM_REPOS": REPO}).report_digest,
                         ((10, 0), (21, 0)))
        self.assertEqual(station.Settings.from_env({"LOOM_REPOS": REPO,
                                                    "LOOM_REPORT_DIGEST_TIMES": ""}).report_digest, ())


class ReportsTest(Base):
    def setUp(self):
        super().setUp()
        self.settings.webhook_ssm = "/example/webhook"
        self.settings.mention = "123456"
        self.sent = []
        self.fail_post = False

    def add_goal(self, number=40):
        self.fake.add(number, title="Goal title", state="goal")
        self.fake.issues[number]["labels"] = [{"name": "loom:goal"}]
        return self.fake.comments[number]

    def comment(self, number, ident, body, author="alice-example"):
        self.fake.comments[number].append({"id": ident, "body": body,
            "user": {"login": author}, "html_url": "https://github.com/%s/issues/%d#issuecomment-%d" % (REPO, number, ident)})

    def go(self, step=station.REPORT_CHECK + 1):
        def post(url, payload):
            if self.fail_post:
                raise OSError("down")
            self.sent.append(payload)
        self.clock = getattr(self, "clock", time.time()) + step
        gh = GitHub("token", transport=self.fake)
        station.Station(gh, self.settings, now=self.clock, read_secret=lambda n, r: URL,
                        post=post).run_pass()

    def test_goal_homes_are_read_at_most_every_few_minutes(self):
        self.prime()
        self.fake.calls.clear()
        self.go(step=10)
        self.assertFalse(any("comments" in c[1] for c in self.fake.calls))
        self.go()
        reads = [c[1] for c in self.fake.calls if "/issues/40/comments" in c[1]
                 and "since=" in c[1]]
        self.assertTrue(reads)

    def prime(self, number=40):
        self.add_goal(number)
        self.go()
        self.sent.clear()

    def test_new_trusted_report_is_sent_once_without_mention(self):
        self.prime()
        self.comment(40, 100, station.REPORT_MARKER + "\nFive report lines")
        self.go()
        self.go()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["content"], "Goal title\nFive report lines\nhttps://github.com/%s/issues/40#issuecomment-100" % REPO)
        self.assertEqual(self.sent[0]["allowed_mentions"], {"parse": []})
        self.assertEqual(self.sent[0]["flags"], station.SUPPRESS_EMBEDS)

    def test_untrusted_and_journal_comments_are_ignored(self):
        self.prime()
        self.comment(40, 100, station.REPORT_MARKER + "\nreport", "stranger")
        self.comment(40, 101, "journal entry")
        self.go()
        self.assertEqual(self.sent, [])

    def test_first_run_records_existing_report_without_sending(self):
        self.add_goal()
        self.comment(40, 100, station.REPORT_MARKER + "\nhistory")
        self.go()
        self.assertEqual(self.sent, [])
        with open(os.path.join(self.home, "state", "station", "reports.json")) as fh:
            self.assertEqual(json.load(fh)[REPO.replace("/", ".")], 100)

    def test_closed_goal_home_is_not_read_or_forwarded(self):
        self.add_goal()
        self.fake.issues[40]["state"] = "closed"
        self.comment(40, 100, station.REPORT_MARKER + "\nclosed history")
        self.go()
        self.assertEqual(self.sent, [])
        self.assertFalse(any("/issues/40/comments" in path
                             for _, path in self.fake.calls))

    def test_failed_send_is_retried(self):
        self.prime()
        self.comment(40, 100, station.REPORT_MARKER + "\nreport")
        self.fail_post = True
        self.go()
        self.assertEqual(self.sent, [])
        self.fail_post = False
        self.go()
        self.assertEqual(len(self.sent), 1)

    def test_a_pass_killed_after_a_send_does_not_send_it_again(self):
        self.prime()
        self.comment(40, 100, station.REPORT_MARKER + "\nfirst report")
        self.comment(40, 101, station.REPORT_MARKER + "\nsecond report")

        # The station is killed hard mid-pass: after the first report is out
        # and before the pass writes its ledger at the end. A BaseException
        # is not caught by _send's broad except, as a bootout kill is not.
        class Killed(BaseException):
            pass

        def post(url, payload):
            if self.sent:
                raise Killed()
            self.sent.append(payload)

        self.clock += station.REPORT_CHECK + 1
        gh = GitHub("token", transport=self.fake)
        with self.assertRaises(Killed):
            station.Station(gh, self.settings, now=self.clock,
                            read_secret=lambda n, r: URL, post=post).run_pass()
        self.assertEqual(len(self.sent), 1)
        self.assertIn("first report", self.sent[0]["content"])

        # The next pass sends only the report not yet sent.
        self.go()
        self.assertEqual(len(self.sent), 2)
        self.assertIn("second report", self.sent[1]["content"])

    def test_an_unreadable_webhook_keeps_the_read_window(self):
        self.prime()
        self.comment(40, 100, station.REPORT_MARKER + "\nreport")
        gh = GitHub("token", transport=self.fake)
        self.clock += station.REPORT_CHECK + 1
        from unittest import mock
        with mock.patch.object(station, "read_ssm", side_effect=OSError("unreadable")):
            station.Station(gh, self.settings, now=self.clock,
                            post=lambda u, p: self.sent.append(p)).run_pass()
        self.assertEqual(self.sent, [])
        self.go()
        self.assertEqual(len(self.sent), 1)

    def test_the_ledger_forgets_repos_that_left(self):
        self.prime()
        path = os.path.join(self.home, "state", "station", "reports.json")
        with open(path) as fh:
            ledger = json.load(fh)
        ledger["gone.repo"] = 5
        with open(path, "w") as fh:
            json.dump(ledger, fh)
        self.go()
        with open(path) as fh:
            self.assertNotIn("gone.repo", json.load(fh))

    def test_long_report_is_cut_at_line_break_and_keeps_link(self):
        self.prime()
        self.comment(40, 100, station.REPORT_MARKER + "\n" + "line\n" * 700)
        self.go()
        content = self.sent[0]["content"]
        self.assertLessEqual(len(content), station.DISCORD_MAX)
        self.assertTrue(content.endswith("https://github.com/%s/issues/40#issuecomment-100" % REPO))
        self.assertTrue(content.splitlines()[-2] == "line")

    def test_station_posts_an_overdue_data_only_standup_once(self):
        self.add_goal()
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(
            time.time() - brain.REPORT_EVERY - brain.REPORT_GRACE - 60))
        stuck_old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(
            time.time() - station.factory.STUCK_AGE - 60))
        recent = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60))
        self.comment(40, 1, "<!-- loom:report v1 -->\nold report")
        self.fake.comments[40][0]["created_at"] = old
        self.fake.sub_issues[40] = [
            {"number": 41, "title": "Merged task", "state": "closed", "updated_at": recent,
             "labels": [{"name": "loom:ticket"}, {"name": "loom:merged"}]},
            {"number": 42, "title": "Open task", "state": "open",
             "labels": [{"name": "loom:ticket"}, {"name": "loom:pr"}]},
            {"number": 43, "title": "Verified task", "state": "closed", "updated_at": recent,
             "labels": [{"name": "loom:ticket"}, {"name": "loom:verified"}]},
            {"number": 44, "title": "Sent back task", "state": "closed", "updated_at": recent,
             "labels": [{"name": "loom:ticket"}, {"name": "loom:shipped"}]},
            {"number": 45, "title": "Failed task", "state": "open",
             "labels": [{"name": "loom:ticket"}, {"name": "loom:failed"}]},
            {"number": 46, "title": "Stuck task", "state": "open", "created_at": stuck_old,
             "labels": [{"name": "loom:ticket"}, {"name": "loom:pr"}]},
            {"number": 47, "title": "Decision task", "state": "open",
             "labels": [{"name": "loom:ticket"}, {"name": station.NEEDS_OPERATOR}]},
        ]
        self.clock = time.time()
        st = station.Station(GitHub("token", transport=self.fake), self.settings,
                             now=self.clock)
        st.post_overdue_standups()
        st.post_overdue_standups()
        made = [c for c in self.fake.comments[40] if str(c.get("body", "")).startswith(
            station.REPORT_MARKER)]
        self.assertEqual(len(made), 2)
        body = made[-1]["body"]
        for heading in ("### Going well", "### Not going well", "### Still stuck",
                        "### Next", "### Needs you"):
            self.assertIn(heading, body)
        for number, title in ((41, "Merged task"), (42, "Open task"),
                              (43, "Verified task"), (44, "Sent back task"),
                              (45, "Failed task"), (46, "Stuck task"),
                              (47, "Decision task")):
            self.assertIn("#%d %s" % (number, title), body)
        self.assertEqual(body.splitlines()[1], "1 merged, 1 verified, 1 shipped, 1 failed, "
                         "1 stuck, 2 in flight, 1 waiting on you since the last report.")
        brain._check_written_text(body)

    def test_station_standup_lists_the_tickets_of_the_goals_epics(self):
        self.add_goal()
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(
            time.time() - brain.REPORT_EVERY - brain.REPORT_GRACE - 60))
        self.comment(40, 1, "<!-- loom:report v1 -->\nold report")
        self.fake.comments[40][0]["created_at"] = old
        ticket = lambda n, title: {"number": n, "title": title, "state": "open",
                                   "labels": [{"name": "loom:ticket"}, {"name": "loom:pr"}]}
        self.fake.sub_issues[40] = [
            ticket(42, "Direct task"),
            {"number": 50, "title": "Epic: one chunk", "state": "open",
             "labels": [{"name": "epic"}]}]
        self.fake.sub_issues[50] = [ticket(51, "Epic task one"), ticket(52, "Epic task two")]
        station.Station(GitHub("token", transport=self.fake), self.settings,
                        now=time.time()).post_overdue_standups()
        body = self.fake.comments[40][-1]["body"]
        for number, title in ((42, "Direct task"), (51, "Epic task one"),
                              (52, "Epic task two")):
            self.assertIn("#%d %s" % (number, title), body)
        self.assertNotIn("#50 Epic", body)
        self.assertIn("3 in flight", body.splitlines()[1])

    def test_station_standup_flags_an_epic_whose_tickets_are_all_closed(self):
        self.add_goal()
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(
            time.time() - brain.REPORT_EVERY - brain.REPORT_GRACE - 60))
        self.comment(40, 1, "<!-- loom:report v1 -->\nold report")
        self.fake.comments[40][0]["created_at"] = old
        epic = lambda n, title: {"number": n, "title": title, "state": "open",  # noqa: E731
                                 "labels": [{"name": "epic"}]}
        done = lambda n: {"number": n, "title": "Done %d" % n, "state": "closed",  # noqa: E731
                          "labels": [{"name": "loom:ticket"}, {"name": "loom:verified"}]}
        self.fake.sub_issues[40] = [epic(50, "Epic: all done"), epic(60, "Epic: still going")]
        self.fake.sub_issues[50] = [done(51), done(52)]
        self.fake.sub_issues[60] = [done(61), {"number": 62, "title": "Open", "state": "open",
                                               "labels": [{"name": "loom:ticket"},
                                                          {"name": "loom:pr"}]}]
        station.Station(GitHub("token", transport=self.fake), self.settings,
                        now=time.time()).post_overdue_standups()
        body = self.fake.comments[40][-1]["body"]
        self.assertIn("Epics with every ticket closed (check the done-when, then close): "
                      "#50 Epic: all done.", body)
        self.assertNotIn("#60 Epic", body)

    def test_station_standup_lists_only_what_changed_since_the_last_report(self):
        self.add_goal()
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(
            time.time() - brain.REPORT_EVERY - brain.REPORT_GRACE - 60))
        older = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(
            time.time() - brain.REPORT_EVERY - brain.REPORT_GRACE - 3600))
        self.comment(40, 1, "<!-- loom:report v1 -->\nold report")
        self.fake.comments[40][0]["created_at"] = old
        self.fake.sub_issues[40] = [
            {"number": 41, "title": "Merged long ago", "state": "closed", "updated_at": older,
             "labels": [{"name": "loom:ticket"}, {"name": "loom:merged"}]}]
        station.Station(GitHub("token", transport=self.fake), self.settings,
                        now=time.time()).post_overdue_standups()
        body = self.fake.comments[40][-1]["body"]
        self.assertNotIn("Merged long ago", body)
        self.assertEqual(body.splitlines()[1], "Nothing moved since the last report.")

    def test_a_quiet_standup_edits_the_last_quiet_report_instead_of_posting(self):
        self.add_goal()
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(
            time.time() - brain.REPORT_EVERY - brain.REPORT_GRACE - 60))
        st = station.Station(GitHub("token", transport=self.fake), self.settings,
                             now=time.time())
        quiet = st._overdue_standup_body([], None)
        self.comment(40, 5, quiet + "\n" + station.QUIET_STAMP % "2026-01-01T00:00Z")
        self.fake.comments[40][0]["created_at"] = old
        st.post_overdue_standups()
        self.assertEqual(len(self.fake.comments[40]), 1)  # nothing posted, so no email
        self.assertEqual(len(self.fake.edits), 1)
        self.assertTrue(self.fake.edits[0].startswith(quiet + "\n"))
        self.assertNotIn("2026-01-01T00:00Z", self.fake.edits[0])

    def test_a_standup_after_a_report_that_was_not_quiet_is_posted(self):
        self.add_goal()
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(
            time.time() - brain.REPORT_EVERY - brain.REPORT_GRACE - 60))
        self.comment(40, 5, station.REPORT_MARKER + "\n1 merged since the last report.")
        self.fake.comments[40][0]["created_at"] = old
        station.Station(GitHub("token", transport=self.fake), self.settings,
                        now=time.time()).post_overdue_standups()
        self.assertEqual(len(self.fake.comments[40]), 2)
        self.assertEqual(self.fake.comments[40][-1]["body"].splitlines()[1],
                         "Nothing moved since the last report.")
        self.assertEqual(self.fake.edits, [])

    def test_station_standup_still_stuck_names_a_goal_the_gate_is_holding(self):
        st = station.Station(GitHub("token", transport=self.fake), self.settings,
                             now=time.time())
        hold = {"repo": REPO, "goal": 30, "since": time.time() - 3700,
                "count": 27, "limit": 15, "tickets": {"1": "Fix the gate",
                                                     "2": "File the invariant"},
                "idle": 4}
        body = st._overdue_standup_body([], None, None, gate_hold=hold)
        stuck = body.split("### Still stuck")[1].split("### Next")[0]
        self.assertIn("goal #30", stuck)
        self.assertIn("27", stuck)
        self.assertIn("15", stuck)
        self.assertIn("4", stuck)
        plain = st._overdue_standup_body([], None, None)
        self.assertNotIn("unproven-work gate", plain)
        brain._check_written_text(body)

    def test_station_standup_says_nothing_in_flight_without_tickets(self):
        self.add_goal()
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(
            time.time() - brain.REPORT_EVERY - brain.REPORT_GRACE - 60))
        self.comment(40, 1, "<!-- loom:report v1 -->\nold report")
        self.fake.comments[40][0]["created_at"] = old
        station.Station(GitHub("token", transport=self.fake), self.settings,
                        now=time.time()).post_overdue_standups()
        self.assertIn("In flight: none.", self.fake.comments[40][-1]["body"])

    def test_station_standup_lists_queued_tickets_needing_difficulty_once(self):
        self.add_goal()
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(
            time.time() - brain.REPORT_EVERY - brain.REPORT_GRACE - 60))
        self.comment(40, 1, "<!-- loom:report v1 -->\nold report")
        self.fake.comments[40][0]["created_at"] = old
        self.fake.sub_issues[40] = [
            {"number": 41, "title": "Needs a tier", "state": "open", "body": "## Why\nx",
             "labels": [{"name": "loom:ticket"}, {"name": "loom:queued"}]},
            {"number": 42, "title": "Has a tier", "state": "open",
             "body": "Difficulty: medium\n\n## Why\nx",
             "labels": [{"name": "loom:ticket"}, {"name": "loom:queued"}]},
        ]
        station.Station(GitHub("token", transport=self.fake), self.settings,
                        now=time.time()).post_overdue_standups()
        body = self.fake.comments[40][-1]["body"]
        self.assertIn("Needs difficulty: #41 Needs a tier.", body)
        self.assertEqual(body.count("Needs difficulty:"), 1)

    def report_aged(self, seconds):
        self.add_goal()
        self.comment(40, 1, "<!-- loom:report v1 -->\nearlier report")
        self.fake.comments[40][0]["created_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds))
        station.Station(GitHub("token", transport=self.fake), self.settings,
                        now=time.time()).post_overdue_standups()
        return [c for c in self.fake.comments[40][1:]
                if str(c.get("body", "")).startswith(station.REPORT_MARKER)]

    def test_station_posts_before_a_report_turns_two_hours_old(self):
        made = self.report_aged(brain.REPORT_EVERY - station.STANDUP_LEAD + 300)
        self.assertEqual(len(made), 1)

    def test_station_waits_while_the_report_is_well_inside_two_hours(self):
        made = self.report_aged(brain.REPORT_EVERY - station.STANDUP_LEAD - 900)
        self.assertEqual(made, [])

    def test_station_does_not_post_for_a_fresh_report(self):
        self.add_goal()
        fresh = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time()))
        self.comment(40, 1, "<!-- loom:report v1 -->\nfresh report")
        self.fake.comments[40][0]["created_at"] = fresh
        station.Station(GitHub("token", transport=self.fake), self.settings,
                        now=time.time()).post_overdue_standups()
        self.assertEqual(len(self.fake.comments[40]), 1)



class BrainTest(Base):
    def setUp(self):
        super().setUp()
        self.settings.webhook_ssm = "/example/webhook"
        self.settings.status_repo = REPO
        self.sent = []

    def tick(self, outcome="ok", code=0, ok_age=None, reason=None):
        state = os.path.join(self.home, "state")
        last = {"at": int(time.time()), "outcome": outcome, "exit": code}
        if reason is not None:
            last["reason"] = reason
        self.write(os.path.join(state, "brain.json"), json.dumps(last))
        if ok_age is not None:
            ok = os.path.join(state, "brain-ok")
            self.write(ok, "")
            at = time.time() - ok_age
            os.utime(ok, (at, at))

    def go(self, later=0):
        def post(url, payload):
            self.sent.append(payload["content"])
        gh = GitHub("token", transport=self.fake)
        station.Station(gh, self.settings, now=time.time() + later,
                        read_secret=lambda n, r: URL, post=post).run_pass()

    def test_no_brain_tick_installed_means_no_row_and_no_message(self):
        self.go()
        self.assertNotIn("| brain |", self.fake.posts[0])
        self.assertEqual(self.sent, [])

    def test_a_fresh_brain_is_published_and_quiet(self):
        self.tick(ok_age=60)
        self.go()
        self.assertIn("| brain | yes |", self.fake.posts[0])
        self.assertEqual(self.sent, [])

    def test_a_stale_brain_is_told_once_then_its_return_once(self):
        self.tick(outcome="failed", code=124, ok_age=3 * 3600)
        self.go()
        self.go(60)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Urgent: the Loom brain has not finished a tick since", self.sent[0])
        self.assertIn("failed (exit 124)", self.sent[0])
        self.assertIn("| brain | no |", self.fake.posts[0])
        self.tick(ok_age=0)
        self.go(120)
        self.go(180)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("ticking again", self.sent[1])

    def test_a_fresh_journal_keeps_a_stale_local_tick_quiet(self):
        self.tick(outcome="failed", code=1, ok_age=3 * 3600)
        self.fake.add_goal(50, [("alice-example", 600)])
        self.go()
        self.assertEqual(self.sent, [])

    def test_journals_from_other_logins_do_not_count(self):
        self.tick(outcome="failed", code=1, ok_age=3 * 3600)
        self.fake.add_goal(50, [("stranger-example", 60)])
        self.go()
        self.assertEqual(len(self.sent), 1)

    def test_both_stale_sends_once_naming_the_journal_then_recovery_once(self):
        self.tick(outcome="failed", code=1, ok_age=3 * 3600)
        self.fake.add_goal(50, [("bob-example", 3 * 3600)])
        self.go()
        self.go(60)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Newest journal:", self.sent[0])
        self.assertIn("%s#50" % REPO, self.sent[0])
        self.fake.add_goal(51, [("alice-example", 0)])
        self.go(700)
        self.go(800)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("ticking again", self.sent[1])

    def test_a_journal_older_than_a_day_is_not_named(self):
        self.tick(outcome="failed", code=1, ok_age=3 * 3600)
        self.fake.add_goal(50, [("alice-example", 25 * 3600)])
        self.go()
        self.assertEqual(len(self.sent), 1)
        self.assertIn("No journal from an allowed author in the last day.", self.sent[0])

    def test_a_journal_just_under_a_day_old_is_named(self):
        self.tick(outcome="failed", code=1, ok_age=3 * 3600)
        self.fake.add_goal(50, [("alice-example", 23 * 3600)])
        self.go()
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Newest journal:", self.sent[0])
        self.assertIn("%s#50" % REPO, self.sent[0])

    def test_a_github_read_error_sends_nothing(self):
        self.tick(outcome="failed", code=1, ok_age=3 * 3600)
        self.fake.goal_error = True
        self.go()
        self.assertEqual(self.sent, [])

    def test_a_brain_that_never_ended_well_is_told(self):
        self.tick(outcome="failed", code=1)
        self.go()
        self.assertEqual(len(self.sent), 1)
        self.assertIn("has not finished a tick yet", self.sent[0])

    def test_a_tick_skipped_with_no_lane_says_why_it_ran_nothing(self):
        # brain.sh touches no brain-ok when no rung has usage, so the skip
        # goes stale and the alert carries its reason.
        self.tick(outcome="skipped", ok_age=3 * 3600,
                  reason="no brain lane has usage: claude: unavailable; codex: no usage heartbeat")
        self.go()
        self.assertEqual(len(self.sent), 1)
        self.assertIn("skipped (exit 0) because no brain lane has usage: claude: unavailable; "
                      "codex: no usage heartbeat. Its log", self.sent[0])

    def test_a_skip_for_a_held_lock_counts_as_alive(self):
        self.tick(outcome="skipped", ok_age=0)
        self.go()
        self.assertEqual(self.sent, [])

    def test_odd_values_in_the_tick_file_never_reach_the_message(self):
        self.tick(outcome="/private/x <@1>", code="rm", ok_age=3 * 3600)
        self.go()
        self.assertIn("unknown (exit ?)", self.sent[0])
        self.assertNotIn("/private", self.sent[0])

    def test_a_failed_send_is_retried_next_pass(self):
        self.tick(outcome="failed", code=1, ok_age=3 * 3600)
        gh = GitHub("token", transport=self.fake)

        def down(url, payload):
            raise OSError("down")
        station.Station(gh, self.settings, read_secret=lambda n, r: URL, post=down).run_pass()
        self.go()
        self.assertEqual(len(self.sent), 1)


if __name__ == "__main__":
    unittest.main()


BOT_TOKEN = "BOT-TOKEN-SECRET"
OPERATOR = "123456"


class FakeDiscord:
    """The channel: messages in, the bot's posts and reactions out."""

    def __init__(self):
        self.messages = []
        self.calls = []
        self.sent = []
        self.reactions = []
        self.threads = []
        self.in_thread = []
        self.fail_post = False
        self.fail_thread = False

    def add(self, ident, content, author=OPERATOR, bot=False, attachments=()):
        self.messages.append({"id": str(ident), "content": content,
                              "author": {"id": author, "bot": bot},
                              "attachments": [{"url": u, "filename": "f.png"}
                                              for u in attachments]})

    def __call__(self, method, path, token, body):
        assert token == BOT_TOKEN
        self.calls.append((method, path))
        m = re.match(r"/channels/555/messages\?after=(\d+)&limit=100$", path)
        if method == "GET" and m:
            newer = [x for x in self.messages if int(x["id"]) > int(m.group(1))]
            return sorted(newer, key=lambda x: -int(x["id"]))
        if method == "PUT" and re.match(r"/channels/555/messages/\d+/reactions/%s/@me$"
                                        % station.CHECKMARK, path):
            self.reactions.append(path.split("/")[4])
            return None
        if method == "POST" and path == "/channels/555/messages":
            if self.fail_post:
                raise station.DiscordError("discord POST answered 500")
            self.sent.append(body)
            return {"id": str(1000 + len(self.sent))}
        m = re.match(r"/channels/555/messages/(\d+)/threads$", path)
        if method == "POST" and m:
            if self.fail_thread:
                raise station.DiscordError("discord POST answered 403")
            self.threads.append((m.group(1), body))
            return {"id": "9001"}
        if method == "POST" and path == "/channels/9001/messages":
            self.in_thread.append(body)
            return {"id": "9002"}
        raise AssertionError("unexpected discord call %s %s" % (method, path))


class OperatorTest(Base):
    def setUp(self):
        super().setUp()
        self.settings.channel_id = "555"
        self.settings.bot_token_ssm = "/example/bot-token"
        self.settings.mention = OPERATOR
        self.discord = FakeDiscord()
        self.clock = time.time()
        self.base = station._snowflake(self.clock)
        for number, title in ((40, "Members find events faster"),
                              (41, "Loom ships tickets quicker")):
            self.fake.add(number, title=title, state="goal")
            self.fake.issues[number]["labels"] = [{"name": "loom:goal"}]
        self.secrets = []

    def go(self, step=station.OPERATOR_CHECK + 1):
        def secret(name, region):
            self.secrets.append(name)
            return BOT_TOKEN
        self.clock += step
        gh = GitHub("token", transport=self.fake)
        station.Station(gh, self.settings, now=self.clock, read_secret=secret,
                        discord=self.discord).run_pass()

    def notes(self, number):
        return [c["body"] for c in self.fake.comments[number]
                if c["body"].startswith("<!-- loom:operator")]

    def say(self, k, content, **kw):
        ident = self.base + k
        self.discord.add(ident, content, **kw)
        return str(ident)

    def test_only_the_operators_messages_become_notes(self):
        mine = self.say(1, "Please look at %s#41 today" % REPO)
        self.say(2, "%s#41 from a stranger" % REPO, author="999")
        self.say(3, "%s#41 from a bot" % REPO, author=OPERATOR, bot=True)
        self.go()
        self.assertEqual(self.notes(41), ["<!-- loom:operator v1 discord=%s -->\n"
                                          "Please look at %s#41 today" % (mine, REPO)])
        self.assertEqual(self.notes(40), [])
        self.assertEqual(self.discord.reactions, [mine])
        self.assertEqual(len(self.discord.sent), 1)
        ack = self.discord.sent[0]
        self.assertIn("%s#41" % REPO, ack["content"])
        self.assertEqual(ack["message_reference"]["message_id"], mine)
        self.assertEqual(ack["allowed_mentions"], {"parse": []})
        self.assertEqual(ack["flags"], station.SUPPRESS_EMBEDS)

    def test_a_message_the_text_checks_refuse_is_not_noted(self):
        self.say(2, "%s#41 this fixes #3" % REPO)
        self.go()
        self.assertEqual(self.notes(41), [])
        self.assertTrue(any("Not noted" in b["content"] for b in self.discord.sent))

    def test_goal_choice_by_reference_number_title_word_and_default(self):
        self.say(1, "about #40 please")
        self.say(2, "the TICKETS are slow")
        self.go()
        self.assertEqual(len(self.notes(40)), 1)
        self.assertEqual(len(self.notes(41)), 1)
        self.assertTrue(self.notes(41)[0].endswith("the TICKETS are slow"))
        # Nothing named and no default: not noted, and told so.
        self.say(3, "hello there")
        self.go()
        self.assertEqual((len(self.notes(40)), len(self.notes(41))), (1, 1))
        self.assertIn("Not noted", self.discord.sent[-1]["content"])
        self.settings.default_goal = "%s#40" % REPO
        self.say(4, "hello again")
        self.go()
        self.assertTrue(self.notes(40)[-1].endswith("hello again"))

    def test_a_reference_outside_the_goal_homes_falls_back_to_the_default(self):
        self.settings.default_goal = "%s#41" % REPO
        self.say(1, "other/repo#40 and #12 and words in both: faster quicker")
        self.go()
        self.assertEqual(self.notes(40), [])
        self.assertEqual(len(self.notes(41)), 1)

    def test_each_message_is_relayed_once_across_passes(self):
        self.say(1, "#40 first")
        self.go()
        self.go()
        self.say(2, "#40 second")
        self.go()
        self.go()
        self.assertEqual([n.split("\n")[1] for n in self.notes(40)], ["#40 first", "#40 second"])
        self.assertEqual(len(self.discord.reactions), 2)

    def test_the_channel_is_read_at_most_every_thirty_seconds(self):
        self.go()
        self.go(step=10)
        self.assertEqual(len([c for c in self.discord.calls if c[0] == "GET"]), 1)
        self.go(step=station.OPERATOR_CHECK)
        self.assertEqual(len([c for c in self.discord.calls if c[0] == "GET"]), 2)

    def test_old_history_is_not_relayed_on_the_first_read(self):
        self.discord.add(station._snowflake(self.clock - 3600), "#40 an hour ago")
        self.say(1, "#40 just now")
        self.go()
        self.assertEqual([n.split("\n")[1] for n in self.notes(40)], ["#40 just now"])

    def test_a_github_failure_keeps_the_message_for_the_next_pass(self):
        self.say(1, "#40 keep me")
        real = self.fake

        def flaky(method, url, headers, body):
            if method == "POST" and url.endswith("/issues/40/comments"):
                real.calls.append((method, url))
                return reply(502, {"message": "bad gateway"})
            return real(method, url, headers, body)
        self.fake = flaky
        self.go()
        self.assertEqual(self.discord.reactions, [])
        self.fake = real
        self.go()
        self.assertEqual(len(self.notes(40)), 1)

    def reply_comment(self, number, ident, msg_id, text, author="alice-example", age=60):
        self.fake.comments[number].append({
            "id": ident, "user": {"login": author},
            "body": "<!-- loom:reply v1 discord=%s -->\n%s" % (msg_id, text),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                        time.gmtime(self.clock - age)),
            "html_url": "https://github.com/%s/issues/%d#issuecomment-%d"
                        % (REPO, number, ident)})

    def test_a_reply_comment_is_forwarded_once_as_a_reply(self):
        self.go()
        self.reply_comment(40, 900, "777", "Done: shipped the fix.")
        self.reply_comment(40, 901, "778", "A stranger's reply", author="stranger")
        self.reply_comment(41, 902, "779", "Too old", age=station.REPLY_LOOKBACK + 60)
        self.go()
        self.go()
        self.assertEqual(len(self.discord.sent), 1)
        sent = self.discord.sent[0]
        self.assertEqual(sent["content"], "Done: shipped the fix.\nhttps://github.com/"
                         "%s/issues/40#issuecomment-900" % REPO)
        self.assertEqual(sent["message_reference"]["message_id"], "777")
        self.assertEqual(sent["allowed_mentions"], {"parse": [], "replied_user": True})
        self.assertEqual(sent["flags"], station.SUPPRESS_EMBEDS)

    def test_a_failed_reply_is_sent_on_a_later_pass(self):
        self.go()
        self.reply_comment(40, 900, "777", "Answer")
        self.discord.fail_post = True
        self.go()
        self.assertEqual(self.discord.sent, [])
        self.discord.fail_post = False
        self.go(step=station.REPLY_OVERLAP)
        self.go()
        self.assertEqual([s["content"].split("\n")[0] for s in self.discord.sent], ["Answer"])

    def test_the_token_is_never_logged(self):
        import io
        from contextlib import redirect_stderr
        from unittest import mock
        self.say(1, "#40 hi")

        def leaky(method, path, token, body):
            raise ValueError("bad request with " + token)

        def opener_fails(req, timeout):
            raise OSError("connect failed, header Bot " + BOT_TOKEN)
        err = io.StringIO()
        with redirect_stderr(err):
            self.discord = leaky
            self.go()
            self.discord = station.discord_call
            with mock.patch.object(station._OPENER, "open", side_effect=opener_fails):
                self.go()
            gh = GitHub("token", transport=self.fake)

            def secret_fails(name, region):
                raise OSError("token " + BOT_TOKEN)
            self.clock += station.OPERATOR_CHECK + 1
            station.Station(gh, self.settings, now=self.clock,
                            read_secret=secret_fails).run_pass()
        log = err.getvalue()
        self.assertIn("discord call failed", log)
        self.assertIn("bot token parameter", log)
        self.assertNotIn(BOT_TOKEN, log)

    def test_with_the_settings_empty_nothing_runs(self):
        self.say(1, "#40 hi")
        for field in ("channel_id", "bot_token_ssm", "mention"):
            saved = getattr(self.settings, field)
            setattr(self.settings, field, "")
            self.go()
            setattr(self.settings, field, saved)
        self.assertEqual(self.discord.calls, [])
        self.assertEqual(self.secrets, [])
        self.assertEqual(self.notes(40), [])

    def test_settings_come_from_the_environment(self):
        s = station.Settings.from_env({
            "LOOM_DISCORD_CHANNEL_ID": " 555 ", "LOOM_DISCORD_BOT_TOKEN_SSM": "/x/bot",
            "LOOM_DISCORD_DEFAULT_GOAL": "a/b#3"})
        self.assertEqual((s.channel_id, s.bot_token_ssm, s.default_goal),
                         ("555", "/x/bot", "a/b#3"))

    def test_attachments_are_linked_in_the_note(self):
        shot = "https://cdn.example.com/attachments/1/2/shot.png"
        mine = self.say(1, "#40 change these", attachments=[shot])
        self.go()
        self.assertEqual(self.notes(40), ["<!-- loom:operator v1 discord=%s -->\n"
                                          "#40 change these\n\nAttachments:\n- %s"
                                          % (mine, shot)])

    def test_an_attachment_only_message_is_relayed_and_the_ack_names_no_goal(self):
        self.settings.default_goal = "%s#41" % REPO
        shot = "https://cdn.example.com/attachments/1/2/shot.png"
        mine = self.say(1, "", attachments=[shot])
        self.go()
        self.assertEqual(self.notes(41), ["<!-- loom:operator v1 discord=%s -->\n"
                                          "Attachments:\n- %s" % (mine, shot)])
        ack = self.discord.sent[-1]
        self.assertTrue(ack["content"].startswith("Noted for the brain https://"))
        self.assertNotIn("#41", ack["content"].split(" https://")[0])
        self.assertEqual(ack["flags"], station.SUPPRESS_EMBEDS)


class BotReportsTest(Base):
    def setUp(self):
        super().setUp()
        self.settings.channel_id = "555"
        self.settings.bot_token_ssm = "/example/bot-token"
        self.discord = FakeDiscord()
        self.fake.add(40, title="Members find events faster than ever before",
                      body="## Why\nBecause.\n\nShort name: Events\n", state="goal")
        self.fake.issues[40]["labels"] = [{"name": "loom:goal"}]
        self.fake.add(41, title="Loom ships tickets quicker", state="goal")
        self.fake.issues[41]["labels"] = [{"name": "loom:goal"}]
        self.clock = time.time()
        self.go()

    def go(self):
        self.clock += station.REPORT_CHECK + 1
        gh = GitHub("token", transport=self.fake)
        station.Station(gh, self.settings, now=self.clock,
                        read_secret=lambda n, r: BOT_TOKEN, discord=self.discord,
                        post=lambda u, p: self.fail("the webhook was used")).run_pass()

    def comment(self, number, ident, body):
        self.fake.comments[number].append({
            "id": ident, "body": station.REPORT_MARKER + "\n" + body,
            "user": {"login": "alice-example"},
            "html_url": "https://github.com/%s/issues/%d#issuecomment-%d"
                        % (REPO, number, ident)})

    def test_a_report_is_a_short_line_with_a_thread_holding_the_full_report(self):
        self.comment(40, 100, "## Shipped the search page\nDetail one\nDetail two")
        self.comment(41, 101, "Two tickets merged\nMore")
        self.go()
        self.go()
        self.assertEqual([m["content"] for m in self.discord.sent],
                         ["**Events** Shipped the search page",
                          "**Loom ships tickets quicker** Two tickets merged"])
        self.assertEqual([t[0] for t in self.discord.threads], ["1001", "1002"])
        self.assertTrue(self.discord.threads[0][1]["name"].startswith("Events 20"))
        link = "https://github.com/%s/issues/40#issuecomment-100" % REPO
        self.assertEqual(self.discord.in_thread[0]["content"],
                         "Detail one\nDetail two\n" + link)
        for body in self.discord.sent + self.discord.in_thread:
            self.assertEqual(body["flags"], station.SUPPRESS_EMBEDS)
            self.assertEqual(body["allowed_mentions"], {"parse": []})

    def test_a_long_report_fills_several_thread_messages(self):
        self.comment(40, 100, "line\n" * 700)
        self.go()
        parts = [m["content"] for m in self.discord.in_thread]
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= station.DISCORD_MAX for p in parts))
        # The first line is the channel line, so the thread holds the rest.
        self.assertEqual("\n".join(parts).count("line"), 699)
        self.assertTrue(parts[-1].endswith("#issuecomment-100"))

    def test_a_failed_line_is_retried_and_a_failed_thread_is_not(self):
        self.comment(40, 100, "report")
        self.discord.fail_post = True
        self.go()
        self.assertEqual(self.discord.sent, [])
        self.discord.fail_post = False
        self.go()
        self.assertEqual(len(self.discord.sent), 1)
        self.assertEqual(len(self.discord.threads), 1)
        # Once the line is out, a failed thread does not post it again, and
        # the full report goes in the channel instead.
        self.comment(41, 101, "second report")
        self.discord.fail_thread = True
        self.go()
        self.go()
        self.assertEqual([m["content"].split("\n")[0] for m in self.discord.sent],
                         ["**Events** report", "**Loom ships tickets quicker** second report",
                          "second report"])
        # A one-line report is all in its channel line; its thread holds the link.
        self.assertTrue(self.discord.in_thread[-1]["content"].endswith("#issuecomment-100"))

    def test_markdown_in_the_short_name_is_dropped(self):
        self.fake.issues[41]["body"] = "Short name: **Big** _win_ `x` ~~y~~ ||z||\n"
        self.comment(41, 101, "status")
        self.go()
        self.assertEqual(self.discord.sent[0]["content"], "**Big win x y z** status")

    def test_a_report_on_an_issue_without_the_goal_label_is_not_forwarded(self):
        self.fake.add(42, title="Not a goal", body="Short name: Nope\n", state="briefed")
        self.comment(42, 102, "should stay on GitHub")
        self.go()
        self.assertEqual(self.discord.sent, [])


UPLOAD_URL = "https://dashboard.example.com/api/transcripts?key=QUERY-SECRET"
UPLOAD_TOKEN = "UPLOAD-TOKEN-SECRET"


class TranscriptUploadTest(Base):
    """bus/station.py upload_transcripts: each finished run goes to the
    dashboard once, failures are retried, and nothing goes without settings."""

    def setUp(self):
        super().setUp()
        self.settings.transcript_url = UPLOAD_URL
        self.settings.transcript_token_ssm = "/example/transcript-token"
        self.sent = []
        self.fail = 0
        self.reads = 0
        self.projects = os.path.join(self._tmp.name, "claude-projects")

    def read_secret(self, name, region):
        self.reads += 1
        self.assertEqual(name, "/example/transcript-token")
        return UPLOAD_TOKEN

    def upload(self, url, token, body):
        if isinstance(body, dict) and body["ticket"] in getattr(self, "refuse", ()):
            raise station.UploadRefused(400)
        if self.fail:
            self.fail -= 1
            raise OSError("connect to %s with %s failed" % (url, token))
        if isinstance(body, list):  # a usage snapshot, not a transcript
            self.usage_sent = getattr(self, "usage_sent", []) + [body]
            return
        self.sent.append((url, token, body))

    def usage(self):
        err = io.StringIO()
        st = station.Station(GitHub("token", transport=self.fake), self.settings,
                             read_secret=self.read_secret, upload=self.upload)
        with contextlib.redirect_stderr(err), \
                mock.patch.object(station.time, "sleep") as pause:
            st.upload_usage_limits([{"lane": "codex"}])
        return err.getvalue(), pause

    def test_a_usage_snapshot_that_fails_once_is_sent_again(self):
        self.fail = 1
        out, pause = self.usage()
        self.assertEqual(self.usage_sent, [[{"lane": "codex"}]])
        self.assertNotIn("usage snapshot upload failed", out)
        pause.assert_called_once_with(station.USAGE_RETRY_PAUSE)

    def test_a_usage_snapshot_that_fails_twice_says_why_without_the_token(self):
        self.fail = 2
        out, _ = self.usage()
        self.assertEqual(getattr(self, "usage_sent", []), [])
        self.assertIn("usage snapshot upload failed twice: OSError: connect to", out)
        self.assertNotIn(UPLOAD_TOKEN, out)

    def go(self, read_secret=None):
        err = io.StringIO()
        st = station.Station(GitHub("token", transport=self.fake), self.settings,
                             read_secret=read_secret or self.read_secret, upload=self.upload)
        st.claude_projects = Path(self.projects)
        with contextlib.redirect_stderr(err):
            st.upload_transcripts()
        return err.getvalue()

    def ref(self, name):
        """The ticket each finished brief names in its Issue: line, one per name."""
        self.issues = getattr(self, "issues", {})
        return "%s#%d" % (REPO, self.issues.setdefault(name, 100 + len(self.issues)))

    def finish(self, name="T1", kind="done", lane="anthropic-2"):
        self.write(self.inbox(lane, kind, name + ".md"),
                   "# Brief: %s\n\nModel: model-x\nIssue: %s\n" % (name, self.ref(name)))
        self.write(os.path.join(self.home, "logs", lane, name + ".log"),
                   "[2026-10-13T09:00:00Z] %s start, lane %s\nagent said hi from /Users/someone\n"
                   % (name, lane))

    def ledger(self):
        with open(os.path.join(self.home, "state", "station", "transcripts.json")) as fh:
            return json.load(fh)

    def test_a_finished_run_uploads_once_with_scrubbed_events(self):
        self.go()  # the first pass only records what had already finished
        self.finish()
        self.go()
        self.go()
        self.assertEqual(len(self.sent), 1)
        url, token, body = self.sent[0]
        self.assertEqual((url, token), (UPLOAD_URL, UPLOAD_TOKEN))
        self.assertEqual({k: body[k] for k in ("ticket", "slot", "model", "outcome", "started_at")},
                         {"ticket": self.ref("T1"), "slot": "anthropic-2", "model": "model-x",
                          "outcome": "done", "started_at": "2026-10-13T09:00:00Z"})
        self.assertEqual([e["kind"] for e in body["events"]], ["log"])
        self.assertIn("agent said hi from <path>", body["events"][0]["text"])
        self.assertEqual(self.ledger()["anthropic-2/T1"], "sent")

    def test_a_refused_run_is_given_up_at_once_and_the_next_still_sends(self):
        self.go()
        self.finish("A")
        self.finish("B")
        self.refuse = {self.ref("A")}
        out = self.go()
        self.assertEqual([b["ticket"] for _, _, b in self.sent], [self.ref("B")])
        self.assertEqual(self.ledger()["anthropic-2/A"], "refused")
        self.assertIn("refused the transcript of anthropic-2/A (400)", out)

    def test_a_wrong_token_is_retried_not_given_up(self):
        self.assertNotIn(401, station.REFUSED_BODY)
        self.assertNotIn(403, station.REFUSED_BODY)

    def test_a_run_with_no_ticket_is_skipped_and_the_next_still_sends(self):
        self.go()
        self.write(self.inbox("anthropic-2", "done", "T001-old-style.md"), "# Brief: T001\n")
        self.finish("B")
        self.go()
        self.assertEqual([b["ticket"] for _, _, b in self.sent], [self.ref("B")])
        self.assertEqual(self.ledger()["anthropic-2/T001-old-style"], "no ticket")

    def test_runs_finished_before_uploads_were_turned_on_are_not_sent(self):
        self.finish("OLD", kind="failed")
        self.go()
        self.finish("NEW")
        self.go()
        self.assertEqual([b["ticket"] for _, _, b in self.sent], [self.ref("NEW")])
        self.assertEqual(self.ledger()["anthropic-2/OLD"], "old")

    def test_a_failed_upload_is_retried_on_the_next_pass(self):
        self.go()
        self.finish()
        self.fail = 1
        log = self.go()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.ledger()["anthropic-2/T1"], 1)
        self.assertEqual(log, "")
        self.go()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.ledger()["anthropic-2/T1"], "sent")

    def test_a_run_is_given_up_after_five_failures_with_one_line(self):
        self.go()
        self.finish()
        self.fail = 99
        logs = "".join(self.go() for _ in range(7))
        self.assertEqual(self.ledger()["anthropic-2/T1"], "gave up")
        self.assertEqual(logs.count("\n"), 1)
        self.assertIn("gave up uploading the transcript of anthropic-2/T1 after 5 tries (OSError)", logs)
        for secret in (UPLOAD_TOKEN, "QUERY-SECRET", "dashboard.example.com"):
            self.assertNotIn(secret, logs)
        self.assertEqual(self.fail, 94)

    def test_a_failed_upload_ends_the_pass_so_a_down_dashboard_costs_one_timeout(self):
        self.go()
        self.finish("A")
        self.finish("B")
        self.fail = 1
        self.go()
        self.assertEqual(self.sent, [])
        self.go()
        self.assertEqual(sorted(b["ticket"] for _, _, b in self.sent), sorted([self.ref("A"), self.ref("B")]))

    def test_nothing_is_sent_without_both_settings(self):
        for url, ssm in (("", "/example/transcript-token"), (UPLOAD_URL, "")):
            self.settings.transcript_url, self.settings.transcript_token_ssm = url, ssm
            self.go()
            self.finish()
            self.go()
        self.assertEqual((self.sent, self.reads), ([], 0))
        self.assertFalse(os.path.exists(
            os.path.join(self.home, "state", "station", "transcripts.json")))

    def test_an_unreadable_or_hung_token_read_skips_the_pass_without_the_secret(self):
        import subprocess

        def hung(name, region):
            raise subprocess.TimeoutExpired(["aws", "ssm"], 30, output=UPLOAD_TOKEN)
        self.go()
        self.finish()
        log = self.go(read_secret=hung)
        self.assertIn("could not read the transcript token parameter (TimeoutExpired)", log)
        self.assertNotIn(UPLOAD_TOKEN, log)
        self.assertNotIn("anthropic-2/T1", self.ledger())  # not counted as a try
        self.go()
        self.assertEqual(len(self.sent), 1)

    def test_the_token_is_read_only_when_there_is_something_to_send(self):
        self.go()
        self.go()
        self.assertEqual(self.reads, 0)

    def brain_tick(self, started, outcome="ok"):
        work = os.path.join(self.home, "brain")
        project = os.path.join(self.projects, work.replace("/", "-").replace(".", "-"))
        self.write(os.path.join(project, "s%d.jsonl" % started), json.dumps(
            {"type": "user", "cwd": work, "message": {"content": "tick %d" % started}}) + "\n")
        os.utime(os.path.join(project, "s%d.jsonl" % started), (started + 10, started + 10))
        self.write(os.path.join(self.home, "state", "brain.json"), json.dumps(
            {"at": started + 20, "outcome": outcome, "exit": 0, "started": started,
             "model": "model-b"}))

    def test_each_finished_brain_tick_uploads_once(self):
        self.brain_tick(1000)
        self.go()  # already finished when uploads were turned on
        self.brain_tick(2000)
        self.go()
        self.go()
        self.brain_tick(3000, outcome="failed")
        self.go()
        self.assertEqual([(b["slot"], b["ticket"], b["model"], b["outcome"]) for _, _, b in self.sent],
                         [("brain", "brain/1970-01-01T00:33:20Z", "model-b", "ok"),
                          ("brain", "brain/1970-01-01T00:50:00Z", "model-b", "failed")])
        self.assertEqual([e["text"] for e in self.sent[0][2]["events"]], ["tick 2000"])

    def test_a_skipped_brain_tick_uploads_nothing(self):
        self.go()
        self.write(os.path.join(self.home, "state", "brain.json"),
                   json.dumps({"at": 5000, "outcome": "skipped", "exit": 0}))
        self.go()
        self.assertEqual(self.sent, [])

    def test_the_pass_runs_with_the_station(self):
        self.go()
        self.finish(lane="codex")
        gh = GitHub("token", transport=self.fake)
        st = station.Station(gh, self.settings, read_secret=self.read_secret, upload=self.upload)
        with contextlib.redirect_stderr(io.StringIO()):
            st.run_pass()
        self.assertEqual([b["slot"] for _, _, b in self.sent], ["codex"])

    def test_settings_come_from_the_environment(self):
        s = station.Settings.from_env({"LOOM_TRANSCRIPT_URL": " %s " % UPLOAD_URL,
                                       "LOOM_TRANSCRIPT_TOKEN_SSM": " /x/token "})
        self.assertEqual((s.transcript_url, s.transcript_token_ssm), (UPLOAD_URL, "/x/token"))

    def test_install_passes_both_settings_to_the_station(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "lanes", "mac", "install.sh")) as fh:
            text = fh.read()
        for name in ("LOOM_TRANSCRIPT_URL", "LOOM_TRANSCRIPT_TOKEN_SSM"):
            self.assertIn('<key>%s</key><string>$(xml "${%s:-}")</string>' % (name, name), text)


class PostTranscriptTest(unittest.TestCase):
    def test_only_https_and_errors_never_carry_the_url_or_token(self):
        with self.assertRaises(OSError) as ctx:
            station.post_transcript("http://dashboard.example.com/x?key=Q", UPLOAD_TOKEN, {})
        self.assertNotIn("dashboard", str(ctx.exception))

    def test_a_refused_upload_names_only_the_status(self):
        import urllib.error

        def refuse(req, timeout):
            self.assertEqual(req.get_header("Authorization"), "Bearer " + UPLOAD_TOKEN)
            raise urllib.error.HTTPError(req.full_url, 401, "no", {}, io.BytesIO(b""))
        with mock.patch.object(station._OPENER, "open", refuse):
            with self.assertRaises(OSError) as ctx:
                station.post_transcript(UPLOAD_URL, UPLOAD_TOKEN, {"events": []})
        self.assertEqual(str(ctx.exception), "the transcript upload answered 401")


class VerifyDueTest(unittest.TestCase):
    def test_oldest_shipped_proofless_ticket_and_six_hour_cooldown(self):
        def item(number, shipped_at, **extra):
            return (REPO, {"number": number, "state": "open", "shipped_at": shipped_at,
                           "labels": [{"name": "loom:ticket"}, {"name": "loom:shipped"}],
                           **extra})
        tickets = [item(2, 200), item(1, 100), item(3, 50, has_proof=True)]
        found = station.verify_due(tickets, {"%s#1" % REPO: 1000}, 1001)
        self.assertEqual([issue["number"] for _, issue in found], [2])

    def test_only_shipped_ticket_without_verified_state_is_due(self):
        issue = {"number": 4, "state": "open", "shipped_at": 1,
                 "labels": [{"name": "loom:ticket"}, {"name": "loom:verified"}]}
        self.assertEqual(station.verify_due([(REPO, issue)], {}, 99999), [])


SHIPPED_BODY = ("Difficulty: easy\n\n## Why\nBecause.\n\n## Acceptance criteria\n"
                "- [ ] A test passes\n- [ ] Live: the daily line shows it\n")
HOUR = 3600


def shipped_at(age_hours):
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - age_hours * HOUR))
    return [dict(labeled("loom:shipped", "alice-example"), created_at=stamp)]


class VerifyDispatchTest(Base):
    def setUp(self):
        super().setUp()
        self.fake.private = False
        self.fake.add(1, lane="codex", body=SHIPPED_BODY, state="shipped", events=shipped_at(10))
        self.fake.add(2, lane="codex", body=SHIPPED_BODY, state="shipped", events=shipped_at(30))

    def briefs(self, lane="codex"):
        try:
            return sorted(n for n in os.listdir(self.inbox(lane)) if n.endswith(".md"))
        except OSError:
            return []

    def text(self, lane="codex"):
        with open(os.path.join(self.inbox(lane), self.briefs(lane)[0])) as fh:
            return fh.read()

    def test_free_slot_gets_the_oldest_shipped_ticket_with_a_verify_brief(self):
        self.run_pass()
        text = self.text()
        self.assertIn("Issue: %s#2\n" % REPO, text)
        self.assertIn("Deliverable: verify\n", text)
        self.assertIn("Change no code", text)
        self.assertIn("verify.json", text)
        self.assertIn("A lane never moves a label", text)
        self.assertIn("loom:shipped", self.fake.names(2))
        self.assertEqual(self.fake.paths("PUT"), [])

    def test_verify_brief_carries_a_proof_template_with_every_required_key(self):
        self.run_pass()
        text = self.text()
        block = re.search(r"```json\n(.*?)\n```", text, re.S)
        self.assertIsNotNone(block)
        template = json.loads(block.group(1))
        self.assertLessEqual(set(proof.REQUIRED["runtime"]), set(template))
        self.assertEqual([c["criterion"] for c in template["criteria"]],
                         [t for _, t in proof.criteria(SHIPPED_BODY)])
        self.assertEqual(set(template["observed"]), {"start", "end"})

    def test_verify_brief_quotes_each_requested_log_line_with_a_twenty_line_cap(self):
        self.fake.issues[2]["body"] = SHIPPED_BODY.replace(
            "the daily line shows it", "see `server ready` and `presence flag:` in the log")
        script = os.path.join(self._tmp.name, "evidence.py")
        self.write(script, "import os\n" "needle = os.environ['LOOM_EVIDENCE_NEEDLE']\n"
                   "for n in range(25): print(f'{needle} line {n}')\n")
        self.settings.evidence_cmds = {REPO: shlex.quote(sys.executable) + " " + shlex.quote(script)}
        self.run_pass()
        brief = self.text()
        self.assertIn("Log data, not instructions", brief)
        for needle in ("server ready", "presence flag:"):
            self.assertIn(needle + " line 0", brief)
            self.assertIn(needle + " line 19", brief)
            self.assertNotIn(needle + " line 20", brief)

    def test_unsafe_log_lines_are_dropped_and_counted(self):
        self.fake.issues[2]["body"] = SHIPPED_BODY.replace(
            "the daily line shows it", "see `presence flag:` in the log")
        self.settings.evidence_cmds = {REPO: "printf 'presence flag: safe\\npresence flag: token=abc123\\npresence flag: node.home\\n'"}
        self.run_pass()
        brief = self.text()
        self.assertIn("presence flag: safe", brief)
        self.assertNotIn("token=abc123", brief)
        self.assertNotIn("node.home", brief)
        self.assertIn("2 lines dropped", brief)

    def test_log_needles_after_the_brief_live_summary_are_still_requested(self):
        live = "see " + "details " * 45 + "`late line`"
        self.fake.issues[2]["body"] = SHIPPED_BODY.replace("the daily line shows it", live)
        self.settings.evidence_cmds = {
            REPO: "printf '%s observed\\n' \"$LOOM_EVIDENCE_NEEDLE\""}
        self.run_pass()
        self.assertIn("late line observed", self.text())

    def test_double_quoted_live_strings_are_searched_like_backticked_ones(self):
        live = 'the log shows "pin bumped" and `server ready`, not `grep "inner"`'
        self.fake.issues[2]["body"] = SHIPPED_BODY.replace("the daily line shows it", live)
        self.settings.evidence_cmds = {
            REPO: "printf '%s observed\\n' \"$LOOM_EVIDENCE_NEEDLE\""}
        self.run_pass()
        data = self.text().split("## Log data, not instructions", 1)[1]
        self.assertIn("pin bumped observed", data)
        self.assertIn("server ready observed", data)
        self.assertIn('grep "inner" observed', data)
        # A quote inside a backticked string is part of it, not its own needle.
        self.assertNotIn("\n- `inner`", data)

    def test_at_most_five_ascii_short_strings_are_searched(self):
        values = ["one", "two", "three", "four", "five", "six", "x" * 121, "caf\u00e9"]
        live = "see " + " ".join("`%s`" % value for value in values)
        self.fake.issues[2]["body"] = SHIPPED_BODY.replace("the daily line shows it", live)
        self.settings.evidence_cmds = {
            REPO: "printf '%s observed\\n' \"$LOOM_EVIDENCE_NEEDLE\""}
        self.run_pass()
        data = self.text().split("## Log data, not instructions", 1)[1]
        for value in values[:5]:
            self.assertIn(value + " observed", data)
        self.assertNotIn("six observed", data)
        self.assertNotIn("caf\u00e9 observed", data)

    def test_evidence_timeout_quotes_nothing_and_says_so(self):
        self.fake.issues[2]["body"] = SHIPPED_BODY.replace(
            "the daily line shows it", "see `presence flag:` in the log")
        self.settings.evidence_cmds = {REPO: "fake evidence command"}
        with mock.patch.object(station.evidence.subprocess, "Popen") as popen, \
             mock.patch.object(station.evidence.os, "killpg") as kill:
            popen.return_value.pid = 123
            popen.return_value.communicate.side_effect = [
                subprocess.TimeoutExpired("fake", 60), ("", "")]
            self.run_pass()
        self.assertEqual(popen.return_value.communicate.call_args_list[0].kwargs["timeout"], 60)
        self.assertEqual(popen.call_args.args[0], ["/bin/sh", "-c", "fake evidence command"])
        self.assertFalse(popen.call_args.kwargs.get("shell", False))
        kill.assert_called_once()
        self.assertIn("timed out", self.text())
        self.assertNotIn("presence flag: safe", self.text())

    def test_no_log_line_for_the_live_criterion_spends_no_verify_run(self):
        # 2026-10-18: a live criterion waiting on a rare in-game event got
        # a verify run and an emailed comment every 6 hours for days.
        self.fake.issues[2]["body"] = SHIPPED_BODY.replace(
            "the daily line shows it", "see `presence flag:` in the log")
        self.settings.evidence_cmds = {REPO: "fake evidence command"}
        with mock.patch.object(station.evidence.subprocess, "Popen") as popen:
            popen.return_value.communicate.return_value = ("other line\n", "")
            popen.return_value.returncode = 0
            self.run_pass()
        # Ticket 2 gets no run; the free slot goes to the next due ticket.
        self.assertFalse(any("-2-verify-" in n for n in self.briefs()), self.briefs())
        self.assertIn("loom:awaiting-evidence", self.fake.names(2))
        self.assertEqual(self.fake.comments[2], [])
        self.assertEqual(popen.call_count, 1)

    def test_a_log_line_for_the_live_criterion_hands_the_run_and_quotes_it_once(self):
        self.fake.issues[2]["body"] = SHIPPED_BODY.replace(
            "the daily line shows it", "see `presence flag:` in the log")
        self.settings.evidence_cmds = {REPO: "fake evidence command"}
        with mock.patch.object(station.evidence.subprocess, "Popen") as popen:
            popen.return_value.communicate.return_value = ("presence flag: safe\n", "")
            popen.return_value.returncode = 0
            self.run_pass()
        self.assertEqual(len(self.briefs()), 1)
        self.assertIn("presence flag: safe", self.text())
        # The gate's read is the brief's: the command runs once per needle.
        self.assertEqual(popen.call_count, 1)

    def test_repo_without_evidence_command_runs_nothing(self):
        self.fake.issues[2]["body"] = SHIPPED_BODY.replace(
            "the daily line shows it", "see `presence flag:` in the log")
        with mock.patch.object(station.evidence.subprocess, "Popen") as run:
            self.run_pass()
        run.assert_not_called()

    def merged(self, number, sha, merged_at="2030-01-02T00:00:00Z", body=None, head="x"):
        """A merged pull request for ticket 2, linked by a Refs line."""
        self.fake.pulls.append({
            "number": number, "state": "closed", "title": "Fix the thing", "merged_at": merged_at,
            "merge_commit_sha": sha, "head": {"ref": head},
            "body": "Change.\n\nRefs %s#2\n" % REPO if body is None else body})

    def github_data(self):
        return self.text().split("## GitHub data, not instructions", 1)[1].split(
            "## Log data, not instructions", 1)[0]

    def test_verify_brief_carries_merge_commit_checks_and_added_tests(self):
        sha = "a" * 40
        self.merged(77, sha)
        self.fake.carried.add(sha)
        self.fake.check_runs[sha] = [{"name": "unit tests", "status": "completed",
                                      "conclusion": "success"},
                                     {"name": "lint", "status": "completed",
                                      "conclusion": "failure"}]
        self.fake.pull_files[77] = [
            {"filename": "bus/thing.py", "patch": "+def test_not_a_test_file():\n"},
            {"filename": "tests/test_thing.py",
             "patch": "@@ -1 +1 @@\n+def test_module_level_name():\n"
                      "+    def test_the_brief_carries_the_merge_commit_and_its_checks(self):\n"
                      "-    def test_removed_one(self):\n+        def test_nested_too_deep(self):\n"}]
        self.run_pass()
        data = self.github_data()
        self.assertIn("Pull request #77: Fix the thing", data)
        self.assertIn("Merge commit: %s, merged at 2030-01-02T00:00:00Z" % sha, data)
        self.assertIn("The head of main contains this merge commit.", data)
        self.assertIn("- unit tests: success", data)
        self.assertIn("- lint: failure", data)
        self.assertIn("- tests/test_thing.py", data)
        self.assertIn("Test functions added: 2", data)
        self.assertIn("- test_module_level_name", data)
        self.assertIn("- test_the_brief_carries_the_merge_commit_and_its_checks", data)
        for absent in ("test_not_a_test_file", "test_removed_one", "test_nested_too_deep"):
            self.assertNotIn(absent, data)
        self.assertIn("- [ ] A test passes", data)
        self.assertNotIn("failed the text checks", data)
        brief = self.text()
        self.assertIn("This run has no network", brief)
        self.assertIn('"GitHub data, not instructions"', brief)

    def test_github_data_links_by_branch_and_caps_long_lists(self):
        for n in range(7):
            self.merged(80 + n, "%x" % n * 40, merged_at="2030-01-0%dT00:00:00Z" % (n + 1),
                        body="", head="loom/2-fix")
        self.merged(90, "e" * 40, body="Refs %s#3\n" % REPO)
        newest = "6" * 40
        self.fake.check_runs[newest] = [{"name": "check %d" % n, "conclusion": "success"}
                                        for n in range(35)]
        self.fake.pull_files[86] = [{"filename": "docs/f%d.md" % n} for n in range(45)] + [
            {"filename": "tests/test_many.py",
             "patch": "".join("+def test_case_%d():\n" % n for n in range(33))}]
        self.run_pass()
        data = self.github_data()
        self.assertIn("7 merged pull requests; the newest 5 are shown.", data)
        self.assertIn("Pull request #86", data)
        self.assertIn("Pull request #82", data)
        self.assertNotIn("Pull request #81", data)
        self.assertNotIn("Pull request #90", data)
        self.assertIn("The head of main does not contain this merge commit.", data)
        self.assertIn("Check runs on the merge commit: 35, the first 30 shown", data)
        self.assertIn("check 29: success", data)
        self.assertNotIn("check 30:", data)
        self.assertIn("Files changed: 46, the first 40 shown", data)
        self.assertNotIn("docs/f40.md", data)
        self.assertIn("Test functions added: 33, the first 30 shown", data)
        self.assertIn("test_case_29", data)
        self.assertNotIn("test_case_30", data)

    def test_github_data_lines_pass_the_log_text_checks(self):
        sha = "b" * 40
        self.merged(77, sha)
        self.fake.check_runs[sha] = [{"name": "deploy token=abc123", "conclusion": "success"},
                                     {"name": "probe node.internal", "conclusion": "success"},
                                     {"name": "ping 10.1.2.3", "conclusion": "success"},
                                     {"name": "kept check", "conclusion": "success"}]
        self.fake.pull_files[77] = [{"filename": "/home/example/notes.txt"},
                                    {"filename": "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"},
                                    {"filename": "tests/test_kept.py",
                                     "patch": "+def test_a_long_snake_case_name_is_not_a_token():\n"}]
        self.run_pass()
        data = self.github_data()
        for absent in ("token=abc123", "node.internal", "10.1.2.3", "/home/", "AbCdEf"):
            self.assertNotIn(absent, data)
        self.assertIn("kept check: success", data)
        self.assertIn("test_a_long_snake_case_name_is_not_a_token", data)
        self.assertIn("Merge commit: %s" % sha, data)
        self.assertIn("5 lines failed the text checks and were dropped.", data)

    def test_only_station_extracted_names_skip_the_long_token_test(self):
        sha = "c" * 40
        uuid = "123e4567-e89b-42d3-a456-426614174000"
        hex_dash = "deadbeef-cafebabe-0123abcd-4567ef01-89ab"
        lowercase_key = "alpha-bravo-charlie-delta-echo-foxtrot"
        long_name = "test_a_merged_pr_delivers_the_ticket_while_a_duplicate_is_open"
        self.merged(77, sha)
        self.fake.pulls[0]["title"] = "Ship %s" % uuid
        self.fake.check_runs[sha] = [{"name": hex_dash, "conclusion": "success"},
                                     {"name": lowercase_key, "conclusion": "success"}]
        self.fake.pull_files[77] = [{"filename": "docs/%s.md" % uuid},
                                    {"filename": "tests/test_long.py",
                                     "patch": "+    def %s(self):\n" % long_name}]
        self.run_pass()
        data = self.github_data()
        for absent in (uuid, hex_dash, lowercase_key):
            self.assertNotIn(absent, data)
        self.assertIn("- tests/test_long.py", data)
        self.assertIn("- " + long_name, data)
        self.assertIn("4 lines failed the text checks and were dropped.", data)

    def test_a_known_name_skips_the_token_test_only_on_its_own_line(self):
        sha = "e" * 40
        long_name = "test_a_merged_pr_delivers_the_ticket_while_a_duplicate_is_open"
        # Too short to trip the long-token test alone; joined to the known
        # name it forms one long run.
        token = "Zq9XvB7mK2pL4nR8"
        self.merged(77, sha)
        self.fake.pulls[0]["title"] = "Add %s%s" % (long_name, token)
        self.fake.check_runs[sha] = [{"name": long_name + token, "conclusion": "success"}]
        self.fake.pull_files[77] = [{"filename": "tests/test_long.py",
                                     "patch": "+    def %s(self):\n" % long_name}]
        self.run_pass()
        data = self.github_data()
        self.assertNotIn(token, data)
        self.assertNotIn("Pull request #77:", data)
        self.assertIn("\n        - %s\n" % long_name, data)
        self.assertIn("2 lines failed the text checks and were dropped.", data)

    def test_a_ticket_with_no_merged_pull_request_says_so_in_one_line(self):
        self.fake.pulls = [{"number": 70, "state": "closed", "merged_at": None,
                            "merge_commit_sha": "c" * 40, "body": "Refs %s#2\n" % REPO}]
        self.run_pass()
        data = self.github_data()
        self.assertEqual(data.count("pull request"), 1)
        self.assertIn("- No merged pull request names this ticket.", data)
        self.assertIn("- [ ] Live: the daily line shows it", data)

    def test_a_github_failure_is_one_line_and_the_brief_still_goes_out(self):
        self.merged(77, "d" * 40)
        self.fake.check_runs_status = 500
        self.run_pass()
        data = self.github_data()
        self.assertIn("- GitHub could not be read (status 500); pull request data may be "
                      "missing.", data)
        self.assertNotIn("Pull request #77", data)
        self.assertIn("Issue: %s#2\n" % REPO, self.text())

    def test_a_ticket_is_verified_at_most_once_per_six_hours(self):
        self.run_pass()
        os.remove(os.path.join(self.inbox(), self.briefs()[0]))
        self.run_pass()
        self.assertIn("Issue: %s#1\n" % REPO, self.text())
        os.remove(os.path.join(self.inbox(), self.briefs()[0]))
        self.run_pass()
        self.assertEqual(self.briefs(), [])

    def test_a_ticket_with_a_proof_is_not_due(self):
        self.fake.comments[2] = [{"body": proof.PROOF_MARKER + "\n```json\n{}\n```",
                                  "user": {"login": "alice-example"}}]
        self.run_pass()
        self.assertIn("Issue: %s#1\n" % REPO, self.text())

    def failed_audit(self, lane, hours_ago, login="alice-example"):
        """A closed ticket whose lane proof failed its audit hours_ago."""
        self.fake.add(9, lane=lane, state="shipped")
        self.fake.issues[9]["state"] = "closed"
        self.fake.issues[9]["labels"].append({"name": audit.LABEL})
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - hours_ago * HOUR))
        self.fake.comments[9] = [{"body": audit.marker("fail", lane) + "\nAudit failed.",
                                  "user": {"login": login}, "created_at": stamp}]

    def test_a_failed_audit_holds_the_lane_from_verify_runs_for_a_day(self):
        self.failed_audit("codex", 2)
        self.run_pass()
        self.assertEqual(self.briefs(), [])

    def test_the_hold_ends_after_a_day_and_only_a_trusted_audit_holds(self):
        for hours, login in ((25, "alice-example"), (2, "someone-else")):
            with self.subTest(hours=hours, login=login):
                self.failed_audit("codex", hours, login)
                self.run_pass()
                self.assertIn("Issue: %s#2\n" % REPO, self.text())
                os.remove(os.path.join(self.inbox(), self.briefs()[0]))
                os.remove(os.path.join(self.home, "state", "station", station.VERIFY_FILE))

    def test_a_ticket_whose_proof_failed_its_audit_is_due_again(self):
        self.fake.comments[2] = [
            {"body": proof.PROOF_MARKER + "\n```json\n{}\n```", "user": {"login": "alice-example"}},
            {"body": audit.marker("fail", "zen"), "user": {"login": "alice-example"}}]
        self.run_pass()
        self.assertIn("Issue: %s#2\n" % REPO, self.text())

    def test_an_anthropic_slot_is_never_handed_a_verify_brief(self):
        self.heartbeat("codex", available=False)
        self.heartbeat("anthropic")
        self.heartbeat("claude")
        self.run_pass()
        self.assertEqual(self.briefs("anthropic"), [])
        self.assertEqual(self.briefs("claude"), [])

    def test_build_work_for_the_slot_comes_first(self):
        self.fake.add(3, lane="codex", state="briefed")
        self.run_pass()
        self.assertIn("Issue: %s#3\n" % REPO, self.text())
        self.assertEqual(len(self.briefs()), 1)

    def test_a_queued_ticket_of_the_slots_tier_holds_the_slot(self):
        self.fake.add(3, lane="codex", state="queued", body="Difficulty: medium\n\nBody\n")
        self.run_pass()
        self.assertEqual(self.briefs(), [])

    def test_a_private_repo_is_not_verified_on_a_free_model_lane(self):
        self.fake.private = True
        self.heartbeat("codex", available=False)
        self.heartbeat("zen")
        self.run_pass()
        self.assertEqual(self.briefs("zen"), [])


class VerifyResultTest(Base):
    def setUp(self):
        super().setUp()
        self.fake.add(5, lane="codex", body=SHIPPED_BODY, state="shipped",
                      events=shipped_at(30))
        self.name = "example-owner.example-repo-5-verify-1"
        brief = "# Brief: %s\n\nRepo: %s\nBranch: loom/verify-5\nLane: codex\nIssue: %s#5\n" \
                "Deliverable: verify\n\n# T5: Fix the thing\n" % (self.name, REPO, REPO)
        self.write(os.path.join(self.inbox("codex", "done"), self.name + ".md"), brief)

    def result(self, data):
        text = data if isinstance(data, str) else json.dumps(data)
        self.write(os.path.join(self.home, "logs", "codex", self.name + ".verify.json"), text)

    def good(self, **changes):
        out = {"kind": "docs", "revision": "abc1234",
               "observed": {"start": "2030-01-01T00:00:00Z", "end": "2030-01-01T01:00:00Z"},
               "sources": ["https://example.com/logs/1"],
               "criteria": [{"criterion": "A test passes", "result": "pass", "evidence": "ci"},
                            {"criterion": "Live: the daily line shows it", "result": "pass",
                             "evidence": "daily line seen at 2026-10-15T14:11Z"}]}
        out.update(changes)
        return out

    def verify_failed(self, reason, name=None, now=None):
        name = name or self.name
        self.write(os.path.join(self.home, "logs", "codex", name + ".log"),
                   "[t] %s start, lane codex\n[t] %s failed: %s\n" % (name, name, reason))
        st = station.Station(GitHub("token", transport=self.fake), self.settings,
                             now=now or time.time())
        st._report_verify("codex", "failed", REPO, 5, name)
        return st

    def verify_runs(self):
        with open(os.path.join(self.home, "state", "station", station.VERIFY_FILE)) as fh:
            return json.load(fh)

    def test_a_transient_verify_failure_makes_its_ticket_due_again_now(self):
        self.write(os.path.join(self.home, "state", "station", station.VERIFY_FILE),
                   json.dumps({"%s#5" % REPO: time.time() - 60}))
        self.verify_failed("[transient] agent exited 1")
        self.assertNotIn("%s#5" % REPO, self.verify_runs())

    def test_a_verify_run_that_failed_on_the_work_keeps_its_cooldown(self):
        self.write(os.path.join(self.home, "state", "station", station.VERIFY_FILE),
                   json.dumps({"%s#5" % REPO: time.time() - 60}))
        self.verify_failed("[work] no verify.json written")
        self.assertIn("%s#5" % REPO, self.verify_runs())

    def test_two_transient_verify_failures_in_a_row_hold_the_lane_for_an_hour(self):
        now = time.time()
        self.verify_failed("[transient] agent exited 1", name="a-verify-1", now=now)
        st = self.verify_failed("[transient] agent exited 124", name="a-verify-2", now=now)
        self.assertTrue(st._verify_lane_held("codex"))
        self.assertFalse(st._verify_lane_held("zen"))
        later = station.Station(GitHub("token", transport=self.fake), self.settings,
                                now=now + station.VERIFY_LANE_HOLD + 1)
        self.assertFalse(later._verify_lane_held("codex"))

    def test_a_valid_proof_ticks_the_criteria_and_verifies_through_proof_py(self):
        self.result(self.good())
        self.run_pass()
        self.assertIn("loom:verified", self.fake.names(5))
        self.assertNotIn("loom:shipped", self.fake.names(5))
        self.assertEqual(len(proof.criteria(self.fake.issues[5]["body"])), 2)
        self.assertTrue(all(done for done, _ in proof.criteria(self.fake.issues[5]["body"])))
        comments = [c["body"] for c in self.fake.comments[5]]
        self.assertTrue(any(c.startswith(proof.PROOF_MARKER) for c in comments))

    def test_a_valid_proof_verifies_and_closes_the_ticket(self):
        self.result(self.good())
        self.run_pass()
        self.assertIn("loom:verified", self.fake.names(5))
        self.assertNotIn("loom:shipped", self.fake.names(5))
        self.assertEqual(self.fake.issues[5]["state"], "closed")
        self.assertEqual(self.fake.issues[5].get("state_reason"), "completed")

    def test_a_verified_ticket_with_an_unticked_box_is_not_closed(self):
        self.result(self.good())
        original_move = brain.move

        def move_then_untick(gh, repo, number, state, **kwargs):
            original_move(gh, repo, number, state, **kwargs)
            body = self.fake.issues[number]["body"]
            self.fake.issues[number]["body"] = body + "\n- [ ] A late criterion"

        with mock.patch.object(brain, "move", side_effect=move_then_untick):
            with mock.patch("bus.station._say") as say:
                self.run_pass()
        self.assertIn("loom:verified", self.fake.names(5))
        self.assertNotIn("loom:shipped", self.fake.names(5))
        self.assertEqual(self.fake.issues[5]["state"], "open")
        logged = " ".join(str(c) for c in say.call_args_list)
        self.assertIn("A late criterion", logged)
        self.assertIn("verified but not closed", logged)

    def test_a_github_failure_on_close_is_logged_and_the_ticket_stays_verified(self):
        self.result(self.good())
        err = station.GitHubError("server error", 502)
        with mock.patch.object(brain, "close", side_effect=err):
            with mock.patch("bus.station._say") as say:
                self.run_pass()
        self.assertIn("loom:verified", self.fake.names(5))
        self.assertEqual(self.fake.issues[5]["state"], "open")
        self.assertIn("verified but not closed", " ".join(str(c) for c in say.call_args_list))

    def proofs(self):
        return [c["body"] for c in self.fake.comments[5] if c["body"].startswith(proof.PROOF_MARKER)]

    def test_a_lane_proof_names_the_lane_and_model_that_made_it(self):
        # The lane cannot pass its proof off as the brain's.
        self.result(self.good(verified_by="brain"))
        self.write(os.path.join(self.home, "logs", "codex", self.name + ".verify.model"), "model-x\n")
        self.run_pass()
        self.assertIn("loom:verified", self.fake.names(5))
        self.assertEqual(proof.read(self.proofs()[0])["verified_by"], "codex/model-x")
        self.assertIn("Verified by: `codex/model-x`", self.proofs()[0])

    def test_a_run_that_recorded_no_model_still_names_its_lane(self):
        self.result(self.good())
        self.run_pass()
        self.assertEqual(proof.read(self.proofs()[0])["verified_by"], "codex/unknown")

    def test_an_invalid_proof_leaves_the_ticket_shipped_with_no_comment(self):
        self.result(self.good(criteria=[]))
        self.run_pass()
        self.run_pass()
        self.assertIn("loom:shipped", self.fake.names(5))
        self.assertEqual(self.fake.issues[5]["body"], SHIPPED_BODY)
        self.assertEqual(self.fake.comments[5], [])  # a log line, not an email (2026-10-18)

    def test_a_missing_result_names_the_missing_evidence_in_the_log(self):
        self.result({"missing": "the deployed station log for 2026-10-16"})
        # Handing the run out recorded the attempt, so the cooldown holds the
        # ticket back from a second run in this pass.
        self.write(os.path.join(self.home, "state", "station", station.VERIFY_FILE),
                   json.dumps({"%s#5" % REPO: time.time()}))
        self.run_pass()
        # A verifier found the live evidence has not happened yet: the ticket is
        # labelled so it stops holding the unproven-work limit (2026-10-17: 54 of
        # 58 counted tickets were waiting on the world, and the goal was frozen).
        self.assertIn("loom:awaiting-evidence", self.fake.names(5))
        self.run_pass()
        self.assertIn("loom:shipped", self.fake.names(5))
        self.assertEqual(self.fake.comments[5], [])  # a log line, not an email (2026-10-18)

    def test_a_bare_word_of_missing_evidence_does_not_earn_the_label(self):
        # The result is lane output: "missing" alone must not switch the limit off.
        self.result({"missing": "later"})
        self.run_pass()
        self.assertNotIn("loom:awaiting-evidence", self.fake.names(5))
        self.assertEqual(self.fake.comments[5], [])  # a log line, not an email (2026-10-18)

    def test_a_retry_handed_out_drops_the_label_so_the_run_decides_afresh(self):
        self.fake.issues[5]["labels"].append({"name": "loom:awaiting-evidence"})
        # An idle slot with the cooldown over is handed the ticket again.
        station.Station(GitHub("token", transport=self.fake), self.settings,
                        now=time.time())._write_verify_brief(REPO, "codex", self.fake.issues[5])
        self.assertNotIn("loom:awaiting-evidence", self.fake.names(5))
        self.assertIn("loom:shipped", self.fake.names(5))

    def test_a_refused_proof_or_a_crashed_run_is_not_awaiting_evidence(self):
        self.result(self.good(criteria=[]))
        self.run_pass()
        self.assertNotIn("loom:awaiting-evidence", self.fake.names(5))

    def test_a_proof_holding_a_credential_is_refused_with_no_comment(self):
        self.result(self.good(sources=["token=abcdef0123456789"]))
        self.run_pass()
        self.assertIn("loom:shipped", self.fake.names(5))
        self.assertEqual(self.fake.issues[5]["body"], SHIPPED_BODY)
        self.assertEqual(self.fake.comments[5], [])  # a log line, not an email (2026-10-18)

    def test_no_verify_json_leaves_it_shipped_with_no_comment(self):
        self.run_pass()
        self.assertIn("loom:shipped", self.fake.names(5))
        self.assertEqual(self.fake.comments[5], [])  # a log line, not an email (2026-10-18)


class UnseenLiveEvidenceTest(unittest.TestCase):
    """A live criterion passes only on something seen, with its time
    (2026-10-15: a lane passed one on what the logs 'would show')."""

    def record(self, evidence, criterion="Live proof: on the first busy evening, the line appears"):
        return {"criteria": [
            {"criterion": "A test fails without the change", "result": "pass",
             "evidence": "tests/test_x.py would fail on main"},
            {"criterion": criterion, "result": "pass", "evidence": evidence}]}

    def test_a_prediction_is_refused(self):
        why = station.unseen_live_evidence(self.record(
            "Server logs would show 'lowest free memory' before each batch run."))
        self.assertIn("would", why)

    def test_evidence_without_a_time_is_refused(self):
        why = station.unseen_live_evidence(self.record("The line appears in the log."))
        self.assertIn("names no time", why)

    def test_an_observed_line_with_its_time_passes(self):
        self.assertEqual(station.unseen_live_evidence(self.record(
            "server log 2026-10-15T21:04:11Z: 'lowest free memory 64%' before batch 8")), "")

    def test_non_live_criteria_are_not_judged(self):
        self.assertEqual(station.unseen_live_evidence(
            {"criteria": [{"criterion": "A test fails without it", "evidence": "it would fail"}]}), "")


class UnprovenGateTest(unittest.TestCase):
    """loom#483: a goal with too much unproven work gets no new build claim
    from the station. Unproven = open loom:ticket at shipped (a
    verified ticket carries loom:verified instead, so it never counts)."""

    GOAL = 40

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = os.path.join(self._tmp.name, "loom")
        self.fake = FakeGitHub()
        self.settings = station.Settings(self.home, [REPO], list(AUTHORS), list(OWNERS))
        d = os.path.join(self.home, "state", "usage")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "codex.json"), "w") as fh:
            json.dump({"lane": "codex", "at": int(time.time()), "available": True, "reason": ""}, fh)
        self.fake.issues[self.GOAL] = {"number": self.GOAL, "title": "Goal", "state": "open",
                                       "labels": [{"name": "loom:goal"}]}
        self.fake.comments[self.GOAL] = []
        self.fake.events[self.GOAL] = []

    def gh(self):
        return GitHub("token", transport=self.fake)

    def child(self, number, state="shipped"):
        issue = {"number": number, "title": "Done %d" % number, "state": "open",
                 "labels": [{"name": "loom:ticket"}, {"name": "loom:" + state}],
                 "body": "Done.\n\nPart of #%d\n" % self.GOAL}
        self.fake.issues[number] = issue
        self.fake.comments[number] = []
        self.fake.events[number] = []
        return issue

    def over_limit(self, count=None):
        count = brain.DEFAULT_UNVERIFIED_LIMIT + 1 if count is None else count
        for n in range(101, 101 + count):
            self.child(n)
        self.fake.sub_issues[self.GOAL] = [self.fake.issues[n] for n in range(101, 101 + count)]

    def brief(self, number, *extra_labels):
        self.fake.add(number, state="briefed", lane="codex",
                      body=BODY + "\nPart of #%d\n" % self.GOAL)
        self.fake.issues[number]["labels"] += [{"name": label} for label in extra_labels]

    def run_pass(self):
        return station.run(self.settings, self.gh())

    def test_a_goal_over_the_limit_gets_no_new_build_claim(self):
        self.over_limit()
        self.brief(7)
        self.assertEqual(self.run_pass(), 0)
        self.assertIn("loom:briefed", self.fake.names(7))
        self.assertNotIn("loom:running", self.fake.names(7))

    def test_a_gated_ticket_carries_wait_gate_once_and_no_comment(self):
        self.over_limit()
        self.brief(7)
        self.run_pass()
        self.assertIn("wait:gate", self.fake.names(7))
        self.assertEqual(self.fake.comments[7], [])
        puts = self.fake.paths("PUT").count("/repos/%s/issues/7/labels" % REPO)
        self.run_pass()
        self.assertEqual(self.fake.paths("PUT").count("/repos/%s/issues/7/labels" % REPO), puts)

    def test_regression_revert_and_urgent_tickets_still_go_over_the_limit(self):
        for number, label in ((8, "loom:regression"), (9, "loom:revert"), (10, "loom:urgent")):
            with self.subTest(label=label):
                self.setUp()
                self.over_limit()
                self.brief(number, label)
                self.assertEqual(self.run_pass(), 0)
                self.assertIn("loom:running", self.fake.names(number))

    def test_a_goal_at_the_limit_is_still_claimed(self):
        self.over_limit(15)
        self.brief(7)
        self.assertEqual(self.run_pass(), 0)
        self.assertIn("loom:running", self.fake.names(7))

    def test_a_ticket_with_no_goal_is_never_gated(self):
        self.over_limit()
        self.fake.add(7, state="briefed", lane="codex", body=BODY)
        self.assertEqual(self.run_pass(), 0)
        self.assertIn("loom:running", self.fake.names(7))

    def test_a_gated_lane_is_left_idle_for_verify_runs(self):
        self.over_limit()
        self.brief(7)
        with mock.patch.object(station.Station, "_hand_verifies", autospec=True) as handed:
            self.assertEqual(self.run_pass(), 0)
        self.assertIn("codex", handed.call_args[0][1])

    def test_a_queued_ticket_the_gate_refuses_does_not_hold_its_lanes_from_verify(self):
        # 2026-10-18: a queued hard ticket under an over-limit goal named
        # every lane of its tier as wanted for build work, so those lanes
        # took no verify run either and the goal's shipped work never shrank.
        self.over_limit()
        for n in self.fake.sub_issues[self.GOAL]:
            n["user"] = {"login": "alice-example"}
        self.fake.issues[7] = {
            "number": 7, "title": "Fix the thing", "state": "open",
            "body": BODY + "\nPart of #%d\n" % self.GOAL,
            "user": {"login": "alice-example"}, "created_at": "2030-01-01T00:00:00Z",
            "updated_at": "2030-01-01T00:00:00Z",
            "labels": [{"name": "loom:ticket"}, {"name": "loom:queued"}]}
        self.fake.comments[7] = []
        self.fake.events[7] = []
        st = station.Station(self.gh(), self.settings)
        st.claim_new()
        self.assertIn("loom:queued", self.fake.names(7))
        inbox = st.inbox("codex")
        names = os.listdir(inbox) if os.path.isdir(inbox) else []
        self.assertTrue(any("-verify-" in n for n in names), names)

    def epic(self, number, goal=None):
        self.fake.issues[number] = {"number": number, "title": "Epic: a part of the goal",
                                    "state": "open", "labels": [{"name": "epic"}],
                                    "body": "Done when: it works.\n\nPart of #%d\n" % (goal or self.GOAL)}
        self.fake.comments[number] = []
        self.fake.events[number] = []

    def brief_under(self, number, parent):
        self.fake.add(number, state="briefed", lane="codex", body=BODY)
        self.fake.issues[number]["parent_issue_url"] = (
            "https://api.github.com/repos/%s/issues/%d" % (REPO, parent))

    def test_a_ticket_under_an_epic_is_gated_by_the_epics_goal(self):
        # 2026-10-17: a ticket under an epic handed the epic's number to the
        # unproven-work check, which raised and ended every station pass.
        self.over_limit()
        self.epic(50)
        self.brief_under(7, 50)
        self.assertEqual(self.run_pass(), 0)
        self.assertIn("loom:briefed", self.fake.names(7))
        self.assertNotIn("loom:running", self.fake.names(7))

    def test_a_ticket_under_an_epic_of_a_goal_under_the_limit_is_claimed(self):
        self.epic(50)
        self.brief_under(7, 50)
        self.assertEqual(self.run_pass(), 0)
        self.assertIn("loom:running", self.fake.names(7))

    def test_a_ticket_under_a_plain_issue_is_not_gated_and_the_pass_completes(self):
        self.over_limit()
        self.fake.issues[60] = {"number": 60, "title": "A note", "state": "open",
                                "labels": [], "body": "Just a note."}
        self.fake.comments[60] = []
        self.fake.events[60] = []
        self.brief_under(7, 60)
        self.assertEqual(self.run_pass(), 0)
        self.assertIn("loom:running", self.fake.names(7))

    # loom#767: factory-repair tickets (Priority: P0, or under an epic named
    # in LOOM_REPAIR_EPICS) pass the limit, at most two in flight at once.
    def repair_epic(self, number=50):
        patcher = mock.patch.dict(os.environ, {"LOOM_REPAIR_EPICS": "%s#%d" % (REPO, number)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.epic(number)

    def running_repair(self, number):
        issue = self.child(number, state="running")
        issue["body"] = "Priority: P0\n\n## Why\nx\n\nPart of #%d\n" % self.GOAL
        self.fake.sub_issues[self.GOAL].append(issue)

    def test_a_repair_epic_child_over_the_limit_is_claimed(self):
        self.over_limit()
        self.repair_epic(50)
        self.brief_under(7, 50)
        self.assertEqual(self.run_pass(), 0)
        self.assertIn("loom:running", self.fake.names(7))

    def test_a_p0_ticket_over_the_limit_is_claimed(self):
        self.over_limit()
        self.fake.add(7, state="briefed", lane="codex",
                      body="Priority: P0\n" + BODY + "\nPart of #%d\n" % self.GOAL)
        self.assertEqual(self.run_pass(), 0)
        self.assertIn("loom:running", self.fake.names(7))

    def test_a_repair_ticket_goes_while_one_other_is_in_flight(self):
        self.over_limit()
        self.repair_epic(50)
        self.running_repair(201)
        self.brief_under(7, 50)
        self.assertEqual(self.run_pass(), 0)
        self.assertIn("loom:running", self.fake.names(7))

    def test_a_third_repair_ticket_waits_while_two_are_in_flight(self):
        self.over_limit()
        self.repair_epic(50)
        self.running_repair(201)
        self.running_repair(202)
        self.brief_under(7, 50)
        self.assertEqual(self.run_pass(), 0)
        self.assertIn("loom:briefed", self.fake.names(7))
        self.assertNotIn("loom:running", self.fake.names(7))

    def test_a_ticket_under_another_epic_still_waits(self):
        self.over_limit()
        self.repair_epic(50)
        self.epic(51)
        self.brief_under(7, 51)
        self.assertEqual(self.run_pass(), 0)
        self.assertNotIn("loom:running", self.fake.names(7))

    def test_ticket_goal_reads_the_part_of_line(self):
        self.assertEqual(station._ticket_goal({"body": "x\n\nPart of #40\n"}), 40)
        self.assertEqual(station._ticket_goal({"body": "x", "parent_issue_url":
                             "https://api.github.com/repos/o/r/issues/41"}), 41)
        self.assertIsNone(station._ticket_goal({"body": "x"}))


class AskDigestEpicTest(Base):
    """loom#640: an ask naming an epic is grouped under that epic's goal."""

    def test_an_ask_naming_an_epic_goes_under_the_epics_goal(self):
        self.fake.add_goal(30, title="Ship the factory")
        self.fake.epics = [{"number": 50, "title": "Epic: the work", "state": "open",
                            "labels": [{"name": "epic"}], "body": "Done when: x.\n\nPart of #30\n"}]
        asks = [{"number": 7, "title": "Pick a name", "body": "Part of #50."},
                {"number": 8, "title": "Loose ask", "body": "No parent."},
                {"number": 9, "title": "Direct ask", "body": "Part of #30."}]
        st = station.Station(GitHub("token", transport=self.fake), self.settings)
        page = st._page_body(REPO, asks, "2030-01-01")
        goal_part = page.split("## Ship the factory (#30)")[1].split("## Not filed")[0]
        self.assertIn("- #7 Pick a name", goal_part)
        self.assertIn("- #9 Direct ask", goal_part)
        self.assertIn("- #8 Loose ask", page.split("## Not filed under a goal")[1])


class TwoLoomsTest(Base):
    """An issue or goal another operator owns is left alone (loom#649)."""

    OTHER = "carol-example"

    def run_quiet(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.run_pass()
        return err.getvalue()

    def assign(self, number, *logins):
        self.fake.issues[number]["assignees"] = [{"login": x} for x in logins]

    def test_a_ticket_assigned_to_another_operator_is_not_claimed(self):
        self.fake.add(12)
        self.assign(12, self.OTHER)
        err = self.run_quiet()
        self.assertEqual(self.listing(), [])
        self.assertEqual(self.fake.names(12), ["loom:ticket", "loom:briefed", "lane:codex"])
        self.assertEqual(self.fake.comments[12], [])
        self.assertNotIn("/repos/%s/issues/12/assignees" % REPO, self.fake.paths("POST"))
        self.assertIn("leaving %s#12 alone: assigned to %s" % (REPO, self.OTHER), err)
        # The skip is logged once, not every pass.
        self.assertNotIn("leaving %s#12 alone" % REPO, self.run_quiet())

    def test_a_ticket_assigned_to_one_of_our_logins_is_claimed(self):
        self.fake.add(12)
        self.assign(12, "bob-example")
        self.run_quiet()
        self.assertEqual(self.listing(), ["example-owner.example-repo-12.md"])
        self.assertIn("loom:running", self.fake.names(12))

    def test_a_claim_marks_the_operator_with_a_label_not_an_assignment(self):
        # 2026-10-18: an assignee is emailed for every comment on the ticket;
        # the claim label already tells another operator's Loom it is taken.
        self.settings.authors = ["loom-app[bot]"] + list(AUTHORS)
        self.fake.add(12)
        self.run_quiet()
        self.assertEqual(self.listing(), ["example-owner.example-repo-12.md"])
        self.assertEqual(tickets.assignees(self.fake.issues[12]), [])
        self.assertIn("claim:alice-example", self.fake.names(12))
        self.assertFalse(any("/assignees" in p for p in self.fake.paths("POST")))

    def test_a_claim_adds_and_creates_the_operator_claim_label(self):
        self.fake.add(12)
        self.run_quiet()
        self.assertIn("claim:alice-example", self.fake.names(12))
        self.assertEqual(self.fake.made_labels, ["claim:alice-example"])

    def test_a_claim_label_for_another_login_is_skipped_like_an_assignment(self):
        self.settings.factory_on = True
        self.fake.add(12)
        self.fake.issues[12]["labels"].append({"name": "claim:" + self.OTHER})
        self.fake.add(14, state="queued")
        self.fake.issues[14]["labels"].append({"name": "claim:" + self.OTHER})
        err = self.run_quiet()
        self.assertEqual(self.listing(), [])
        self.assertIn("loom:briefed", self.fake.names(12))
        self.assertIn("loom:queued", self.fake.names(14))
        self.assertIn("leaving %s#12 alone: assigned to %s" % (REPO, self.OTHER), err)

    def test_a_claim_label_for_our_own_login_is_claimed(self):
        self.fake.add(12)
        self.fake.issues[12]["labels"].append({"name": "claim:Bob-Example"})
        self.run_quiet()
        self.assertEqual(self.listing(), ["example-owner.example-repo-12.md"])

    def test_triage_adoption_and_rewrite_skip_a_claim_label_for_another_login(self):
        from bus import rewrite
        self.fake.add_goal(30, title="Backlog: every open issue in %s" % REPO)
        self.fake.add_open_issue(100, body=READY_BODY)
        self.fake.open_issues[0]["labels"] = [{"name": "claim:" + self.OTHER}]
        self.fake.issues[100]["labels"] = [{"name": "claim:" + self.OTHER}]
        self.fake.comments.setdefault(99, [])
        self.fake.pulls.append({
            "number": 99, "state": "open", "title": "Tidy the docs",
            "user": {"login": "alice-example"}, "body": "", "draft": False,
            "created_at": "2030-01-01T00:00:00Z", "labels": [{"name": "claim:" + self.OTHER}],
            "head": {"ref": "fix/stray-files", "repo": {"full_name": REPO}},
            "base": {"ref": "main", "repo": {"full_name": REPO}}})
        with mock.patch.dict(os.environ, {"LOOM_BRIEF_AUTHORS": " ".join(AUTHORS)}):
            self.run_quiet()
            gh = mock.Mock()
            gh.paginate.return_value = [
                {"number": 1, "labels": [{"name": "claim:" + self.OTHER}]},
                {"number": 2, "labels": [{"name": "claim:alice-example"}]}]
            self.assertEqual([i["number"] for i in rewrite.candidates(gh, REPO)], [2])
        self.assertEqual(self.fake.names(100), ["claim:" + self.OTHER])
        self.assertEqual([c for c in self.fake.created
                          if c["title"].startswith("Land or supersede #")], [])

    def test_a_queued_ticket_assigned_elsewhere_gets_no_lane(self):
        self.settings.factory_on = True
        self.fake.add(12, state="queued")
        self.assign(12, self.OTHER)
        self.fake.add(13, state="queued")
        self.run_quiet()
        self.assertIn("loom:queued", self.fake.names(12))
        self.assertNotIn("loom:queued", self.fake.names(13))

    def test_triage_skips_an_issue_assigned_elsewhere(self):
        self.fake.add_goal(30, title="Backlog: every open issue in %s" % REPO)
        self.fake.add_open_issue(100, body=READY_BODY)
        self.fake.add_open_issue(101, body="It is slow.")
        self.fake.open_issues[0]["assignees"] = [{"login": self.OTHER}]
        self.fake.open_issues[1]["assignees"] = [{"login": self.OTHER}]
        with mock.patch.dict(os.environ, {"LOOM_BRIEF_AUTHORS": " ".join(AUTHORS)}):
            self.run_quiet()
        self.assertEqual(self.fake.names(100), [])
        self.assertEqual(self.fake.names(101), [])
        self.assertEqual(self.fake.comments[101], [])

    def test_adoption_skips_a_pull_request_assigned_elsewhere(self):
        self.fake.add_goal(30)
        self.fake.comments.setdefault(99, [])
        self.fake.pulls.append({
            "number": 99, "state": "open", "title": "Tidy the docs",
            "user": {"login": "alice-example"}, "body": "", "draft": False,
            "created_at": "2030-01-01T00:00:00Z", "assignees": [{"login": self.OTHER}],
            "head": {"ref": "fix/stray-files", "repo": {"full_name": REPO}},
            "base": {"ref": "main", "repo": {"full_name": REPO}}})
        self.run_quiet()
        self.assertEqual([c for c in self.fake.created
                          if c["title"].startswith("Land or supersede #")], [])

    def test_the_verify_picker_skips_a_shipped_ticket_assigned_elsewhere(self):
        self.fake.private = False
        self.fake.add(2, lane="codex", body=SHIPPED_BODY, state="shipped", events=shipped_at(30))
        self.assign(2, self.OTHER)
        self.run_quiet()
        self.assertEqual([n for n in self.listing() if n.endswith(".md")], [])

    def test_a_goal_home_naming_another_operator_is_not_worked(self):
        self.fake.add_goal(30, title="Backlog: every open issue in %s" % REPO)
        self.fake.goals[0]["body"] = "Operator: %s\n\n## Destination\nAll of it." % self.OTHER
        self.fake.add_open_issue(100, body=READY_BODY)
        err = self.run_quiet()
        self.assertEqual(self.fake.names(100), [])
        self.assertIn("leaving %s#30 alone: goal home names operator %s" % (REPO, self.OTHER),
                      err)

    def test_a_goal_home_naming_our_operator_is_worked(self):
        self.fake.add_goal(30, title="Backlog: every open issue in %s" % REPO)
        self.fake.goals[0]["body"] = "Operator: Alice-Example\n\n## Destination\nAll of it."
        self.fake.add_open_issue(100, body=READY_BODY)
        self.run_quiet()
        self.assertIn("loom:queued", self.fake.names(100))

    def test_a_ticket_under_another_operators_goal_is_not_claimed(self):
        self.fake.add_goal(30)
        self.fake.issues[30]["body"] = "Operator: %s\n\n## Destination\nAll." % self.OTHER
        self.fake.add(12, body=BODY + "\nPart of #30\n")
        err = self.run_quiet()
        self.assertEqual(self.listing(), [])
        self.assertIn("leaving %s#12 alone: its goal #30 names operator %s" % (REPO, self.OTHER),
                      err)
        self.fake.issues[30]["body"] = "Operator: alice-example\n\n## Destination\nAll."
        self.fake.issues[12]["updated_at"] = "2030-01-02T00:00:00Z"
        self.fake.version += 1
        self.run_quiet()
        self.assertEqual(self.listing(), ["example-owner.example-repo-12.md"])


class OwnershipHelpersTest(unittest.TestCase):
    def test_owned_elsewhere(self):
        self.assertIsNone(tickets.owned_elsewhere({}, AUTHORS))
        self.assertIsNone(tickets.owned_elsewhere(
            {"assignees": [{"login": "Bob-Example"}, {"login": "carol-example"}]}, AUTHORS))
        self.assertEqual(tickets.owned_elsewhere(
            {"assignees": [{"login": "carol-example"}]}, AUTHORS), "carol-example")
        self.assertEqual(tickets.owned_elsewhere(
            {"assignee": {"login": "carol-example"}}, AUTHORS), "carol-example")

    def test_goal_operator_reads_only_the_header(self):
        self.assertEqual(tickets.goal_operator({"body": "Trunk: side\nOperator: Carol-Example\n"}),
                         "carol-example")
        self.assertIsNone(tickets.goal_operator({"body": "## Rules\nOperator: carol-example\n"}))
        self.assertTrue(tickets.goal_ours({"body": "## Destination\n"}, AUTHORS))
        self.assertFalse(tickets.goal_ours({"body": "Operator: carol-example\n"}, AUTHORS))

    def test_operator_login_skips_apps(self):
        self.assertEqual(tickets.operator_login(["loom-app[bot]", "alice-example"]),
                         "alice-example")
        self.assertIsNone(tickets.operator_login([]))

    def test_move_refuses_another_operators_ticket(self):
        fake = FakeGitHub()
        fake.add(12, state="queued")
        fake.issues[12]["assignees"] = [{"login": "carol-example"}]
        gh = GitHub("token", transport=fake)
        with mock.patch.dict(os.environ, {"LOOM_BRIEF_AUTHORS": " ".join(AUTHORS)}):
            with self.assertRaisesRegex(ValueError, "assigned to carol-example"):
                brain.move(gh, REPO, 12, "briefed", lane="codex")
        self.assertIn("loom:queued", fake.names(12))

    def test_rewrite_candidates_skip_another_operators_issue(self):
        from bus import rewrite
        fake = mock.Mock()
        fake.paginate.return_value = [
            {"number": 1, "assignees": [{"login": "carol-example"}]},
            {"number": 2, "assignees": []}]
        with mock.patch.dict(os.environ, {"LOOM_BRIEF_AUTHORS": " ".join(AUTHORS)}):
            self.assertEqual([i["number"] for i in rewrite.candidates(fake, REPO)], [2])


class AppLoginTrustTest(Base):
    """The Loom App's own bot login is trusted without listing it (loom#660)."""

    def setUp(self):
        super().setUp()
        self.settings.authors = ["alice-example"]

    def app_github(self):
        def transport(method, url, headers, body):
            path = urlsplit(url).path
            if method == "POST" and path.endswith("/access_tokens"):
                return reply(201, {"token": "t", "expires_at": "2099-01-01T00:00:00Z"})
            if method == "GET" and path == "/app":
                return reply(200, {"slug": "loom-app"})
            return self.fake(method, url, headers, body)
        app = AppToken("1", "2", lambda: "key", sign=lambda data, key: b"sig")
        return GitHub(app, transport=transport)

    def brief_by(self, number, labeller):
        self.fake.add(number, events=[labeled("loom:briefed", labeller),
                                      labeled("lane:codex", labeller)])

    def test_a_ticket_briefed_by_the_apps_bot_login_is_claimed(self):
        self.brief_by(12, "loom-app[bot]")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(station.run(self.settings, self.app_github()), 0)
        self.assertEqual(self.listing(), ["example-owner.example-repo-12.md"])
        self.assertIn("trusting briefing logins: alice-example loom-app[bot]", err.getvalue())

    def test_without_app_settings_the_bot_login_is_not_trusted(self):
        self.brief_by(12, "loom-app[bot]")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.run_pass()
        self.assertEqual(self.listing(), [])
        self.assertIn("trusting briefing logins: alice-example\n", err.getvalue())

    def test_refusals_for_an_untrusted_labeler_are_counted_per_login(self):
        for number in (12, 13):
            self.brief_by(number, "stranger[bot]")
        with contextlib.redirect_stderr(io.StringIO()):
            self.run_pass()
        path = os.path.join(self.home, "state", "station", "labeler_refusals.json")
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(len(json.load(fh)["stranger[bot]"]), 2)


class BudgetExhaustedTest(unittest.TestCase):
    """loom#666: a 403 that says the hourly budget is spent is not a missing
    grant. The pass stops in one line, the next pass waits for the reset
    without a request, and nothing logs a per-repo 403."""

    RESET = int(time.time()) + 1800

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = os.path.join(self._tmp.name, "loom")
        self.settings = station.Settings(self.home, [REPO, "example-owner/other"],
                                         list(AUTHORS), list(OWNERS))
        d = os.path.join(self.home, "state", "usage")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "codex.json"), "w") as fh:
            json.dump({"lane": "codex", "at": int(time.time()), "available": True,
                       "reason": ""}, fh)
        self.calls = []

    def refused(self, headers, body):
        def transport(method, url, headers_in, payload):
            self.calls.append((method, url))
            return 403, dict(headers), json.dumps(body).encode()
        return GitHub("token", transport=transport)

    def spent(self):
        # Recorded from the station's 2026-10-18 pass at 0 of 5000.
        return self.refused(
            {"X-RateLimit-Limit": "5000", "X-RateLimit-Remaining": "0",
             "X-RateLimit-Reset": str(self.RESET), "X-RateLimit-Resource": "core"},
            {"message": "API rate limit exceeded for installation ID 172475698.",
             "documentation_url": "https://docs.github.com/rest/overview/rate-limits-for-the-rest-api",
             "status": "403"})

    def run_pass(self, gh):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = station.run(self.settings, gh)
        return code, out.getvalue()

    def test_a_spent_budget_stops_the_pass_in_one_line(self):
        code, said = self.run_pass(self.spent())
        self.assertEqual(code, 0)
        self.assertIn("GitHub budget exhausted until", said)
        self.assertNotIn("could not list", said)
        self.assertEqual(said.count("GitHub budget exhausted"), 1, said)
        with open(os.path.join(self.home, "state", "station", "budget.json")) as fh:
            self.assertEqual(json.load(fh)["limited_until"], self.RESET)

    def test_the_next_pass_waits_for_the_reset_without_a_request(self):
        self.run_pass(self.spent())
        self.calls.clear()
        code, said = self.run_pass(self.spent())
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [])
        self.assertIn("this pass waits for the reset", said)

    def test_a_secondary_rate_limit_is_a_budget_refusal_too(self):
        gh = self.refused({"Retry-After": "120"},
                          {"message": "You have exceeded a secondary rate limit. "
                                      "Please wait a few minutes before you try again."})
        with self.assertRaises(github_module.RateLimited) as caught:
            gh.rest("GET", "/repos/%s/issues" % REPO)
        self.assertGreaterEqual(caught.exception.until, int(time.time()) + 119)
        self.calls.clear()
        with self.assertRaises(github_module.RateLimited):
            gh.rest("GET", "/repos/%s/pulls" % REPO)
        self.assertEqual(self.calls, [])

    def test_a_403_without_rate_limit_signs_is_still_a_missing_grant(self):
        gh = self.refused({"X-RateLimit-Limit": "5000", "X-RateLimit-Remaining": "4100",
                           "X-RateLimit-Reset": str(self.RESET)},
                          {"message": "Resource not accessible by integration"})
        with self.assertRaises(GitHubError) as caught:
            gh.rest("GET", "/repos/%s/issues" % REPO)
        self.assertNotIsInstance(caught.exception, github_module.RateLimited)
        self.assertIsNone(gh.limited_until)
