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

    def merged_in_other_repo(self, number, body=None):
        self.fake.routes["/repos/%s/issues/206/timeline" % REPO] = [{
            "event": "cross-referenced", "source": {"type": "issue", "issue": {
                "number": number, "body": "Refs %s#206\n" % REPO if body is None else body,
                "title": "Ship it", "repository": {"full_name": OTHER},
                "pull_request": {"merged_at": iso(3600)}}}}]

    def test_a_merged_pull_request_naming_it_only_under_out_of_scope_does_not_hold_it(self):
        # loom#1856: a pull request names a ticket under Out of scope because
        # it does not build it; the stale check held such tickets on a person.
        for heading in ("## Out of scope", "### Not in scope", "**Follow-ups**"):
            with self.subTest(heading=heading):
                self.merged_in_other_repo(31, "## Why\nShip the caller.\n\nRefs %s#3\n\n%s\n"
                                              "- The key, %s#206.\n" % (OTHER, heading, REPO))
                self.assertIsNone(self.check())

    def test_a_merged_pull_request_naming_it_only_in_a_filed_note_does_not_hold_it(self):
        for note in ("(%s#206 filed for a way out of the yard)",
                     "Filed as %s#206 for the rest.",
                     "The rest is a follow-up: %s#206."):
            with self.subTest(note=note):
                self.merged_in_other_repo(31, "## Why\nShip the caller.\n\n" + note % REPO)
                self.assertIsNone(self.check())

    def test_a_refs_line_still_holds_it_beside_an_out_of_scope_section(self):
        self.merged_in_other_repo(31, "## Why\nShip it.\n\nRefs %s#206\n\n## Out of scope\n"
                                      "- Other work.\n" % REPO)
        got = self.check()
        self.assertEqual(got.key, "pr-%s#31" % OTHER)
        self.assertIn("names this ticket", brief_check.ask(got))
        # A Refs line counts even when it also says follow-up.
        self.merged_in_other_repo(32, "Refs %s#206 (a follow-up to the first slice)\n" % REPO)
        self.assertEqual(self.check().key, "pr-%s#32" % OTHER)

    def test_a_link_only_under_the_tickets_out_of_scope_does_not_hold_it(self):
        # The same rule for a pull request a person linked on the ticket.
        said = [comment("## Out of scope\n- https://github.com/%s/pull/45 covers the rest."
                        % OTHER)]
        self.fake.routes["/repos/%s/pulls/45" % OTHER] = {"merged_at": iso(3600), "title": "x"}
        self.assertIsNone(self.check(comments=said))

    def test_a_scrubbed_ask_on_a_public_repo_holds_it_once(self):
        # loom#2835: the public scrub rewrote the private repo named in the
        # marker key, the read-back saw another key, and the same evidence
        # held the ticket on every pass.
        from bus import text
        self.merged_in_other_repo(31)
        first = self.check()
        posted = text.scrub_public_body(brief_check.ask(first), REPO, [OTHER], [])
        self.assertNotIn(OTHER, posted)
        asked = comment(posted, login="loom-station[bot]", bot=True)
        self.assertIsNone(self.check(comments=[asked]))

    def test_a_raw_marker_written_on_a_private_repo_still_counts(self):
        self.merged_in_other_repo(31)
        raw = "<!-- loom:stale-brief pr-%s#31 -->\nKind: decision" % OTHER
        self.assertIsNone(self.check(comments=[comment(raw, login="loom-station[bot]",
                                                       bot=True)]))

    def test_new_evidence_still_holds_a_ticket_asked_about_before(self):
        from bus import text
        self.merged_in_other_repo(31)
        posted = text.scrub_public_body(brief_check.ask(self.check()), REPO, [OTHER], [])
        asked = comment(posted, login="loom-station[bot]", bot=True)
        self.merged_in_other_repo(32)
        got = self.check(comments=[asked])
        self.assertEqual(got.key, "pr-%s#32" % OTHER)

    def test_a_read_that_fails_holds_the_ticket(self):
        self.fake.routes["/repos/%s/issues/206/timeline" % REPO] = []
        self.fake.fail.add("/repos/%s/issues/206/timeline" % REPO)
        with self.assertRaises(brief_check.Unread):
            self.check()


