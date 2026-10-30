"""The Fleet timeline (/fleet): every engine as a row over the last 24 hours."""
import unittest
from datetime import datetime, timedelta, timezone

from dashboard import fleet, server
from dashboard.shell import VIEWS

NOW = datetime(2030, 1, 8, 12, 0, tzinfo=timezone.utc)
START = NOW - timedelta(hours=24)
REPO = "example-owner/sample"
ISSUES = "https://api.github.com/repos/example-owner/sample/issues/%d"
STATION = {"login": "station-bot[bot]", "type": "Bot"}
BRAIN = {"login": "brain-bot[bot]", "type": "Bot"}
LANES = {"login": "lanes-bot[bot]", "type": "Bot"}
PERSON = {"login": "the-operator", "type": "User"}


def journal(at, holder, line):
    return {"id": 1, "created_at": at, "repo": REPO, "issue_number": 30, "user": BRAIN, "body": "\n".join([
        "<!-- loom:journal v1 -->", "Tick %s, %s" % (at, holder), "- Shipped: none", "- Briefed: %s" % line,
        "- Cost: 10 requests, 2 minutes"])}


def comment(at, body, user=STATION, number=41):
    return {"repo": REPO, "created_at": at, "body": body, "user": user, "issue_url": ISSUES % number,
            "html_url": "https://github.com/example-owner/sample/issues/%d#issuecomment-1" % number}


JOURNALS = [
    journal("2030-01-07T11:50:00Z", "muse", "nothing, before the window"),
    journal("2030-01-08T09:00:00Z", "muse", "example-owner/sample#41 to codex"),
    journal("2030-01-08T09:30:00Z", "muse", "<script>alert(1)</script> to claude"),
    journal("2030-01-08T11:35:00Z", "muse", "example-owner/sample#42 to codex"),
    journal("2030-01-08T07:00:00Z", "operator-session", "a tally"),
    # A holder that last ticked days ago is not in today's fleet.
    journal("2030-01-05T07:00:00Z", "old-holder", "a tick from three days ago"),
]
ROUND = ("<!-- loom:lane-round v1 -->\nLane rounds on this pull request, newest first.\n\n"
         "<!-- loom:round 1 -->\n### Round 1, 2030-01-08T11:00:00Z\n\n"
         "Lane codex (model-x) pushed 1 commit for: Acceptance criteria not ticked\n")
COMMENTS = [
    comment("2030-01-08T11:20:00Z", "Shipped: the station deployed revision abc123 at 2030-01-08 11:10:00Z, "
            "which carries pull request #77 (merge commit abc123)."),
    comment("2030-01-08T10:40:00Z", "Lane codex opened a pull request #77 for this ticket."),
    comment("2030-01-08T10:50:00Z", "Lane claude failed: [work] tests failed on <b>main</b>", number=42),
    comment("2030-01-08T02:05:00Z", "<!-- loom:report v1 -->\n3 merged, 1 failed since the last report.\n"
            "Going well: the merge queue drained.", user=BRAIN, number=30),
    comment("2030-01-08T11:27:00Z", "Tally at the 09:27 check: 51 closes.", user=PERSON, number=30),
    comment("2030-01-08T11:30:00Z", "<!-- loom:remote-claim v1 muse 2250 1893497400.000 -->", user=LANES, number=2250),
    comment("2030-01-08T11:00:00Z", ROUND, user=LANES, number=77),
]
PULLS = [{"number": 77, "html_url": "https://github.com/example-owner/sample/pull/77?a=1&b=2",
          "body": "Fix.\n\nLoom ticket example-owner.sample-41, lane codex, model model-x.\n",
          "created_at": "2030-01-08T10:40:00Z", "merged_at": "2030-01-08T11:05:00Z",
          "head": {"ref": "loom/41-x"}, "base": {"repo": {"full_name": REPO}}}]
LANE_STATUS = {"created_at": "2030-01-01T00:00:00Z", "updated_at": "2030-01-08T11:58:00Z", "user": STATION,
               "body": "<!-- loom:lanes v1 -->\nUpdated 2030-01-08 11:58:00Z\n\n| lane | available | heartbeat (UTC) |\n"
                       "|---|---|---|\n| codex | yes | 2030-01-08 11:55:00Z |\n| claude | no | 2030-01-08 11:40:00Z |\n"}
LANE_ROWS = [{"lane": "codex", "available": True, "heartbeat": "2030-01-08 11:55:00Z"},
             {"lane": "claude", "available": False, "heartbeat": "2030-01-08 11:40:00Z"}]
