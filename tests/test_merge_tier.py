"""bus/merge_tier.py: a lane pull request's risk tier decides who merges it.

The fixtures are lane changes an audit of 2026-10-22 found damaging after
the station and brain Apps merged them unattended, with paths and names made
generic: a reusable CI build workflow that lost its fallback on every build
node but one, a runner-pool affinity on the wrong target, a cloud patch
baseline that turned every preview red, a systemd role deleted while its
replacement ran nowhere, and a dev-world config override a release retired.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bus import brain, factory, merge_tier, status  # noqa: E402
from bus.github import GitHub  # noqa: E402
from test_bus_factory import Base, REPO as REPO_F  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

REPO = "example-owner/example-repo"
BOT = "grug-tribe"
HEAD_AT = "2030-01-02T10:00:00Z"

# The audit's damaging lane changes, by the files they changed.
BUILD_WORKFLOW = (".github/workflows/_reusable.build-to-cluster-registry.yml",)
RUNNER_POOL = ("production/manifests/runner-pool-a/values.yaml",
               "production/manifests/runner-pool-b/values.yaml", "tests/test_runner_pools.py")
PATCH_BASELINE = ("pulumi/ssm-hybrid/__main__.py", "pulumi/ssm-hybrid/test_baseline.py")
SYSTEMD_ROLE = ("production/playbooks/connector.yml", "production/roles/connector/tasks/main.yml",
                "production/oke/manifests/connector/deployment.yaml")
CONFIG_OVERRIDE = ("dev-world/config/module.overrides.conf", "dev-world/kustomization.yaml")
LOOM_PIN = ("production/oke/manifests/loom/pin.env",)
# Lane changes the audit found good, which stay Loom's to merge.
APP_CHANGE = ("dashboard.py", "tests/test_dashboard.py")
DOCS_CHANGE = ("docs/runbooks/vm-egress-proxies.md",)


# A hunk that moves nothing: bus/operator_hold.py holds a manifest whose
# patch changes placement, or that has none.
PLAIN_HUNK = "@@ -1 +1 @@\n-retries: 1\n+retries: 2"


def entries(paths, patches=None):
    return [{"filename": p, "patch": (patches or {}).get(p, PLAIN_HUNK)} for p in paths]


def thread(body, resolved=False, created="2030-01-02T09:00:00Z", bot=True):
    author = {"__typename": "Bot" if bot else "User", "login": BOT}
    return {"isResolved": resolved,
            "comments": {"nodes": [{"author": author, "body": body, "createdAt": created}]}}


def by_bot(comment):
    return brain.by_bot(dict(comment), BOT)


class Tiers(unittest.TestCase):
    def test_the_audits_damaging_deploy_and_ci_changes_are_operator_tiers(self):
        for paths, tier in ((BUILD_WORKFLOW, merge_tier.SECURITY),
                            (RUNNER_POOL, merge_tier.DEPLOY_CONFIG),
                            (PATCH_BASELINE, merge_tier.DEPLOY_CONFIG),
                            (SYSTEMD_ROLE, merge_tier.DEPLOY_CONFIG),
                            (CONFIG_OVERRIDE, merge_tier.DEPLOY_CONFIG),
                            (LOOM_PIN, merge_tier.CROSS_REPO)):
            with self.subTest(paths=paths):
                got, why = merge_tier.classify(REPO, entries(paths))
                self.assertEqual(got, tier)
                self.assertNotIn(got, merge_tier.auto_tiers({}))
                self.assertIn(paths[0], why)

    def test_app_code_and_docs_stay_auto_tiers(self):
        self.assertEqual(merge_tier.classify(REPO, entries(APP_CHANGE))[0], merge_tier.APP_CODE)
        self.assertEqual(merge_tier.classify(REPO, entries(DOCS_CHANGE))[0], merge_tier.DOCS_TESTS)
        self.assertEqual(merge_tier.auto_tiers({}), (merge_tier.DOCS_TESTS, merge_tier.APP_CODE))

    def test_the_highest_file_sets_the_tier(self):
        got, why = merge_tier.classify(REPO, entries(APP_CHANGE + BUILD_WORKFLOW))
        self.assertEqual(got, merge_tier.SECURITY)
        self.assertIn(BUILD_WORKFLOW[0], why)

    def test_security_words_dependency_manifests_and_loom_config(self):
        for path, tier in (("services/auth/login.py", merge_tier.SECURITY),
                           ("pulumi/tailscale/policy.py", merge_tier.SECURITY),
                           ("bus/authors.py", merge_tier.APP_CODE),
                           (".loom/validation.conf", merge_tier.SECURITY),
                           ("./.github/CODEOWNERS", merge_tier.SECURITY),
                           ("requirements.txt", merge_tier.DEPLOY_CONFIG),
                           ("package.json", merge_tier.DEPLOY_CONFIG),
                           ("Dockerfile", merge_tier.DEPLOY_CONFIG),
                           ("tests/test_auth.py", merge_tier.SECURITY),
                           ("README.md", merge_tier.DOCS_TESTS)):
            with self.subTest(path=path):
                self.assertEqual(merge_tier.file_tier(path), tier)

    def test_a_submodule_move_or_a_ticket_naming_another_repo_is_cross_repo(self):
        gitlink = entries(("modules/engine",),
                          {"modules/engine": "-Subproject commit 4a424cda\n+Subproject commit ff14f90f"})
        self.assertEqual(merge_tier.classify(REPO, gitlink)[0], merge_tier.CROSS_REPO)
        # A Refs line in the cross-repo shape is what this test is about.
        cite = "Refs example-owner/third#2\n"  # leak-guard-allow
        ticket = "## What\nAlso change example-owner/other-repo's deploy to match.\n\n" + cite
        got, why = merge_tier.classify(REPO, entries(APP_CHANGE), ticket)
        self.assertEqual((got, why), (merge_tier.CROSS_REPO,
                                      "its ticket names another repo (example-owner/other-repo)"))
        # A citation line alone names no change in another repo.
        self.assertEqual(merge_tier.classify(REPO, entries(APP_CHANGE),
                                             cite)[0], merge_tier.APP_CODE)

    def test_a_repo_named_only_in_the_loom_status_section_is_not_cross_repo(self):
        # loom#1959: the status section is Loom's log, not the ticket's words.
        log = ("%s\n---\n\n- 2026-10-26T13:33:57Z Factory incident example-owner/loom#7 "  # leak-guard-allow
               "is closed: the ticket is back in the queue.\n%s\n" % (
                   status.BODY_BEGIN, status.BODY_END))
        ticket = "## What\nCount timed-out runs as progress.\n\n" + log
        self.assertEqual(merge_tier.classify(REPO, entries(APP_CHANGE), ticket)[0],
                         merge_tier.APP_CODE)
        # A repo the ticket's own words name is still cross-repo.
        asked = "## What\nAlso change example-owner/other-repo to match.\n\n" + log
        self.assertEqual(merge_tier.classify(REPO, entries(APP_CHANGE), asked),
                         (merge_tier.CROSS_REPO,
                          "its ticket names another repo (example-owner/other-repo)"))

    def test_a_base_line_naming_a_branch_is_not_cross_repo(self):
        # A Base: line names a branch; owner/name there is never a repo.
        body = "## What\nCount timed-out runs as progress.\n\nBase: quadseven/prod\n"
        self.assertEqual(merge_tier.classify("quadseven/example", entries(APP_CHANGE), body)[0],
                         merge_tier.APP_CODE)

    def test_a_repo_named_outside_a_base_line_is_still_cross_repo(self):
        body = "Base: quadseven/prod\n\n## What\nAlso change quadseven/other to match.\n"
        self.assertEqual(merge_tier.classify("quadseven/example", entries(APP_CHANGE), body),
                         (merge_tier.CROSS_REPO, "its ticket names another repo (quadseven/other)"))

    def test_a_test_or_doc_named_deploy_file_is_never_low_risk(self):
        # Security review: docs and tests were matched before deploy rules.
        for path in ("tests/manifests/app/deployment.yaml", "tests/main.tf",
                     "production/oke/manifests/app/test_values.yaml", "docs/Dockerfile",
                     "tests/fixtures/requirements.txt", "docs/charts/app/values.yaml"):
            with self.subTest(path=path):
                self.assertNotEqual(merge_tier.file_tier(path), merge_tier.DOCS_TESTS)

    def test_a_rename_takes_the_higher_tier_of_its_two_paths(self):
        # Security review: a manifest renamed under tests/ read as a test.
        moved = [{"filename": "tests/fixtures/values.txt", "patch": PLAIN_HUNK,
                  "previous_filename": "production/manifests/runner-pool-a/values.yaml"}]
        self.assertEqual(merge_tier.classify(REPO, moved)[0], merge_tier.DEPLOY_CONFIG)

    def test_a_sensitive_class_or_unread_files_never_read_as_docs(self):
        self.assertEqual(merge_tier.classify(REPO, entries(DOCS_CHANGE), sensitive=True)[0],
                         merge_tier.SECURITY)
        self.assertEqual(merge_tier.classify(REPO, []),
                         (merge_tier.SECURITY, "its changed files could not be read"))

    def test_the_operator_narrows_the_auto_tiers(self):
        self.assertEqual(merge_tier.auto_tiers({"LOOM_AUTO_MERGE_TIERS": "docs-tests, bogus"}),
                         (merge_tier.DOCS_TESTS,))
        self.assertEqual(merge_tier.auto_tiers({"LOOM_AUTO_MERGE_TIERS": ""}), ())


class Findings(unittest.TestCase):
    DEAD = "\U0001f7e1 medium | _correctness_ | `dead-code` | small\n\nNothing calls it."
    INTENT = "\U0001f7e0 high | _correctness_ | `intent-mismatch` | medium\n\nNo criterion met."
    STYLE = "\U0001f7e1 medium | _style_ | `naming` | small\n\nRename it."

    def test_a_bad_work_rule_blocks_at_any_severity_and_style_does_not(self):
        self.assertTrue(merge_tier.bad_work(self.DEAD))
        self.assertFalse(merge_tier.bad_work(self.STYLE))
        self.assertTrue(merge_tier.bad_work(self.STYLE, {"LOOM_ELDER_BLOCKING_RULES": "naming"}))
        self.assertTrue(merge_tier.bad_work("\U0001f7e2 low | _bad-work_ | `new-rule` | small"))
        found = merge_tier.blocking_findings([thread(self.DEAD), thread(self.STYLE),
                                              thread(self.INTENT, bot=False)], HEAD_AT, by_bot)
        self.assertEqual(found, ["medium dead-code"])

    def test_a_finding_resolved_with_no_fix_after_it_still_blocks(self):
        deferred = thread(self.DEAD, resolved=True, created="2030-01-02T11:00:00Z")
        fixed = thread(self.INTENT, resolved=True, created="2030-01-02T09:00:00Z")
        self.assertEqual(merge_tier.blocking_findings([deferred, fixed], HEAD_AT, by_bot),
                         ["medium dead-code"])

    def test_the_factory_never_defers_a_bad_work_finding(self):
        threads = [{"id": "T1", "isResolved": False,
                    "root": {"nodes": [{"author": {"__typename": "Bot", "login": BOT},
                                        "body": body}]}} for body in (self.DEAD,)]
        self.assertEqual(factory.deferrable(threads, BOT, set(), 9, immediate=True), [])
        threads[0]["root"]["nodes"][0]["body"] = self.STYLE
        self.assertEqual(len(factory.deferrable(threads, BOT, set(), 9, immediate=True)), 1)


class Caps(unittest.TestCase):
    NOW = datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc)

    def gh(self, in_repo, overall):
        queries = []

        def transport(method, url, headers, body):
            queries.append(url)
            count = in_repo if "repo%3A" in url else overall
            return 200, {}, json.dumps({"total_count": count}).encode()
        return GitHub("t", transport=transport), queries

    def test_a_repo_at_its_hourly_cap_waits(self):
        gh, queries = self.gh(2, 2)
        why = merge_tier.cap_refusal(gh, REPO, {}, self.NOW)
        self.assertIn("2 loom/ pull requests merged in %s in the last hour, the cap is 2" % REPO, why)
        self.assertIn("merged%3A%3E%3D2030-01-02T11%3A00%3A00Z", queries[0])

    def test_the_owner_wide_cap_and_its_setting(self):
        gh, _ = self.gh(0, 4)
        self.assertIn("the cap is 4 (LOOM_AUTO_MERGE_PER_HOUR)",
                      merge_tier.cap_refusal(gh, REPO, {}, self.NOW))
        self.assertEqual(merge_tier.cap_refusal(gh, REPO, {"LOOM_AUTO_MERGE_PER_HOUR": "9"},
                                                self.NOW), "")

    def test_a_count_github_will_not_give_refuses(self):
        def transport(method, url, headers, body):
            return 422, {}, b'{"message": "no"}'
        why = merge_tier.cap_refusal(GitHub("t", transport=transport), REPO, {}, self.NOW)
        self.assertIn("could not count", why)


class Route:
    """GitHub by method and path prefix; writes are recorded."""

    def __init__(self, routes):
        self.routes = routes
        self.writes = []

    def __call__(self, method, url, headers, body):
        path = url.split("api.github.com")[-1]
        if path == "/graphql":
            query = json.loads(body)
            if "pageInfo" not in query["query"]:
                return 200, {}, json.dumps(self.routes["/graphql"]).encode()
            # merge_tier.review_threads: one page per cursor.
            pages = self.routes["threads"]
            at = int(query["variables"].get("after") or 0)
            if pages is None:
                return 200, {}, json.dumps({"errors": [{"message": "no"}]}).encode()
            info = {"hasNextPage": at + 1 < len(pages), "endCursor": str(at + 1)}
            return 200, {}, json.dumps({"data": {"repository": {"pullRequest": {
                "reviewThreads": {"pageInfo": info, "nodes": pages[at]}}}}}).encode()
        if method != "GET":
            self.writes.append((method, path, json.loads(body) if body else None))
            return 200, {}, json.dumps({"merged": path.endswith("/merge")}).encode()
        for prefix, data in self.routes.items():
            if path.startswith(prefix):
                return 200, {}, json.dumps(data).encode()
        return 404, {}, b"{}"


def routes(files, conclusion="success", threads=(), merged_last_hour=0, ticket_labels=(),
           title="Keep the build cache warm", body="## Summary\nx\n", pages=None,
           labeler="loom-station[bot]", commit_files=None, carried="ahead", patches=None):
    pr = {"number": 8, "state": "open", "draft": False, "title": title,
          "body": body + "\nRefs %s#9" % REPO,
          "head": {"ref": "loom/9-work", "sha": "checked", "repo": {"full_name": REPO}},
          "base": {"ref": "main", "repo": {"full_name": REPO}}, "mergeable": True}
    graph = {"isDraft": False, "mergeable": "MERGEABLE", "headRefName": "loom/9-work",
             "headRefOid": "checked", "headRepository": {"nameWithOwner": REPO},
             "commits": {"nodes": [{"commit": {"committedDate": HEAD_AT,
                                               "statusCheckRollup": {"state": "SUCCESS"}}}]},
             "reviewThreads": {"nodes": list(threads or ())},
             "files": {"totalCount": len(files), "nodes": [{"path": f} for f in files]}}
    ticket = {"number": 9, "state": "open", "body": "## What\nx\n",
              "labels": [{"name": n} for n in ("loom:ticket", "loom:pr") + tuple(ticket_labels)]}
    return {
        "/repos/%s/pulls/8/files" % REPO: entries(files, patches),
        "/repos/%s/pulls/8" % REPO: pr,
        "/graphql": {"data": {"repository": {"pullRequest": graph}}},
        "/repos/%s/issues/9/comments" % REPO: [],
        # The pull request's own comments: no fleet hold on it (loom#2393).
        "/repos/%s/issues/8/comments" % REPO: [],
        "/repos/%s/issues/9/events" % REPO: [
            {"event": "labeled", "label": {"name": "loom:revert"}, "actor": {"login": labeler}}],
        "/repos/%s/issues/9" % REPO: ticket,
        "threads": pages if pages is not None else (None if threads is None
                                                     else [list(threads)]),
        "/repos/%s/compare/abc1234...main" % REPO: {"status": carried},
        "/repos/%s/compare/main...checked" % REPO: {"behind_by": 0},
        "/repos/%s/commits/abc1234" % REPO: {"files": commit_files or []},
        "/repos/%s/commits/checked/check-runs" % REPO: {"check_runs": [
            {"name": "Grug - Elder", "status": "completed", "conclusion": conclusion}]},
        "/search/issues": {"total_count": merged_last_hour},
        # No deploy workflows: bus/operator_hold.py reads them for placement.
        "/repos/%s/contents/.github/workflows" % REPO: [],
    }


class BrainMerge(unittest.TestCase):
    ENV = {"LOOM_MERGE_SETTLE_SECONDS": "0", "LOOM_REVIEW_BOT": BOT,
           "LOOM_BRIEF_AUTHORS": "loom-station[bot] loom-lanes[bot]"}

    def merge(self, **kw):
        fake = Route(routes(**kw))
        try:
            brain.merge(GitHub("t", transport=fake), REPO, 8, (REPO, 9), env=dict(self.ENV))
        except ValueError as err:
            return fake, str(err)
        return fake, ""

    @staticmethod
    def merged(fake):
        return any(p.endswith("/pulls/8/merge") for _, p, _ in fake.writes)

    def test_each_damaging_deploy_change_is_refused_and_asks_the_operator_once(self):
        for files in (BUILD_WORKFLOW, PATCH_BASELINE, SYSTEMD_ROLE, LOOM_PIN):
            with self.subTest(files=files):
                fake, refused = self.merge(files=files)
                self.assertIn("held for the operator: it is a", refused)
                self.assertFalse(self.merged(fake))
                asks = [b["body"] for _, p, b in fake.writes if p.endswith("/issues/9/comments")]
                self.assertEqual(len(asks), 1)
                self.assertIn("merge #8 yourself", asks[0])

    def test_app_code_merges_on_a_successful_review_under_the_caps(self):
        fake, refused = self.merge(files=APP_CHANGE)
        self.assertEqual(refused, "")
        self.assertTrue(self.merged(fake))

    def test_app_code_waits_for_a_neutral_review_an_open_bad_work_finding_or_the_cap(self):
        dead = thread(Findings.DEAD, resolved=True, created="2030-01-02T11:00:00Z")
        for kw, why in (({"conclusion": "neutral"}, "succeeded on its head commit"),
                        ({"threads": [dead]}, "open review findings hold the merge: medium dead-code"),
                        ({"merged_last_hour": 2}, "the auto-merge cap holds the merge")):
            with self.subTest(why=why):
                fake, refused = self.merge(files=APP_CHANGE, **kw)
                self.assertIn(why, refused)
                self.assertFalse(self.merged(fake))

    # The merged commit a revert undoes, and its exact inverse.
    FORWARD = "@@ -1 +1 @@\n-buildctl-daemonless.sh build\n+buildctl --addr unix:///sock build"
    INVERSE = "@@ -1 +1 @@\n-buildctl --addr unix:///sock build\n+buildctl-daemonless.sh build"

    def revert(self, **kw):
        args = dict(files=BUILD_WORKFLOW, ticket_labels=("loom:revert",),
                    title="Revert abc1234: Use the shared build socket",
                    body="## Summary\ngit revert --no-edit abc1234\n", merged_last_hour=9,
                    patches={BUILD_WORKFLOW[0]: self.INVERSE},
                    commit_files=[{"filename": BUILD_WORKFLOW[0], "patch": self.FORWARD}])
        args.update(kw)
        return self.merge(**args)

    def test_a_proven_revert_is_exempt_from_tier_and_caps(self):
        fake, refused = self.revert()
        self.assertEqual(refused, "")
        self.assertTrue(self.merged(fake))

    def behind_revert(self, missed, drop=()):
        """A proven revert with `missed` {sha: paths} landed on main since it branched."""
        routes_ = routes(files=BUILD_WORKFLOW, ticket_labels=("loom:revert",),
                         title="Revert abc1234: Use the shared build socket",
                         body="## Summary\ngit revert --no-edit abc1234\n", merged_last_hour=9,
                         patches={BUILD_WORKFLOW[0]: self.INVERSE},
                         commit_files=[{"filename": BUILD_WORKFLOW[0], "patch": self.FORWARD}])
        routes_.update({
            "/repos/%s/compare/main...checked" % REPO: {"behind_by": len(missed)},
            "/repos/%s/compare/checked...main" % REPO: {
                "commits": [{"sha": sha} for sha in missed]},
            **{"/repos/%s/commits/%s" % (REPO, sha): {
                "files": [{"filename": path} for path in paths]}
               for sha, paths in missed.items()}})
        for path in drop:
            del routes_[path]
        fake = Route(routes_)
        try:
            brain.merge(GitHub("t", transport=fake), REPO, 8, (REPO, 9), env=dict(self.ENV))
        except ValueError as err:
            return fake, str(err)
        return fake, ""

    def test_a_revert_lands_behind_when_no_missed_commit_touches_its_files(self):
        fake, refused = self.behind_revert({"c1": ["docs/a.md"], "c2": ["bus/other.py"]})
        self.assertEqual(refused, "")
        self.assertTrue(self.merged(fake))

    def test_a_revert_is_refused_when_the_base_landed_the_commit_again(self):
        # Undone and landed again after the branch: the two-point diff is
        # empty, so only the commits show it.
        fake, refused = self.behind_revert(
            {"undo": BUILD_WORKFLOW, "again": BUILD_WORKFLOW})
        self.assertIn("behind its base", refused)
        self.assertIn(BUILD_WORKFLOW[0], refused)
        self.assertFalse(self.merged(fake))

    def test_a_revert_is_refused_when_the_base_already_reverted_the_commit(self):
        fake, refused = self.behind_revert({"undo": BUILD_WORKFLOW})
        self.assertIn("behind its base", refused)
        self.assertFalse(self.merged(fake))

    def test_a_failed_read_gives_a_revert_no_exemption(self):
        missing = "/repos/%s/commits/c1" % REPO
        compare = "/repos/%s/compare/checked...main" % REPO
        for path in (missing, compare):
            with self.subTest(path=path):
                fake, refused = self.behind_revert({"c1": ["docs/a.md"]}, drop=(path,))
                self.assertIn("could not be checked", refused)
                self.assertFalse(self.merged(fake))

    def test_a_short_commit_list_gives_a_revert_no_exemption(self):
        fake = Route({**routes(files=BUILD_WORKFLOW)})
        gh = GitHub("t", transport=fake)
        fake.routes["/repos/%s/compare/checked...main" % REPO] = {"commits": [{"sha": "c1"}]}
        with self.assertRaisesRegex(ValueError, "listed 1 of 3"):
            brain._missed_paths(gh, REPO, {"base": {"ref": "main"}, "head": {"sha": "checked"}}, 3)

    def test_an_unproven_revert_takes_the_normal_gates(self):
        # Security review: the label and the title alone exempted it.
        for kw in ({"ticket_labels": ()},
                   {"labeler": "loom-lanes[bot]"},
                   {"labeler": "someone-else"},
                   {"patches": {BUILD_WORKFLOW[0]: self.FORWARD}},
                   {"commit_files": [{"filename": "other.py", "patch": self.FORWARD}]},
                   {"carried": "diverged"},
                   {"body": "## Summary\nRevert it.\n"}):
            with self.subTest(kw=kw):
                fake, refused = self.revert(**kw)
                self.assertIn("held for the operator", refused)
                self.assertFalse(self.merged(fake))

    def test_a_finding_on_a_later_page_of_threads_blocks(self):
        # Security review: only the first page of review threads was read.
        dead = thread(Findings.DEAD)
        fake, refused = self.merge(files=APP_CHANGE, pages=[[thread(Findings.STYLE, resolved=True)]
                                                            for _ in range(2)] + [[dead]])
        self.assertIn("medium dead-code", refused)
        self.assertFalse(self.merged(fake))

    def test_threads_that_cannot_be_read_in_full_refuse(self):
        fake, refused = self.merge(files=APP_CHANGE, pages=None, threads=None)
        self.assertIn("could not read the review threads", refused)
        self.assertFalse(self.merged(fake))


class Unreadable(unittest.TestCase):
    def test_a_file_list_github_will_not_send_refuses_the_merge(self):
        # Review finding: the read raised out of the gate unexplained.
        fake = Route(routes(files=APP_CHANGE))

        def transport(method, url, headers, body):
            if "/pulls/8/files" in url:
                return 422, {}, b'{"message": "too many files"}'
            return fake(method, url, headers, body)
        with self.assertRaisesRegex(ValueError, "changed files could not be read"):
            brain.merge(GitHub("t", transport=transport), REPO, 8, (REPO, 9),
                        env=dict(BrainMerge.ENV))
        self.assertFalse(BrainMerge.merged(fake))

    def test_revert_refusal_reads_the_labelers_from_the_environment(self):
        with mock.patch.dict(os.environ, {"LOOM_REVERT_LABELERS": "a-bot[bot]"}):
            self.assertEqual(merge_tier.revert_labelers(None), ["a-bot[bot]"])


class FactoryHold(Base):
    def setUp(self):
        super().setUp()
        defaults = mock.patch.dict(os.environ, {"LOOM_AUTO_MERGE_TIERS": "docs-tests,app-code"})
        defaults.start()
        self.addCleanup(defaults.stop)

    def test_a_deploy_change_is_held_for_the_operator_and_app_code_lands(self):
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, files=RUNNER_POOL[:1] + ("tests/test_a.py",))
        self.go()
        self.assertEqual(self.merged, [])
        self.assertEqual(len(self.humans), 1)
        self.assertIn("deploy-config change", str(self.humans[0]))
        self.setUp()
        self.gh.ticket(5, "pr")
        self.gh.pull(50, 5, files=APP_CHANGE)
        self.go()
        self.assertEqual(self.merged, [(50, (REPO_F, 5))])




if __name__ == "__main__":
    unittest.main()
