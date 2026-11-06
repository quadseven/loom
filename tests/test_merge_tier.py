"""bus/merge_tier.py: a lane pull request's risk tier decides who merges it.

The fixtures are lane changes an audit of 2026-10-22 found damaging after
the station and brain Apps merged them unattended, with paths and names made
generic: a reusable CI build workflow that lost its fallback on every build
node but one, a runner-pool affinity on the wrong target, a cloud patch
baseline that turned every preview red, a systemd role deleted while its
replacement ran nowhere, and a dev-world config override a release retired.
"""
from __future__ import annotations

import base64
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


def commit(date, parents=1):
    return {"commit": {"committedDate": date, "parents": {"totalCount": parents}}}


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

    def test_an_answered_high_or_bad_work_finding_blocks_even_when_held(self):
        """loom#2519 acceptance: a resolved high or bad-work finding whose
        thread is in `held` still blocks the merge with no fix commit after
        it. An answer the station held no longer stands in for the fix, so
        this replaces the #2285 tests that pinned the opposite,
        `test_a_resolved_finding_whose_answer_held_on_the_head_does_not_block`
        and `test_a_finding_answered_and_held_on_the_head_does_not_hold_the_merge`."""
        for body, want in ((self.INTENT, "high intent-mismatch"),
                           (self.DEAD, "medium dead-code")):
            with self.subTest(want=want):
                answered = dict(thread(body, resolved=True, created="2030-01-02T11:00:00Z"),
                                id="T1")
                self.assertEqual(merge_tier.blocking_findings([answered], HEAD_AT, by_bot,
                                                              held={"T1"}), [want])
                opened = dict(answered, isResolved=False)
                self.assertEqual(merge_tier.blocking_findings([opened], HEAD_AT, by_bot,
                                                              held={"T1"}), [want])

    def test_a_base_merge_after_the_finding_is_not_a_fix(self):
        # loom#2285: an update-branch merge is newer than the finding but
        # proves nothing; only a commit with one parent counts.
        found = thread(self.DEAD, resolved=True, created="2030-01-02T09:00:00Z")
        commits = [commit("2030-01-02T08:00:00Z"), commit("2030-01-02T10:00:00Z", parents=2)]
        since = merge_tier.fix_committed(commits)
        self.assertEqual(since, "2030-01-02T08:00:00Z")
        self.assertEqual(merge_tier.blocking_findings([found], since, by_bot),
                         ["medium dead-code"])
        fixed = commits + [commit("2030-01-02T10:30:00Z")]
        self.assertEqual(merge_tier.blocking_findings(
            [found], merge_tier.fix_committed(fixed), by_bot), [])
        self.assertEqual(merge_tier.fix_committed([commit("2030-01-02T10:00:00Z", parents=2)]), "")

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


