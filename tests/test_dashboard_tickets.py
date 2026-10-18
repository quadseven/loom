"""The Tickets page (loom#608): a dense table grouped by goal home, in-flight
tickets by default, and the model each ticket's last run used."""
import os
import re
import time
import unittest
from unittest import mock
from urllib.parse import urlsplit

from bus import factory
from bus.metrics import entered_state
from dashboard import transcripts
from dashboard.server import _floor, _tickets

GOAL_A = "example-owner/app#30"
GOAL_B = "example-owner/app#40"


def _row(number, title, state, goal=GOAL_A, lane="codex", model=None, **extra):
    return dict({"repo": "example-owner/app", "number": number, "title": title, "state": state,
                 "goal": goal, "lane": lane, "model": model, "attempts": None, "open": True,
                 "body": "Part of #%s\n" % (goal or "#0").split("#")[-1] + (
                     "Model: %s\n" % model if model else ""),
                 "labels": ["loom:ticket", "loom:" + state], "updated_at": "2026-10-17T10:00:00Z"}, **extra)


def _footer(number, model, opened, pr):
    return {"number": pr, "html_url": "https://github.com/example-owner/app/pull/%d" % pr,
            "created_at": opened, "updated_at": opened,
            "body": "## Why\nx\n\n---\nLoom ticket example-owner/app#%d, lane codex, model %s." % (number, model)}


def _claim(number, lane, at):
    return {"repo": "example-owner/app", "created_at": at,
            "issue_url": "https://api.github.com/repos/example-owner/app/issues/%d" % number,
            "body": "Claimed for lane %s (body sha256 0123456789ab)" % lane}


class State:
    stale = False
    context = {}
    waits = {}

    def __init__(self, rows, prs=(), comments=(), entered=None, runs=()):
        self.data = {"example-owner/app": list(rows)}
        self.prs, self.comments, self.entered = list(prs), list(comments), entered or {}
        self.transcripts = transcripts.Store(transcripts.MemoryBackend())
        for run in runs:
            self.transcripts.upload(dict({"slot": "codex", "outcome": "done", "events": []}, **run))
        # What the background refresh (TicketStore.refresh) reads once a pass.
        self.latest_runs = self.transcripts.latest()

    def response(self):
        return {"tickets": self.data, "pull_requests": self.prs, "issue_comments": self.comments,
                "state_entered": self.entered, "latest_runs": self.latest_runs,
                "goal_context": {GOAL_A: {"title": "Make the app faster"},
                                 GOAL_B: {"title": "Make the app safer"}}}


def _cell(body, number, cls):
    """The text of one ticket row's cell, tags stripped."""
    row = re.search(r'<tr><td class="tickets-ticket"><a href="/t/example-owner/app/%d".*?</tr>' % number, body, re.S)
    assert row, "no row for #%d" % number
    cell = re.search(r'<td class="%s[^"]*"[^>]*>(.*?)</td>' % cls, row.group(0), re.S)
    return re.sub(r"<[^>]+>", "", cell.group(1))


