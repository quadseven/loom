"""bus/brief_check.py: a stale or already-shipped ticket never reaches a build
lane, and a health ticket names its cause.

The cases are the audit's of 2026-10-22, made generic: a rewrite of an issue
whose leak-scan caller and deny-list layer had already shipped, which a
lane met by deleting the working deny-list layer (twice, in two repos), and
the health ticket a lane met by changing the measurement (a health read
that began counting money the fund's holder carried, and a sibling that
held refunds back to keep the balance above its floor).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bus import brief_check, factory, operator_hold, station  # noqa: E402
from bus.github import GitHub  # noqa: E402

REPO = "example-owner/example-app"
OTHER = "example-owner/example-infra"
NOW = 2_000_000_000.0
OPERATOR = "the-operator"
TRUSTED = [OPERATOR, "loom-station[bot]", "loom-brain[bot]"]
MARKER = "<!-- loom:health key=%s -->" % ("a" * 40)

# The audit's stale rewrite: the leak-scan workflow and its deny-list layer
# had shipped under the source issue weeks before.
STALE_REWRITE = """Difficulty: medium

## Why
Every pull request should run the shared leak scan.

## What
- Add the reusable leak-scan caller.

## Acceptance criteria
- [ ] A new `.github/workflows/check.leak-scan.yml` runs the shared leak scan on every pull request.

## Size
S

## Out of scope
- The deny-list layer.

Rewritten from issue 124.
"""

# The audit's health ticket, as health intake files it: no cause named.
HEALTH_INTAKE = """## Why

Loom's health intake filed this ticket: the goal's health endpoint reports a live problem \
in the fund area. It affects 65 now.

## What

Find why the health endpoint reports this problem and fix the cause in this repo.

## Acceptance criteria

- [ ] A test fails without the fix and passes with it.

%s
""" % MARKER


def iso(ago):
    return datetime.fromtimestamp(NOW - ago, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def comment(body, login=OPERATOR, ago=60, ident=1, bot=False):
    return {"id": ident, "body": body, "created_at": iso(ago),
            "user": {"login": login, "type": "Bot" if bot else "User"}}


class Route:
    """GitHub by path: GETs from `routes` (longest prefix first), a missing
    path is a 404, a path in `fail` a 422; writes are recorded."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.fail = set()
        self.writes = []
        self.reads = []

    def __call__(self, method, url, headers, body):
        path = url.split("api.github.com")[-1]
        if method != "GET":
            self.writes.append((method, path, json.loads(body) if body else None))
            return 200, {}, b"{}"
        self.reads.append(path)
        for prefix in sorted(self.routes, key=len, reverse=True):
            if path.startswith(prefix):
                if prefix in self.fail:
                    return 422, {}, b'{"message": "unprocessable"}'
                return 200, {}, json.dumps(self.routes[prefix]).encode()
        return 404, {}, b'{"message": "Not Found"}'


def ticket(body=STALE_REWRITE, number=206, labels=("loom:ticket", "loom:briefed", "lane:codex")):
    return {"number": number, "state": "open", "body": body, "created_at": iso(86400),
            "user": {"login": "loom-brain[bot]"}, "labels": [{"name": n} for n in labels]}


