"""Ticket detail rendering and attempt event contracts."""
from __future__ import annotations

import sys
import json
import os
import io
import unittest
from pathlib import Path
from email.message import Message
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard.ticket import interleave_activity, render
from dashboard import transcripts
from dashboard.server import _dispatch_get, _handler_for


class TicketRenderingTests(unittest.TestCase):
    def test_review_threads_interleave_with_events_by_timestamp(self):
        rows = interleave_activity(
            [{"ts": "2026-10-14T10:00:02Z", "role": "assistant", "kind": "text", "text": "continued"}],
            [{"ts": "2026-10-14T10:00:01Z", "text": "review note", "resolved": False,
              "answer": "lane answer", "answer_ts": "2026-10-14T10:00:03Z"}],
        )
        self.assertEqual([row["activity_type"] for row in rows], ["review", "transcript", "answer"])

    def test_two_attempt_ticket_pairs_tool_result_hides_thought_and_collapses_brief(self):
        record = {"repo": "example-owner/sample", "number": 12, "title": "Example", "state": "running",
                  "body": "- [x] First\n- [ ] Second", "lane": "codex", "model": "model-x",
                  "difficulty": "hard", "goal": "example-owner/sample#30"}
        runs = [{"id": 1, "attempt": 1}, {"id": 2, "attempt": 2}]
        events = [{"ts": "2026-10-14T10:00:00Z", "role": "user", "kind": "text", "text": "Brief\nFull detail"},
                  {"ts": "2026-10-14T10:00:01Z", "role": "thinking", "kind": "text", "text": "Reasoning"},
                  {"ts": "2026-10-14T10:00:02Z", "role": "assistant", "kind": "tool_use", "text": "$ pytest"},
                  {"ts": "2026-10-14T10:00:03Z", "role": "tool", "kind": "tool_result", "text": "OK"}]
        body = render(record, runs, 2, events)
        self.assertEqual(body.count('class="attempt-chip '), 2)
        self.assertIn("tool-block", body)
        self.assertIn('class="tool-result good"', body)
        self.assertIn('data-thought', body)
        self.assertIn('data-thought-toggle', body)
        self.assertIn('<details class="transcript-brief">', body)
        self.assertIn("show the full brief", body)

    def test_attempt_after_filter_and_upload_authentication(self):
        class State:
            data = {"example-owner/sample": [{"repo": "example-owner/sample", "number": 12,
                    "title": "Example", "state": "running", "body": "- [x] check"}]}
            transcripts = transcripts.Store(transcripts.MemoryBackend())
            def response(self):
                return {"tickets": {}, "pull_requests": []}

        state = State()
        handler_type = _handler_for(state)
        payload = {"ticket": "example-owner/sample#12", "slot": "codex", "model": "model-x",
                   "outcome": "running", "started_at": "2026-10-14T10:00:00Z",
                   "finished_at": "2026-10-14T10:01:00Z", "events": [
                       {"ts": "2026-10-14T10:00:01Z", "role": "assistant", "kind": "text", "text": "one"},
                       {"ts": "2026-10-14T10:00:02Z", "role": "assistant", "kind": "text", "text": "two"}]}
        with patch.dict(os.environ, {"LOOM_UPLOAD_TOKEN": "YOUR_UPLOAD_TOKEN_HERE"}):
            def post(auth):
                handler = handler_type.__new__(handler_type)
                handler.path = "/api/transcripts"
                handler.headers = Message()
                handler.headers["Content-Length"] = str(len(json.dumps(payload).encode()))
                if auth:
                    handler.headers["Authorization"] = auth
                handler.rfile = io.BytesIO(json.dumps(payload).encode())
                result = []
                handler.send_json = lambda code, value: result.append((code, value))
                handler.do_POST()
                return result[0]
            self.assertEqual(post("")[0], 401)
            self.assertEqual(post("Bearer YOUR_UPLOAD_TOKEN_HERE")[0], 201)
        class Handler:
            path = "/api/tickets/example-owner/sample/12/runs/1?after=2026-10-14T10%3A00%3A01Z"
            def send_json(self, code, value):
                self.result = (code, value)
        handler = Handler()
        _dispatch_get(handler, state)
        result = handler.result[1]
        self.assertEqual(handler.result[0], 200)
        self.assertEqual([event["text"] for event in result["events"]], ["two"])


