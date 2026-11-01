from __future__ import annotations

import contextlib
import io
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
BRIEFED = (REPO, 10)


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
        # An undatable journal counts as fresh only when the comment itself
        # was created or updated within QUIET (acceptance criterion 2).
        now = datetime(2026, 10, 26, 13, 51, tzinfo=timezone.utc)
        # Recent comment (within QUIET): should count as fresh for peer
        recent = {"body": "\n".join([handoff.JOURNAL_MARKER, "Tick later, muse"]),
                  "at": "not-a-time", "login": "allowed-bot",
                  "created_at": "2026-10-26T12:00:00Z", "updated_at": "2026-10-26T12:00:00Z"}
        self.assertEqual([recent], handoff.peer_journals([recent], "lane-machine", now))
        self.assertEqual([], handoff.peer_journals([recent], "muse", now))
        # Old comment (outside QUIET): should NOT count as fresh for peer
        old = {"body": "\n".join([handoff.JOURNAL_MARKER, "Tick later, muse"]),
               "at": "not-a-time", "login": "allowed-bot",
               "created_at": "2026-10-25T12:00:00Z", "updated_at": "2026-10-25T12:00:00Z"}
        self.assertEqual([], handoff.peer_journals([old], "lane-machine", now))
        self.assertEqual([], handoff.peer_journals([old], "muse", now))

    def test_tick_time_without_date_uses_comment_created_at_day(self):
        # A Tick line with only a time (no date) should be dated from the
        # comment's createdAt day. A muse journal from 2026-10-19 with
        # "Tick 20:30Z, muse" should not appear fresh at 2026-10-26T13:51Z.
        now = datetime(2026, 10, 26, 13, 51, tzinfo=timezone.utc)
        # GitHub's comment timestamps date the time-only Tick lines. The
        # journal was edited in place, so each tick's own time is its `at`.
        created_at = "2026-10-19T19:30:00Z"
        updated_at = "2026-10-19T20:31:00Z"
        body = "\n".join([handoff.JOURNAL_MARKER, "Tick 20:30Z, muse",
                          "- Waiting: merge deferred for budget",
                          handoff.EARLIER_OPEN, "Tick 19:30Z, muse",
                          "- Waiting: merge deferred for budget", handoff.EARLIER_CLOSE])
        comment = {"body": body, "at": created_at, "login": "allowed-bot",
                   "createdAt": created_at, "updatedAt": updated_at}
        expanded = handoff.journals([comment], AUTHORS)
        self.assertEqual(["2026-10-19T19:30:00Z", "2026-10-19T20:30:00Z"],
                         [c["at"] for c in expanded])
        # peer_journals should not count it as fresh (7 days old)
        peers = handoff.peer_journals(expanded, "lane-machine", now)
        self.assertEqual([], peers)
        # The resume path passes only body, at (the comment's createdAt) and
        # login: the comment's own time still dates the Tick line.
        bare = {"body": body, "at": created_at, "login": "allowed-bot"}
        self.assertEqual([], handoff.peer_journals(handoff.journals([bare], AUTHORS),
                                                   "lane-machine", now))

    def test_tick_time_without_date_uses_day_before_if_after_updated_at(self):
        # If dating from createdAt day would put the tick after updated_at,
        # use the day before instead.
        now = datetime(2026, 10, 26, 13, 51, tzinfo=timezone.utc)
        created_at = "2026-10-19T10:00:00Z"
        updated_at = "2026-10-19T15:00:00Z"
        # Tick time 20:30Z on createdAt day (2026-10-19) would be after updated_at (15:00Z)
        # So it should be dated to 2026-10-18T20:30:00Z
        body = "\n".join([handoff.JOURNAL_MARKER, "Tick 20:30Z, muse",
                          "- Waiting: merge deferred for budget"])
        comment = {"body": body, "at": created_at, "login": "allowed-bot",
                   "created_at": created_at, "updated_at": updated_at}
        expanded = handoff.expand_journals([comment])
        self.assertEqual(len(expanded), 1)
        self.assertEqual(expanded[0]["at"], "2026-10-18T20:30:00Z")

    def test_unreadable_time_fresh_only_when_comment_recent(self):
        # A journal whose time cannot be read counts as fresh only when the
        # comment itself was created or updated within QUIET.
        now = datetime(2026, 10, 26, 13, 51, tzinfo=timezone.utc)
        # Comment created 3 hours ago (outside QUIET) with unreadable time
        old_created = (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        old_comment = {"body": "\n".join([handoff.JOURNAL_MARKER, "Tick later, muse"]),
                       "at": "not-a-time", "login": "allowed-bot",
                       "created_at": old_created, "updated_at": old_created}
        peers = handoff.peer_journals([old_comment], "lane-machine", now)
        self.assertEqual([], peers)  # Not fresh because comment is old

        # Comment created 1 hour ago (within QUIET) with unreadable time
        recent_created = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        recent_comment = {"body": "\n".join([handoff.JOURNAL_MARKER, "Tick later, muse"]),
                          "at": "not-a-time", "login": "allowed-bot",
                          "created_at": recent_created, "updated_at": recent_created}
        peers = handoff.peer_journals([recent_comment], "lane-machine", now)
        self.assertEqual([recent_comment], peers)  # Fresh because comment is recent

        # Comment created 3 hours ago but updated 1 hour ago (within QUIET)
        old_created2 = (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        recent_updated = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        updated_comment = {"body": "\n".join([handoff.JOURNAL_MARKER, "Tick later, muse"]),
                           "at": "not-a-time", "login": "allowed-bot",
                           "created_at": old_created2, "updated_at": recent_updated}
        peers = handoff.peer_journals([updated_comment], "lane-machine", now)
        self.assertEqual([updated_comment], peers)  # Fresh because updated recently

    def test_quiet_peers_leave_every_stage_open_as_before(self):
        found = handoff.journals([journal(180, "muse"), journal(10, "lane-machine")], AUTHORS)
        self.assertTrue(handoff.may_act("merge", PR, "lane-machine", found, NOW, {}))

    def test_a_done_marker_retires_an_older_defer(self):
        found = handoff.journals([journal(60, "muse", defer(PR, "pr-abc")),
                                  journal(30, "lane-machine", done(PR))], AUTHORS)
        marks = handoff.marks(found)
        self.assertEqual([], handoff.resume_items(marks))
        self.assertFalse(handoff.may_act("merge", PR, "muse", found, NOW, {PR: "pr-abc"}))

    def test_legacy_brief_markers_without_keys_still_parse_and_pair(self):
        old_defer = '<!-- loom:defer v1 stage=brief item=%s#1 rev=open holder=muse note="old note" -->' % REPO
        old_done = '<!-- loom:done v1 stage=brief item=%s#1 holder=lane-machine -->' % REPO
        found = handoff.journals([journal(20, "muse", old_defer),
                                  journal(10, "lane-machine", old_done)], AUTHORS)
        self.assertEqual([], handoff.resume_items(handoff.marks(found)))

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

    def test_an_epic_whose_tickets_are_all_closed_is_a_brain_item(self):
        routes = self.routes()
        epic = {"number": 20, "title": "Epic: the work", "state": "open",
                "labels": [{"name": "epic"}], "body": "Done when: x.\n\nPart of #1\n"}
        busy = dict(epic, number=21, title="Epic: still going")
        empty = dict(epic, number=22, title="Epic: just opened")
        closed = lambda n: {"number": n, "state": "closed",  # noqa: E731
                            "labels": [{"name": "loom:ticket"}]}
        routes[("GET", "/repos/%s/issues/1/sub_issues" % REPO)] = (200, [epic, busy, empty])
        routes[("GET", "/repos/%s/issues/20/sub_issues" % REPO)] = (200, [closed(30), closed(31)])
        routes[("GET", "/repos/%s/issues/21/sub_issues" % REPO)] = (
            200, [closed(32), {"number": 33, "state": "open", "labels": [{"name": "loom:ticket"}]}])
        out = brain.resume(GitHub("t", transport=Routes(routes)), [REPO],
                           "lane-machine", LOCK_REPO, NOW, {"LOOM_BRIEF_AUTHORS": "allowed-bot"})
        section = out.split("## Finished epics (every ticket closed)")[1].split("\n## ")[0]
        self.assertIn("- resume close-epic %s#20 Epic: the work: check its done-when" % REPO,
                      section)
        self.assertIn("bus.brain close-epic %s#20 --note TEXT" % REPO, section)
        self.assertNotIn("#21", section)
        self.assertNotIn("#22", section)

    def test_the_lock_holder_gets_the_merge_and_failed_ticket(self):
        out = brain.resume(GitHub("t", transport=Routes(self.routes())), [REPO],
                           "lane-machine", LOCK_REPO, NOW, {"LOOM_BRIEF_AUTHORS": "allowed-bot"})
        self.assertIn("Another brain journaled in the last 2 hours (muse", out)
        self.assertIn("- resume merge %s#7 (deferred by muse at %s)" % (REPO, at(5)), out)
        self.assertIn("- resume failed %s#9" % REPO, out)
        self.assertIn("- stage merge: yielded by muse", out)

    def test_resume_refuses_an_empty_trust_list_before_any_read(self):
        # loom#783: with LOOM_BRIEF_AUTHORS empty, resume trusted nobody,
        # printed "no stage handoffs" on every goal and called every stage open.
        fake = Routes(self.routes())
        with self.assertRaisesRegex(ValueError, "resume trusts no login: LOOM_BRIEF_AUTHORS is empty"):
            brain.resume(GitHub("t", transport=fake), [REPO], "lane-machine", LOCK_REPO, NOW, {})
        self.assertEqual([], fake.calls)

    def test_resume_names_the_trusted_logins_and_the_journal_logins_it_skipped(self):
        # The operator journals on the goal, but the setting names only the
        # App: the goal shows no handoffs, and now also who was skipped.
        out = brain.resume(GitHub("t", transport=Routes(self.routes())), [REPO],
                           "lane-machine", LOCK_REPO, NOW,
                           {"LOOM_BRIEF_AUTHORS": "example-app[bot]"})
        self.assertIn("Trusted logins (LOOM_BRIEF_AUTHORS): example-app[bot]", out)
        goal = out.split("## %s#1 Goal" % REPO, 1)[1].split("\n## ", 1)[0]
        self.assertIn("- no stage handoffs", goal)
        self.assertIn("- skipped journals by logins not in LOOM_BRIEF_AUTHORS: allowed-bot", goal)

    def test_resume_matches_a_trusted_login_in_any_case(self):
        out = brain.resume(GitHub("t", transport=Routes(self.routes())), [REPO],
                           "lane-machine", LOCK_REPO, NOW, {"LOOM_BRIEF_AUTHORS": "Allowed-Bot"})
        self.assertIn("- resume merge %s#7 (deferred by muse at %s)" % (REPO, at(5)), out)
        self.assertNotIn("skipped journals", out)

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

    # loom#2174: a needs-brain decision on a ticket that stays loom:briefed
    # gets its own stage, so the defer is a marker and not journal prose.
    def briefed_routes(self, journals=None, **tickets):
        """self.routes() plus open loom:briefed needs-brain tickets #10 and
        #11 (or the labels given as t10/t11), and the goal journals given."""
        routes = self.routes()
        for number in (10, 11):
            labels = tickets.get("t%d" % number, [
                "loom:ticket", "loom:briefed", "needs-brain", "claim:human", "wait:operator"])
            routes[("GET", "/repos/%s/issues/%d" % (REPO, number))] = (
                200, {"state": "open", "comments": 2, "labels": [{"name": x} for x in labels]})
        if journals is not None:
            routes[("GET", "/repos/%s/issues/1/comments" % REPO)] = (
                200, [comment(j) for j in journals])
        return routes

    def resume_out(self, routes):
        fake = Routes(routes)
        out = brain.resume(GitHub("t", transport=fake), [REPO], "lane-machine",
                           LOCK_REPO, NOW, {"LOOM_BRIEF_AUTHORS": "allowed-bot"})
        return out, fake

    def test_needs_brain_defer_on_a_briefed_ticket_is_a_formal_marker(self):
        # Criterion 1: on the old code the stage did not exist, so the
        # decision lived only in journal prose.
        marker = brain.defer(GitHub("t", transport=Routes(self.briefed_routes())),
                             "needs-brain", BRIEFED, "muse", note="decision needs review")
        self.assertEqual('<!-- loom:defer v1 stage=needs-brain item=%s#10 rev=briefed-c2 '
                         'holder=muse note="decision needs review" -->' % REPO, marker)
        found = handoff.marks(handoff.journals([journal(5, "muse", marker)], AUTHORS))
        self.assertEqual([("defer", "needs-brain", BRIEFED, "briefed-c2", "decision needs review")],
                         [(m.kind, m.stage, m.item, m.rev, m.note) for m in found])

    def test_resume_lists_a_needs_brain_defer_once_with_its_newest_reason(self):
        # Criterion 2: the next resume shows ticket, stage and reason; a
        # repeated defer (same tick, or a later tick with a new reason)
        # replaces the older one instead of adding a second entry.
        first = handoff.defer_marker("needs-brain", BRIEFED, "briefed-c2", "muse",
                                     note="decision needs review")
        again = handoff.defer_marker("needs-brain", BRIEFED, "briefed-c2", "muse",
                                     note="split or close")
        out, _ = self.resume_out(self.briefed_routes([
            journal(35, "muse", first, first), journal(5, "muse", again, again)]))
        self.assertIn("- stage needs-brain: yielded by muse at %s" % at(5), out)
        self.assertIn("- resume needs-brain %s#10: split or close (deferred by muse at %s)"
                      % (REPO, at(5)), out)
        lines = [x for x in out.splitlines() if "needs-brain %s#10" % REPO in x]
        self.assertEqual(1, len(lines), lines)

    def test_recording_a_needs_brain_defer_leaves_the_ticket_alone(self):
        # Criterion 3: defer and the resume that lists it only read: no label
        # change (needs-brain, claim and wait holds stay), no state change, no
        # lane start, no merge, no comment.
        routes = self.briefed_routes()
        before = json.dumps(routes[("GET", "/repos/%s/issues/10" % REPO)], sort_keys=True)
        fake = Routes(routes)
        marker = brain.defer(GitHub("t", transport=fake), "needs-brain", BRIEFED, "muse",
                             note="decision needs review")
        routes[("GET", "/repos/%s/issues/1/comments" % REPO)] = (
            200, [comment(journal(5, "muse", marker))])
        out, read = self.resume_out(routes)
        self.assertIn("- resume needs-brain %s#10" % REPO, out)
        for calls in (fake.calls, read.calls):
            self.assertEqual([], [c for c in calls if c[0] != "GET" and c[1] != "/graphql"])
        self.assertEqual([], [p for p in fake.posted + read.posted if p[0] != "/graphql"])
        self.assertEqual(before, json.dumps(routes[("GET", "/repos/%s/issues/10" % REPO)],
                                            sort_keys=True))

    def test_needs_brain_defer_refuses_a_ticket_that_is_not_briefed_with_needs_brain(self):
        # Criterion 4: briefed without needs-brain, needs-brain without
        # briefed, and a failed ticket are all refused by this stage.
        for labels in (["loom:ticket", "loom:briefed"], ["loom:ticket", "needs-brain"],
                       ["loom:ticket", "loom:failed", "needs-brain"]):
            with self.subTest(labels=labels):
                fake = Routes(self.briefed_routes(t10=labels))
                with self.assertRaisesRegex(ValueError, "loom:briefed ticket with needs-brain"):
                    brain.defer(GitHub("t", transport=fake), "needs-brain", BRIEFED, "muse",
                                note="decision needs review")
                self.assertEqual([], fake.posted)

    def test_failed_defer_still_takes_only_a_failed_ticket(self):
        # Criterion 4: the failed stage is unchanged by the new one.
        line = brain.defer(GitHub("t", transport=Routes(self.briefed_routes())), "failed",
                           TICKET, "muse")
        self.assertEqual("<!-- loom:defer v1 stage=failed item=%s#9 rev=failed-c2 holder=muse -->"
                         % REPO, line)
        with self.assertRaisesRegex(ValueError, "a failed item is a loom:failed ticket"):
            brain.defer(GitHub("t", transport=Routes(self.briefed_routes())), "failed",
                        BRIEFED, "muse")

    def test_done_resolves_a_needs_brain_defer_and_keeps_the_others(self):
        # Criterion 5: the done marker from `bus.brain done` retires this
        # ticket's defer; the failed defer and another ticket's needs-brain
        # defer stay standing.
        args = brain.parser().parse_args(["done", "%s#10" % REPO, "--stage", "needs-brain",
                                          "--holder", "lane-machine"])
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            brain.COMMANDS["done"](None, args)
        resolved = printed.getvalue().strip()
        self.assertEqual("<!-- loom:done v1 stage=needs-brain item=%s#10 holder=lane-machine -->"
                         % REPO, resolved)
        other = (REPO, 11)
        standing = [journal(35, "muse",
                            handoff.defer_marker("needs-brain", BRIEFED, "briefed-c2", "muse",
                                                 note="decision needs review"),
                            handoff.defer_marker("needs-brain", other, "briefed-c2", "muse",
                                                 note="pick a lane"),
                            defer(TICKET, "failed-c2", stage="failed"))]
        out, _ = self.resume_out(self.briefed_routes(standing))
        self.assertIn("- resume needs-brain %s#10: decision needs review" % REPO, out)
        out, _ = self.resume_out(self.briefed_routes(
            standing + [journal(5, "lane-machine", resolved)]))
        self.assertNotIn("- resume needs-brain %s#10" % REPO, out)
        self.assertIn("- stage needs-brain: held by lane-machine; last done %s#10" % REPO, out)
        self.assertIn("- resume needs-brain %s#11: pick a lane" % REPO, out)
        self.assertIn("- resume failed %s#9" % REPO, out)

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
                        note="checkout fix")

    def test_defer_brief_writes_marker_listed_by_resume_and_dropped_by_done(self):
        routes = self.routes()
        marker = brain.defer(GitHub("t", transport=Routes(routes)), "brief",
                             (REPO, 1), "muse", note="checkout fix")
        self.assertIn(" key=", marker)
        self.assertEqual(
            '<!-- loom:defer v1 stage=brief item=%s#1 rev=open key=345c819b holder=muse note="checkout fix" -->' % REPO,
            marker)
        routes[("GET", "/repos/%s/issues/1/comments" % REPO)] = (200, [
            comment(journal(10, "muse", marker)),
        ])
        out = brain.resume(GitHub("t", transport=Routes(routes)), [REPO],
                           "lane-machine", LOCK_REPO, NOW,
                           {"LOOM_BRIEF_AUTHORS": "allowed-bot"})
        self.assertIn("- resume brief %s#1: checkout fix (deferred by muse at %s)" % (
            REPO, at(10)), out)

        done_line = handoff.done_marker("brief", (REPO, 1), "lane-machine", key="345c819b")
        routes[("GET", "/repos/%s/issues/1/comments" % REPO)] = (200, [
            comment(journal(10, "muse", marker)),
            comment(journal(5, "lane-machine", done_line)),
        ])
        out_after_done = brain.resume(GitHub("t", transport=Routes(routes)), [REPO],
                                      "lane-machine", LOCK_REPO, NOW,
                                      {"LOOM_BRIEF_AUTHORS": "allowed-bot"})
        self.assertNotIn("- resume brief", out_after_done)

    def test_brief_defers_on_one_goal_resume_independently_by_key(self):
        routes = self.routes()
        first = handoff.defer_marker("brief", (REPO, 1), "open", "muse",
                                     note="first problem", key="11111111")
        second = handoff.defer_marker("brief", (REPO, 1), "open", "muse",
                                      note="second problem", key="22222222")
        routes[("GET", "/repos/%s/issues/1/comments" % REPO)] = (200, [
            comment(journal(20, "muse", first)), comment(journal(10, "muse", second))])
        out = brain.resume(GitHub("t", transport=Routes(routes)), [REPO],
                           "lane-machine", LOCK_REPO, NOW,
                           {"LOOM_BRIEF_AUTHORS": "allowed-bot"})
        lines = [line for line in out.splitlines() if line.startswith("- resume brief")]
        self.assertEqual(2, len(lines))
        self.assertTrue(any("first problem" in line for line in lines))
        self.assertTrue(any("second problem" in line for line in lines))

        done_line = handoff.done_marker("brief", (REPO, 1), "lane-machine", key="11111111")
        routes[("GET", "/repos/%s/issues/1/comments" % REPO)] = (200, [
            comment(journal(20, "muse", first)), comment(journal(10, "muse", second)),
            comment(journal(5, "lane-machine", done_line))])
        out = brain.resume(GitHub("t", transport=Routes(routes)), [REPO],
                           "lane-machine", LOCK_REPO, NOW,
                           {"LOOM_BRIEF_AUTHORS": "allowed-bot"})
        lines = [line for line in out.splitlines() if line.startswith("- resume brief")]
        self.assertEqual(1, len(lines))
        self.assertIn("second problem", lines[0])

    def test_brief_handoff_whose_goal_home_was_closed_is_dropped_from_resume(self):
        routes = self.routes(goal_state="closed")
        marker = handoff.defer_marker("brief", (REPO, 1), "open", "muse",
                                     note="checkout fix")
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


BRIEF = ("## Why\nwhy: the page query is slow for every visitor.\n## What\nwhat\n"
         "## Acceptance criteria\n- [ ] done\n- [ ] tested\n- [ ] live\n## Size\nS\n"
         "## Out of scope\n- none")
BRIEF_DEFER = handoff.defer_marker("brief", (REPO, 1), "open", "muse", note="checkout fix")
ENV = {"LOOM_BRIEF_AUTHORS": "allowed-bot"}


class BriefDoneTests(unittest.TestCase):
    """A brief filed under a goal whose brief stage stands deferred records
    the done marker itself, so resume stops listing the finished brief."""

    def routes(self, *journals, ticket=(201, {"id": 212, "number": 12,
                                               "html_url": "https://example.com/12"})):
        routes = ResumeCommandTests.routes(self)
        routes.update({
            ("GET", "/repos/%s/issues/1/comments" % REPO): (200, [comment(j) for j in journals]),
            ("GET", "/repos/%s/issues/1/sub_issues" % REPO): (200, []),
            ("POST", "/repos/%s/issues" % REPO): ticket,
            ("POST", "/repos/%s/issues/1/sub_issues" % REPO): (201, {}),
            ("POST", "/repos/%s/issues/1/comments" % REPO): (201, {}),
            # The journal writer looks for its own journal of the day first.
            ("GET", "/user"): (200, {"login": "allowed-bot"}),
            # Epic #3 under goal #1: briefs file under an open epic.
            ("GET", "/repos/%s/issues/3" % REPO): (200, {
                "number": 3, "id": 103, "state": "open", "title": "Epic: the work",
                "labels": [{"name": "epic"}], "body": "Done when: done.\n\nPart of #1\n"}),
            ("GET", "/repos/%s/issues/3/sub_issues" % REPO): (200, []),
            ("POST", "/repos/%s/issues/3/sub_issues" % REPO): (201, {}),
        })
        return routes

    def journals_posted(self, fake):
        return [body["body"] for path, body in fake.posted
                if path == "/repos/%s/issues/1/comments" % REPO]

    def test_a_brief_under_a_standing_brief_defer_retires_it_in_resume(self):
        routes = self.routes(journal(10, "muse", BRIEF_DEFER))
        fake = Routes(routes)
        url = brain.brief(GitHub("t", transport=fake), REPO, 3, "Ticket", BRIEF,
                          holder="lane-machine", env=ENV)
        self.assertEqual("https://example.com/12", url)
        [posted] = self.journals_posted(fake)
        self.assertTrue(posted.startswith(handoff.JOURNAL_MARKER))
        self.assertIn(handoff.done_marker("brief", (REPO, 1), "lane-machine"), posted)
        self.assertIn("https://example.com/12", posted)
        self.assertEqual("lane-machine", handoff.journal_holder({"body": posted}))

        # Create a mock journal with the done marker at test's NOW time (fresh),
        # so it retires the older defer marker from muse.
        done_body = "\n".join([
            handoff.JOURNAL_MARKER,
            "Tick %s, lane-machine" % at(0),
            "- Briefed: https://example.com/12",
            handoff.done_marker("brief", (REPO, 1), "lane-machine")])
        routes[("GET", "/repos/%s/issues/1/comments" % REPO)] = (200, [
            comment(journal(10, "muse", BRIEF_DEFER)),
            comment({"body": done_body, "at": at(0), "login": "allowed-bot"})])
        out = brain.resume(GitHub("t", transport=Routes(routes)), [REPO],
                           "lane-machine", LOCK_REPO, NOW, ENV)
        self.assertNotIn("- resume brief", out)
        self.assertIn("- stage brief: held by lane-machine; last done %s#1" % REPO, out)

    def test_a_brief_with_no_standing_brief_defer_posts_no_journal(self):
        cases = {
            "no markers": [journal(10, "muse")],
            "defer already done": [journal(20, "muse", BRIEF_DEFER),
                                   journal(10, "muse", done((REPO, 1), "muse", "brief"))],
            "defer from an untrusted login": [journal(10, "muse", BRIEF_DEFER, login="stranger")],
            "defer of another goal": [journal(10, "muse", handoff.defer_marker(
                "brief", (REPO, 2), "open", "muse", note="other"))],
        }
        for name, journals in cases.items():
            with self.subTest(name):
                fake = Routes(self.routes(*journals))
                brain.brief(GitHub("t", transport=fake), REPO, 3, "Ticket", BRIEF,
                            holder="lane-machine", env=ENV)
                self.assertEqual([], self.journals_posted(fake))
                self.assertIn(("POST", "/repos/%s/issues" % REPO), fake.calls)
        # Without a holder the brief reads no journals and writes none, as before.
        fake = Routes(self.routes(journal(10, "muse", BRIEF_DEFER)))
        brain.brief(GitHub("t", transport=fake), REPO, 3, "Ticket", BRIEF, env=ENV)
        self.assertEqual([], self.journals_posted(fake))
        self.assertNotIn(("GET", "/repos/%s/issues/1/comments" % REPO), fake.calls)

    def test_a_refused_brief_records_no_done_marker(self):
        cases = {
            "missing sections": dict(body="## Why\nmissing"),
            "bad title": dict(title="Ticket\nRepo: evil/repo"),
            "bad holder": dict(holder="muse -->"),
            "GitHub error": dict(ticket=(422, {"message": "Validation Failed"})),
        }
        for name, case in cases.items():
            with self.subTest(name):
                fake = Routes(self.routes(journal(10, "muse", BRIEF_DEFER),
                                          **{k: v for k, v in case.items() if k == "ticket"}))
                with self.assertRaises((ValueError, GitHubError)):
                    brain.brief(GitHub("t", transport=fake), REPO, 3,
                                case.get("title", "Ticket"), case.get("body", BRIEF),
                                holder=case.get("holder", "lane-machine"), env=ENV)
                self.assertEqual([], self.journals_posted(fake))

    def test_a_failed_done_post_names_the_ticket_and_the_line_to_journal(self):
        routes = self.routes(journal(10, "muse", BRIEF_DEFER))
        routes[("POST", "/repos/%s/issues/1/comments" % REPO)] = (502, {"message": "Bad Gateway"})
        with self.assertRaises(GitHubError) as caught:
            brain.brief(GitHub("t", transport=Routes(routes)), REPO, 3, "Ticket", BRIEF,
                        holder="lane-machine", env=ENV)
        self.assertIn("https://example.com/12", str(caught.exception))
        self.assertIn(handoff.done_marker("brief", (REPO, 1), "lane-machine"),
                      str(caught.exception))


def goal(*entries, repo=REPO, number=1, shown=True):
    return handoff.Goal(repo, number, shown, handoff.journals(list(entries), AUTHORS))


def packet(goals, record=None, now=NOW, public=lambda repo: True):
    return handoff.packet(goals, record, ("c" * 40, ["- Write ASCII only."]),
                          now, public)


class HandoffPacketTests(unittest.TestCase):
    def test_a_defer_from_a_brain_quiet_for_two_hours_is_open_work_for_any_brain(self):
        stale = goal(journal(180, "muse", defer(PR, "pr-abc")),
                     journal(10, "lane-machine", defer(TICKET, "failed-c3",
                                                       holder="lane-machine", stage="failed")))
        text = packet([stale])
        self.assertIn("## Open defers (2, 1 from brains no longer ticking)", text)
        merge = next(line for line in text.splitlines() if line.startswith("- merge %s#7" % REPO))
        self.assertIn("Open work for any brain: muse last ticked at %s" % at(180), merge)
        failed = next(line for line in text.splitlines() if line.startswith("- failed %s#9" % REPO))
        self.assertNotIn("Open work for any brain", failed)

    def test_a_defer_whose_brain_never_ticked_is_open_work_for_any_brain(self):
        text = packet([goal(journal(10, "muse", defer(PR, "pr-abc", holder="ghost")))])
        self.assertIn("## Open defers (1, 1 from brains no longer ticking)", text)
        self.assertIn("deferred by ghost at %s. Open work for any brain: ghost last "
                      "ticked never, over 2 hours ago." % at(10), text)

    def test_a_done_marker_for_the_item_drops_the_standing_defer(self):
        before = goal(journal(180, "muse", defer(PR, "pr-abc")))
        self.assertIn("- merge %s#7, deferred by muse" % REPO, packet([before]))
        after = goal(journal(180, "muse", defer(PR, "pr-abc")),
                     journal(5, "lane-machine", done(PR)))
        text = packet([after])
        self.assertNotIn("- merge %s#7, deferred" % REPO, text)
        self.assertIn("## Open defers (0, 0 from brains no longer ticking)\n- none", text)

    def test_a_brief_done_drops_only_the_brief_defer_with_its_key(self):
        home = (REPO, 1)
        first = handoff.defer_marker("brief", home, "open", "muse", note="one", key="aaaaaaaa")
        second = handoff.defer_marker("brief", home, "open", "muse", note="two", key="bbbbbbbb")
        finished = handoff.done_marker("brief", home, "lane-machine", key="aaaaaaaa")
        text = packet([goal(journal(60, "muse", first, second),
                            journal(5, "lane-machine", finished))])
        self.assertIn("## Open defers (1,", text)
        self.assertIn(": two.", text)
        self.assertNotIn(": one", text)

    def test_a_done_marker_on_another_goal_home_drops_the_defer_too(self):
        text = packet([goal(journal(180, "muse", defer(PR, "pr-abc"))),
                       goal(journal(5, "lane-machine", done(PR)), number=2)])
        self.assertNotIn("deferred by muse", text)

    def test_the_last_five_ticks_newest_first_with_holder_model_and_decision(self):
        entries = [journal(m, "muse" if m % 20 else "lane-machine", "- Model: model-x")
                   for m in (70, 60, 50, 40, 30, 20)]
        entries[-1]["body"] += "\n- Decided: merged the ready pull request\n" + defer(PR, "pr-abc")
        text = packet([goal(*entries)])
        ticks = text.split("## Last 5 ticks\n", 1)[1].split("\n\n", 1)[0].splitlines()
        self.assertEqual(5, len(ticks))
        self.assertTrue(ticks[0].startswith("- %s lane-machine, model model-x, on %s#1: "
                                            "0 done, 1 deferred." % (at(20), REPO)), ticks[0])
        self.assertIn("Decided: merged the ready pull request.", ticks[0])
        self.assertIn("Left open: merge %s#7." % REPO, ticks[0])
        self.assertNotIn(at(70), text)

    def test_a_tick_with_no_journal_since_its_lock_shows_its_step(self):
        record = {"holder": "muse", "started_at": at(3), "step": "merge", "step_index": 3,
                  "steps": list(brain.TICK_STEPS),
                  "notes": [{"ts": at(2), "step": "merge", "text": "two ready"}]}
        text = packet([goal(journal(40, "muse"))], record)
        self.assertIn("- %s muse, no journal yet: at step merge (3/11). Last note: two ready."
                      % at(3), text)

    def test_a_private_goal_home_is_neither_named_nor_quoted(self):
        secret = goal(journal(5, "muse", "- Decided: something private",
                              handoff.defer_marker("brief", ("example-owner/hidden", 4), "open",
                                                   "muse", note="private plan")),
                      repo="example-owner/hidden", number=4, shown=False)
        text = packet([secret], public=lambda repo: False)
        self.assertNotIn("hidden", text)
        self.assertNotIn("private plan", text)
        self.assertNotIn("something private", text)
        self.assertIn("issue 4 of a private repo", text)

    def test_quoted_text_drops_at_signs_and_non_ascii(self):
        self.assertEqual("ping someone ok?", handoff.quote("ping @someone ok\u2014"))

    def test_the_packet_does_not_move_with_the_clock_alone(self):
        found = [goal(journal(30, "muse", defer(PR, "pr-abc")))]
        self.assertEqual(packet(found), packet(found, now=NOW + timedelta(minutes=30)))

    def test_a_compact_journal_gives_the_same_packet(self):
        entries = [journal(200, "muse", "Some long prose " * 40, "- Model: model-x",
                           defer(PR, "pr-abc")),
                   journal(30, "lane-machine", done(PR), "## Heading", "First line here."),
                   journal(10, "lane-machine", "- Decided: briefed one", defer(TICKET, "c3",
                                                                              stage="failed"))]
        full = goal(*entries)
        small = handoff.Goal(full.repo, full.number, full.shown,
                             [handoff.compact(j) for j in full.journals])
        self.assertEqual(packet([full]), packet([small]))
        self.assertLess(len(small.journals[0]["body"]), len(full.journals[0]["body"]))

    def test_rules_section_reads_to_the_next_heading(self):
        doc = "# Doc\n\n## Tick\n1. Lock.\n\n## Rules\n\n- One.\n  more.\n- Two.\n\n## After\nx\n"
        self.assertEqual(["- One.", "  more.", "- Two."], handoff.rules_section(doc))
        self.assertEqual([], handoff.rules_section("# Doc\n"))


if __name__ == "__main__":
    unittest.main()


class StoredPacketTests(unittest.TestCase):
    """handoff.stored_packet: the packet rendered from the dashboard's brain
    memory, for a public comment."""

    STORED = {"ticks": [{"holder": "muse", "lane": "brain", "model": "model-x",
                         "started": at(30), "ended": at(10), "outcome": "ok",
                         "decided": ["shipped something private"],
                         "left_open": ["brief example-owner/hidden#4: private plan"],
                         "handed_off": []}],
              "commitments": [{"tick_id": 2, "holder": "muse", "item": "example-owner/hidden#4",
                               "stage": "brief", "what": "brief: private plan", "due": at(-90)},
                              {"tick_id": 2, "holder": "muse", "item": "%s#7" % REPO,
                               "stage": "merge", "what": "merge: wait", "due": None}],
              "rules": [{"source": "example-owner/hidden#2", "source_ref": "comment 55",
                         "text": "secret rule"},
                        {"source": "docs/MUSE.md", "source_ref": "d" * 40, "text": "- one"}]}

    def render(self, stored, public=lambda repo: repo == REPO):
        return handoff.stored_packet(stored, ("c" * 40, ["- Write ASCII only."]), public)

    def test_a_private_item_or_rules_source_is_neither_named_nor_quoted(self):
        text = self.render(self.STORED)
        self.assertNotIn("hidden", text)
        self.assertNotIn("private plan", text)
        self.assertNotIn("something private", text)
        self.assertNotIn("secret rule", text)
        self.assertNotIn("comment 55", text)
        self.assertIn("- brief issue 4 of a private repo, from muse's tick 2, due %s." % at(-90), text)
        self.assertIn("- merge %s#7, from muse's tick 2, due none." % REPO, text)
        self.assertIn("- issue 2 of a private repo\n- docs/MUSE.md at %s" % ("d" * 40), text)
        self.assertIn("1 decided, 1 left open, 0 handed off.", text)

    def test_open_commitments_past_the_cap_are_counted(self):
        many = dict(self.STORED, commitments=[dict(self.STORED["commitments"][1], tick_id=n)
                                              for n in range(handoff.PACKET_DEFERS + 2)])
        text = self.render(many)
        self.assertIn("## Open commitments (%d)" % (handoff.PACKET_DEFERS + 2), text)
        self.assertIn("- and 2 more; `resume` lists them all.", text)

    def test_a_stored_name_that_fails_the_posting_checks_is_not_quoted(self):
        tick = dict(self.STORED["ticks"][0], holder="@someone")
        text = self.render(dict(self.STORED, ticks=[tick]))
        self.assertNotIn("@", text)