class Health(unittest.TestCase):
    def test_a_health_intake_ticket_with_no_cause_gets_no_lane(self):
        got = brief_check.health_verdict(HEALTH_INTAKE)
        self.assertEqual(got.kind, brief_check.HEALTH)
        self.assertIn("names no cause", got.reason)

    def test_the_audits_measurement_cause_is_refused(self):
        body = HEALTH_INTAKE + "\nCause: The health read does not count money the fund's holder carries.\n"
        self.assertIn("names the measurement", brief_check.health_verdict(body).reason)
        body = HEALTH_INTAKE + "\nCause: the floor is too strict for a fund this size.\n"
        self.assertIn("names the measurement", brief_check.health_verdict(body).reason)

    def test_a_what_that_only_moves_the_bound_is_refused(self):
        # The sibling change: hold the floor amount back from refunds.
        body = HEALTH_INTAKE.replace(
            "Find why the health endpoint reports this problem and fix the cause in this repo.",
            "- Hold the floor amount back from every refund so the balance stays above the floor.")
        body += "\nCause: refunds pay out more than the fund takes in each week.\n"
        self.assertIn("changes the measurement", brief_check.health_verdict(body).reason)

    def test_a_named_cause_lets_it_through(self):
        body = HEALTH_INTAKE + ("\nCause: members pay the repair fee twice when a refund "
                                "and a payout land in the same tick.\n")
        self.assertIsNone(brief_check.health_verdict(body))
        self.assertFalse(brief_check.health_hold({"body": body}))
        self.assertTrue(brief_check.health_hold({"body": HEALTH_INTAKE}))

    def test_a_ticket_that_names_no_health_finding_is_not_a_health_ticket(self):
        self.assertIsNone(brief_check.health_verdict(STALE_REWRITE))
        self.assertIsNotNone(brief_check.health_verdict(
            "## Why\nThe health endpoint reports 12 stuck jobs.\n\n## What\n- Fix it.\n"))


class MeasurementHold(unittest.TestCase):
    """At merge, a lane change to the health read or a bound on a health
    ticket waits for the operator (bus/operator_hold.py)."""

    HEALTH_READ = [{"filename": "api/health.py", "patch": "@@ -1 +1 @@\n-total = fund\n"
                    "+total = fund + carried"}]
    FLOOR = [{"filename": "app/fund_plan.py",
              "patch": "@@ -1 +1,2 @@\n+FUND_FLOOR = 100\n refund = owed"}]
    APP = [{"filename": "app/refunds.py", "patch": "@@ -1 +1 @@\n-pay(owed)\n+pay(owed, once=True)"}]

    def reasons(self, entries, body=HEALTH_INTAKE):
        gh = GitHub("t", transport=Route({"/repos/%s/contents/.github/workflows" % REPO: []}))
        return operator_hold.reasons(gh, REPO, {"number": 8, "body": "", "base": {"ref": "main"}},
                                     body, entries)

    def test_the_audits_health_read_and_floor_changes_are_held(self):
        for entries in (self.HEALTH_READ, self.FLOOR):
            with self.subTest(entries=entries[0]["filename"]):
                why = self.reasons(entries)
                self.assertEqual(len(why), 1)
                self.assertIn("changes the health read or a bound", why[0])

    def test_a_file_whose_patch_is_missing_cut_or_renamed_from_the_read_is_held(self):
        # Security review: a binary or very large file has no patch, a long
        # patch is cut, and a rename was judged by its new name only.
        for entries in ([{"filename": "data/fund.bin"}],
                        [{"filename": "app/refunds.py", "additions": 400, "deletions": 3,
                          "patch": "@@ -1 +1 @@\n-pay(owed)\n+pay(owed, once=True)"}],
                        [{"filename": "app/fund_report.py", "previous_filename": "api/health.py",
                          "patch": "@@ -1 +1 @@\n-x = 1\n+x = 2"}]):
            with self.subTest(entries=entries[0]["filename"]):
                self.assertEqual(len(self.reasons(entries)), 1)

    def test_a_file_list_that_cannot_be_read_in_full_is_held(self):
        fake = Route({"/repos/%s/contents/.github/workflows" % REPO: []})
        fake.routes["/repos/%s/pulls/8/files" % REPO] = []
        fake.fail.add("/repos/%s/pulls/8/files" % REPO)
        why = operator_hold.measurement_reason(GitHub("t", transport=fake), REPO, {"number": 8},
                                               HEALTH_INTAKE, [{"filename": "app/a.py"}])
        self.assertIn("could not be read", why)
        many = [{"filename": "app/f%d.py" % n, "patch": "@@ -1 +1 @@\n-a\n+b"}
                for n in range(operator_hold.FILES_CAP)]
        self.assertIn("or more files", operator_hold.measurement_reason(
            GitHub("t", transport=fake), REPO, {"number": 8}, HEALTH_INTAKE, many))

    def test_a_fix_of_the_cause_and_a_non_health_ticket_are_not(self):
        self.assertEqual(self.reasons(self.APP), [])
        self.assertEqual(self.reasons(self.HEALTH_READ, body=STALE_REWRITE), [])


