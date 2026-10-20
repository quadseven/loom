#!/usr/bin/env python3
"""Limit detection per lane type, the heartbeat limit payload, and
limit-aware routing (loom#1101).

Each lane type implements a method to detect its current usage vs limit
(bus.limits.detect); the heartbeat carries the result as the "limits"
member (remaining, reset_at, expires_at); the dispatcher prefers lanes
with expiring credits first, then by remaining capacity.
"""

import json
import os
import time
import unittest
from pathlib import Path
from unittest import mock

import bus.limits as limits
import dispatch.dispatcher as dispatcher

NOW = 1_780_000_000.0
FIELDS = ("remaining", "reset_at", "expires_at", "unit", "source", "stale")


def payload(**kw):
    base = {"remaining": None, "reset_at": None, "expires_at": None,
            "unit": None, "source": "test", "stale": False}
    base.update(kw)
    return base


def make_home(root, files):
    home = Path(root) / "loom-home"
    for rel, text in files.items():
        p = home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return str(home)


class DetectTests(unittest.TestCase):
    def test_payload_has_all_fields(self):
        got = limits.detect("muse", home=self._tmp(), env={}, now=NOW)
        self.assertEqual(tuple(sorted(got)), tuple(sorted(FIELDS)))

    def test_unknown_lane_is_unknown(self):
        got = limits.detect("nope", home=self._tmp(), env={}, now=NOW)
        self.assertIsNone(got["remaining"])
        self.assertIn("unknown", got["source"])

    def test_muse_reports_no_usage_reaches_loom(self):
        got = limits.detect("muse", home=self._tmp(), env={}, now=NOW)
        self.assertIsNone(got["remaining"])
        self.assertIn("no usage", got["source"])

    def test_sparks_reads_nvidia_smi(self):
        with mock.patch.object(limits, "_run", return_value="25\n") as run:
            got = limits.detect("sparks", home=self._tmp(), env={}, now=NOW)
        run.assert_called_once()
        self.assertAlmostEqual(got["remaining"], 0.75)
        self.assertEqual(got["unit"], "gpu")
        self.assertIn("nvidia-smi", got["source"])

    def test_sparks_without_nvidia_smi_is_unknown(self):
        with mock.patch.object(limits, "_run", side_effect=OSError("nope")):
            got = limits.detect("sparks", home=self._tmp(), env={}, now=NOW)
        self.assertIsNone(got["remaining"])
        self.assertIn("nvidia-smi", got["source"])

    def test_openrouter_uses_key_endpoint(self):
        body = {"data": {"limit": 10.0, "usage": 2.5,
                         "rate_limit": {"requests": 20, "interval": "1m"}}}
        with mock.patch.object(limits, "_http_json", return_value=body) as get:
            got = limits.detect("openrouter", home=self._tmp(),
                                env={"LOOM_OPENROUTER_API_KEY": "k"}, now=NOW)
        get.assert_called_once()
        self.assertAlmostEqual(got["remaining"], 0.75)
        self.assertEqual(got["reset_at"], NOW + 60)
        self.assertEqual(got["unit"], "credits")

    def test_openrouter_without_key_is_unknown(self):
        got = limits.detect("openrouter", home=self._tmp(), env={}, now=NOW)
        self.assertIsNone(got["remaining"])
        self.assertIn("LOOM_OPENROUTER_API_KEY", got["source"])

    def test_claude_limit_hold_reports_zero_remaining(self):
        beat = {"lane": "claude", "at": NOW, "available": False,
                "reason": "limit: the 5-hour window; unavailable until reset",
                "retry_at": NOW + 3600}
        home = make_home(self._tmp(), {"state/usage/claude.json": json.dumps(beat)})
        got = limits.detect("claude", home=home, env={}, now=NOW)
        self.assertEqual(got["remaining"], 0.0)
        self.assertEqual(got["reset_at"], NOW + 3600)

    def test_codex_without_hold_is_unknown(self):
        beat = {"lane": "codex", "at": NOW, "available": True, "reason": ""}
        home = make_home(self._tmp(), {"state/usage/codex.json": json.dumps(beat)})
        got = limits.detect("codex", home=home, env={}, now=NOW)
        self.assertIsNone(got["remaining"])
        self.assertIn("no usage API", got["source"])

    def test_anthropic_spend_vs_budget(self):
        rows = [json.dumps({"at": NOW - 100, "slot": "anthropic", "cost_usd": 30.0})]
        home = make_home(self._tmp(),
                         {"state/usage/spend.jsonl": "\n".join(rows) + "\n"})
        env = {"LOOM_ANTHROPIC_BUDGET_USD": "200",
               "LOOM_ANTHROPIC_BUDGET_SINCE": "2026-01-01",
               "LOOM_ANTHROPIC_BUDGET_EXPIRES": "2026-10-21"}
        got = limits.detect("anthropic", home=home, env=env, now=NOW)
        self.assertAlmostEqual(got["remaining"], 0.85)
        self.assertEqual(got["unit"], "usd")
        self.assertIsNotNone(got["expires_at"])

    def test_anthropic_without_budget_is_unknown(self):
        got = limits.detect("anthropic", home=self._tmp(), env={}, now=NOW)
        self.assertIsNone(got["remaining"])
        self.assertIn("LOOM_ANTHROPIC_BUDGET_USD", got["source"])

    def test_opencode_go_probe_hold(self):
        row = {"at": NOW, "limit_name": "MonthlyLimit", "retry_after": NOW + 7200}
        home = make_home(self._tmp(),
                         {"state/usage/opencode-go.provider-limit.json": json.dumps(row)})
        got = limits.detect("opencode-go", home=home, env={}, now=NOW)
        self.assertEqual(got["remaining"], 0.0)
        self.assertEqual(got["reset_at"], NOW + 7200)

    def _tmp(self):
        import tempfile
        d = tempfile.mkdtemp(prefix="limits-test-")
        self.addCleanup(__import__("shutil").rmtree, d, True)
        return d