class TicketsTableTest(unittest.TestCase):
    def test_groups_tickets_under_their_goal_home_and_hides_done_states_by_default(self):
        state = State([
            _row(31, "Cache the index", "running"),
            _row(32, "Trim the payload", "queued"),
            _row(41, "Check the token", "failed", goal=GOAL_B),
            _row(33, "Ship the cache", "merged"),
            _row(34, "Old verified work", "verified", goal=GOAL_B),
            _row(35, "Closed while queued", "queued", open=False),
        ])
        body = _tickets(state, urlsplit("/tickets"))
        groups = re.findall(r'<details class="tickets-group" open>.*?</details>', body, re.S)
        self.assertEqual(len(groups), 2)
        first = next(g for g in groups if "Make the app faster" in g)
        second = next(g for g in groups if "Make the app safer" in g)
        self.assertIn("app#30", first)
        self.assertIn("Cache the index", first)
        self.assertIn("Trim the payload", first)
        self.assertNotIn("Check the token", first)
        self.assertIn("Check the token", second)
        self.assertIn("<table", first)
        for hidden in ("Ship the cache", "Old verified work", "Closed while queued"):
            self.assertNotIn(hidden, body)
        done = re.search(r'<a class="chip tickets-counter tickets-done" href="/tickets\?done=1"[^>]*>(.*?)</a>', body)
        self.assertIsNotNone(done)
        self.assertIn("<b>3</b>", done.group(1))
        self.assertIn("merged 1, verified 1, closed 1", done.group(1))
        done_body = _tickets(state, urlsplit("/tickets?done=1"))
        for shown in ("Ship the cache", "Old verified work", "Closed while queued"):
            self.assertIn(shown, done_body)
        self.assertNotIn("Cache the index", done_body)

    def test_state_counter_filters_the_table(self):
        state = State([_row(31, "Cache the index", "running"), _row(32, "Trim the payload", "queued"),
                       _row(33, "Ship the cache", "merged")])
        body = _tickets(state, urlsplit("/tickets"))
        self.assertIn('href="/tickets?state=queued"', body)
        self.assertIn('href="/tickets?state=running"', body)
        queued = _tickets(state, urlsplit("/tickets?state=queued"))
        self.assertIn("Trim the payload", queued)
        self.assertNotIn("Cache the index", queued)
        self.assertRegex(queued, r'href="/tickets\?state=queued" aria-current="true"')
        merged = _tickets(state, urlsplit("/tickets?state=merged"))
        self.assertIn("Ship the cache", merged)
        self.assertNotIn("Trim the payload", merged)

    def test_model_is_the_last_runs_and_a_ticket_with_no_run_says_not_run_yet(self):
        state = State(
            [_row(31, "Footer ticket", "pr", lane="opencode-go"),
             _row(32, "Never ran", "queued", lane=None),
             _row(33, "Claimed after its pull request", "running", lane="claude"),
             _row(34, "Transcript after its claim", "running", lane="spark"),
             _row(35, "Claimed on an unnamed model", "running", lane="spark")],
            prs=[_footer(31, "model-old", "2026-10-10T00:00:00Z", 90),
                 _footer(31, "model-footer", "2026-10-16T00:00:00Z", 91),
                 _footer(33, "model-first", "2026-10-15T00:00:00Z", 92)],
            comments=[_claim(31, "opencode-go", "2026-10-15T23:00:00Z"),
                      _claim(33, "claude", "2026-10-16T05:00:00Z"),
                      _claim(34, "spark", "2026-10-16T05:00:00Z"),
                      _claim(35, "spark", "2026-10-16T05:00:00Z")],
            runs=[{"ticket": "example-owner/app#34", "model": "model-transcript",
                   "started_at": "2026-10-16T05:00:30Z", "finished_at": "2026-10-16T06:00:00Z"},
                  {"ticket": "example-owner/app#35", "model": "model-before",
                   "started_at": "2026-10-15T05:00:00Z", "finished_at": "2026-10-15T06:00:00Z"}])
        with mock.patch.dict(os.environ, {"LOOM_CLAUDE_MODEL": "claude-opus-5-5"}):
            body = _tickets(state, urlsplit("/tickets"))
        self.assertEqual(_cell(body, 31, "tickets-model"), "opencode-go / model-footer")
        self.assertEqual(_cell(body, 31, "tickets-pr"), "#91")
        self.assertEqual(_cell(body, 32, "tickets-model"), "no lane / not run yet")
        # The claim is newer than the pull request: the claude lane's
        # configured model (LOOM_CLAUDE_MODEL) ran.
        self.assertEqual(_cell(body, 33, "tickets-model"), "claude / claude-opus-5-5")
        self.assertEqual(_cell(body, 34, "tickets-model"), "spark / model-transcript")
        # The newest run is a claim no record names a model for: it says so,
        # never the older run's model.
        self.assertEqual(_cell(body, 35, "tickets-model"), "spark / model not recorded")
        self.assertNotIn("model unknown", body)
        # The model filter (#533) reads the same model.
        only = _tickets(state, urlsplit("/tickets?model=model-transcript"))
        self.assertIn("Transcript after its claim", only)
        self.assertNotIn("Footer ticket", only)

    def test_floor_runaway_rows_show_the_last_runs_model(self):
        row = _row(31, "Runaway ticket", "failed", lane="codex")
        row["labels"] = row["labels"] + ["loom:runaway"]
        state = State([row, _row(32, "Fresh ticket", "briefed", lane="codex")],
                      prs=[_footer(31, "model-footer", "2026-10-16T00:00:00Z", 91)])
        body = _floor(state)
        runaway = re.search(r'<section class="section runaway-tickets">.*?</section>', body, re.S).group(0)
        self.assertIn("codex / model-footer", runaway)
        self.assertIn("codex / not run yet", body)
        self.assertNotIn("model unknown", body)

    def test_age_in_state_reads_the_label_event_and_says_when_it_could_not(self):
        now = time.time()
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 5 * 3600))
        state = State([_row(31, "Fresh label", "running"), _row(32, "Moved since the read", "queued",
                                                                 updated_at=stamp)],
                      entered={"example-owner/app#31": ("running", now - 3 * 3600),
                               "example-owner/app#32": ("briefed", now - 9 * 3600)})
        body = _tickets(state, urlsplit("/tickets"))
        self.assertEqual(_cell(body, 31, "tickets-age"), "3h")
        self.assertEqual(_cell(body, 32, "tickets-age"), "upd 5h")