class Stale(unittest.TestCase):
    def setUp(self):
        self.fake = Route({
            "/repos/%s/issues/124" % REPO: {"number": 124, "state": "open"},
            "/repos/%s/issues/206/timeline" % REPO: [],
            "/repos/%s/contents/.github/workflows/check.leak-scan.yml" % REPO: {"type": "file"},
        })
        self.fake.routes.pop("/repos/%s/contents/.github/workflows/check.leak-scan.yml" % REPO)

    def check(self, issue=None, comments=()):
        gh = GitHub("t", transport=self.fake)
        return brief_check.check(gh, REPO, issue or ticket(), list(comments), TRUSTED, "main")

    def test_a_fresh_ticket_goes_to_its_lane(self):
        self.assertIsNone(self.check())

    def test_the_audits_stale_rewrite_whose_source_shipped(self):
        self.fake.routes["/repos/%s/issues/124" % REPO] = {
            "number": 124, "state": "closed", "state_reason": "completed"}
        got = self.check()
        self.assertEqual((got.kind, got.key), (brief_check.STALE, "source-124"))
        self.assertIn("issue #124", got.reason)

    def test_every_file_the_criteria_add_already_exists(self):
        self.fake.routes["/repos/%s/contents/.github/workflows/check.leak-scan.yml" % REPO] = {
            "type": "file"}
        got = self.check()
        self.assertIn("already exists on main: `.github/workflows/check.leak-scan.yml`", got.reason)

    def test_the_operators_newest_comment_says_it_is_done(self):
        said = [comment("Not done yet, see below.", ago=600, ident=1),
                comment("This already shipped in #126 and #193; superseded.", ago=60, ident=2)]
        got = self.check(comments=said)
        self.assertEqual(got.key, "comment-2")
        self.assertIsNone(self.check(comments=list(reversed(said[:1]))))
        self.assertIsNone(self.check(comments=[comment("It is not done.", ident=3)]))
        # A Loom App's words are not the operator's.
        self.assertIsNone(self.check(comments=[comment("Already done.", login="loom-brain[bot]",
                                                       bot=True)]))

    def test_a_merged_pull_request_in_another_repo_that_names_it(self):
        self.fake.routes["/repos/%s/issues/206/timeline" % REPO] = [{
            "event": "cross-referenced", "source": {"type": "issue", "issue": {
                "number": 31, "body": "Refs %s#206\n" % REPO, "title": "Ship the caller",
                "repository": {"full_name": OTHER},
                "pull_request": {"merged_at": iso(3600)}}}}]
        got = self.check()
        self.assertEqual(got.key, "pr-%s#31" % OTHER)

    def test_a_linked_pull_request_merged_after_the_ticket_was_filed(self):
        said = [comment("Shipping it in https://github.com/%s/pull/45 instead." % OTHER)]
        self.fake.routes["/repos/%s/pulls/45" % OTHER] = {"merged_at": iso(3600), "title": "x"}
        self.assertEqual(self.check(comments=said).key, "pr-%s#45" % OTHER)
        # Merged before the ticket was filed: context, not the work.
        self.fake.routes["/repos/%s/pulls/45" % OTHER] = {"merged_at": iso(90000), "title": "x"}
        self.assertIsNone(self.check(comments=said))

    def test_a_strangers_link_decides_nothing(self):
        # Security review: anyone can comment on a public ticket, and a
        # link to any merged pull request held it as shipped.
        said = [comment("Done in https://github.com/%s/pull/45" % OTHER, login="a-stranger")]
        self.fake.routes["/repos/%s/pulls/45" % OTHER] = {"merged_at": iso(3600), "title": "x"}
        self.assertIsNone(self.check(comments=said))
        self.assertNotIn("/repos/%s/pulls/45" % OTHER, self.fake.reads)

    def test_evidence_the_operator_was_asked_about_holds_it_once(self):
        self.fake.routes["/repos/%s/issues/124" % REPO] = {
            "number": 124, "state": "closed", "state_reason": "completed"}
        asked = comment(brief_check.ask(self.check()), login="loom-station[bot]", bot=True)
        self.assertIsNone(self.check(comments=[asked]))

    def test_a_read_that_fails_holds_the_ticket(self):
        self.fake.routes["/repos/%s/issues/206/timeline" % REPO] = []
        self.fake.fail.add("/repos/%s/issues/206/timeline" % REPO)
        with self.assertRaises(brief_check.Unread):
            self.check()


