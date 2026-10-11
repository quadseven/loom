"""The transcript viewer: every lane's session, rendered like a terminal chat.

Seam: transcripts.load_events(...) turns each CLI's own record into one event
list; render.page(...) turns events into HTML. Fixtures mirror the real formats
seen on the lane machine (Claude Code .jsonl, opencode 1.18 SQLite, codex
rollout .jsonl).
"""

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lanes" / "viewer"))

import render  # noqa: E402
import serve  # noqa: E402
import transcripts  # noqa: E402


def claude_lines():
    return [
        {"type": "user", "message": {"role": "user", "content": "# Brief: T1\nFix the README"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Reading the README."},
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "cat README.md"}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "# Title\nold line"}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t2", "name": "Edit", "input": {
                "file_path": "/w/README.md", "old_string": "old line", "new_string": "new line"}}]}},
        {"type": "attachment", "noise": True},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Done <script>alert(1)</script>"}]}},
    ]


class Fixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.loom = self.root / "loom"
        self.claude_projects = self.root / "claude-projects"
        self.codex_sessions = self.root / "codex-sessions"

    def tearDown(self):
        self._tmp.cleanup()

    def write_jsonl(self, path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))


class LoadEvents(Fixture):
    def test_anthropic_lane_reads_its_archived_claude_transcript(self):
        self.write_jsonl(self.loom / "logs/anthropic/T1/.claude/projects/x/s.jsonl", claude_lines())
        events = transcripts.load_events("anthropic", "T1", self.loom, self.claude_projects, self.codex_sessions)
        kinds = [e["kind"] for e in events]
        self.assertEqual(kinds, ["user", "assistant", "tool", "tool", "assistant"])
        bash = events[2]
        self.assertEqual((bash["tool"], bash["input"]["command"]), ("Bash", "cat README.md"))
        self.assertIn("old line", bash["output"])

    def test_claude_lane_reads_the_worktree_project_in_claude_home(self):
        tree = self.loom / "worktrees" / "T1"
        mangled = str(tree).replace("/", "-").replace(".", "-")
        rows = [dict(r, cwd=str(tree)) for r in claude_lines()]  # real rows record their cwd
        self.write_jsonl(self.claude_projects / mangled / "s.jsonl", rows)
        events = transcripts.load_events("claude", "T1", self.loom, self.claude_projects, self.codex_sessions)
        self.assertEqual(events[0]["kind"], "user")

    def test_claude_lane_matches_sessions_by_cwd_not_just_the_mangled_name(self):
        # "T1.a" and "T1-a" mangle to the same project directory.
        mine = self.loom / "worktrees" / "T1.a"
        other = self.loom / "worktrees" / "T1-a"
        mangled = str(mine).replace("/", "-").replace(".", "-")
        rows_mine = [dict(r, cwd=str(mine)) for r in claude_lines()]
        rows_other = [{"type": "user", "cwd": str(other), "message": {"content": "someone else's brief"}}]
        self.write_jsonl(self.claude_projects / mangled / "a.jsonl", rows_other)
        self.write_jsonl(self.claude_projects / mangled / "b.jsonl", rows_mine)
        events = transcripts.load_events("claude", "T1.a", self.loom, self.claude_projects, self.codex_sessions)
        self.assertNotIn("someone else's brief", [e.get("text") for e in events])
        self.assertEqual(events[0]["kind"], "user")

    def test_a_corrupt_opencode_store_reads_as_no_transcript(self):
        db = self.loom / "logs/openrouter/T2/.local/share/opencode/opencode.db"
        db.parent.mkdir(parents=True)
        db.write_bytes(b"not sqlite at all")
        (self.loom / "logs/openrouter/T2.log").write_text("printed\n")
        events = transcripts.load_events("openrouter", "T2", self.loom, self.claude_projects, self.codex_sessions)
        self.assertEqual(events, [{"kind": "log", "text": "printed\n"}])

    def test_opencode_lane_reads_the_archived_sqlite_store(self):
        db = self.loom / "logs/openrouter/T1/.local/share/opencode/opencode.db"
        db.parent.mkdir(parents=True)
        con = sqlite3.connect(db)
        con.execute("create table message (id text, session_id text, time_created int, time_updated int, data text)")
        con.execute("create table part (id text, message_id text, session_id text, time_created int, time_updated int, data text)")
        con.execute("insert into message values ('m1','s',1,1,?)", (json.dumps({"role": "user"}),))
        con.execute("insert into message values ('m2','s',2,2,?)", (json.dumps({"role": "assistant"}),))
        rows = [("p1", "m1", 1, {"type": "text", "text": "Run ls"}),
                ("p2", "m2", 2, {"type": "reasoning", "text": "I will list"}),
                ("p3", "m2", 3, {"type": "tool", "tool": "bash", "state": {"input": {"command": "ls"}, "output": "README"}}),
                ("p4", "m2", 4, {"type": "step-finish"}),
                ("p5", "m2", 5, {"type": "text", "text": "Listed."})]
        for pid, mid, t, data in rows:
            con.execute("insert into part values (?,?,'s',?,?,?)", (pid, mid, t, t, json.dumps(data)))
        con.commit(); con.close()
        events = transcripts.load_events("openrouter", "T1", self.loom, self.claude_projects, self.codex_sessions)
        self.assertEqual([e["kind"] for e in events], ["user", "thinking", "tool", "assistant"])
        self.assertEqual(events[2]["output"], "README")

    def test_codex_lane_reads_the_rollout_whose_cwd_is_the_ticket_tree(self):
        tree = self.loom / "worktrees" / "T1"
        self.write_jsonl(self.codex_sessions / "2026/10/11/rollout-other.jsonl", [
            {"type": "session_meta", "payload": {"cwd": "/elsewhere"}}])
        self.write_jsonl(self.codex_sessions / "2026/10/11/rollout-mine.jsonl", [
            {"type": "session_meta", "payload": {"cwd": str(tree)}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "# Brief: T1"}]}},
            {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "input": "ls"}},
            {"type": "response_item", "payload": {"type": "custom_tool_call_output", "output": "README"}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Done."}]}}])
        events = transcripts.load_events("codex", "T1", self.loom, self.claude_projects, self.codex_sessions)
        self.assertEqual([e["kind"] for e in events], ["user", "tool", "assistant"])
        self.assertEqual(events[1]["output"], "README")

    def test_a_ticket_without_a_transcript_falls_back_to_its_log(self):
        log = self.loom / "logs/openrouter/T9.log"
        log.parent.mkdir(parents=True)
        log.write_text("agent printed this\n")
        events = transcripts.load_events("openrouter", "T9", self.loom, self.claude_projects, self.codex_sessions)
        self.assertEqual(events, [{"kind": "log", "text": "agent printed this\n"}])

    def test_unsafe_lane_or_ticket_names_are_refused(self):
        for lane, ticket in (("codex", "../x"), ("bogus", "T1"), ("codex", "T1/../../x")):
            with self.assertRaises(ValueError):
                transcripts.load_events(lane, ticket, self.loom, self.claude_projects, self.codex_sessions)


class RenderPage(unittest.TestCase):
    def events(self):
        return transcripts.claude_events([json.dumps(r) for r in claude_lines()])

    def test_tool_calls_render_with_their_input_and_output(self):
        html = render.page("T1", "anthropic", self.events())
        self.assertIn("Bash(cat README.md)", html)
        self.assertIn("old line", html)

    def test_edit_calls_render_as_a_diff(self):
        html = render.page("T1", "anthropic", self.events())
        self.assertIn('<div class="del">- old line</div>', html)
        self.assertIn('<div class="add">+ new line</div>', html)

    def test_agent_text_is_escaped(self):
        html = render.page("T1", "anthropic", self.events())
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)


class Server(Fixture):
    def test_index_lists_every_brief_with_its_lane_and_status(self):
        for rel in ("briefs/codex/done/T1.md", "briefs/anthropic/failed/T2.md", "briefs/openrouter/T3.md"):
            p = self.loom / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("brief")
        got = {(r["lane"], r["ticket"], r["status"]) for r in serve.rows(self.loom)}
        self.assertEqual(got, {("codex", "T1", "done"), ("anthropic", "T2", "failed"),
                               ("openrouter", "T3", "queued")})

    def test_only_localhost_host_headers_are_served(self):
        self.assertTrue(serve.allowed_host("localhost:8765", 8765))
        self.assertTrue(serve.allowed_host("127.0.0.1:8765", 8765))
        self.assertFalse(serve.allowed_host("evil.example:8765", 8765))
        self.assertFalse(serve.allowed_host("", 8765))
        self.assertTrue(serve.allowed_host(" LocalHost:8765 ", 8765))


if __name__ == "__main__":
    unittest.main()
