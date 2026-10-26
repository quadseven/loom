"""bus/live_cluster.py: a lane's manifest change checked against the live
cluster, SSM and DNS before a push (loom#1832). kubectl, aws and DNS are
fakes: a function handed to check(), or scripts on PATH for the command.
No test opens a socket, so none needs the lane check sandbox's localhost."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bus import live_cluster as lc  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

DEPLOYMENT = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
spec:
  template:
    spec:
      nodeSelector:
        tier: "{tier}"   # where it runs
      containers:
      - name: app
        image: example/app:1
"""

AFFINITY = """apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: store
spec:
  template:
    spec:
      tolerations:
      - key: dedicated
        operator: Equal
        value: storage
        effect: NoSchedule
      affinity:
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
            - matchExpressions:
              - key: disk
                operator: In
                values: [ssd, "nvme"]
      containers:
      - name: db
        image: example/db:1
"""

CRONJOB_WITH_SECRETS = """apiVersion: batch/v1
kind: CronJob
metadata:
  name: smoke-check
spec:
  jobTemplate:
    spec:
      template:
        spec:
          containers:
          - name: smoke
            image: example/curl:1
            args:
            - |
              curl -fsS https://app.example.com/health
              curl -fsS http://api.svc.cluster.local/ready
---
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: app-env
spec:
  data:
  - secretKey: token
    remoteRef:
      key: /example/app/token
  - secretKey: other
    remoteRef: {key: not-an-ssm-path}
"""

ANALYSIS = """apiVersion: argoproj.io/v1alpha1
kind: AnalysisTemplate
metadata:
  name: success-rate
spec:
  metrics:
  - name: up
    provider:
      web:
        url: "https://metrics.example.org/api/v1/query?q={{args.service}}"
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: probed
spec:
  template:
    spec:
      containers:
      - name: app
        image: example/app:1
        readinessProbe:
          httpGet: {path: /ready, port: 8080, host: ready.example.net}
"""


