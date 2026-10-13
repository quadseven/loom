"""The factory pass (bus/factory.py) with a fake GitHub and recorded actions."""

import os
import re
import tempfile
import unittest
from datetime import datetime, timezone

from bus import factory, station
from bus.github import Response
from bus.tickets import FIX_MARKER

REPO = "example-org/example-repo"
NOW = 2_000_000_000.0
# A loom:fix comment that asked for thread PRRT_1 and a failing check.
FIX_ASKED = FIX_MARKER + "\nPull request: x#50\n\n### Check: lint\n\n    E1\n\n### Thread PRRT_1 (comment 1) at a.py:1\n\n    rename"
ME = {"login": "alice-example"}
STRANGER = {"login": "mallory-example"}


def iso(seconds_ago):
    return datetime.fromtimestamp(NOW - seconds_ago, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeGitHub:
    def __init__(self):
        self.issues = {}
        self.comments = {}
        self.events = {}
        self.pulls = {}
        self.runs = {}
        self.details = {}
        self.writes = []
        self.behind = {}
        self.attempts = {}
        self.update_status = 202

    def ticket(self, number, state, lane="codex", body="Difficulty: medium\n\n## Why\nx\n"):
        self.issues[number] = {"number": number, "state": "open", "body": body, "user": ME,
                               "labels": [{"name": "loom:ticket"}, {"name": "loom:" + state},
                                          {"name": "lane:" + lane}]}
        self.comments[number] = []
        self.events[number] = []

    def pull(self, number, ticket, created=3600, mergeable=True, green=True,
             resolved=True, runs=(("unittest", "completed"), (factory.DEFAULT_REVIEW_CHECK, "completed")),
             updated=3600):
        sha = "sha%d" % number
        self.pulls[number] = {
            "number": number, "state": "open", "draft": False, "body": "", "user": ME,
            "head": {"ref": "loom/%d-some-title" % ticket, "sha": sha,
                     "repo": {"full_name": REPO}},
            "base": {"ref": "main", "repo": {"full_name": REPO}},
            "created_at": iso(created), "updated_at": iso(updated), "mergeable": mergeable}
        self.runs[sha] = [{"name": n, "status": s} for n, s in runs]
        self.details[number] = {
            "isDraft": False, "mergeable": "MERGEABLE" if mergeable else "CONFLICTING",
            "headRefName": "loom/%d-some-title" % ticket, "headRefOid": sha,
            "headRepository": {"nameWithOwner": REPO},
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {
                "state": "SUCCESS" if green else "FAILURE"}}}]},
            "reviewThreads": {"nodes": [{"id": "PRRT_1", "isResolved": resolved,
                                         "comments": {"nodes": []}}]}}

    # -- the GitHub client surface the factory uses --------------------
    def paginate(self, path):
        if re.search(r"/issues\?labels=loom:ticket", path):
            return list(self.issues.values())
        if path.endswith("/pulls?state=open"):
            return list(self.pulls.values())
        m = re.search(r"/issues/(\d+)/comments$", path)
        if m:
            return list(self.comments[int(m.group(1))])
        m = re.search(r"/issues/(\d+)/events$", path)
        if m:
            return list(self.events[int(m.group(1))])
        raise AssertionError("unexpected paginate %s" % path)

    def rest(self, method, path, body=None, etag=None, ok=()):
        if method == "GET":
            m = re.search(r"/pulls/(\d+)$", path)
            if m:
                return Response(200, self.pulls[int(m.group(1))])
            m = re.search(r"/commits/(\w+)/check-runs", path)
            if m:
                return Response(200, {"check_runs": self.runs[m.group(1)]})
            m = re.search(r"/compare/main\.\.\.sha(\d+)$", path)
            if m:
                return Response(200, {"behind_by": self.behind.get(int(m.group(1)), 0)})
            m = re.search(r"/actions/runs/(\d+)$", path)
            if m:
                return Response(200, {"run_attempt": self.attempts.get(m.group(1), 1)})
            raise AssertionError("unexpected GET %s" % path)
        self.writes.append((method, path, body))
        if path.endswith("/update-branch"):
            return Response(self.update_status, {})
        m = re.search(r"/issues/(\d+)(/labels|/comments)?$", path)
        if m:
            number = int(m.group(1))
            if m.group(2) == "/labels":
                self.issues[number]["labels"] = [{"name": x} for x in body["labels"]]
            elif m.group(2) == "/comments":
                self.comments[number].append({"body": body["body"], "created_at": iso(0), "user": ME})
            elif method == "PATCH":
                self.issues[number]["body"] = body["body"]
        return Response(200, {})

    def graphql(self, query, variables=None):
        number = int(re.search(r"pullRequest\(number:(\d+)\)", query).group(1))
        return {"repository": {"pullRequest": self.details[number]}}