class Area(unittest.TestCase):
    """A repo that files tickets by area of play (it has `area:` labels)
    refuses a ticket carrying none; a repo with none is not held."""

    def test_a_ticket_with_no_area_label_in_an_area_repo_gets_no_lane(self):
        got = brief_check.area_verdict(ticket(), ["area:billing", "area:search"])
        self.assertEqual(got.kind, brief_check.AREA)
        self.assertEqual(got.key, "area-206")
        self.assertIn("no `area:` label", got.reason)

    def test_a_ticket_with_an_area_label_passes(self):
        issue = ticket(labels=("loom:ticket", "loom:briefed", "area:billing"))
        self.assertIsNone(brief_check.area_verdict(issue, ["area:billing"]))

    def test_a_repo_with_no_area_labels_holds_nothing(self):
        self.assertIsNone(brief_check.area_verdict(ticket(), []))

    def test_a_bare_area_prefix_is_not_an_area(self):
        issue = ticket(labels=("loom:ticket", "area:"))
        self.assertIsNotNone(brief_check.area_verdict(issue, ["area:billing"]))

    def test_the_repo_areas_are_read_from_its_labels(self):
        fake = Route({"/repos/%s/labels" % REPO: [
            {"name": "area:billing"}, {"name": "bug"}, {"name": "area:search"}]})
        got = brief_check.area_labels(GitHub("t", transport=fake), REPO)
        self.assertEqual(got, ["area:billing", "area:search"])


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

    def test_a_ticket_with_no_area_label_in_an_area_repo_goes_to_the_brain(self):
        issue = ticket()
        fake = self.routes(issue)
        fake.routes["/repos/%s/labels" % REPO] = [{"name": "area:billing"}]
        self.assertTrue(self.station(fake)._brief_holds(REPO, issue, []))
        self.assertIn(["loom:ticket", "lane:codex", "loom:queued"], self.labels_put(fake))
        self.assertIn(("POST", "/repos/%s/issues/206/labels" % REPO, {"labels": ["needs-brain"]}),
                      fake.writes)
        notes = [b["body"] for m, p, b in fake.writes
                 if m in ("POST", "PATCH") and "/comments" in p]
        self.assertTrue(any("no `area:` label" in n for n in notes), notes)
        self.assertFalse(any("loom:stale-brief" in n for n in notes))

    def test_a_ticket_with_an_area_label_goes_on_to_the_stale_check(self):
        issue = ticket(labels=("loom:ticket", "loom:briefed", "lane:codex", "area:billing"))
        fake = self.routes(issue)
        fake.routes["/repos/%s/labels" % REPO] = [{"name": "area:billing"}]
        self.assertTrue(self.station(fake)._brief_holds(REPO, issue, []))
        asks = [b["body"] for m, p, b in fake.writes if m == "POST" and p.endswith("/comments")]
        self.assertEqual(len(asks), 1)
        self.assertIn("<!-- loom:stale-brief source-124 -->", asks[0])

    def test_an_area_label_read_that_fails_holds_it_and_writes_nothing(self):
        issue = ticket()
        fake = self.routes(issue)
        fake.routes["/repos/%s/labels" % REPO] = []
        fake.fail.add("/repos/%s/labels" % REPO)
        self.assertTrue(self.station(fake)._brief_holds(REPO, issue, []))
        self.assertEqual(fake.writes, [])

    def test_a_regression_needs_no_area_label(self):
        issue = ticket(labels=("loom:ticket", "loom:briefed", "lane:codex", "loom:regression"))
        fake = self.routes(issue)
        fake.routes["/repos/%s/labels" % REPO] = [{"name": "area:billing"}]
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

    def test_a_queued_two_platform_brief_gains_needs_brain_and_one_note(self):
        # loom#2929: the split hold (loom#1651) at the factory queue gate.
        self.queued(7, Split.MULTI)
        single = "Difficulty: medium\n\n" + Split.SINGLE
        self.queued(9, single)
        for _ in range(2):
            worker = factory.Factory(self.gh, [self.repo], NOW, trusted=["alice-example"])
            worker.assign_queued()
        self.assertIn("spans more than one platform", worker.waits["%s#7" % self.repo])
        self.assertEqual(self.lane_of(7), [])
        self.assertIn("needs-brain", [x["name"] for x in self.gh.issues[7]["labels"]])
        notes = [c for c in self.gh.comments.get(7, [])
                 if "more than one platform" in str(c.get("body"))]
        self.assertEqual(len(notes), 1)
        self.assertIn("After:", notes[0]["body"])
        self.assertNotIn("%s#9" % self.repo, worker.waits)
        self.assertNotEqual(self.lane_of(9), [])
        self.assertEqual(self.gh.issues[9]["body"], single)

    def test_a_diff_estimate_over_800_lines_asks_for_a_split_before_assignment(self):
        for number, size in ((7, "1,200"), (9, "800")):
            self.queued(number, "Difficulty: medium\n\n" + Split.SINGLE
                        + "\nEstimated diff: %s lines\n" % size)
        worker = factory.Factory(self.gh, [self.repo], NOW, trusted=["alice-example"])
        worker.assign_queued()
        self.assertEqual(self.lane_of(7), [])
        note = self.gh.comments[7][0]["body"]
        self.assertIn("1200 lines", note)
        self.assertIn("After:", note)
        self.assertNotEqual(self.lane_of(9), [])

    def test_a_ticket_with_no_area_label_in_an_area_repo_gets_no_lane_and_one_note(self):
        good = ("Difficulty: medium\n\n" + HEALTH_INTAKE
                + "\nCause: refunds post to the wrong account when two land at once.\n")
        self.gh.repo_labels = ["area:billing", "bug"]
        self.queued(7, good)
        self.queued(9, good, extra=("area:billing",))
        for _ in range(2):
            worker = factory.Factory(self.gh, [self.repo], NOW, trusted=["alice-example"])
            worker.assign_queued()
        self.assertEqual(worker.waits["%s#7" % self.repo], "carries no area: label")
        self.assertEqual(self.lane_of(7), [])
        self.assertIn("needs-brain", [x["name"] for x in self.gh.issues[7]["labels"]])
        notes = [c for c in self.gh.comments.get(7, []) if "no `area:` label" in str(c.get("body"))]
        self.assertEqual(len(notes), 1)
        self.assertNotIn("%s#9" % self.repo, worker.waits)
        self.assertNotEqual(self.lane_of(9), [])

    def test_a_repo_with_no_area_labels_holds_no_ticket_for_one(self):
        self.queued(9, "Difficulty: medium\n\n" + HEALTH_INTAKE
                    + "\nCause: refunds post to the wrong account when two land at once.\n")
        worker = factory.Factory(self.gh, [self.repo], NOW, trusted=["alice-example"])
        worker.assign_queued()
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

    def test_a_ticket_github_marks_blocked_by_an_open_issue_waits_until_it_closes(self):
        # The issue's own "blocked by" relationship, not a section in the
        # brief: a wayfinder ticket wired in the tracker carries only that.
        self.queued(7, "Difficulty: medium\n\n## Why\nx\n\n## Acceptance criteria\n- [ ] y\n")
        self.gh.issues[7]["issue_dependencies_summary"] = {"blocked_by": 1, "total_blocked_by": 1}
        self.gh.blockers = {7: [{"number": 15, "state": "open",
                                 "repository_url": "https://api.github.com/repos/%s" % self.repo}]}
        worker = self.assign()
        self.assertEqual(worker.waits["%s#7" % self.repo], "blocked on #15")
        self.assertIn("wait:blocked", self.names(7))
        self.assertFalse(any(n.startswith("lane:") for n in self.names(7)))
        self.gh.issues[7]["issue_dependencies_summary"] = {"blocked_by": 0, "total_blocked_by": 1}
        self.gh.blockers = {7: [{"number": 15, "state": "closed",
                                 "repository_url": "https://api.github.com/repos/%s" % self.repo}]}
        worker = self.assign()
        self.assertNotIn("%s#7" % self.repo, worker.waits)
        self.assertTrue(any(n.startswith("lane:") for n in self.names(7)))

    def test_a_blocker_in_another_repo_is_named_with_its_repo(self):
        self.queued(7, "Difficulty: medium\n\n## Why\nx\n\n## Acceptance criteria\n- [ ] y\n")
        self.gh.issues[7]["issue_dependencies_summary"] = {"blocked_by": 1}
        other = "example-owner/other"
        self.gh.blockers = {7: [{"number": 4, "state": "open",
                                 "repository_url": "https://api.github.com/repos/" + other}]}
        worker = self.assign()
        self.assertEqual(worker.waits["%s#7" % self.repo], "blocked on %s#4" % other)

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