RECORD = {"repo": "example-owner/sample", "number": 12, "title": "Example change", "state": "pr",
          "body": "- [x] A test fails without the fix\n- [x] The change\n- [ ] Live: the proof shows",
          "lane": "codex", "model": "model-x", "difficulty": "medium", "goal": "example-owner/sample#30"}
COMMENTS = [
    {"body": "Claimed for lane codex (body sha256 0123456789ab)", "created_at": "2026-10-14T10:00:00Z"},
    {"body": "Lane codex opened a pull request: https://github.com/example-owner/sample/pull/13",
     "created_at": "2026-10-14T10:05:00Z"},
    {"body": "<!-- loom:fix v1 -->\nPull request: example-owner/sample#13\n\n### Check: unittest\n\n    E1\n\n"
             "### Thread PRRT_1 (comment 1) at a.py:1\n\n    rename", "created_at": "2026-10-14T10:10:00Z"},
    {"body": "Lane codex failed: [work] validation failed: the check exited 2 at abc",
     "created_at": "2026-10-14T10:20:00Z"},
    {"body": "<!-- loom:rung 1 -->\nModel: model-y\nMoved up the ladder.", "created_at": "2026-10-14T10:30:00Z"},
]
EVENTS = [{"event": "labeled", "label": {"name": "loom:queued"}, "created_at": "2026-10-14T09:00:00Z"},
          {"event": "labeled", "label": {"name": "lane:codex"}, "created_at": "2026-10-14T09:01:00Z"}]


class FakeGitHub:
    def __init__(self):
        self.calls = []

    def paginate(self, path):
        self.calls.append(path)
        if path.endswith("/issues/12/comments"):
            return COMMENTS
        if path.endswith("/issues/12/events"):
            return EVENTS
        if path.endswith("/pulls/13/files"):
            return [{"filename": "bus/a.py", "additions": 24, "deletions": 5}]
        raise AssertionError(path)

    def rest(self, method, path):
        self.calls.append(path)
        class Reply:
            data = None
        reply = Reply()
        if path.endswith("/pulls/13"):
            reply.data = {"number": 13, "title": "Example change", "state": "closed",
                          "merged_at": "2026-10-14T11:00:00Z", "html_url": "https://github.com/example-owner/sample/pull/13",
                          "head": {"sha": "abc123", "ref": "loom/12-example"}}
        elif "/check-runs" in path:
            reply.data = {"check_runs": [{"name": "unittest", "conclusion": "success"},
                                         {"name": "review", "conclusion": "failure"}]}
        return reply

    def graphql(self, query, variables):
        self.calls.append("graphql")
        return {"repository": {"pullRequest": {"reviewThreads": {"nodes": [
            {"isResolved": False, "comments": {"nodes": [
                {"author": {"login": "review-bot"}, "createdAt": "2026-10-14T10:08:00Z",
                 "body": "medium | _correctness_ | `x`\n\n**What the bot sees**\n\nThe cache is never cleared.\n"},
                {"author": {"login": "lane-bot"}, "createdAt": "2026-10-14T10:12:00Z", "body": "Fixed in abc."}]}}]}}}}


