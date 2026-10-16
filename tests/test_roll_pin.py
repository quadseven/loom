"""Tests for station roll source-pin PR management."""
from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from bus import hunt_plan, roll_pin
from bus.github import GitHubError

GOAL_REPO = "quadseven/wow"
GOAL_NUMBER = 123
MODULE_REPO = "quadseven/mod-dungeon-clear"
DEPLOY_REPO = "quadseven/worldserver-deploy"
UNRELATED_REPO = "quadseven/unrelated-repo"

OLD_SHA = "0" * 40
FIRST_SHA = "1" * 40
SECOND_SHA = "2" * 40

DOCKERFILE_ORIG = 'ARG DUNGEON_CLEAR_SHA="%s"\n' % OLD_SHA
PINS_ENV_ORIG = 'MOD_DUNGEON_CLEAR_REV="%s"\n' % OLD_SHA
CMAKE_ORIG = 'set(DUNGEON_CLEAR_COMMIT "%s")\n' % OLD_SHA

SHIP_DOC = (
    "## Roll\n"
    "Roll: worldserver\n"
    "Deployment repo: %s\n\n"
    "| Module | Path | Variable |\n"
    "| --- | --- | --- |\n"
    "| mod-dungeon-clear | docker/Dockerfile | DUNGEON_CLEAR_SHA |\n"
    "| mod-dungeon-clear | build/pins.env | MOD_DUNGEON_CLEAR_REV |\n"
    "| mod-dungeon-clear | CMakeLists.txt | DUNGEON_CLEAR_COMMIT |\n"
    % DEPLOY_REPO
)