class TestAsProof(unittest.TestCase):
    """Owner ruling of 2026-11-19 (#3277): proof belongs in a test that runs
    in CI or a throwaway environment at build time. A new build ticket names
    that test in an acceptance box; one that names none is refused where it
    is filed (the brief command and the rewrite), so it goes back to be
    rewritten before any lane sees it."""

    BASE = "## Why\n\nThe totals are wrong.\n\n## What\n\n- Fix the total.\n\n## Acceptance criteria\n"

    def body(self, *boxes):
        return self.BASE + "".join("- [ ] %s\n" % box for box in boxes)

    def test_a_build_ticket_whose_boxes_name_no_test_is_sent_back(self):
        got = brief_check.test_verdict(self.body("The total is right.", "The page shows it."))
        self.assertEqual(got.kind, brief_check.CRITERIA)
        self.assertTrue(got.key.startswith("test-"), got.key)
        self.assertIn("names the test that proves it", got.reason)
        for form in ("a test file path", "a command that runs in CI or a throwaway environment"):
            self.assertIn(form, got.reason)

    def test_a_box_naming_a_test_file_or_a_test_command_is_enough(self):
        for box in ("tests/test_totals.py covers the rounding case.",
                    "A new case in `bus/tests/test_totals.py` fails on main.",
                    "`internal/ledger/total_test.go` proves the sum.",
                    "`web/src/total.spec.ts` covers the empty cart.",
                    "`python3 -m unittest tests.test_totals` passes.",
                    "`uv run pytest -q tests/test_totals.py` passes.",
                    "`go test ./internal/ledger/...` passes.",
                    "`npm test -- total` passes in CI.",
                    "`bash scripts/tests/test_totals.sh` passes.",
                    "A test fails without the fix and passes with it.",
                    "A test fails without the cache.",
                    "A new test fails on main."):
            with self.subTest(box=box):
                self.assertIsNone(brief_check.test_verdict(self.body("The total is right.", box)))

    def test_a_mention_of_testing_that_names_no_test_is_not_enough(self):
        for box in ("The change is tested.", "Tested by hand on the phone.",
                    "The contest page still loads.", "CI is green."):
            with self.subTest(box=box):
                self.assertIsNotNone(brief_check.test_verdict(self.body(box)))

    def test_an_answer_ticket_stays_exempt(self):
        self.assertIsNone(brief_check.test_verdict("Deliverable: answer\n\n" + self.body("Say why.")))

    def test_a_ticket_with_no_criteria_is_left_to_the_criteria_verdict(self):
        self.assertIsNone(brief_check.test_verdict("## Why\n\nx\n\n## What\n\n- y\n"))

    def test_the_intake_templates_pass_with_their_templated_boxes(self):
        from bus import friction_intake, health
        for box in (health.LIVE_BOX, friction_intake.LIVE_BOX):
            with self.subTest(box=box):
                body = self.BASE + "- [ ] A test fails without the fix and passes with it.\n" + box + "\n"
                self.assertIsNone(brief_check.test_verdict(body))
                from bus import brain
                brain._check_live_criterion(body)


