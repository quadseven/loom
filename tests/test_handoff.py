from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain, handoff
from bus.github import GitHub

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


class HandoffRuleTests(unittest.TestCase):
    def test_repeated_deferred_merge_transfers_despite_fresh_journals(self):
        # Muse journals every half hour and defers the same ready merge each
        # time. The freshness-only rule never lets the live brain act; the
        # stage handoff gives the merge to it.
        found = handoff.journals([journal(m, "muse", defer(PR, "pr-abc"))
                                  for m in (95, 65, 35, 5)], AUTHORS)
        current = {PR: "pr-abc"}
        self.assertFalse(handoff.quiet(found, "lane-machine", NOW))
        self.assertTrue(handoff.may_act("merge", PR, "lane-machine", found, NOW, current))
        # Only the item that was yielded, and only for its stage.
        self.assertFalse(handoff.may_act("merge", (REPO, 8), "lane-machine", found, NOW, current))
        self.assertFalse(handoff.may_act("review", PR, "lane-machine", found, NOW, current))

    def test_a_journal_with_an_unreadable_time_counts_as_fresh(self):
        # An undatable journal must not open broad work to the live brain.
        odd = {"body": "\n".join([handoff.JOURNAL_MARKER, "Tick later, muse"]),
               "at": "not-a-time", "login": "allowed-bot"}
        self.assertEqual([odd], handoff.peer_journals([odd], "lane-machine", NOW))
        self.assertEqual([], handoff.peer_journals([odd], "muse", NOW))

    def test_quiet_peers_leave_every_stage_open_as_before(self):
        found = handoff.journals([journal(180, "muse"), journal(10, "lane-machine")], AUTHORS)
        self.assertTrue(handoff.may_act("merge", PR, "lane-machine", found, NOW, {}))

    def test_a_done_marker_retires_an_older_defer(self):
        found = handoff.journals([journal(60, "muse", defer(PR, "pr-abc")),
                                  journal(30, "lane-machine", done(PR))], AUTHORS)
        marks = handoff.marks(found)
        self.assertEqual([], handoff.resume_items(marks))
        self.assertFalse(handoff.may_act("merge", PR, "muse", found, NOW, {PR: "pr-abc"}))

    def test_a_changed_or_closed_item_retires_its_defer(self):
        found = handoff.journals([journal(30, "muse", defer(PR, "pr-abc"),
                                          defer(TICKET, "failed-c3", stage="failed"))], AUTHORS)
        items = handoff.resume_items(handoff.marks(found))
        self.assertEqual(2, len(handoff.live(items, {PR: "pr-abc", TICKET: "failed-c3"})))
        self.assertEqual([], handoff.live(items, {PR: "pr-def"}))
        self.assertFalse(handoff.may_act("merge", PR, "lane-machine", found, NOW, {PR: "pr-def"}))

    def test_a_newer_defer_replaces_the_older_one(self):
        found = handoff.journals([journal(60, "muse", defer(PR, "pr-abc")),
                                  journal(20, "muse", defer(PR, "pr-def"))], AUTHORS)
        items = handoff.resume_items(handoff.marks(found))
        self.assertEqual(["pr-def"], [m.rev for m in items])

    def test_markers_from_other_logins_are_data(self):
        found = handoff.journals([journal(5, "muse", defer(PR, "pr-abc"), login="stranger")],
                                 AUTHORS)
        self.assertEqual([], found)

    def test_stage_lines_name_the_holder_and_last_done(self):
        found = handoff.journals([journal(60, "lane-machine", done(PR)),
                                  journal(20, "muse", defer((REPO, 8), "pr-1"))], AUTHORS)
        lines = handoff.stage_lines(handoff.marks(found))
        self.assertEqual(["- stage merge: yielded by muse at %s; last done %s#7 by "
                          "lane-machine at %s" % (at(20), REPO, at(60))], lines)

    def test_markers_refuse_unknown_stages_and_holders(self):
        with self.assertRaises(ValueError):
            handoff.defer_marker("ship", PR, "pr-abc", "muse")
        with self.assertRaises(ValueError):
            handoff.done_marker("merge", PR, "muse -->")


