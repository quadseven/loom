"""Finished runs become the dashboard's upload body (bus/transcripts.py), read
through the one shared parser (bus/sessions.py). Fixtures mirror the real
formats on the lane machine: Claude Code .jsonl, opencode 1.18 SQLite, and the
runner's lane log."""

import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lanes" / "viewer"))

from bus import sessions, transcripts  # noqa: E402

# Key shapes built in pieces, so no scanner mistakes the fixtures for keys.
GITHUB = "gh" + "p_" + "A1b2C3" * 6
ANTHROPIC = "sk-" + "ant-api03-" + "x" * 40
OPENAI = "sk-" + "proj-" + "Y" * 40
AWS_ID = "AK" + "IA" + "ABCDEFGHIJKLMNOP"
AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/" + "bPxRfiCYEXAMPLEKEY"
CLOUDFLARE = "Zz9" * 14


def claude_rows(tree):
    return [
        {"type": "user", "timestamp": "2030-01-01T10:00:00.000Z", "cwd": tree,
         "message": {"role": "user", "content": "# Brief: T1\nFix the README"}},
        {"type": "assistant", "timestamp": "2030-01-01T10:00:05.000Z", "cwd": tree,
         "message": {"role": "assistant", "model": "claude-sonnet-5-5", "content": [
             {"type": "text", "text": "Reading %s/README.md" % tree},
             {"type": "tool_use", "id": "t1", "name": "Bash",
              "input": {"command": "export GH_TOKEN=%s; cat README.md" % GITHUB}}]}},
        {"type": "user", "timestamp": "2030-01-01T10:00:06.000Z", "cwd": tree,
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "t1",
              "content": "ANTHROPIC_API_KEY=%s\nsee /home/example/notes.txt" % ANTHROPIC}]}},
        {"type": "assistant", "timestamp": "2030-01-01T10:00:09.000Z", "cwd": tree,
         "message": {"role": "assistant", "content": [{"type": "text", "text": "Done."}]}},
    ]


class Fixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.loom = self.root / "loom"
        self.user = self.root / "user"

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def jsonl(self, path, rows):
        self.write(path, "".join(json.dumps(r) + "\n" for r in rows))

    def log(self, slot, ticket, text):
        self.write(self.loom / "logs" / slot / (ticket + ".log"), text)

    def body(self, slot, ticket, outcome="done", brief="Model: claude-sonnet-5-5\n"):
        return transcripts.run_body(str(self.loom), slot, ticket, outcome, brief,
                                    str(self.user), 1893492000)

    def all_text(self, body):
        return json.dumps(body)


class ClaudeRun(Fixture):
    def setUp(self):
        super().setUp()
        tree = str(self.loom / "worktrees" / "T1")
        self.jsonl(self.loom / "logs/anthropic-2/T1/.claude/projects/p/s.jsonl", claude_rows(tree))
        self.log("anthropic-2", "T1", "[2030-01-01T09:59:58Z] T1 start, lane anthropic-2, model claude-sonnet-5-5\n"
                 "Done.\n[2030-01-01T10:01:00Z] T1 done\n")

    def test_a_finished_claude_run_becomes_ordered_events(self):
        body = self.body("anthropic-2", "T1")
        self.assertEqual((body["ticket"], body["slot"], body["model"], body["outcome"]),
                         ("T1", "anthropic-2", "claude-sonnet-5-5", "done"))
        self.assertEqual((body["started_at"], body["finished_at"]),
                         ("2030-01-01T09:59:58Z", "2030-01-01T10:01:00Z"))
        got = [(e["role"], e["kind"]) for e in body["events"]]
        self.assertEqual(got, [("user", "text"), ("assistant", "text"), ("assistant", "tool_use"),
                               ("tool", "tool_result"), ("assistant", "text"),
                               ("runner", "log"), ("runner", "log")])
        self.assertEqual(body["events"][3]["ts"], "2030-01-01T10:00:06.000Z")
        self.assertTrue(all(set(e) == {"ts", "role", "kind", "text"} for e in body["events"]))
        self.assertTrue(all(e["ts"] for e in body["events"]))

    def test_secrets_and_local_paths_are_scrubbed(self):
        body = self.body("anthropic-2", "T1")
        text = self.all_text(body)
        for secret in (GITHUB, ANTHROPIC, str(self.loom), "/home/example"):
            self.assertNotIn(secret, text)
        self.assertIn("Reading ./README.md", body["events"][1]["text"])
        self.assertIn("GH_TOKEN=[redacted]", body["events"][2]["text"])
        self.assertIn("ANTHROPIC_API_KEY=[redacted]", body["events"][3]["text"])
        self.assertIn("see <path>", body["events"][3]["text"])

    def test_the_model_falls_back_to_the_runner_log(self):
        self.assertEqual(self.body("anthropic-2", "T1", brief="no model line\n")["model"],
                         "claude-sonnet-5-5")