class Base(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        self.merged, self.fixed, self.moved = [], [], []

    def go(self):
        def merge(gh, repo, number, ticket):
            self.merged.append((number, ticket))
        def fix(gh, repo, number, ticket, notes):
            self.fixed.append((number, ticket, gh.issues[ticket[1]]["labels"][:]))
        def move(gh, repo, number, state, lane=None, note=None):
            self.moved.append((number, state, lane))
        f = factory.Factory(self.gh, [REPO], NOW, trusted=["Alice-Example"],
                            merge=merge, fix=fix, move=move)
        f.run()
        return f

    def lane(self, number):
        return factory._lane(self.gh.issues[number])

    def model(self, number):
        m = re.search(r"^Model: (\S+)$", self.gh.issues[number]["body"], re.M)
        return m.group(1) if m else None


class PullRequests(Base):
    def test_a_ready_pull_request_is_merged(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5)
        self.go()
        self.assertEqual(self.merged, [(50, (REPO, 5))])
        self.assertEqual(self.fixed, [])

    def test_it_waits_while_a_check_runs(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, resolved=False, runs=(("unittest", "in_progress"),
                                                  (factory.DEFAULT_REVIEW_CHECK, "completed")))
        self.go()
        self.assertEqual((self.merged, self.fixed), ([], []))

    def test_it_never_merges_without_the_review_on_the_head_commit(self):
        self.gh.ticket(5, "pr")
        for updated in (60, factory.REVIEW_GRACE + 60):
            self.gh.pull(50, 5, runs=(("unittest", "completed"),), updated=updated)
            self.go()
        self.assertEqual((self.merged, self.fixed), ([], []))

    def test_an_unreviewed_failing_pull_request_still_goes_back(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, green=False, runs=(("unittest", "completed"),))
        self.go()
        self.assertEqual([f[:2] for f in self.fixed], [(50, (REPO, 5))])

    def test_unknown_mergeability_waits(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, mergeable=None)
        self.go()
        self.assertEqual((self.merged, self.fixed), ([], []))

    def test_a_first_send_back_of_a_young_pull_request_stays_on_its_lane(self):
        self.gh.ticket(5, "pr", lane="codex")
        self.gh.pull(50, 5, resolved=False)
        self.go()
        self.assertEqual([f[:2] for f in self.fixed], [(50, (REPO, 5))])
        self.assertEqual((self.lane(5), self.model(5)), ("codex", None))
        self.assertEqual(factory.rung_of(self.gh.comments[5]), 0)

    def test_a_thread_that_survives_a_fix_round_climbs_to_the_first_rung(self):
        self.gh.ticket(5, "pr", lane="opencode-go")
        self.gh.comments[5].append({"body": FIX_ASKED, "user": ME})
        self.gh.pull(50, 5, resolved=False)
        self.go()
        self.assertEqual((self.lane(5), self.model(5)), ("claude", "claude-sonnet-5-5"))
        self.assertEqual(factory.rung_of(self.gh.comments[5]), 1)
        # The lane label changed before the fix brief went out.
        self.assertIn({"name": "lane:claude"}, self.fixed[0][2])

    def test_a_review_round_that_finds_new_things_stays_on_its_lane(self):
        self.gh.ticket(5, "pr", lane="codex")
        self.gh.comments[5].append({"body": FIX_ASKED.replace("PRRT_1", "PRRT_0").replace(
            "### Check: lint\n\n    E1\n\n", ""), "user": ME})
        self.gh.pull(50, 5, resolved=False)
        self.go()
        self.assertEqual((self.lane(5), self.model(5)), ("codex", None))
        self.assertEqual(len(self.fixed), 1)

    def test_checks_still_failing_after_a_round_that_asked_for_them_climb(self):
        self.gh.ticket(5, "pr")
        self.gh.comments[5].append({"body": FIX_ASKED, "user": ME})
        self.gh.pull(50, 5, green=False)
        self.go()
        self.assertEqual(self.model(5), "claude-sonnet-5-5")

    def test_a_conflict_alone_never_climbs(self):
        self.gh.ticket(5, "pr")
        self.gh.comments[5].append({"body": FIX_ASKED, "user": ME})
        self.gh.pull(50, 5, mergeable=False)
        self.go()
        self.assertEqual(self.model(5), None)
        self.assertEqual(len(self.fixed), 1)

    def test_a_fresh_push_without_its_review_yet_waits(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, resolved=False, runs=(), updated=60)
        self.go()
        self.assertEqual((self.merged, self.fixed), ([], []))

    def test_a_rung_zero_marker_starts_the_ladder_again(self):
        self.gh.ticket(6, "failed")
        self.gh.comments[6] += [{"body": factory.RUNG_MARKER % 3, "user": ME},
                                {"body": factory.RUNG_MARKER % 0, "user": ME}]
        self.go()
        self.assertEqual(self.model(6), "claude-sonnet-5-5")

    def test_age_climbs_once_per_window_not_every_round(self):
        self.gh.ticket(5, "pr")
        self.gh.comments[5].append({"body": factory.RUNG_MARKER % 1, "user": ME,
                                    "created_at": iso(3600)})
        self.gh.pull(50, 5, mergeable=False, created=factory.STUCK_AGE * 2)
        self.go()
        self.assertEqual(self.model(5), None, "climbed an hour after the last climb")
        self.assertEqual(len(self.fixed), 1)
        self.gh.comments[5][0]["created_at"] = iso(factory.STUCK_AGE + 60)
        self.gh.issues[5]["labels"] = [{"name": x} for x in ("loom:ticket", "loom:pr", "lane:codex")]
        self.go()
        self.assertEqual(self.model(5), "gpt-6-sol")

    def test_an_old_pull_request_climbs_on_its_first_send_back(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, mergeable=False, created=factory.STUCK_AGE + 60)
        self.go()
        self.assertEqual(self.model(5), "claude-sonnet-5-5")
        self.assertEqual(len(self.fixed), 1)

    def test_the_rungs_go_sonnet_then_sol_then_opus(self):
        self.gh.ticket(5, "pr")
        self.gh.comments[5].append({"body": FIX_ASKED, "user": ME})
        seen = []
        for _ in range(3):
            self.gh.pull(50, 5, resolved=False)
            self.gh.issues[5]["labels"] = [{"name": x} for x in
                                           ("loom:ticket", "loom:pr", "lane:" + (self.lane(5) or "codex"))]
            self.go()
            seen.append((self.lane(5), self.model(5)))
        self.assertEqual(seen, [("claude", "claude-sonnet-5-5"), ("codex", "gpt-6-sol"),
                                ("claude", "claude-opus-5-5")])

    def test_past_the_last_rung_it_is_left_failed_for_the_brain(self):
        self.gh.ticket(5, "pr")
        self.gh.comments[5] += [{"body": FIX_ASKED, "user": ME}, {"body": factory.RUNG_MARKER % 3, "user": ME}]
        self.gh.pull(50, 5, resolved=False)
        self.go()
        self.assertEqual(self.fixed, [])
        self.assertEqual(self.moved, [(5, "failed", None)])

    def test_a_ready_pull_request_behind_its_base_is_brought_up_to_date_not_merged(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5)
        self.gh.behind[50] = 3
        self.go()
        self.assertEqual(self.merged, [])
        self.assertIn(("PUT", "/repos/%s/pulls/50/update-branch" % REPO,
                       {"expected_head_sha": "sha50"}), self.gh.writes)

    def test_a_refused_update_waits_for_the_next_pass(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5)
        self.gh.behind[50] = 1
        self.gh.update_status = 422
        f = self.go()
        self.assertEqual((self.merged, self.fixed, f.actions), ([], [], 0))

    def test_each_repo_lands_one_pull_request_a_pass_oldest_first(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, created=7200)
        self.gh.ticket(6, "pr")
        self.gh.pull(60, 6, created=3600)
        self.gh.pulls = dict(reversed(list(self.gh.pulls.items())))
        self.go()
        self.assertEqual(self.merged, [(50, (REPO, 5))])

    def test_an_urgent_tickets_pull_request_lands_first(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, created=7200)
        self.gh.ticket(6, "pr")
        self.gh.issues[6]["labels"].append({"name": "loom:urgent"})
        self.gh.pull(60, 6, created=3600)
        self.go()
        self.assertEqual(self.merged, [(60, (REPO, 6))])

    def test_a_failed_actions_job_is_run_once_more_before_a_send_back(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, green=False)
        self.gh.runs["sha50"] = [
            {"name": "unittest", "status": "completed", "conclusion": "failure",
             "details_url": "https://github.com/%s/actions/runs/77/job/1" % REPO},
            {"name": factory.DEFAULT_REVIEW_CHECK, "status": "completed", "conclusion": "success"}]
        self.go()
        self.assertEqual(self.fixed, [])
        self.assertIn(("POST", "/repos/%s/actions/runs/77/rerun-failed-jobs" % REPO, None),
                      self.gh.writes)
        self.gh.attempts["77"] = 2
        self.go()
        self.assertEqual([f[:2] for f in self.fixed], [(50, (REPO, 5))])

    def test_a_failed_review_is_never_run_again(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, green=False)
        self.gh.runs["sha50"] = [
            {"name": factory.DEFAULT_REVIEW_CHECK, "status": "completed", "conclusion": "failure",
             "details_url": "https://github.com/%s/actions/runs/78/job/1" % REPO}]
        self.go()
        self.assertEqual(len(self.fixed), 1)
        self.assertFalse(any("rerun" in w[1] for w in self.gh.writes))

    def test_pull_requests_of_tickets_not_in_pr_are_left_alone(self):
        self.gh.ticket(5, "briefed")
        self.gh.pull(50, 5)
        self.gh.pulls[51] = dict(self.gh.pulls[50], number=51, head={"ref": "feature/x"})
        self.go()
        self.assertEqual((self.merged, self.fixed), ([], []))


class Trust(Base):
    def test_a_strangers_ticket_or_pull_request_is_never_touched(self):
        self.gh.ticket(5, "pr")
        self.gh.issues[5]["user"] = STRANGER
        self.gh.pull(50, 5)
        self.gh.ticket(6, "pr")
        self.gh.pull(60, 6)
        self.gh.pulls[60]["user"] = STRANGER
        self.gh.ticket(7, "failed")
        self.gh.issues[7]["user"] = STRANGER
        self.go()
        self.assertEqual((self.merged, self.fixed, self.moved), ([], [], []))

    def test_a_strangers_rung_or_fix_comment_does_not_move_the_ladder(self):
        self.gh.ticket(5, "pr")
        self.gh.comments[5] += [{"body": FIX_MARKER, "user": STRANGER},
                                {"body": factory.RUNG_MARKER % 3, "user": STRANGER}]
        self.gh.pull(50, 5, resolved=False)
        self.go()
        # Treated as a first send-back on its own lane, not past the last rung.
        self.assertEqual(len(self.fixed), 1)
        self.assertEqual((self.lane(5), self.model(5)), ("codex", None))


class Tickets(Base):
    def test_a_failed_ticket_without_a_pull_request_is_rebriefed_a_rung_up(self):
        self.gh.ticket(6, "failed", lane="zai")
        self.go()
        self.assertEqual(self.moved, [(6, "briefed", "claude")])
        self.assertEqual(self.model(6), "claude-sonnet-5-5")

    def test_failed_tickets_spread_past_a_busy_sonnet_lane(self):
        for n in (1, 2, 3, 4):
            self.gh.ticket(n, "failed", lane="zai")
        self.go()
        self.assertEqual([m[2] for m in self.moved], ["claude", "claude", "codex", "codex-2"])

    def test_a_failed_ticket_with_a_pull_request_gets_a_fix_brief(self):
        self.gh.ticket(6, "failed")
        self.gh.pull(60, 6, runs=(("unittest", "in_progress"),))
        self.go()
        self.assertEqual([f[:2] for f in self.fixed], [(60, (REPO, 6))])
        self.assertIn({"name": "loom:pr"}, self.fixed[0][2])
        self.assertNotIn({"name": "loom:failed"}, self.fixed[0][2])

    def test_a_failed_ticket_past_the_last_rung_is_left_alone(self):
        self.gh.ticket(6, "failed")
        self.gh.comments[6].append({"body": factory.RUNG_MARKER % 3, "user": ME})
        self.go()
        self.assertEqual((self.moved, self.fixed), ([], []))

    def test_a_briefed_ticket_nobody_claims_moves_up(self):
        self.gh.ticket(7, "briefed", lane="codex")
        self.gh.events[7] = [{"event": "labeled", "label": {"name": "loom:briefed"},
                              "created_at": iso(factory.BRIEFED_STALL + 600)}]
        self.go()
        self.assertEqual((self.lane(7), self.model(7)), ("claude", "claude-sonnet-5-5"))

    def test_a_held_briefed_ticket_never_counts_as_stalled(self):
        self.gh.ticket(7, "briefed", body="Not before: 2099-01-01T00:00:00Z\nDifficulty: easy\n\n## Why\nx\n")
        self.gh.events[7] = [{"event": "labeled", "label": {"name": "loom:briefed"},
                              "created_at": iso(factory.BRIEFED_STALL + 600)}]
        self.go()
        self.assertEqual(self.model(7), None)

    def test_a_claimed_or_fresh_briefed_ticket_stays(self):
        self.gh.ticket(7, "briefed")
        self.gh.events[7] = [{"event": "labeled", "label": {"name": "loom:briefed"},
                              "created_at": iso(factory.BRIEFED_STALL + 600)}]
        self.gh.comments[7].append({"body": "Claimed for lane codex (body sha256 0123456789ab)",
                                    "created_at": iso(600), "user": ME})
        self.gh.ticket(8, "briefed")
        self.gh.events[8] = [{"event": "labeled", "label": {"name": "loom:briefed"},
                              "created_at": iso(600)}]
        self.go()
        self.assertEqual((self.model(7), self.model(8)), (None, None))

    def test_a_pass_stops_after_its_action_cap(self):
        for n in range(1, factory.MAX_ACTIONS + 5):
            self.gh.ticket(n, "failed")
        self.go()
        self.assertEqual(len(self.moved), factory.MAX_ACTIONS)


class Helpers(unittest.TestCase):
    def test_with_model_replaces_or_inserts_the_header(self):
        self.assertEqual(factory.with_model("Difficulty: hard\nModel: a\n\n## Why\nModel: x", "b"),
                         "Difficulty: hard\nModel: b\n\n## Why\nModel: x")
        self.assertEqual(factory.with_model("Difficulty: hard\n\n## Why", "b"),
                         "Model: b\nDifficulty: hard\n\n## Why")

    def test_a_busy_rung_spills_to_the_next(self):
        self.assertEqual(factory.pick_rung(1, {}), 1)
        self.assertEqual(factory.pick_rung(1, {"claude": 2}), 2)
        self.assertEqual(factory.pick_rung(1, {"claude": 2, "codex": 2, "codex-2": 3}), 3)
        self.assertEqual(factory.pick_rung(2, {"codex": 0}), 2)
        self.assertEqual(factory.pick_rung(3, {}), 3)
        self.assertEqual(factory.pick_rung(4, {}), 0)

    def test_pick_lane_takes_the_least_loaded(self):
        self.assertEqual(factory.pick_lane(("codex", "codex-2"), {"codex": 2, "codex-2": 1}), "codex-2")
        self.assertEqual(factory.pick_lane(("codex", "codex-2"), {}), "codex")

    def test_due_runs_at_most_every_check_interval(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(factory.due(d, NOW))
            self.assertFalse(factory.due(d, NOW + 10))
            self.assertFalse(factory.due(d, NOW - 10), "a clock stepped back still waits")
            self.assertTrue(factory.due(d, NOW + factory.FACTORY_CHECK + 1))
            # A stored time far in the future does not shut the factory out.
            self.assertTrue(factory.due(d, NOW - 10 * factory.FACTORY_CHECK))

    def test_check_runs_are_read_across_pages(self):
        gh = FakeGitHub()
        pages = {1: [{"name": "c%d" % i, "status": "completed"} for i in range(100)],
                 2: [{"name": factory.DEFAULT_REVIEW_CHECK, "status": "completed"}]}

        def rest(method, path, body=None, etag=None, ok=()):
            page = int(re.search(r"page=(\d+)$", path).group(1))
            return Response(200, {"check_runs": pages.get(page, [])})
        gh.rest = rest
        f = factory.Factory(gh, [REPO], NOW, trusted=["alice-example"])
        self.assertEqual(f._review_state(REPO, 1, "sha", NOW), "reviewed")

    def test_the_station_setting_turns_it_off(self):
        self.assertFalse(station.Settings.from_env({"LOOM_FACTORY": "0"}).factory_on)
        on = station.Settings.from_env({})
        self.assertTrue(on.factory_on)
        self.assertEqual(on.review_check, factory.DEFAULT_REVIEW_CHECK)


    def test_the_station_runs_the_factory_when_on_and_throttles_it(self):
        calls = []

        class Fake:
            def __init__(self, gh, repos, now, trusted, review_check):
                calls.append((tuple(repos), tuple(trusted), review_check))

            def run(self):
                calls.append("ran")

        real = factory.Factory
        factory.Factory = Fake
        try:
            with tempfile.TemporaryDirectory() as home:
                settings = station.Settings(home, [REPO, "not a repo"], ["alice-example"], [],
                                            factory_on=True, review_check="Review")
                st = station.Station(FakeGitHub(), settings, now=NOW)
                os.makedirs(st.state, exist_ok=True)
                st.run_factory()
                st.run_factory()
                settings.factory_on = False
                st.now += factory.FACTORY_CHECK + 1
                st.run_factory()
        finally:
            factory.Factory = real
        self.assertEqual(calls, [((REPO,), ("alice-example",), "Review"), "ran"])


if __name__ == "__main__":
    unittest.main()