BEATS = [datetime(2030, 1, 8, 8, 0, tzinfo=timezone.utc) + timedelta(minutes=10 * i) for i in range(24)]
# A 40 minute silence between 09:00 and 09:40: longer than 10 minutes plus 50 percent.
CHAIN_SEEN = [b.timestamp() for b in BEATS if not (datetime(2030, 1, 8, 9, 0, tzinfo=timezone.utc)
                                                      < b < datetime(2030, 1, 8, 9, 40, tzinfo=timezone.utc))]


def payload(**extra):
    data = {"improving": {"journals": JOURNALS}, "issue_comments": COMMENTS, "pull_requests": PULLS,
            "lanes": [LANE_STATUS], "stale": False, "hot_spots_error": ""}
    data.update(extra)
    return data


def build(**extra):
    return fleet.build_fleet(payload(**extra), LANE_ROWS, CHAIN_SEEN, NOW)


def row(data, key):
    return next(r for r in data["rows"] if r["key"] == key)


class FleetBuildTest(unittest.TestCase):
    def test_every_engine_is_a_row_in_fleet_order(self):
        data = build()
        self.assertEqual([r["key"] for r in data["rows"]],
                         ["brain:muse", "brain:lane-machine", "brain:operator-session", "reports", "station",
                          "notes", "lane:claude", "lane:codex", "lane:muse"])
        self.assertEqual(row(data, "brain:muse")["label"], "Muse brain")
        self.assertNotIn("brain:old-holder", [r["key"] for r in data["rows"]])
        self.assertEqual(data["start"], START)
        self.assertEqual(data["end"], NOW + fleet.AHEAD)

    def test_journal_ticks_are_events_timed_by_the_tick_with_their_gist(self):
        muse = row(build(), "brain:muse")
        self.assertEqual([e["at"].strftime("%H:%M") for e in muse["events"]], ["09:00", "09:30", "11:35"])
        self.assertEqual(muse["events"][0]["gist"], "ok: Briefed: example-owner/sample#41 to codex")
        self.assertTrue(all(e["beat"] for e in muse["events"]))
        self.assertEqual(muse["cadence"], 30)
        self.assertEqual(muse["next"], datetime(2030, 1, 8, 12, 5, tzinfo=timezone.utc))

    def test_a_silence_past_the_cadence_and_a_half_is_a_missed_run(self):
        data = build()
        muse = row(data, "brain:muse")
        self.assertEqual([(m["from"].strftime("%H:%M"), m["to"].strftime("%H:%M"), m["minutes"]) for m in muse["missed"]],
                         [("11:50", "09:00", 1270), ("09:30", "11:35", 125)])
        # A documented brain with no tick in the window is one missed run, start to now.
        quiet = row(data, "brain:lane-machine")
        self.assertEqual(quiet["events"], [])
        self.assertEqual([(m["from"], m["to"], m["minutes"]) for m in quiet["missed"]], [(START, NOW, 1440)])
        self.assertIn("no tick in the last 24 hours", quiet["note"])
        # The station's silence is judged from its chain posts, not from its comments.
        station = row(data, "station")
        self.assertEqual([(m["from"].strftime("%H:%M"), m["to"].strftime("%H:%M"), m["minutes"])
                          for m in station["missed"]], [("09:00", "09:40", 40)])
        # A lane has no cadence, so a quiet lane is not a missed run.
        self.assertEqual(row(data, "lane:claude")["missed"], [])

    def test_the_station_row_holds_its_comments_and_its_chain_posts(self):
        station = row(build(), "station")
        kinds = {(e["at"].strftime("%H:%M"), e["kind"]) for e in station["events"] if not e["beat"]}
        self.assertEqual(kinds, {("11:20", "shipped"), ("10:40", "outcome"), ("10:50", "failed"), ("11:58", "status")})
        self.assertEqual(sum(1 for e in station["events"] if e["beat"]), len(CHAIN_SEEN))
        self.assertEqual(station["cadence"], 10)
        self.assertEqual(station["next"], datetime(2030, 1, 8, 12, 0, tzinfo=timezone.utc))

    def test_without_chain_posts_the_station_says_so_instead_of_marking_misses(self):
        data = fleet.build_fleet(payload(), LANE_ROWS, [], NOW)
        station = row(data, "station")
        self.assertEqual(station["missed"], [])
        self.assertIn("no chain snapshot", station["note"])

    def test_lanes_get_claims_rounds_outcomes_pull_requests_and_heartbeats(self):
        data = build()
        codex = row(data, "lane:codex")
        self.assertEqual([(e["at"].strftime("%H:%M"), e["kind"]) for e in codex["events"]],
                         [("10:40", "outcome"), ("10:40", "opened"), ("11:00", "round"), ("11:05", "merged"),
                          ("11:55", "heartbeat")])
        self.assertEqual(codex["events"][1]["href"], "https://github.com/example-owner/sample/pull/77?a=1&b=2")
        self.assertEqual(codex["events"][2]["gist"],
                         "round 1: Lane codex (model-x) pushed 1 commit for: Acceptance criteria not ticked")
        muse_lane = row(data, "lane:muse")
        self.assertEqual([(e["kind"], e["gist"]) for e in muse_lane["events"]],
                         [("claim", "claimed example-owner/sample#2250")])
        self.assertEqual(row(data, "lane:claude")["events"][0]["kind"], "failed")

    def test_reports_and_notes_rows_carry_their_new_york_due_marks(self):
        data = build()
        reports = row(data, "reports")
        self.assertEqual([e["gist"] for e in reports["events"]], ["3 merged, 1 failed since the last report."])
        # 10:00 and 21:00 New York on 7 January 2030 (EST) are 15:00Z and 02:00Z the next day.
        self.assertEqual([d.strftime("%dT%H:%M") for d in reports["due"]], ["07T15:00", "08T02:00"])
        notes = row(data, "notes")
        self.assertEqual([e["gist"] for e in notes["events"]], ["Tally at the 09:27 check: 51 closes."])
        self.assertEqual([d.strftime("%dT%H:%M") for d in notes["due"]], ["07T14:05", "07T22:05"])

    def test_an_empty_snapshot_renders_with_reasons_not_errors(self):
        data = fleet.build_fleet({}, [], [], NOW)
        self.assertEqual([r["key"] for r in data["rows"]], ["brain:muse", "brain:lane-machine", "reports", "station", "notes"])
        page = fleet.render_fleet(data)
        self.assertIn("no tick in the last 24 hours", page)
        self.assertIn("No lane has a status row or an event", page)


