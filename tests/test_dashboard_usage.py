"""Dashboard usage upload and rendering behavior."""
import io
import json
import os
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard import usage
from dashboard.server import _dispatch_get, _handler_for
from dashboard.usage import Store

NOW = datetime(2026, 10, 18, 14, 30, tzinfo=timezone.utc)


def reading(provider, lanes, window, used=None, resets_at=None, age=timedelta(minutes=8),
            source="fixture", note="", **extra):
    row = {"provider": provider, "lanes": lanes, "window": window, "used_pct": used,
           "remaining": None, "unit": "percent" if used is not None else None,
           "resets_at": resets_at, "source": source, "as_of": (NOW - age).isoformat(),
           "note": note, "holds": {}}
    row.update(extra)
    return row


def section(body, heading_id):
    """The markup of one section of the Usage view, by its heading id."""
    start = body.index('aria-labelledby="%s"' % heading_id)
    return body[start:body.index("</section>", start)]


def provider_row(body, name):
    """The markup of one provider row, by its name."""
    match = re.search(r'<li class="u[pq]"><div class="u[pq]-head"><h3 class="up-name">%s</h3>.*?</div></li>'
                      % re.escape(name), body, re.S)
    if not match:
        raise AssertionError("no provider row for %s" % name)
    return match.group(0)


class UsageDashboardTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(os.environ, {"LOOM_DASHBOARD_TZ": "UTC"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_authenticated_upload_renders_the_usage_page_worst_first(self):
        class State:
            pass

        state = State()
        handler_type = _handler_for(state)
        now = datetime.now(timezone.utc)
        stamp = now.isoformat()
        readings = [
            {"provider": "codex", "lanes": ["codex"], "window": "weekly", "used_pct": 65,
             "remaining": None, "unit": "percent", "resets_at": (now + timedelta(days=3)).isoformat(),
             "source": "session fixture", "as_of": stamp, "note": ""},
            {"provider": "anthropic", "lanes": ["anthropic"], "window": "credit", "used_pct": 90,
             "remaining": 2, "unit": "USD", "resets_at": (now + timedelta(hours=2)).isoformat(),
             "source": "budget fixture", "as_of": stamp, "note": ""},
            {"provider": "spark", "lanes": ["spark"], "window": "all", "used_pct": None,
             "remaining": None, "unit": None, "resets_at": None, "source": "not exposed",
             "as_of": stamp, "note": "adapter unavailable"},
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
        code, body = handler.result
        self.assertEqual(code, 200)
        limits = section(body, "usage-providers")
        # The fullest meter comes first; a provider with no figure is in its own section.
        self.assertLess(limits.index(">anthropic<"), limits.index(">codex<"))
        self.assertNotIn(">spark<", limits)
        self.assertIn(">spark<", section(body, "usage-quiet"))
        anthropic = provider_row(body, "anthropic")
        self.assertIn("<strong>90%</strong> used", anthropic)
        self.assertIn("<strong>10%</strong> left", anthropic)
        self.assertIn("(USD 2.00 left)", anthropic)
        self.assertIn("near limit", anthropic)
        self.assertIn("Expires ", anthropic)
        self.assertIn("budget fixture", anthropic)
        self.assertIn("anthropic credit is 90% used.", section(body, "usage-asks"))
        self.assertIn("Adapter unavailable.", provider_row(body, "spark"))
        self.assertIn("Snapshot read", body)

    def test_the_page_answers_used_left_reset_pace_and_lanes_from_fixture_data(self):
        rows = [
            # 18 hours into a 7-day window at 33%: the pace runs it out early.
            reading("codex", ["codex", "codex-2"], "weekly", 33.0,
                    (NOW + timedelta(days=6, hours=6)).isoformat(), source="codex session logs",
                    window_minutes=10080),
            # 1h 14m into a 5-hour window at 10%: it lasts to the reset.
            reading("codex", ["codex", "codex-2"], "5-hour", 10.0,
                    (NOW + timedelta(hours=3, minutes=38)).isoformat(), source="codex session logs"),
        ]
        prs = [{"body": "Loom ticket quadseven.loom-%d, lane codex, model gpt-6-sol." % n,
                "created_at": (NOW - timedelta(days=n)).isoformat()} for n in (1, 2, 30)]
        body = usage.render(usage.build(rows, NOW, prs=prs))
        codex = provider_row(body, "codex")
        self.assertIn("2 of 2 lanes free", codex)
        self.assertIn('aria-label="33% used, 67% left"', codex)
        self.assertIn("Resets 2026-10-24 20:30 UTC (in 6d 6h).", codex)
        self.assertIn("runs out early", codex)
        self.assertRegex(codex, r"Pace 1\.\d+%/h since the window opened: runs out")
        self.assertIn("lasts to the reset, about", codex)
        self.assertIn('class="um-proj"', codex)
        # The lanes that spend it, their model, and their pull requests this week.
        self.assertIn("codex, codex-2", codex)
        self.assertIn("gpt-6-sol / 2 PRs in 7d", codex)
        self.assertIn("codex weekly: at this pace it runs out in ", section(body, "usage-asks"))
        self.assertIn("Tightest meter: codex weekly at 33% used.", body)

    def test_unknown_usage_shows_unknown_and_no_figure(self):
        rows = [reading("claude", ["claude"], "all", source="not exposed", note="not exposed yet"),
                reading("anthropic", ["anthropic"], "credit", source="bus/spend.py",
                        note="budget is not configured")]
        rows[0]["holds"] = {"anthropic": "limit: out of credit; unavailable until 2026-10-19T00:11:00Z"}
        body = usage.render(usage.build(rows, NOW))
        anthropic = provider_row(body, "anthropic")
        self.assertIn('<div class="um-bar um-unknown" aria-hidden="true"></div>', anthropic)
        self.assertIn('<p class="um-fig">Unknown</p>', anthropic)
        self.assertIn("Budget is not configured.", anthropic)
        self.assertNotIn("% used", anthropic)
        self.assertIn("out of credit until 2026-10-19 00:11 UTC (in 9h 41m)", anthropic)
        self.assertIn(">held<", anthropic)
        claude = provider_row(body, "claude")
        self.assertIn("Not exposed yet.", claude)
        self.assertNotIn("Source: not exposed", claude)
        asks = section(body, "usage-asks")
        self.assertIn("anthropic: 1 lane out of credit until", asks)
        self.assertIn("the spend budget is not configured", asks)
        self.assertIn("1 of 2 lanes can take work: 1 held at a limit.", body)

    def test_a_snapshot_older_than_two_hours_is_marked_stale_everywhere(self):
        rows = [reading("codex", ["codex"], "weekly", 50, (NOW + timedelta(days=2)).isoformat(),
                        age=timedelta(hours=19)),
                reading("zen", ["zen"], "all", age=timedelta(hours=19), note="not exposed yet")]
        body = usage.render(usage.build(rows, NOW))
        self.assertIn("<strong>Stale snapshot.</strong>", body)
        self.assertIn("2026-10-17 19:30 UTC (19h 0m ago)", body)
        self.assertNotIn("Snapshot read", body)
        codex = provider_row(body, "codex")
        self.assertIn(">stale<", codex)
        self.assertIn("um-bar um-stale", codex)
        self.assertIn('aria-label="50% used, 50% left, stale"', codex)
        self.assertIn("Stale: read 19h 0m ago.", codex)
        self.assertNotIn("Pace", codex)
        asks = section(body, "usage-asks")
        self.assertIn("The usage snapshot is 19h 0m old", asks)
        # One ask for the whole snapshot, not one per reading.
        self.assertEqual(asks.count("<li"), 1)
        # A stale figure never names the tightest meter.
        self.assertNotIn("Tightest meter", body)
        # zen has no figure, so staleness does not move it out of the quiet list.
        self.assertIn(">zen<", section(body, "usage-quiet"))

    def test_one_stale_reading_in_a_fresh_snapshot_is_flagged_on_its_own(self):
        rows = [reading("codex", ["codex"], "weekly", 40, (NOW + timedelta(days=2)).isoformat(),
                        observed_at=(NOW - timedelta(hours=5)).isoformat()),
                reading("zen", ["zen"], "all")]
        body = usage.render(usage.build(rows, NOW))
        self.assertIn("Snapshot read 2026-10-18 14:22 UTC (8m ago)", body)
        self.assertIn("codex weekly: reading is stale (read 5h 0m ago).", section(body, "usage-asks"))
        self.assertIn("codex last reported 2026-10-18 09:30 UTC (5h 0m ago)", provider_row(body, "codex"))

    def test_a_window_that_reset_after_its_reading_is_history_not_a_figure(self):
        rows = [reading("codex", ["codex"], "5-hour", 97, (NOW - timedelta(minutes=20)).isoformat(),
                        age=timedelta(minutes=50))]
        body = usage.render(usage.build(rows, NOW))
        codex = provider_row(body, "codex")
        self.assertIn("Stale: the window reset 20m ago, after this reading.", codex)
        self.assertNotIn("near limit", codex)
        self.assertNotIn("is 97% used", body)

    def test_lanes_group_by_state_and_holds_read_in_the_dashboard_timezone(self):
        rows = [reading("spark", [lane], "thermal hold", source="state/usage/%s.limit" % lane,
                        note="no active hold") for lane in ("spark", "spark-2", "spark-3")]
        rows[0]["holds"] = {"spark-2": "the spark endpoint did not answer (curl exit 28)",
                            "spark-3": "limit: provider limit reached; lane waits until 2026-10-18T14:00:00Z"}
        with patch.dict(os.environ, {"LOOM_DASHBOARD_TZ": "America/New_York"}):
            body = usage.render(usage.build(rows, NOW))
        spark = provider_row(body, "spark")
        self.assertIn("none active on 3 lanes", spark)
        self.assertEqual(spark.count("Thermal hold"), 1)
        self.assertIn("partly down", spark)
        self.assertIn("ul ul-down", spark)
        self.assertIn("down: the spark endpoint did not answer (curl exit 28)", spark)
        # spark-3's hold ended 30m ago: it no longer holds the lane.
        self.assertIn("free: its hold (provider limit reached) ended 2026-10-18 10:00 EDT (30m ago)", spark)
        self.assertIn("state/usage/spark.limit, state/usage/spark-2.limit, state/usage/spark-3.limit", spark)

    def test_a_hold_that_has_ended_stops_counting_the_lane_down(self):
        # The live page showed a spark lane "down" from a hold that ended over
        # an hour before, because the lane had written no heartbeat since.
        rows = [reading("spark", ["spark", "spark-2"], "all", note="not exposed")]
        rows[0]["holds"] = {"spark": "limit: the spark endpoint answered 503; unavailable until 2026-10-18T13:25:00Z",
                            "spark-2": "limit: the spark endpoint answered 503; unavailable until 2026-10-18T15:00:00Z"}
        data = usage.build(rows, NOW)
        spark = data["providers"][0]
        self.assertEqual((data["free"], data["down"], data["held"]), (1, 1, 0))
        self.assertEqual(spark["status"], "partly down")
        kinds = {lane: g["kind"] for g in spark["groups"] for lane in g["lanes"]}
        self.assertEqual(kinds, {"spark": "free", "spark-2": "down"})
        asks = " ".join(text for _, text in data["asks"])
        self.assertIn("spark: 1 lane down: the spark endpoint answered 503 until 2026-10-18 15:00 UTC (in 30m).", asks)
        self.assertNotIn("ended", asks)
        body = usage.render(data)
        self.assertIn("free: its hold (the spark endpoint answered 503) ended 2026-10-18 13:25 UTC (1h 5m ago)",
                      provider_row(body, "spark"))

    def test_every_lane_whose_hold_ended_is_free_not_down(self):
        rows = [reading("spark", ["spark"], "all", note="not exposed")]
        rows[0]["holds"] = {"spark": "limit: the spark endpoint answered 503; unavailable until 2026-10-18T13:25:00Z"}
        data = usage.build(rows, NOW)
        self.assertEqual(data["providers"][0]["status"], "no meter")
        self.assertEqual((data["free"], data["down"]), (1, 0))
        self.assertIn("1 of 1 lane can take work.", usage.render(data))

    def test_no_snapshot_says_what_is_missing_and_when_it_fills(self):
        body = usage.render(usage.build([], NOW))
        self.assertIn('class="empty-state"', body)
        self.assertIn("No usage snapshot has arrived since the dashboard started", body)
        self.assertIn("after its next pass", body)

    def test_nothing_to_ask_says_so(self):
        rows = [reading("codex", ["codex"], "weekly", 10, (NOW + timedelta(days=1)).isoformat())]
        body = usage.render(usage.build(rows, NOW))
        self.assertIn("Nothing: no lane is held", section(body, "usage-asks"))
        self.assertIn(">ok<", provider_row(body, "codex"))

    def test_upload_keeps_valid_observed_time_and_window_length_and_drops_bad_ones(self):
        good = reading("codex", ["codex"], "weekly", 10, observed_at="2026-10-18T14:00:00Z",
                       window_minutes=10080)
        bad = reading("codex", ["codex"], "5-hour", 10, observed_at="yesterday", window_minutes=True)
        rows = usage.validate([good, bad])
        self.assertEqual((rows[0]["observed_at"], rows[0]["window_minutes"]), ("2026-10-18T14:00:00Z", 10080))
        self.assertEqual((rows[1]["observed_at"], rows[1]["window_minutes"]), (None, None))

    def test_store_keeps_the_newest_upload_per_provider(self):
        store = Store()
        store.upload([reading("codex", ["codex"], "weekly", 10), reading("zen", ["zen"], "all")])
        store.upload([reading("codex", ["codex"], "weekly", 20)])
        self.assertEqual(store.providers["codex"][0]["used_pct"], 20)
        self.assertIn("zen", store.providers)


if __name__ == "__main__":
    unittest.main()