class StationClaim(unittest.TestCase):
    def station(self, fake):
        home = tempfile.mkdtemp()
        settings = station.Settings(home, [REPO], TRUSTED, ["example-owner"])
        return station.Station(GitHub("t", transport=fake), settings, now=NOW)

    def routes(self, issue):
        return Route({
            "/repos/%s" % REPO: {"default_branch": "main"},
            "/repos/%s/issues/206" % REPO: issue,
            "/repos/%s/issues/206/comments" % REPO: [],
            "/repos/%s/issues/206/timeline" % REPO: [],
            "/repos/%s/issues/124" % REPO: {"number": 124, "state": "closed",
                                            "state_reason": "completed"},
        })

    def labels_put(self, fake):
        return [b["labels"] for m, p, b in fake.writes if m == "PUT" and p.endswith("/labels")]

    def test_a_stale_ticket_goes_back_to_the_queue_with_one_ask(self):
        issue = ticket()
        fake = self.routes(issue)
        held = self.station(fake)._brief_holds(REPO, issue, [])
        self.assertTrue(held)
        self.assertIn(["loom:ticket", "lane:codex", "loom:queued"], self.labels_put(fake))
        asks = [b["body"] for m, p, b in fake.writes if m == "POST" and p.endswith("/comments")]
        self.assertEqual(len(asks), 1)
        self.assertIn("<!-- loom:stale-brief source-124 -->", asks[0])
        self.assertIn(("POST", "/repos/%s/issues/206/labels" % REPO, {"labels": ["needs:human"]}),
                      fake.writes)

    def test_a_health_ticket_with_no_cause_goes_to_the_brain(self):
        issue = ticket(body=HEALTH_INTAKE)
        fake = self.routes(issue)
        self.assertTrue(self.station(fake)._brief_holds(REPO, issue, []))
        self.assertIn(["loom:ticket", "lane:codex", "loom:queued"], self.labels_put(fake))
        self.assertTrue(any(m == "POST" and b == {"labels": ["needs-brain"]}
                            for m, _, b in fake.writes))
        self.assertEqual(len(self.labels_put(fake)), 1)

    def test_a_read_that_fails_holds_it_and_writes_nothing(self):
        issue = ticket()
        fake = self.routes(issue)
        fake.fail.add("/repos/%s/issues/124" % REPO)
        self.assertTrue(self.station(fake)._brief_holds(REPO, issue, []))
        self.assertEqual(fake.writes, [])

    def test_a_regression_is_not_stale_for_its_own_merged_change(self):
        issue = ticket(labels=("loom:ticket", "loom:briefed", "lane:codex", "loom:regression"))
        fake = self.routes(issue)
        self.assertFalse(self.station(fake)._brief_holds(REPO, issue, []))
        self.assertEqual(fake.writes, [])


class FactoryQueue(unittest.TestCase):
    def setUp(self):
        from test_bus_factory import FakeGitHub, REPO as FREPO
        self.repo = FREPO
        self.gh = FakeGitHub()
        lanes = mock.patch.dict(os.environ, {"LOOM_AUTO_MERGE_TIERS": "docs-tests,app-code"})
        lanes.start()
        self.addCleanup(lanes.stop)

    def queued(self, number, body, extra=()):
        self.gh.ticket(number, "queued", body=body)
        self.gh.issues[number]["labels"] = [{"name": n} for n in
                                            ("loom:ticket", "loom:queued") + tuple(extra)]

    def lane_of(self, number):
        return [x["name"] for x in self.gh.issues[number]["labels"] if x["name"].startswith("lane:")]

    def test_a_health_ticket_with_no_cause_and_a_held_ticket_get_no_lane(self):
        self.queued(7, "Difficulty: medium\n\n" + HEALTH_INTAKE)
        self.queued(8, "Difficulty: medium\n\n## Why\nx\n", extra=("loom:human",))
        self.queued(9, "Difficulty: medium\n\n" + HEALTH_INTAKE
                    + "\nCause: refunds post to the wrong account when two land at once.\n")
        worker = factory.Factory(self.gh, [self.repo], NOW, trusted=["alice-example"])
        worker.assign_queued()
        self.assertEqual(worker.waits["%s#7" % self.repo], "health ticket names no cause")
        self.assertEqual(worker.waits["%s#8" % self.repo], "waits for a person (needs:human)")
        self.assertEqual((self.lane_of(7), self.lane_of(8)), ([], []))
        self.assertNotIn("%s#9" % self.repo, worker.waits)