class FakeGitHub:
    def __init__(self) -> None:
        self.files: Dict[str, Dict[str, str]] = {
            "main": {
                "docker/Dockerfile": DOCKERFILE_ORIG,
                "build/pins.env": PINS_ENV_ORIG,
                "CMakeLists.txt": CMAKE_ORIG,
            }
        }
        self.branches: Dict[str, str] = {"main": "commit-main"}
        self.pull: Optional[Dict[str, Any]] = None
        self.created_pulls = 0
        self.updated_pulls = 0
        self.goal_comments: List[str] = []
        self.module_merges: Dict[str, List[Dict[str, Any]]] = {}
        self.build_runs: Dict[str, List[Dict[str, Any]]] = {}
        self.calls: List[tuple] = []

    def rest(self, method: str, path: str, body: Any = None, ok: tuple = ()) -> SimpleNamespace:
        self.calls.append((method, path, body))
        if path == "/repos/%s" % DEPLOY_REPO:
            return SimpleNamespace(status=200, data={"default_branch": "main"})
        if path.startswith("/repos/%s/contents/" % DEPLOY_REPO):
            return self._handle_deploy_contents(method, path, body)
        if path.startswith("/repos/%s/git/ref/heads/" % DEPLOY_REPO) or path == "/repos/%s/git/refs" % DEPLOY_REPO:
            return self._handle_deploy_refs(method, path, body)
        if path.startswith("/repos/%s/pulls" % DEPLOY_REPO):
            return self._handle_deploy_pulls(method, path, body)
        if "/check-runs" in path or "/actions/runs" in path:
            return self._handle_runs(path)
        if path.startswith("/repos/") and "/pulls?state=closed" in path:
            return self._handle_closed_pulls(path)
        if path.startswith("/repos/%s/issues/%d/comments" % (GOAL_REPO, GOAL_NUMBER)) and method == "POST":
            self.goal_comments.append(str((body or {}).get("body") or ""))
            return SimpleNamespace(status=201, data={"id": len(self.goal_comments)})
        if path.startswith("/repos/%s/contents/" % GOAL_REPO):
            return SimpleNamespace(status=404, data=None)
        if 404 in ok:
            return SimpleNamespace(status=404, data=None)
        raise AssertionError("unexpected call: %s %s %r" % (method, path, body))

    def _handle_deploy_contents(self, method: str, path: str, body: Any) -> SimpleNamespace:
        file_path, _, ref = path.partition("?ref=")
        file_path = file_path.removeprefix("/repos/%s/contents/" % DEPLOY_REPO)
        branch = ref if ref else "main"
        branch_files = self.files.get(branch, self.files["main"])
        if method == "GET":
            if file_path not in branch_files:
                return SimpleNamespace(status=404, data=None)
            content = branch_files[file_path]
            return SimpleNamespace(status=200, data={
                "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
                "sha": "sha-%s" % file_path,
            })
        if method == "PUT":
            branch = str(body.get("branch") or "main")
            if branch not in self.files:
                self.files[branch] = dict(self.files["main"])
            raw = base64.b64decode(body["content"]).decode("utf-8")
            self.files[branch][file_path] = raw
            return SimpleNamespace(status=200, data={})
        raise AssertionError("unsupported method %s on contents" % method)

    def _handle_deploy_refs(self, method: str, path: str, body: Any) -> SimpleNamespace:
        if method == "GET":
            branch = path.removeprefix("/repos/%s/git/ref/heads/" % DEPLOY_REPO)
            if branch in self.branches:
                return SimpleNamespace(status=200, data={"object": {"sha": self.branches[branch]}})
            return SimpleNamespace(status=404, data=None)
        if method == "POST" and path == "/repos/%s/git/refs" % DEPLOY_REPO:
            branch = str(body["ref"]).removeprefix("refs/heads/")
            self.branches[branch] = str(body["sha"])
            if branch not in self.files:
                self.files[branch] = dict(self.files["main"])
            return SimpleNamespace(status=201, data={"ref": body["ref"], "object": {"sha": body["sha"]}})
        raise AssertionError("unsupported ref call: %s %s" % (method, path))

    def _handle_deploy_pulls(self, method: str, path: str, body: Any) -> SimpleNamespace:
        if path == "/repos/%s/pulls?state=open&per_page=100" % DEPLOY_REPO:
            return SimpleNamespace(status=200, data=[self.pull] if self.pull else [])
        if path == "/repos/%s/pulls" % DEPLOY_REPO and method == "POST":
            if hunt_plan.problems(body.get("body", "")):
                raise GitHubError("body fails Hunt Plan", 422)
            self.created_pulls += 1
            self.pull = {
                "number": 59,
                "title": body["title"],
                "head": {"ref": body["head"]},
                "base": body["base"],
                "body": body["body"],
            }
            return SimpleNamespace(status=201, data=self.pull)
        if path == "/repos/%s/pulls/59" % DEPLOY_REPO and method == "PATCH":
            if hunt_plan.problems(body.get("body", "")):
                raise GitHubError("body fails Hunt Plan", 422)
            self.updated_pulls += 1
            assert self.pull is not None
            self.pull["title"] = body["title"]
            self.pull["body"] = body["body"]
            return SimpleNamespace(status=200, data=self.pull)
        raise AssertionError("unsupported pulls call: %s %s" % (method, path))

    def _handle_runs(self, path: str) -> SimpleNamespace:
        head = path.split("/commits/")[1].split("/")[0] if "/commits/" in path else ""
        runs = self.build_runs.get(head, [{"id": 101, "html_url": "https://github.com/runs/101"}])
        if "/check-runs" in path:
            return SimpleNamespace(status=200, data={"check_runs": runs})
        return SimpleNamespace(status=200, data={"workflow_runs": runs})

    def _handle_closed_pulls(self, path: str) -> SimpleNamespace:
        repo = path.split("/repos/")[1].split("/pulls")[0]
        merges = self.module_merges.get(repo, [])
        return SimpleNamespace(status=200, data=merges)


class RollPinTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.tmp_dir.name, "roll-pins.json")

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_merge_on_roll_shipped_module_opens_and_updates_same_pull_request(self) -> None:
        gh = FakeGitHub()
        gh.branches["loom/source-pin-worldserver"] = "commit-branch-1"
        gh.module_merges[MODULE_REPO] = [{
            "number": 36,
            "title": "Fix Deadmines chest",
            "merged_at": "2026-10-16T06:20:00Z",
            "merge_commit_sha": FIRST_SHA,
        }]
        gh.build_runs["commit-branch-1"] = [
            {"id": "run-98765", "html_url": "https://github.com/quadseven/worldserver-deploy/actions/runs/98765"}
        ]

        goal_issue = {"number": GOAL_NUMBER, "body": SHIP_DOC}
        goal_homes = [(GOAL_REPO, goal_issue)]

        # Pass 1: first merge event on mod-dungeon-clear
        count1 = roll_pin.run_station(gh, goal_homes, self.state_file, now=1000.0)
        self.assertEqual(count1, 1)
        self.assertEqual(gh.created_pulls, 1)
        self.assertEqual(gh.updated_pulls, 0)
        self.assertIsNotNone(gh.pull)
        assert gh.pull is not None
        self.assertEqual(gh.pull["number"], 59)
        self.assertIn("Source pin: worldserver mod-dungeon-clear to %s" % FIRST_SHA[:12], gh.pull["title"])
        self.assertEqual(hunt_plan.problems(gh.pull["body"]), [])
        self.assertIn(FIRST_SHA[:12], gh.pull["body"])
        self.assertIn("Fix Deadmines chest", gh.pull["body"])

        # Check that exactly the 3 pinned locations moved
        branch_files = gh.files["loom/source-pin-worldserver"]
        self.assertEqual(branch_files["docker/Dockerfile"], 'ARG DUNGEON_CLEAR_SHA="%s"\n' % FIRST_SHA)
        self.assertEqual(branch_files["build/pins.env"], 'MOD_DUNGEON_CLEAR_REV="%s"\n' % FIRST_SHA)
        self.assertEqual(branch_files["CMakeLists.txt"], 'set(DUNGEON_CLEAR_COMMIT "%s")\n' % FIRST_SHA)

        # Check goal journal comment
        self.assertEqual(len(gh.goal_comments), 1)
        comment = gh.goal_comments[0]
        self.assertIn("<!-- loom:journal v1 -->", comment)
        self.assertIn("run-98765", comment)
        self.assertIn("source-pin PR #59", comment)

        # Pass 2 before landing: second merge event on mod-dungeon-clear
        gh.branches["loom/source-pin-worldserver"] = "commit-branch-2"
        gh.module_merges[MODULE_REPO] = [{
            "number": 37,
            "title": "Fix second boss door",
            "merged_at": "2026-10-16T08:00:00Z",
            "merge_commit_sha": SECOND_SHA,
        }]
        gh.build_runs["commit-branch-2"] = [
            {"id": "run-98766", "html_url": "https://github.com/quadseven/worldserver-deploy/actions/runs/98766"}
        ]

        count2 = roll_pin.run_station(gh, goal_homes, self.state_file, now=1500.0)
        self.assertEqual(count2, 1)

        # PR count remains 1: updated existing pull request instead of opening a second!
        self.assertEqual(gh.created_pulls, 1)
        self.assertEqual(gh.updated_pulls, 1)
        self.assertIn("Source pin: worldserver mod-dungeon-clear to %s" % SECOND_SHA[:12], gh.pull["title"])
        self.assertEqual(hunt_plan.problems(gh.pull["body"]), [])
        self.assertIn(SECOND_SHA[:12], gh.pull["body"])
        self.assertIn("Fix second boss door", gh.pull["body"])

        # Exactly the 3 pinned locations moved to SECOND_SHA
        branch_files = gh.files["loom/source-pin-worldserver"]
        self.assertEqual(branch_files["docker/Dockerfile"], 'ARG DUNGEON_CLEAR_SHA="%s"\n' % SECOND_SHA)
        self.assertEqual(branch_files["build/pins.env"], 'MOD_DUNGEON_CLEAR_REV="%s"\n' % SECOND_SHA)
        self.assertEqual(branch_files["CMakeLists.txt"], 'set(DUNGEON_CLEAR_COMMIT "%s")\n' % SECOND_SHA)

        # Goal home journal recorded the second build run
        self.assertEqual(len(gh.goal_comments), 2)
        comment2 = gh.goal_comments[1]
        self.assertIn("run-98766", comment2)

        # Pass 3: no new merges - nothing changed
        count3 = roll_pin.run_station(gh, goal_homes, self.state_file, now=2000.0)
        self.assertEqual(count3, 0)
        self.assertEqual(gh.created_pulls, 1)
        self.assertEqual(gh.updated_pulls, 1)
        self.assertEqual(len(gh.goal_comments), 2)

    def test_merge_on_repo_no_goal_ships_by_roll_opens_nothing(self) -> None:
        gh = FakeGitHub()
        # Unrelated repo has a merge event
        gh.module_merges[UNRELATED_REPO] = [{
            "number": 5,
            "title": "Unrelated feature",
            "merged_at": "2026-10-16T09:00:00Z",
            "merge_commit_sha": FIRST_SHA,
        }]

        # Goal home only ships mod-dungeon-clear, not unrelated-repo
        goal_issue = {"number": GOAL_NUMBER, "body": SHIP_DOC}
        goal_homes = [(GOAL_REPO, goal_issue)]

        count = roll_pin.run_station(gh, goal_homes, self.state_file, now=1000.0)
        self.assertEqual(count, 0)
        self.assertEqual(gh.created_pulls, 0)
        self.assertEqual(gh.updated_pulls, 0)
        self.assertEqual(len(gh.goal_comments), 0)
        self.assertNotIn("loom/source-pin-worldserver", gh.files)

    def test_parse_ship_document_json(self) -> None:
        json_doc = json.dumps({
            "roll": "worldserver",
            "deployment_repo": DEPLOY_REPO,
            "modules": {
                "mod-dungeon-clear": [
                    {"path": "docker/Dockerfile", "var": "DUNGEON_CLEAR_SHA"},
                    {"path": "build/pins.env", "var": "MOD_DUNGEON_CLEAR_REV"},
                    {"path": "CMakeLists.txt", "var": "DUNGEON_CLEAR_COMMIT"},
                ]
            }
        })
        spec = roll_pin.parse_ship_document("```json\n%s\n```" % json_doc)
        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertEqual(spec.roll, "worldserver")
        self.assertEqual(spec.deploy_repo, DEPLOY_REPO)
        self.assertEqual(len(spec.modules["mod-dungeon-clear"]), 3)

    def test_update_location_content_various_formats(self) -> None:
        new_sha = "f" * 40
        # Docker ARG
        docker = roll_pin.update_location_content(
            'ARG MOD_SHA="old"\n',
            roll_pin.Location("docker/Dockerfile", var="MOD_SHA"),
            new_sha,
        )
        self.assertEqual(docker, 'ARG MOD_SHA="%s"\n' % new_sha)

        # Environment variable
        env = roll_pin.update_location_content(
            'MOD_REV="old"\n',
            roll_pin.Location("pins.env", var="MOD_REV"),
            new_sha,
        )
        self.assertEqual(env, 'MOD_REV="%s"\n' % new_sha)

        # CMake
        cmake = roll_pin.update_location_content(
            'set(MOD_COMMIT "old")\n',
            roll_pin.Location("CMakeLists.txt", var="MOD_COMMIT"),
            new_sha,
        )
        self.assertEqual(cmake, 'set(MOD_COMMIT "%s")\n' % new_sha)

        # JSON key
        raw_json = roll_pin.update_location_content(
            '{"mod_sha": "old"}\n',
            roll_pin.Location("config.json", key="mod_sha"),
            new_sha,
        )
        self.assertEqual(raw_json, '{"mod_sha": "%s"}\n' % new_sha)


if __name__ == "__main__":
    unittest.main()
