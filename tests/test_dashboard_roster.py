"""Fixture-backed tests for the /roster view: every brain and the
workhorse, their recent ticks, and a cadence-relative lateness state.

Fixtures are shaped like the real records (a brain day journal carries its
newest tick first and the earlier ones under a details fold, each tick
stating a Moved line; the workhorse journal is one issue body overwritten
in place) with placeholder repos and round numbers.
"""
import os
import unittest
from datetime import datetime, timezone
from unittest import mock

from dashboard import github as dash_github
from dashboard import roster

NOW = datetime(2030, 1, 8, 12, 30, tzinfo=timezone.utc)


def journal(body, created="2030-01-08T00:05:00Z", updated=None):
    return {"body": body, "created_at": created, "updated_at": updated or created,
            "repo": "example-owner/loom", "user": {"login": "station-bot"}}


MUSE_DAY = journal("\n".join([
    "<!-- loom:journal v1 -->",
    "Tick 2030-01-08T12:17:00Z, muse",
    "Merge-first tick; two lane PRs were ready.",
    "- Moved: merged 2, briefed 1, closed 3, verified 0, deferred 1.",
    "",
    "<details><summary>Earlier ticks today</summary>",
    "",
    "Tick 2030-01-08T11:47:00Z, muse",
    "Nothing was ready to merge.",
    "- Moved: merged 0, briefed 0, closed 0, verified 0, deferred 3.",
    "</details>",
]), created="2030-01-08T11:47:00Z", updated="2030-01-08T12:17:05Z")

LANE_MACHINE_DAY = journal("\n".join([
    "<!-- loom:journal v1 -->",
    "Tick 2030-01-07T07:17:00Z, lane-machine",
    "Resume-scope tick.",
    "- Moved: merged 0, briefed 0, closed 1, verified 2, deferred 1.",
]), created="2030-01-07T00:30:00Z", updated="2030-01-07T07:17:10Z")

OPERATOR_DAY = journal("\n".join([
    "<!-- loom:journal v1 -->",
    "Tick 2030-01-08T10:30:00Z, operator-session",
    "Operator session tick.",
    "- Moved: merged 1, briefed 0, closed 0, verified 0, deferred 0.",
]))

THIRD_BRAIN_DAY = journal("\n".join([
    "<!-- loom:journal v1 -->",
    "Tick 2030-01-08T12:00:00Z, brain-c",
    "A third brain appears on its own.",
    "- Moved: merged 0, briefed 2, closed 0, verified 0, deferred 0.",
]))

WORKHORSE_BODY = "\n".join([
    "Goal journal for the workhorse session. Overwritten after every batch "
    "(at most 2 hours apart). Not a work ticket.",
    "",
    "Updated: 2030-01-08 12:01Z UTC",
    "",
    "## Counted so far",
    "- Window one (closed): final 203 counted; the target was not met.",
    "- Window two, latest 2-hour line posted 10:17Z: counted 81 (closed 35, "
    "merged 28, rewritten 18), opened 56, blocked 25, pace 13 per hour.",
])
WORKHORSE = {"ref": "example-owner/overseer#99", "body": WORKHORSE_BODY,
             "updated_at": "2030-01-08T12:01:30Z"}


def by_name(data):
    return {actor["name"]: actor for actor in data["actors"]}


class BrainRosterTest(unittest.TestCase):
    def test_holders_come_from_the_journals_not_a_list(self):
        data = roster.build_roster([MUSE_DAY, LANE_MACHINE_DAY, OPERATOR_DAY,
                                    THIRD_BRAIN_DAY], None, NOW)
        actors = by_name(data)
        self.assertEqual(set(actors), {"muse", "lane-machine", "operator-session", "brain-c"})
        self.assertEqual(actors["muse"]["role"], "primary brain")
        self.assertEqual(actors["lane-machine"]["role"], "backup brain")
        self.assertEqual(actors["brain-c"]["role"], "brain")

    def test_a_down_backup_brain_is_named_down(self):
        data = roster.build_roster([MUSE_DAY, LANE_MACHINE_DAY], None, NOW)
        actors = by_name(data)
        # 29h13m since the last lane-machine tick, on a 60-minute cadence.
        self.assertEqual(actors["lane-machine"]["status"], "down")
        self.assertEqual(actors["muse"]["status"], "ok")

    def test_ticks_are_newest_first_with_the_counts_the_journal_states(self):
        data = roster.build_roster([MUSE_DAY], None, NOW)
        ticks = by_name(data)["muse"]["ticks"]
        self.assertEqual([t["at"] for t in ticks],
                         ["2030-01-08T12:17:00Z", "2030-01-08T11:47:00Z"])
        self.assertEqual(ticks[0]["counts"],
                         {"merged": 2, "briefed": 1, "closed": 3, "verified": 0, "deferred": 1})
        self.assertIn("Merge-first tick", ticks[0]["line"])

    def test_the_same_tick_in_two_journals_counts_once(self):
        data = roster.build_roster([MUSE_DAY, dict(MUSE_DAY)], None, NOW)
        self.assertEqual(len(by_name(data)["muse"]["ticks"]), 2)

    def test_a_holder_without_a_cadence_gets_no_lateness_verdict(self):
        data = roster.build_roster([OPERATOR_DAY], None, NOW)
        actor = by_name(data)["operator-session"]
        self.assertEqual(actor["status"], "untracked")
        self.assertIsNone(actor["cadence"])

    def test_status_boundaries_follow_the_cadence(self):
        def status_at(tick_iso):
            day = journal("\n".join([
                "<!-- loom:journal v1 -->",
                "Tick %s, muse" % tick_iso,
                "- Moved: merged 0, briefed 0, closed 0, verified 0, deferred 0.",
            ]))
            return by_name(roster.build_roster([day], None, NOW))["muse"]["status"]

        # The primary brain ticks every 30 minutes: LATE past 45, DOWN past 75.
        self.assertEqual(status_at("2030-01-08T11:45:00Z"), "ok")    # exactly 45m
        self.assertEqual(status_at("2030-01-08T11:44:00Z"), "late")  # 46m
        self.assertEqual(status_at("2030-01-08T11:15:00Z"), "late")  # exactly 75m
        self.assertEqual(status_at("2030-01-08T11:14:00Z"), "down")  # 76m


