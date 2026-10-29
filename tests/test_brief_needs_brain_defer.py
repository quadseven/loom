from __future__ import annotations

import json
import os
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain, handoff
from bus.github import GitHub, GitHubError

REPO = "example-owner/example-repo"
LOCK_REPO = "example-owner/loom"
NOW = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
AUTHORS = ["allowed-bot"]
PR = (REPO, 7)
TICKET = (REPO, 9)


def at(minutes_ago: int) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def journal(minutes_ago, holder, *lines, login="allowed-bot"):
    body = "\n".join([handoff.JOURNAL_MARKER, "Tick %s, %s" % (at(minutes_ago), holder),
                      "- Waiting: merge deferred for budget"] + list(lines))
    return {"body": body, "at": at(minutes_ago), "login": login}


def defer(item, rev, holder="muse", stage="merge"):
    return handoff.defer_marker(stage, item, rev, holder)


def done(item, holder="lane-machine", stage="merge"):
    return handoff.done_marker(stage, item, holder)


def comment(entry):
    return {"body": entry["body"], "created_at": entry["at"], "user": {"login": entry["login"]}}


class BriefNeedsBrainDeferTests(unittest.TestCase):
    """Tests for needs-brain defer on briefed tickets."""

    def routes(self, *journals):
        routes = self._base_routes(*journals)
        routes.update({
            ("GET", "/repos/%s/issues/1/comments" % REPO): (200, [comment(j) for j in journals]),
            ("GET", "/repos/%s/issues/1/sub_issues" % REPO): (200, []),
            ("POST", "/repos/%s/issues" % REPO): (201, {"number": 12, "id": 212, "html_url": "https://example.com/12"}),
            ("POST", "/repos/%s/issues/1/sub_issues" % REPO): (201, {}),
            ("POST", "/repos/%s/issues/1/comments" % REPO): (201, {}),
        })
        return routes

    def _base_routes(self, *journals):
        goal = {"number": 1, "title": "Goal", "state": "open", "labels": [{"name": "loom:goal"}]}
        comments = [comment(journal(35, "muse", defer(PR, "pr-abcdef123456"))),
                    comment(journal(5, "muse", defer(PR, "pr-abcdef123456"),
                                    defer(TICKET, "failed-c2", stage="failed")))]
        routes = {
            ("GET", "/repos/%s/issues" % REPO): (200, [goal]),
            ("GET", "/repos/%s/issues/1" % REPO): (200, goal),
            "default": comments,
        }
        return routes

    def journals_posted(self, fake):
        return [body["body"] for path, body in fake.posted
                if path == "/repos/%s/issues/1/comments" % REPO]

    def test_a_brief_with_needs_brain_defer_records_no_done_marker(self):
        """A brief under a standing brief-needs-brain defer records its own done marker."""
        brief_needs_brain_defer = handoff.defer_marker("brief-needs-brain", (REPO, 1), "open", "muse",
                                                       note="needs-brain defer on briefed ticket")
        routes = self.routes(journal(10, "muse", brief_needs_brain_defer))
        fake = Routes(routes)
        url = brain.brief(GitHub("t", transport=fake), REPO, 3, "Ticket", BRIEF,
                         holder="lane-machine", env=ENV)
        self.assertEqual("https://example.com/12", url)
        [posted] = self.journals_posted(fake)
        self.assertTrue(posted.startswith(handoff.JOURNAL_MARKER))
        self.assertIn(handoff.done_marker("brief-needs-brain", (REPO, 1), "lane-machine", key="aaaaaaaa"), posted)
        self.assertIn("https://example.com/12", posted)
        self.assertEqual("lane-machine", handoff.journal_holder({"body": posted}))

    def test_a_brief_with_needs_brain_defer_retires_it_in_resume(self):
        """A brief under a standing brief-needs-brain defer retires it in resume."""
        brief_needs_brain_defer = handoff.defer_marker("brief-needs-brain", (REPO, 1), "open", "muse",
                                                       note="needs-brain defer on briefed ticket", key="aaaaaaaa")
        routes = self.routes(journal(10, "muse", brief_needs_brain_defer))
        fake = Routes(routes)
        brain.brief(GitHub("t", transport=fake), REPO, 3, "Ticket", BRIEF,
                    holder="lane-machine", env=ENV)
        [posted] = self.journals_posted(fake)
        done_body = "\n".join([
            handoff.JOURNAL_MARKER,
            "Tick %s, lane-machine" % at(0),
            "- Briefed: https://example.com/12",
            handoff.done_marker("brief-needs-brain", (REPO, 1), "lane-machine", key="aaaaaaaa")])
        routes[("GET", "/repos/%s/issues/1/comments" % REPO)] = (200, [
            comment(journal(10, "muse", brief_needs_brain_defer)),
            comment({"body": done_body, "at": at(0), "login": "allowed-bot"})])
        out = brain.resume(GitHub("t", transport=Routes(routes)), [REPO],
                           "lane-machine", LOCK_REPO, NOW, ENV)
        self.assertNotIn("- resume brief-needs-brain", out)
        self.assertIn("- stage brief-needs-brain: held by lane-machine; last done %s#1" % REPO, out)

    def test_a_brief_with_no_needs_brain_defer_posts_no_journal(self):
        """A brief with no standing brief-needs-brain defer posts no journal."""
        brief_needs_brain_defer = handoff.defer_marker("brief-needs-brain", (REPO, 1), "open", "muse",
                                                       note="needs-brain defer on briefed ticket", key="aaaaaaaa")
        done_marker = handoff.done_marker("brief-needs-brain", (REPO, 1), "muse", key="aaaaaaaa")
        cases = {
            "no markers": [journal(10, "muse")],
            "defer already done": [journal(20, "muse", brief_needs_brain_defer),
                                  journal(10, "muse", done_marker)],
            "defer from untrusted login": [journal(10, "muse", brief_needs_brain_defer, login="stranger")],
            "defer of another goal": [journal(10, "muse", handoff.defer_marker(
                "brief-needs-brain", (REPO, 2), "open", "muse", note="other"))],
        }
        for name, journals in cases.items():
            with self.subTest(name):
                fake = Routes(self.routes(*journals))
                brain.brief(GitHub("t", transport=fake), REPO, 3, "Ticket", BRIEF,
                            holder="lane-machine", env=ENV)
                self.assertEqual([], self.journals_posted(fake))
                self.assertIn(("POST", "/repos/%s/issues" % REPO), fake.calls)

    def test_a_failed_done_post_names_the_ticket_and_the_line_to_journal_for_needs_brain(self):
        """A failed brief done post names the ticket and the line to journal for brief-needs-brain."""
        brief_needs_brain_defer = handoff.defer_marker("brief-needs-brain", (REPO, 1), "open", "muse",
                                                       note="needs-brain defer on briefed ticket")
        routes = self.routes(journal(10, "muse", brief_needs_brain_defer))
        routes[("POST", "/repos/%s/issues/1/comments" % REPO)] = (502, {"message": "Bad Gateway"})
        with self.assertRaises(GitHubError) as caught:
            brain.brief(GitHub("t", transport=Routes(routes)), REPO, 3, "Ticket", BRIEF,
                        holder="lane-machine", env=ENV)
        self.assertIn("https://example.com/12", str(caught.exception))
        self.assertIn(handoff.done_marker("brief-needs-brain", (REPO, 1), "lane-machine"),
                      str(caught.exception))

    def test_brief_needs_brain_defer_prints_marker_with_revision_now(self):
        """Brief-needs-brain defer prints marker with revision now."""
        routes = self.routes()
        line = brain.defer(GitHub("t", transport=Routes(routes)), "brief-needs-brain", (REPO, 1), "muse")
        self.assertIn(" key=", line)
        self.assertIn("note=", line)

    def test_brief_needs_brain_defer_refuses_brief_needs_brain_without_note(self):
        """Brief-needs-brain defer requires a note naming the problem statement."""
        routes = self.routes()
        with self.assertRaises(ValueError) as caught:
            brain.defer(GitHub("t", transport=Routes(routes)), "brief-needs-brain", (REPO, 1), "muse")
        self.assertIn("a brief-needs-brain defer requires --note naming the problem statement", str(caught.exception))