class TicketDataTests(unittest.TestCase):
    def test_datetimes_from_the_database_come_back_as_iso_strings(self):
        from datetime import datetime, timezone

        class Backend:
            def read(self, query, ticket=None):
                return [{"id": 1, "slot": "codex", "outcome": "done",
                         "started_at": datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc),
                         "finished_at": datetime(2026, 10, 14, 10, 5)}]

            def get(self, id_):
                return dict(self.read("list")[0], events=[])

        store = transcripts.Store(Backend())
        attempts = store.attempts("example-owner/sample#12")
        self.assertEqual(attempts[0]["started_at"], "2026-10-14T10:00:00Z")
        self.assertEqual(attempts[0]["finished_at"], "2026-10-14T10:05:00Z")
        json.dumps(attempts)
        json.dumps(store.get(1))

    def test_the_pull_request_is_found_from_the_station_comment_and_read_in_full(self):
        from dashboard import ticket_data
        detail = ticket_data.TicketDetails(FakeGitHub()).get("example-owner/sample", 12)
        pr = detail["pull_request"]
        self.assertEqual((pr["number"], pr["state"]), (13, "merged"))
        self.assertEqual(pr["files"], [{"name": "bus/a.py", "add": 24, "del": 5}])
        self.assertEqual([c["state"] for c in pr["checks"]], ["success", "failure"])
        self.assertEqual(pr["threads"][0]["severity"], "medium")
        self.assertEqual(pr["threads"][0]["text"], "The cache is never cleared.")
        self.assertEqual(pr["threads"][0]["answer"], "Fixed in abc.")

    def test_a_lane_branch_or_refs_line_finds_the_pull_request_without_a_comment(self):
        from dashboard import ticket_data
        prs = [{"number": 7, "body": "x", "head": {"ref": "loom/120-other"}},
               {"number": 8, "body": "x", "head": {"ref": "loom/12-example-r2"}}]
        self.assertEqual(ticket_data.pr_number_from("example-owner/sample", 12, [], prs), 8)
        prs = [{"number": 9, "body": "Refs example-owner/sample#12", "head": {"ref": "feature"}}]
        self.assertEqual(ticket_data.pr_number_from("example-owner/sample", 12, [], prs), 9)

    def test_details_are_read_once_within_the_cache_window(self):
        from dashboard import ticket_data
        gh = FakeGitHub()
        details = ticket_data.TicketDetails(gh, ttl=90, clock=lambda: 100.0)
        details.get("example-owner/sample", 12)
        first = len(gh.calls)
        details.get("example-owner/sample", 12)
        self.assertEqual(len(gh.calls), first)