# loom#2033, made generic: a rewrite whose own brief said a later issue
# supersedes the work and that issue is blocked, with the original's own
# "Blocked by" section quoted below it. The station queued it and two lanes
# took it, the second finding nothing to build.
SUPERSEDED_REWRITE = """Model: gpt-6-luna
Difficulty: medium

## Why
The issue asks for timed traces around core load, ROM load, and shader or context
initialization so failures during startup can be located even when logs go quiet.
A later comment says issue #21 supersedes this work by sending those spans through
the agent spool, and that #21 is blocked on #15.

## What
Implement startup spans through the agent spool as described by #21.

## Acceptance criteria
- [ ] Core load, ROM load, and shader or context initialization emit spans.
- [ ] Spans are closed on failure as well as success.
- [ ] Sampling and export work off the frame loop.

## Size
M

## Out of scope
- Adding a general-purpose tracing framework.

Part of #30

## Further notes

The quoted original issue below is kept for reference; its boxes are not claimed.

<details><summary>Original issue</summary>

> Original title: trace core load and ROM load
>
> ## Blocked by
>
> - #9 - the worker stops after one cycle.

</details>
"""

BLOCKED_BRIEF = """Difficulty: medium

## Why
Ship the export once the transport lands.

## Blocked on
- #15 - the shared transport.
- #16

## Acceptance criteria
- [ ] Spans export. See #40 for context.
"""


class SupersededOrBlocked(unittest.TestCase):
    """loom#2033: the queue step gives no lane to a brief that says it is
    superseded, or whose Blocked on section names an open issue."""

    def test_the_brief_says_which_issue_supersedes_it(self):
        self.assertEqual(brief_check.superseded_by(SUPERSEDED_REWRITE), 21)
        for text, want in (("Superseded by #21.", 21), ("superseded by issue #7", 7),
                           ("#8 now supersedes this ticket", 8),
                           ("This is not superseded by #21.", None),
                           ("> Superseded by #21", None), ("A superseded test.", None)):
            with self.subTest(text=text):
                self.assertEqual(brief_check.superseded_by(text), want)

    def test_only_the_brief_s_own_blocked_on_section_counts(self):
        self.assertEqual(brief_check.blocked_on(BLOCKED_BRIEF), [15, 16])
        self.assertEqual(brief_check.blocked_on("## Why\nBlocked on: #3 and #4\n"), [3, 4])
        # The quoted original's Blocked by section is the source's words.
        self.assertEqual(brief_check.blocked_on(SUPERSEDED_REWRITE), [])
        self.assertEqual(brief_check.blocked_on("## Why\nwhy\n"), [])

    def setUp(self):
        from test_bus_factory import FakeGitHub, REPO as FREPO
        self.repo = FREPO
        self.gh = FakeGitHub()

    def queued(self, number, body):
        self.gh.ticket(number, "queued", body=body)
        self.gh.issues[number]["labels"] = [{"name": "loom:ticket"}, {"name": "loom:queued"}]

    def names(self, number):
        return [x["name"] for x in self.gh.issues[number]["labels"]]

    def assign(self):
        worker = factory.Factory(self.gh, [self.repo], NOW, trusted=["alice-example"],
                                 available=["codex", "codex-2", "openrouter"])
        worker.assign_queued()
        return worker

    def test_a_superseded_brief_gets_no_lane_and_its_label(self):
        self.queued(12, SUPERSEDED_REWRITE.replace("Model: gpt-6-luna\n", ""))
        worker = self.assign()
        self.assertEqual(worker.waits["%s#12" % self.repo], "superseded by #21")
        self.assertIn("wait:superseded", self.names(12))
        self.assertIn("loom:queued", self.names(12))
        self.assertFalse(any(n.startswith("lane:") for n in self.names(12)))
        # A second pass labels nothing again.
        writes = len(self.gh.writes)
        self.assign()
        self.assertEqual(len(self.gh.writes), writes)

    def test_a_model_pinned_superseded_brief_gets_no_lane(self):
        self.queued(12, SUPERSEDED_REWRITE)
        worker = self.assign()
        self.assertEqual(worker.waits["%s#12" % self.repo], "superseded by #21")
        self.assertFalse(any(n.startswith("lane:") for n in self.names(12)))

    def test_a_brief_blocked_on_an_open_issue_waits_until_it_closes(self):
        self.gh.ticket(15, "running")
        self.gh.ticket(16, "merged")
        self.gh.issues[16]["state"] = "closed"
        self.queued(7, BLOCKED_BRIEF)
        worker = self.assign()
        self.assertEqual(worker.waits["%s#7" % self.repo], "blocked on #15")
        self.assertIn("wait:blocked", self.names(7))
        self.assertFalse(any(n.startswith("lane:") for n in self.names(7)))
        self.gh.issues[15]["state"] = "closed"
        worker = self.assign()
        self.assertNotIn("%s#7" % self.repo, worker.waits)
        self.assertTrue(any(n.startswith("lane:") for n in self.names(7)))
        self.assertNotIn("wait:blocked", self.names(7))

