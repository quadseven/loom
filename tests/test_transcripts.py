"""A finished run as the dashboard's upload body (bus/transcripts.py).

Seam: transcripts.run_body / brain_body read the same files the viewer does
(through bus/sessions.py) and return the JSON body; nothing is sent here.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bus import transcripts  # noqa: E402

# Fake credentials in each provider's shape, built so no file holds one whole.
GITHUB = "ghp_" + "A1b2C3d4E5" * 4
ANTHROPIC = "sk-ant-api03-" + "x7Y" * 10
OPENAI = "sk-proj-" + "Q9w" * 10
AWS_ID = "AKIA" + "EXAMPLE7EXAMPLE7"
AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/" + "bPxRfiCYEXAMPLEKEY"
CLOUDFLARE = "Zy8" * 13 + "Q"
GIT_SHA = "0123456789abcdef" * 2 + "01234567"


def claude_rows(cwd=None):
    rows = [
        {"type": "user", "timestamp": "2026-10-13T10:00:00.000Z",
         "message": {"content": "# Brief: T1\nFix the README"}},
        {"type": "assistant", "timestamp": "2026-10-13T10:00:05.000Z", "message": {"content": [
            {"type": "thinking", "thinking": "Look first."},
            {"type": "text", "text": "Reading the README."},
            {"type": "tool_use", "id": "t1", "name": "Bash",
             "input": {"command": "cat /Users/someone/loom/worktrees/T1/README.md"}}]}},
        {"type": "user", "timestamp": "2026-10-13T10:00:06.000Z", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": "token=%s\nsha %s" % (GITHUB, GIT_SHA)}]}},
        {"type": "assistant", "timestamp": "2026-10-13T10:00:09.000Z", "message": {"content": [
            {"type": "text", "text": "Done."}]}},
    ]
    return [dict(r, cwd=cwd) for r in rows] if cwd else rows


class Fixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "loom"
        self.projects = self.root / "claude-projects"
        self.codex = self.root / "codex-sessions"

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def jsonl(self, path, rows):
        self.write(path, "".join(json.dumps(r) + "\n" for r in rows))

    def body(self, slot, ticket="T1", outcome="done"):
        return transcripts.run_body(self.home, slot, ticket, outcome, "model-x",
                                    self.projects, self.codex, issue="example/repo#1")


class Formats(Fixture):
    def test_a_claude_jsonl_run_becomes_text_tool_use_and_tool_result_events(self):
        self.jsonl(self.home / "logs/anthropic-2/T1/.claude/projects/x/s.jsonl", claude_rows())
        body = self.body("anthropic-2")
        self.assertEqual(
            [(e["role"], e["kind"]) for e in body["events"]],
            [("user", "text"), ("thinking", "text"), ("assistant", "text"),
             ("assistant", "tool_use"), ("tool", "tool_result"), ("assistant", "text")])
        self.assertEqual(body["events"][0]["ts"], "2026-10-13T10:00:00.000Z")
        self.assertEqual(body["events"][3]["text"], "Bash\ncommand: cat <path>")
        self.assertEqual({k: body[k] for k in ("ticket", "slot", "model", "outcome")},
                         {"ticket": "example/repo#1", "slot": "anthropic-2", "model": "model-x",
                          "outcome": "done"})

    def test_the_claude_lane_reads_the_worktree_session_in_claude_home(self):
        tree = self.home / "worktrees" / "T1"
        mangled = str(tree).replace("/", "-").replace(".", "-")
        self.jsonl(self.projects / mangled / "s.jsonl", claude_rows(cwd=str(tree)))
        kinds = [e["kind"] for e in self.body("claude")["events"]]
        self.assertIn("tool_use", kinds)

    def test_an_opencode_run_becomes_events(self):
        db = self.home / "logs/opencode-go-3/T1/.local/share/opencode/opencode.db"
        db.parent.mkdir(parents=True)
        con = sqlite3.connect(db)
        con.execute("create table message (id text, session_id text, time_created int, "
                    "time_updated int, data text)")
        con.execute("create table part (id text, message_id text, session_id text, "
                    "time_created int, time_updated int, data text)")
        con.execute("insert into message values ('m1', 's', 1, 1, ?)", (json.dumps({"role": "user"}),))
        con.execute("insert into message values ('m2', 's', 2, 2, ?)",
                    (json.dumps({"role": "assistant"}),))
        parts = [("p1", "m1", 1760349600000, {"type": "text", "text": "Fix it"}),
                 ("p2", "m2", 1760349601000, {"type": "tool", "tool": "bash", "state": {
                     "input": {"command": "ls"}, "output": "README.md"}}),
                 ("p3", "m2", 1760349602000, {"type": "text", "text": "Fixed."})]
        for pid, mid, at, data in parts:
            con.execute("insert into part values (?, ?, 's', ?, ?, ?)",
                        (pid, mid, at, at, json.dumps(data)))
        con.commit()
        con.close()
        events = self.body("opencode-go-3")["events"]
        self.assertEqual([(e["role"], e["kind"], e["text"]) for e in events],
                         [("user", "text", "Fix it"), ("assistant", "tool_use", "bash\ncommand: ls"),
                          ("tool", "tool_result", "README.md"), ("assistant", "text", "Fixed.")])
        self.assertEqual(events[0]["ts"], "2025-10-13T10:00:00Z")

    def test_an_opencode_2_run_becomes_events(self):
        db = self.home / "logs/opencode-go-3/T1/.local/share/opencode/opencode.db"
        db.parent.mkdir(parents=True)
        con = sqlite3.connect(db)
        con.execute("create table session_message (id text, session_id text, type text, "
                    "seq int, data text)")
        rows = [("user", {"text": "Fix it"}),
                ("assistant", {"content": [{"type": "text", "text": "Fixed."}]})]
        for seq, (kind, data) in enumerate(rows):
            con.execute("insert into session_message values (?, 's', ?, ?, ?)",
                        ("r%d" % seq, kind, seq, json.dumps(data)))
        con.commit()
        con.close()
        self.assertEqual([(e["role"], e["text"]) for e in self.body("opencode-go-3")["events"]],
                         [("user", "Fix it"), ("assistant", "Fixed.")])

    def test_a_plain_log_run_is_its_last_attempt_as_one_log_event(self):
        self.write(self.home / "logs/zen/T1.log",
                   "[2026-10-12T09:00:00Z] T1 start, lane zen\nold attempt\n"
                   "[2026-10-13T09:00:00Z] T1 start, lane zen\nagent printed this\n"
                   "[2026-10-13T09:05:00Z] T1 failed: [work] tests failed\n")
        body = self.body("zen", outcome="failed")
        self.assertEqual([(e["role"], e["kind"]) for e in body["events"]], [("runner", "log")])
        self.assertNotIn("old attempt", body["events"][0]["text"])
        self.assertIn("agent printed this", body["events"][0]["text"])
        self.assertEqual(body["started_at"], "2026-10-13T09:00:00Z")
        self.assertIsNotNone(body["finished_at"])

    def test_a_transcript_run_also_carries_its_log_after_the_session(self):
        self.jsonl(self.home / "logs/anthropic/T1/.claude/projects/x/s.jsonl", claude_rows())
        self.write(self.home / "logs/anthropic/T1.log", "[2026-10-13T09:00:00Z] T1 start, lane anthropic\n")
        self.assertEqual(self.body("anthropic")["events"][-1]["kind"], "log")

    def test_an_unsafe_ticket_name_is_refused(self):
        with self.assertRaises(ValueError):
            self.body("anthropic", ticket="../x")


class Scrubbing(Fixture):
    def test_secrets_and_local_paths_are_scrubbed_from_every_event(self):
        secrets = [GITHUB, ANTHROPIC, OPENAI, AWS_ID, AWS_SECRET, CLOUDFLARE, "Bearer abcdefgh12345"]
        rows = [{"type": "user", "message": {"content": "keys: " + " ".join(secrets)}},
                {"type": "assistant", "message": {"content": [{"type": "text", "text":
                 "see /Users/someone/loom and -Users-someone-loom-brain"}]}}]
        self.jsonl(self.home / "logs/anthropic/T1/.claude/projects/x/s.jsonl", rows)
        self.write(self.home / "logs/anthropic/T1.log", "pushed with %s from /home/someone\n" % GITHUB)
        raw = json.dumps(self.body("anthropic"))
        for secret in secrets + ["/Users/someone", "/home/someone", "-Users-someone"]:
            self.assertNotIn(secret.split()[-1], raw)
        self.assertEqual(raw.count("[redacted]"), len(secrets) + 1)

    def test_a_git_sha_and_ordinary_words_survive(self):
        text = "commit %s in owner/name, see docs/LANES.md; my-home-page" % GIT_SHA
        self.assertEqual(transcripts.scrub(text), text)

    def test_the_aws_secret_fixture_has_the_real_shape(self):
        self.assertEqual(len(AWS_SECRET), 40)
        self.assertEqual(transcripts.scrub("key " + AWS_SECRET), "key [redacted]")


class Limits(Fixture):
    def test_each_event_is_capped_and_a_log_keeps_its_tail(self):
        rows = [{"type": "user", "message": {"content": "a" * 50000 + "END"}}]
        self.jsonl(self.home / "logs/anthropic/T1/.claude/projects/x/s.jsonl", rows)
        self.write(self.home / "logs/anthropic/T1.log", "START" + "b" * 50000 + "\nT1 failed: why\n")
        events = self.body("anthropic")["events"]
        for e in events:
            self.assertLessEqual(len(e["text"]), transcripts.EVENT_MAX)
        self.assertTrue(events[0]["text"].endswith("[cut]"))
        self.assertTrue(events[-1]["text"].endswith("T1 failed: why\n"))
        self.assertNotIn("START", events[-1]["text"])

    def test_the_body_is_capped_dropping_the_oldest_tool_results_first(self):
        rows = []
        for i in range(600):
            rows.append({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "step %d" % i},
                {"type": "tool_use", "id": "t%d" % i, "name": "Bash", "input": {"command": "ls"}}]}})
            rows.append({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t%d" % i, "content": "%d " % i + "z" * 19000}]}})
        self.jsonl(self.home / "logs/anthropic/T1/.claude/projects/x/s.jsonl", rows)
        body = self.body("anthropic")
        self.assertLessEqual(len(json.dumps(body).encode()), transcripts.BODY_MAX)
        results = [e for e in body["events"] if e["kind"] == "tool_result"]
        self.assertTrue(results)
        self.assertLess(len(results), 600)
        self.assertTrue(results[-1]["text"].startswith("599 "))
        self.assertFalse(any(e["text"].startswith("0 ") for e in results))
        # Every text and tool call is kept; only results went.
        self.assertEqual(sum(e["kind"] == "text" for e in body["events"]), 600)
        self.assertEqual(sum(e["kind"] == "tool_use" for e in body["events"]), 600)


class Brain(Fixture):
    def test_a_brain_tick_is_the_sessions_written_during_it(self):
        work = self.home / "brain"
        project = self.projects / str(work).replace("/", "-").replace(".", "-")
        self.jsonl(project / "before.jsonl", [dict(r, cwd=str(work)) for r in
                                              [{"type": "user", "message": {"content": "old tick"}}]])
        self.jsonl(project / "tick.jsonl", claude_rows(cwd=str(work)))
        os.utime(project / "before.jsonl", (1000, 1000))
        os.utime(project / "tick.jsonl", (2000, 2000))
        body = transcripts.brain_body(self.home, 1500, 2100, "ok", "model-x", self.projects)
        self.assertEqual((body["slot"], body["ticket"], body["outcome"]),
                         ("brain", "brain/1970-01-01T00:25:00Z", "ok"))
        texts = [e["text"] for e in body["events"]]
        self.assertIn("Done.", texts)
        self.assertNotIn("old tick", texts)


if __name__ == "__main__":
    unittest.main()


class TicketRef(Fixture):
    """The dashboard accepts only owner/name#N (or brain/<time>) as a ticket:
    on 2026-10-14 every upload answered 400 "invalid ticket" because the body
    carried the brief's file name."""

    def test_the_issue_line_is_the_ticket(self):
        self.assertEqual(transcripts.ticket_ref("anything", "example-owner/sample-repo#931"),
                         "example-owner/sample-repo#931")

    def test_the_brief_name_is_the_fallback_also_for_a_retry_and_a_dashed_repo(self):
        self.assertEqual(transcripts.ticket_ref("example.loom-229"), "example/loom#229")
        self.assertEqual(transcripts.ticket_ref("example.sample-repo-931-r3"),
                         "example/sample-repo#931")

    def test_a_name_with_no_ticket_is_refused(self):
        with self.assertRaises(ValueError):
            transcripts.ticket_ref("T1", "not a ref")

    def test_a_run_body_carries_the_ref_and_a_start_even_without_a_start_line(self):
        self.write(self.home / "briefs/codex/done/example.loom-7.md", "# Brief\n")
        body = transcripts.run_body(self.home, "codex", "example.loom-7", "done", "m",
                                    self.projects, self.codex)
        self.assertEqual(body["ticket"], "example/loom#7")
        self.assertTrue(body["started_at"] and body["finished_at"])
        self.assertEqual(body["started_at"], body["finished_at"])


class EventShape(Fixture):
    def test_an_event_with_no_time_sends_an_empty_string(self):
        events = transcripts.convert([{"kind": "log", "ts": None, "text": "runner line"}])
        self.assertEqual(events[0]["ts"], "")
        self.assertTrue(all(isinstance(e[k], str) for e in events for k in ("ts", "role", "kind", "text")))

    def test_a_brief_with_no_model_says_not_recorded(self):
        self.write(self.home / "briefs/codex/done/example.loom-8.md", "# Brief\n")
        body = transcripts.run_body(self.home, "codex", "example.loom-8", "done", "",
                                    self.projects, self.codex)
        self.assertEqual(body["model"], "not recorded")
