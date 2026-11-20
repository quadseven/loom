from __future__ import annotations

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain, merge_verdict  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

REPO = "example-owner/example-repo"
BODY = ("## Why\nwhy\n## What\nwhat\n## Acceptance criteria\n"
        "- [ ] pytest tests/test_thing.py::test_it passes.\n## Size\nS\n")


class FakeGitHub:
    def __init__(self, names):
        self.names = names

    def rest(self, method, path, *args, **kwargs):
        data = [{"filename": n, "patch": "+x"} for n in self.names]
        return types.SimpleNamespace(data=data, status=200)


def tier(names, label="class:logic", body=BODY):
    pr = {"number": 5, "labels": [{"name": label}]}
    return brain._risk_tier(FakeGitHub(names), REPO, 5, pr, {"body": body}, None)


class NamedTestTests(unittest.TestCase):
    def test_a_fixture_only_test_change_is_blocked_and_a_test_module_is_not(self):
        with self.assertRaisesRegex(ValueError, merge_verdict.NO_NAMED_TEST):
            tier(["app/thing.py", "tests/fixtures/x.json"])
        tier(["app/thing.py", "tests/test_thing.py"])

    def test_a_docs_pull_request_and_a_ticket_naming_no_test_are_not_blocked(self):
        tier(["README.md"], label="class:docs")
        tier(["app/thing.py"], body=BODY.replace("pytest tests/test_thing.py::test_it passes",
                                                 "the page loads"))

    def test_the_verdict_rule_alone(self):
        rule = merge_verdict.named_test_blocker
        self.assertEqual(rule("logic", ["a test passes"], ["a.py"]), merge_verdict.NO_NAMED_TEST)
        self.assertIsNone(rule("tests", ["a test passes"], ["a.py"]))
        self.assertIsNone(rule("logic", ["a test passes"], ["spec/a.spec.js"]))


if __name__ == "__main__":
    unittest.main()
