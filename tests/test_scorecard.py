"""bus/scorecard.py: each lane and model keeps a scorecard, and a lane that
ships bad work is demoted.

Built from the 2026-10-22 audit's per-lane counts (bus/scorecard.AUDIT,
aggregated, no repo names): the lanes whose damaging verdicts reach the
threshold are demoted, and lane pull requests read from GitHub (a bad-work
finding, a revert, a failed deploy) demote a lane the same way.
"""
from __future__ import annotations

import contextlib
import io
import os
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bus import factory, scorecard  # noqa: E402
from bus.github import GitHub  # noqa: E402
from dashboard import views  # noqa: E402
from test_bus_factory import Base as FactoryBase, iso as factory_iso  # noqa: E402
from test_bus_factory import NOW as FACTORY_NOW, REPO as FACTORY_REPO  # noqa: E402

AUDIT_AT = scorecard._ts(scorecard.AUDIT_AT)
NOW = AUDIT_AT + 86400
REPO = "example-owner/example-app"
BOT = "grug-tribe"
LANES_LOGIN = "loom-lanes[bot]"
# The lanes the audit's damaging verdicts reach at the default threshold of 3.
DEMOTED_AT_3 = {"claude", "codex", "spark", "opencode-go", "free"}


def iso(at):
    return datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def card(now=NOW, env=None, state=None):
    return scorecard.Scorecard(state or tempfile.mkdtemp(), now, env=env or {})


def quiet(fn, *args, **kw):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args, **kw)


def audit_records(at):
    """The audit's damaging verdicts per lane and model, as records the
    station would keep: one bad-work finding each, merged at `at`."""
    records = {}
    for (lane, model), (_, _, damaging) in scorecard.AUDIT.items():
        for n in range(damaging):
            records["example-owner/r#%s-%s-%d" % (lane, model, n)] = {
                "lane": lane, "model": model or "unknown", "at": at, "review": "bad-work",
                "merged": True, "sent_back": 0}
    return records


class AuditCounts(unittest.TestCase):
    def seeded(self, now=NOW, env=None, state=None):
        got = card(now=now, env=env, state=state)
        got.data["records"] = audit_records(AUDIT_AT)
        return got

    def test_the_audits_damaging_lanes_are_demoted_at_the_threshold(self):
        got = self.seeded()
        self.assertEqual(set(quiet(got.demote)), DEMOTED_AT_3)
        self.assertIn("8 bad-work findings", got.demoted()["claude"])
        self.assertEqual(set(quiet(self.seeded(env={"LOOM_LANE_DEMOTE_AT": "7"}).demote)),
                         {"claude", "codex"})

    def test_outcomes_past_the_window_demote_nothing(self):
        self.assertEqual(quiet(self.seeded(now=AUDIT_AT + 8 * 86400).demote), [])

    def test_the_audits_verdicts_are_shown_and_do_not_demote_on_their_own(self):
        got = card()
        self.assertEqual(quiet(got.demote), [])
        self.assertEqual(got.table()[("claude", "claude-opus-5-5")]["audit damaging"], 3)

    def test_a_demotion_stays_until_the_operator_clears_it(self):
        state = tempfile.mkdtemp()
        first = self.seeded(state=state)
        quiet(first.demote)
        first.save()
        later = card(now=AUDIT_AT + 30 * 86400, state=state)
        self.assertIn("codex", later.demoted())
        self.assertTrue(later.clear("codex"))
        quiet(later.demote)
        self.assertNotIn("codex", later.demoted())
        # Clearing forgets the outcomes before it, inside the window too.
        again = self.seeded()
        again.clear("spark")
        self.assertNotIn("spark", quiet(again.demote))

    def test_the_cli_clears_a_lane(self):
        state = tempfile.mkdtemp()
        first = self.seeded(state=state)
        quiet(first.demote)
        first.save()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            scorecard.main(["clear", "codex-2", "--state", state])
        self.assertIn("cleared lane codex-2", out.getvalue())
        self.assertNotIn("codex", card(state=state).demoted())

    def test_demoted_lanes_cover_their_slots(self):
        slots = scorecard.demoted_slots({"codex": "x", "free": "y", "opencode-go": "z"},
                                        ["codex", "codex-2", "free-kilo", "opencode-go-3",
                                         "claude", "zen"])
        self.assertEqual(slots, {"codex": "x", "codex-2": "x", "free-kilo": "y",
                                 "opencode-go-3": "z"})


