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

from bus import brain, factory, merge_tier, operator_hold, ship_paths  # noqa: E402
from bus.github import GitHub  # noqa: E402
from test_bus_factory import Base, FakeGitHub, NOW, REPO as FACTORY_REPO  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

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

# The second lane pull request the brain merged on 2026-10-22, at 12:04Z:
# every criterion of its ticket is a check on a live host, so the lane
# replaced the repo's 129-line root README (101 lines deleted) with a
# write-up of the ticket and added an unused script. Hosts, addresses and
# product names are replaced; the criteria are as written.
PR_5814_BODY = """## Intent
Class: logic
- The PHP flaw Fix Implementation Summary
Files: 1 logic, 1 docs

## Why
The PHP flaw (two CVEs) is a critical PHP core RCE, fixed in PHP 8.4.27 on 2026-10-21. Details leaked a day early, and mass exploitation is expected. Sweep 2026-10-21 (read-only):
- **host-a** (NAS OS 7.3.1, PHP 8.4.21): the web GUI (nginx + php-fpm, ports 80 and 8443) listens on 192.0.2.30, the IoT VLAN address 198.51.100.114 and its tailnet address. Any IoT-VLAN device can reach it.
- **host-b** (NAS OS 7.3.1, PHP 8.4.21): GUI on ports 80 and 443 at 192.0.2.40 and its tailnet address.
- No router port forward reaches either GUI (live `rest/portforward` read). There is no Tailscale serve or funnel, and the vendor cloud remote access is off.
- Both hosts are also behind NAS OS 7.3.2 (2026-07-08), which fixed a WebGUI path-traversal RCE (a CVE).
- No other PHP runs in the fleet or the cluster.

## Summary
1. Now: block the GUI ports on host-a's IoT interface (br0.150). Restrict tailnet access to ports 80/443/8443 on both hosts to admin devices.
2. Update both hosts to the newest NAS OS release, then to the release that carries PHP 8.4.27 or later when the vendor ships it. Each update reboots the host: drain the k8s worker first (the database, the media server) and roll with the service.

## Acceptance criteria
- [ ] `php -v` on host-a and host-b reports 8.4.27 or later.
- [ ] From an IoT-VLAN address, ports 80, 443 and 8443 on 198.51.100.114 are closed.
- [ ] The tailnet ACL lets only admin devices reach the GUI ports on both hosts.

Size: S

## Out of scope
The host-a Proxmox rebuild (#5777), which removes the NAS OS GUI entirely.
Part of #1423.
Part of #5785

---
Loom ticket example-owner.deploy-5783-r3, lane codex, model gpt-6-luna.
Refs #5783
Validation: check exited 0 at 1e0433a843eb (.loom/validation.conf).
Refs #5783"""
TICKET_5783 = "Difficulty: medium\n\n## Why\n" + PR_5814_BODY.split(
    "\n## Why\n", 1)[1].split("\n---\n", 1)[0].replace("## Summary", "## What")
README_ENTRY = {"filename": "README.md", "status": "modified", "additions": 87, "deletions": 101}
README_BASE = "".join("Homelab overview line %d\n" % n for n in range(1, 129)) + "end"

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

    def test_a_merge_hold_in_the_imperative_is_a_step(self):
        # Replay case operator-step-02 (assay suites/loom_gates): the lane
        # wrote this section and the brain merged it.
        body = PLAIN_BODY.replace(
            "## Operator steps before merge\nnone",
            "## Merge hold\n\nDo not merge until renewal is proven on a real handset "
            "(issue 22). The operator confirms that step.")
        self.assertEqual(operator_hold.operator_steps(body),
                         ["Do not merge until renewal is proven on a real handset (issue 22)."])
        for said in ("Don't merge this before the backup finishes.",
                     "Please do not merge it unless the operator signs off.",
                     "Merge only after the operator rotates the key.",
                     "Merge this once the DNS record exists."):
            self.assertTrue(operator_hold.named_steps(said), said)

    def test_a_sentence_about_merging_that_gives_no_order_is_not_a_step(self):
        for said in ("The brain never merges until the review check passes.",
                     "Loom will not merge a pull request until its review passes.",
                     "This fixes the merge until-loop in the station.",
                     "## Do not merge until\nnone",
                     "After the merge, the station closes the ticket once it ships."):
            self.assertEqual(operator_hold.named_steps(said), [], said)

    def test_a_refactors_cut_over_and_a_loom_status_block_are_not_steps(self):
        body = ("## Summary\nThe old chain's lookups stay until cut-over.\n"
                "<!-- loom:status-body v1 begin -->\n- leave it to the operator\n"
                "<!-- loom:status-body v1 end -->")
        self.assertEqual(operator_hold.operator_steps(body), [])


