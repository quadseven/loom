"""Dashboard usage upload and rendering behavior."""
import io
import json
import os
import sys
import unittest
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard.server import _dispatch_get, _handler_for
from dashboard.usage import Store
from dashboard.views import render_usage_row


class UsageDashboardTests(unittest.TestCase):
    def test_authenticated_upload_and_sorted_usage_page(self):
        class State:
            pass

        state = State()
        handler_type = _handler_for(state)
        readings = [
            {"provider": "codex", "lanes": ["codex"], "window": "weekly", "used_pct": 65,
             "remaining": None, "unit": "percent", "resets_at": "2026-10-14T15:00:00Z",
             "source": "session fixture", "as_of": "2026-10-14T12:00:00Z", "note": ""},
            {"provider": "anthropic", "lanes": ["anthropic"], "window": "credit", "used_pct": 90,
             "remaining": 2, "unit": "USD", "resets_at": "2026-10-14T14:00:00Z",
             "source": "budget fixture", "as_of": "2026-10-14T12:00:00Z", "note": ""},
            {"provider": "spark", "lanes": ["spark"], "window": "all", "used_pct": None,
             "remaining": None, "unit": None, "resets_at": None, "source": "not exposed",
             "as_of": "2026-10-14T12:00:00Z", "note": "adapter unavailable"},
        ]
        with patch.dict(os.environ, {"LOOM_UPLOAD_TOKEN": "YOUR_UPLOAD_TOKEN_HERE"}):
            def post(auth):
                handler = handler_type.__new__(handler_type)
                handler.path = "/api/usage"
                handler.headers = Message()
                raw = json.dumps(readings).encode()
                handler.headers["Content-Length"] = str(len(raw))
                if auth:
                    handler.headers["Authorization"] = auth
                handler.rfile = io.BytesIO(raw)
                result = []
                handler.send_json = lambda code, value: result.append((code, value))
                handler.do_POST()
                return result[0]
            self.assertEqual(post("")[0], 401)
            self.assertEqual(post("Bearer YOUR_UPLOAD_TOKEN_HERE")[0], 201)

        class Handler:
            path = "/usage"
            def send_page(self, body, code=200):
                self.result = (code, body)
        handler = Handler()
        _dispatch_get(handler, state)
        body = handler.result[1]
        self.assertLess(body.index("anthropic"), body.index("codex"))
        self.assertLess(body.index("codex"), body.index("spark"))
        self.assertIn("90.0% used / 10.0% left", body)
        self.assertIn("Resets at 2026-10-14 14:00 UTC", body)
        self.assertIn("budget fixture", body)
        self.assertIn("Not exposed yet.", body)
        self.assertIn("adapter unavailable", body)

    def test_reading_older_than_two_hours_is_stale(self):
        row = {"provider": "codex", "window": "weekly", "lanes": [], "used_pct": 50,
               "as_of": "2026-10-14T09:00:00Z", "source": "fixture"}
        now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
        rendered = render_usage_row(row, now)
        self.assertIn("Stale", rendered)

    def test_lane_limit_reason_is_rendered_in_dashboard_timezone(self):
        from dashboard.views import render_lane_usage
        with patch.dict(os.environ, {"LOOM_DASHBOARD_TZ": "America/New_York"}):
            body = render_lane_usage([{"holds": {"claude": "limit: out of credit; unavailable until 2030-01-02T00:01:27Z"}}])
        self.assertIn("claude", body)
        self.assertIn("out of credit until 2030-01-01 19:01 EST", body)


if __name__ == "__main__":
    unittest.main()