class FleetRenderTest(unittest.TestCase):
    def setUp(self):
        self.page = fleet.render_fleet(build())

    def test_rows_events_and_misses_are_drawn_with_their_gists(self):
        self.assertIn('<strong>Muse brain</strong>', self.page)
        self.assertIn('<strong>codex</strong>', self.page)
        self.assertIn('class="fleet-miss"', self.page)
        self.assertIn("no tick for 125 minutes, cadence 30", self.page)
        self.assertIn("no run for 40 minutes, cadence 10", self.page)
        self.assertIn('class="fleet-gist">09:00 ok: Briefed: example-owner/sample#41 to codex', self.page)
        self.assertIn("next 12:05", self.page)
        # The axis: 24 hours back and 2 ahead, one pixel a minute, with the now line.
        self.assertIn('style="--fleet-w:%dpx"' % (26 * 60), self.page)
        self.assertIn('class="fleet-now" style="left:1440px"', self.page)
        self.assertIn('class="fleet-due" style="left:1470px"', self.page)   # 12:30, on Muse's 30 minute grid
        self.assertIn('class="fleet-next" style="left:1445px"', self.page)  # 12:05, Muse's next tick
        self.assertIn('class="fleet-ev ev-tick" style="left:1260px"', self.page)   # the 09:00 tick

    def test_issue_text_is_escaped_everywhere(self):
        self.assertNotIn("<script>", self.page)
        self.assertNotIn("<b>main</b>", self.page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt; to claude", self.page)
        self.assertIn("tests failed on &lt;b&gt;main&lt;/b&gt;", self.page)
        self.assertIn('href="https://github.com/example-owner/sample/pull/77?a=1&amp;b=2"', self.page)
        self.assertTrue(self.page.isascii())

    def test_the_page_adds_no_script_and_scrolls_inside_its_own_box(self):
        self.assertNotIn("<script", self.page)
        self.assertIn('<div class="fleet-scroll">', self.page)
        self.assertIn('tabindex="0"', self.page)


class FleetRouteTest(unittest.TestCase):
    def test_the_page_is_routed_and_in_the_nav_after_brain(self):
        sent = {}

        class Handler:
            path = "/fleet"

            def send_page(self, body, code=200):
                sent["body"] = body

        class State:
            chain = None
            chain_seen = CHAIN_SEEN

            def response(self):
                return payload()

        server._dispatch_get(Handler(), State())
        self.assertIn('href="/fleet" aria-current="page">Fleet timeline</a>', sent["body"])
        self.assertIn("<strong>Muse brain</strong>", sent["body"])
        keys = [key for key, _, _ in VIEWS]
        self.assertEqual(keys[keys.index("brain") + 1], "fleet")

    def test_a_chain_post_is_remembered_as_a_station_beat(self):
        class State:
            pass
        state = State()
        fleet.remember_beat(state, 100.0)
        fleet.remember_beat(state, 160.0)
        self.assertEqual(list(state.chain_seen), [100.0, 160.0])


if __name__ == "__main__":
    unittest.main()