def finding(rule, severity="medium"):
    return {"isResolved": True, "comments": {"nodes": [{
        "author": {"__typename": "Bot", "login": BOT}, "createdAt": iso(NOW),
        "body": "* %s | _general_ | `%s` | quick" % (severity, rule)}]}}


class Route:
    def __init__(self, routes, threads):
        self.routes, self.threads, self.fail = routes, threads, set()

    def __call__(self, method, url, headers, body):
        path = url.split("api.github.com")[-1]
        if path == "/graphql":
            number = json.loads(body)["variables"]["number"]
            return 200, {}, json.dumps({"data": {"repository": {"pullRequest": {
                "reviewThreads": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                                  "nodes": self.threads.get(number, [])}}}}}).encode()
        for prefix in sorted(self.routes, key=len, reverse=True):
            if path.startswith(prefix):
                if prefix in self.fail:
                    return 422, {}, b'{"message": "no"}'
                return 200, {}, json.dumps(self.routes[prefix]).encode()
        return 404, {}, b"{}"


def lane_pr(number, lane="spark-2", model="nemotron-3.5-super-vl", merged=True, title=None,
            body=None, ago=3600):
    return {"number": number, "title": title or "Change %d" % number,
            "user": {"login": LANES_LOGIN}, "head": {"ref": "loom/%d-x" % (number + 100)},
            "merge_commit_sha": "%040d" % number, "closed_at": iso(NOW - ago),
            "merged_at": iso(NOW - ago) if merged else None,
            "body": body if body is not None else
            "Refs %s#%d\n\n---\nLoom ticket %s#%d, lane %s, model %s.\n"
            % (REPO, number + 100, REPO, number + 100, lane, model)}


class Records(unittest.TestCase):
    def setUp(self):
        self.pulls = [lane_pr(1), lane_pr(2), lane_pr(3), lane_pr(4, lane="codex", model="gpt-6-luna"),
                      # A revert of #2 merged.
                      {"number": 9, "title": 'Revert "Change 2"', "user": {"login": "someone"},
                       "head": {"ref": "revert-2"}, "merged_at": iso(NOW - 60),
                       "closed_at": iso(NOW - 60), "merge_commit_sha": "9" * 40, "body": ""}]
        self.fake = Route({"/repos/%s/pulls?state=closed" % REPO: self.pulls,
                           "/repos/%s/issues/" % REPO: []},
                          {1: [finding("dead-code")], 4: [finding("naming", "low")]})
        self.fake.routes["/repos/%s/issues/101/comments" % REPO] = [
            {"body": "<!-- loom:fix v1 -->\nfix it"}, {"body": "<!-- loom:fix v1 -->\nagain"}]

    def refresh(self, got, deploys=None, audits=None):
        quiet(got.refresh, GitHub("t", transport=self.fake), [REPO], [LANES_LOGIN], BOT,
              audits or {}, deploys or {})
        return got

    def test_outcomes_are_recorded_per_lane_and_model(self):
        got = self.refresh(card())
        table = got.table()
        spark = table[("spark", "nemotron-3.5-super-vl")]
        self.assertEqual((spark["prs"], spark["bad-work"], spark["clean"], spark["reverted"],
                          spark["merged"], spark["sent back"]), (3, 1, 2, 1, 3, 2))
        self.assertEqual(table[("codex", "gpt-6-luna")]["findings"], 1)

    def test_a_bad_work_finding_a_revert_and_a_failed_deploy_demote_a_lane(self):
        state = tempfile.mkdtemp()
        env = {"LOOM_LANE_DEMOTE_AT": "3"}
        later = NOW + 30 * 86400
        for pr in self.pulls:
            for key in ("closed_at", "merged_at"):
                if pr.get(key):
                    pr[key] = iso(later - 3600)
        got = self.refresh(card(now=later, env=env, state=state))
        self.assertNotIn("spark", got.demoted())  # one bad-work finding, one revert
        deploys = {"%s#3" % REPO: {"state": "reverted", "failure": "rollout-aborted: x"}}
        got = self.refresh(card(now=later + scorecard.REFRESH, env=env, state=state), deploys)
        self.assertIn("spark", got.demoted())
        self.assertIn("1 bad-work findings, 2 reverts, 1 failed deploys", got.demoted()["spark"])
        self.assertNotIn("codex", got.demoted())

    def test_a_repo_that_cannot_be_read_keeps_its_records_and_demotions(self):
        state = tempfile.mkdtemp()
        first = self.refresh(card(state=state))
        first.save()
        self.fake.fail.add("/repos/%s/pulls?state=closed" % REPO)
        got = self.refresh(card(now=NOW + scorecard.REFRESH, state=state))
        self.assertEqual(len(got.data["records"]), 4)
        self.assertEqual(set(got.demoted()), set(first.demoted()))

    def test_failed_proof_audits_count_against_their_lane(self):
        later = NOW + 30 * 86400
        got = self.refresh(card(now=later, env={"LOOM_LANE_DEMOTE_AT": "2"}),
                           audits={"zen": later + 3600, "zen-x": later})
        self.assertEqual(got.table()[("zen", "")]["audit failed"], 1)
        self.assertNotIn("zen", got.demoted())