class Routes:
    """A fake transport answering by method and path; records every call."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []
        self.posted = {}

    def __call__(self, method, url, headers, body):
        path = url.split("api.github.com", 1)[-1].split("?")[0]
        self.calls.append((method, path))
        if body:
            try:
                self.posted[(method, path)] = json.loads(body.decode())
            except Exception:
                pass
        if method == "GET" and path.startswith("/repos/") and path.endswith("/pulls"):
            return 200, {}, b"[]"
        if (method, path) not in self.routes:
            raise AssertionError("unexpected call %s %s" % (method, path))
        status, data = self.routes[(method, path)]
        return status, {}, json.dumps(data).encode()


def lock_routes(holder="lane-machine", expires="2030-01-01T12:30:00Z"):
    return {
        ("GET", "/repos/%s/git/ref/heads/loom-lock/brain" % LOCK_REPO): (200, {"object": {"sha": "s1"}}),
        ("GET", "/repos/%s/git/commits/s1" % LOCK_REPO):
            (200, {"message": "holder=%s expires=%s" % (holder, expires)}),
    }


def comment(entry):
    return {"body": entry["body"], "created_at": entry["at"], "user": {"login": entry["login"]}}


class ResumeCommandTests(unittest.TestCase):
    def routes(self, pr_head="abcdef1234567890", pr_state="open", goal_state="open", **lock):
        goal = {"number": 1, "title": "Goal", "state": goal_state, "labels": [{"name": "loom:goal"}]}
        comments = [comment(journal(35, "muse", defer(PR, "pr-abcdef123456"))),
                    comment(journal(5, "muse", defer(PR, "pr-abcdef123456"),
                                    defer(TICKET, "failed-c2", stage="failed")))]
        routes = lock_routes(**lock)
        routes.update({
            ("GET", "/repos/%s/issues" % REPO): (200, [goal]),
            ("GET", "/repos/%s/issues/1" % REPO): (200, goal),
            ("GET", "/repos/%s/issues/1/comments" % REPO): (200, comments),
            ("GET", "/repos/%s/issues/7" % REPO): (200, {"state": pr_state, "pull_request": {}}),
            ("GET", "/repos/%s/pulls/7" % REPO): (200, {"state": pr_state, "head": {"sha": pr_head}}),
            ("GET", "/repos/%s/issues/9" % REPO):
                (200, {"state": "open", "comments": 2,
                       "labels": [{"name": "loom:ticket"}, {"name": "loom:failed"}]}),
        })
        return routes

    def test_the_lock_holder_gets_the_merge_and_failed_ticket(self):
        out = brain.resume(GitHub("t", transport=Routes(self.routes())), [REPO],
                           "lane-machine", LOCK_REPO, NOW, {"LOOM_BRIEF_AUTHORS": "allowed-bot"})
        self.assertIn("Another brain journaled in the last 2 hours (muse", out)
        self.assertIn("- resume merge %s#7 (deferred by muse at %s)" % (REPO, at(5)), out)
        self.assertIn("- resume failed %s#9" % REPO, out)
        self.assertIn("- stage merge: yielded by muse", out)

    def test_anyone_but_the_lock_holder_is_refused_before_any_read(self):
        for lock in ({"holder": "muse"}, {"expires": "2030-01-01T11:00:00Z"}):
            with self.subTest(**lock):
                fake = Routes(self.routes(**lock))
                with self.assertRaises(ValueError):
                    brain.resume(GitHub("t", transport=fake), [REPO], "lane-machine",
                                 LOCK_REPO, NOW, {"LOOM_BRIEF_AUTHORS": "allowed-bot"})
                self.assertEqual(2, len(fake.calls))

    def test_a_merged_or_pushed_pull_request_is_dropped(self):
        for kw, why in (({"pr_state": "closed"}, "finished or closed"),
                        ({"pr_head": "0123456789abcdef"}, "changed")):
            with self.subTest(why=why):
                out = brain.resume(GitHub("t", transport=Routes(self.routes(**kw))), [REPO],
                                   "lane-machine", LOCK_REPO, NOW,
                                   {"LOOM_BRIEF_AUTHORS": "allowed-bot"})
                self.assertNotIn("- resume merge", out)
                self.assertIn("- dropped merge %s#7: %s since the defer" % (REPO, why), out)
                self.assertIn("- resume failed %s#9" % REPO, out)

    def test_defer_prints_the_marker_with_the_revision_now(self):
        routes = self.routes()
        line = brain.defer(GitHub("t", transport=Routes(routes)), "merge", PR, "muse")
        self.assertEqual("<!-- loom:defer v1 stage=merge item=%s#7 rev=pr-abcdef123456 "
                         "holder=muse -->" % REPO, line)
        line = brain.defer(GitHub("t", transport=Routes(routes)), "failed", TICKET, "muse")
        self.assertIn("rev=failed-c2", line)

    def test_defer_refuses_an_unknown_stage_with_a_value_error(self):
        with self.assertRaises(ValueError) as caught:
            brain._check_stage_item(GitHub("t", transport=Routes(self.routes())), "ship", *PR)
        self.assertIn("unknown stage ship", str(caught.exception))

    def test_defer_refuses_an_item_of_the_wrong_kind(self):
        with self.assertRaises(ValueError):
            brain.defer(GitHub("t", transport=Routes(self.routes())), "merge", TICKET, "muse")
        with self.assertRaises(ValueError):
            brain.defer(GitHub("t", transport=Routes(self.routes())), "failed", PR, "muse")
        with self.assertRaises(ValueError):
            brain.defer(GitHub("t", transport=Routes(self.routes())), "brief", TICKET, "muse",
                        note="party-trip fix")

    def test_defer_brief_writes_marker_listed_by_resume_and_dropped_by_done(self):
        routes = self.routes()
        marker = brain.defer(GitHub("t", transport=Routes(routes)), "brief",
                             (REPO, 1), "muse", note="party-trip fix")
        self.assertEqual(
            '<!-- loom:defer v1 stage=brief item=%s#1 rev=open holder=muse note="party-trip fix" -->' % REPO,
            marker)
        routes[("GET", "/repos/%s/issues/1/comments" % REPO)] = (200, [
            comment(journal(10, "muse", marker)),
        ])
        out = brain.resume(GitHub("t", transport=Routes(routes)), [REPO],
                           "lane-machine", LOCK_REPO, NOW,
                           {"LOOM_BRIEF_AUTHORS": "allowed-bot"})
        self.assertIn("- resume brief %s#1: party-trip fix (deferred by muse at %s)" % (
            REPO, at(10)), out)

        done_line = handoff.done_marker("brief", (REPO, 1), "lane-machine")
        routes[("GET", "/repos/%s/issues/1/comments" % REPO)] = (200, [
            comment(journal(10, "muse", marker)),
            comment(journal(5, "lane-machine", done_line)),
        ])
        out_after_done = brain.resume(GitHub("t", transport=Routes(routes)), [REPO],
                                      "lane-machine", LOCK_REPO, NOW,
                                      {"LOOM_BRIEF_AUTHORS": "allowed-bot"})
        self.assertNotIn("- resume brief", out_after_done)

    def test_brief_handoff_whose_goal_home_was_closed_is_dropped_from_resume(self):
        routes = self.routes(goal_state="closed")
        marker = handoff.defer_marker("brief", (REPO, 1), "open", "muse",
                                     note="party-trip fix")
        routes[("GET", "/repos/%s/issues/1/comments" % REPO)] = (200, [
            comment(journal(10, "muse", marker)),
        ])
        out = brain.resume(GitHub("t", transport=Routes(routes)), [REPO],
                           "lane-machine", LOCK_REPO, NOW,
                           {"LOOM_BRIEF_AUTHORS": "allowed-bot"})
        self.assertNotIn("- resume brief", out)
        self.assertIn("- dropped brief %s#1: finished or closed since the defer" % REPO, out)

    def test_done_on_another_goal_drops_defer(self):
        routes = self.routes()
        goals = [dict(number=n, title="Goal %d" % n, state="open",
                      labels=[{"name": "loom:goal"}]) for n in (1, 2)]
        routes[("GET", "/repos/%s/issues" % REPO)] = (200, goals)
        routes[("GET", "/repos/%s/issues/1/comments" % REPO)] = (200, [comment(
            journal(30, "muse", handoff.defer_marker("brief", (REPO, 1), "open", "muse",
                                                      note="example brief")) )])
        routes[("GET", "/repos/%s/issues/2/comments" % REPO)] = (200, [comment(
            journal(10, "lane-machine", handoff.done_marker("brief", (REPO, 1),
                                                              "lane-machine")) )])
        out = brain.resume(GitHub("t", transport=Routes(routes)), [REPO],
                           "lane-machine", LOCK_REPO, NOW,
                           {"LOOM_BRIEF_AUTHORS": "allowed-bot"})
        self.assertIn("- dropped brief %s#1: finished or closed since the defer" % REPO, out)
        self.assertNotIn("- resume brief %s#1" % REPO, out)

    def test_newer_cross_goal_defer_is_listed_once_under_newest_goal(self):
        routes = self.routes()
        goals = [dict(number=n, title="Goal %d" % n, state="open",
                      labels=[{"name": "loom:goal"}]) for n in (1, 2)]
        routes[("GET", "/repos/%s/issues" % REPO)] = (200, goals)
        routes[("GET", "/repos/%s/issues/1/comments" % REPO)] = (200, [comment(
            journal(30, "muse", handoff.defer_marker("brief", (REPO, 1), "open", "muse",
                                                      note="example brief")) )])
        routes[("GET", "/repos/%s/issues/2/comments" % REPO)] = (200, [comment(
            journal(10, "lane-machine", handoff.defer_marker("brief", (REPO, 1), "open",
                                                              "lane-machine", note="example brief")) )])
        out = brain.resume(GitHub("t", transport=Routes(routes)), [REPO],
                           "lane-machine", LOCK_REPO, NOW,
                           {"LOOM_BRIEF_AUTHORS": "allowed-bot"})
        lines = [line for line in out.splitlines() if line.startswith("- resume brief %s#1" % REPO)]
        self.assertEqual(1, len(lines))
        self.assertIn("(deferred by lane-machine at %s)" % at(10), lines[0])
        self.assertNotIn("- dropped brief %s#1" % REPO, out)

    def test_defer_refuses_brief_without_note(self):
        routes = self.routes()
        with self.assertRaises(ValueError):
            brain.defer(GitHub("t", transport=Routes(routes)), "brief", (REPO, 1), "muse")

    def test_brief_records_done_marker_retiring_live_brief_defer(self):
        routes = self.routes()
        defer_line = handoff.defer_marker("brief", (REPO, 1), "open", "muse", note="cache query")
        routes[("GET", "/repos/%s/issues/1/comments" % REPO)] = (200, [
            comment(journal(10, "muse", defer_line)),
        ])
        routes[("GET", "/repos/%s/issues/1/sub_issues" % REPO)] = (200, [])
        routes[("POST", "/repos/%s/issues" % REPO)] = (200, {
            "number": 2, "id": 102, "html_url": "https://github.com/%s/issues/2" % REPO,
        })
        routes[("POST", "/repos/%s/issues/1/sub_issues" % REPO)] = (200, {})
        routes[("POST", "/repos/%s/issues/1/comments" % REPO)] = (200, {"id": 201})

        fake = Routes(routes)
        gh = GitHub("t", transport=fake)
        env = {"LOOM_BRIEF_AUTHORS": "allowed-bot"}

        # Before brief: resume lists the brief item
        out_before = brain.resume(gh, [REPO], "lane-machine", LOCK_REPO, NOW, env)
        self.assertIn("- resume brief %s#1: cache query (deferred by muse at %s)" % (
            REPO, at(10)), out_before)

        # Brain briefs the ticket
        url = brain.brief(gh, REPO, 1, "T2: Cache query", VALID_BRIEF, lane="codex",
                          holder="lane-machine", env=env)
        self.assertEqual("https://github.com/%s/issues/2" % REPO, url)

        # Check comment was posted on goal home with done marker and new ticket number
        self.assertIn(("POST", "/repos/%s/issues/1/comments" % REPO), fake.calls)
        posted_body = fake.posted[("POST", "/repos/%s/issues/1/comments" % REPO)]["body"]
        self.assertTrue(posted_body.startswith(handoff.JOURNAL_MARKER))
        self.assertIn("- Briefed: #2", posted_body)
        self.assertIn(handoff.done_marker("brief", (REPO, 1), "lane-machine"), posted_body)

        # Now resume no longer lists the resume brief item
        routes[("GET", "/repos/%s/issues/1/comments" % REPO)] = (200, [
            comment(journal(10, "muse", defer_line)),
            {"body": posted_body, "created_at": at(5), "user": {"login": "allowed-bot"}},
        ])
        out_after = brain.resume(GitHub("t", transport=Routes(routes)), [REPO],
                                 "lane-machine", LOCK_REPO, NOW, env)
        self.assertNotIn("- resume brief", out_after)
        self.assertIn("- dropped brief %s#1: finished or closed since the defer" % REPO, out_after)

    def test_brief_without_live_brief_defer_posts_no_extra_comment(self):
        routes = self.routes()
        routes[("GET", "/repos/%s/issues/1/comments" % REPO)] = (200, [])
        routes[("GET", "/repos/%s/issues/1/sub_issues" % REPO)] = (200, [])
        routes[("POST", "/repos/%s/issues" % REPO)] = (200, {
            "number": 2, "id": 102, "html_url": "https://github.com/%s/issues/2" % REPO,
        })
        routes[("POST", "/repos/%s/issues/1/sub_issues" % REPO)] = (200, {})

        fake = Routes(routes)
        gh = GitHub("t", transport=fake)
        env = {"LOOM_BRIEF_AUTHORS": "allowed-bot"}
        url = brain.brief(gh, REPO, 1, "T2: Cache query", VALID_BRIEF, lane="codex",
                          holder="lane-machine", env=env)
        self.assertEqual("https://github.com/%s/issues/2" % REPO, url)
        self.assertNotIn(("POST", "/repos/%s/issues/1/comments" % REPO), fake.calls)

    def test_refused_brief_records_no_done_marker(self):
        routes = self.routes()
        defer_line = handoff.defer_marker("brief", (REPO, 1), "open", "muse", note="cache query")
        routes[("GET", "/repos/%s/issues/1/comments" % REPO)] = (200, [
            comment(journal(10, "muse", defer_line)),
        ])
        routes[("GET", "/repos/%s/issues/1/sub_issues" % REPO)] = (200, [])
        routes[("POST", "/repos/%s/issues" % REPO)] = (500, {"message": "GitHub error"})

        fake = Routes(routes)
        gh = GitHub("t", transport=fake)
        env = {"LOOM_BRIEF_AUTHORS": "allowed-bot"}

        # Missing sections
        with self.assertRaises(ValueError):
            brain.brief(gh, REPO, 1, "T2: Incomplete", "## Why\nonly why",
                        lane="codex", holder="lane-machine", env=env)
        self.assertNotIn(("POST", "/repos/%s/issues/1/comments" % REPO), fake.calls)

        # Bad title
        with self.assertRaises(ValueError):
            brain.brief(gh, REPO, 1, "T2: Bad\ntitle", VALID_BRIEF,
                        lane="codex", holder="lane-machine", env=env)
        self.assertNotIn(("POST", "/repos/%s/issues/1/comments" % REPO), fake.calls)

        # GitHub error
        with self.assertRaises(brain.GitHubError):
            brain.brief(gh, REPO, 1, "T2: Cache query", VALID_BRIEF,
                        lane="codex", holder="lane-machine", env=env)
        self.assertNotIn(("POST", "/repos/%s/issues/1/comments" % REPO), fake.calls)


VALID_BRIEF = """## Why
The page query is slow for every visitor.

## What
Cache the query.

## Acceptance criteria
- [ ] The page loads in under a second.
- [ ] A test fails without the cache.
- [ ] The query runs once a minute.

## Size

S

## Out of scope
- Other pages.
"""


if __name__ == "__main__":
    unittest.main()