class OpencodeRun(Fixture):
    def test_an_opencode_run_becomes_events(self):
        db = self.loom / "logs/openrouter/T2/.local/share/opencode/opencode.db"
        db.parent.mkdir(parents=True)
        con = sqlite3.connect(db)
        con.execute("create table message (id text, session_id text, time_created int, time_updated int, data text)")
        con.execute("create table part (id text, message_id text, session_id text, time_created int, time_updated int, data text)")
        con.execute("insert into message values ('m1','s',1,1,?)", (json.dumps({"role": "user"}),))
        con.execute("insert into message values ('m2','s',2,2,?)", (json.dumps({"role": "assistant"}),))
        ms = 1893492000000
        for pid, mid, t, data in (
                ("p1", "m1", ms, {"type": "text", "text": "Run ls"}),
                ("p2", "m2", ms + 1000, {"type": "reasoning", "text": "I will list"}),
                ("p3", "m2", ms + 2000, {"type": "tool", "tool": "bash", "state": {
                    "input": {"command": "ls"}, "output": "README " + OPENAI,
                    "time": {"start": ms + 2000, "end": ms + 4000}}}),
                ("p4", "m2", ms + 5000, {"type": "text", "text": "Listed."})):
            con.execute("insert into part values (?,?,'s',?,?,?)", (pid, mid, t, t, json.dumps(data)))
        con.commit()
        con.close()
        body = self.body("openrouter", "T2", brief="Model: openrouter/x:free\n")
        got = [(e["role"], e["kind"]) for e in body["events"]]
        self.assertEqual(got, [("user", "text"), ("thinking", "text"), ("assistant", "tool_use"),
                               ("tool", "tool_result"), ("assistant", "text")])
        self.assertEqual(body["events"][0]["ts"], "2030-01-01T10:00:00Z")
        self.assertEqual(body["events"][3]["ts"], "2030-01-01T10:00:04Z")
        self.assertEqual(body["events"][3]["text"], "README [redacted]")
        self.assertEqual(body["model"], "openrouter/x:free")


class LogRun(Fixture):
    def test_a_run_with_only_a_log_becomes_log_events(self):
        self.log("agy", "T3", "[2030-01-01T10:00:00Z] T3 start, lane agy, model gemini-x\n"
                 "agent printed this\n[2030-01-01T10:05:00Z] T3 failed: agent exited 1\n")
        body = self.body("agy", "T3", outcome="failed", brief="")
        self.assertEqual([(e["ts"], e["role"], e["kind"]) for e in body["events"]],
                         [("2030-01-01T10:00:00Z", "runner", "log"),
                          ("2030-01-01T10:05:00Z", "runner", "log")])
        self.assertIn("agent printed this", body["events"][0]["text"])
        self.assertEqual((body["model"], body["outcome"]), ("gemini-x", "failed"))

    def test_a_run_without_any_record_has_no_body(self):
        self.assertIsNone(self.body("codex", "T4"))

    def test_the_viewer_and_the_upload_read_the_same_parser(self):
        import serve
        self.assertIs(serve.transcripts, sessions)


