"""bus/deploy_watch.py: a Loom auto-merge whose deploy fails is reverted.

The cases are the audit's of 2026-10-22, with names made generic: a canary
aborted at its progress deadline and a Degraded Rollout (the deployment
repo's sync workflow reports both as annotation titles), a reusable build
workflow that passed on the pull request and failed after merge on every
build node but one, and a dev-world config roll whose seed step crash-looped
the StatefulSet that runs it.
"""
from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bus import deploy_watch, station  # noqa: E402
from bus.github import GitHub  # noqa: E402

REPO = "example-owner/example-deploy"
NOW = 2_000_000_000.0
STATION = "loom-station[bot]"
LANES = "loom-lanes[bot]"
ENV = {"LOOM_BRIEF_AUTHORS": "%s %s" % (STATION, LANES), "LOOM_MERGE_SETTLE_SECONDS": "0"}
MERGED = "a" * 40
SYNC = "sync.manifests.yml"
SIGNALS = "workflow %s production/manifests/**\n" % SYNC
FORWARD = "@@ -1 +1 @@\n-image: app:1\n+image: app:2"
INVERSE = "@@ -1 +1 @@\n-image: app:2\n+image: app:1"
CHANGED = "production/manifests/app/rollout.yaml"


def iso(seconds_ago):
    return datetime.fromtimestamp(NOW - seconds_ago, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def note(title, message, level="failure"):
    return {"title": title, "message": message, "annotation_level": level}


class FakeGitHub:
    """GitHub by path: GETs answer from `routes` (longest prefix first);
    writes are recorded and answered by `answer`."""

    def __init__(self):
        self.routes = {}
        self.writes = []
        self.gone = set()
        self.revert_number = 77

    def __call__(self, method, url, headers, body):
        path = url.split("api.github.com")[-1]
        data = json.loads(body) if body else None
        if path == "/graphql":
            query = data["query"]
            if "revertPullRequest" in query:
                self.writes.append(("REVERT", path, data["variables"]))
                return 200, {}, json.dumps({"data": {"revertPullRequest": {
                    "revertPullRequest": {"number": self.revert_number}}}}).encode()
            return 200, {}, json.dumps({"data": {"repository": {
                "pullRequest": self.routes.get("graph", {})}}}).encode()
        if method != "GET":
            self.writes.append((method, path, data))
            if path == "/repos/%s/issues" % REPO and method == "POST":
                return 201, {}, json.dumps({"number": 60}).encode()
            return 200, {}, json.dumps({"merged": path.endswith("/merge")}).encode()
        for prefix in sorted(self.routes, key=len, reverse=True):
            if path.startswith(prefix) and prefix != "graph":
                if prefix in self.gone:
                    return 422, {}, b'{"message": "unprocessable"}'
                return 200, {}, json.dumps(self.routes[prefix]).encode()
        return 404, {}, b'{"message": "Not Found"}'


def lane_pr(number=41, ticket=40, title="Roll the app to 2", merged_ago=600,
            merger=STATION, merger_type="Bot", head="loom/40-roll", body=None):
    return {"number": number, "title": title, "user": {"login": LANES},
            "body": body if body is not None else "Refs %s#%d\n" % (REPO, ticket),
            "merged_at": iso(merged_ago), "merge_commit_sha": MERGED, "node_id": "PR_n41",
            "head": {"ref": head, "sha": "h" * 40}, "base": {"ref": "main"},
            "merged_by": {"login": merger, "type": merger_type}}


class Base(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        self.state = tempfile.mkdtemp()
        self.pages = []
        self.page_answer = True
        self.merge_pr()
        self.signal_file(SIGNALS)
        self.gh.routes["/repos/%s/commits/%s" % (REPO, MERGED)] = {
            "files": [{"filename": CHANGED, "patch": FORWARD}]}
        self.gh.routes["/repos/%s/issues?labels=" % REPO] = []

    def merge_pr(self, **kw):
        pr = lane_pr(**kw)
        self.gh.routes["/repos/%s/pulls?state=closed" % REPO] = [pr]
        self.gh.routes["/repos/%s/pulls/%d" % (REPO, pr["number"])] = pr
        self.gh.routes["/repos/%s/issues/40" % REPO] = {
            "number": 40, "state": "closed", "body": "## What\nRoll the app.\n",
            "labels": [{"name": "loom:ticket"}, {"name": "loom:shipped"},
                       {"name": "lane:codex"}]}

    def signal_file(self, content):
        self.gh.routes["/repos/%s/contents/.loom/deploy-signal" % REPO] = {
            "encoding": "base64", "content": base64.b64encode(content.encode()).decode()}

    def sync_run(self, conclusion="failure", status="completed", notes=(), head=MERGED):
        self.gh.routes["/repos/%s/actions/workflows/%s/runs" % (REPO, SYNC)] = {
            "workflow_runs": [{"id": 900, "head_sha": head, "status": status,
                               "conclusion": conclusion if status == "completed" else None}]}
        self.gh.routes["/repos/%s/actions/runs/900/jobs" % REPO] = {"jobs": [
            {"id": 901, "check_run_url": "https://api.github.com/repos/%s/check-runs/901" % REPO}]}
        self.gh.routes["/repos/%s/check-runs/901/annotations" % REPO] = list(notes)

    def page(self, message):
        self.pages.append(message)
        return self.page_answer

    def watch(self, now=NOW, **kw):
        w = deploy_watch.Watch(GitHub("t", transport=self.gh), [REPO], now,
                               trusted=[STATION, LANES], state_dir=self.state,
                               page=self.page, env=dict(ENV), **kw)
        with contextlib.redirect_stdout(io.StringIO()):
            w.run()
        return w

    def entry(self):
        with open(os.path.join(self.state, deploy_watch.LEDGER)) as fh:
            return json.load(fh)["merges"]["%s#41" % REPO]

    def revert_ready(self, labeler=STATION, inverse=INVERSE, rollup="SUCCESS"):
        """The revert pull request #77 GitHub opened, for revert ticket #60."""
        body = ("Refs %s#60\n\nThe post-merge watch saw the deploy of #41 fail.\n\n"
                "git revert --no-edit %s\n" % (REPO, MERGED))
        self.gh.routes["/repos/%s/pulls/77" % REPO] = {
            "number": 77, "state": "open", "draft": False, "title": 'Revert "Roll the app to 2"',
            "body": body, "user": {"login": STATION}, "mergeable": True,
            "head": {"ref": "revert-41-loom/40-roll", "sha": "r" * 40,
                     "repo": {"full_name": REPO}},
            "base": {"ref": "main", "repo": {"full_name": REPO}}}
        self.gh.routes["graph"] = {
            "isDraft": False, "mergeable": "MERGEABLE", "headRefName": "revert-41-loom/40-roll",
            "headRefOid": "r" * 40, "headRepository": {"nameWithOwner": REPO},
            "commits": {"nodes": [{"commit": {"committedDate": iso(60),
                                              "statusCheckRollup": {"state": rollup}}}]},
            "reviewThreads": {"nodes": []},
            "files": {"totalCount": 1, "nodes": [{"path": CHANGED}]}}
        self.gh.routes["/repos/%s/pulls/77/files" % REPO] = [
            {"filename": CHANGED, "patch": inverse}]
        self.gh.routes["/repos/%s/issues/60" % REPO] = {
            "number": 60, "state": "open", "body": "## What\nx\n",
            "labels": [{"name": n} for n in ("loom:ticket", "loom:pr", "loom:revert")]}
        self.gh.routes["/repos/%s/issues/60/comments" % REPO] = []
        self.gh.routes["/repos/%s/issues/60/events" % REPO] = [
            {"event": "labeled", "label": {"name": "loom:revert"}, "actor": {"login": labeler}}]
        self.gh.routes["/repos/%s/compare/%s...main" % (REPO, MERGED)] = {"status": "ahead"}
        self.gh.routes["/repos/%s/contents/.github/workflows" % REPO] = []

    def merged(self):
        return [p for m, p, _ in self.gh.writes if m == "PUT" and p.endswith("/merge")]

    def reverts_opened(self):
        return [v for m, _, v in self.gh.writes if m == "REVERT"]

    def failed_then_reverted(self):
        """Run the watch to the end: the failed read, then the merge."""
        self.watch()
        self.revert_ready()
        self.watch(now=NOW + 60)


class Signals(Base):
    def test_an_aborted_canary_is_failed(self):
        self.sync_run("failure", notes=[note(
            "rollout-aborted", "apps/app revision 6: Degraded: ReplicaSet has timed out progressing")])
        self.watch()
        got = self.entry()
        self.assertEqual(got["state"], deploy_watch.REVERTING)
        self.assertIn("rollout-aborted", got["failure"])

    def test_a_degraded_rollout_is_failed(self):
        self.sync_run("failure", notes=[note("rollout-degraded", "apps/app revision 3: Degraded")])
        self.watch()
        self.assertIn("rollout-degraded", self.entry()["failure"])

    def test_a_healthy_rollout_is_healthy_and_nothing_is_reverted(self):
        self.sync_run("success", notes=[note("rollout-healthy", "apps/app revision 7", "notice")])
        self.watch()
        got = self.entry()
        self.assertEqual(got["state"], deploy_watch.HEALTHY)
        self.assertIn("1 Rollout healthy", got["reason"])
        self.assertEqual((self.reverts_opened(), self.pages), ([], []))

    def test_the_audits_build_workflow_failing_on_another_node_after_merge(self):
        # The pull request's own build passed on the node it ran on; the
        # merge's build failed on the next one: no rollout title at all.
        self.signal_file("workflow build-images.yml\n")
        self.gh.routes["/repos/%s/actions/workflows/build-images.yml/runs" % REPO] = {
            "workflow_runs": [{"id": 900, "head_sha": MERGED, "status": "completed",
                               "conclusion": "failure"}]}
        self.sync_run("failure", notes=[note(
            "build", "buildctl: failed to dial the build socket on build node b")])
        self.gh.routes["/repos/%s/actions/workflows/build-images.yml/runs" % REPO] = {
            "workflow_runs": [{"id": 900, "head_sha": MERGED, "status": "completed",
                               "conclusion": "failure"}]}
        self.watch()
        got = self.entry()
        self.assertEqual(got["state"], deploy_watch.REVERTING)
        self.assertIn("build-images.yml run 900 failure: buildctl", got["failure"])

    def test_the_audits_config_roll_that_crash_looped_a_statefulset(self):
        # Not a Rollout: the sync's readiness wait failed the run.
        self.sync_run("failure", notes=[note(
            "", "rollout of StatefulSet/world-db in dev-world did not complete - failing the sync")])
        self.watch()
        self.assertIn("StatefulSet/world-db", self.entry()["failure"])

    def test_a_merge_that_changes_nothing_the_workflow_deploys_is_healthy(self):
        self.gh.routes["/repos/%s/actions/workflows/%s/runs" % (REPO, SYNC)] = {"workflow_runs": []}
        self.gh.routes["/repos/%s/commits/%s" % (REPO, MERGED)] = {
            "files": [{"filename": "docs/runbook.md"}]}
        self.watch()
        self.assertEqual(self.entry()["state"], deploy_watch.HEALTHY)

    def test_an_unknown_signal_kind_is_never_nothing_to_watch(self):
        self.signal_file("prometheus error-rate\n")
        self.watch()
        got = self.entry()
        self.assertEqual(got["state"], deploy_watch.WATCHING)
        self.assertIn("unknown signal kind", got["reason"])


class FailClosed(Base):
    def test_annotations_that_cannot_be_read_are_pending_not_healthy(self):
        self.sync_run("success")
        self.gh.gone.add("/repos/%s/check-runs/901/annotations" % REPO)
        self.watch()
        got = self.entry()
        self.assertEqual(got["state"], deploy_watch.WATCHING)
        self.assertIn("could not be read", got["reason"])

    def test_a_cancelled_run_judges_nothing(self):
        self.sync_run("cancelled")
        self.watch()
        self.assertEqual(self.entry()["state"], deploy_watch.WATCHING)

    def test_pending_at_the_end_of_the_window_pages_and_reverts_nothing(self):
        self.sync_run(status="in_progress")
        self.watch()
        self.watch(now=NOW + deploy_watch.DEFAULT_WINDOW)
        got = self.entry()
        self.assertEqual(got["state"], deploy_watch.UNVERIFIED)
        self.assertEqual(self.reverts_opened(), [])
        self.assertEqual(len(self.pages), 1)
        self.assertIn("could not tell whether the deploy of %s#41" % REPO, self.pages[0])

    def test_the_window_is_set_in_minutes(self):
        self.assertEqual(deploy_watch.window({"LOOM_POST_MERGE_WATCH": "15"}), 900)
        self.assertEqual(deploy_watch.window({"LOOM_POST_MERGE_WATCH": "soon"}), 3600)


class Revert(Base):
    def setUp(self):
        super().setUp()
        self.sync_run("failure", notes=[note("rollout-aborted", "apps/app revision 6: aborted")])

    def test_a_failed_deploy_is_reverted_through_the_revert_path_and_paged_once(self):
        self.failed_then_reverted()
        filed = [b for m, p, b in self.gh.writes if m == "POST" and p == "/repos/%s/issues" % REPO]
        self.assertEqual(len(filed), 1)
        self.assertIn("loom:revert", filed[0]["labels"])
        self.assertIn("`git revert --no-edit %s`" % MERGED, filed[0]["body"])
        self.assertIn(CHANGED, filed[0]["body"])
        opened = self.reverts_opened()
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0]["id"], "PR_n41")
        self.assertIn("Refs %s#60" % REPO, opened[0]["body"])
        self.assertEqual(self.merged(), ["/repos/%s/pulls/77/merge" % REPO])
        self.assertEqual(self.entry()["state"], deploy_watch.REVERTED)
        self.assertEqual(self.pages, [
            "Loom reverted %s#41 (Roll the app to 2): its deploy failed after Loom merged it: "
            "workflow %s: rollout-aborted: apps/app revision 6: aborted. Revert %s#77 merged. "
            "Check the deploy is healthy again, then re-brief ticket #40 or close it."
            % (REPO, SYNC, REPO)])
        self.watch(now=NOW + 600)
        self.assertEqual((len(self.pages), len(self.merged())), (1, 1))

    def test_a_revert_the_station_did_not_label_is_left_for_the_operator(self):
        self.watch()
        self.revert_ready(labeler=LANES)
        self.watch(now=NOW + 60)
        self.assertEqual(self.merged(), [])
        self.assertEqual(self.entry()["state"], deploy_watch.LEFT)
        self.assertIn("loom:revert was applied by %s" % LANES, self.pages[0])

    def test_a_revert_that_is_not_the_exact_inverse_is_left(self):
        self.watch()
        self.revert_ready(inverse=FORWARD)
        self.watch(now=NOW + 60)
        self.assertEqual(self.merged(), [])
        self.assertIn("not the exact inverse", self.pages[0])

    def test_a_revert_waits_for_its_checks_and_is_left_when_they_fail(self):
        self.watch()
        self.revert_ready(rollup="PENDING")
        self.watch(now=NOW + 60)
        self.assertEqual((self.merged(), self.pages), ([], []))
        self.revert_ready(rollup="FAILURE")
        self.watch(now=NOW + 120)
        self.assertEqual(self.merged(), [])
        self.assertIn("its checks failed", self.pages[0])

    def test_a_merge_the_red_branch_pass_already_reverts_gets_no_second_revert(self):
        self.gh.routes["/repos/%s/issues?labels=" % REPO] = [
            {"number": 55, "state": "open", "body": "- `git revert --no-edit %s`\n" % MERGED}]
        self.watch()
        self.assertEqual(self.reverts_opened(), [])
        self.assertEqual(self.entry()["state"], deploy_watch.LEFT)
        self.assertIn("revert ticket #55 already names it", self.pages[0])

    def test_a_page_that_did_not_go_out_is_sent_again_next_pass(self):
        self.page_answer = False
        self.failed_then_reverted()
        self.page_answer = True
        self.watch(now=NOW + 120)
        self.assertEqual(len(self.pages), 2)
        self.assertTrue(self.entry()["paged"])


