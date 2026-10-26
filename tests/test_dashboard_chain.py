"""Fixture-backed tests for the /chain view: the command chain from the
operator down to the review bot, and the audit of what Loom did alone. The
station and audit layers come from the station's POST /api/chain snapshot."""
import io
import json
import os
import shutil
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest import mock

from bus import shadow, station
from dashboard import chain_view, server
from dashboard.shell import VIEWS

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "YOUR_UPLOAD_TOKEN_HERE"
NOW = 1_900_000_000.0
JOURNAL = {"created_at": "2030-01-08T11:00:00Z", "body": "\n".join([
    "<!-- loom:journal v1 -->",
    "Tick 2030-01-08T11:00:00Z, brain-a",
    "- Briefed: example-owner/sample#42 to codex",  # leak-guard-allow
    "- Next: merge example-owner/sample#41 once its review is clean.",  # leak-guard-allow
    "- Cost: 40 requests, 6 minutes"])}
PAYLOAD = {"tickets": {"example-owner/sample": [
    {"repo": "example-owner/sample", "number": 1, "state": "running", "open": True},
    {"repo": "example-owner/sample", "number": 2, "state": "queued", "open": True},
    {"repo": "example-owner/sample", "number": 3, "state": "queued", "open": True}]},
    "stale": False, "brain_step": None, "brain_step_error": "",
    "improving": {"journals": [JOURNAL]},
    "lanes": [{"lane": "codex", "available": False}, {"lane": "agy", "available": True}]}
SNAPSHOT = {"at": NOW - 300, "shadow_on": True,
            "repos": [{"repo": "example-owner/sample", "shadow": True},
                      {"repo": "example-owner/other", "shadow": False}],
            "held": [{"action": "merge", "repo": "example-owner/sample", "number": 40,
                      "at": "2030-01-08T09:00:00Z", "source": "bus/factory.py:10"},
                     {"action": "close", "repo": "example-owner/sample", "number": 41,
                      "at": "2030-01-08T10:00:00Z", "source": "bus/station.py:20"}],
            "acted": [{"action": "merge", "repo": "example-owner/other", "number": 7,
                       "at": "2030-01-08T08:00:00Z", "source": "bus/factory.py:10"},
                      {"action": "close", "repo": "example-owner/other", "number": 8,
                       "at": "2030-01-08T08:30:00Z", "source": "bus/station.py:20"}]}
HEADINGS = ("Operator", "Mastermind", "Brain", "Station", "Lanes and factory", "Review bot",
            "Audit: what Loom did on its own in live repos")
# Split so that this file carries neither name either.
NAMES = ("Ev" + "an", "Ca" + "it")


def stored(snapshot=SNAPSHOT, received=NOW - 120):
    return chain_view.chain_snapshot(snapshot, received)


def build(chain=None, payload=PAYLOAD, env=None):
    return chain_view.build_chain(payload, chain, NOW, env or {})


def section(body, heading, after):
    return body[body.index('<h2 class="label">%s</h2>' % heading):body.index('<h2 class="label">%s</h2>' % after)]