if __name__ == "__main__":
    unittest.main()


class Criteria(unittest.TestCase):
    """A brief is not dispatchable until each acceptance criterion is
    restated as the check that proves it (loom#2162): a ticket with no
    criteria can carry no proof checks, so it gets no lane. The runner
    already parks such a brief as a work failure; the station now holds
    the ticket for the brain instead of spending the claim and the lane
    slot to learn it."""

    NO_CRITERIA = ("## Why\n\nThe totals are wrong.\n\n"
                   "## What\n\n- Fix the total.\n")
    WITH_CRITERIA = (NO_CRITERIA + "\n## Acceptance criteria\n"
                     "- [ ] A test fails without the fix and passes with it.\n")
    EMPTY_SECTION = (NO_CRITERIA + "\n## Acceptance criteria\n\n## Size\nS\n")
    SHORT_HEADING = (NO_CRITERIA + "\n## Acceptance\n"
                     "- [ ] A test fails without the fix and passes with it.\n")
    ANSWER = ("Deliverable: answer\n\n" + NO_CRITERIA)

    def setUp(self):
        self.fake = Route({"/repos/%s/issues/206/timeline" % REPO: []})

    def check(self, body):
        gh = GitHub("t", transport=self.fake)
        return brief_check.check(gh, REPO, ticket(body=body), [], TRUSTED, "main")

    def test_a_ticket_with_no_criteria_is_not_dispatchable(self):
        got = brief_check.criteria_verdict(self.NO_CRITERIA)
        self.assertEqual(got.kind, brief_check.CRITERIA)
        self.assertIn("names no acceptance criteria", got.reason)
        self.assertEqual(self.check(self.NO_CRITERIA).kind, brief_check.CRITERIA)

    def test_an_empty_criteria_section_is_no_criteria(self):
        self.assertIsNotNone(brief_check.criteria_verdict(self.EMPTY_SECTION))

    def test_a_ticket_with_criteria_is_dispatchable(self):
        self.assertIsNone(brief_check.criteria_verdict(self.WITH_CRITERIA))
        self.assertIsNone(self.check(self.WITH_CRITERIA))

    def test_the_short_acceptance_heading_counts(self):
        # The runner reads criteria under "## Acceptance" too; a ticket it
        # would build is not held.
        self.assertIsNone(brief_check.criteria_verdict(self.SHORT_HEADING))

    def test_an_answer_ticket_needs_no_criteria(self):
        # An answer brief opens no pull request; the runner's criteria park
        # never applied to it, and neither does this hold.
        self.assertIsNone(brief_check.criteria_verdict(self.ANSWER))