class NotWatched(Base):
    def test_a_revert_is_never_reverted(self):
        for kw in ({"title": 'Revert "Roll the app to 2"'},
                   {"body": "Refs %s#40\n\ngit revert --no-edit %s\n" % (REPO, "b" * 40)}):
            with self.subTest(kw=kw):
                self.setUp()
                self.merge_pr(**kw)
                self.sync_run("failure", notes=[note("rollout-aborted", "x")])
                self.watch()
                self.assertEqual(self.reverts_opened(), [])
        self.setUp()
        self.gh.routes["/repos/%s/issues/40" % REPO]["labels"].append({"name": "loom:revert"})
        self.sync_run("failure", notes=[note("rollout-aborted", "x")])
        self.watch()
        self.assertEqual(self.entry()["state"], deploy_watch.UNWATCHED)
        self.assertEqual(self.reverts_opened(), [])

    def test_a_merge_by_a_person_is_the_operators(self):
        self.merge_pr(merger="the-operator", merger_type="User")
        self.sync_run("failure", notes=[note("rollout-aborted", "x")])
        self.watch()
        self.assertEqual(self.entry()["state"], deploy_watch.UNWATCHED)
        self.assertEqual(self.reverts_opened(), [])

    def test_a_merge_older_than_the_window_is_not_picked_up(self):
        self.merge_pr(merged_ago=deploy_watch.DEFAULT_WINDOW + 60)
        self.sync_run("failure", notes=[note("rollout-aborted", "x")])
        w = self.watch()
        self.assertEqual(w.ledger["merges"], {})