class Routes:
    """A fake transport answering by method and path; records every call."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []
        self.posted = []

    def __call__(self, method, url, headers, body):
        path = url.split("api.github.com", 1)[-1].split("?")[0]
        self.calls.append((method, path))
        if method == "POST":
            self.posted.append((path, json.loads(body) if body else None))
        if method == "GET" and path.startswith("/repos/") and path.endswith("/pulls"):
            return 200, {}, b"[]"
        if method == "POST" and path == "/graphql" and "subIssues" in json.loads(body)["query"]:
            return 200, {}, json.dumps({"data": {"repository": self.sub_issue_states(
                json.loads(body)["query"])}}).encode()
        if (method == "GET" and path.endswith("/sub_issues")
                and (method, path) not in self.routes):
            return 200, {}, b"[]"  # a goal with no epics or tickets linked
        if (method, path) not in self.routes:
            raise AssertionError("unexpected call %s %s" % (method, path))
        status, data = self.routes[(method, path)]
        return status, {}, json.dumps(data).encode()

    def sub_issue_states(self, query):
        """Resume's batched sub-issue states (brain._sub_issue_states), from
        the same sub_issues routes a REST read answers."""
        repo = "%s/%s" % re.search(r'owner:"([^"]+)",name:"([^"]+)"', query).groups()
        out = {}
        for number in re.findall(r"issue\(number:(\d+)\)", query):
            _, subs = self.routes.get(
                ("GET", "/repos/%s/issues/%s/sub_issues" % (repo, number)), (200, []))
            out["e" + number] = {"subIssues": {"totalCount": len(subs), "nodes": [
                {"number": s["number"], "state": s["state"].upper()} for s in subs]}}
        return out


# Import test data from handoff.py
BRIEF = """## Why
why: the page query is slow for every visitor.
## What
what
## Acceptance criteria
- [ ] done
- [ ] tested
- [ ] live
## Size
S
## Out of scope
- none"""

BRIEF_DEFER = handoff.defer_marker("brief", (REPO, 1), "open", "muse", note="checkout fix")

ENV = {"LOOM_BRIEF_AUTHORS": "allowed-bot"}