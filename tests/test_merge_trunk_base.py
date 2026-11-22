"""A pull request based on the goal home's Trunk: branch is not stacked."""
import json
import unittest
import unittest.mock

from bus import brain
from bus.github import GitHub
from tests.test_bus_brain import Routed
from shadow_env import setUpModule, tearDownModule  # noqa: F401

REPO = "example-owner/example-repo"
TRUNK = "feature/trunk"


def reply(status, body):
    return status, {}, json.dumps(body).encode()


def routes(base):
    pr = {"number": 9, "body": "", "base": {"ref": base,
          "repo": {"default_branch": "main"}}, "head": {"ref": "loom/7-work"}}
    goal = {"number": 5, "state": "open", "labels": [{"name": "loom:goal"}],
            "body": "Goal home\nTrunk: %s\n\n## Why\n" % TRUNK}
    ticket = {"number": 7, "state": "open", "labels": [{"name": "loom:ticket"}],
              "body": "Part of #5\n"}
    return {("GET", "/pulls/9"): reply(200, pr),
            ("GET", "/issues/7"): reply(200, ticket),
            ("GET", "/issues/5"): reply(200, goal)}


class MergeTrunkBaseTests(unittest.TestCase):
    def merge_error(self, base):
        gh = GitHub("t", transport=Routed(routes(base)))
        stop = RuntimeError("past the stacked check")
        with unittest.mock.patch.object(brain, "_pr_graphql", side_effect=stop):
            try:
                brain.merge(gh, REPO, 9, ticket=(REPO, 7))
            except (ValueError, RuntimeError) as exc:
                return str(exc)
        return ""

    def test_merge_does_not_call_the_goal_trunk_base_stacked(self):
        self.assertNotIn("stacked on branch", self.merge_error(TRUNK))

    def test_merge_still_names_a_base_that_is_not_the_trunk(self):
        self.assertIn("stacked on branch other", self.merge_error("other"))

    def test_status_line_carries_no_stacked_blocker_for_a_trunk_base(self):
        gh = GitHub("t", transport=Routed(routes(TRUNK)))
        pr = {"number": 9, "title": "Work", "head": {"ref": "loom/7-work", "sha": "h"},
              "labels": [], "base": {"ref": TRUNK}}
        data = {"trunk": "main", "prs": {9: None}}
        line = brain._pull_request_line(gh, REPO, pr, data, "grug-tribe", (), "Grug - Elder")
        self.assertNotIn("stacked on branch", line)
        gh = GitHub("t", transport=Routed(routes("other")))
        pr["base"] = {"ref": "other"}
        line = brain._pull_request_line(gh, REPO, pr, data, "grug-tribe", (), "Grug - Elder")
        self.assertIn("stacked on branch other", line)


if __name__ == "__main__":
    unittest.main()