class Split(unittest.TestCase):
    """A brief that spans more than one platform is split before it reaches
    a lane (loom#1651): the multi-platform discovery brief behind this rule
    burned five lane attempts and the whole model ladder before a person
    split it by hand in minutes. The hold is at the same intake seam as the
    criteria hold, so the ticket costs no claim and no lane run."""

    # The discovery issue's brief as filed, with its project identifiers replaced.
    MULTI = ("Difficulty: hard\n\n"
             "## Why\n"
             "The app cannot discover other authenticated example devices on the same network. "
             "Currently, peers must be manually configured in the router's example.toml, and "
             "new devices like Android phones are invisible until an operator edits the config. "
             "The router only knows pre-configured legs and cannot provide visibility into "
             "unconfigured devices or function in client mode where two phones share no router view.\n"
             "## What\n"
             "Implement same-LAN service discovery using Bonjour (NWListener/NWBrowser on iOS, "
             "NSD on Android) for a `_example._tcp` service. Discovered services must prove "
             "identity by presenting credentials from the pairing ceremony before being trusted. "
             "Only verified peers should be offered as configurable legs, eliminating the need "
             "for manual router config edits.\n"
             "## Acceptance criteria\n"
             "- [ ] Implement Bonjour service advertisement and browsing for `_example._tcp` "
             "on iOS and NSD-based discovery on Android\n"
             "- [ ] Require discovered peers to present pairing credentials and verify them "
             "against claimed client id using existing Go primitives\n"
             "- [ ] Offer verified peers as configurable legs in the UI without requiring "
             "manual router config edits\n"
             "## Size\nM\n"
             "## Out of scope\n"
             "- Bluetooth peer discovery\n"
             "- Server-side lookup or centralized discovery mechanism\n"
             "- Trusting discovered services without credential verification\n\n"
             "Rewritten from issue 144.\n\nPart of #305\n")
    APP_AND_CORE = ("## Why\n\nThe app shows stale results because the core "
                    "never tells it a check finished.\n\n"
                    "## What\n\n- Change the app code to refresh.\n"
                    "- Change the Go core to emit the event.\n\n"
                    "## Acceptance criteria\n"
                    "- [ ] The app refreshes when the core emits.\n")
    SINGLE = ("## Why\n\nDiscovery is missing on one phone platform the "
              "field team carries.\n\n"
              "## What\n\n- Add iOS discovery.\n- Show it in the iOS list.\n\n"
              "## Acceptance criteria\n"
              "- [ ] iOS discovery lists nearby devices.\n")

    def setUp(self):
        self.fake = Route({"/repos/%s/issues/206/timeline" % REPO: [],
                           "/repos/%s/issues/144" % REPO: {"state": "open"}})

    def check(self, body):
        gh = GitHub("t", transport=self.fake)
        return brief_check.check(gh, REPO, ticket(body=body), [], TRUSTED, "main")

    def test_a_brief_naming_two_platforms_is_refused_at_queue_time(self):
        got = brief_check.split_verdict(self.MULTI)
        self.assertEqual(got.kind, brief_check.SPLIT)
        self.assertIn("split", got.reason)
        self.assertEqual(self.check(self.MULTI).kind, brief_check.SPLIT)

    def test_app_code_and_the_go_core_is_two_platforms(self):
        self.assertEqual(brief_check.split_verdict(self.APP_AND_CORE).kind,
                         brief_check.SPLIT)

    def test_a_single_platform_brief_queues_unchanged(self):
        self.assertIsNone(brief_check.split_verdict(self.SINGLE))
        self.assertIsNone(self.check(self.SINGLE))

    def test_a_large_diff_estimate_is_a_split_verdict_at_claim(self):
        found = self.check(self.SINGLE + "\nEstimated diff: 801 lines\n")
        self.assertEqual(found.kind, brief_check.SPLIT)
        self.assertIn("801 lines", found.reason)

    def test_the_ui_beside_one_platform_is_two_platforms(self):
        body = ("## Why\n\nDiscovery is missing on one phone platform.\n\n"
                "## What\n\n- Add iOS discovery.\n- Change the UI list.\n\n"
                "## Acceptance criteria\n"
                "- [ ] iOS discovery lists nearby devices.\n")
        self.assertEqual(brief_check.split_verdict(body).kind,
                         brief_check.SPLIT)

    def test_a_ui_mention_in_a_criterion_is_not_a_second_platform(self):
        body = ("## Why\n\nDiscovery is missing on one phone platform.\n\n"
                "## What\n\n- Add iOS discovery.\n\n"
                "## Acceptance criteria\n"
                "- [ ] iOS discovery lists nearby devices.\n"
                "- [ ] The UI test passes on the device list.\n")
        self.assertIsNone(brief_check.split_verdict(body))
        self.assertIsNone(self.check(body))

    def test_windows_in_the_time_sense_is_not_a_platform(self):
        # Usage windows and time windows beside one platform stay one ticket.
        for phrase in ("the end of its usage windows", "its time windows",
                       "the rate windows", "the reset windows",
                       "a window of 30 minutes", "the maintenance windows"):
            body = ("## Why\n\nA lane on the host stops early.\n\n"
                    "## What\n\n- The Linux host stops at %s.\n\n"
                    "## Acceptance criteria\n"
                    "- [ ] The Linux host reports %s.\n" % (phrase, phrase))
            self.assertIsNone(brief_check.split_verdict(body), phrase)

    def test_windows_as_a_platform_still_splits(self):
        body = ("## Why\n\nThe tool is missing on two hosts.\n\n"
                "## What\n\n- Build it on Windows.\n- Build it on Linux.\n\n"
                "## Acceptance criteria\n- [ ] Both builds pass.\n")
        self.assertEqual(brief_check.split_verdict(body).kind, brief_check.SPLIT)

    def test_a_split_no_line_with_a_reason_lifts_the_hold(self):
        body = self.MULTI + "\nSplit: no, one script runs on every platform\n"
        self.assertIsNone(brief_check.split_verdict(body))
        self.assertIsNone(self.check(body))

    def test_a_split_no_line_without_a_reason_does_not_count(self):
        for line in ("Split: no", "Split: no.", "Split: no\n\nOther text"):
            body = self.MULTI + "\n" + line + "\n"
            self.assertEqual(brief_check.split_verdict(body).kind,
                             brief_check.SPLIT, line)

    def test_platforms_named_only_in_the_why_do_not_split(self):
        body = ("## Why\n\niOS and Android users both asked for this, but "
                "the change itself is in the Go core only.\n\n"
                "## What\n\n- Change the Go core event.\n\n"
                "## Acceptance criteria\n"
                "- [ ] The Go core emits the event.\n")
        self.assertIsNone(brief_check.split_verdict(body))


if __name__ == "__main__":
    unittest.main()
