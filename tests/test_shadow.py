"""Shadow mode (bus/shadow.py, loom#1515): Loom runs as usual, but every
merge, issue close, merge label and branch deletion is recorded in a
ledger instead of sent, until the operator takes a repo out of shadow.

Loom was paused after its Apps merged damaging lane pull requests unattended.
It resumes in shadow: these tests fail if any merge or close path still
reaches GitHub while LOOM_SHADOW=1, if the switch fails open, or if a repo
leaves shadow without the operator removing it.
"""
from __future__ import annotations

import ast
import io
import json
import os
import re
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain, invariants, lock, shadow, station, watch  # noqa: E402
from bus.github import GitHub, GitHubError, ShadowHeld  # noqa: E402

import test_bus_brain as tbb  # noqa: E402
import test_bus_station as tbs  # noqa: E402
import test_merge_tier as tmt  # noqa: E402
import test_station_live_proof as tslp  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
REPO = "example-owner/example-repo"


class Recorder:
    """A transport that answers GETs from `items` (path -> JSON) and records
    every other request, as the wire would see it."""

    def __init__(self, items=None, fail_reads=False):
        self.items = items or {}
        self.fail_reads = fail_reads
        self.sent = []

    def __call__(self, method, url, headers, body):
        path = url.split("api.github.com", 1)[-1]
        if method == "GET":
            if self.fail_reads:
                return 502, {}, b"{}"
            data = self.items.get(path.split("?", 1)[0])
            return (200, {}, json.dumps(data).encode()) if data is not None else (404, {}, b"{}")
        self.sent.append((method, path, json.loads(body) if body else None))
        return 200, {}, json.dumps({"merged": True}).encode()


def issue(number, labels=(), pr=False):
    found = {"number": number, "state": "open", "body": "",
             "labels": [{"name": n} for n in labels]}
    if pr:
        found["pull_request"] = {"url": "x"}
    return found


class ShadowCase(unittest.TestCase):
    """Shadow on, every repo, with a ledger of the test's own."""

    ENV = {"LOOM_SHADOW": "1"}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ledger = os.path.join(tmp.name, "state", "ledger.jsonl")
        env = {k: v for k, v in os.environ.items() if not k.startswith("LOOM_SHADOW")}
        env.update(self.ENV, LOOM_SHADOW_LEDGER=self.ledger)
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def entries(self):
        return shadow.read(self.ledger)


class SwitchTest(unittest.TestCase):
    def test_only_an_explicit_off_turns_shadow_off(self):
        for value in ("0", "false", "OFF", " no "):
            with self.subTest(value=value):
                self.assertFalse(shadow.active("o/r", {"LOOM_SHADOW": value}))
        for value in ("1", "true", "", "yes please", "O"):
            with self.subTest(value=value):
                self.assertTrue(shadow.active("o/r", {"LOOM_SHADOW": value}))

    def test_an_unset_switch_is_shadow(self):
        # Muse's environment, or a plist written before this change.
        self.assertTrue(shadow.active("o/r", {}))
        self.assertTrue(shadow.active(None, {}))

    def test_a_switch_that_cannot_be_read_is_shadow(self):
        class Broken(dict):
            def get(self, *a, **k):
                raise OSError("unreadable")
        self.assertTrue(shadow.active("o/r", Broken()))
        self.assertEqual(shadow.scope(Broken()), (True, None))

    def test_a_repo_leaves_shadow_only_when_the_operator_removes_it(self):
        env = {"LOOM_SHADOW": "1", "LOOM_SHADOW_REPOS": "example-owner/a, Example-Owner/B"}
        self.assertTrue(shadow.active("example-owner/a", env))
        self.assertTrue(shadow.active("example-owner/b", env))
        self.assertFalse(shadow.active("example-owner/c", env))
        # A request that names no repo (a GraphQL mutation) stays held.
        self.assertTrue(shadow.active(None, env))

    def test_an_empty_star_or_malformed_repo_list_means_every_repo(self):
        for listed in ("", "  ", "*", "example-owner/a *", "example-owner/a not-a-repo"):
            with self.subTest(listed=listed):
                env = {"LOOM_SHADOW": "1", "LOOM_SHADOW_REPOS": listed}
                self.assertTrue(shadow.active("example-owner/z", env))

    def test_the_brain_prompt_line_reads_the_same_scope_as_the_gates(self):
        # loom#1960: the tick prompt must not call a graduated repo in shadow.
        listed = shadow.prompt_line({"LOOM_SHADOW": "1",
                                     "LOOM_SHADOW_REPOS": "example-owner/b example-owner/a"})
        self.assertIn("is on only for example-owner/a, example-owner/b", listed)
        self.assertIn("never merge, close an issue", listed)
        self.assertIn("Every other repo is out of shadow", listed)
        for env in ({}, {"LOOM_SHADOW": "1"}, {"LOOM_SHADOW": "1", "LOOM_SHADOW_REPOS": "*"},
                    {"LOOM_SHADOW": "yes", "LOOM_SHADOW_REPOS": "bad"}):
            with self.subTest(env=env):
                line = shadow.prompt_line(env)
                self.assertIn("is on for every repo: never merge", line)
                self.assertNotIn("out of shadow", line)
        off = shadow.prompt_line({"LOOM_SHADOW": "0", "LOOM_SHADOW_REPOS": "example-owner/a"})
        self.assertIn("is off (LOOM_SHADOW=0) for every repo", off)
        self.assertIn("only through bus.brain merge", off)
        for line in (listed, off):
            self.assertTrue(line.startswith("Shadow mode (MUSE.md) "), line)
            self.assertNotIn("\n", line)

    def test_the_prompt_command_prints_the_line(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"LOOM_SHADOW": "1",
                                          "LOOM_SHADOW_REPOS": "example-owner/a"}), \
                redirect_stdout(out):
            self.assertEqual(0, shadow.main(["prompt"]))
        self.assertEqual(shadow.prompt_line({"LOOM_SHADOW": "1",
                                             "LOOM_SHADOW_REPOS": "example-owner/a"}),
                         out.getvalue().strip())

    def test_off_ends_shadow_for_every_repo_whatever_the_list(self):
        env = {"LOOM_SHADOW": "0", "LOOM_SHADOW_REPOS": "example-owner/a"}
        self.assertFalse(shadow.active("example-owner/a", env))