class Table(unittest.TestCase):
    def test_the_status_table_round_trips_and_the_dashboard_shows_it(self):
        got = card()
        got.data["records"] = audit_records(AUDIT_AT)
        quiet(got.demote)
        body = "\n".join(["<!-- loom:lanes v1 -->", "| lane | available | heartbeat (UTC) |",
                          "|---|---|---|", "| codex | yes | now |"] + got.markdown())
        rows = scorecard.parse_rows(body)
        claude = next(r for r in rows if (r["lane"], r["model"]) == ("claude", "claude-opus-5-5"))
        self.assertEqual((claude["counts"]["audit damaging"], claude["counts"]["bad-work"]), (3, 3))
        self.assertTrue(claude["demoted"])
        self.assertEqual([r["lane"] for r in views.parse_lane_rows(body)], ["codex"])
        page = views.render_lanes({"lanes": [], "scorecard": rows})
        self.assertIn("Scorecard", page)
        self.assertIn("5 demoted", page)
        # The demoted lanes come first.
        self.assertIn('<tbody><tr class="scorecard-demoted"><th scope="row">claude', page)


class StationStep(unittest.TestCase):
    def test_the_station_scores_hourly_and_keeps_demoted_lanes_from_build_claims(self):
        from bus import station
        fake = Route({"/repos/%s/pulls?state=closed" % REPO: [], "/repos/%s/issues" % REPO: [],
                      "/search/issues": {"total_count": 0, "items": []}}, {})
        home = tempfile.mkdtemp()
        settings = station.Settings(home, [REPO], [LANES_LOGIN], ["example-owner"],
                                    factory_on=True)
        st = station.Station(GitHub("t", transport=fake), settings, now=NOW)
        seed = scorecard.Scorecard(st.state, NOW)
        seed.data["records"] = audit_records(AUDIT_AT)
        seed.save()
        quiet(st.score_lanes)
        demoted = st.demoted_lanes()
        self.assertIn("codex-2", demoted)
        self.assertNotIn("zen", demoted)
        self.assertEqual(st._factory().demoted, demoted)
        refreshed = scorecard.Scorecard(st.state, NOW).data["refreshed"]
        st.now = NOW + 60
        quiet(st.score_lanes)
        self.assertEqual(scorecard.Scorecard(st.state, NOW).data["refreshed"], refreshed)
        self.assertIn("| scorecard lane | model |", "\n".join(st._scorecard_lines()))


class FailClosed(unittest.TestCase):
    def test_a_scorecard_file_that_cannot_be_read_demotes_every_lane(self):
        from bus import station
        home = tempfile.mkdtemp()
        settings = station.Settings(home, [REPO], [LANES_LOGIN], ["example-owner"],
                                    factory_on=True)
        st = station.Station(GitHub("t", transport=Route({}, {})), settings, now=NOW)
        os.makedirs(st.state, exist_ok=True)
        with open(os.path.join(st.state, scorecard.FILE), "w") as fh:
            fh.write('{"demoted": {"codex"')
        demoted = quiet(st.demoted_lanes)
        self.assertIn("zen", demoted)
        self.assertIn("cannot be read", demoted["codex-2"])
        with self.assertRaises(ValueError):
            scorecard.Scorecard(st.state, NOW).save()

    def test_a_review_that_cannot_be_read_records_nothing_yet(self):
        fake = Route({"/repos/%s/pulls?state=closed" % REPO: [lane_pr(1)]}, {})
        got = card()

        def broken(*args, **kw):
            raise ValueError("could not read every review thread")
        with mock.patch("bus.merge_tier.review_threads", broken):
            quiet(got.refresh, GitHub("t", transport=fake), [REPO], [LANES_LOGIN], BOT, {}, {})
        self.assertEqual(got.data["records"], {})