class LastingHolds(unittest.TestCase):
    """A merge no brain can make, so no brain defers it to the next one
    (loom#2667): every tick on 11-05 deferred 9 to 11 items, four of them
    merges only the operator or the pull request's author could move."""

    def test_an_operator_tier_label_is_a_lasting_hold(self):
        why = merge_tier.lasting_hold(["class:sensitive", "tier:security"], "loom/1-x", True,
                                      env={})
        self.assertIn("tier:security", why)
        self.assertIn("operator", why)

    def test_an_auto_tier_label_is_no_hold(self):
        self.assertEqual("", merge_tier.lasting_hold(["tier:app-code"], "loom/1-x", True, env={}))
        self.assertEqual("", merge_tier.lasting_hold(
            ["tier:security"], "loom/1-x", True, env={"LOOM_AUTO_MERGE_TIERS": "security"}))

    def test_a_conflicting_head_outside_loom_is_a_lasting_hold(self):
        why = merge_tier.lasting_hold([], "codex/1-review-queue", False, env={})
        self.assertIn("not a loom branch", why)
        self.assertIn("conflicts", why)

    def test_a_conflicting_loom_head_or_an_unknown_mergeable_is_no_hold(self):
        # bus.brain fix sends a loom/ head back to its lane, and GitHub may
        # still be computing mergeable (None).
        self.assertEqual("", merge_tier.lasting_hold([], "loom/1-x", False, env={}))
        self.assertEqual("", merge_tier.lasting_hold([], "codex/1-x", None, env={}))


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
           labeler="loom-station[bot]", commit_files=None, carried="ahead", patches=None,
           commits=None):
    pr = {"number": 8, "state": "open", "draft": False, "title": title,
          "body": body + "\nRefs %s#9" % REPO,
          "head": {"ref": "loom/9-work", "sha": "checked", "repo": {"full_name": REPO}},
          "base": {"ref": "main", "repo": {"full_name": REPO}}, "mergeable": True}
    graph = {"isDraft": False, "mergeable": "MERGEABLE", "headRefName": "loom/9-work",
             "headRefOid": "checked", "headRepository": {"nameWithOwner": REPO},
             "commits": {"nodes": commits or [{"commit": {
                 "committedDate": HEAD_AT, "parents": {"totalCount": 1},
                 "statusCheckRollup": {"state": "SUCCESS"}}}]},
             "reviewThreads": {"nodes": list(threads or ())},
             "files": {"totalCount": len(files), "nodes": [{"path": f} for f in files]}}
    ticket = {"number": 9, "state": "open", "body": "## What\nx\n",
              "labels": [{"name": n} for n in ("loom:ticket", "loom:pr") + tuple(ticket_labels)]}
    return {
        "/repos/%s/pulls/8/files" % REPO: entries(files, patches),
        # No reviews on the pull request unless a test adds its own route
        # (this key must precede the pull request's: it is a prefix of it).
        "/repos/%s/pulls/8/reviews" % REPO: [],
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
        "/repos/%s/compare/abc1234...checked" % REPO: {"status": "ahead"},
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

    def answered(self, answer):
        """The dead-code finding, resolved by the station on a lane answer,
        with no commit after it (loom#2285)."""
        reply = "Lane codex %s\n\n> %s" % (factory.ANSWER_REPLY, answer)
        return dict(thread(Findings.DEAD, resolved=True, created="2030-01-02T11:00:00Z"),
                    id="T1", path="a.py",
                    latest={"nodes": [{"author": {"__typename": "Bot", "login": "loom-station"},
                                       "body": reply}]})

    def test_a_bad_work_finding_answered_and_held_on_the_head_still_holds_the_merge(self):
        # loom#2519: an answer stands in for no fix commit on a bad-work finding.
        fake, refused = self.merge(files=APP_CHANGE, threads=[self.answered("Fixed in abc1234")])
        self.assertIn("open review findings hold the merge: medium dead-code", refused)
        self.assertFalse(self.merged(fake))

    def test_a_base_merge_after_a_finding_does_not_release_it(self):
        found = thread(Findings.DEAD, resolved=True, created="2030-01-02T09:00:00Z")
        update = [{"commit": {"committedDate": "2030-01-02T08:00:00Z", "parents": {"totalCount": 1}}},
                  {"commit": {"committedDate": HEAD_AT, "parents": {"totalCount": 2},
                              "statusCheckRollup": {"state": "SUCCESS"}}}]
        fake, refused = self.merge(files=APP_CHANGE, threads=[found], commits=update)
        self.assertIn("open review findings hold the merge: medium dead-code", refused)
        self.assertFalse(self.merged(fake))
        fake, refused = self.merge(files=APP_CHANGE, threads=[found])
        self.assertEqual(refused, "")

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


S110 = "\U0001f534 high | _security_ | `S110` | trivial\n\ntry, except, pass detected."


def review(rid, body, state="COMMENTED", when="2030-01-02T09:30:00Z", bot=True):
    """A REST review on pull request 8, as the reviews endpoint sends it."""
    return {"id": rid, "state": state, "body": body, "submitted_at": when,
            "html_url": "https://example.test/pull/8#pullrequestreview-%d" % rid,
            "user": {"login": BOT + "[bot]" if bot else "alice",
                     "type": "Bot" if bot else "User"}}


def pr_comment(body, when="2030-01-02T09:45:00Z"):
    return {"body": body, "created_at": when,
            "user": {"login": "alice", "type": "User"}}


class StubGH:
    """paginate() only: the reviews and the pull request's comments."""

    def __init__(self, reviews=(), comments=()):
        self._reviews, self._comments = list(reviews), list(comments)

    def paginate(self, path):
        return self._reviews if "/reviews" in path else self._comments


class ReviewBodies(unittest.TestCase):
    """loom#1933: the gate reads review bodies, not only review threads."""

    def test_body_findings_reads_the_finding_lines_and_ignores_prose(self):
        body = "Two things need work.\n\n%s\n\n%s\n" % (Findings.DEAD, Findings.STYLE)
        found = merge_tier.body_findings(body)
        self.assertEqual([f[2] for f in found], ["dead-code", "naming"])
        self.assertEqual(merge_tier.body_findings("Nothing to see here."), [])
        self.assertEqual(merge_tier.body_findings(""), [])

    def test_thread_states_follow_the_newest_thread_for_a_rule(self):
        older = thread(Findings.STYLE, resolved=True, created="2030-01-02T08:00:00Z")
        newer = thread(Findings.STYLE, resolved=False, created="2030-01-02T09:00:00Z")
        self.assertEqual(merge_tier.thread_states([older, newer], HEAD_AT, by_bot),
                         {"naming": ("open", "")})
        self.assertEqual(merge_tier.thread_states([newer, older], HEAD_AT, by_bot),
                         {"naming": ("open", "")})
        fixed = dict(thread(S110, resolved=True, created="2030-01-02T09:00:00Z"),
                     path="dashboard.py")
        self.assertEqual(merge_tier.thread_states([fixed], HEAD_AT, by_bot),
                         {"s110": ("addressed", "dashboard.py")})
        # Resolved with no fix commit read (committed unknown): resolved.
        self.assertEqual(merge_tier.thread_states([fixed], "", by_bot),
                         {"s110": ("resolved", "dashboard.py")})
        # A person's thread and a finding-free body set no state.
        self.assertEqual(merge_tier.thread_states(
            [thread(Findings.STYLE, bot=False), thread("no finding")], HEAD_AT, by_bot), {})

    def test_unanswered_bodies_skip_dismissed_people_and_answered_reviews(self):
        settled = thread(Findings.STYLE, resolved=True)
        gh = StubGH(reviews=[review(41, Findings.STYLE, state="DISMISSED"),
                             review(42, Findings.STYLE, bot=False),
                             review(43, Findings.STYLE),
                             review(44, "No findings in this one."),
                             review(45, Findings.STYLE)],
                    comments=[pr_comment("Answering review 43: "
                                         "https://example.test/x#pullrequestreview-43")])
        bodies = merge_tier.unanswered_bodies(gh, REPO, 8, [settled], HEAD_AT, by_bot)
        self.assertEqual([b["review"]["id"] for b in bodies], [45])
        self.assertTrue(bodies[0]["settled"])
        self.assertEqual(bodies[0]["findings"],
                         [(merge_tier.finding(Findings.STYLE), "addressed", "")])

    def test_a_body_finding_with_no_thread_reads_open(self):
        gh = StubGH(reviews=[review(46, Findings.STYLE)])
        bodies = merge_tier.unanswered_bodies(gh, REPO, 8, [], HEAD_AT, by_bot)
        self.assertEqual(len(bodies), 1)
        self.assertFalse(bodies[0]["settled"])
        self.assertIn("naming", merge_tier.body_refusal(bodies))
        self.assertIn("still open", merge_tier.body_refusal(bodies))

    def test_lint_rules_are_ruff_codes_not_elder_words(self):
        for rule, want in (("s110", True), ("e501", True), ("b905", True),
                           ("dead-code", False), ("naming", False),
                           ("intent-mismatch", False), ("s", False)):
            with self.subTest(rule=rule):
                self.assertEqual(merge_tier.lint_rule(rule), want)

    def test_lint_targets_are_addressed_high_lint_threads_only(self):
        addressed = dict(thread(S110, resolved=True, created="2030-01-02T09:00:00Z"),
                         path="dashboard.py")
        open_high = dict(thread(S110, resolved=False, created="2030-01-02T09:30:00Z"),
                         path="other.py")
        medium = dict(thread("\U0001f7e1 medium | _style_ | `E501` | trivial",
                             resolved=True, created="2030-01-02T09:00:00Z"), path="dashboard.py")
        elder = dict(thread(Findings.INTENT, resolved=True,
                            created="2030-01-02T09:00:00Z"), path="dashboard.py")
        self.assertEqual(merge_tier.lint_targets([addressed, open_high, medium, elder],
                                                 HEAD_AT, by_bot),
                         [("high", "s110", "dashboard.py")])

    def test_run_ruff_maps_exit_codes_and_cannot_run_to_none(self):
        class Done:
            def __init__(self, returncode):
                self.returncode = returncode

        def runner(rc):
            return lambda *a, **k: Done(rc)

        self.assertIs(merge_tier.run_ruff("s110", "a.py", "x", runner=runner(1)), True)
        self.assertIs(merge_tier.run_ruff("s110", "a.py", "x", runner=runner(0)), False)
        self.assertIs(merge_tier.run_ruff("s110", "a.py", "x", runner=runner(2)), None)

        def missing(*a, **k):
            raise OSError("no ruff")
        self.assertIs(merge_tier.run_ruff("s110", "a.py", "x", runner=missing), None)


class ReviewBodyGate(BrainMerge):
    """loom#1933 criteria 1, 2 and 4, through brain.merge itself."""

    def gate_merge(self, reviews=(), comments=(), contents=None, lint_run=None, **kw):
        base = routes(**kw)
        # The reviews route comes first (the pull request's route is a
        # prefix of it) and carries this test's reviews, not the default.
        ordered = {"/repos/%s/pulls/8/reviews" % REPO: list(reviews)}
        for key, value in base.items():
            if key not in ordered:
                ordered[key] = value
        if comments:
            ordered["/repos/%s/issues/8/comments" % REPO] = list(comments)
        for path, text in (contents or {}).items():
            ordered["/repos/%s/contents/%s" % (REPO, path)] = {
                "encoding": "base64",
                "content": base64.b64encode(text.encode()).decode()}
        fake = Route(ordered)
        try:
            brain.merge(GitHub("t", transport=fake), REPO, 8, (REPO, 9),
                        env=dict(self.ENV), lint_run=lint_run)
        except ValueError as err:
            return fake, str(err)
        return fake, ""

    def answers(self, fake):
        return [b for _, p, b in fake.writes
                if p.endswith("/issues/8/comments") and b and "body" in b]

    def test_settled_review_body_is_answered_once_then_the_pr_merges(self):
        # Criterion 1: the finding's thread is resolved and a commit on the
        # pull request follows it; the review body itself was never answered.
        addressed = dict(thread(S110, resolved=True, created="2030-01-02T09:00:00Z"),
                         path="dashboard.py")
        calls = []

        def clean(rule, path, text):
            calls.append((rule, path, text))
            return False
        fake, refused = self.gate_merge(
            files=APP_CHANGE, threads=[addressed],
            reviews=[review(41, "One finding from the review.\n\n" + S110)],
            contents={"dashboard.py": "def ok():\n    return 1\n"},
            lint_run=clean)
        self.assertEqual(refused, "")
        self.assertTrue(self.merged(fake))
        # The addressed high lint finding was re-checked on the head's file.
        self.assertEqual(calls, [("s110", "dashboard.py", "def ok():\n    return 1\n")])
        answers = self.answers(fake)
        self.assertEqual(len(answers), 1)
        self.assertIn(merge_tier.REVIEW_ANSWER_MARKER, answers[0]["body"])
        self.assertIn("#pullrequestreview-41", answers[0]["body"])
        self.assertIn("s110", answers[0]["body"])
        # The answer is posted before the merge.
        posted = [i for i, (_, p, _) in enumerate(fake.writes)
                  if p.endswith("/issues/8/comments")]
        merged_at = [i for i, (_, p, _) in enumerate(fake.writes)
                     if p.endswith("/pulls/8/merge")]
        self.assertLess(posted[0], merged_at[0])

    def test_a_high_lint_finding_that_still_hits_on_the_head_blocks(self):
        # Criterion 2: the thread reads addressed, but the rule still hits
        # on the head version of the file, so nothing merges or is answered.
        addressed = dict(thread(S110, resolved=True, created="2030-01-02T09:00:00Z"),
                         path="dashboard.py")
        for run, why in ((lambda rule, path, text: True, "still hit on the head"),
                         (lambda rule, path, text: None, "could not be re-checked")):
            with self.subTest(why=why):
                fake, refused = self.gate_merge(
                    files=APP_CHANGE, threads=[addressed],
                    reviews=[review(41, S110)],
                    contents={"dashboard.py": "try:\n    pass\nexcept Exception:\n    pass\n"},
                    lint_run=run)
                self.assertIn("s110", refused)
                self.assertIn(why, refused)
                self.assertFalse(self.merged(fake))
                self.assertEqual(self.answers(fake), [])

    def test_an_unsettled_review_body_blocks_and_is_not_answered(self):
        # The open thread sits past the first page of threads: the merge
        # validation reads the first page only, the gate reads them all.
        open_thread = thread(Findings.STYLE, resolved=False)
        fake, refused = self.gate_merge(
            files=APP_CHANGE, threads=[], pages=[[open_thread]],
            reviews=[review(42, Findings.STYLE)])
        self.assertIn("unanswered review", refused)
        self.assertIn("naming", refused)
        self.assertFalse(self.merged(fake))
        self.assertEqual(self.answers(fake), [])
        # The same body with its thread resolved merges, answered once,
        # and no lint re-check runs for a non-lint rule.
        calls = []
        fake, refused = self.gate_merge(
            files=APP_CHANGE, threads=[thread(Findings.STYLE, resolved=True)],
            reviews=[review(42, Findings.STYLE)],
            lint_run=lambda *a: calls.append(a) or False)
        self.assertEqual(refused, "")
        self.assertTrue(self.merged(fake))
        self.assertEqual(calls, [])
        self.assertEqual(len(self.answers(fake)), 1)

    def test_an_already_answered_body_needs_no_factory_answer(self):
        fake, refused = self.gate_merge(
            files=APP_CHANGE, threads=[],
            pages=[[thread(Findings.STYLE, resolved=False)]],
            reviews=[review(43, Findings.STYLE)],
            comments=[pr_comment("Answering the review: "
                                 "https://example.test/x#pullrequestreview-43")])
        self.assertEqual(refused, "")
        self.assertTrue(self.merged(fake))
        self.assertEqual(self.answers(fake), [])

    def test_a_new_duplicate_thread_holds_the_merge_until_it_is_settled(self):
        # Criterion 4 (the PR #1996 fixture): the older duplicate thread is
        # answered and resolved; the identical newer thread is not. An
        # answer to the older thread is no answer to the newer one.
        older = dict(thread(S110, resolved=True, created="2030-01-02T08:00:00Z"),
                     path="dashboard.py")
        newer = dict(thread(S110, resolved=False, created="2030-01-02T09:30:00Z"),
                     path="dashboard.py")
        # The newer thread is past the first page: the first-page threads
        # all read resolved, as they did when PR #1996 merged.
        fake, refused = self.gate_merge(
            files=APP_CHANGE, threads=[older], pages=[[older], [newer]],
            reviews=[review(44, S110)],
            contents={"dashboard.py": "def ok():\n    return 1\n"},
            lint_run=lambda rule, path, text: False)
        self.assertIn("high s110", refused)
        self.assertFalse(self.merged(fake))
        # Once the new thread is settled too, the gate answers and merges.
        fake, refused = self.gate_merge(
            files=APP_CHANGE, threads=[older],
            pages=[[older], [dict(newer, isResolved=True)]],
            reviews=[review(44, S110)],
            contents={"dashboard.py": "def ok():\n    return 1\n"},
            lint_run=lambda rule, path, text: False)
        self.assertEqual(refused, "")
        self.assertTrue(self.merged(fake))
        self.assertEqual(len(self.answers(fake)), 1)


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
