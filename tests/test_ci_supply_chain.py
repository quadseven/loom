"""loom is public and the station deploys the newest main commit whose test
run passed, so the workflows that gate a deploy are supply chain. Every
third-party action is pinned to a commit SHA (a moved tag cannot change what
runs), and every pull request is leak-scanned, including one opened outside a
lane and a title or body edited after the push."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
USES = re.compile(r"^\s*(?:-\s+)?uses:\s*([^\s#]+)", re.MULTILINE)
SHA = re.compile(r"@[0-9a-f]{40}$")
LEAK_SCAN = "quadseven/infra-public/.github/workflows/_reusable.leak-scan.yml@"


def workflow_files() -> list[Path]:
    return sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))


class ActionPins(unittest.TestCase):
    def test_every_third_party_action_is_pinned_to_a_commit_sha(self):
        checked = 0
        for path in workflow_files():
            for ref in USES.findall(path.read_text()):
                if ref.startswith("./"):
                    continue
                checked += 1
                self.assertRegex(ref, SHA, "%s: %s is not pinned to a full commit SHA"
                                 % (path.name, ref))
        self.assertGreater(checked, 0, "no uses: line found; the pattern matched nothing")

    def test_the_pattern_flags_a_tag_and_skips_a_local_action(self):
        text = ("    steps:\n      - uses: actions/checkout@v4\n"
                "      - uses: ./.github/actions/local\n"
                "    uses: org/repo/.github/workflows/x.yml@%s # v1\n" % ("a" * 40))
        refs = USES.findall(text)
        self.assertEqual(refs, ["actions/checkout@v4", "./.github/actions/local",
                                "org/repo/.github/workflows/x.yml@" + "a" * 40])
        self.assertIsNone(SHA.search(refs[0]))
        self.assertIsNotNone(SHA.search(refs[2]))


class LeakScanCaller(unittest.TestCase):
    def test_pull_requests_call_the_shared_leak_scan_at_a_commit_sha(self):
        path = WORKFLOWS / "check.leak-scan.yml"
        self.assertTrue(path.is_file(), "check.leak-scan.yml is missing")
        text = path.read_text()
        calls = [ref for ref in USES.findall(text) if ref.startswith(LEAK_SCAN)]
        self.assertEqual(len(calls), 1, "check.leak-scan.yml must call %s" % LEAK_SCAN)
        self.assertRegex(calls[0], SHA)
        # edited: a title or body can leak long after the last push.
        self.assertIn("types: [opened, synchronize, reopened, edited]", text)
        self.assertIn("contents: read", text)


class HygieneLinter(unittest.TestCase):
    def test_workflow_changes_run_the_pinned_public_linter(self):
        path = WORKFLOWS / "check.workflow-hygiene.yml"
        self.assertTrue(path.is_file(), "check.workflow-hygiene.yml is missing")
        text = path.read_text()
        self.assertIn("repository: quadseven/infra-public", text)
        self.assertRegex(text, r"ref: [0-9a-f]{40}\n")
        self.assertIn("workflow_hygiene.py", text)
        self.assertIn("- '.github/**'", text)


if __name__ == "__main__":
    unittest.main()