class HostOnly(unittest.TestCase):
    def test_a_ticket_of_host_checks_only_is_host_only(self):
        self.assertEqual(len(operator_hold.host_only(TICKET_5783)), 3)

    def test_a_ticket_with_one_criterion_a_pull_request_can_meet_is_not(self):
        for body in (TICKET_5783.replace("## Acceptance criteria\n",
                                         "## Acceptance criteria\n- [ ] A test covers the "
                                         "port check script\n"),
                     PLAIN_BODY, TICKET_BODY, "## Why\nx"):
            self.assertEqual(operator_hold.host_only(body), [], body[:60])

    def test_the_brain_refuses_to_brief_a_ticket_no_lane_can_meet(self):
        with self.assertRaisesRegex(ValueError, "live host or network"):
            brain.brief(GitHub("t", transport=Route({})), BRAIN_REPO, 2,
                        "Patch the PHP flaw on both hosts", TICKET_5783)

    def test_a_queued_ticket_of_host_checks_gets_no_lane_and_one_ask(self):
        gh = FakeGitHub()
        gh.ticket(7, "queued", body=TICKET_5783)
        gh.issues[7]["labels"] = [{"name": "loom:ticket"}, {"name": "loom:queued"}]
        asked = []

        def human(gh, repo, number, ask):
            asked.append((number, ask))
            gh.issues[number]["labels"].append({"name": "loom:human"})
        for _ in range(2):
            worker = factory.Factory(gh, [FACTORY_REPO], NOW, trusted=["alice-example"],
                                     available=["codex"], human=human)
            worker.assign_queued()
        self.assertEqual(factory._state(gh.issues[7]), "queued")
        self.assertFalse(factory._lane(gh.issues[7]))
        self.assertEqual(len(asked), 1)
        self.assertIn("no pull request can meet it", asked[0][1])


class HostOnlyRefusedAsk(unittest.TestCase):
    def test_a_refused_ask_still_gives_the_ticket_no_lane(self):
        # Review finding: a failed ask must not crash the queue pass or
        # read as an asked ticket.
        gh = FakeGitHub()
        gh.ticket(7, "queued", body=TICKET_5783)
        gh.issues[7]["labels"] = [{"name": "loom:ticket"}, {"name": "loom:queued"}]

        def human(gh, repo, number, ask):
            raise ValueError("text contains a private hostname")
        worker = factory.Factory(gh, [FACTORY_REPO], NOW, trusted=["alice-example"],
                                 available=["codex"], human=human)
        worker.assign_queued()
        self.assertEqual(factory._state(gh.issues[7]), "queued")
        self.assertFalse(factory._lane(gh.issues[7]))
        self.assertNotIn("loom:human", factory._labels(gh.issues[7]))


