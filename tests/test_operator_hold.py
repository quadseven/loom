"""A lane pull request that declares operator steps, or whose merge deploys
a placement change, waits for the operator (bus/operator_hold.py).

PR_BODY is the body of the lane pull request the brain merged on 2026-10-22
whose deploy took a service down for about four hours. Host names, addresses,
private references and project words are replaced (this repo is public); the
operator sentences are as written."""
from __future__ import annotations

import base64
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from bus import brain, operator_hold, ship_paths  # noqa: E402
from bus.github import GitHub  # noqa: E402
from test_bus_factory import Base, REPO as FACTORY_REPO  # noqa: E402

PR_BODY = """## Intent
Class: logic
- app-dev service moved from node-a to node-b: nodeSelector, dedicated=build:NoSchedule toleration, app-dev-service priority (75000, preemption Never) all retargeted to node-b.
- Service address changed 192.0.2.10 -> 192.0.2.20 (hostIP auth/world, REALM_ADDRESS init env, placement.yaml single source of truth).
- Server footprint for 5000 bots: requests 4cpu/10Gi, limits 6cpu/24Gi, inside the 90% allocatable line on the 44 GiB node-b VM.
- Workers MaxRandomBots/MinRandomBots 2600 -> 5000; RandomBotAccountCount 1050 (1000 RNDbot + 50 AddClass).
- app-chat Ollama disabled on node-b (replicas 0); app-chat uses the Spark gateway; OllamaChat.Url points at spark-gateway service; rollback manifests retained.
- New PriorityClass app-dev-service (value 75000, preemptionPolicy Never); service workloads reference it via priorityClassName patches.
- buildkitd-build-node CPU limit 500m added so a build cannot starve the service.
- log-archive DaemonSet tolerates the build taint so archive pods land on node-b.
- Client/config scripts updated: Start-App.ps1, recording.py, watchdog test fixtures all point at 192.0.2.20:3725.
- Docs updated: INFRASTRUCTURE.md (node-b VM 36->44 GiB), the host README (VM 36->44 GiB), stateful-mobility.md (app-dev rows to node-b, G5), app-dev/README.md (placement, node table), base/README.md (node comparison), backup.yaml (node-b references).
- New runbook docs/runbooks/app-dev-move-to-node-b.md covers cutover, rollback, and local-path data handling.
- All 117 app-dev isolation tests pass; validation suite (ruff, workflow_hygiene, unittest, pytest, check_suite_census, check_python_interpreter) exits 0.
Files: 12 logic, 3 tests, 7 docs

## Why
Move the app-dev service from node-a to the node-b worker so it can support the planned climb to 5000 bots within node memory and CPU limits. The issue lays out four dependent stages: relocate Ollama, grow the worker VM, move the service and its data, then verify the new deployment.

## Summary
Implement the repository changes for the Ollama relocation and service cutover described in the issue, including workload placement and tolerations, service priority, build CPU limits, endpoint updates, documentation, and isolation tests. Update the worker host documentation for the planned 44 GiB VM. Provide the migration and rollback runbook for the local-path data and cutover. Leave VM resizing and the live data migration to the operator.

## Acceptance criteria
- [x] app-dev service placed on node-b worker with build-taint toleration and app-dev-service priority
- [x] Service address 192.0.2.20 used consistently (placement.yaml, client scripts, docs)
- [x] Server resources sized for 5000-bot footprint (4cpu/10Gi request, 6cpu/24Gi limit)
- [x] Workers population 5000 (Max/MinRandomBots, RandomBotAccountCount 1050)
- [x] app-chat Ollama disabled on node-b, moved to Spark gateway
- [x] buildkitd CPU limit 500m; log-archive tolerates build taint
- [x] Migration/rollback runbook written (docs/runbooks/app-dev-move-to-node-b.md)
- [x] All app-dev isolation tests pass; validation suite exits 0
- [ ] Live cutover (not built: operator task - VM grow to 44 GiB and kubectl apply via merged PR)

Size: L

## Out of scope
- Performing the VM resize or live data migration.
- Climbing the bot count beyond the 2600-bot cutover; those steps remain in #5731.
- Raising the MySQL buffer pool or moving the client container.
Rewritten from issue 5744.
Part of #5697

---
Loom ticket example-owner.deploy-5771, lane spark, model spark/example-model.
Validation: check exited 0 at 4bea540420ce (.loom/validation.conf).
Refs #5771"""

TICKET_BODY = """Difficulty: hard

## Why
Move the app-dev service from node-a to the node-b worker.

## What
Implement the repository changes for the cutover. Leave VM resizing and the live data
migration to the operator.

## Acceptance criteria
- [ ] Workloads are configured for the node-b worker.

## Size
L

## Out of scope
- Performing the VM resize or live data migration.
"""