class Scrub(unittest.TestCase):
    def test_key_shapes_become_redacted(self):
        for raw in (GITHUB, "github_pat_" + "a" * 40, ANTHROPIC, OPENAI, AWS_ID,
                    "cfut_" + "q" * 40):
            self.assertEqual(transcripts.clean("key %s here" % raw), "key [redacted] here", raw)
        self.assertEqual(transcripts.clean("aws_secret_access_key = %s" % AWS_SECRET),
                         "aws_secret_access_key = [redacted]")
        self.assertEqual(transcripts.clean("CLOUDFLARE_API_TOKEN=%s" % CLOUDFLARE),
                         "CLOUDFLARE_API_TOKEN=[redacted]")
        self.assertEqual(transcripts.clean("Authorization: Bearer abcdefghijklmnopqrstuvwxyz"),
                         "Authorization: Bearer [redacted]")

    def test_ordinary_words_survive(self):
        text = "task-runner-for-the-loom-lanes and risk-assessment-of-everything"
        self.assertEqual(transcripts.clean(text), text)

    def test_the_worktree_becomes_dot_but_not_a_longer_name(self):
        self.assertEqual(transcripts.clean("/w/T1/a and /w/T10/b", tree="/w/T1"),
                         "./a and <path>")


class Limits(unittest.TestCase):
    def test_each_event_is_capped(self):
        out = transcripts.upload_events([{"kind": "assistant", "ts": "t", "text": "x" * 50000}])
        self.assertEqual(len(out[0]["text"]), transcripts.EVENT_MAX)
        self.assertTrue(out[0]["text"].endswith("[truncated]"))

    def test_the_body_is_capped_dropping_the_oldest_tool_results_first(self):
        ev = lambda kind, n: {"ts": "t", "role": "tool", "kind": kind, "text": str(n) * 1000}
        events = [ev("tool_result", 1), ev("text", 2), ev("tool_result", 3), ev("text", 4)]
        full = transcripts.make_body("T", "s", "m", "done", None, None, events)
        self.assertEqual(len(full["events"]), 4)
        limit = len(json.dumps(full)) - 500
        body = transcripts.fit(dict(full), limit=limit)
        self.assertEqual([e["text"][0] for e in body["events"]], ["2", "3", "4"])
        self.assertLessEqual(len(json.dumps(body)), limit)
        body = transcripts.fit(dict(full), limit=limit - 1500)
        self.assertEqual([e["text"][0] for e in body["events"]], ["2", "4"])
        body = transcripts.fit(dict(full), limit=limit - 2500)
        self.assertEqual([e["text"][0] for e in body["events"]], ["4"])

    def test_a_huge_run_fits_in_eight_megabytes(self):
        events = [{"kind": "tool", "ts": "t", "tool": "Bash", "input": {"command": "x"},
                   "output": "y" * 30000, "output_ts": "t"} for _ in range(600)]
        body = transcripts.make_body("T", "s", "m", "done", None, None,
                                     transcripts.upload_events(events))
        self.assertLessEqual(len(json.dumps(body)), transcripts.BODY_MAX)
        self.assertEqual(sum(e["kind"] == "tool_use" for e in body["events"]), 600)


class BrainTick(Fixture):
    def test_a_brain_tick_reads_its_new_sessions_and_its_log_lines(self):
        work = str(self.loom / "brain")
        project = sessions.claude_project(self.user / ".claude" / "projects", work)
        started = time.time() - 100
        old = project / "old.jsonl"
        self.jsonl(old, [{"type": "user", "message": {"content": "an earlier tick"}}])
        os.utime(old, (started - 3600, started - 3600))
        self.jsonl(project / "new.jsonl", [{"type": "user", "message": {"content": "this tick in %s/loom" % work}}])
        at = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
        self.write(self.loom / "logs" / "brain.log",
                   "[%s] brain: tick end (exit 0)\n[%s] brain: tick start (model m, scope all)\n"
                   "printed\n[%s] brain: tick end (exit 0)\n"
                   % (at(started - 3600), at(started), at(started + 50)))
        body = transcripts.brain_body(str(self.loom), str(self.user), started, started + 50,
                                      "done", "claude-opus-5-5")
        self.assertEqual(body["ticket"], "brain/" + at(started))
        self.assertEqual((body["slot"], body["model"]), ("brain", "claude-opus-5-5"))
        texts = [e["text"] for e in body["events"]]
        self.assertEqual(texts[0], "this tick in ./loom")
        self.assertNotIn("an earlier tick", texts)
        self.assertEqual(len(texts), 3)
        self.assertIn("printed", texts[1])


if __name__ == "__main__":
    unittest.main()