class HeartbeatPayloadTests(unittest.TestCase):
    def test_limits_cli_prints_payload_json(self):
        import io
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            rc = limits.main(["muse"])
        self.assertEqual(rc, 0)
        got = json.loads(buf.getvalue())
        self.assertEqual(tuple(sorted(got)), tuple(sorted(FIELDS)))

    def test_limits_cli_needs_a_lane(self):
        with self.assertRaises(SystemExit):
            limits.main([])


class RoutingTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.root = Path(tempfile.mkdtemp(prefix="routing-test-"))
        self.addCleanup(__import__("shutil").rmtree, str(self.root), True)
        self._state = dispatcher.STATE
        dispatcher.STATE = self.root / "state"

    def tearDown(self):
        dispatcher.STATE = self._state

    def write_beat(self, lane, available=True, limits_payload=None, age=10):
        usage = dispatcher.STATE / "usage"
        usage.mkdir(parents=True, exist_ok=True)
        beat = {"lane": lane, "at": time.time() - age,
                "available": available, "reason": ""}
        if limits_payload is not None:
            beat["limits"] = limits_payload
        (usage / (lane + ".json")).write_text(json.dumps(beat))

    def test_prefers_expiring_credits_first(self):
        self.write_beat("anthropic", limits_payload=payload(
            remaining=0.8, expires_at=time.time() + 3600, unit="usd"))
        self.write_beat("claude", limits_payload=payload(remaining=0.95))
        self.write_beat("codex", limits_payload=payload(remaining=0.95))
        self.assertEqual(dispatcher.pick_lane("hard", {}), "anthropic")

    def test_soonest_expiry_wins_among_expiring(self):
        now = time.time()
        self.write_beat("anthropic", limits_payload=payload(
            remaining=0.8, expires_at=now + 7200, unit="usd"))
        self.write_beat("claude", limits_payload=payload(
            remaining=0.8, expires_at=now + 3600, unit="usd"))
        self.assertEqual(dispatcher.pick_lane("hard", {}), "claude")

    def test_then_by_remaining_capacity(self):
        self.write_beat("claude", limits_payload=payload(remaining=0.2))
        self.write_beat("codex", limits_payload=payload(remaining=0.7))
        self.assertEqual(dispatcher.pick_lane("hard", {}), "codex")

    def test_legacy_beats_without_limits_keep_order(self):
        self.write_beat("anthropic")
        self.write_beat("claude")
        self.write_beat("codex")
        self.assertEqual(dispatcher.pick_lane("hard", {}), "anthropic")
        self.assertEqual(dispatcher.pick_lane("easy", {}), "anthropic")

    def test_empty_expiring_lane_not_preferred(self):
        self.write_beat("anthropic", limits_payload=payload(
            remaining=0.0, expires_at=time.time() + 3600, unit="usd"))
        self.write_beat("codex", limits_payload=payload(remaining=0.6))
        self.assertEqual(dispatcher.pick_lane("hard", {}), "codex")

    def test_stale_and_unavailable_still_skipped(self):
        self.write_beat("anthropic", limits_payload=payload(
            remaining=0.9, expires_at=time.time() + 3600), age=99999)
        self.write_beat("claude", available=False)
        self.write_beat("codex", limits_payload=payload(remaining=0.5))
        self.assertEqual(dispatcher.pick_lane("hard", {}), "codex")


if __name__ == "__main__":
    unittest.main()