class Rewrite(unittest.TestCase):
    class Contents:
        def __init__(self, text):
            self.text, self.reads = text, 0

        def rest(self, method, path, body=None, ok=()):
            self.reads += 1
            data = ({"content": base64.b64encode(self.text.encode()).decode()}
                    if self.text is not None else None)
            return type("R", (), {"data": data})()

    def test_replacing_most_of_the_root_readme_the_ticket_does_not_name_is_a_rewrite(self):
        gh = self.Contents(README_BASE)
        self.assertEqual(operator_hold.rewritten_docs(gh, "o/r", {}, [README_ENTRY],
                                                      TICKET_5783), ["README.md"])

    def test_a_named_a_small_or_an_unreadable_doc_change(self):
        named = TICKET_5783 + "\nUpdate README.md with the new port table.\n"
        self.assertEqual(operator_hold.rewritten_docs(
            self.Contents(README_BASE), "o/r", {}, [README_ENTRY], named), [])
        small = dict(README_ENTRY, deletions=3)
        self.assertEqual(operator_hold.rewritten_docs(
            self.Contents(README_BASE), "o/r", {}, [small], TICKET_5783), [])
        code = dict(README_ENTRY, filename="bus/a.py")
        self.assertEqual(operator_hold.rewritten_docs(
            self.Contents(README_BASE), "o/r", {}, [code], TICKET_5783), [])
        # A base that cannot be read counts as replaced: the hold fails closed.
        self.assertEqual(operator_hold.rewritten_docs(
            self.Contents(None), "o/r", {}, [README_ENTRY], TICKET_5783), ["README.md"])
        removed = {"filename": "docs/GUIDE.md", "status": "removed", "deletions": 40}
        self.assertEqual(operator_hold.rewritten_docs(
            self.Contents(None), "o/r", {}, [removed], TICKET_5783), ["docs/GUIDE.md"])


class RenamedAway(unittest.TestCase):
    def test_a_root_readme_renamed_away_is_removed(self):
        # Security review: renaming README.md away and adding a new file
        # escaped the hold, which read only the new filename.
        renamed = {"filename": "docs/old-overview.txt", "previous_filename": "README.md",
                   "status": "renamed", "deletions": 0}
        added = {"filename": "README.md", "status": "added", "deletions": 0}
        self.assertEqual(operator_hold.rewritten_docs(
            Rewrite.Contents(README_BASE), "o/r", {}, [renamed, added], TICKET_5783),
            ["README.md"])

    def test_any_top_level_readme_agents_or_claude_is_guarded(self):
        for path in ("README", "Readme.rst", "readme.txt", "AGENTS.md", "CLAUDE.md"):
            entry = {"filename": path, "status": "removed", "deletions": 3}
            self.assertEqual(operator_hold.rewritten_docs(
                Rewrite.Contents(None), "o/r", {}, [entry], "## Why\nx"), [path])
        nested = {"filename": "src/readme.txt", "status": "removed", "deletions": 3}
        self.assertEqual(operator_hold.rewritten_docs(
            Rewrite.Contents(None), "o/r", {}, [nested], "## Why\nx"), [])

    def test_a_graphql_rename_falls_back_to_rest_for_the_old_path(self):
        details = {"files": {"totalCount": 1, "nodes": [
            {"path": "docs/old-overview.txt", "deletions": 0, "changeType": "RENAMED"}]}}
        self.assertIsNone(brain._changed_files(details))


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
               "reviewThreads": {"nodes": [], "pageInfo": {"hasNextPage": False}},
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
        # bus/merge_tier.py: the review check passed on the head, and no
        # loom/ pull request merged in the last hour.
        "/repos/%s/commits/checked/check-runs" % BRAIN_REPO: {"check_runs": [
            {"name": "Grug - Elder", "status": "completed", "conclusion": "success"}]},
        "/search/issues": {"total_count": 0},
    }