class FactoryStepAside(FactoryBase):
    def test_a_ticket_on_a_demoted_lane_steps_aside_and_says_why(self):
        self.gh.ticket(7, "briefed", lane="spark-2", body="Difficulty: medium\n\n## Why\nx\n")
        self.gh.events[7] = [{"event": "labeled", "label": {"name": "loom:briefed"},
                              "created_at": factory_iso(60)}]
        why = "3 bad outcomes in 7 days (1 reverts, 2 failed deploys)"
        with mock.patch("bus.factory._say"):
            self.go(demoted={"spark-2": why},
                    available=[x for x in factory.TIERS["medium"] if x != "spark-2"])
        self.assertNotEqual(self.lane(7), "spark-2")
        self.assertIn("lane spark-2 is demoted (%s; the operator clears it)" % why,
                      self.gh.comments[7][-1]["body"])

    def queued(self, number, body="Difficulty: medium\n\n## Why\nx\n"):
        self.gh.ticket(number, "queued", body=body)
        self.gh.issues[number]["labels"] = [{"name": "loom:ticket"}, {"name": "loom:queued"}]
        self.gh.issues[number]["created_at"] = factory_iso(0)

    def notes(self, number):
        return "\n".join(str(c.get("body") or "") for c in self.gh.comments[number])

    def test_a_demoted_lane_with_a_fresh_heartbeat_is_never_given_a_queued_ticket(self):
        # loom#2430: _up read only limited and available, so a demoted lane
        # with a fresh heartbeat took a queued ticket, and the same pass's
        # step-aside moved it straight back ("The station gave this queued
        # ticket lane X" then "Not a failure (lane X is demoted ...)").
        self.queued(7)
        first = factory.TIERS["medium"][0]
        why = "3 bad outcomes in 7 days (1 reverts, 2 failed deploys)"
        worker = factory.Factory(self.gh, [FACTORY_REPO], FACTORY_NOW, trusted=["alice-example"],
                                 available=list(factory.TIERS["medium"]),
                                 demoted={first: why})
        with mock.patch("bus.factory._say"):
            worker.assign_queued()
            worker.run()
        self.assertEqual(factory._state(self.gh.issues[7]), "briefed")
        self.assertNotEqual(self.lane(7), first)
        self.assertIn("The station gave this queued ticket lane %s" % self.lane(7), self.notes(7))
        self.assertNotIn("is demoted", self.notes(7))

    def test_a_queued_ticket_whose_only_up_lane_is_demoted_waits(self):
        self.queued(7)
        first = factory.TIERS["medium"][0]
        worker = factory.Factory(self.gh, [FACTORY_REPO], FACTORY_NOW, trusted=["alice-example"],
                                 available=[first], demoted={first: "bad work"})
        with mock.patch("bus.factory._say"):
            worker.assign_queued()
        self.assertEqual(factory._state(self.gh.issues[7]), "queued")
        self.assertNotIn("lane:" + first, [x["name"] for x in self.gh.issues[7]["labels"]])

    def test_a_step_aside_never_moves_a_ticket_onto_a_demoted_lane(self):
        # The tier move off a limited lane skipped only limited and
        # unavailable lanes: it picked the demoted one, which bounced it on.
        self.gh.ticket(7, "briefed", lane="codex", body="Difficulty: medium\n\n## Why\nx\n")
        self.gh.events[7] = [{"event": "labeled", "label": {"name": "loom:briefed"},
                              "created_at": factory_iso(60)}]
        first = factory.TIERS["medium"][0]
        with mock.patch("bus.factory._say"):
            self.go(limited=["codex"], demoted={first: "bad work"},
                    available=list(factory.TIERS["medium"]))
        self.assertNotIn(self.lane(7), (first, "codex"))
        self.assertNotIn("is demoted", self.notes(7))


if __name__ == "__main__":
    unittest.main()