class ChokePointTest(ShadowCase):
    """bus/github.py: every Python write to GitHub passes GitHub._rest, so
    each held shape is refused there whoever sends it."""

    HELD = [
        ("PUT", "/repos/%s/pulls/8/merge" % REPO, {"merge_method": "squash", "sha": "a" * 40},
         shadow.MERGE),
        ("POST", "/repos/%s/merges" % REPO, {"base": "main", "head": "x"}, shadow.MERGE),
        ("PATCH", "/repos/%s/issues/9" % REPO, {"state": "closed", "state_reason": "completed"},
         shadow.CLOSE),
        ("PATCH", "/repos/%s/issues/9" % REPO, {"state": "closed", "state_reason": "not_planned"},
         shadow.CLOSE),
        ("POST", "/repos/%s/issues/9/labels" % REPO, {"labels": ["ready-to-land"]}, shadow.LABEL),
        ("PUT", "/repos/%s/issues/9/labels" % REPO, {"labels": ["loom:ticket", "ready-to-land"]},
         shadow.LABEL),
        ("PATCH", "/repos/%s/issues/9" % REPO, {"labels": ["ready-to-land"]}, shadow.LABEL),
        ("PATCH", "/repos/%s/issues/9" % REPO, {"state": "open"}, shadow.REOPEN),
        ("POST", "/graphql", {"query": "mutation { reopenIssue(input:{issueId:\"x\"}) { issue { id } } }"},
         shadow.REOPEN),
        ("POST", "/repos/%s/pulls/8/reviews" % REPO, {"event": "APPROVE"}, shadow.APPROVE),
        ("DELETE", "/repos/%s/git/refs/heads/loom/9-work" % REPO, None, shadow.DELETE_BRANCH),
        ("POST", "/graphql", {"query": "mutation { mergePullRequest(input:{pullRequestId:\"x\"})"
                                       " { clientMutationId } }"}, shadow.MERGE),
        ("POST", "/graphql", {"query": "mutation($id:ID!) { enablePullRequestAutoMerge("
                                       "input:{pullRequestId:$id}) { clientMutationId } }"},
         shadow.MERGE),
        ("POST", "/graphql", {"query": "mutation { closeIssue(input:{issueId:\"x\"}) { issue { id } } }"},
         shadow.CLOSE),
        ("POST", "/graphql", {"query": "mutation($s:IssueState!) { updateIssue(input:{id:\"x\","
                                       "state:$s}) { issue { id } } }",
                              "variables": {"s": "CLOSED"}}, shadow.CLOSE),
    ]

    def gh(self, **kw):
        fake = Recorder({"/repos/%s/issues/9" % REPO: issue(9, ("loom:ticket", "loom:merged"))},
                        **kw)
        return GitHub("t", transport=fake), fake

    def test_each_irreversible_request_is_recorded_and_never_sent(self):
        for method, path, body, action in self.HELD:
            with self.subTest(method=method, path=path, body=body):
                gh, fake = self.gh()
                with self.assertRaises(ShadowHeld) as caught:
                    gh.rest(method, path, body)
                self.assertEqual(fake.sent, [])
                self.assertEqual(caught.exception.entry["action"], action)
        actions = {e["action"] for e in self.entries()}
        self.assertEqual(actions, set(shadow.ACTIONS))

    def test_a_shipped_label_is_a_state_fact_and_goes_through(self):
        # loom#2350: a move to loom:shipped must reach GitHub in shadow, or a
        # deployed ticket can never be verified and closed.
        gh, fake = self.gh()
        for method, path in (
                ("POST", "/repos/%s/issues/9/labels" % REPO),
                ("PUT", "/repos/%s/issues/9/labels" % REPO),
                ("PATCH", "/repos/%s/issues/9" % REPO)):
            gh.rest(method, path, {"labels": ["loom:shipped"]})
        self.assertEqual([m for m, _, _ in fake.sent], ["POST", "PUT", "PATCH"])
        self.assertEqual(self.entries(), [])

    def test_a_shipped_label_needs_no_read_of_the_item(self):
        gh, fake = self.gh(fail_reads=True)
        gh.rest("POST", "/repos/%s/issues/9/labels" % REPO, {"labels": ["loom:shipped"]})
        self.assertEqual(len(fake.sent), 1)
        self.assertEqual(self.entries(), [])

    def test_a_held_action_reads_as_a_refused_write_and_a_refused_gate(self):
        # Callers catch one or the other: either keeps a station pass going.
        gh, _ = self.gh()
        with self.assertRaises(GitHubError):
            gh.rest("PUT", "/repos/%s/pulls/8/merge" % REPO, {})
        with self.assertRaises(ValueError):
            gh.rest("PUT", "/repos/%s/pulls/8/merge" % REPO, {})

    def test_an_item_that_cannot_be_read_holds_the_close_and_the_label(self):
        gh, fake = self.gh(fail_reads=True)
        for body, path in (({"state": "closed"}, "/repos/%s/issues/9" % REPO),
                           ({"labels": ["ready-to-land"]}, "/repos/%s/issues/9/labels" % REPO)):
            with self.subTest(body=body):
                with self.assertRaises(ShadowHeld):
                    gh.rest("PATCH" if "state" in body else "POST", path, body)
        self.assertEqual(fake.sent, [])

    def test_closing_a_pull_request_unmerged_and_a_factory_incident_goes_through(self):
        items = {"/repos/%s/issues/20" % REPO: issue(20, pr=True),
                 "/repos/%s/issues/21" % REPO: issue(21, ("loom:triage", "loom:invariant")),
                 "/repos/%s/issues/22" % REPO: issue(22, ("loom:factory",)),
                 "/repos/%s/issues/23" % REPO: issue(23, ("loom:triage", "loom:ticket")),
                 "/repos/%s/issues/24" % REPO: issue(24, ()),
                 "/repos/%s/issues/25" % REPO: issue(25, ("loom:factory", "loom:invariant"))}
        fake = Recorder(items)
        gh = GitHub("t", transport=fake)
        gh.rest("PATCH", "/repos/%s/pulls/19" % REPO, {"state": "closed"})
        for number in (20, 22, 25):
            gh.rest("PATCH", "/repos/%s/issues/%d" % (REPO, number),
                    {"state": "closed", "state_reason": "not_planned"})
        # An invariant label with no breach marker in the body, a ticket, and
        # an issue Loom cannot tell is its own.
        for number in (21, 23, 24):
            with self.assertRaises(ShadowHeld):
                gh.rest("PATCH", "/repos/%s/issues/%d" % (REPO, number),
                        {"state": "closed", "state_reason": "not_planned"})
        self.assertEqual([p for _, p, _ in fake.sent],
                         ["/repos/%s/pulls/19" % REPO] +
                         ["/repos/%s/issues/%d" % (REPO, n) for n in (20, 22, 25)])
        self.assertEqual(sorted(e["number"] for e in self.entries()), [21, 23, 24])

    def test_a_merge_label_already_on_is_no_new_grant(self):
        items = {"/repos/%s/issues/30" % REPO: issue(30, ("loom:ticket", "ready-to-land"))}
        fake = Recorder(items)
        gh = GitHub("t", transport=fake)
        gh.rest("PUT", "/repos/%s/issues/30/labels" % REPO, {"labels": ["ready-to-land", "x"]})
        gh.rest("POST", "/repos/%s/issues/30/labels" % REPO, {"labels": ["loom:merged"]})
        self.assertEqual(len(fake.sent), 2)
        self.assertEqual(self.entries(), [])

    def test_a_new_merge_label_after_one_already_on_is_still_held(self):
        # Security review: only the first merge label was checked, so one
        # already on, listed first, let a new one through.
        items = {"/repos/%s/issues/32" % REPO: issue(32, ("loom:ticket", "loom:shipped"))}
        for method, body in (("POST", {"labels": ["loom:shipped", "ready-to-land"]}),
                             ("PUT", {"labels": [{"name": "loom:shipped"}, "Ready-To-Land"]})):
            with self.subTest(method=method):
                fake = Recorder(items)
                with self.assertRaisesRegex(ShadowHeld, "would add ready-to-land to"):
                    GitHub("t", transport=fake).rest(
                        method, "/repos/%s/issues/32/labels" % REPO, body)
                self.assertEqual(fake.sent, [])
        self.assertEqual([(e["action"], e["label"]) for e in self.entries()],
                         [("label", "ready-to-land")])

    def test_every_new_merge_label_is_recorded(self):
        fake = Recorder({"/repos/%s/issues/33" % REPO: issue(33, ("loom:ticket",))})
        with mock.patch.dict(shadow.MERGE_LABELS, {"another-label": ()}):
            with self.assertRaises(ShadowHeld):
                GitHub("t", transport=fake).rest(
                    "POST", "/repos/%s/issues/33/labels" % REPO,
                    {"labels": ["ready-to-land", "another-label"]})
            self.assertEqual(sorted(e["label"] for e in self.entries()),
                             ["another-label", "ready-to-land"])

    def test_a_held_merge_label_is_recorded_beside_a_shipped_one(self):
        fake = Recorder({"/repos/%s/issues/33" % REPO: issue(33, ("loom:ticket",))})
        with self.assertRaises(ShadowHeld):
            GitHub("t", transport=fake).rest("POST", "/repos/%s/issues/33/labels" % REPO,
                                             {"labels": ["ready-to-land", "loom:shipped"]})
        self.assertEqual([e["label"] for e in self.entries()], ["ready-to-land"])

    def test_a_label_list_it_cannot_read_is_held(self):
        for body in ({"labels": "ready-to-land"}, {"labels": [["ready-to-land"]]},
                     {"labels": [{"name": None}]}):
            with self.subTest(body=body):
                fake = Recorder({"/repos/%s/issues/34" % REPO: issue(34)})
                with self.assertRaises(ShadowHeld):
                    GitHub("t", transport=fake).rest(
                        "POST", "/repos/%s/issues/34/labels" % REPO, body)
                self.assertEqual(fake.sent, [])

    def test_the_brain_lock_branch_is_loom_own_and_may_be_deleted(self):
        self.assertTrue(lock.LOCK_BRANCH.startswith(shadow.OWN_BRANCH_PREFIX))
        fake = Recorder()
        GitHub("t", transport=fake).rest(
            "DELETE", "/repos/%s/git/refs/heads/%s" % (REPO, lock.LOCK_BRANCH))
        self.assertEqual(len(fake.sent), 1)

    def test_with_shadow_off_or_the_repo_graduated_the_merge_is_sent(self):
        for env in ({"LOOM_SHADOW": "0"},
                    {"LOOM_SHADOW": "1", "LOOM_SHADOW_REPOS": "example-owner/other"}):
            with self.subTest(env=env), mock.patch.dict(os.environ, env):
                gh, fake = self.gh()
                gh.rest("PUT", "/repos/%s/pulls/8/merge" % REPO, {})
                self.assertEqual([m for m, _, _ in fake.sent], ["PUT"])
        self.assertEqual(self.entries(), [])

    def test_a_ledger_that_cannot_be_written_still_holds_the_action(self):
        with open(os.path.join(os.path.dirname(os.path.dirname(self.ledger)), "state"), "w"):
            pass  # a file where the ledger's folder must go
        gh, fake = self.gh()
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            with self.assertRaises(ShadowHeld):
                gh.rest("PUT", "/repos/%s/pulls/8/merge" % REPO, {})
        self.assertIn("held anyway", err.getvalue())
        self.assertEqual(fake.sent, [])