class ChainViewTest(unittest.TestCase):
    def test_every_layer_heading_renders_in_order(self):
        body = chain_view.render_chain(build(stored()))
        found = [body.index('<h2 class="label">%s</h2>' % heading) for heading in HEADINGS]
        self.assertEqual(found, sorted(found))

    def test_station_layer_shows_each_repo_as_the_station_sent_it_with_the_age(self):
        body = chain_view.render_chain(build(stored()))
        layer = section(body, "Station", "Lanes and factory")
        self.assertIn('<span class="machine-fact">example-owner/other</span> <strong>live</strong>', layer)
        self.assertIn('<span class="machine-fact">example-owner/sample</span> <strong>shadow</strong>', layer)
        self.assertIn("sent by the station 2 minutes ago", layer)

    def test_audit_renders_acted_and_held_rows_newest_first_with_links(self):
        body = chain_view.render_chain(build(stored()))
        audit = body[body.index("Audit: what Loom did"):]
        acted = audit[audit.index("Loom acted on its own"):audit.index("Held by shadow mode")]
        held = audit[audit.index("Held by shadow mode"):]
        self.assertLess(acted.index("example-owner/other#8"), acted.index("example-owner/other#7"))  # leak-guard-allow
        self.assertIn('href="https://github.com/example-owner/other/issues/7"', acted)
        self.assertIn("merge", acted)
        self.assertLess(held.index("example-owner/sample#41"), held.index("example-owner/sample#40"))  # leak-guard-allow
        self.assertIn('href="https://github.com/example-owner/sample/issues/40"', held)

    def test_no_snapshot_says_no_data_from_the_station_yet(self):
        body = chain_view.render_chain(build(None))
        self.assertEqual(section(body, "Station", "Lanes and factory").count("no data from the station yet"), 1)
        self.assertIn("no data from the station yet", body[body.index("Audit: what Loom did"):])
        self.assertNotIn("<strong>shadow</strong>", body)

    def test_missing_brain_data_says_no_data(self):
        payload = dict(PAYLOAD, improving={}, brain_step=None)
        self.assertIn("no data", section(chain_view.render_chain(build(payload=payload)), "Brain", "Station"))

    def test_lanes_layer_counts_running_queued_and_held(self):
        self.assertEqual(build()["lanes"], {"running": 1, "queued": 2, "held": 1})
        self.assertIn("<li>1 ticket running</li><li>2 tickets queued</li><li>1 lane held</li>",
                      chain_view.render_chain(build()))

    def test_a_malformed_snapshot_is_refused(self):
        for body in ({}, {"repos": "x"}, dict(SNAPSHOT, repos=[{"repo": "<b>x</b>", "shadow": True}]),
                     dict(SNAPSHOT, acted=[dict(SNAPSHOT["acted"][0], number="7")]),
                     dict(SNAPSHOT, held=[{"action": "merge"}] * 3), dict(SNAPSHOT, shadow_on="yes")):
            with self.assertRaises(ValueError, msg=body):
                chain_view.chain_snapshot(body, NOW)

    def test_no_output_writes_the_bot_handle_or_a_personal_name(self):
        from dashboard.shell import page
        for chain in (None, stored()):
            body = page("Command chain", "chain", chain_view.render_chain(build(chain)), status="live")
            self.assertIn("grug-tribe[bot]", body)
            self.assertNotIn("@" + "grug", body)
            for name in NAMES:
                self.assertNotIn(name, body)
        for rel in ("dashboard/chain_view.py", "tests/test_dashboard_chain.py", "docs/DASHBOARD.md"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertNotIn("@" + "grug", text, rel)
            for name in NAMES:
                self.assertNotIn(name, text, rel)


class ChainStationTest(unittest.TestCase):
    """The station sends the snapshot every CHAIN_EVERY seconds, from
    bus.shadow alone: no GitHub request."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        ledger = os.path.join(self.dir, "shadow", "ledger.jsonl")
        patch = mock.patch.dict(os.environ, {"LOOM_SHADOW": "1", "LOOM_SHADOW_LEDGER": ledger,
                                             "LOOM_SHADOW_REPOS": "example-owner/sample"})
        patch.start()
        self.addCleanup(patch.stop)
        shadow.record("merge", "example-owner/sample", 40, "pr", source="bus/factory.py:10")
        shadow.record_acted("close", "example-owner/other", 8, "issue", "bus/station.py:20")

    def station(self):
        st = station.Station.__new__(station.Station)
        st.state = os.path.join(self.dir, "state")
        os.makedirs(st.state)
        st.now = NOW
        st.s = mock.Mock(repos=["example-owner/sample", "example-owner/other"],
                         transcript_url="https://dash.example.com/api/transcripts", transcript_token_ssm="/x")
        st.gh = mock.Mock(side_effect=AssertionError("no GitHub request"))
        st._transcript_token = lambda: "token"
        self.sent = []
        st.upload = lambda url, token, body: self.sent.append((url, body))
        return st

    def test_the_snapshot_goes_out_on_schedule_with_its_shape(self):
        st = self.station()
        st.post_chain()
        url, body = self.sent[-1]
        self.assertTrue(url.endswith(station.CHAIN_PATH), url)
        self.assertEqual(body["at"], NOW)
        self.assertIs(body["shadow_on"], True)
        self.assertEqual(body["repos"], [{"repo": "example-owner/sample", "shadow": True},
                                         {"repo": "example-owner/other", "shadow": False}])
        self.assertEqual([(h["action"], h["repo"], h["number"]) for h in body["held"]],
                         [("merge", "example-owner/sample", 40)])
        self.assertEqual(set(body["held"][0]), {"action", "repo", "number", "at", "source"})
        self.assertEqual([(a["action"], a["repo"], a["number"]) for a in body["acted"]],
                         [("close", "example-owner/other", 8)])
        self.assertEqual(set(body["acted"][0]), {"action", "repo", "number", "at", "source"})
        chain_view.chain_snapshot(body, NOW)   # the dashboard takes what the station sends
        st.now += station.CHAIN_EVERY - 1
        st.post_chain()
        self.assertEqual(len(self.sent), 1)
        st.now += 1
        st.post_chain()
        self.assertEqual(len(self.sent), 2)
        st.gh.assert_not_called()

    def test_a_failed_send_is_logged_and_waits_for_the_next_run(self):
        st = self.station()
        st.upload = mock.Mock(side_effect=OSError("down"))
        with mock.patch("sys.stderr", new_callable=io.StringIO):
            st.post_chain()
        self.assertEqual(st.upload.call_count, 1)


class ChainRouteTest(unittest.TestCase):
    def state(self):
        class State:
            stale = False
            repos = ["example-owner/sample"]
            chain = None

            def response(self):
                return PAYLOAD
        return State()

    def get(self, state):
        class Handler:
            path = "/chain"

            def send_page(self, body, code=200):
                self.result = (code, body)
        handler = Handler()
        server._dispatch_get(handler, state)
        return handler.result

    def post(self, state, body, auth="Bearer " + TOKEN):
        handler_type = server._handler_for(state)
        handler = handler_type.__new__(handler_type)
        handler.path = "/api/chain"
        handler.headers = Message()
        raw = json.dumps(body).encode()
        handler.headers["Content-Length"] = str(len(raw))
        if auth:
            handler.headers["Authorization"] = auth
        handler.rfile = io.BytesIO(raw)
        result = []
        handler.send_json = lambda code, value: result.append((code, value))
        with mock.patch.dict(os.environ, {"LOOM_UPLOAD_TOKEN": TOKEN}):
            handler.do_POST()
        return result[0]

    def test_chain_route_renders_and_the_nav_links_it_before_brain(self):
        code, body = self.get(self.state())
        self.assertEqual(code, 200)
        self.assertIn("no data from the station yet", body)
        self.assertIn('href="/chain" aria-current="page">Command chain</a>', body)
        keys = [key for key, _, _ in VIEWS]
        self.assertEqual(keys.index("chain") + 1, keys.index("brain"))

    def test_a_post_needs_the_token_and_its_snapshot_renders(self):
        state = self.state()
        self.assertEqual(self.post(state, SNAPSHOT, auth="")[0], 401)
        self.assertIsNone(state.chain)
        self.assertEqual(self.post(state, {"repos": "x"})[0], 400)
        self.assertEqual(self.post(state, SNAPSHOT), (201, {"repos": 2}))
        code, body = self.get(state)
        self.assertEqual(code, 200)
        self.assertIn('href="https://github.com/example-owner/other/issues/8"', body)
        self.assertIn('href="https://github.com/example-owner/sample/issues/41"', body)
        self.assertNotIn("no data from the station yet", body)


if __name__ == "__main__":
    unittest.main()