class TicketPageTests(unittest.TestCase):
    def test_the_path_comes_from_comments_and_label_events_and_ends_in_the_next_step(self):
        from dashboard.ticket import path_steps
        steps = path_steps(RECORD, COMMENTS, EVENTS)
        self.assertEqual([s["who"] for s in steps], ["brain", "lane", "lane", "grug", "fail", "brain", "next"])
        self.assertEqual(steps[2]["text"], "Lane codex opened PR #13.")
        self.assertEqual(steps[3]["text"], "Sent back for a fix round: 1 review thread and 1 failing check.")
        self.assertEqual(steps[5]["meta"], "model-y")
        self.assertTrue(steps[-1]["text"].startswith("Next: checks and the review pass"))

    def test_a_runner_log_without_a_time_goes_after_the_timed_events(self):
        events = [{"ts": "", "role": "runner", "kind": "log", "text": "line\n" * 30},
                  {"ts": "2026-10-14T10:00:00Z", "role": "user", "kind": "text", "text": "Brief"},
                  {"ts": "2026-10-14T10:00:05Z", "role": "assistant", "kind": "text", "text": "Done."}]
        body = render(RECORD, [{"id": 1, "attempt": 1, "slot": "codex", "model": "model-x", "outcome": "done"}],
                      1, events)
        self.assertLess(body.index("transcript-brief"), body.index('class="runner"'))
        self.assertIn("show the full runner log (30 lines)", body)

    def test_a_tool_call_and_its_output_are_one_block_and_a_lone_call_is_closed(self):
        events = [{"ts": "2026-10-14T10:00:01Z", "role": "assistant", "kind": "tool_use",
                   "text": "Bash\ncommand: python3 -m unittest\ndescription: run tests"},
                  {"ts": "2026-10-14T10:00:02Z", "role": "tool", "kind": "tool_result",
                   "text": "FAIL: test_x\nRan 3 tests\nOK"},
                  {"ts": "2026-10-14T10:00:03Z", "role": "assistant", "kind": "tool_use",
                   "text": "Edit\nfile_path: bus/a.py\nold_string: x = 1\nnew_string: x = 2"}]
        body = render(RECORD, [{"id": 1, "attempt": 1}], 1, events)
        self.assertEqual(body.count('<div class="tool-block">'), 2)
        self.assertEqual(body.count("<div"), body.count("</div>"))
        self.assertIn("$ python3 -m unittest", body)
        self.assertIn('<span class="ln bad">FAIL: test_x</span>', body)
        self.assertIn('<span class="ln good">OK</span>', body)
        self.assertIn('<span class="ln good">+ x = 2</span>', body)
        self.assertIn('<span class="tx-label">edited</span>', body)

    def test_attempt_chips_carry_their_outcome_and_the_run_line_names_the_run(self):
        runs = [{"id": 5, "attempt": 1, "slot": "codex", "model": "model-x", "outcome": "failed"},
                {"id": 6, "attempt": 2, "slot": "claude", "model": "model-y", "outcome": "done",
                 "started_at": "2026-10-14T10:00:00Z"}]
        body = render(RECORD, runs, None, [{"ts": "", "role": "runner", "kind": "log", "text": "x"}])
        self.assertIn('class="attempt-chip failed"', body)
        self.assertIn('class="attempt-chip done selected"', body)
        self.assertIn("attempt 2 / claude / model-y / done / started 10:00 UTC", body)
        self.assertIn("kept only the runner log", body)
        self.assertIn('href="?run=1&amp;tab=transcript"', body)

    LIMIT_LOOP = [c for i in range(3) for c in (
        {"body": "Claimed for lane agy (body sha256 a1fc29bcee5e)",
         "created_at": "2026-10-18T13:%02d:07Z" % (10 + 3 * i)},
        {"body": "Lane agy is unavailable (limit: requested model gemini-4-argon-high limited until "
                 "2026-10-19T11:33:47Z). This is not a failure: the ticket goes to the next lane.",
         "created_at": "2026-10-18T13:%02d:35Z" % (11 + 3 * i)})]

    def running(self, comments, state="running"):
        record = dict(RECORD, state=state, lane="agy")
        runs = [{"id": 7, "attempt": 3, "slot": "agy", "model": "gemini-4-argon-high", "outcome": "failed"}]
        return render(record, runs, None, [{"ts": "", "role": "runner", "kind": "log", "text": "x"}],
                      detail={"comments": comments, "events": [], "errors": {}})

    def test_a_running_ticket_whose_last_attempt_hit_a_limit_shows_waiting_not_live(self):
        body = self.running(self.LIMIT_LOOP)
        self.assertNotIn("working... streaming", body)
        self.assertIn('data-running="false"', body)
        self.assertIn("Waiting: gemini-4-argon-high is limited until 2026-10-19 11:33 UTC (lane agy).", body)

    def test_a_briefed_ticket_put_back_after_a_limit_says_what_it_waits_for(self):
        body = self.running(self.LIMIT_LOOP, state="briefed")
        self.assertNotIn("working... streaming", body)
        self.assertIn("Waiting: gemini-4-argon-high is limited until 2026-10-19 11:33 UTC (lane agy).", body)

    def test_a_ticket_moved_off_the_limited_lane_no_longer_waits_on_it(self):
        body = self.running(self.LIMIT_LOOP + [
            {"body": "Not a failure (lane agy is at a limit): the factory moved this ticket to lane "
                     "codex, in its difficulty tier.", "created_at": "2026-10-18T13:30:00Z"}], state="briefed")
        self.assertNotIn("Waiting:", body)

    ESCALATE = {"body": "<!-- loom:escalate v1 -->\nEscalated to rung 3 of 6: gpt-6 on lane codex.\n"
                        "Reason: limit on agy (attempts 3, tried gemini-4-argon-high).\nRung: 3 gpt-6 codex",
                "created_at": "2026-10-18T19:19:00Z"}

    def test_an_escalated_ticket_no_longer_waits_on_the_old_lane_and_names_the_new_one(self):
        body = self.running(self.LIMIT_LOOP + [self.ESCALATE], state="briefed")
        self.assertNotIn("Waiting:", body)
        self.assertNotIn("lane agy is", body)
        self.assertIn("Escalated to rung 3 of 6: gpt-6 on lane codex. Next: lane codex claims it.", body)

    def test_the_path_shows_the_escalation_as_its_own_step(self):
        from dashboard.ticket import path_steps
        steps = path_steps(dict(RECORD, state="briefed"), self.LIMIT_LOOP + [self.ESCALATE], [])
        self.assertEqual([s["who"] for s in steps], ["wait", "brain", "next"], steps)
        self.assertEqual(steps[1]["text"], "Escalated to rung 3 of 6: gpt-6 on lane codex.")
        self.assertEqual(steps[1]["meta"], "limit on agy (attempts 3, tried gemini-4-argon-high).")

    def test_a_claim_on_the_new_lane_after_an_escalation_is_live(self):
        body = self.running(self.LIMIT_LOOP + [self.ESCALATE, {
            "body": "Claimed for lane codex (body sha256 a1fc29bcee5e)", "created_at": "2026-10-18T19:21:00Z"}])
        self.assertIn("working... streaming from codex", body)

    def test_a_running_ticket_whose_last_attempt_failed_says_so(self):
        body = self.running([
            {"body": "Claimed for lane codex (body sha256 0123456789ab)", "created_at": "2026-10-14T10:00:00Z"},
            {"body": "Lane codex failed: [work] validation failed: the check exited 2 at abc",
             "created_at": "2026-10-14T10:20:00Z"}])
        self.assertNotIn("working... streaming", body)
        self.assertIn("The last attempt failed on lane codex: [work] validation failed", body)

    def test_a_claim_with_no_outcome_yet_is_live(self):
        body = self.running(self.LIMIT_LOOP + [
            {"body": "Claimed for lane agy (body sha256 a1fc29bcee5e)", "created_at": "2026-10-18T14:01:09Z"}])
        self.assertIn("working... streaming from agy", body)
        self.assertIn('data-running="true"', body)
        self.assertNotIn("Waiting:", body)

    def test_a_claimed_unfinished_run_names_lane_model_claim_time_and_step(self):
        record = dict(RECORD, state="running", lane="agy", model="gemini-4-argon-high")
        detail = {"comments": [{"body": "Claimed for lane agy (body sha256 a1fc29bcee5e)",
                                "created_at": "2026-10-24T17:21:00Z"}],
                  "events": [], "errors": {}, "step": "agent"}
        body = render(record, [], None, [], detail=detail)
        self.assertNotIn("No run yet", body)
        self.assertIn("lane agy / gemini-4-argon-high / claimed 17:21 UTC / agent", body)
        self.assertIn('data-running="true"', body)
        self.assertNotIn("No transcript yet", body)

    def test_repeated_claims_handed_back_at_a_limit_are_one_path_row(self):
        from dashboard.ticket import path_steps
        steps = path_steps(dict(RECORD, state="running"), self.LIMIT_LOOP, [])
        self.assertEqual(len(steps), 2, steps)
        self.assertEqual(steps[0]["who"], "wait")
        self.assertEqual(steps[0]["text"], "Lane agy claimed it and handed it back: limit: requested model "
                                           "gemini-4-argon-high limited until 2026-10-19T11:33:47Z.")
        self.assertEqual(steps[0]["meta"], "3 times, 13:10 to 13:17 UTC")
        self.assertEqual(steps[0]["ts"], "2026-10-18T13:10:07Z")

    def test_the_pr_pane_shows_checks_files_threads_attempts_and_proof(self):
        from dashboard import ticket_data
        detail = ticket_data.TicketDetails(FakeGitHub()).get("example-owner/sample", 12)
        runs = [{"id": 5, "attempt": 1, "outcome": "failed"}, {"id": 6, "attempt": 2, "outcome": "done"}]
        body = render(RECORD, runs, 6, [], detail=detail)
        self.assertIn("PR #13</a> / merged / checks", body)
        self.assertIn('<span class="tone-bad">failure</span>', body)
        self.assertIn("+24", body)
        self.assertIn("medium / open", body)
        self.assertIn("2 attempts: 1 done, 1 failed. What the failures had in common: validation failed (1).", body)
        self.assertIn("Proof / waiting", body)
        self.assertNotIn("no linked pull request", body)

    def test_the_ticket_page_reads_github_through_the_store_client(self):
        class State:
            data = {"example-owner/sample": [dict(RECORD)]}
            transcripts = transcripts.Store(transcripts.MemoryBackend())
            gh = FakeGitHub()
            stale = False
            def response(self):
                return {"tickets": {}, "pull_requests": []}

        class Handler:
            path = "/t/example-owner/sample/12"
            def send_page(self, body, code=200):
                self.result = (code, body)
        handler = Handler()
        _dispatch_get(handler, State())
        self.assertEqual(handler.result[0], 200)
        self.assertIn("PR #13", handler.result[1])
        self.assertIn("Lane codex claimed it.", handler.result[1])


if __name__ == "__main__":
    unittest.main()
