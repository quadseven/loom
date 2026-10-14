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
        self.assertEqual(body.count('class="attempt-chip"'), 2)
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


if __name__ == "__main__":
    unittest.main()
