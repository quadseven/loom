"""The Floor shows each lane with the ticket it works and what it is doing
now, from live lane state, and a lane with no ticket reads idle (loom#2157)."""
import json
import unittest

from dashboard import server, views
from dashboard.github import ticket_record

REPO = "example-owner/sample"
LANES = (views.LANES_MARKER + "\n| Lane | Available | Heartbeat |\n|---|---|---|\n"
         "| codex | yes | 2026-10-28T12:00:00Z |\n| anthropic | yes | 2026-10-28T12:00:01Z |\n"
         "| gemini | no | 2026-10-28T12:00:02Z |\n")


def issue(number, state, lane=None, title=None, labels=()):
    names = ["loom:ticket", "loom:" + state] + (["lane:" + lane] if lane else []) + list(labels)
    return {"number": number, "title": title or "Ticket %d" % number, "body": "## Why\nx",
            "labels": [{"name": n} for n in names]}


def fix_comment(number):
    return {"issue_url": "https://api.github.com/repos/%s/issues/%d" % (REPO, number),
            "body": "<!-- loom:fix v1 -->\nPull request: %s#9, branch loom/x" % REPO,
            "created_at": "2026-10-28T11:00:00Z"}


class State:
    def __init__(self, issues, comments=()):
        self.issues, self.comments = issues, list(comments)
        self.context = {}
        self.repos = [REPO]

    def response(self):
        return {"tickets": {REPO: [ticket_record(REPO, i) for i in self.issues]},
                "operator_items": [], "footer_models": {},
                "lanes": [{"body": LANES, "created_at": "2026-10-28T12:00:00Z"}],
                "issue_comments": self.comments}


def card(html, lane):
    start = html.rindex("<article", 0, html.index('<strong>%s</strong>' % lane))
    return html[start:html.index("</article>", start)]


class FloorLanes(unittest.TestCase):
    def test_a_ticket_is_attached_to_its_lane_with_its_activity(self):
        live = server._floor_live(State([issue(1, "running", "codex", "Add the thing"),
                                         issue(2, "pr", "anthropic")]))["lanes"]
        self.assertIn("implementing", card(live, "codex"))
        self.assertIn("Add the thing", card(live, "codex"))
        self.assertIn("in review", card(live, "anthropic"))

    def test_a_running_ticket_with_a_fix_comment_is_a_fix_round(self):
        live = server._floor_live(State([issue(1, "running", "codex")], [fix_comment(1)]))["lanes"]
        self.assertIn("fix round", card(live, "codex"))

    def test_a_shipped_ticket_is_verifying_and_a_briefed_one_is_waiting_for_the_station(self):
        live = server._floor_live(State([issue(1, "shipped", "codex"), issue(2, "briefed", "anthropic")]))["lanes"]
        self.assertIn("verifying", card(live, "codex"))
        self.assertIn("briefed", card(live, "anthropic"))

    def test_a_stage_change_changes_the_markup(self):
        before = server._floor_live(State([issue(1, "running", "codex")]))
        after = server._floor_live(State([issue(1, "pr", "codex")]))
        self.assertIn("implementing", before["lanes"])
        self.assertIn("in review", after["lanes"])
        self.assertNotIn("implementing", after["lanes"])
        self.assertNotEqual(before["moving"], after["moving"])

    def test_a_change_of_hands_moves_the_ticket_between_lanes(self):
        a = server._floor_live(State([issue(1, "running", "codex", "Move me")]))["lanes"]
        b = server._floor_live(State([issue(1, "running", "anthropic", "Move me")]))["lanes"]
        self.assertIn("Move me", card(a, "codex"))
        self.assertNotIn("Move me", card(a, "anthropic"))
        self.assertIn("Move me", card(b, "anthropic"))

    def test_a_lane_with_no_ticket_reads_idle_and_a_held_lane_reads_held(self):
        live = server._floor_live(State([issue(1, "running", "codex")]))["lanes"]
        self.assertIn("idle, able", card(live, "anthropic"))
        self.assertIn("lane-free", card(live, "anthropic"))
        self.assertIn("idle, held", card(live, "gemini"))
        self.assertNotIn("idle", card(live, "codex"))

    def test_ticket_text_is_escaped(self):
        live = server._floor_live(State([issue(1, "running", "codex", "<script>x</script>")]))
        self.assertNotIn("<script>", live["lanes"] + live["moving"])

    def test_a_finished_or_closed_ticket_is_not_attached(self):
        closed = dict(issue(2, "running", "anthropic", "Gone"), state="closed")
        live = server._floor_live(State([issue(1, "verified", "codex", "Done"), closed]))["lanes"]
        self.assertNotIn("Done", live)
        self.assertNotIn("Gone", live)

    def test_the_floor_page_carries_the_live_hooks_and_the_script_polls_them(self):
        page = server._floor(State([issue(1, "running", "codex")]))
        self.assertIn('data-live="lanes"', page)
        self.assertIn('data-live="moving"', page)
        self.assertIn('data-live-url="/api/floor/live"', page)
        with open("dashboard/static/loom.js", encoding="utf-8") as handle:
            self.assertIn("data-live-url", handle.read())

    def test_the_live_endpoint_serves_the_same_two_fragments_as_json(self):
        sent = []

        class Handler:
            path = "/api/floor/live"

            def send_json(self, code, value):
                sent.append((code, value))
        server._dispatch_basic(Handler(), State([issue(1, "running", "codex")]), "/api/floor/live")
        code, value = sent[0]
        self.assertEqual(code, 200)
        self.assertEqual(set(value), {"lanes", "moving"})
        json.dumps(value)


if __name__ == "__main__":
    unittest.main()
