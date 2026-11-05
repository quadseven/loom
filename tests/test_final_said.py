"""bus/final_said.py picks what an agent said last, not what it printed last
(loom#1799)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bus import final_said  # noqa: E402

GIT_CONFIG = "[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n\tbare = false\n"


class FinalSaidTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)

    def write(self, name, text):
        path = self.root / name
        path.write_text(text)
        return str(path)

    def test_the_final_message_file_wins_over_the_tail(self):
        message = self.write("msg", "I stopped: the module is gone.\n")
        self.assertEqual(final_said.choose(message, None, GIT_CONFIG),
                         ("I stopped: the module is gone.", False))

    def test_the_last_opencode_text_event_wins_over_the_tail(self):
        events = self.write("events", "\n".join(json.dumps(e) for e in (
            {"type": "text", "part": {"text": "Let me read the config."}},
            {"type": "tool_use", "part": {"tool": "read"}},
            {"type": "text", "part": {"text": "Blocked: the test needs\nthe network."}},
            {"type": "step_finish"},
        )))
        self.assertEqual(final_said.choose(None, events, GIT_CONFIG),
                         ("Blocked: the test needs the network.", False))

    def test_a_missing_or_empty_message_falls_back_to_the_tail(self):
        empty = self.write("msg", "\n")
        tail = "I could not find the module."
        self.assertEqual(final_said.choose(empty, str(self.root / "none"), tail), (tail, False))

    def test_tool_output_alone_is_a_dump(self):
        for tail in (GIT_CONFIG,
                     "# the parser keeps one entry per line\nreturn entries",
                     "self.assertEqual(result.returncode, 0, result.stderr)",
                     "def main():\n    run()",
                     "assert len(rows) == 3"):
            with self.subTest(tail=tail):
                self.assertEqual(final_said.choose(None, None, tail), ("", True))

    def test_prose_after_a_dump_is_kept(self):
        tail = GIT_CONFIG + "The remote is read-only, so I could not push."
        self.assertEqual(final_said.choose(None, None, tail),
                         ("The remote is read-only, so I could not push.", False))

    def test_escape_sequences_are_removed(self):
        tail = "\x1b[1;33mPermission needed:\x1b[0m run \x1b]8;;x\x07make\x1b]8;;\x07 test?"
        said, dump = final_said.choose(None, None, tail)
        self.assertFalse(dump)
        self.assertEqual(said, "Permission needed: run make test?")

    def test_prose_is_not_read_as_tool_output(self):
        for line in ("Stopped: the fetch failed.", "I am blocked: open the file.",
                     "## Summary", "- the test needs the network", "    - a nested point",
                     "The feature is already implemented and all tests pass."):
            with self.subTest(line=line):
                self.assertFalse(final_said.looks_like_tool_output(line))

    def test_nothing_is_nothing(self):
        self.assertEqual(final_said.choose(None, None, "\n \n"), ("", False))

    def test_local_paths_read_path(self):
        said, _ = final_said.choose(None, None, "I am blocked: open /Users/someone/x denied.")  # leak-guard-allow: the case under test
        self.assertEqual(said, "I am blocked: open <path> denied.")


if __name__ == "__main__":
    unittest.main()