class LedgerFailureTest(ShadowCase):
    """Review: a ledger write that fails in a way other than OSError must
    still hold the action, as a ShadowHeld every caller handles."""

    def test_a_ledger_it_cannot_decode_still_holds_the_merge(self):
        os.makedirs(os.path.dirname(self.ledger), exist_ok=True)
        with open(self.ledger, "wb") as fh:
            fh.write(b"\xff\xfe not utf-8\n")
        fake = Recorder()
        with self.assertRaises(ShadowHeld):
            GitHub("t", transport=fake).rest("PUT", "/repos/%s/pulls/8/merge" % REPO, {})
        self.assertEqual(fake.sent, [])
        # The line it cannot read is skipped, and the merge is still recorded.
        self.assertEqual([e["action"] for e in self.entries()], [shadow.MERGE])

    def test_a_record_that_raises_anything_still_holds_the_request(self):
        fake = Recorder({"/repos/%s/issues/9" % REPO: issue(9, ("loom:ticket",))})
        with mock.patch.object(shadow, "record", side_effect=TypeError("bad entry")), \
                mock.patch("sys.stderr", new_callable=io.StringIO):
            for method, path, body in (
                    ("PUT", "/repos/%s/pulls/8/merge" % REPO, {}),
                    ("POST", "/repos/%s/issues/9/labels" % REPO, {"labels": ["ready-to-land"]})):
                with self.subTest(path=path):
                    with self.assertRaises(ShadowHeld) as caught:
                        GitHub("t", transport=fake).rest(method, path, body)
                    self.assertIn(caught.exception.entry["action"], (shadow.MERGE, shadow.LABEL))
            self.assertEqual(shadow.main(["gh-guard", os.getcwd(), "pr", "merge", "8",
                                          "-R", REPO]), 1)
        self.assertEqual(fake.sent, [])


class CallSiteTest(ShadowCase):
    """The real merge and close paths, end to end on a fake GitHub."""

    def test_bus_brain_merge_runs_every_gate_then_records_instead_of_merging(self):
        fake = tmt.Route(tmt.routes(files=tmt.APP_CHANGE))
        with self.assertRaisesRegex(ShadowHeld, "would merge %s#8 at checked" % REPO):
            brain.merge(GitHub("t", transport=fake), REPO, 8, (REPO, 9),
                        env=dict(tmt.BrainMerge.ENV))
        self.assertFalse(tmt.BrainMerge.merged(fake))
        self.assertFalse(any(m == "DELETE" for m, _, _ in fake.writes))
        self.assertFalse(any("/labels" in p for _, p, _ in fake.writes))
        [entry] = self.entries()
        self.assertEqual((entry["action"], entry["repo"], entry["number"], entry["sha"]),
                         ("merge", REPO, 8, "checked"))
        self.assertEqual(entry["tier"], "app-code")
        self.assertIn("no operator hold", entry["gates"])
        self.assertEqual(entry["url"], "https://github.com/%s/pull/8" % REPO)

    def test_a_refused_gate_is_still_a_hold_not_a_shadow_record(self):
        fake = tmt.Route(tmt.routes(files=tmt.APP_CHANGE, conclusion="neutral"))
        with self.assertRaises(ValueError) as caught:
            brain.merge(GitHub("t", transport=fake), REPO, 8, (REPO, 9),
                        env=dict(tmt.BrainMerge.ENV))
        self.assertNotIsInstance(caught.exception, ShadowHeld)
        self.assertEqual(self.entries(), [])

    def test_the_brain_muse_runs_merges_nothing_when_its_environment_has_no_switch(self):
        # docs/MUSE.md: Muse runs `python3 -m bus.brain merge` from its own
        # checkout and environment. With no LOOM_SHADOW at all it is in shadow.
        os.environ.pop("LOOM_SHADOW", None)
        fake = tmt.Route(tmt.routes(files=tmt.APP_CHANGE))
        with self.assertRaises(ShadowHeld):
            brain.merge(GitHub("t", transport=fake), REPO, 8, (REPO, 9),
                        env=dict(tmt.BrainMerge.ENV))
        self.assertFalse(tmt.BrainMerge.merged(fake))

    def test_bus_brain_close_records_before_it_posts_or_closes(self):
        older = tbb.ProofTests._proof_comment(proof_record=tbb.record(revision="0000000"))
        comments = [older, tbb.ProofTests._proof_comment(login="other")]
        fake = tbb.Fake(tbb.reply(200, tbb.ticket("verified")), tbb.reply(200, comments))
        with self.assertRaisesRegex(ShadowHeld, "would close %s#9 as completed" % tbb.REPO):
            brain.close(GitHub("t", transport=fake), tbb.REPO, 9, note="Done.", env=tbb.ENV)
        self.assertEqual([c[0] for c in fake.calls], ["GET", "GET"])
        [entry] = self.entries()
        self.assertEqual(entry["action"], "close")
        self.assertIn("proof matches every criterion", entry["gates"])

    def test_the_station_done_close_leaves_the_ticket_open_and_unlabelled(self):
        case = tbs.CloseDoneTest("test_a_shipped_ticket_with_no_searchable_live_string_closes")
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.fake.private = False
        case.fake.add(5, lane="codex", body="Difficulty: easy\n\n## Acceptance criteria\n"
                      "- [ ] A test passes\n", state="shipped")
        station.Station(GitHub("token", transport=case.fake), case.settings).close_done_tickets()
        self.assertEqual(case.fake.issues[5]["state"], "open")
        self.assertNotIn("force-close", case.fake.names(5))
        self.assertEqual(case.fake.comments[5], [])
        [entry] = self.entries()
        self.assertEqual((entry["action"], entry["number"], entry["source"]),
                         ("close", 5, "station done close"))

    def test_the_station_live_proof_close_records_and_sends_nothing(self):
        # loom#1608: a shipped ticket whose live proof is met is verified and
        # closed by the station; in shadow the close is recorded before the
        # tick, the proof comment and the verified label.
        case = tslp.StationLiveProofTest("test_a_log_needle_closes_with_the_lines_quoted_once_its_window_has_passed")
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.ship(tslp.LOG_BODY, hours_ago=30)
        before = dict(case.fake.issues[1])
        case.close_pass(tslp.FakeLog(["%s worker: daily status ready" % tslp.stamp(case.now - 3600)]))
        self.assertEqual(case.fake.issues[1]["state"], "open")
        self.assertEqual(case.fake.issues[1]["body"], before["body"])
        self.assertNotIn("loom:verified", case.fake.names(1))
        self.assertEqual(case.fake.comments[1], [])
        self.assertEqual([m for m, _ in case.fake.calls if m != "GET"], [])
        [entry] = self.entries()
        self.assertEqual((entry["action"], entry["number"], entry["source"]),
                         ("close", 1, "station live proof"))


