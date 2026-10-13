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

    def ticket(self, number, state, lane="codex", body="Difficulty: medium\n\n## Why\nx\n"):
        self.issues[number] = {"number": number, "state": "open", "body": body,
                               "labels": [{"name": "loom:ticket"}, {"name": "loom:" + state},
                                          {"name": "lane:" + lane}]}
        self.comments[number] = []
        self.events[number] = []

    def pull(self, number, ticket, created=3600, mergeable=True, green=True,
             resolved=True, runs=(("unittest", "completed"), (factory.DEFAULT_REVIEW_CHECK, "completed")),
             updated=3600):
        sha = "sha%d" % number
        self.pulls[number] = {
            "number": number, "state": "open", "draft": False, "body": "",
            "head": {"ref": "loom/%d-some-title" % ticket, "sha": sha,
                     "repo": {"full_name": REPO}},
            "base": {"repo": {"full_name": REPO}},
            "created_at": iso(created), "updated_at": iso(updated), "mergeable": mergeable}
        self.runs[sha] = [{"name": n, "status": s} for n, s in runs]
        self.details[number] = {
            "isDraft": False, "mergeable": "MERGEABLE" if mergeable else "CONFLICTING",
            "headRefName": "loom/%d-some-title" % ticket, "headRefOid": sha,
            "headRepository": {"nameWithOwner": REPO},
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {
                "state": "SUCCESS" if green else "FAILURE"}}}]},
            "reviewThreads": {"nodes": [{"isResolved": resolved, "comments": {"nodes": []}}]}}

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
            raise AssertionError("unexpected GET %s" % path)
        self.writes.append((method, path, body))
        m = re.search(r"/issues/(\d+)(/labels|/comments)?$", path)
        if m:
            number = int(m.group(1))
            if m.group(2) == "/labels":
                self.issues[number]["labels"] = [{"name": x} for x in body["labels"]]
            elif m.group(2) == "/comments":
                self.comments[number].append({"body": body["body"], "created_at": iso(0)})
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
        f = factory.Factory(self.gh, [REPO], NOW, merge=merge, fix=fix, move=move)
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

    def test_it_waits_for_the_review_on_a_fresh_push_and_not_forever(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, runs=(("unittest", "completed"),), updated=60)
        self.go()
        self.assertEqual(self.merged, [])
        self.gh.pull(50, 5, runs=(("unittest", "completed"),), updated=factory.REVIEW_GRACE + 60)
        self.go()
        self.assertEqual(self.merged, [(50, (REPO, 5))])

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

    def test_a_second_send_back_climbs_to_the_first_rung(self):
        self.gh.ticket(5, "pr", lane="opencode-go")
        self.gh.comments[5].append({"body": FIX_MARKER + "\nfirst round"})
        self.gh.pull(50, 5, green=False)
        self.go()
        self.assertEqual((self.lane(5), self.model(5)), ("claude", "claude-sonnet-5-5"))
        self.assertEqual(factory.rung_of(self.gh.comments[5]), 1)
        # The lane label changed before the fix brief went out.
        self.assertIn({"name": "lane:claude"}, self.fixed[0][2])

    def test_an_old_pull_request_climbs_on_its_first_send_back(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, mergeable=False, created=factory.STUCK_AGE + 60)
        self.go()
        self.assertEqual(self.model(5), "claude-sonnet-5-5")
        self.assertEqual(len(self.fixed), 1)

    def test_the_rungs_go_sonnet_then_sol_then_opus(self):
        self.gh.ticket(5, "pr")
        self.gh.comments[5].append({"body": FIX_MARKER})
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
        self.gh.comments[5] += [{"body": FIX_MARKER}, {"body": factory.RUNG_MARKER % 3}]
        self.gh.pull(50, 5, resolved=False)
        self.go()
        self.assertEqual(self.fixed, [])
        self.assertEqual(self.moved, [(5, "failed", None)])

    def test_pull_requests_of_tickets_not_in_pr_are_left_alone(self):
        self.gh.ticket(5, "briefed")
        self.gh.pull(50, 5)
        self.gh.pulls[51] = dict(self.gh.pulls[50], number=51, head={"ref": "feature/x"})
        self.go()
        self.assertEqual((self.merged, self.fixed), ([], []))


class Tickets(Base):
    def test_a_failed_ticket_without_a_pull_request_is_rebriefed_a_rung_up(self):
        self.gh.ticket(6, "failed", lane="zai")
        self.go()
        self.assertEqual(self.moved, [(6, "briefed", "claude")])
        self.assertEqual(self.model(6), "claude-sonnet-5-5")

    def test_a_failed_ticket_with_a_pull_request_gets_a_fix_brief(self):
        self.gh.ticket(6, "failed")
        self.gh.pull(60, 6, runs=(("unittest", "in_progress"),))
        self.go()
        self.assertEqual([f[:2] for f in self.fixed], [(60, (REPO, 6))])
        self.assertIn({"name": "loom:pr"}, self.fixed[0][2])
        self.assertNotIn({"name": "loom:failed"}, self.fixed[0][2])

    def test_a_failed_ticket_past_the_last_rung_is_left_alone(self):
        self.gh.ticket(6, "failed")
        self.gh.comments[6].append({"body": factory.RUNG_MARKER % 3})
        self.go()
        self.assertEqual((self.moved, self.fixed), ([], []))

    def test_a_briefed_ticket_nobody_claims_moves_up(self):
        self.gh.ticket(7, "briefed", lane="codex")
        self.gh.events[7] = [{"event": "labeled", "label": {"name": "loom:briefed"},
                              "created_at": iso(factory.BRIEFED_STALL + 600)}]
        self.go()
        self.assertEqual((self.lane(7), self.model(7)), ("claude", "claude-sonnet-5-5"))

    def test_a_claimed_or_fresh_briefed_ticket_stays(self):
        self.gh.ticket(7, "briefed")
        self.gh.events[7] = [{"event": "labeled", "label": {"name": "loom:briefed"},
                              "created_at": iso(factory.BRIEFED_STALL + 600)}]
        self.gh.comments[7].append({"body": "Claimed for lane codex (body sha256 0123456789ab)",
                                    "created_at": iso(600)})
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

    def test_pick_lane_takes_the_least_loaded(self):
        self.assertEqual(factory.pick_lane(("codex", "codex-2"), {"codex": 2, "codex-2": 1}), "codex-2")
        self.assertEqual(factory.pick_lane(("codex", "codex-2"), {}), "codex")

    def test_due_runs_at_most_every_check_interval(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(factory.due(d, NOW))
            self.assertFalse(factory.due(d, NOW + 10))
            self.assertTrue(factory.due(d, NOW + factory.FACTORY_CHECK + 1))

    def test_the_station_setting_turns_it_off(self):
        self.assertFalse(station.Settings.from_env({"LOOM_FACTORY": "0"}).factory_on)
        on = station.Settings.from_env({})
        self.assertTrue(on.factory_on)
        self.assertEqual(on.review_check, factory.DEFAULT_REVIEW_CHECK)


    def test_the_station_runs_the_factory_when_on_and_throttles_it(self):
        calls = []

        class Fake:
            def __init__(self, gh, repos, now, review_check):
                calls.append((tuple(repos), review_check))

            def run(self):
                calls.append("ran")

        real = factory.Factory
        factory.Factory = Fake
        try:
            with tempfile.TemporaryDirectory() as home:
                settings = station.Settings(home, [REPO, "not a repo"], [], [],
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
        self.assertEqual(calls, [((REPO,), "Review"), "ran"])


if __name__ == "__main__":
    unittest.main()