def node(name, labels, ready=True, taints=()):
    return {"metadata": {"name": name, "labels": labels},
            "spec": {"taints": list(taints)},
            "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}


class FakeTools:
    """kubectl and aws for check(): records each command, answers from state."""

    def __init__(self, nodes=(), params=(), kubectl_exit=0, aws_exit=0):
        self.nodes, self.params = list(nodes), set(params)
        self.kubectl_exit, self.aws_exit = kubectl_exit, aws_exit
        self.calls = []

    def __call__(self, cmd):
        self.calls.append(cmd)
        if cmd[0] == "kubectl":
            return self.kubectl_exit, json.dumps({"items": self.nodes}) if not self.kubectl_exit else ""
        if self.aws_exit:
            return self.aws_exit, ""
        wanted = next(a for a in cmd if a.startswith("Key=Name")).split("Values=", 1)[1].split(",")
        return 0, json.dumps([n for n in wanted if n in self.params])


def change(files):
    """A Change whose every line is added."""
    return lc.Change({p: (t, set(range(1, t.count("\n") + 2))) for p, t in files.items()})


class YamlReaderTest(unittest.TestCase):
    def test_reads_a_pod_template_with_flow_and_block_values(self):
        docs = lc.load_documents(AFFINITY + "---\n" + CRONJOB_WITH_SECRETS)
        self.assertEqual(len(docs), 3)
        spec = docs[0]["spec"]["template"]["spec"]
        term = spec["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
        self.assertEqual(term["nodeSelectorTerms"][0]["matchExpressions"][0]["values"], ["ssd", "nvme"])
        args = docs[1]["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["args"]
        self.assertTrue(args[0].startswith("curl -fsS https://app.example.com/health\n"))
        text = AFFINITY + "---\n" + CRONJOB_WITH_SECRETS
        self.assertEqual(args[0].line, text.splitlines().index(
            "              curl -fsS https://app.example.com/health") + 1)

    def test_anchors_and_escaped_quotes_are_read(self):
        docs = lc.load_documents('env: &shared\n  - {name: A, value: /etc/x}\n'
                                 'command: ["sh", "-c", "echo \\"hi\\""]\n')
        self.assertEqual(docs[0]["env"], [{"name": "A", "value": "/etc/x"}])
        self.assertEqual(docs[0]["command"][2], 'echo "hi"')

    def test_text_it_cannot_read_is_an_error_not_a_guess(self):
        with self.assertRaises(lc.YamlError):
            lc.load_documents("a: 1\n    b: 2\nc: [x\n")


class NamedInTest(unittest.TestCase):
    def test_collects_selectors_terms_params_and_hosts(self):
        named, touched = lc.named_in(change({
            "apps/web.yaml": DEPLOYMENT.format(tier="edge"),
            "apps/store.yaml": AFFINITY,
            "apps/smoke.yaml": CRONJOB_WITH_SECRETS,
            "apps/analysis.yaml": ANALYSIS,
        }))
        self.assertTrue(touched)
        self.assertEqual({p.workload: p.selector for p in named.placements},
                         {"Deployment/web": {"tier": "edge"}, "StatefulSet/store": {}})
        store = next(p for p in named.placements if p.workload == "StatefulSet/store")
        self.assertEqual(store.terms, [[("disk", "In", ["ssd", "nvme"])]])
        self.assertEqual(named.params, {"/example/app/token"})
        self.assertEqual(named.hosts, {"app.example.com", "api.svc.cluster.local",
                                       "metrics.example.org", "ready.example.net"})

    def test_only_lines_the_change_adds_are_checked(self):
        text = DEPLOYMENT.format(tier="edge")
        tier_line = text.splitlines().index('        tier: "edge"   # where it runs') + 1
        named, _ = lc.named_in(lc.Change({"web.yaml": (text, {tier_line})}))
        self.assertEqual(len(named.placements), 1)
        named, touched = lc.named_in(lc.Change({"web.yaml": (text, {tier_line + 2})}))
        self.assertTrue(touched)
        self.assertEqual(named.placements, [])

    def test_ssm_sync_entries_and_aws_cli_names_are_params(self):
        sync = json.dumps({"_doc": "see {/not/this}", "ns/app": {"token": "/example/sync/token",
                                                                  "url": "x={/example/sync/url}"}},
                          indent=1)
        named, touched = lc.named_in(change({
            "manifests/ssm-secret-sync/secrets.json": sync,
            "scripts/seed.sh": "aws ssm put-parameter --name /example/seeded --type String\n"
                               "aws ssm get-parameters --names '/example/a' /example/b --query x\n",
        }))
        self.assertTrue(touched)
        self.assertEqual(named.params, {"/example/sync/token", "/example/sync/url", "/example/seeded",
                                        "/example/a", "/example/b"})

    def test_a_change_with_no_manifest_is_not_touched(self):
        named, touched = lc.named_in(change({"README.md": "see https://docs.example.com\n",
                                             "config.yaml": "retries: 3\n"}))
        self.assertFalse(touched)
        self.assertEqual((named.placements, named.params), ([], set()))


class CheckTest(unittest.TestCase):
    NODES = [node("n1", {"tier": "web", "disk": "ssd"}),
             node("n2", {"tier": "edge"}, ready=False),
             node("n3", {"disk": "nvme"}, taints=[{"key": "dedicated", "value": "storage",
                                                  "effect": "NoSchedule"}])]

    def run_check(self, files, tools, resolver=lambda h: "ok", pod=False):
        named, _ = lc.named_in(change(files))
        return lc.check(named, run=tools, resolver=resolver, pod=pod)

    def test_a_label_only_a_not_ready_node_carries_is_a_miss(self):
        result = self.run_check({"web.yaml": DEPLOYMENT.format(tier="edge")}, FakeTools(self.NODES))
        self.assertEqual(result.misses, ["node label tier=edge (Deployment/web) is on no Ready node"])
        code, lines = lc.report(result)
        self.assertEqual(code, 1)
        self.assertEqual(lines, ["live-cluster check refused the change: node label tier=edge "
                                 "(Deployment/web) is on no Ready node"])

    def test_a_label_a_ready_node_carries_passes(self):
        result = self.run_check({"web.yaml": DEPLOYMENT.format(tier="web")}, FakeTools(self.NODES))
        self.assertEqual((result.misses, result.notes), ([], []))
        self.assertEqual(lc.report(result)[0], 0)

    def test_affinity_terms_and_taints_are_checked(self):
        self.assertEqual(self.run_check({"s.yaml": AFFINITY}, FakeTools(self.NODES)).misses, [])
        no_toleration = AFFINITY.replace("value: storage", "value: other")
        only_tainted = [self.NODES[2]]
        self.assertEqual(self.run_check({"s.yaml": no_toleration}, FakeTools(only_tainted)).misses,
                         ["no Ready node matches every node selector and affinity term of "
                          "StatefulSet/store and has no taint it does not tolerate"])
        missing = AFFINITY.replace("[ssd, \"nvme\"]", "[hdd]")
        self.assertEqual(self.run_check({"s.yaml": missing}, FakeTools(self.NODES)).misses,
                         ["node affinity disk In [hdd] (StatefulSet/store) matches no Ready node"])

    def test_a_missing_ssm_parameter_is_named_and_no_value_is_read(self):
        tools = FakeTools(self.NODES, params=set())
        result = self.run_check({"c.yaml": CRONJOB_WITH_SECRETS}, tools)
        self.assertEqual(result.misses, ["SSM parameter /example/app/token does not exist"])
        aws = [c for c in tools.calls if c[0] == "aws"]
        self.assertEqual([c[:3] for c in aws], [["aws", "ssm", "describe-parameters"]])
        self.assertFalse(any("get-parameter" in " ".join(c) for c in tools.calls))
        tools = FakeTools(self.NODES, params={"/example/app/token"})
        self.assertEqual(self.run_check({"c.yaml": CRONJOB_WITH_SECRETS}, tools).misses, [])

    def test_a_host_that_does_not_resolve_is_named_and_in_cluster_names_are_skipped(self):
        asked = []

        def resolver(host):
            asked.append(host)
            return "missing" if host == "ready.example.net" else "ok"

        result = self.run_check({"a.yaml": ANALYSIS}, FakeTools(), resolver)
        self.assertEqual(result.misses, ["host ready.example.net does not resolve"])
        self.assertEqual(sorted(asked), ["metrics.example.org", "ready.example.net"])

    def test_dns_that_gives_no_answer_is_a_note_not_a_miss(self):
        with mock.patch.object(lc, "DNS_TIMEOUT", 0.2):
            started = time.monotonic()
            result = self.run_check({"a.yaml": ANALYSIS}, FakeTools(),
                                    lambda h: time.sleep(5) or "ok")
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(result.misses, [])
        self.assertEqual(result.notes, ["live-cluster check could not run: DNS gave no answer "
                                        "for 2 host(s), so they were not checked"])

    def test_an_unreachable_cluster_and_ssm_fail_open_with_a_note(self):
        tools = FakeTools(kubectl_exit=1, aws_exit=255)
        result = self.run_check({"w.yaml": DEPLOYMENT.format(tier="edge"),
                                 "c.yaml": CRONJOB_WITH_SECRETS}, tools)
        self.assertEqual(result.misses, [])
        self.assertEqual(result.notes, [
            "live-cluster check could not run: kubectl get nodes: kubectl exited 1, "
            "so node selectors were not checked",
            "live-cluster check could not run: aws ssm describe-parameters: aws exited 255, "
            "so SSM parameters were not checked"])
        self.assertEqual(lc.report(result)[0], 0)

    def test_a_lane_pod_skips_kubectl_and_ssm_with_a_note(self):
        tools = FakeTools(self.NODES)
        result = self.run_check({"w.yaml": DEPLOYMENT.format(tier="edge"),
                                 "c.yaml": CRONJOB_WITH_SECRETS}, tools, pod=True)
        self.assertEqual(tools.calls, [])
        self.assertEqual(result.misses, [])
        self.assertEqual(result.notes, [
            "live-cluster check could not run: a lane pod has no kubeconfig, so node selectors were not checked",
            "live-cluster check could not run: a lane pod has no AWS key, so SSM parameters were not checked"])

    def test_resolve_tells_a_missing_name_from_a_dns_failure(self):
        def fail(code):
            return mock.patch.object(socket, "getaddrinfo", side_effect=socket.gaierror(code, "x"))

        with fail(socket.EAI_NONAME):
            self.assertEqual(lc.resolve("gone.example.com"), "missing")
        with fail(socket.EAI_AGAIN):
            self.assertEqual(lc.resolve("slow.example.com"), "unknown")
        with mock.patch.object(socket, "getaddrinfo", return_value=[]):
            self.assertEqual(lc.resolve("here.example.com"), "ok")


FAKE_KUBECTL = """#!/usr/bin/env bash
printf 'kubectl %s\\n' "$*" >> "$FAKE_LOG"
printf '%s' "$FAKE_NODES"
"""

FAKE_AWS = """#!/usr/bin/env bash
printf 'aws %s\\n' "$*" >> "$FAKE_LOG"
printf '[]'
"""


class MainTest(unittest.TestCase):
    """The module as the runner calls it: a git range in a tree."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.tree, self.bin, self.log = root / "tree", root / "bin", root / "calls.log"
        self.bin.mkdir()
        self.log.touch()
        for name, text in (("kubectl", FAKE_KUBECTL), ("aws", FAKE_AWS)):
            (self.bin / name).write_text(text)
            (self.bin / name).chmod(0o755)
        self.git("init", "-q", "-b", "main", str(self.tree), cwd=root)
        (self.tree / "README").write_text("seed\n")
        (self.tree / "old.yaml").write_text(DEPLOYMENT.format(tier="retired"))
        self.commit("seed")

    def tearDown(self):
        self._tmp.cleanup()

    def git(self, *args, cwd=None):
        subprocess.run(["git", *args], cwd=cwd or self.tree, check=True, capture_output=True)

    def commit(self, message):
        self.git("add", "-A")
        self.git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", message)

    def run_main(self, **env):
        nodes = json.dumps({"items": [node("n1", {"tier": "web"})]})
        return subprocess.run(
            [sys.executable, "-B", "-m", "bus.live_cluster", str(self.tree), "HEAD~1..HEAD"],
            cwd=ROOT, capture_output=True, text=True, timeout=60,
            env={"PATH": "%s:/usr/bin:/bin" % self.bin, "FAKE_LOG": str(self.log),
                 "FAKE_NODES": nodes, "HOME": self._tmp.name, **env})

    def test_a_miss_exits_1_naming_the_label_and_the_key(self):
        (self.tree / "web.yaml").write_text(DEPLOYMENT.format(tier="edge"))
        (self.tree / "secret.yaml").write_text(CRONJOB_WITH_SECRETS.split("---\n", 1)[1])
        self.commit("add web")
        done = self.run_main()
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertEqual(done.stdout.strip(),
                         "live-cluster check refused the change: node label tier=edge (Deployment/web) is on no "
                         "Ready node; SSM parameter /example/app/token does not exist")
        # The base's own label (tier=retired) is not this change's to fix.
        self.assertNotIn("retired", done.stdout)

    def test_a_change_with_no_manifest_runs_nothing(self):
        (self.tree / "README").write_text("changed\n")
        self.commit("docs")
        done = self.run_main()
        self.assertEqual((done.returncode, done.stdout), (0, ""), done.stderr)
        self.assertEqual(self.log.read_text(), "")

    def test_a_lane_pod_runs_no_kubectl_or_aws(self):
        (self.tree / "web.yaml").write_text(DEPLOYMENT.format(tier="edge"))
        self.commit("add web")
        done = self.run_main(LOOM_SSM_DIR=self._tmp.name)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("live-cluster check could not run: a lane pod has no kubeconfig", done.stdout)
        self.assertEqual(self.log.read_text(), "")

    def test_kustomization_resources_it_adds_are_read_whole(self):
        (self.tree / "app").mkdir()
        (self.tree / "app" / "web.yaml").write_text(DEPLOYMENT.format(tier="edge"))
        self.commit("unlisted manifest")
        (self.tree / "app" / "kustomization.yaml").write_text(
            "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\n"
            "resources:\n- web.yaml\n")
        self.commit("list it")
        done = self.run_main()
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("node label tier=edge (Deployment/web)", done.stdout)

    def test_the_trees_git_config_runs_no_program(self):
        # The tree is the agent's: an external diff or textconv driver it
        # sets in .git/config must not run while the check reads the change.
        marker = Path(self._tmp.name) / "ran"
        driver = Path(self._tmp.name) / "driver.sh"
        driver.write_text("#!/bin/sh\ntouch '%s'\ncat \"$1\"\n" % marker)
        driver.chmod(0o755)
        self.git("config", "diff.external", str(driver))
        self.git("config", "diff.lc.textconv", str(driver))
        self.git("config", "diff.lc.command", str(driver))
        (self.tree / ".gitattributes").write_text("*.yaml diff=lc\n")
        (self.tree / "web.yaml").write_text(DEPLOYMENT.format(tier="edge"))
        self.commit("add web")
        done = self.run_main(GIT_EXTERNAL_DIFF=str(driver))
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("node label tier=edge (Deployment/web)", done.stdout)
        self.assertFalse(marker.exists())

    def test_a_bad_range_exits_2(self):
        done = subprocess.run([sys.executable, "-B", "-m", "bus.live_cluster", str(self.tree), "nope..HEAD"],
                              cwd=ROOT, capture_output=True, text=True, timeout=60,
                              env={**os.environ, "PATH": "%s:/usr/bin:/bin" % self.bin})
        self.assertEqual(done.returncode, 2)


if __name__ == "__main__":
    unittest.main()
