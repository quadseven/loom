"""The Floor shows each lane with the ticket it works and what it is doing
now, from live lane state, and a lane with no ticket reads idle (loom#2157)."""
import hashlib
import json
import os
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from bus.github import GitHub
from dashboard import server, views
from dashboard.github import TicketStore, ticket_record

REPO = "example-owner/sample"
REFRESH_START = 1793188800  # 2026-10-28 12:00 UTC
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


class FloorGitHub:
    """Issues and lane status, with GitHub's pagination and conditional GETs."""

    def __init__(self):
        self.issues = {n: dict(issue(n, "verified"), state="closed", updated_at="2026-10-27T00:00:00Z")
                       for n in range(1, 103)}
        self.calls = []
        self.fail_delta = False

    def __call__(self, method, url, headers, body):
        parsed = urlsplit(url)
        query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        response_headers, records = {}, []
        if parsed.path == "/repos/%s/issues/200/comments" % REPO:
            records = [{"body": LANES, "created_at": "2026-10-28T12:00:00Z"}]
        elif parsed.path == "/repos/%s/issues" % REPO:
            label, since = query.get("labels"), query.get("since")
            if label == "loom:lanes":
                records = [{"number": 200}]
            elif query.get("state") == "all" and label in (None, "loom:ticket"):
                if since and not label and self.fail_delta:
                    return 403, {}, b'{"message":"fixture read failure"}'
                records = [row for row in reversed(self.issues.values())
                           if (not since or row["updated_at"] > since)
                           and (not label or {"name": label} in row["labels"])]
                if query.get("sort") == "updated":
                    records.sort(key=lambda row: row["updated_at"], reverse=query.get("direction") == "desc")
                size, page = int(query["per_page"]), int(query.get("page", 1))
                if len(records) > page * size:
                    next_url = url.split("&page=", 1)[0] + "&page=%d" % (page + 1)
                    response_headers["Link"] = '<%s>; rel="next"' % next_url
                records = records[(page - 1) * size:page * size]
                if not since or label is None:
                    self.calls.append((since, headers.get("If-None-Match")))
        payload = json.dumps(records).encode()
        etag = '"%s"' % hashlib.sha256(payload).hexdigest()
        response_headers["ETag"] = etag
        if headers.get("If-None-Match") == etag:
            return 304, response_headers, b""
        return 200, response_headers, payload


class FloorRefresh(unittest.TestCase):
    def setUp(self):
        cache = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {"LOOM_STATUS_REPO": REPO, "LOOM_TRANSCRIPTS_MEMORY": "1"},
                                     clear=True))
        self.github = FloorGitHub()
        gh = GitHub("", api="https://api.github.invalid", cache_dir=cache, transport=self.github)
        self.store = TicketStore([REPO], gh=gh, budget_floor=0)

    def refresh(self, seconds=0):
        with patch("dashboard.github.time.time", return_value=REFRESH_START + seconds):
            self.store.refresh()

    def poll(self):
        class Poll:
            path = "/api/floor/live"

            def send_json(self, code, value):
                return code, value

        status, payload = server._dispatch_get(Poll(), self.store)
        self.assertEqual(status, 200)
        return payload

    def test_the_next_floor_poll_moves_a_ticket_to_its_review_lane(self):
        self.github.issues[103] = dict(issue(103, "running", "codex", "Move me"),
                                       updated_at="2026-10-28T11:59:50Z")
        self.github.issues[104] = dict(issue(104, "briefed", "gemini", "Waiting for station"),
                                       updated_at="2026-10-28T11:59:50Z")
        self.refresh()
        before = self.poll()
        self.assertIn("Move me", card(before["lanes"], "codex"))
        self.assertIn("implementing", card(before["lanes"], "codex"))
        self.assertIn("idle, able", card(before["lanes"], "anthropic"))
        self.assertIn("briefed", card(before["lanes"], "gemini"))
        self.assertEqual(len(self.github.calls), 2)

        self.github.issues[103] = dict(issue(103, "pr", "anthropic", "Move me"),
                                       updated_at="2026-10-28T12:00:30Z")
        self.github.calls.clear()
        self.refresh(60)
        after = self.poll()
        self.assertIn("idle, able", card(after["lanes"], "codex"))
        self.assertIn("Move me", card(after["lanes"], "anthropic"))
        self.assertIn("in review", card(after["lanes"], "anthropic"))
        self.assertEqual(card(before["lanes"], "gemini"), card(after["lanes"], "gemini"))
        self.assertNotEqual(before["moving"], after["moving"])
        self.assertEqual(len(self.github.calls), 1)
        self.assertIsNotNone(self.github.calls[0][0])

        self.refresh(120)
        self.github.calls.clear()
        self.refresh(180)
        self.assertEqual(self.poll(), after)
        self.assertEqual(len(self.github.calls), 1)
        since, etag = self.github.calls[0]
        self.assertIsNotNone(since)
        self.assertIsNotNone(etag)

    def test_the_next_poll_drops_closed_and_unlabeled_tickets_after_a_retry(self):
        for number, title in ((103, "Closed ticket"), (104, "Unlabeled ticket")):
            self.github.issues[number] = dict(issue(number, "running", "codex", title),
                                              updated_at="2026-10-28T11:59:50Z")
        self.refresh()
        before = self.poll()
        self.assertIn("Closed ticket", before["lanes"])
        self.assertIn("Unlabeled ticket", before["lanes"])

        self.github.issues[103].update(state="closed", updated_at="2026-10-28T12:00:00Z")
        self.github.issues[104]["labels"].remove({"name": "loom:ticket"})
        self.github.issues[104]["updated_at"] = "2026-10-28T12:00:00Z"
        self.github.fail_delta = True
        self.refresh(60)
        self.assertEqual(self.poll(), before)

        self.github.fail_delta = False
        self.refresh(120)
        after = self.poll()
        self.assertNotIn("Closed ticket", after["lanes"] + after["moving"])
        self.assertNotIn("Unlabeled ticket", after["lanes"] + after["moving"])
        self.assertIn("idle, able", card(after["lanes"], "codex"))

    def test_the_hourly_full_read_reconciles_a_deleted_ticket(self):
        self.github.issues[103] = dict(issue(103, "running", "codex", "Deleted ticket"),
                                       updated_at="2026-10-28T11:59:50Z")
        self.refresh()
        del self.github.issues[103]
        self.refresh(60)
        self.assertIn("Deleted ticket", self.poll()["lanes"])
        self.refresh(3600)
        self.assertNotIn("Deleted ticket", self.poll()["lanes"])


if __name__ == "__main__":
    unittest.main()
