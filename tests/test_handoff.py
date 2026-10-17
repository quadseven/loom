from __future__ import annotations

import json
import os
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
        self.assertIn(" key=", marker)
        self.assertEqual(
            '<!-- loom:defer v1 stage=brief item=%s#1 rev=open key=c7ec7cc4 holder=muse note="party-trip fix" -->' % REPO,
            marker)
        routes[("GET", "/repos/%s/issues/1/comments" % REPO)] = (200, [
            comment(journal(10, "muse", marker)),
        ])
        out = brain.resume(GitHub("t", transport=Routes(routes)), [REPO],
                           "lane-machine", LOCK_REPO, NOW,
                           {"LOOM_BRIEF_AUTHORS": "allowed-bot"})
        self.assertIn("- resume brief %s#1: party-trip fix (deferred by muse at %s)" % (
            REPO, at(10)), out)

        done_line = handoff.done_marker("brief", (REPO, 1), "lane-machine", key="c7ec7cc4")
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


BRIEF = ("## Why\nwhy: the page query is slow for every visitor.\n## What\nwhat\n"
         "## Acceptance criteria\n- [ ] done\n- [ ] tested\n- [ ] live\n## Size\nS\n"
         "## Out of scope\n- none")
BRIEF_DEFER = handoff.defer_marker("brief", (REPO, 1), "open", "muse", note="party-trip fix")
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
        })
        return routes

    def journals_posted(self, fake):
        return [body["body"] for path, body in fake.posted
                if path == "/repos/%s/issues/1/comments" % REPO]

    def test_a_brief_under_a_standing_brief_defer_retires_it_in_resume(self):
        routes = self.routes(journal(10, "muse", BRIEF_DEFER))
        fake = Routes(routes)
        url = brain.brief(GitHub("t", transport=fake), REPO, 1, "Ticket", BRIEF,
                          holder="lane-machine", env=ENV)
        self.assertEqual("https://example.com/12", url)
        [posted] = self.journals_posted(fake)
        self.assertTrue(posted.startswith(handoff.JOURNAL_MARKER))
        self.assertIn(handoff.done_marker("brief", (REPO, 1), "lane-machine"), posted)
        self.assertIn("https://example.com/12", posted)
        self.assertEqual("lane-machine", handoff.journal_holder({"body": posted}))

        routes[("GET", "/repos/%s/issues/1/comments" % REPO)] = (200, [
            comment(journal(10, "muse", BRIEF_DEFER)),
            comment({"body": posted, "at": at(0), "login": "allowed-bot"})])
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
                brain.brief(GitHub("t", transport=fake), REPO, 1, "Ticket", BRIEF,
                            holder="lane-machine", env=ENV)
                self.assertEqual([], self.journals_posted(fake))
                self.assertIn(("POST", "/repos/%s/issues" % REPO), fake.calls)
        # Without a holder the brief reads no journals and writes none, as before.
        fake = Routes(self.routes(journal(10, "muse", BRIEF_DEFER)))
        brain.brief(GitHub("t", transport=fake), REPO, 1, "Ticket", BRIEF, env=ENV)
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
                    brain.brief(GitHub("t", transport=fake), REPO, 1,
                                case.get("title", "Ticket"), case.get("body", BRIEF),
                                holder=case.get("holder", "lane-machine"), env=ENV)
                self.assertEqual([], self.journals_posted(fake))

    def test_a_failed_done_post_names_the_ticket_and_the_line_to_journal(self):
        routes = self.routes(journal(10, "muse", BRIEF_DEFER))
        routes[("POST", "/repos/%s/issues/1/comments" % REPO)] = (502, {"message": "Bad Gateway"})
        with self.assertRaises(GitHubError) as caught:
            brain.brief(GitHub("t", transport=Routes(routes)), REPO, 1, "Ticket", BRIEF,
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

    def test_a_done_marker_for_the_item_drops_the_standing_defer(self):
        before = goal(journal(180, "muse", defer(PR, "pr-abc")))
        self.assertIn("- merge %s#7, deferred by muse" % REPO, packet([before]))
        after = goal(journal(180, "muse", defer(PR, "pr-abc")),
                     journal(5, "lane-machine", done(PR)))
        text = packet([after])
        self.assertNotIn("- merge %s#7, deferred" % REPO, text)
        self.assertIn("## Open defers (0, 0 from brains no longer ticking)\n- none", text)

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
        self.assertIn("- %s muse, no journal yet: at step merge (3/10). Last note: two ready."
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

    def test_rules_section_reads_to_the_next_heading(self):
        doc = "# Doc\n\n## Tick\n1. Lock.\n\n## Rules\n\n- One.\n  more.\n- Two.\n\n## After\nx\n"
        self.assertEqual(["- One.", "  more.", "- Two."], handoff.rules_section(doc))
        self.assertEqual([], handoff.rules_section("# Doc\n"))


if __name__ == "__main__":
    unittest.main()
