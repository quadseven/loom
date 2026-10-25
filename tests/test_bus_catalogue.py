"""Tests for model catalogue discovery, allowance tracking, and model choice."""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from typing import Any, List

from bus import catalogue

NOW = 1792324800.0


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FakeFetch:
    def __init__(self, body: Any):
        self.body = body
        self.requests: List[Any] = []

    def __call__(self, request: Any):
        self.requests.append(request)
        if isinstance(self.body, Exception):
            raise self.body
        raw = self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode("utf-8")
        return FakeResponse(raw)


class ModelCatalogueTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.home = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_endpoint_discovery_adds_new_model_without_code_changes(self):
        payload = {
            "object": "list",
            "data": [
                {"id": "beam-free", "object": "model"},
                {"id": "new-novel-model-free", "object": "model"},
                {"id": "deepseek-v4.1-flash", "object": "model"},
                {"id": "unapproved-paid-model", "object": "model"},
            ],
        }
        fetch = FakeFetch(payload)
        models = catalogue.discover_models(
            url="http://192.0.2.1/inference/go/v1/models",
            allowlist={"deepseek-v4.1-flash"},
            fetch=fetch,
        )
        self.assertEqual(len(fetch.requests), 1)
        self.assertEqual(fetch.requests[0].full_url, "http://192.0.2.1/inference/go/v1/models")

        # New model with -free suffix appears without code changes
        self.assertIn("opencode-go/new-novel-model-free", models)
        entry = models["opencode-go/new-novel-model-free"]
        self.assertEqual(entry.id, "opencode-go/new-novel-model-free")
        self.assertEqual(entry.raw_id, "new-novel-model-free")
        self.assertEqual(entry.provider, "opencode-go")
        self.assertEqual(entry.api_shape, "openai-compatible")
        self.assertTrue(entry.is_free)
        self.assertEqual(entry.tier, "free")

        # Allowlisted model is included
        self.assertIn("opencode-go/deepseek-v4.1-flash", models)
        flash_entry = models["opencode-go/deepseek-v4.1-flash"]
        self.assertFalse(flash_entry.is_free)
        self.assertEqual(flash_entry.tier, "flash")

        # Unapproved paid model without -free suffix is excluded
        self.assertNotIn("opencode-go/unapproved-paid-model", models)

    def test_tier_priority_order_hard_strong(self):
        cat = {
            "opencode-go/kimi-k3": catalogue.ModelEntry(
                id="opencode-go/kimi-k3",
                provider="opencode-go",
                raw_id="kimi-k3",
                api_shape="openai-compatible",
                is_free=False,
                tier="strong",
            ),
            "opencode-go/deepseek-v4.1-flash": catalogue.ModelEntry(
                id="opencode-go/deepseek-v4.1-flash",
                provider="opencode-go",
                raw_id="deepseek-v4.1-flash",
                api_shape="openai-compatible",
                is_free=False,
                tier="flash",
            ),
            "opencode-go/beam-free": catalogue.ModelEntry(
                id="opencode-go/beam-free",
                provider="opencode-go",
                raw_id="beam-free",
                api_shape="openai-compatible",
                is_free=True,
                tier="free",
            ),
        }
        tracker = catalogue.AllowanceTracker()

        # Priority 1 for hard/strong: strong models
        choice1 = catalogue.select_model("hard", cat, tracker, now=NOW)
        self.assertIsNotNone(choice1)
        self.assertEqual(choice1.id, "opencode-go/kimi-k3")

        # When strong model is limited, fallback to flash models
        tracker.record_limit("opencode-go", "opencode-go/kimi-k3", "5-hour", 18000, now=NOW)
        choice2 = catalogue.select_model("hard", cat, tracker, now=NOW)
        self.assertIsNotNone(choice2)
        self.assertEqual(choice2.id, "opencode-go/deepseek-v4.1-flash")

        # When flash model is also limited, fallback to free models
        tracker.record_limit("opencode-go", "opencode-go/deepseek-v4.1-flash", "5-hour", 18000, now=NOW)
        choice3 = catalogue.select_model("hard", cat, tracker, now=NOW)
        self.assertIsNotNone(choice3)
        self.assertEqual(choice3.id, "opencode-go/beam-free")

    def test_tier_priority_order_flash_easy_medium(self):
        cat = {
            "opencode-go/kimi-k3": catalogue.ModelEntry(
                id="opencode-go/kimi-k3",
                provider="opencode-go",
                raw_id="kimi-k3",
                api_shape="openai-compatible",
                is_free=False,
                tier="strong",
            ),
            "opencode-go/mimo-v2.6-flash": catalogue.ModelEntry(
                id="opencode-go/mimo-v2.6-flash",
                provider="opencode-go",
                raw_id="mimo-v2.6-flash",
                api_shape="openai-compatible",
                is_free=False,
                tier="flash",
            ),
            "opencode-go/beam-free": catalogue.ModelEntry(
                id="opencode-go/beam-free",
                provider="opencode-go",
                raw_id="beam-free",
                api_shape="openai-compatible",
                is_free=True,
                tier="free",
            ),
        }
        tracker = catalogue.AllowanceTracker()

        # Easy and medium tiers pick high-allowance Flash models first
        for tier in ("easy", "medium", "flash"):
            choice = catalogue.select_model(tier, cat, tracker, now=NOW)
            self.assertIsNotNone(choice)
            self.assertEqual(choice.id, "opencode-go/mimo-v2.6-flash")

        # When flash model is limited, fallback to free model
        tracker.record_limit("opencode-go", "opencode-go/mimo-v2.6-flash", "5-hour", 18000, now=NOW)
        for tier in ("easy", "medium", "flash"):
            choice = catalogue.select_model(tier, cat, tracker, now=NOW)
            self.assertIsNotNone(choice)
            self.assertEqual(choice.id, "opencode-go/beam-free")

    def test_tier_priority_order_free_fallback(self):
        cat = {
            "opencode-go/mimo-v2.6-flash": catalogue.ModelEntry(
                id="opencode-go/mimo-v2.6-flash",
                provider="opencode-go",
                raw_id="mimo-v2.6-flash",
                api_shape="openai-compatible",
                is_free=False,
                tier="flash",
            ),
            "opencode-go/beam-free": catalogue.ModelEntry(
                id="opencode-go/beam-free",
                provider="opencode-go",
                raw_id="beam-free",
                api_shape="openai-compatible",
                is_free=True,
                tier="free",
            ),
        }
        tracker = catalogue.AllowanceTracker()
        choice = catalogue.select_model("free", cat, tracker, now=NOW)
        self.assertIsNotNone(choice)
        self.assertEqual(choice.id, "opencode-go/beam-free")

        choice_fb = catalogue.select_model("fallback", cat, tracker, now=NOW)
        self.assertIsNotNone(choice_fb)
        self.assertEqual(choice_fb.id, "opencode-go/beam-free")

    def test_brief_model_override_takes_precedence(self):
        cat = {
            "opencode-go/mimo-v2.6-flash": catalogue.ModelEntry(
                id="opencode-go/mimo-v2.6-flash",
                provider="opencode-go",
                raw_id="mimo-v2.6-flash",
                api_shape="openai-compatible",
                is_free=False,
                tier="flash",
            ),
            "opencode-go/beam-free": catalogue.ModelEntry(
                id="opencode-go/beam-free",
                provider="opencode-go",
                raw_id="beam-free",
                api_shape="openai-compatible",
                is_free=True,
                tier="free",
            ),
        }
        tracker = catalogue.AllowanceTracker()
        # Even on hard tier, Model: line wins
        choice = catalogue.select_model("hard", cat, tracker, now=NOW, pinned_model="opencode-go/beam-free")
        self.assertIsNotNone(choice)
        self.assertEqual(choice.id, "opencode-go/beam-free")

    def test_allowance_bucket_limit_tracking_and_expiry(self):
        tracker = catalogue.AllowanceTracker()
        self.assertFalse(tracker.is_limited("opencode-go", "opencode-go/mimo-v2.6-flash", now=NOW))

        # 429 GoUsageLimitError for 5-hour window
        tracker.record_limit("opencode-go", "opencode-go/mimo-v2.6-flash", "5-hour", 18000, now=NOW)
        self.assertTrue(tracker.is_limited("opencode-go", "opencode-go/mimo-v2.6-flash", now=NOW))
        self.assertTrue(tracker.is_limited("opencode-go", "opencode-go/mimo-v2.6-flash", now=NOW + 17999))

        bucket = tracker.get_bucket("opencode-go", "opencode-go/mimo-v2.6-flash")
        self.assertIsNotNone(bucket)
        self.assertEqual(bucket.window, "5-hour")
        self.assertEqual(bucket.resets_at, NOW + 18000)

        # After reset time, model comes back
        self.assertFalse(tracker.is_limited("opencode-go", "opencode-go/mimo-v2.6-flash", now=NOW + 18001))

    def test_local_call_counting(self):
        tracker = catalogue.AllowanceTracker()
        tracker.record_call("opencode-go", "opencode-go/deepseek-v4.1-flash")
        tracker.record_call("opencode-go", "opencode-go/deepseek-v4.1-flash")
        tracker.record_call("opencode-go", "opencode-go/beam-free")

        b1 = tracker.get_bucket("opencode-go", "opencode-go/deepseek-v4.1-flash")
        self.assertIsNotNone(b1)
        self.assertEqual(b1.calls_count, 2)

        b2 = tracker.get_bucket("opencode-go", "opencode-go/beam-free")
        self.assertIsNotNone(b2)
        self.assertEqual(b2.calls_count, 1)

    def test_save_and_load_persistence(self):
        path = self.home / "state" / "usage" / "allowance.json"
        tracker1 = catalogue.AllowanceTracker()
        tracker1.record_limit("opencode-go", "opencode-go/mimo-v2.6-flash", "5-hour", 18000, now=NOW)
        tracker1.record_call("opencode-go", "opencode-go/mimo-v2.6-flash")
        tracker1.save(path)

        tracker2 = catalogue.AllowanceTracker.load(path)
        self.assertTrue(tracker2.is_limited("opencode-go", "opencode-go/mimo-v2.6-flash", now=NOW))
        b = tracker2.get_bucket("opencode-go", "opencode-go/mimo-v2.6-flash")
        self.assertIsNotNone(b)
        self.assertEqual(b.calls_count, 1)
        self.assertEqual(b.window, "5-hour")

    def test_candidates_order_for_tiers(self):
        cat = {
            "opencode-go/kimi-k3": catalogue.ModelEntry(
                id="opencode-go/kimi-k3",
                provider="opencode-go",
                raw_id="kimi-k3",
                api_shape="openai-compatible",
                is_free=False,
                tier="strong",
            ),
            "opencode-go/deepseek-v4.1-flash": catalogue.ModelEntry(
                id="opencode-go/deepseek-v4.1-flash",
                provider="opencode-go",
                raw_id="deepseek-v4.1-flash",
                api_shape="openai-compatible",
                is_free=False,
                tier="flash",
            ),
            "opencode-go/beam-free": catalogue.ModelEntry(
                id="opencode-go/beam-free",
                provider="opencode-go",
                raw_id="beam-free",
                api_shape="openai-compatible",
                is_free=True,
                tier="free",
            ),
        }
        hard_order = catalogue.tier_candidates("hard", cat)
        self.assertEqual(hard_order, [
            "opencode-go/kimi-k3",
            "opencode-go/deepseek-v4.1-flash",
            "opencode-go/beam-free",
        ])

        easy_order = catalogue.tier_candidates("easy", cat)
        self.assertEqual(easy_order, [
            "opencode-go/deepseek-v4.1-flash",
            "opencode-go/beam-free",
        ])

    def test_fetch_rejects_unpermitted_scheme(self):
        models = catalogue.fetch_remote_models("file:///etc/passwd")
        self.assertEqual(models, [])

    def test_preferred_model_selected_until_limited(self):
        cat = {
            "opencode-go/mimo-v2.6-flash": catalogue.ModelEntry(
                id="opencode-go/mimo-v2.6-flash",
                provider="opencode-go",
                raw_id="mimo-v2.6-flash",
                api_shape="openai-compatible",
                is_free=False,
                tier="flash",
            ),
            "opencode-go/deepseek-v4.1-flash": catalogue.ModelEntry(
                id="opencode-go/deepseek-v4.1-flash",
                provider="opencode-go",
                raw_id="deepseek-v4.1-flash",
                api_shape="openai-compatible",
                is_free=False,
                tier="flash",
            ),
        }
        tracker = catalogue.AllowanceTracker()
        choice = catalogue.select_model("medium", cat, tracker, now=NOW,
                                        preferred_model="opencode-go/mimo-v2.6-flash")
        self.assertIsNotNone(choice)
        self.assertEqual(choice.id, "opencode-go/mimo-v2.6-flash")

        # When preferred model is limited, fallback to next tier candidate
        tracker.record_limit("opencode-go", "opencode-go/mimo-v2.6-flash", "5-hour", 18000, now=NOW)
        choice2 = catalogue.select_model("medium", cat, tracker, now=NOW,
                                         preferred_model="opencode-go/mimo-v2.6-flash")
        self.assertIsNotNone(choice2)
        self.assertEqual(choice2.id, "opencode-go/deepseek-v4.1-flash")

    def test_preferred_contributor_model_excluded_when_public_only(self):
        cat = {
            "opencode-go/muse-spark-1.3-contributor": catalogue.ModelEntry(
                id="opencode-go/muse-spark-1.3-contributor",
                provider="opencode-go",
                raw_id="muse-spark-1.3-contributor",
                api_shape="openai-compatible",
                is_free=False,
                tier="flash",
            ),
            "opencode-go/deepseek-v4.1-flash": catalogue.ModelEntry(
                id="opencode-go/deepseek-v4.1-flash",
                provider="opencode-go",
                raw_id="deepseek-v4.1-flash",
                api_shape="openai-compatible",
                is_free=False,
                tier="flash",
            ),
        }
        tracker = catalogue.AllowanceTracker()
        # When public_only=True, preferred contributor model is excluded
        choice = catalogue.select_model("medium", cat, tracker, now=NOW,
                                        public_only=True,
                                        preferred_model="opencode-go/muse-spark-1.3-contributor")
        self.assertIsNotNone(choice)
        self.assertEqual(choice.id, "opencode-go/deepseek-v4.1-flash")

        # When public_only=False, preferred contributor model is allowed
        choice2 = catalogue.select_model("medium", cat, tracker, now=NOW,
                                         public_only=False,
                                         preferred_model="opencode-go/muse-spark-1.3-contributor")
        self.assertIsNotNone(choice2)
        self.assertEqual(choice2.id, "opencode-go/muse-spark-1.3-contributor")

    def test_cli_select_model_command(self):
        cat = {
            "opencode-go/mimo-v2.6-flash": catalogue.ModelEntry(
                id="opencode-go/mimo-v2.6-flash",
                provider="opencode-go",
                raw_id="mimo-v2.6-flash",
                api_shape="openai-compatible",
                is_free=False,
                tier="flash",
            ),
        }
        catalogue.save_catalogue(cat, catalogue.catalogue_file(self.home))
        env_backup = os.environ.get("LOOM_HOME")
        try:
            os.environ["LOOM_HOME"] = str(self.home)
            stdout = io.StringIO()
            with mock.patch("sys.stdout", stdout):
                code = catalogue.main(["select-model", "medium"])
            self.assertEqual(code, 0)
            self.assertEqual(stdout.getvalue().strip(), "opencode-go/mimo-v2.6-flash")
        finally:
            if env_backup is not None:
                os.environ["LOOM_HOME"] = env_backup
            else:
                os.environ.pop("LOOM_HOME", None)


if __name__ == "__main__":
    unittest.main()
