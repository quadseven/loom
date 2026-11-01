"""The station reports an overdue Loom review and unchecked guards (bus/review_debt.py)."""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import review_debt as rd, station  # noqa: E402
from bus.github import GitHubError  # noqa: E402

REPO = "example-owner/factory"
GOAL = 30
NOW = 1_900_000_000.0
DAY = 24 * 3600.0
AUTHOR = "trusted-example"
HEAD = "e1e2e3e4" + "0" * 32
MERGE = "d1d2d3d4" + "0" * 32


def stamp(at):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))


def journal(ago_days, body="Loom review, duty 7, first pass.", login=AUTHOR):
    return {"id": int(ago_days * 10) + 1, "created_at": stamp(NOW - ago_days * DAY), "body": body,
            "user": {"login": login}}


class Reply:
    def __init__(self, data):
        self.data = data
        self.status = 200


class FakeGitHub:
    def __init__(self):
        self.goal_comments = []
        self.tickets = []
        self.timelines = {}
        self.pulls = {}
        self.files = {}
        self.broken = set()
        self.off_branch = set()
        self.lost = set()
        self.calls = []

    def paginate(self, path):
        self.calls.append(path)
        if "comments" in self.broken and "/comments" in path:
            raise GitHubError("no", status=502)
        if "tickets" in self.broken and "labels=loom:factory" in path:
            raise GitHubError("no", status=502)
        return list(self.goal_comments if "/comments" in path else self.tickets)

    def rest(self, method, path, body=None, ok=()):
        self.calls.append(path)
        if "pull" in self.broken and "/pulls/" in path:
            raise GitHubError("no", status=502)
        if "timeline" in self.broken and "/timeline" in path:
            raise GitHubError("no", status=502)
        if "/compare/" in path:
            sha = path.split("...", 1)[1].split("?")[0]
            if sha in self.lost:
                raise GitHubError("no", status=502)
            return Reply({"status": "diverged" if sha in self.off_branch else "behind"})
        if "/timeline" in path:
            number = int(path.split("/issues/")[1].split("/")[0])
            return Reply(self.timelines.get(number, []) if "page=1" in path else [])
        if path.endswith("/files?per_page=100"):
            return Reply(self.files[int(path.split("/pulls/")[1].split("/")[0])])
        return Reply(self.pulls[int(path.rsplit("/", 1)[1])])

    def guard(self, number, state="open", reason=None, closed=NOW - DAY):
        self.tickets.append({
            "number": number, "state": state, "state_reason": reason,
            "created_at": stamp(NOW - 6 * DAY), "updated_at": stamp(NOW - DAY),
            "closed_at": stamp(closed) if state == "closed" else None,
            "body": "## What\n\nThe guard: a check.\n"})

    def delivered(self, number, pull, paths):
        self.pulls[pull] = {"number": pull, "merged_at": stamp(NOW - 2 * DAY), "body":
                            "Refs %s#%d\n" % (REPO, number),
                            "merge_commit_sha": "%040x" % pull,
                            "head": {"sha": HEAD}, "base": {"repo": {
                                "full_name": REPO, "default_branch": "main"}}}
        self.files[pull] = [{"filename": path, "status": "modified"} for path in paths]
        self.timelines.setdefault(number, []).append({
            "event": "cross-referenced", "created_at": stamp(NOW - 3 * DAY),
            "source": {"issue": {"number": pull, "repository": {"full_name": REPO},
                                 "pull_request": {"merged_at": stamp(NOW - 2 * DAY)}}}})


def run(gh, state=None, now=NOW):
    state = {} if state is None else state
    rd.run_pass(gh, REPO, GOAL, state, now, [AUTHOR], lambda line: None)
    return state