class ShipLabelTest(ShadowCase):
    """loom#2350: a move to loom:shipped is a state fact, not a merge or a
    close, so shadow sends it even though merges and closes stay held."""

    def ship_case(self):
        case = tbs.ShipRuleTest("test_a_ship_on_merge_repo_ships_its_merged_tickets")
        case.setUp()
        self.addCleanup(case.doCleanups)
        return case

    def test_a_deployed_ticket_in_a_shadow_repo_moves_to_shipped(self):
        case = self.ship_case()
        case.settings.ship_on_merge = [REPO]
        case.merged(12, 99)
        case.ship()
        self.assertIn("loom:shipped", case.fake.names(12))
        self.assertNotIn("loom:merged", case.fake.names(12))
        self.assertEqual(self.entries(), [])

    def test_a_label_shadow_still_holds_is_logged_once_and_not_as_a_failure(self):
        case = self.ship_case()
        pr = {"number": 99, "merge_commit_sha": "a" * 40}
        runner = station.Station(GitHub("token", transport=case.transport), case.settings)

        def held(*args, **kwargs):
            entry = shadow.record("label", REPO, 12, "issue", label="ready-to-land")
            raise ShadowHeld("shadow mode: would add ready-to-land to %s#12" % REPO, entry)

        with mock.patch("bus.brain.move", side_effect=held), \
                mock.patch.object(station, "_say") as say:
            runner._ship_one(REPO, 12, pr, "test", "merge")
            runner._ship_one(REPO, 12, pr, "test", "merge")
        lines = [str(c.args[0]) for c in say.call_args_list]
        self.assertEqual(len([line for line in lines if "shadow mode holds shipping" in line]), 1)
        self.assertFalse(any("could not ship" in line for line in lines))


class LedgerTest(ShadowCase):
    def test_the_same_action_is_written_once_and_a_new_head_again(self):
        for sha in ("a" * 40, "a" * 40, "b" * 40):
            shadow.record("merge", REPO, 8, "pr", sha=sha, tier="app", reason="r")
        self.assertEqual([e["sha"][:1] for e in self.entries()], ["a", "b"])
        with open(self.ledger) as fh:
            first = json.loads(fh.readline())
        for key in ("time", "repo", "number", "action", "tier", "gates", "reason", "sha", "url"):
            self.assertIn(key, first)

    def test_the_default_ledger_lives_in_loom_home_state(self):
        self.assertEqual(shadow.ledger_path({"LOOM_HOME": "/srv/loom"}),
                         "/srv/loom/state/shadow/ledger.jsonl")
        self.assertEqual(shadow.ledger_path({"LOOM_SHADOW_LEDGER": "/x/l.jsonl"}), "/x/l.jsonl")