class WorkhorseRosterTest(unittest.TestCase):
    def test_the_workhorse_row_reads_its_overwritten_journal(self):
        data = roster.build_roster([], WORKHORSE, NOW)
        actor = by_name(data)["workhorse"]
        self.assertEqual(actor["role"], "workhorse")
        self.assertEqual(actor["last_at"], "2030-01-08T12:01:00Z")
        self.assertEqual(actor["status"], "ok")
        tick = actor["ticks"][0]
        self.assertEqual(tick["counts"], {"closed": 35, "merged": 28, "rewritten": 18})
        self.assertEqual(tick["counted"], 81)
        self.assertEqual(tick["pace"], 13)

    def test_a_workhorse_silent_past_two_and_a_half_cycles_is_down(self):
        stale = dict(WORKHORSE, body=WORKHORSE_BODY.replace("12:01Z", "06:01Z"),
                     updated_at="2030-01-08T06:01:30Z")
        data = roster.build_roster([], stale, NOW)
        self.assertEqual(by_name(data)["workhorse"]["status"], "down")

    def test_no_workhorse_configured_no_workhorse_row(self):
        data = roster.build_roster([MUSE_DAY], None, NOW)
        self.assertNotIn("workhorse", by_name(data))

    def test_a_counted_line_without_a_pace_still_renders(self):
        body = WORKHORSE_BODY.replace(", pace 13 per hour", "")
        data = roster.build_roster([], dict(WORKHORSE, body=body), NOW)
        tick = by_name(data)["workhorse"]["ticks"][0]
        self.assertEqual(tick["counted"], 81)
        self.assertIsNone(tick["pace"])
        self.assertIn("counted 81", roster.render_roster(data))

    def test_a_configured_but_unreadable_workhorse_still_gets_a_row(self):
        broken = {"ref": "example-owner/overseer#99", "body": "", "updated_at": None}
        data = roster.build_roster([], broken, NOW)
        actor = by_name(data)["workhorse"]
        self.assertEqual(actor["status"], "untracked")
        self.assertIsNone(actor["last_at"])


class EmptyRosterTest(unittest.TestCase):
    def test_no_journals_and_no_workhorse_is_an_empty_roster_with_a_reason(self):
        data = roster.build_roster([], None, NOW)
        self.assertEqual(data["actors"], [])
        self.assertTrue(data["reason"])
        self.assertIn("No roster", roster.render_roster(data))


class RenderRosterTest(unittest.TestCase):
    def test_down_actors_render_first_and_unmissable(self):
        html = roster.render_roster(roster.build_roster(
            [MUSE_DAY, LANE_MACHINE_DAY], WORKHORSE, NOW))
        self.assertLess(html.index("lane-machine"), html.index("muse"))
        self.assertIn("actor-down", html)
        self.assertIn("DOWN", html)
        self.assertIn("merged 2", html)
        self.assertIn("counted 81", html)

    def test_ages_come_from_the_journal_timestamps(self):
        html = roster.render_roster(roster.build_roster([LANE_MACHINE_DAY], None, NOW))
        self.assertIn("1 day ago", html)

    def test_the_last_tick_date_is_shown_not_just_its_age(self):
        html = roster.render_roster(roster.build_roster([LANE_MACHINE_DAY], None, NOW))
        self.assertIn("2030-01-07 07:17Z", html)


class WorkhorseReadTest(unittest.TestCase):
    """The snapshot reads the workhorse journal issue when the deployment
    names it in LOOM_WORKHORSE_JOURNAL, the same env-driven way it reads a
    goal home; unset, it reads nothing."""

    class FakeGitHub:
        def __init__(self):
            self.paths = []

        def rest(self, method, path, **kwargs):
            self.paths.append(path)

            class Reply:
                status = 200
                headers = {}
                data = {"body": WORKHORSE_BODY, "updated_at": "2030-01-08T12:01:30Z"}
            return Reply()

    def test_the_named_journal_is_read_into_the_snapshot(self):
        fake = self.FakeGitHub()
        store = dash_github.TicketStore(repos=["example-owner/loom"], gh=fake)
        with mock.patch.dict(os.environ, {"LOOM_WORKHORSE_JOURNAL": "example-owner/overseer#99"}):
            errors = []
            found = store._read_workhorse(errors)
        self.assertEqual(errors, [])
        self.assertEqual(found["ref"], "example-owner/overseer#99")
        self.assertIn("Counted so far", found["body"])
        self.assertEqual(fake.paths, ["/repos/example-owner/overseer/issues/99"])

    def test_unset_env_reads_nothing(self):
        fake = self.FakeGitHub()
        store = dash_github.TicketStore(repos=["example-owner/loom"], gh=fake)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LOOM_WORKHORSE_JOURNAL", None)
            self.assertIsNone(store._read_workhorse([]))
        self.assertEqual(fake.paths, [])


if __name__ == "__main__":
    unittest.main()
