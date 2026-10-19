"""A module merge creates one reviewable source-pin pull request."""
import base64
import json
import unittest
from unittest import mock
from types import SimpleNamespace
from tempfile import TemporaryDirectory

from bus import roll_pin
from bus.hunt_plan import problems
from bus.station import Settings, Station

SOURCE = "example-owner/module"
DEPLOY = "example-owner/deploy"
GOAL = "example-owner/project#12"
FIRST = "a" * 40
SECOND = "b" * 40
OLD = "0" * 40
DOC = "docs/ship.md"
PINS = [{"path": "src/one.env", "key": "MODULE_SHA"},
        {"path": "src/two.env", "key": "MODULE_SHA"},
        {"path": "src/three.env", "key": "MODULE_SHA"}]


class FakeGitHub:
    def __init__(self):
        self.doc = ("# Ship\n```loom-roll-pins\n" + json.dumps({
            "deployment_repo": DEPLOY, "build_workflow": "Build image",
            "sources": [{"repo": SOURCE, "pins": PINS}]}) + "\n```\n")
        self.base = {p["path"]: "MODULE_SHA=%s\nOTHER=keep\n" % OLD for p in PINS}
        self.base[PINS[0]["path"]] = 'MODULE_SHA="%s"\nOTHER=keep\n' % OLD
        self.branch = None
        self.pulls = []
        self.merges = {}
        self.writes = []
        self.comments = []
        self.build_ready = True
        self.build_sha = "e" * 40

    def paginate(self, path):
        if path == "/repos/example-owner/project/issues/12/comments":
            return self.comments
        if path == "/repos/%s/pulls/9/files" % DEPLOY:
            return [{"filename": pin["path"]} for pin in PINS]
        raise AssertionError(path)

    def rest(self, method, path, body=None, ok=()):
        if method != "GET":
            self.writes.append((method, path, body))
        for route in (self._source_route, self._ref_route, self._contents_route,
                      self._pull_route, self._goal_route):
            answer = route(method, path, body)
            if answer is not None:
                return answer
        raise AssertionError((method, path, body))

    def _source_route(self, method, path, body):
        if path == "/repos/%s/contents/%s" % ("example-owner/project", DOC):
            return self.answer({"content": self.encoded(self.doc), "sha": "doc"})
        if path.endswith("/pulls?state=closed&sort=updated&direction=desc&per_page=100"):
            source = path.split("/repos/", 1)[1].split("/pulls?", 1)[0]
            return self.answer(self.merges.get(source, []))
        if path == "/repos/%s" % DEPLOY:
            return self.answer({"default_branch": "main"})
        if path == "/repos/%s/pulls?state=open&per_page=100" % DEPLOY:
            return self.answer(self.pulls)
        return None

    def _ref_route(self, method, path, body):
        if path == "/repos/%s/git/ref/heads/main" % DEPLOY:
            return self.answer({"object": {"sha": "f" * 40}})
        if path.startswith("/repos/%s/git/ref/heads/" % DEPLOY):
            if self.branch is None:
                return SimpleNamespace(status=404, data=None)
            return self.answer({"object": {"sha": "e" * 40}})
        if path == "/repos/%s/git/refs" % DEPLOY:
            self.branch = dict(self.base)
            return self.answer({})
        return None

    def _contents_route(self, method, path, body):
        if not path.startswith("/repos/%s/contents/" % DEPLOY):
            return None
        file = path.split("/contents/", 1)[1].split("?", 1)[0]
        if method == "PUT":
            self.branch[file] = base64.b64decode(body["content"]).decode()
            return self.answer({"commit": {"sha": "e" * 40}})
        value = self.branch[file] if "?ref=" in path else self.base[file]
        return self.answer({"content": self.encoded(value), "sha": "file-sha"})

    def _pull_route(self, method, path, body):
        if path == "/repos/%s/pulls" % DEPLOY and method == "POST":
            self.pulls = [{"number": 8, "head": {"ref": body["head"], "sha": "e" * 40},
                           "html_url": "https://github.com/%s/pull/8" % DEPLOY}]
            return self.answer(self.pulls[0])
        if path == "/repos/%s/pulls/8" % DEPLOY and method == "PATCH":
            return self.answer(self.pulls[0])
        if path.startswith("/repos/%s/actions/runs?" % DEPLOY) or \
                path.startswith("/repos/%s/actions/workflows/build-image.yml/runs?" % DEPLOY):
            return self.answer({"workflow_runs": ([{"id": 4, "head_sha": self.build_sha,
                "name": "Build image",
                "html_url": "https://github.com/%s/actions/runs/4" % DEPLOY}]
                if self.build_ready else [])})
        return None

    def _goal_route(self, method, path, body):
        if path == "/repos/example-owner/project/issues/12/comments?per_page=100":
            return self.answer(self.comments)
        if path == "/repos/example-owner/project/issues/12/comments" and method == "POST":
            self.comments.append(body)
            return self.answer(body)
        return None

    @staticmethod
    def answer(data):
        return SimpleNamespace(status=200, data=data)

    @staticmethod
    def encoded(value):
        return base64.b64encode(value.encode()).decode()