class BrainMerge(unittest.TestCase):
    def merge(self, body, files=("bus/a.py",), patches=None):
        fake = Route(brain_routes(body, list(files), patches or {}))
        try:
            brain.merge(GitHub("t", transport=fake), BRAIN_REPO, 8, (BRAIN_REPO, 9),
                        env={"LOOM_MERGE_SETTLE_SECONDS": "0",
                             # The risk tier has its own tests (test_merge_tier.py).
                             "LOOM_AUTO_MERGE_TIERS": ",".join(merge_tier.TIERS)})
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

    def test_a_body_that_says_do_not_merge_until_is_refused(self):
        # Replay case operator-step-02: on main this merged on green checks.
        body = PLAIN_BODY.replace(
            "## Operator steps before merge\nnone",
            "## Merge hold\n\nDo not merge until renewal is proven on a real handset "
            "(issue 22). The operator confirms that step.")
        fake, refused = self.merge(body)
        self.assertIn("held for the operator", refused)
        self.assertIn("Do not merge until renewal", refused)
        self.assertFalse(self.merged(fake))

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

    def test_the_readme_replacing_pull_request_is_refused(self):
        # 2026-10-22 12:04Z: merged on green checks and passing reviews.
        routes = brain_routes(PR_5814_BODY, ["README.md", "production/scripts/check.py"], {})
        routes["/graphql"]["data"]["repository"]["pullRequest"]["files"] = {
            "totalCount": 2, "nodes": [
                {"path": "README.md", "deletions": 101, "changeType": "MODIFIED"},
                {"path": "production/scripts/check.py", "deletions": 0, "changeType": "ADDED"}]}
        routes["/repos/%s/contents/README.md" % BRAIN_REPO] = {
            "content": base64.b64encode(README_BASE.encode()).decode()}
        fake = Route(routes)
        with self.assertRaisesRegex(ValueError, "mostly replaces README.md"):
            brain.merge(GitHub("t", transport=fake), BRAIN_REPO, 8, (BRAIN_REPO, 9),
                        env={"LOOM_MERGE_SETTLE_SECONDS": "0"})
        self.assertFalse(self.merged(fake))

    def test_a_readme_renamed_away_is_refused_through_the_rest_file_list(self):
        routes = brain_routes(PLAIN_BODY, ["docs/old-overview.txt", "README.md"], {})
        routes["/graphql"]["data"]["repository"]["pullRequest"]["files"] = {
            "totalCount": 2, "nodes": [
                {"path": "docs/old-overview.txt", "deletions": 0, "changeType": "RENAMED"},
                {"path": "README.md", "deletions": 0, "changeType": "ADDED"}]}
        routes["/repos/%s/pulls/8/files" % BRAIN_REPO] = [
            {"filename": "docs/old-overview.txt", "previous_filename": "README.md",
             "status": "renamed", "deletions": 0, "patch": ""},
            {"filename": "README.md", "status": "added", "deletions": 0, "patch": "+x"}]
        fake = Route(routes)
        with self.assertRaisesRegex(ValueError, "mostly replaces README.md"):
            brain.merge(GitHub("t", transport=fake), BRAIN_REPO, 8, (BRAIN_REPO, 9),
                        env={"LOOM_MERGE_SETTLE_SECONDS": "0"})
        self.assertFalse(self.merged(fake))

    def test_status_marks_the_merged_body_blocked(self):
        pr = {"number": 8, "title": "Move", "body": PR_BODY, "labels": [],
              "head": {"ref": "loom/9-work", "sha": "checked"}}
        node = {"commits": {"nodes": [{"commit": {"committedDate": "2030-01-01T12:45:00Z",
                                                  "statusCheckRollup": {"state": "SUCCESS"}}}]},
                "reviews": {"nodes": []}, "reviewThreads": {"nodes": []}}
        routes = {"/repos/%s/issues/9" % BRAIN_REPO: {
                      "state": "open", "labels": [{"name": "loom:ticket"}, {"name": "loom:pr"}]},
                  "/repos/%s/pulls/8/files" % BRAIN_REPO: [{"filename": "bus/a.py"}],
                  "/repos/%s/pulls/8" % BRAIN_REPO: {"mergeable": True},
                  "/search/issues": {"total_count": 0}}
        lines = brain._pull_request_lines(GitHub("t", transport=Route(routes)), BRAIN_REPO, [pr],
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

    def test_a_pull_request_replacing_the_root_readme_is_held(self):
        self.gh.contents["README.md"] = README_BASE
        self.ready(PLAIN_BODY, files=("README.md", "tests/test_a.py"))
        real = self.gh._get

        def with_deletions(path):
            found = real(path)
            if "/pulls/50/files" in path:
                found.data = [dict(e, **README_ENTRY) if e["filename"] == "README.md" else e
                              for e in found.data]
            return found
        self.gh._get = with_deletions
        self.go()
        self.assertEqual(self.merged, [])
        self.assertIn("mostly replaces README.md", self.humans[0][1])

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
        class GH:
            def __init__(self, answer):
                self.answer = answer

            def rest(self, method, path, body=None, ok=()):
                return type("R", (), {"data": self.answer})()

        for answer in ({"content": "%%% not base64 %%%"}, {"type": "dir"}, [], None):
            with self.subTest(answer=answer):
                self.assertEqual(operator_hold._manual_listed(GH(answer), "o/r", "main"), [])

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