class LiveCheck(Base):
    def test_a_live_check_failing_at_the_end_of_the_window_is_failed(self):
        self.signal_file("# no workflow\n")
        self.gh.routes["/repos/%s/issues/40" % REPO]["body"] = (
            "## Acceptance criteria\n- [ ] Live: `https://app.example.com/healthz` answers "
            "`ok` after the deploy.\n")
        calls = []

        def runner(check, gh):
            calls.append(check.target)
            return False, "HTTP 503"
        self.watch(runner=runner)
        self.assertEqual(self.entry()["state"], deploy_watch.WATCHING)
        self.watch(now=NOW + deploy_watch.DEFAULT_WINDOW, runner=runner)
        got = self.entry()
        self.assertEqual(got["state"], deploy_watch.REVERTING)
        self.assertIn("live check https://app.example.com/healthz: HTTP 503", got["failure"])
        self.assertEqual(len(calls), 2)


class StationStep(Base):
    def test_the_station_runs_the_watch_with_the_factory_on(self):
        self.sync_run("failure", notes=[note("rollout-aborted", "x")])
        for factory_on in (False, True):
            home = tempfile.mkdtemp()
            settings = station.Settings(home, [REPO], [STATION, LANES], ["example-owner"],
                                        factory_on=factory_on)
            st = station.Station(GitHub("t", transport=self.gh), settings, now=NOW)
            with contextlib.redirect_stdout(io.StringIO()):
                st.watch_deploys()
            ledger = os.path.join(st.state, deploy_watch.LEDGER)
            self.assertEqual(os.path.exists(ledger), factory_on)


if __name__ == "__main__":
    unittest.main()