class RollPinTest(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        self.specs = [{"goal": GOAL, "repo": "example-owner/project", "path": DOC}]

    def merge(self, sha, repo=SOURCE):
        pulls = self.gh.merges.setdefault(repo, [])
        pulls.insert(0, {"number": len(pulls) + 1,
            "merged_at": "2030-01-01T00:00:00Z", "merge_commit_sha": sha})

    def test_merge_opens_one_pull_and_second_merge_updates_exact_ship_document_pins(self):
        self.merge(FIRST)
        with TemporaryDirectory() as home:
            settings = Settings(home, [], [], [], roll_ship_docs=self.specs)
            station = Station(self.gh, settings)
            station.roll_source_pins()
        self.assertEqual(len([w for w in self.gh.writes if w[1] == "/repos/%s/pulls" % DEPLOY]), 1)
        created = next(body for method, path, body in self.gh.writes
                       if method == "POST" and path == "/repos/%s/pulls" % DEPLOY)
        self.assertEqual(problems(created["body"]), [])
        self.assertEqual(set(self.gh.branch), {p["path"] for p in PINS})
        self.assertEqual(self.gh.branch[PINS[0]["path"]],
                         'MODULE_SHA="%s"\nOTHER=keep\n' % FIRST)
        self.assertTrue(all(text == "MODULE_SHA=%s\nOTHER=keep\n" % FIRST
                            for text in list(self.gh.branch.values())[1:]))
        self.assertIn("<!-- loom:journal v1 -->", self.gh.comments[0]["body"])
        self.assertIn("actions/runs/4", self.gh.comments[0]["body"])
        self.merge(SECOND)
        station.roll_source_pins()
        self.assertEqual(len([w for w in self.gh.writes if w[1] == "/repos/%s/pulls" % DEPLOY]), 1)
        self.assertTrue(all(SECOND in text for text in self.gh.branch.values()))
        writes = len(self.gh.writes)
        station.roll_source_pins()
        self.assertEqual(len(self.gh.writes), writes)

    def test_merge_outside_a_roll_shipped_source_opens_nothing(self):
        self.merge(FIRST, "example-owner/other-module")
        roll_pin.run(self.gh, self.specs)
        self.assertFalse(any(path == "/repos/%s/pulls" % DEPLOY for _, path, _ in self.gh.writes))

    def test_journal_waits_for_the_build_run_and_retries_without_rewriting_the_pull(self):
        self.merge(FIRST)
        self.gh.build_ready = False
        roll_pin.run(self.gh, self.specs)
        self.assertEqual(self.gh.comments, [])
        writes = len(self.gh.writes)
        self.gh.build_ready = True
        roll_pin.run(self.gh, self.specs)
        self.assertEqual(len(self.gh.writes), writes + 1)
        self.assertIn("actions/runs/4", self.gh.comments[0]["body"])

    def test_merge_can_pin_a_named_deployed_fork_branch(self):
        class ForkGitHub(FakeGitHub):
            def rest(self, method, path, body=None, ok=()):
                if path == "/repos/example-owner/deployed-fork/git/ref/heads/live":
                    return self.answer({"object": {"sha": SECOND}})
                return super().rest(method, path, body, ok)

        gh = ForkGitHub()
        gh.doc = gh.doc.replace('"repo": "' + SOURCE + '"',
                                '"repo": "' + SOURCE + '", '
                                '"revision_repo": "example-owner/deployed-fork", '
                                '"revision_ref": "live"')
        gh.merges[SOURCE] = [{"number": 1, "merged_at": "2030-01-01T00:00:00Z",
                              "merge_commit_sha": FIRST}]
        roll_pin.run(gh, self.specs)
        self.assertTrue(all(SECOND in text for text in gh.branch.values()))

    def test_build_started_by_merging_source_pin_pull_is_journaled(self):
        self.merge(FIRST)
        roll_pin.run(self.gh, self.specs)
        pr = dict(self.gh.pulls[0], merged_at="2030-01-01T01:00:00Z",
                  merge_commit_sha="d" * 40,
                  body="Source pins name `%s`" % FIRST)
        self.gh.base = dict(self.gh.branch)
        self.gh.pulls = []
        self.gh.comments = []
        self.gh.merges[DEPLOY] = [pr]
        self.gh.build_sha = "d" * 40
        self.gh.build_ready = False
        roll_pin.run(self.gh, self.specs)
        self.assertEqual(self.gh.comments, [])
        self.gh.build_ready = True
        roll_pin.run(self.gh, self.specs)
        self.assertEqual(len(self.gh.comments), 1)
        self.assertIn("actions/runs/4", self.gh.comments[0]["body"])

    def test_workflow_file_identifies_the_build_run(self):
        self.merge(FIRST)
        self.gh.doc = self.gh.doc.replace('"build_workflow": "Build image"',
                                           '"build_workflow": "build-image.yml"')
        roll_pin.run(self.gh, self.specs)
        self.assertEqual(len(self.gh.comments), 1)

    def test_open_family_gate_alerts_for_a_held_roll_pin_without_a_brain_tick(self):
        self.merge(FIRST)
        self.specs[0]["gate_url"] = "https://example.com/family-gate"
        with TemporaryDirectory() as home, \
                mock.patch("bus.roll_pin.health.fetch", return_value={"open": True}), \
                mock.patch("bus.roll_pin.brain.merge", side_effect=ValueError("checks are not successful")) as merge:
            station = Station(self.gh, Settings(home, [], [], [], roll_ship_docs=self.specs))
            station.roll_source_pins()
            station.roll_source_pins()
        merge.assert_called()
        alerts = [comment["body"] for comment in self.gh.comments
                  if "Family gate is open" in comment["body"]]
        self.assertEqual(len(alerts), 1)
        self.assertIn("needs a brain merge review", alerts[0])

    def test_closed_family_gate_leaves_roll_pin_held_without_alert(self):
        self.merge(FIRST)
        self.specs[0]["gate_url"] = "https://example.com/family-gate"
        with TemporaryDirectory() as home, \
                mock.patch("bus.roll_pin.health.fetch", return_value={"open": False}), \
                mock.patch("bus.roll_pin.brain.merge") as merge:
            station = Station(self.gh, Settings(home, [], [], [], roll_ship_docs=self.specs))
            station.roll_source_pins()
        merge.assert_not_called()
        self.assertEqual(len(self.gh.pulls), 1)
        self.assertFalse(any("Family gate is open" in c["body"] for c in self.gh.comments))

    def test_open_family_gate_merges_a_reviewed_roll_pin_without_a_brain_tick(self):
        self.merge(FIRST)
        self.specs[0]["gate_url"] = "https://example.com/family-gate"
        with TemporaryDirectory() as home, \
                mock.patch("bus.roll_pin.health.fetch", return_value={"open": True}), \
                mock.patch("bus.roll_pin.brain.merge", return_value={"merged": True}) as merge:
            station = Station(self.gh, Settings(home, [], ["example-bot"], [],
                                                roll_ship_docs=self.specs))
            station.roll_source_pins()
        merge.assert_called_once()
        self.assertEqual(merge.call_args.args[1:3], (DEPLOY, 8))
        self.assertTrue(merge.call_args.kwargs["adopt"])
        self.assertEqual(merge.call_args.kwargs["env"]["LOOM_BRIEF_AUTHORS"], "example-bot")
        self.assertFalse(any("Family gate is open" in c["body"] for c in self.gh.comments))

    def test_a_hand_made_roll_pin_is_alerted_when_the_family_gate_opens(self):
        self.merge(FIRST)
        self.specs[0]["gate_url"] = "https://example.com/family-gate"
        self.gh.pulls = [{"number": 9, "title": "Roll source pin", "body": "Held for family gate",
                          "head": {"ref": "manual-roll-pin", "sha": "c" * 40}}]
        with TemporaryDirectory() as home, \
                mock.patch("bus.roll_pin.health.fetch", return_value={"open": True}), \
                mock.patch("bus.roll_pin.brain.merge", side_effect=ValueError("checks pending")) as merge:
            Station(self.gh, Settings(home, [], [], [], roll_ship_docs=self.specs)).roll_source_pins()
        merge.assert_called_once()
        self.assertEqual(merge.call_args.args[1:3], (DEPLOY, 9))
        self.assertTrue(any("Family gate is open" in c["body"] for c in self.gh.comments))

    def test_a_hand_made_roll_pin_is_alerted_when_the_gate_answers_one(self):
        self.merge(FIRST)
        self.specs[0]["gate_url"] = "https://example.com/family-gate"
        self.gh.pulls = [{"number": 9, "title": "Roll source pin", "body": "Held for family gate",
                          "head": {"ref": "manual-roll-pin", "sha": "c" * 40}}]
        with TemporaryDirectory() as home, \
                mock.patch("bus.roll_pin.health.fetch", return_value={"open": 1}), \
                mock.patch("bus.roll_pin.brain.merge", side_effect=ValueError("checks pending")) as merge:
            Station(self.gh, Settings(home, [], [], [], roll_ship_docs=self.specs)).roll_source_pins()
        merge.assert_called_once()
        self.assertTrue(any("Family gate is open" in c["body"] for c in self.gh.comments))

    def test_a_hand_made_roll_pin_stays_held_while_the_family_gate_is_closed(self):
        self.merge(FIRST)
        self.specs[0]["gate_url"] = "https://example.com/family-gate"
        self.gh.pulls = [{"number": 9, "title": "Roll source pin", "body": "Held for family gate",
                          "head": {"ref": "manual-roll-pin", "sha": "c" * 40}}]
        with TemporaryDirectory() as home, \
                mock.patch("bus.roll_pin.health.fetch", return_value={"open": False}), \
                mock.patch("bus.roll_pin.brain.merge") as merge:
            Station(self.gh, Settings(home, [], [], [], roll_ship_docs=self.specs)).roll_source_pins()
        merge.assert_not_called()
        self.assertFalse(any("Family gate is open" in c["body"] for c in self.gh.comments))
        self.assertFalse(any(path == "/repos/%s/pulls" % DEPLOY for _, path, _ in self.gh.writes))

    def test_a_hand_made_roll_pin_stays_held_when_the_gate_answers_zero(self):
        self.merge(FIRST)
        self.specs[0]["gate_url"] = "https://example.com/family-gate"
        self.gh.pulls = [{"number": 9, "title": "Roll source pin", "body": "Held for family gate",
                          "head": {"ref": "manual-roll-pin", "sha": "c" * 40}}]
        with TemporaryDirectory() as home, \
                mock.patch("bus.roll_pin.health.fetch", return_value={"open": 0}), \
                mock.patch("bus.roll_pin.brain.merge") as merge:
            Station(self.gh, Settings(home, [], [], [], roll_ship_docs=self.specs)).roll_source_pins()
        merge.assert_not_called()
        self.assertFalse(any("Family gate is open" in c["body"] for c in self.gh.comments))


if __name__ == "__main__":
    unittest.main()