class Review(unittest.TestCase):
    def test_no_entry_in_eight_days_is_overdue(self):
        gh = FakeGitHub()
        gh.goal_comments = [journal(8)]
        self.assertEqual(rd.review_at(run(gh)), NOW - 8 * DAY)
        self.assertIn("last review 8 days ago", rd.line(run(gh), NOW))

    def test_only_a_trusted_review_entry_counts(self):
        gh = FakeGitHub()
        gh.goal_comments = [journal(1, "Brain tick, nothing about a Loom review"),
                            journal(1.5, "Loom review by a stranger", login="stranger"),
                            journal(1.2, "Loom review by an unlisted App", login="some-app[bot]"),
                            journal(9, "## Loom review - one run sample", login=AUTHOR)]
        self.assertEqual(rd.review_at(run(gh)), NOW - 9 * DAY)

    def test_no_entry_at_all_says_so_and_is_overdue(self):
        state = run(FakeGitHub())
        self.assertEqual(rd.review_at(state), NOW - rd.WINDOW)
        self.assertIn("no review in 14 days or more", rd.line(state, NOW))

    def test_a_failed_read_says_unknown_and_keeps_what_it_knew(self):
        gh = FakeGitHub()
        gh.broken.add("comments")
        state = run(gh)
        self.assertIsNone(rd.review_at(state))
        self.assertIn("last review unknown", rd.line(state, NOW))
        gh = FakeGitHub()
        gh.goal_comments = [journal(8)]
        state = run(gh)
        gh.broken.add("comments")
        run(gh, state, NOW + rd.EVERY)
        self.assertEqual(rd.review_at(state), NOW - 8 * DAY)
        self.assertIn("last review unknown", rd.line(state, NOW + rd.EVERY))

    def test_a_later_pass_reads_only_what_is_new_and_runs_hourly(self):
        gh = FakeGitHub()
        gh.goal_comments = [journal(8)]
        state = run(gh)
        gh.calls.clear()
        run(gh, state, NOW + 60)
        self.assertEqual(gh.calls, [])
        gh.goal_comments = [journal(0)]
        run(gh, state, NOW + rd.EVERY)
        self.assertEqual(rd.review_at(state), NOW)