class RunRecordsTest(unittest.TestCase):
    def test_entered_state_is_the_newest_state_label_still_on(self):
        events = [{"event": "labeled", "label": {"name": "loom:queued"}, "created_at": "2026-10-16T00:00:00Z"},
                  {"event": "labeled", "label": {"name": "loom:running"}, "created_at": "2026-10-16T02:00:00Z"},
                  {"event": "unlabeled", "label": {"name": "loom:queued"}, "created_at": "2026-10-16T02:00:01Z"},
                  {"event": "labeled", "label": {"name": "lane:codex"}, "created_at": "2026-10-16T03:00:00Z"}]
        state, at = entered_state(events)
        self.assertEqual(state, "running")
        self.assertEqual(time.strftime("%H:%M", time.gmtime(at)), "02:00")
        self.assertIsNone(entered_state(events + [
            {"event": "unlabeled", "label": {"name": "loom:running"}, "created_at": "2026-10-16T04:00:00Z"}]))

    def test_claim_models_pairs_each_claim_with_the_model_it_ran_on(self):
        issue = {"body": "Model: model-body\n"}
        claim = {"body": "Claimed for lane spark (body sha256 0123456789ab)"}
        marked = [claim, {"body": factory.RUNG_MARKER % 2}, claim]
        self.assertEqual([m for _, m in factory.claim_models(issue, [claim])], ["model-body"])
        self.assertEqual([m for _, m in factory.claim_models({"body": ""}, [claim])], [None])
        models = [m for c, m in factory.claim_models({"body": ""}, marked) if c]
        self.assertEqual(models, [None, factory.LADDER[1][1]])

    def test_store_latest_is_each_tickets_newest_run(self):
        store = transcripts.Store(transcripts.MemoryBackend())
        for ticket, model, started in (("example-owner/app#1", "model-a", "2026-10-16T00:00:00Z"),
                                       ("example-owner/app#1", "model-b", "2026-10-16T05:00:00Z"),
                                       ("example-owner/app#2", "model-c", "2026-10-15T00:00:00Z")):
            store.upload({"ticket": ticket, "slot": "codex", "model": model, "outcome": "done",
                          "started_at": started, "finished_at": started, "events": []})
        latest = store.latest()
        self.assertEqual({key: row["model"] for key, row in latest.items()},
                         {"example-owner/app#1": "model-b", "example-owner/app#2": "model-c"})


class SlowTranscripts:
    """A transcript store whose every read takes 3 seconds, like a database
    that is slow to answer or holds a lock."""
    def __init__(self, latest=None, error=None):
        self.calls = 0
        self.value, self.error = latest or {}, error

    def latest(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        time.sleep(3)
        return self.value


class LatestRunsOffTheRequestTest(unittest.TestCase):
    """loom#780: pages never wait on the transcript database."""

    def test_a_slow_transcript_store_does_not_delay_a_tickets_render(self):
        state = State([_row(34, "Transcript after its claim", "running", lane="spark")])
        state.transcripts = SlowTranscripts()
        state.latest_runs = {"example-owner/app#34": {
            "ticket": "example-owner/app#34", "model": "model-transcript",
            "started_at": "2026-10-16T05:00:30Z", "finished_at": "2026-10-16T06:00:00Z"}}
        started = time.monotonic()
        body = _tickets(state, urlsplit("/tickets"))
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(state.transcripts.calls, 0)
        self.assertEqual(_cell(body, 34, "tickets-model"), "spark / model-transcript")
        started = time.monotonic()
        _floor(state)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(state.transcripts.calls, 0)

    def _ticket_store(self):
        from bus.github import GitHub
        from dashboard.github import TicketStore

        def transport(method, url, headers, body):
            return 200, {}, b"[]"
        return TicketStore(["example-owner/app"], gh=GitHub("", transport=transport))

    def test_the_background_refresh_reads_the_latest_runs_map(self):
        store = self._ticket_store()
        store.transcripts = transcripts.Store(transcripts.MemoryBackend())
        store.transcripts.upload({"ticket": "example-owner/app#7", "slot": "codex", "model": "model-a",
                                  "outcome": "done", "started_at": "2026-10-16T00:00:00Z",
                                  "finished_at": "2026-10-16T00:10:00Z", "events": []})
        with mock.patch.dict(os.environ, {}, clear=True):
            store.refresh()
        latest = store.response()["latest_runs"]
        self.assertEqual(latest["example-owner/app#7"]["model"], "model-a")

    def test_a_failed_read_keeps_the_last_map_and_says_why(self):
        store = self._ticket_store()
        store.transcripts = transcripts.Store(transcripts.MemoryBackend())
        store.transcripts.upload({"ticket": "example-owner/app#7", "slot": "codex", "model": "model-a",
                                  "outcome": "done", "started_at": "2026-10-16T00:00:00Z",
                                  "finished_at": "2026-10-16T00:10:00Z", "events": []})
        with mock.patch.dict(os.environ, {}, clear=True):
            store.refresh()
            store.transcripts = SlowTranscripts(error=OSError("connect timed out"))
            store.refresh()
        payload = store.response()
        self.assertEqual(payload["latest_runs"]["example-owner/app#7"]["model"], "model-a")
        self.assertTrue(any("connect timed out" in error for error in payload["dashboard_errors"]))

    def test_a_driver_error_outside_storage_errors_never_breaks_the_refresh(self):
        class DriverError(Exception):
            """A driver's own error class, which storage_errors() may not list."""
        store = self._ticket_store()
        store.transcripts = SlowTranscripts(error=DriverError("canceling statement due to statement timeout"))
        with mock.patch.dict(os.environ, {}, clear=True):
            store.refresh()
        payload = store.response()
        self.assertEqual(payload["latest_runs"], {})
        self.assertTrue(any("statement timeout" in error for error in payload["dashboard_errors"]))


if __name__ == "__main__":
    unittest.main()
