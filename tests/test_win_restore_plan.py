"""The lane restore plan after a reboot needs no shell and no workspace CLI."""
import json
import unittest

from lanes.win import restore_plan as rp

MANIFEST = {"lanes": [
    {"name": "a", "kind": "codex", "cwd": "/w/a", "flags": ["--x"],
     "restore_policy": "resume"},
    {"name": "b", "kind": "agy", "cwd": "/w/b", "restore_policy": "resume"},
]}
SNAP = {"lanes": {"a": {"session_id": "s1", "pane": "p1"},
                  "b": {"session_id": "s2"}}}


class RestorePlan(unittest.TestCase):
    def test_each_lane_plans_an_agent_start_with_its_resume_arguments(self):
        out = rp.plan(MANIFEST, SNAP, [])
        self.assertEqual(out["lanes"][0]["command"], [
            "agent", "start", "a", "--kind", "codex", "--pane", "p1",
            "--", "--x", "resume", "s1"])
        self.assertEqual(out["lanes"][1]["command"], [
            "agent", "start", "b", "--kind", "agy", "--", "--conversation", "s2"])
        self.assertEqual(out["lanes"][0]["cwd"], "/w/a")
        self.assertEqual(out["exit"], 0)

    def test_a_second_run_with_every_lane_running_changes_nothing(self):
        out = rp.plan(MANIFEST, SNAP, ["a", "b"])
        self.assertEqual([x["command"] for x in out["lanes"]], [None, None])
        self.assertEqual({x["outcome"] for x in out["lanes"]}, {rp.RUNNING})
        self.assertEqual(out["exit"], 0)

    def test_a_lane_with_no_resumable_id_awaits_a_decision_and_exits_nonzero(self):
        out = rp.plan(MANIFEST, {"lanes": {"a": {"session_id": "s1"}}}, [])
        self.assertEqual(out["lanes"][1]["outcome"], rp.ASK)
        self.assertIsNone(out["lanes"][1]["command"])
        self.assertEqual(out["exit"], 1)

    def test_a_fresh_policy_starts_without_resume_arguments(self):
        manifest = {"lanes": [{"name": "c", "kind": "claude", "restore_policy": "fresh"}]}
        out = rp.plan(manifest, {}, [])
        self.assertEqual(out["lanes"][0]["command"],
                         ["agent", "start", "c", "--kind", "claude", "--"])
        self.assertEqual(out["lanes"][0]["outcome"], rp.FRESH)
        self.assertEqual(out["exit"], 1)

    def test_an_unknown_kind_fails(self):
        manifest = {"lanes": [{"name": "d", "kind": "other", "restore_policy": "resume"}]}
        self.assertEqual(rp.plan(manifest, SNAP, [])["lanes"][0]["outcome"], rp.FAILED)


class SnapshotSecrets(unittest.TestCase):
    def test_the_planned_snapshot_text_holds_no_secret(self):
        rp.check_snapshot(json.dumps(SNAP))

    def test_the_wrapper_variable_and_key_shapes_are_refused(self):
        for text in ("GH_TOKEN=x", "GITHUB_TOKEN", "ghs_" + "a1" * 8,
                     "github_pat_" + "a1" * 8, "sk-" + "a" * 20,
                     "-----BEGIN RSA PRIVATE KEY-----"):
            with self.assertRaises(ValueError, msg=text):
                rp.check_snapshot(json.dumps({"env": text}))


if __name__ == "__main__":
    unittest.main()