class Guards(unittest.TestCase):
    def test_a_guard_closed_with_no_test_bearing_pull_is_flagged(self):
        gh = FakeGitHub()
        gh.guard(1, "closed", "completed")
        gh.guard(2, "closed", "completed")
        gh.delivered(2, 12, ["docs/a.md", "bus/a.py"])
        gh.guard(3, "closed", "completed")
        gh.delivered(3, 13, ["bus/a.py", "tests/test_a.py"])
        state = run(gh)
        self.assertEqual(rd.no_check(state), [1, 2])
        self.assertIn("1 shipped", rd.line(state, NOW))
        self.assertIn("2 closed with no check", rd.line(state, NOW))

    def test_counts_by_open_shipped_and_age(self):
        gh = FakeGitHub()
        gh.guard(1)
        gh.guard(2)
        gh.delivered(2, 12, ["tests/test_a.py"])
        gh.guard(3, "closed", "not_planned")
        gh.tickets.append({"number": 4, "state": "open", "body": "No such line.",
                           "created_at": stamp(NOW), "updated_at": stamp(NOW)})
        said = rd.line(run(gh), NOW)
        self.assertIn("guards 1 open, 1 shipped", said)
        self.assertIn("oldest open 6 days", said)
        self.assertEqual(rd.no_check(run(gh)), [])

    def test_a_failed_read_is_unknown_never_a_clean_count(self):
        gh = FakeGitHub()
        gh.broken.add("tickets")
        self.assertIn("guards unknown", rd.line(run(gh), NOW))
        gh = FakeGitHub()
        gh.guard(1, "closed", "completed")
        gh.delivered(1, 11, ["tests/test_a.py"])
        gh.broken.add("pull")
        state = run(gh)
        self.assertIn("guards unknown", rd.line(state, NOW))
        self.assertEqual(rd.no_check(state), [])

    def test_a_flagged_guard_stays_flagged_while_its_reread_fails(self):
        gh = FakeGitHub()
        gh.guard(1, "closed", "completed")
        state = run(gh)
        self.assertEqual(rd.no_check(state), [1])
        gh.tickets[0]["updated_at"] = stamp(NOW)
        gh.broken.add("timeline")
        run(gh, state, NOW + rd.EVERY)
        self.assertEqual(rd.no_check(state), [1])
        self.assertIn("guards unknown", rd.line(state, NOW + rd.EVERY))

    def test_a_truncated_file_list_is_no_check_unless_a_test_is_found(self):
        gh = FakeGitHub()
        gh.guard(1, "closed", "completed")
        gh.delivered(1, 11, ["bus/%d.py" % n for n in range(100)])
        self.assertEqual(rd.no_check(run(gh)), [1])

    def test_a_truncated_timeline_is_no_check_unless_a_test_is_found(self):
        gh = FakeGitHub()
        gh.guard(1, "closed", "completed")
        gh.timelines[1] = [{"event": "labeled"}] * 100
        real = gh.rest

        def rest(method, path, body=None, ok=()):
            if "/timeline" in path:
                gh.calls.append(path)
                return Reply(gh.timelines[1])
            return real(method, path, body, ok)
        gh.rest = rest
        self.assertEqual(rd.no_check(run(gh)), [1])

    def test_a_removed_test_file_is_no_check(self):
        gh = FakeGitHub()
        gh.guard(1, "closed", "completed")
        gh.delivered(1, 11, ["tests/test_a.py"])
        gh.files[11][0]["status"] = "removed"
        self.assertEqual(rd.no_check(run(gh)), [1])

    def test_a_pull_off_the_default_branch_is_no_check(self):
        gh = FakeGitHub()
        gh.guard(1, "closed", "completed")
        gh.delivered(1, 11, ["tests/test_a.py"])
        gh.off_branch.add("%040x" % 11)
        self.assertEqual(rd.no_check(run(gh)), [1])

    def test_a_placement_that_cannot_be_read_is_unknown(self):
        gh = FakeGitHub()
        gh.guard(1, "closed", "completed")
        gh.delivered(1, 11, ["tests/test_a.py"])
        gh.lost.add("%040x" % 11)
        state = run(gh)
        self.assertEqual(rd.no_check(state), [])
        self.assertIn("guards unknown", rd.line(state, NOW))

    def test_reads_are_charged_as_made_so_small_tickets_all_fit(self):
        gh = FakeGitHub()
        for number in range(1, 13):
            gh.guard(number)
        state = run(gh)
        self.assertNotIn("unknown", rd.line(state, NOW))

    def test_unreadable_tickets_do_not_hide_the_others_and_are_named(self):
        gh = FakeGitHub()
        for number in range(1, 40):
            gh.guard(number)
        real = gh.rest

        def rest(method, path, body=None, ok=()):
            if "/timeline" in path and int(path.split("/issues/")[1].split("/")[0]) <= 3:
                gh.calls.append(path)
                raise GitHubError("no", status=502)
            return real(method, path, body, ok)
        gh.rest = rest
        state = {}
        for n in range(rd.GUARD_TRIES + 1):
            run(gh, state, NOW + n * rd.EVERY)
        self.assertEqual(rd.unreadable(state), [1, 2, 3])
        rows = state["guards"]
        self.assertTrue(all(rows[str(n)].get("fresh") for n in range(4, 40)))
        self.assertEqual(rows["1"]["fails"], rd.GUARD_TRIES + 1)
        self.assertIn("tried", rows["1"])

    def test_a_ticket_rewritten_from_an_issue_keeps_that_issues_pull(self):
        gh = FakeGitHub()
        gh.guard(1, "closed", "completed")
        gh.delivered(1, 11, ["tests/test_a.py"])
        gh.pulls[11]["body"] = "Refs %s#7\n" % REPO
        gh.tickets[0]["body"] += "\nRewritten from issue 7.\n"
        self.assertEqual(rd.no_check(run(gh)), [])

    def test_a_ticket_unchanged_since_it_was_read_is_not_read_again(self):
        gh = FakeGitHub()
        gh.guard(1, "closed", "completed")
        gh.delivered(1, 11, ["tests/test_a.py"])
        state = run(gh)
        gh.calls.clear()
        run(gh, state, NOW + rd.EVERY)
        self.assertFalse([c for c in gh.calls if "/timeline" in c])

    def test_a_pass_stays_inside_its_read_budget_and_later_ones_finish(self):
        gh = FakeGitHub()
        for number in range(1, 60):
            gh.guard(number, "closed", "completed")
            gh.delivered(number, 1000 + number, ["tests/test_a.py"])
        state = run(gh)
        self.assertLessEqual(len(gh.calls), rd.READS_PER_PASS + 2)
        self.assertIn("guards unknown", rd.line(state, NOW))
        for n in range(1, 40):
            run(gh, state, NOW + n * rd.EVERY)
        self.assertIn("59 shipped", rd.line(state, NOW + 40 * rd.EVERY))

    def test_the_budget_counts_a_tickets_worst_case(self):
        self.assertGreaterEqual(rd.TICKET_READS, 3 + 5 + 5)

    def test_the_command_reads_live_with_no_budget_on_guards(self):
        gh = FakeGitHub()
        gh.goal_comments = [journal(2)]
        for number in range(1, 20):
            gh.guard(number)
            gh.delivered(number, 1000 + number, ["tests/test_a.py"])
        said = rd.live_line(gh, "%s#%d" % (REPO, GOAL), [AUTHOR], [], NOW)
        self.assertIn("last review 2 days ago", said)
        self.assertIn("19 shipped", said)