# A plain lane body: no operator steps, the section filled with none.
PLAIN_BODY = """## Why
The page query is slow.

## Summary
Cache the page query.

## Acceptance criteria
- [x] The query is cached

Size: S

## Out of scope
- Other pages

## Operator steps before merge
none

---
Loom ticket T1, lane codex, model gpt-test.
Refs #9"""

MANIFEST = "production/oke/manifests/app-dev/kustomization.yaml"
# The hostname pin hunk of the same pull request, in the JSON patch form a
# kustomization carries: the changed line is the value, the key is on the
# path line beside it.
PIN_PATCH = """@@ -515,7 +515,7 @@ patches:
     patch: |-
       - op: add
         path: /spec/template/spec/nodeSelector/kubernetes.io~1hostname
-        value: node-a
+        value: node-b
   # A CronJob's pod spec sits one level deeper, so CronJobs need their own
   # pin."""
RESOURCES_PATCH = """@@ -124,7 +124,8 @@ spec:
           resources:
             limits:
+              cpu: "500m"
               memory: 2Gi"""
SYNC_WORKFLOW = """name: sync
on:
  push:
    branches: [main]
    paths:
      - "production/oke/manifests/**"
"""


class OperatorSteps(unittest.TestCase):
    def test_the_merged_body_declares_its_operator_steps(self):
        steps = operator_hold.operator_steps(PR_BODY)
        self.assertIn("Leave VM resizing and the live data migration to the operator.", steps)
        self.assertTrue(any("operator task" in s for s in steps), steps)

    def test_the_ticket_alone_holds_a_body_that_says_none(self):
        self.assertEqual(operator_hold.operator_steps(PLAIN_BODY), [])
        self.assertTrue(operator_hold.operator_steps(PLAIN_BODY, TICKET_BODY))

    def test_the_section_lists_steps_and_stops_at_the_footer(self):
        body = PLAIN_BODY.replace("## Operator steps before merge\nnone",
                                  "## Operator steps before merge\n- Scale the app to zero")
        self.assertEqual(operator_hold.section_steps(body), ["Scale the app to zero"])
        self.assertEqual(operator_hold.section_steps(PLAIN_BODY), [])
        self.assertIsNone(operator_hold.section_steps("## Why\nx"))
        self.assertEqual(operator_hold.operator_steps(body), ["Scale the app to zero"])

    def test_a_section_that_starts_with_none_but_names_a_step_is_a_step(self):
        # Security review: "None, but ..." read as no steps.
        body = PLAIN_BODY.replace("## Operator steps before merge\nnone",
                                  "## Operator steps before merge\nNone, but first grow the disk")
        self.assertEqual(operator_hold.section_steps(body), ["None, but first grow the disk"])
        for said in ("none", "None.", "none stated", "N/A"):
            self.assertEqual(operator_hold.section_steps(
                PLAIN_BODY.replace("\nnone\n", "\n%s\n" % said)), [], said)

    def test_a_refactors_cut_over_and_a_loom_status_block_are_not_steps(self):
        body = ("## Summary\nThe old chain's lookups stay until cut-over.\n"
                "<!-- loom:status-body v1 begin -->\n- leave it to the operator\n"
                "<!-- loom:status-body v1 end -->")
        self.assertEqual(operator_hold.operator_steps(body), [])


class Placement(unittest.TestCase):
    def test_a_hostname_pin_value_is_a_placement_change(self):
        self.assertEqual(ship_paths.placement_lines(PIN_PATCH),
                         ["-        value: node-a", "+        value: node-b"])

    def test_tolerations_count_and_a_resource_limit_or_comment_does_not(self):
        added = "@@ -1,2 +1,4 @@\n       nodeSelector:\n+      tolerations:\n+        - key: x\n+  # a nodeSelector note"
        self.assertEqual(ship_paths.placement_lines(added),
                         ["+      tolerations:", "+        - key: x"])
        self.assertEqual(ship_paths.placement_lines(RESOURCES_PATCH), [])

    def test_a_push_workflow_without_a_paths_filter_covers_every_path(self):
        # Security review: a push workflow with no paths filter deploys
        # every path, and was left out of the filters.
        texts = {"sync.yml": SYNC_WORKFLOW, "all.yml": "on: push\njobs: {}\n",
                 "list.yml": "on: [push, pull_request]\n", "pr.yml": "on:\n  pull_request:\n"}
        filters = ship_paths.push_filters(texts)
        self.assertEqual(filters, {"sync.yml": ["production/oke/manifests/**"],
                                   "all.yml": ["**"], "list.yml": ["**"]})
        self.assertEqual(ship_paths.covering(["charts/x/deploy.yaml"], filters),
                         ["all.yml", "list.yml"])

    def test_only_a_manual_paths_listing_exempts_a_file(self):
        self.assertEqual(ship_paths.unlisted([MANIFEST, "charts/x/a.yaml"], []),
                         ["charts/x/a.yaml", MANIFEST])
        self.assertEqual(ship_paths.unlisted([MANIFEST], ["production/**"]), [])


