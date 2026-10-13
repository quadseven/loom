from __future__ import annotations

import calendar
import fcntl
import json
import os
import re
import sys
import tempfile
import time
import unittest
from unittest import mock
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import __main__ as cli  # noqa: E402
from bus import spend, station, tickets  # noqa: E402
from bus.github import GitHub  # noqa: E402

REPO = "example-owner/example-repo"
AUTHORS = ["alice-example", "bob-example"]
OWNERS = ["example-owner"]
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
        self.asks = []
        self.goals = []
        self.goal_error = False
        self.threads = {}
        self.reply_fails = set()
        self.replies = []
        self.resolved = []

    def add_goal(self, number, journals=()):
        """journals: (login, age in seconds) pairs, posted as journal comments."""
        self.goals.append({"number": number, "labels": [{"name": "loom:goal"}]})
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
                labeller="alice-example", urgent=False):
        labels = [{"name": "needs-operator"}] + ([{"name": "loom:urgent"}] if urgent else [])
        self.asks.append({"number": number, "title": title, "user": {"login": author},
                          "labels": labels, "created_at": "2030-01-01T00:00:00Z",
                          "html_url": "https://github.com/%s/issues/%d" % (REPO, number)})
        self.events[number] = [labeled("needs-operator", labeller)]

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
        self.calls.append((method, path))
        data = json.loads(body) if body else None
        if method == "POST" and parts.path == "/graphql":
            return self._graphql(data)
        rest = re.match(r"/repos/%s/(.*)" % REPO, parts.path).group(1)
        if rest == "issues" or re.match(r"issues/comments/\d+$", rest) \
                or (rest == "issues/1/comments" and self.lanes_issues):
            return self._issues_and_status(method, rest, parts.query, headers, data)
        m = re.match(r"issues/(\d+)(?:/(\w+))?$", rest)
        if m:
            return self._issue_call(method, int(m.group(1)), m.group(2), data)
        if method == "GET" and rest == "pulls":
            return reply(200, self.pulls)
        raise AssertionError("unexpected call %s %s" % (method, path))

    def _issues_and_status(self, method, rest, query, headers, data):
        if method == "GET" and rest == "issues":
            if "needs-operator" in query:
                return self._list_asks(headers)
            if "loom:goal" in query:
                if self.goal_error:
                    return reply(500, {"message": "down"})
                if self.goals:
                    return reply(200, self.goals)
                return reply(200, [dict(i, html_url="https://github.com/%s/issues/%d" % (REPO, i["number"]))
                                   for i in self.issues.values() if i["state"] == "open"
                                   and "loom:goal" in self.names(i["number"])])
            if "loom:lanes" in query:
                return reply(200, self.lanes_issues)
            return self._list_briefed(headers)
        if method == "POST" and rest == "issues":
            self.created.append(data)
            self.lanes_issues = [{"number": 1, "labels": [{"name": "loom:lanes"}]}]
            return reply(201, {"number": 1})
        if method == "PATCH":
            self.edits.append(data["body"])
            return reply(200, {})
        if method == "GET":
            return reply(200, self.status_comments)
        self.status_comments.append({"id": 77, "body": data["body"]})
        self.posts.append(data["body"])
        return reply(201, {"id": 77})

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
        return reply(200, {"data": {"repository": {"issue": {
            "editor": {"login": editor} if editor and editor != "<ghost>" else None,
            "lastEditedAt": "2030-01-01T00:05:00Z" if editor else None}}}})

    def _list_briefed(self, headers):
        etag = '"v%d"' % self.version
        if headers.get("If-None-Match") == etag:
            return reply(304, None, {"ETag": etag})
        listed = [dict(i, repository_url="https://api.github.com/repos/" + REPO)
                  for i in self.issues.values() if i["state"] == "open"
                  and any(lab["name"] == "loom:briefed" for lab in i["labels"])]
        return reply(200, listed, {"ETag": etag})

    def _issue_call(self, method, n, sub, data):
        if method == "GET":
            if sub == "events" and n not in self.issues:
                return reply(200, self.events[n])
            found = {None: self.issues, "events": self.events, "comments": self.comments}
            if sub in found:
                return reply(200, found[sub][n])
        if method == "POST" and sub == "comments":
            self.comments[n].append({"body": data["body"]})
            return reply(201, {})
        if method == "PUT" and sub == "labels":
            self.issues[n]["labels"] = [{"name": x} for x in data["labels"]]
            self.version += 1
            return reply(200, [])
        raise AssertionError("unexpected issue call %s %s/%s" % (method, n, sub))


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = os.path.join(self._tmp.name, "loom")
        self.fake = FakeGitHub()
        self.settings = station.Settings(self.home, [REPO], list(AUTHORS), list(OWNERS))
        self.heartbeat("codex")

    def heartbeat(self, lane, available=True, age=0):
        d = os.path.join(self.home, "state", "usage")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, lane + ".json"), "w") as fh:
            json.dump({"lane": lane, "at": int(time.time()) - age,
                       "available": available, "reason": ""}, fh)

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
        self.assertEqual(self.run_pass().requests, 0)


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
    def test_a_trusted_ticket_becomes_a_brief_and_is_claimed(self):
        self.fake.add(12)
        self.run_pass()
        name = "example-owner.example-repo-12"
        self.assertEqual(self.listing(), [name + ".md"])
        with open(self.inbox("codex", name + ".md")) as fh:
            text = fh.read()
        self.assertEqual(text, tickets.render_brief(REPO, self.fake.issues[12], "codex", 1))
        self.assertIn("Issue: %s#12" % REPO, text)
        self.assertEqual(self.fake.names(12), ["loom:ticket", "lane:codex", "loom:running"])
        puts = [p for p in self.fake.paths("PUT")]
        self.assertEqual(puts, ["/repos/%s/issues/12/labels" % REPO])
        sha = __import__("hashlib").sha256(BODY.encode()).hexdigest()[:12]
        self.assertEqual([c["body"] for c in self.fake.comments[12]],
                         ["Claimed for lane codex (body sha256 %s)" % sha])

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

    def test_the_oldest_trusted_ticket_wins_and_untrusted_ones_are_skipped(self):
        self.fake.add(5, author="stranger", created="2030-01-01T00:00:00Z")
        self.fake.add(9, created="2030-01-03T00:00:00Z")
        self.fake.add(7, created="2030-01-02T00:00:00Z")
        self.run_pass()
        self.assertEqual(self.listing(), ["example-owner.example-repo-7.md"])
        self.assertIn("loom:briefed", self.fake.names(5))

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

    def test_a_second_pass_after_a_304_makes_one_call(self):
        self.fake.add(12, author="stranger")
        self.run_pass()
        first = len(self.fake.calls)
        gh = self.run_pass()
        self.assertEqual(len(self.fake.calls), first + 1)
        self.assertEqual(gh.requests, 1)
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
        self.assertEqual(self.fake.names(12), ["loom:ticket", "lane:codex", "loom:running"])
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
        self.assertEqual(self.fake.names(12), ["loom:ticket", "lane:codex", "loom:running"])

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
        self.assertEqual(pulls, ["/repos/%s/pulls?head=example-owner:loom/12-fix-the-thing&state=all" % REPO])

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

    def test_a_fix_whose_branch_moved_goes_back_to_its_pull_request_not_a_failure(self):
        self.failed_fix([
            "[2030-01-01T00:01:00Z] example-owner.example-repo-12-r2 failed: branch moved: "
            "loom/12-fix-the-thing is at 0123456789ab on the remote, not ba9876543210 as when "
            "the run started"])
        self.assertEqual(self.fake.names(12), ["loom:ticket", "lane:codex", "loom:pr"])
        body = self.fake.comments[12][-1]["body"]
        self.assertIn("Lane codex did not push its fix to #99: the branch moved during the run "
                      "(branch moved: loom/12-fix-the-thing is at 0123456789ab", body)
        self.assertIn("not a failure", body)
        self.assertNotIn("failed", body)
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
        self.assertEqual(pulls, ["/repos/%s/pulls?head=example-owner:loom/12-fix-the-thing&state=all" % REPO])

    def test_done_without_a_pull_request_is_retried_not_reported(self):
        self.finished("done")
        self.run_pass()
        self.assertEqual(self.fake.comments[12], [])
        self.assertIn("loom:running", self.fake.names(12))

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
        self.fake.add(12, state="running", lane=lane)
        self.fake.comments[99] = []
        fix = tickets.parse_fix(REPO, fix_body) if fix_body else None
        name = tickets.ticket_name(REPO, 12, 1)
        self.write(self.inbox(lane, "failed", name + ".md"),
                   tickets.render_brief(REPO, self.fake.issues[12], lane, 1, fix))
        self.write(os.path.join(self.home, "logs", lane, name + ".log"),
                   "[t] %s start, lane %s\n[t] %s failed: limit: the spark endpoint refused "
                   "the connection (agent exited 1)\n" % (name, lane, name, ))
        self.run_pass()
        self.run_pass()

    def test_a_limit_is_not_a_failure_and_the_ticket_goes_back_to_briefed(self):
        self.limited()
        self.assertEqual(self.fake.names(12), ["loom:ticket", "lane:spark", "loom:briefed"])
        self.assertEqual(len(self.fake.comments[12]), 1)
        body = self.fake.comments[12][0]["body"]
        self.assertIn("Lane spark is unavailable (limit: the spark endpoint refused", body)
        self.assertIn("not a failure", body)
        self.assertNotIn("failed", body)

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
        self.fake.add(12, lane="anthropic-4")
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
        self.fail = False
        self.read = []

    def go(self, now=NOON):
        def post(url, payload):
            if self.fail:
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
        self.fail = True
        self.go()
        self.assertEqual(self.sent, [])
        self.fail = False
        self.go(NOON + 60)
        self.assertEqual(len(self.sent), 2)  # the urgent ask and the digest
        self.go(NOON + 120)
        self.assertEqual(len(self.sent), 2)


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
        reads = [c[1] for c in self.fake.calls if "/issues/40/comments" in c[1]]
        self.assertTrue(reads and all("since=" in r for r in reads))

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



class BrainTest(Base):
    def setUp(self):
        super().setUp()
        self.settings.webhook_ssm = "/example/webhook"
        self.settings.status_repo = REPO
        self.sent = []

    def tick(self, outcome="ok", code=0, ok_age=None):
        state = os.path.join(self.home, "state")
        self.write(os.path.join(state, "brain.json"),
                   json.dumps({"at": int(time.time()), "outcome": outcome, "exit": code}))
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
                         "## Shipped the search page\nDetail one\nDetail two\n" + link)
        for body in self.discord.sent + self.discord.in_thread:
            self.assertEqual(body["flags"], station.SUPPRESS_EMBEDS)
            self.assertEqual(body["allowed_mentions"], {"parse": []})

    def test_a_long_report_fills_several_thread_messages(self):
        self.comment(40, 100, "line\n" * 700)
        self.go()
        parts = [m["content"] for m in self.discord.in_thread]
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= station.DISCORD_MAX for p in parts))
        self.assertEqual("\n".join(parts).count("line"), 700)
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
        self.assertEqual(self.discord.in_thread[-1]["content"].split("\n")[0], "report")

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