class ReportTest(ShadowCase):
    def fill(self):
        shadow.record("merge", REPO, 8, "pr", sha="c" * 40, tier="app-code",
                      gates=["checks green and mergeable"], reason="ticket %s#9" % REPO,
                      now=1_800_000_000)
        shadow.record("close", "example-owner/other", 3, "issue", reason="close as completed",
                      now=1_800_000_100)
        shadow.record("merge", REPO, 7, "pr", sha="d" * 40, now=1_700_000_000)

    def run_report(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = shadow.main(["report", *argv])
        return code, out.getvalue()

    def test_report_lists_would_actions_since_a_time_with_links(self):
        self.fill()
        code, text = self.run_report("--since", "2027-01-01T00:00:00Z")
        self.assertEqual(code, 0)
        self.assertIn("2 would-actions since 2027-01-01T00:00:00Z", text)
        self.assertIn("https://github.com/%s/pull/8" % REPO, text)
        self.assertIn("https://github.com/example-owner/other/issues/3", text)
        self.assertIn("tier=app-code", text)
        self.assertIn("gates passed: checks green and mergeable", text)
        self.assertNotIn("/pull/7", text)
        self.assertIn("%s tier=app-code merge: 1" % REPO, text)

    def test_report_filters_by_repo_and_speaks_json(self):
        self.fill()
        code, text = self.run_report("--since", "2000-01-01", "--repo", "example-owner/other",
                                     "--json")
        self.assertEqual(code, 0)
        self.assertEqual([e["number"] for e in json.loads(text)], [3])

    def test_a_since_it_cannot_read_is_refused(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(shadow.main(["report", "--since", "last tuesday"]), 2)
        self.assertEqual(shadow.parse_since("24h", 100_000), 100_000 - 86400)

    def test_status_says_which_repos_are_in_shadow(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"LOOM_SHADOW_REPOS": "example-owner/a"}), \
                redirect_stdout(out):
            shadow.main(["status"])
        self.assertIn("shadow mode on for example-owner/a", out.getvalue())


class DigestTest(ShadowCase):
    def test_the_digest_counts_what_was_held_and_names_the_review_command(self):
        shadow.record("merge", REPO, 8, "pr", sha="c" * 40, now=1000)
        shadow.record("close", REPO, 9, "issue", now=1100)
        shadow.record("label", REPO, 9, "issue", label="ready-to-land", now=1200)
        lines = shadow.digest_lines(900, 2000, self.ledger)
        self.assertEqual(len(lines), 2)
        self.assertIn("Shadow mode (every repo): Loom would have done 1 merge, 1 close, "
                      "1 ship or merge label in 1 repo", lines[0])
        self.assertIn("python3 -m bus.shadow report --since 1970-01-01T00:15:00Z", lines[1])
        self.assertEqual(shadow.digest_lines(1500, 2000, self.ledger)[0],
                         "Shadow mode (every repo): nothing would have merged or closed "
                         "since the last digest.")

    def test_the_station_digest_carries_the_shadow_lines(self):
        case = tbs.ReportDigestTest("test_one_digest_at_each_time_lists_movers_and_counts_quiet_goals")
        case.setUp()
        self.addCleanup(case.doCleanups)
        os.environ.pop("LOOM_SHADOW_LEDGER")   # the station's own: LOOM_HOME/state/shadow
        case.goal(40, "Goal forty")
        case.at(9, 0)
        path = shadow.ledger_path({"LOOM_HOME": case.home})
        shadow.record("merge", REPO, 8, "pr", sha="c" * 40, now=case.base + 9 * 3600 + 60,
                      env={"LOOM_SHADOW_LEDGER": path})
        case.at(10, 1)
        self.assertEqual(len(case.sent), 1)
        text = case.sent[0]["content"]
        self.assertIn("Shadow mode (every repo): Loom would have done 1 merge in 1 repo", text)
        self.assertIn("python3 -m bus.shadow report --since", text)


class HoldFailsSafeTest(ShadowCase):
    """Security review of the label hold (loom#1515): every merge
    label in a request is read, and anything the hold cannot read holds the
    whole request."""

    def test_current_labels_it_cannot_read_hold_the_write(self):
        for labels in (5, None, "ready-to-land", {"name": "ready-to-land"}):
            with self.subTest(labels=labels):
                found = issue(40, ("loom:ticket",))
                found["labels"] = labels
                fake = Recorder({"/repos/%s/issues/40" % REPO: found})
                with self.assertRaises(ShadowHeld):
                    GitHub("t", transport=fake).rest(
                        "POST", "/repos/%s/issues/40/labels" % REPO,
                        {"labels": ["loom:merged", "ready-to-land"]})
                self.assertEqual(fake.sent, [])
        # An item read with no labels list at all is unreadable too.
        found = issue(41, ("ready-to-land",))
        del found["labels"]
        fake = Recorder({"/repos/%s/issues/41" % REPO: found})
        with self.assertRaises(ShadowHeld):
            GitHub("t", transport=fake).rest("POST", "/repos/%s/issues/41/labels" % REPO,
                                             {"labels": ["ready-to-land"]})
        self.assertEqual(fake.sent, [])

    def test_a_read_that_fails_in_any_way_holds_and_records_the_write(self):
        class Broken(Recorder):
            def __call__(self, method, url, headers, body):
                if method == "GET":
                    raise RuntimeError("the token could not be minted")
                return super().__call__(method, url, headers, body)
        for method, path, body in (
                ("POST", "/repos/%s/issues/42/labels" % REPO, {"labels": ["ready-to-land"]}),
                ("PATCH", "/repos/%s/issues/42" % REPO, {"state": "closed"})):
            with self.subTest(body=body):
                fake = Broken()
                with self.assertRaises(ShadowHeld):
                    GitHub("t", transport=fake).rest(method, path, body)
                self.assertEqual(fake.sent, [])
        self.assertEqual(sorted(e["action"] for e in self.entries()), ["close", "label"])

    def test_a_close_that_may_go_still_holds_a_new_merge_label_in_the_same_request(self):
        items = {"/repos/%s/issues/43" % REPO: issue(43, pr=True),
                 "/repos/%s/issues/44" % REPO: issue(44, ("loom:factory",))}
        for number in (43, 44):
            with self.subTest(number=number):
                fake = Recorder(items)
                with self.assertRaisesRegex(ShadowHeld, "would add ready-to-land"):
                    GitHub("t", transport=fake).rest(
                        "PATCH", "/repos/%s/issues/%d" % (REPO, number),
                        {"state": "closed", "labels": ["loom:triage", "ready-to-land"]})
                self.assertEqual(fake.sent, [])
        # With no merge label in it, the same close goes through.
        fake = Recorder(items)
        GitHub("t", transport=fake).rest("PATCH", "/repos/%s/issues/43" % REPO,
                                         {"state": "closed", "labels": ["loom:closed-unmerged"]})
        self.assertEqual(len(fake.sent), 1)

    def test_a_shipped_label_in_a_close_that_may_go_does_not_hold_it(self):
        items = {"/repos/%s/issues/43" % REPO: issue(43, pr=True)}
        fake = Recorder(items)
        GitHub("t", transport=fake).rest(
            "PATCH", "/repos/%s/issues/43" % REPO,
            {"state": "closed", "labels": ["loom:triage", "loom:shipped"]})
        self.assertEqual(len(fake.sent), 1)
        self.assertEqual(self.entries(), [])

    def test_a_triage_issue_closes_only_when_it_is_a_watch_signature(self):
        # loom:triage is on every issue not yet a ticket, a person's too: it
        # alone does not make an issue Loom's own signal issue.
        mine = issue(45, ("loom:triage",))
        mine["body"] = "%s\nstation: a signature" % watch.MARKER
        items = {"/repos/%s/issues/45" % REPO: mine,
                 "/repos/%s/issues/46" % REPO: issue(46, ("loom:triage",))}
        fake = Recorder(items)
        gh = GitHub("t", transport=fake)
        gh.rest("PATCH", "/repos/%s/issues/45" % REPO, {"state": "closed"})
        with self.assertRaises(ShadowHeld):
            gh.rest("PATCH", "/repos/%s/issues/46" % REPO,
                    {"state": "closed", "state_reason": "not_planned"})
        self.assertEqual([p for _, p, _ in fake.sent], ["/repos/%s/issues/45" % REPO])
        self.assertEqual(shadow.WATCH_MARKER, watch.MARKER)


class OtherGrantsTest(ShadowCase):
    """Adversarial review of the rest of the diff (loom#1515): an approving
    review, a write straight onto a branch Loom does not own, a merge-queue
    or label mutation, and a path spelled another way are held too."""

    def held(self, method, path, body=None, gh=None):
        fake = Recorder({"/repos/%s/issues/9" % REPO: issue(9, ("loom:ticket",))})
        with self.assertRaises(ShadowHeld) as caught:
            (gh or GitHub)("t", transport=fake).rest(method, path, body)
        self.assertEqual(fake.sent, [])
        return caught.exception.entry

    def sent(self, method, path, body=None):
        fake = Recorder()
        GitHub("t", transport=fake).rest(method, path, body)
        self.assertEqual(len(fake.sent), 1)

    def test_an_approving_review_is_held(self):
        for method, path, body in (
                ("POST", "/repos/%s/pulls/9/reviews" % REPO, {"event": "APPROVE", "body": "ok"}),
                ("POST", "/repos/%s/pulls/9/reviews" % REPO, "APPROVE"),
                ("POST", "/repos/%s/pulls/9/reviews/7/events" % REPO, {"event": "approve"}),
                ("POST", "/graphql", {"query": "mutation($id:ID!) { addPullRequestReview(input:"
                                               "{pullRequestId:$id, event:APPROVE}) { clientMutationId } }"}),
                ("POST", "/graphql", {"query": "mutation($id:ID!,$e:PullRequestReviewEvent!) {"
                                               " submitPullRequestReview(input:{pullRequestReviewId:$id,"
                                               " event:$e}) { clientMutationId } }",
                                      "variables": {"e": "APPROVE"}})):
            with self.subTest(path=path, body=body):
                self.assertEqual(self.held(method, path, body)["action"], shadow.APPROVE)
        self.sent("POST", "/repos/%s/pulls/9/reviews" % REPO, {"event": "COMMENT", "body": "x"})
        self.sent("POST", "/graphql", {"query": "mutation($id:ID!,$body:String!) { "
                                                "addPullRequestReviewThreadReply(input:{body:$body}) { "
                                                "comment { id } } }"})

    def test_a_write_onto_a_branch_loom_does_not_own_is_held(self):
        # A ref moved or a file committed onto the default branch lands code
        # with no pull request: a merge by another name.
        for method, path, body in (
                ("PATCH", "/repos/%s/git/refs/heads/main" % REPO, {"sha": "a" * 40, "force": True}),
                ("PATCH", "/repos/%s/git/refs/heads/release%%2F1" % REPO, {"sha": "a" * 40}),
                ("PUT", "/repos/%s/contents/README.md" % REPO, {"message": "m", "content": "eA=="}),
                ("PUT", "/repos/%s/contents/README.md" % REPO,
                 {"message": "m", "content": "eA==", "branch": "main"}),
                ("DELETE", "/repos/%s/contents/README.md" % REPO, {"message": "m", "sha": "b"}),
                ("PUT", "/repos/%s/contents/README.md" % REPO, "not an object"),
                ("POST", "/repos/%s/merge-upstream" % REPO, {"branch": "main"})):
            with self.subTest(method=method, path=path, body=body):
                self.assertEqual(self.held(method, path, body)["action"], shadow.MERGE)
        # Loom's own work branches (bus/lane.py, bus/pin.py, bus/roll_pin.py)
        # and the brain lock are written as before.
        self.sent("PATCH", "/repos/%s/git/refs/heads/loom%%2F12-fix-a-thing" % REPO,
                  {"sha": "a" * 40, "force": False})
        self.sent("PUT", "/repos/%s/contents/pins.env" % REPO,
                  {"message": "m", "content": "eA==", "sha": "c", "branch": "loom/pin-0123456789ab"})
        self.sent("PATCH", "/repos/%s/git/refs/heads/%s" % (REPO, lock.LOCK_BRANCH), {"sha": "a" * 40})

    def test_merge_queue_label_and_ref_mutations_are_held(self):
        for query, action in (
                ("mutation($id:ID!) { enqueuePullRequest(input:{pullRequestId:$id}) { clientMutationId } }",
                 shadow.MERGE),
                ("mutation { addLabelsToLabelable(input:{labelableId:\"x\", labelIds:[\"y\"]})"
                 " { clientMutationId } }", shadow.LABEL),
                ("mutation { updateIssue(input:{id:\"x\", labelIds:[\"y\"]}) { issue { id } } }",
                 shadow.LABEL),
                ("mutation { updatePullRequest(input:{pullRequestId:\"x\", labelIds:[\"y\"]})"
                 " { clientMutationId } }", shadow.LABEL),
                ("mutation { updateRefs(input:{repositoryId:\"x\", refUpdates:[]}) { clientMutationId } }",
                 shadow.MERGE),
                ("mutation($i:CreateCommitOnBranchInput!) { createCommitOnBranch(input:$i) { commit { oid } } }",
                 shadow.MERGE)):
            with self.subTest(query=query):
                self.assertEqual(self.held("POST", "/graphql", {"query": query})["action"], action)
        self.assertEqual(self.held("POST", "/graphql", ["not", "an", "object"])["action"],
                         shadow.MERGE)

    def test_a_path_spelled_another_way_is_still_held(self):
        for path in ("/repos/%s/pulls/8/merge#x" % REPO,
                     "/repos/%s//pulls/8/merge" % REPO,
                     "/repos/%s/pulls/8/%%6derge" % REPO,
                     "https://api.github.com/repos/%s/pulls/8/merge/" % REPO):
            with self.subTest(path=path):
                self.assertEqual(self.held("PUT", path, {})["action"], shadow.MERGE)
        # The method in lower case is the same request.
        self.assertEqual(self.held("put", "/repos/%s/pulls/8/merge" % REPO, {})["action"],
                         shadow.MERGE)
        # An API under a path prefix (GitHub Enterprise): its absolute URLs.
        api = "https://ghe.example.invalid/api/v3"
        self.assertEqual(self.held("PUT", api + "/repos/%s/pulls/8/merge" % REPO, {},
                                   gh=lambda t, transport: GitHub(t, api=api, transport=transport)
                                   )["action"], shadow.MERGE)

    def test_only_the_brain_lock_itself_may_be_deleted(self):
        for branch in ("loom-lock/../main", "loom-lock%2F..%2Fmain", "loom-lock/a b"):
            with self.subTest(branch=branch):
                self.assertEqual(self.held("DELETE", "/repos/%s/git/refs/heads/%s" % (REPO, branch))
                                 ["action"], shadow.DELETE_BRANCH)


class GhGuardTest(ShadowCase):
    """The brain rung's gh (lanes/mac/brain.sh): a model that holds a token
    may run gh itself, so in shadow its gh refuses and records what the
    choke point would hold."""

    HELD = [
        ["pr", "merge", "5", "-R", REPO, "--squash"],
        ["pr", "merge", "--auto", "https://github.com/%s/pull/5" % REPO],
        ["issue", "close", "5", "--repo", REPO],
        ["pr", "review", "5", "-R", REPO, "--approve"],
        ["pr", "review", "5", "-a", "-R", REPO],
        ["issue", "edit", "5", "-R", REPO, "--add-label", "ready-to-land"],
        ["api", "-X", "PUT", "repos/%s/pulls/5/merge" % REPO],
        ["api", "--method=PATCH", "/repos/%s/issues/5" % REPO, "-f", "state=closed"],
        ["api", "repos/%s/issues/5/labels" % REPO, "-f", "labels[]=ready-to-land"],
        ["api", "graphql", "-f", "query=mutation { mergePullRequest(input:{pullRequestId:\"x\"})"
                                 " { clientMutationId } }"],
        ["api", "-XPATCH", "repos/%s/issues/5" % REPO, "--input", "-"],
        ["api", "-X", "PUT", "repos/{owner}/{repo}/pulls/5/merge"],
        ["api", "-X", "PATCH", "repos/%s/git/refs/heads/main" % REPO, "-f", "sha=abc"],
    ]
    LET = [
        ["pr", "view", "5", "-R", REPO, "--json", "title"],
        ["pr", "close", "5", "-R", REPO, "--comment", "Superseded."],
        ["issue", "comment", "5", "-R", REPO, "--body", "ready-to-land soon"],
        ["issue", "edit", "5", "-R", REPO, "--add-label", "loom:answered"],
        ["issue", "edit", "5", "-R", REPO, "--add-label", "loom:shipped"],
        ["pr", "edit", "5", "-R", REPO, "--add-label=loom:merged,Loom:Shipped"],
        ["pr", "review", "5", "-R", REPO, "--comment", "-b", "x"],
        ["api", "repos/%s/pulls/5/merge" % REPO],
        ["api", "repos/%s/issues/5/comments" % REPO, "-f", "body=state=closed"],
        ["api", "graphql", "-f", "query=query { viewer { login } }"],
        ["--version"],
    ]

    def test_each_held_command_is_refused_and_recorded(self):
        # Narrowed to one repo: a command whose repo gh fills in itself
        # ({owner}/{repo}) is still held. An issue it cannot read is held.
        with mock.patch.dict(os.environ, {"LOOM_SHADOW_REPOS": REPO}), \
                mock.patch.object(shadow, "_gh_item", return_value=None):
            for argv in self.HELD:
                with self.subTest(argv=argv), mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                    self.assertEqual(shadow.main(["gh-guard", os.getcwd()] + argv), 1)
                    self.assertIn("shadow mode: would", err.getvalue())
            for argv in self.LET:
                with self.subTest(argv=argv):
                    self.assertEqual(shadow.main(["gh-guard", os.getcwd()] + argv), 0)
        self.assertEqual({e["source"] for e in self.entries()}, {"brain rung gh"})
        # The ledger keeps one line per action, item and label.
        self.assertEqual({e["action"] for e in self.entries()},
                         {shadow.MERGE, shadow.CLOSE, shadow.APPROVE, shadow.LABEL})

    def test_an_issue_close_reads_the_issue_and_lets_a_factory_incident_go(self):
        # run_lane.sh closes the factory incidents it filed through this
        # guard (--lane): holding one would hold every ticket behind it.
        def answer(labels):
            done = mock.Mock(returncode=0, stdout=json.dumps(issue(5, labels)))
            return mock.patch.object(shadow.subprocess, "run", return_value=done)
        close = ["issue", "close", "--repo", REPO, "5"]
        with mock.patch("sys.stderr", new_callable=io.StringIO):
            with answer(("loom:factory",)) as run:
                self.assertEqual(shadow.main(["gh-guard", "--lane", os.getcwd()] + close), 0)
            self.assertEqual(run.call_args[0][0], ["gh", "api", "repos/%s/issues/5" % REPO])
            for labels in (("loom:triage", "loom:invariant"), ("loom:factory", "loom:ticket")):
                with self.subTest(labels=labels), answer(labels):
                    self.assertEqual(shadow.main(["gh-guard", "--lane", os.getcwd()] + close), 1)
            failed = mock.Mock(returncode=1, stdout="")
            with mock.patch.object(shadow.subprocess, "run", return_value=failed):
                self.assertEqual(shadow.main(["gh-guard", "--lane", os.getcwd()] + close), 1)
            with answer(("loom:triage", "loom:invariant")):
                self.assertEqual(shadow.main(["gh-guard", os.getcwd(), "issue", "reopen", "5",
                                              "-R", REPO]), 1)
        self.assertEqual({(e["action"], e["source"]) for e in self.entries()},
                         {(shadow.CLOSE, "run_lane.sh gh"), (shadow.REOPEN, "brain rung gh")})

    def test_the_brain_rungs_issue_close_is_held_unread_even_for_an_incident(self):
        # The rung's model picks its own arguments: only run_lane.sh's own
        # closes (--lane) read the issue and may let one go.
        done = mock.Mock(returncode=0, stdout=json.dumps(issue(5, ("loom:factory",))))
        with mock.patch.object(shadow.subprocess, "run", return_value=done) as run, \
                mock.patch("sys.stderr", new_callable=io.StringIO):
            for argv in (["issue", "close", "5", "-R", REPO], ["issue", "reopen", "5", "-R", REPO],
                         ["api", "-X", "PATCH", "repos/%s/issues/5" % REPO, "-f", "state=closed"]):
                with self.subTest(argv=argv):
                    self.assertEqual(shadow.main(["gh-guard", os.getcwd()] + argv), 1)
        run.assert_not_called()

    def test_a_lane_close_that_names_its_issue_ambiguously_is_held_unread(self):
        # A value, a second number or a second URL must not let the read of
        # one issue close another (security review of loom#1551).
        other = "https://github.com/example-owner/other/issues/5"
        mine = "https://github.com/%s/issues/5" % REPO
        shapes = [
            ["5", "1542", "-R", REPO],            # two numbers
            [mine, "1542"],                       # a URL and a number
            [mine, mine],                         # two URLs
            [other, "-R", REPO],                  # a URL repo that is not -R
            ["5"],                                # no repo
            ["5", "-R", REPO, "--unknown", "x"],  # a flag gh may read a value for
            ["5", "-R"],                          # a flag with no value
            ["https://github.com/%s/pull/5" % REPO, "-R", REPO],
        ]
        done = mock.Mock(returncode=0, stdout=json.dumps(issue(5, ("loom:factory",))))
        with mock.patch.object(shadow.subprocess, "run", return_value=done) as run, \
                mock.patch("sys.stderr", new_callable=io.StringIO):
            for rest in shapes:
                with self.subTest(argv=rest):
                    self.assertEqual(shadow.main(["gh-guard", "--lane", os.getcwd(), "issue",
                                                  "close"] + rest), 1)
            run.assert_not_called()
            # The shapes gh reads as one issue are read and let go.
            for rest in (["5", "-R", REPO, "-c", "Cleared: see 1542"], ["--repo=%s" % REPO, "5"],
                         [mine, "--comment", "1542"], ["-R%s" % REPO, "-r", "completed", "5"],
                         ["-R", REPO, "--", "5"]):
                with self.subTest(argv=rest):
                    self.assertEqual(shadow.main(["gh-guard", "--lane", os.getcwd(), "issue",
                                                  "close"] + rest), 0)
        self.assertTrue(all(c.args[0] == ["gh", "api", "repos/%s/issues/5" % REPO]
                            for c in run.call_args_list))
        # A comment's value is never the selector: the issue read is the one
        # gh closes.
        for rest, number in ((["5", "-R", REPO, "-c", "1542"], 5),
                             (["-c", "5", "-R", REPO, "1542"], 1542)):
            with self.subTest(argv=rest), mock.patch.object(
                    shadow.subprocess, "run", return_value=done) as run, \
                    mock.patch("sys.stderr", new_callable=io.StringIO):
                shadow.main(["gh-guard", "--lane", os.getcwd(), "issue", "close"] + rest)
                self.assertEqual(run.call_args.args[0],
                                 ["gh", "api", "repos/%s/issues/%d" % (REPO, number)])

    def test_a_body_file_is_read_from_the_callers_folder(self):
        with tempfile.TemporaryDirectory() as here:
            with open(os.path.join(here, "close.json"), "w") as fh:
                json.dump({"state": "closed"}, fh)
            with open(os.path.join(here, "comment.json"), "w") as fh:
                json.dump({"body": "hello"}, fh)
            with mock.patch("sys.stderr", new_callable=io.StringIO), \
                    mock.patch.object(shadow, "_gh_item", return_value=issue(5, ("loom:ticket",))):
                self.assertEqual(shadow.main(["gh-guard", here, "api", "-X", "PATCH",
                                              "repos/%s/issues/5" % REPO, "--input", "close.json"]), 1)
                self.assertEqual(shadow.main(["gh-guard", here, "api", "-X", "POST",
                                              "repos/%s/issues/5/comments" % REPO,
                                              "--input", "comment.json"]), 0)
                # A body it cannot read is the worst body that path takes.
                self.assertEqual(shadow.main(["gh-guard", here, "api", "-X", "PATCH",
                                              "repos/%s/issues/5" % REPO, "--input", "gone.json"]), 1)

    def test_with_shadow_off_or_the_repo_graduated_gh_runs(self):
        for env in ({"LOOM_SHADOW": "0"}, {"LOOM_SHADOW_REPOS": "example-owner/other"}):
            with self.subTest(env=env), mock.patch.dict(os.environ, env):
                self.assertEqual(shadow.main(["gh-guard", os.getcwd(), "pr", "merge", "5",
                                              "-R", REPO]), 0)
        self.assertEqual(self.entries(), [])


class InvariantsCloseTest(ShadowCase):
    """loom#1551: the invariants step closed two cleared breach issues for
    real at 00:42Z on the first shadow night ("closed cleared"). Run on the
    real GitHub client with only its HTTP layer replaced, the step's clear
    path records the close and sends no close and no note."""

    NOW = 1_800_000_000.0
    NUMBER = 77

    def wire(self, state="open", labels=("loom:triage", "loom:invariant"),
             key="goal-epics-over-cap/ad09a1c365b8", age=86400):
        breach = {"number": self.NUMBER, "state": state, "title": "Lane pull request #5 open",
                  "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.NOW - age)),
                  "closed_at": None if state == "open" else time.strftime(
                      "%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.NOW - 3600)),
                  "body": invariants.MARKER % key + "\nA breach.",
                  "labels": [{"name": name} for name in labels]}
        sent = []

        def transport(method, url, headers, body):
            path = url.split("api.github.com", 1)[-1]
            bare = path.split("?", 1)[0]
            if method != "GET":
                sent.append((method, bare, json.loads(body) if body else None))
                return 200, {}, b"{}"
            if bare == "/repos/%s/issues" % REPO:
                return 200, {}, json.dumps([breach]).encode()
            if bare == "/repos/%s/issues/%d" % (REPO, self.NUMBER):
                return 200, {}, json.dumps(breach).encode()
            if bare == "/repos/%s/issues/%d/comments" % (REPO, self.NUMBER):
                return 200, {}, b"[]"
            return 404, {}, b"{}"
        return transport, sent

    def run_filer(self, transport, breaches=(), **kwargs):
        said = []
        with mock.patch("bus.github.urllib_transport", transport):
            gh = GitHub("t")
            invariants.Filer(gh, REPO, 30, {}, self.NOW, said.append,
                             **kwargs).run(list(breaches))
        return said

    def test_a_settled_close_is_held_in_shadow_and_recorded(self):
        # loom#2022: the settled close asks the same guard before its note.
        subject = "%s#5" % REPO
        key = invariants.Breach("lane-pr-stalled", subject, "", (), 0.0).key
        transport, sent = self.wire(labels=("loom:triage", "loom:invariant", "loom:recurring"),
                                    key=key, age=600)
        said = self.run_filer(transport, subjects={key: subject},
                              settled=lambda inv, sub: "lane pull request #5 merged at x")
        self.assertEqual(sent, [], "shadow mode sent a close or a note")
        [entry] = self.entries()
        self.assertEqual((entry["action"], entry["number"], entry["source"]),
                         (shadow.CLOSE, self.NUMBER, "station invariants"))
        self.assertIn("settled", entry.get("reason", ""))
        self.assertTrue(any("would close" in line for line in said), said)
        self.assertFalse(any("closed settled" in line for line in said), said)

    def test_a_cleared_breach_closes_in_shadow_too(self):
        # 2026-10-26: 35 breach issues the station had marked cleared stayed
        # open under shadow and the operator asked why Loom did not clean up.
        transport, sent = self.wire()
        said = self.run_filer(transport)
        self.assertIn(("PATCH", "/repos/%s/issues/%d" % (REPO, self.NUMBER),
                       {"state": "closed", "state_reason": "not_planned"}), sent)
        self.assertTrue(any("closed cleared" in line for line in said), said)
        self.assertEqual(self.entries(), [])

    def test_a_recurring_breach_stays_held_in_shadow(self):
        transport, sent = self.wire(labels=("loom:triage", "loom:invariant", "loom:recurring"))
        said = self.run_filer(transport)
        closes = [r for r in sent if isinstance(r[2], dict) and r[2].get("state") == "closed"]
        self.assertEqual(closes, [], "shadow mode sent a close: %s" % closes)
        self.assertEqual(sent, [], "a held close posts no note either")
        [entry] = self.entries()
        self.assertEqual((entry["action"], entry["number"], entry["source"]),
                         (shadow.CLOSE, self.NUMBER, "station invariants"))
        self.assertTrue(any("would close" in line for line in said), said)
        self.assertFalse(any("closed cleared" in line for line in said), said)
        # The next pass tries again and adds nothing to the ledger or the log.
        said = self.run_filer(transport)
        self.assertEqual((sent, len(self.entries())), ([], 1))
        self.assertFalse(any("would close" in line for line in said), said)

    def test_with_shadow_off_the_cleared_issue_closes(self):
        transport, sent = self.wire()
        with mock.patch.dict(os.environ, {"LOOM_SHADOW": "0"}):
            said = self.run_filer(transport)
        self.assertIn(("PATCH", "/repos/%s/issues/%d" % (REPO, self.NUMBER),
                       {"state": "closed", "state_reason": "not_planned"}), sent)
        self.assertTrue(any("closed cleared" in line for line in said), said)
        self.assertEqual(self.entries(), [])


class EveryPathTest(unittest.TestCase):
    """By symbol, across the whole tree: every merge, close or branch delete
    is a GitHub.rest call (held at the choke point) or a listed exception."""

    SHAPES = re.compile(
        r"/merge\b|/merges\b|\"state\": \"closed\"|'state': 'closed'|state=closed|"
        r"git/refs/heads|mergePullRequest|closeIssue|AutoMerge|deleteRef|"
        r"gh pr merge|gh issue close")
    # A gh CLI call from Python: its first two words.
    GH_ARGV = re.compile(r"\[\s*[\"']gh[\"']\s*,\s*[\"'](\w+)[\"']\s*,\s*[\"'](\w+)[\"']")
    # The one write outside Python: run_lane.sh closes the factory incident
    # issues it filed itself once the check passes again. It asks the gh
    # guard first (test_every_shell_close_asks_the_gh_guard_first), which
    # lets a factory incident go: holding it would hold every ticket behind it.
    SHELL_ALLOWED = {("lanes/run_lane.sh", "gh issue close --repo \"$FACTORY_REPO\"")}
    # An issue close or reopen, as Python writes it or as gh does.
    CLOSE_SHAPE = re.compile(r"""[\"']state[\"']\s*:\s*[\"'](closed|open)[\"']|"""
                             r"\b(closeIssue|reopenIssue)\b")
    SHELL_CLOSE = re.compile(r"\bgh\s+issue\s+(close|reopen)\b|"
                             r"\bgh\s+api\b.*\bstate=(closed|open)\b")
    # Every Python function that closes or reopens: bus/closing.py alone
    # (loom#2444), which asks the guard before its note, so a held close never
    # says it closed. Its request still goes through GitHub.rest, where
    # _hold_in_shadow holds it. The value is what the site asks itself ("":
    # the asking is in _change, checked below).
    CLOSE_SITES = {
        ("bus/closing.py", "close"): "",
        ("bus/closing.py", "close_pull"): "",   # a pull request: let through
        ("bus/closing.py", "reopen"): "",
    }
    # A state of "closed" or "open" that is no write: the guard's own tables
    # and a dashboard row.
    NOT_WRITES = {("bus/shadow.py", "<module>"), ("dashboard/free_view.py", "_ticket_shape")}
    GUARDS = ("_held", "guard")

    def files(self):
        for top in ("bus", "dispatch", "lanes", "dashboard"):
            for path in sorted((ROOT / top).rglob("*")):
                if path.suffix in (".py", ".sh", ".js", ".mjs") and path.is_file():
                    yield path

    def test_no_merge_or_close_bypasses_the_github_client(self):
        found = []
        for path in self.files():
            rel = str(path.relative_to(ROOT))
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                code = line.split("#", 1)[0] if path.suffix in (".py", ".sh") else line
                if not self.SHAPES.search(code) or "?state=closed" in code \
                        or "pulls?state=closed" in code or "state=closed&" in code:
                    continue
                if path.suffix == ".py":
                    continue  # Python reaches GitHub only through bus.github (below)
                if not any(rel == f and s in code for f, s in self.SHELL_ALLOWED):
                    found.append("%s: %s" % (rel, code.strip()))
        self.assertEqual(found, [])

    @staticmethod
    def _call_name(node):
        func = node.func
        return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")

    def close_sites(self):
        """(file, function) -> the first line of each issue close or reopen
        found by grep, with the function's source; guard calls left out."""
        found = {}
        for path in self.files():
            if path.suffix != ".py":
                continue
            text = path.read_text(encoding="utf-8")
            lines = text.splitlines()
            hits = [i + 1 for i, line in enumerate(lines)
                    if self.CLOSE_SHAPE.search(line.split("#", 1)[0])]
            if not hits:
                continue
            tree = ast.parse(text)
            defs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
            for line in hits:
                around = [c for c in calls if c.lineno <= line <= c.end_lineno]
                inner = min(around, key=lambda c: c.end_lineno - c.lineno) if around else None
                if inner is not None and self._call_name(inner) in self.GUARDS:
                    continue
                owner = [d for d in defs if d.lineno <= line <= d.end_lineno]
                fn = min(owner, key=lambda d: d.end_lineno - d.lineno) if owner else None
                key = (str(path.relative_to(ROOT)), fn.name if fn else "<module>")
                if key not in found:
                    src = "\n".join(lines[fn.lineno - 1:line]) if fn else ""
                    found[key] = (line, src, inner is not None and self._call_name(inner) == "rest")
        return found

    def test_every_issue_close_and_reopen_goes_through_the_shadow_guard(self):
        found = self.close_sites()
        self.assertEqual(sorted(set(found) - self.NOT_WRITES), sorted(self.CLOSE_SITES),
                         "a new issue close or reopen: route it through the shadow guard, "
                         "then list it in CLOSE_SITES")
        for key, asks in sorted(self.CLOSE_SITES.items()):
            line, before, _ = found[key]
            with self.subTest(site=key, line=line):
                if asks:
                    self.assertIn(asks, before, "%s:%d closes before it asks the guard" % (key[0], line))
        closes = (ROOT / "bus" / "closing.py").read_text(encoding="utf-8")
        self.assertLess(closes.index("_check_not_held(gh, path, change)"),
                        closes.index("notify.post("))
        # The choke point every one of them sends through.
        rest = (ROOT / "bus" / "github.py").read_text(encoding="utf-8")
        self.assertRegex(rest, r"def _rest\(self[^)]*\)[^:]*:\n(?:.*\n){0,4}\s+(?:acted = )?self\._hold_in_shadow\(")

    def test_every_shell_close_asks_the_gh_guard_first(self):
        unguarded = []
        for path in self.files():
            if path.suffix != ".sh":
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            for i, line in enumerate(lines):
                code = line.split("#", 1)[0]
                if not self.SHELL_CLOSE.search(code):
                    continue
                if not any("bus.shadow gh-guard" in prior for prior in lines[max(0, i - 4):i]):
                    unguarded.append("%s:%d: %s" % (path.relative_to(ROOT), i + 1, code.strip()))
        self.assertEqual(unguarded, [])

    def test_python_reaches_github_only_through_bus_github(self):
        senders = []
        for path in self.files():
            if path.suffix != ".py" or path.name == "github.py" and path.parent.name == "bus":
                continue
            text = path.read_text(encoding="utf-8")
            if "api.github.com" in text:
                senders.append(str(path.relative_to(ROOT)))
            # A gh CLI call may read or edit a pull request's text, never
            # merge or close (bus/pr_repair.py runs gh pr view and edit).
            for verb in self.GH_ARGV.findall(text):
                if verb[1] in ("merge", "close") or verb[0] == "api":
                    senders.append("%s: gh %s %s" % (path.relative_to(ROOT), *verb))
        self.assertEqual(senders, [])

    def test_the_muse_and_brain_docs_name_the_switch(self):
        muse = (ROOT / "docs" / "MUSE.md").read_text()
        self.assertIn("LOOM_SHADOW", muse)
        self.assertIn("never merge or close", muse.lower())
        brain_doc = (ROOT / "docs" / "BRAIN.md").read_text()
        self.assertIn("## Shadow mode", brain_doc)
        self.assertIn("LOOM_SHADOW_REPOS", brain_doc)


class ActedTest(ShadowCase):
    """An irreversible request Loom sends (its repo out of shadow, or a close
    let through) leaves one line in acted.jsonl, for the /chain audit."""

    ENV = {"LOOM_SHADOW": "1", "LOOM_SHADOW_REPOS": "example-owner/held"}

    def acted(self):
        return shadow.read_acted(50)

    def test_a_let_through_merge_is_recorded_and_a_held_one_is_not(self):
        fake = Recorder()
        GitHub("t", transport=fake).rest("PUT", "/repos/%s/pulls/8/merge" % REPO, {})
        with self.assertRaises(ShadowHeld):
            GitHub("t", transport=fake).rest("PUT", "/repos/example-owner/held/pulls/9/merge", {})
        rows = self.acted()
        self.assertEqual([(r["action"], r["repo"], r["number"], r["kind"]) for r in rows],
                         [(shadow.MERGE, REPO, 8, "pr")])
        self.assertTrue(rows[0]["at"])
        self.assertTrue(rows[0]["source"])
        self.assertEqual(shadow.acted_path(), os.path.join(os.path.dirname(self.ledger), "acted.jsonl"))
        self.assertEqual(shadow.acted_path({"LOOM_HOME": "/srv/loom"}),
                         "/srv/loom/state/shadow/acted.jsonl")

    def test_a_failed_write_never_blocks_the_request(self):
        fake = Recorder()
        with mock.patch.object(shadow, "acted_path", return_value="/dev/null/nope/acted.jsonl"), \
                mock.patch.object(shadow, "_acted_warned", False), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            GitHub("t", transport=fake).rest("PUT", "/repos/%s/pulls/8/merge" % REPO, {})
            GitHub("t", transport=fake).rest("PUT", "/repos/%s/pulls/9/merge" % REPO, {})
        self.assertEqual(len(fake.sent), 2)
        self.assertEqual(err.getvalue().count("shadow: could not write"), 1)

    def test_the_file_keeps_the_last_500_once_past_1000(self):
        for number in range(1, shadow.ACTED_MAX + 2):
            shadow.record_acted(shadow.MERGE, REPO, number, "pr", "test")
        rows = shadow.read_acted(shadow.ACTED_MAX * 2)
        self.assertEqual(len(rows), shadow.ACTED_KEEP)
        self.assertEqual(rows[-1]["number"], shadow.ACTED_MAX + 1)
        self.assertEqual([r["number"] for r in shadow.read_acted(2)], [shadow.ACTED_MAX, shadow.ACTED_MAX + 1])


if __name__ == "__main__":
    unittest.main()
