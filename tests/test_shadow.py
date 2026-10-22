"""Shadow mode (bus/shadow.py, loom#1515): Loom runs as usual, but every
merge, issue close, ship or merge label and branch deletion is recorded in a
ledger instead of sent, until the operator takes a repo out of shadow.

Loom was paused after its Apps merged damaging lane pull requests unattended.
It resumes in shadow: these tests fail if any merge or close path still
reaches GitHub while LOOM_SHADOW=1, if the switch fails open, or if a repo
leaves shadow without the operator removing it.
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain, lock, shadow, station  # noqa: E402
from bus.github import GitHub, GitHubError, ShadowHeld  # noqa: E402

import test_bus_brain as tbb  # noqa: E402
import test_bus_station as tbs  # noqa: E402
import test_merge_tier as tmt  # noqa: E402

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
        ("POST", "/repos/%s/issues/9/labels" % REPO, {"labels": ["loom:shipped"]}, shadow.LABEL),
        ("PUT", "/repos/%s/issues/9/labels" % REPO, {"labels": ["loom:ticket", "ready-to-land"]},
         shadow.LABEL),
        ("PATCH", "/repos/%s/issues/9" % REPO, {"labels": ["loom:shipped"]}, shadow.LABEL),
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
                           ({"labels": ["loom:shipped"]}, "/repos/%s/issues/9/labels" % REPO)):
            with self.subTest(body=body):
                with self.assertRaises(ShadowHeld):
                    gh.rest("PATCH" if "state" in body else "POST", path, body)
        self.assertEqual(fake.sent, [])

    def test_closing_a_pull_request_unmerged_and_loom_signal_issues_goes_through(self):
        items = {"/repos/%s/issues/20" % REPO: issue(20, pr=True),
                 "/repos/%s/issues/21" % REPO: issue(21, ("loom:triage", "loom:invariant")),
                 "/repos/%s/issues/22" % REPO: issue(22, ("loom:factory",)),
                 "/repos/%s/issues/23" % REPO: issue(23, ("loom:triage", "loom:ticket")),
                 "/repos/%s/issues/24" % REPO: issue(24, ())}
        fake = Recorder(items)
        gh = GitHub("t", transport=fake)
        gh.rest("PATCH", "/repos/%s/pulls/19" % REPO, {"state": "closed"})
        for number in (20, 21, 22):
            gh.rest("PATCH", "/repos/%s/issues/%d" % (REPO, number),
                    {"state": "closed", "state_reason": "not_planned"})
        for number in (23, 24):   # a ticket, and an issue Loom cannot tell is its own
            with self.assertRaises(ShadowHeld):
                gh.rest("PATCH", "/repos/%s/issues/%d" % (REPO, number),
                        {"state": "closed", "state_reason": "not_planned"})
        self.assertEqual([p for _, p, _ in fake.sent],
                         ["/repos/%s/pulls/19" % REPO] +
                         ["/repos/%s/issues/%d" % (REPO, n) for n in (20, 21, 22)])

    def test_a_label_already_on_or_already_past_is_no_new_grant(self):
        items = {"/repos/%s/issues/30" % REPO: issue(30, ("loom:ticket", "loom:shipped")),
                 "/repos/%s/issues/31" % REPO: issue(31, ("loom:ticket", "loom:verified"))}
        fake = Recorder(items)
        gh = GitHub("t", transport=fake)
        gh.rest("PUT", "/repos/%s/issues/30/labels" % REPO, {"labels": ["loom:shipped", "x"]})
        # A failed audit moves a verified ticket back to shipped: not a ship.
        gh.rest("PUT", "/repos/%s/issues/31/labels" % REPO, {"labels": ["loom:shipped"]})
        gh.rest("POST", "/repos/%s/issues/30/labels" % REPO, {"labels": ["loom:merged"]})
        self.assertEqual(len(fake.sent), 3)

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
        shadow.record("label", REPO, 9, "issue", label="loom:shipped", now=1200)
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
    # issues it filed itself (loom:factory, a signal issue) once the check
    # passes again; holding it would hold every ticket the incident blocks.
    SHELL_ALLOWED = {("lanes/run_lane.sh", "gh issue close --repo \"$FACTORY_REPO\"")}

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


if __name__ == "__main__":
    unittest.main()