class Route:
    """GitHub by method and path; every write is recorded."""

    def __init__(self, routes):
        self.routes = routes
        self.writes = []

    def __call__(self, method, url, headers, body):
        path = url.split("api.github.com")[-1]
        if path == "/graphql":
            return 200, {}, json.dumps(self.routes["/graphql"]).encode()
        if method != "GET":
            self.writes.append((method, path, json.loads(body) if body else None))
            return 200, {}, json.dumps({"merged": path.endswith("/merge")}).encode()
        for prefix, data in self.routes.items():
            if path.startswith(prefix):
                return 200, {}, json.dumps(data).encode()
        return 404, {}, b"{}"


BRAIN_REPO = "example-owner/example-repo"


def brain_routes(body, files, patches):
    pr = {"number": 8, "state": "open", "draft": False, "body": body + "\nRefs %s#9" % BRAIN_REPO,
          "head": {"ref": "loom/9-work", "sha": "checked", "repo": {"full_name": BRAIN_REPO}},
          "base": {"ref": "main", "repo": {"full_name": BRAIN_REPO}}, "mergeable": True}
    details = {"isDraft": False, "mergeable": "MERGEABLE", "headRefName": "loom/9-work",
               "headRefOid": "checked", "headRepository": {"nameWithOwner": BRAIN_REPO},
               "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]},
               "reviewThreads": {"nodes": []},
               "files": {"totalCount": len(files), "nodes": [{"path": f} for f in files]}}
    ticket = {"number": 9, "state": "open", "body": TICKET_BODY if "Leave" in body else "x",
              "labels": [{"name": "loom:ticket"}, {"name": "loom:pr"}]}
    return {
        "/repos/%s/pulls/8/files" % BRAIN_REPO: [{"filename": f, "patch": patches.get(f, "")}
                                                 for f in files],
        "/repos/%s/pulls/8" % BRAIN_REPO: pr,
        "/graphql": {"data": {"repository": {"pullRequest": details}}},
        "/repos/%s/issues/9/comments" % BRAIN_REPO: [],
        "/repos/%s/issues/9" % BRAIN_REPO: ticket,
        "/repos/%s/contents/.github/workflows/sync.yml" % BRAIN_REPO: {
            "content": base64.b64encode(SYNC_WORKFLOW.encode()).decode()},
        "/repos/%s/contents/.github/workflows" % BRAIN_REPO: [{"name": "sync.yml"}],
    }


class BrainMerge(unittest.TestCase):
    def merge(self, body, files=("bus/a.py",), patches=None):
        fake = Route(brain_routes(body, list(files), patches or {}))
        try:
            brain.merge(GitHub("t", transport=fake), BRAIN_REPO, 8, (BRAIN_REPO, 9),
                        env={"LOOM_MERGE_SETTLE_SECONDS": "0"})
        except ValueError as err:
            return fake, str(err)
        return fake, ""

    def merged(self, fake):
        return any(path.endswith("/pulls/8/merge") for _, path, _ in fake.writes)

    def test_the_merged_body_is_refused_and_its_ticket_asks_the_operator(self):
        # 2026-10-22: this body merged on green checks and a passing review.
        fake, refused = self.merge(PR_BODY)
        self.assertIn("held for the operator", refused)
        self.assertFalse(self.merged(fake))
        asked = [b for m, p, b in fake.writes if p.endswith("/issues/9/comments")]
        self.assertEqual(len(asked), 1)
        self.assertIn("merge #8 yourself", asked[0]["body"])
        self.assertIn("Kind: decision", asked[0]["body"])
        labels = [b for m, p, b in fake.writes if p.endswith("/issues/9/labels")]
        self.assertEqual(labels, [{"labels": ["loom:human"]}])

    def test_a_deployed_placement_change_is_refused(self):
        fake, refused = self.merge(PLAIN_BODY, (MANIFEST,), {MANIFEST: PIN_PATCH})
        self.assertIn("node pins", refused)
        self.assertFalse(self.merged(fake))

    def test_a_deployed_resource_change_and_a_plain_body_still_merge(self):
        for files, patches in (((MANIFEST,), {MANIFEST: RESOURCES_PATCH}), (("bus/a.py",), {})):
            with self.subTest(files=files):
                fake, refused = self.merge(PLAIN_BODY, files, patches)
                self.assertEqual(refused, "")
                self.assertTrue(self.merged(fake))

    def test_status_marks_the_merged_body_blocked(self):
        pr = {"number": 8, "title": "Move", "body": PR_BODY, "labels": [],
              "head": {"ref": "loom/9-work", "sha": "checked"}}
        node = {"commits": {"nodes": [{"commit": {"committedDate": "2030-01-01T12:45:00Z",
                                                  "statusCheckRollup": {"state": "SUCCESS"}}}]},
                "reviews": {"nodes": []}, "reviewThreads": {"nodes": []}}
        lines = brain._pull_request_lines(GitHub("t", transport=Route({})), BRAIN_REPO, [pr],
                                          {"prs": {8: node}}, "grug-tribe")
        self.assertIn("blocked: held for the operator", lines[1])