class TargetReached(Exception):
    pass


class Carried(unittest.TestCase):
    def test_a_pass_stopped_at_the_request_target_runs_again_next_pass(self):
        gh = FakeGitHub()
        state = {}
        real = gh.paginate

        def stop(path):
            raise TargetReached()
        gh.paginate = stop
        with self.assertRaises(TargetReached):
            run(gh, state)
        gh.paginate = real
        gh.calls.clear()
        run(gh, state, NOW + 60)
        self.assertTrue(gh.calls)


class Step(unittest.TestCase):
    def test_an_unreadable_state_file_keeps_what_the_invariants_last_knew(self):
        gh = FakeGitHub()
        gh.goal_comments = [journal(8)]
        with tempfile.TemporaryDirectory() as home:
            settings = station.Settings(home, [REPO], [AUTHOR], [], status_repo=REPO,
                                        invariant_goal="%s#%d" % (REPO, GOAL))
            st = station.Station(gh, settings)
            st.now = NOW
            st.report_review_debt()
            kept = {}
            st._invariant_debt_facts(station.invariants.Facts(now=NOW), kept)
            with open(os.path.join(st.state, rd.STATE_FILE), "w") as fh:
                fh.write("{broken")
            facts = station.invariants.Facts(now=NOW)
            st._invariant_debt_facts(facts, kept)
            self.assertEqual(facts.review_at, NOW - 8 * DAY)

    def test_the_station_step_keeps_its_state_and_the_invariants_read_it(self):
        gh = FakeGitHub()
        gh.goal_comments = [journal(8)]
        with tempfile.TemporaryDirectory() as home:
            settings = station.Settings(home, [REPO], [AUTHOR], [], status_repo=REPO,
                                        invariant_goal="%s#%d" % (REPO, GOAL))
            st = station.Station(gh, settings)
            st.now = NOW
            st.report_review_debt()
            facts = station.invariants.Facts(now=NOW)
            st._invariant_debt_facts(facts, {})
            self.assertEqual(facts.review_at, NOW - 8 * DAY)
            self.assertEqual(facts.guards_no_check, [])
