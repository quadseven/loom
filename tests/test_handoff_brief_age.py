from __future__ import annotations

import json
import os
import re
import sys
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain, handoff
from bus.github import GitHub

REPO = "example-owner/example-repo"
LOCK_REPO = "example-owner/loom"
NOW = datetime(2030, 1, 10, 12, 0, tzinfo=timezone.utc)


def at(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def defer(stage: str, number: int, days_ago: float, key: str = "") -> handoff.Mark:
    return handoff.Mark("defer", stage, (REPO, number), "open", "muse",
                        at(days_ago), "", key)


class BriefAgeTests(unittest.TestCase):
    def test_brief_defer_older_than_seven_days_is_not_a_resume_item(self):
        found = [defer("brief", 1, 8, "11111111"),
                 defer("brief", 2, 6, "22222222")]
        items = handoff.resume_items(found, NOW)
        self.assertEqual([(REPO, 2)], [m.item for m in items])

    def test_env_cap_of_two_days_drops_a_three_day_old_brief_defer(self):
        found = [defer("brief", 1, 3, "11111111"),
                 defer("brief", 2, 1, "22222222")]
        with unittest.mock.patch.dict(os.environ, {"LOOM_BRIEF_DEFER_DAYS": "2"}):
            items = handoff.resume_items(found, NOW)
            dropped = handoff.dropped_brief_defers(found, NOW)
        self.assertEqual([(REPO, 2)], [m.item for m in items])
        self.assertEqual([(REPO, 1)], [m.item for m in dropped])

    def test_without_a_reference_time_nothing_ages_out(self):
        # Callers that retire defers (brief done markers, may_act) call
        # resume_items with no reference time and must see every standing
        # defer, however old: aging out is a resume-listing rule only.
        found = [defer("brief", 1, 30, "11111111")]
        self.assertEqual([(REPO, 1)],
                         [m.item for m in handoff.resume_items(found)])
        self.assertEqual([], handoff.dropped_brief_defers(found))

    def test_old_defers_of_other_stages_are_not_aged_out(self):
        found = [defer("merge", 7, 30), defer("failed", 9, 30)]
        items = handoff.resume_items(found, NOW)
        self.assertEqual([(REPO, 7), (REPO, 9)], [m.item for m in items])


def at_minutes(minutes_ago: int) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def journal(minutes_ago, holder, *lines, login="allowed-bot"):
    body = "\n".join([handoff.JOURNAL_MARKER,
                      "Tick %s, %s" % (at_minutes(minutes_ago), holder)] + list(lines))
    return {"body": body, "at": at_minutes(minutes_ago), "login": login}


def comment(entry):
    return {"body": entry["body"], "created_at": entry["at"],
            "user": {"login": entry["login"]}}


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
        if (method == "POST" and path == "/graphql" and body
                and "pullRequests(states:OPEN)" in json.loads(body)["query"]):
            return 200, {}, b'{"data": {}}'
        if method == "POST" and path == "/graphql" and "subIssues" in json.loads(body)["query"]:
            return 200, {}, json.dumps({"data": {"repository": self.sub_issue_states(
                json.loads(body)["query"])}}).encode()
        if (method == "GET" and path.endswith("/sub_issues")
                and (method, path) not in self.routes):
            return 200, {}, b"[]"
        if (method, path) not in self.routes:
            raise AssertionError("unexpected call %s %s" % (method, path))
        status, data = self.routes[(method, path)]
        return status, {}, json.dumps(data).encode()

    def sub_issue_states(self, query):
        repo = "%s/%s" % re.search(r'owner:"([^"]+)",name:"([^"]+)"', query).groups()
        out = {}
        for number in re.findall(r"issue\(number:(\d+)\)", query):
            _, subs = self.routes.get(
                ("GET", "/repos/%s/issues/%s/sub_issues" % (repo, number)), (200, []))
            out["e" + number] = {"subIssues": {"totalCount": len(subs), "nodes": [
                {"number": s["number"], "state": s["state"].upper(),
                 "title": s.get("title"), "createdAt": s.get("created_at"),
                 "closedAt": s.get("closed_at"), "updatedAt": s.get("updated_at"),
                 "labels": {"nodes": [{"name": x["name"]} for x in s.get("labels") or []]}}
                for s in subs]}}
        return out


class ResumeBriefAgeTests(unittest.TestCase):
    def routes(self):
        goal = {"number": 1, "title": "Goal", "state": "open",
                "labels": [{"name": "loom:goal"}]}
        old = handoff.defer_marker("brief", (REPO, 1), "open", "muse",
                                   note="old problem", key="11111111")
        fresh = handoff.defer_marker("brief", (REPO, 1), "open", "muse",
                                     note="fresh problem", key="22222222")
        comments = [comment(journal(3 * 24 * 60, "muse", old)),
                    comment(journal(10, "muse", fresh))]
        routes = {
            ("GET", "/repos/%s/git/ref/heads/loom-lock/brain" % LOCK_REPO):
                (200, {"object": {"sha": "s1"}}),
            ("GET", "/repos/%s/git/commits/s1" % LOCK_REPO):
                (200, {"message": "holder=lane-machine expires=2030-01-10T12:30:00Z"}),
            ("GET", "/repos/%s/issues" % REPO): (200, [goal]),
            ("GET", "/repos/%s/issues/1" % REPO): (200, goal),
            ("GET", "/repos/%s/issues/1/comments" % REPO): (200, comments),
        }
        return routes

    def test_resume_summarises_brief_defers_dropped_for_age_in_one_line(self):
        with unittest.mock.patch.dict(os.environ, {"LOOM_BRIEF_DEFER_DAYS": "2"}):
            out = brain.resume(GitHub("t", transport=Routes(self.routes())), [REPO],
                               "lane-machine", LOCK_REPO, NOW,
                               {"LOOM_BRIEF_AUTHORS": "allowed-bot"})
        lines = [line for line in out.splitlines() if "brief defers older than" in line]
        self.assertEqual(["- dropped 1 brief defers older than 2 days"], lines)
        self.assertIn("- resume brief %s#1: fresh problem" % REPO, out)
        self.assertNotIn("old problem", out)


if __name__ == "__main__":
    unittest.main()