class FactoryHold(Base):
    def ready(self, body, files=("bus/a.py", "tests/test_a.py"), ticket_body=None):
        self.gh.ticket(5, "pr", body=ticket_body or "Difficulty: medium\n\n## Why\nx\n")
        self.gh.pull(50, 5, files=files, body=body)

    def test_the_merged_body_is_held_and_asked_about_once(self):
        self.ready(PR_BODY)
        self.go()
        self.assertEqual(self.merged, [])
        self.assertEqual(len(self.humans), 1)
        self.assertIn("Loom will not merge it", self.humans[0][1])
        self.go()
        self.assertEqual(self.merged, [])

    def test_a_ticket_that_leaves_steps_to_the_operator_holds_a_plain_body(self):
        ticket = TICKET_BODY.replace("Workloads are configured for the node-b worker.",
                                     "The query is cached")
        self.ready(PLAIN_BODY, ticket_body=ticket)
        self.go()
        self.assertEqual(self.merged, [])
        self.assertEqual(len(self.humans), 1)
        self.assertIn("Leave VM resizing", self.humans[0][1])

    def test_a_placement_change_merging_deploys_is_held(self):
        self.gh.contents[".github/workflows/sync.yml"] = SYNC_WORKFLOW
        self.gh.patches[MANIFEST] = PIN_PATCH
        self.ready(PLAIN_BODY, files=(MANIFEST, "tests/test_a.py"))
        self.go()
        self.assertEqual(self.merged, [])
        self.assertIn("node pins", self.humans[0][1])

    def test_a_placement_change_no_workflow_covers_is_still_held(self):
        # Security review: a manifest no workflow scopes is not "not deployed".
        self.gh.contents[".github/workflows/sync.yml"] = SYNC_WORKFLOW.replace(
            "production/oke/manifests/**", "other/**")
        self.gh.patches[MANIFEST] = PIN_PATCH
        self.ready(PLAIN_BODY, files=(MANIFEST, "tests/test_a.py"))
        self.go()
        self.assertEqual(self.merged, [])
        self.assertIn("no push workflow covers it", self.humans[0][1])

    def test_a_manual_paths_listing_lets_a_placement_change_land(self):
        self.gh.contents[".loom/manual-paths"] = "production/oke/manifests/app-dev/**\n"
        self.gh.contents[".github/workflows/sync.yml"] = SYNC_WORKFLOW
        self.gh.patches[MANIFEST] = PIN_PATCH
        self.ready(PLAIN_BODY, files=(MANIFEST, "tests/test_a.py"))
        self.go()
        self.assertEqual(self.merged, [(50, (FACTORY_REPO, 5))])
        self.assertEqual(self.humans, [])

    def test_an_unreadable_manual_paths_listing_exempts_nothing(self):
        # Review finding: a bad read of .loom/manual-paths must not let a
        # placement change land. No content, or content that is not base64,
        # reads as no listing, so the change stays held.
        for answer in ({"content": "%%% not base64 %%%"}, {"type": "dir"}, [], None):
            with self.subTest(answer=answer):
                class GH:
                    def rest(self, method, path, body=None, ok=()):
                        return type("R", (), {"data": answer})()
                self.assertEqual(operator_hold._manual_listed(GH(), "o/r", "main"), [])

    def test_a_change_to_the_deploy_rules_beside_a_placement_change_is_held(self):
        # Security review: the base branch's listing cannot stand when the
        # pull request edits it or a workflow.
        for rules in (".loom/manual-paths", ".github/workflows/sync.yml"):
            with self.subTest(rules=rules):
                self.setUp()
                self.gh.contents[".loom/manual-paths"] = "production/oke/manifests/app-dev/**\n"
                self.gh.contents[".github/workflows/sync.yml"] = SYNC_WORKFLOW
                self.gh.patches[MANIFEST] = PIN_PATCH
                self.ready(PLAIN_BODY, files=(MANIFEST, rules, "tests/test_a.py"))
                self.go()
                self.assertEqual(self.merged, [])
                self.assertEqual(len(self.humans), 1)


if __name__ == "__main__":
    unittest.main()
