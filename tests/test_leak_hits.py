"""bus/leak_hits.py: a public repo leak scan refusal named by rule and place."""

import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from bus import leak_hits

# A -U0 diff as the scanner reads it: line 7 adds a.txt line 3, line 9 is
# the header of a file whose own name hit, line 20 adds b.txt line 1.
DIFF = """diff --git a/a.txt b/a.txt
index 1111111..2222222 100644
--- a/a.txt
+++ b/a.txt
@@ -3 +3,2 @@
-old
+first
+second
diff --git a/flagged-name.txt b/flagged-name.txt
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/flagged-name.txt
@@ -0,0 +1 @@
+inside
diff --git a/b.txt b/b.txt
--- a/b.txt
+++ b/b.txt
@@ -0,0 +1 @@
+added
"""


def scanner_line(label, number, rule, shown):
    """One hit line in the real scanner's format."""
    return "  %s:%d  [%s] %s - why it is private" % (label, number, rule, shown)


class PlacesTest(unittest.TestCase):
    def test_an_added_line_maps_to_its_file_and_new_line(self):
        mapped = leak_hits.places(DIFF)
        self.assertEqual(mapped[7], ("a.txt", 3))
        self.assertEqual(mapped[8], ("a.txt", 4))
        self.assertEqual(mapped[20], ("b.txt", 1))

    def test_a_header_line_maps_to_its_path_alone(self):
        self.assertEqual(leak_hits.places(DIFF)[9], ("flagged-name.txt", None))

    def test_a_removed_or_hunk_line_maps_nowhere(self):
        mapped = leak_hits.places(DIFF)
        self.assertNotIn(6, mapped)
        self.assertNotIn(5, mapped)


class DescribeTest(unittest.TestCase):
    def describe(self, output):
        with mock.patch.object(leak_hits, "_git", return_value=DIFF):
            return leak_hits.summary(leak_hits.describe_diff(leak_hits.hits(output), "B..H"))

    def test_each_hit_is_named_by_rule_and_path_line_never_by_its_text(self):
        out = "\n".join([
            "BLOCKED: private infrastructure identifiers found (1 shape patterns, 2 deny-list terms)",
            "",
            scanner_line("B..H", 7, "deny-list", "<redacted, 6 chars>"),
            scanner_line("B..H", 20, "private-issue-ref", "'SECRETWORD'"),
        ])
        line = self.describe(out)
        self.assertEqual(line, "2 hits: [deny-list] a.txt:3, [private-issue-ref] b.txt:1")
        self.assertNotIn("SECRETWORD", line)
        self.assertNotIn("redacted", line)

    def test_a_file_whose_name_hit_is_never_named(self):
        out = "\n".join([
            scanner_line("B..H", 9, "deny-list", "<redacted, 4 chars>"),
            scanner_line("B..H", 15, "deny-list", "<redacted, 4 chars>"),
        ])
        line = self.describe(out)
        self.assertNotIn("flagged-name", line)
        self.assertEqual(line, "1 hit: [deny-list] a file whose name was flagged")

    def test_a_commit_message_hit_names_the_commit_and_line(self):
        out = scanner_line("0123456789ab message", 2, "deny-list", "<redacted, 4 chars>")
        self.assertEqual(self.describe(out), "1 hit: [deny-list] commit 0123456789ab message line 2")

    def test_the_same_hit_in_the_net_diff_and_a_commit_patch_is_named_once(self):
        out = "\n".join([
            scanner_line("B..H", 7, "deny-list", "<redacted, 4 chars>"),
            scanner_line("0123456789ab patch", 7, "deny-list", "<redacted, 4 chars>"),
        ])
        self.assertEqual(self.describe(out), "1 hit: [deny-list] a.txt:3")

    def test_more_than_five_hits_name_five_and_count_the_rest(self):
        out = "\n".join(scanner_line("B..H", n, "rule-%d" % n, "'x'") for n in range(1, 9))
        line = self.describe(out)
        self.assertEqual(line.count("[rule-"), 5)
        self.assertTrue(line.startswith("8 hits: "), line)
        self.assertTrue(line.endswith(", and 3 more in the lane log"), line)

    def test_output_with_no_hit_line_says_so(self):
        self.assertEqual(self.describe("FATAL: could not read deny-list from SSM"),
                         "the scanner named no hit line; its output is in the lane log")

    def test_a_text_hit_names_the_text_and_line_not_the_scanned_file(self):
        out = scanner_line("/some/run/dir/body.txt", 4, "deny-list", "<redacted, 4 chars>")
        line = leak_hits.summary(leak_hits.describe_text(leak_hits.hits(out), "PR body"))
        self.assertEqual(line, "1 hit: [deny-list] PR body line 4")


class GitTest(unittest.TestCase):
    def test_line_numbers_match_the_diff_the_scanner_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            def git(*args):
                return subprocess.run(["git", "-C", tmp, *args], capture_output=True, text=True,
                                      check=True).stdout.strip()
            git("init", "-q")
            git("-c", "user.name=t", "-c", "user.email=t@example.invalid",
                "commit", "-q", "--allow-empty", "-m", "base")
            base = git("rev-parse", "HEAD")
            Path(tmp, "src").mkdir()
            Path(tmp, "src", "x.py").write_text("one\ntwo\nthree\n")
            git("add", ".")
            git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "add")
            rng = "%s..HEAD" % base
            diff = git("diff", "-U0", rng).splitlines()
            number = diff.index("+three") + 1
            real = leak_hits._git
            out = io.StringIO()
            # The runner runs it in the tree; here git is pointed at it.
            with mock.patch.object(leak_hits, "_git", lambda args: real(["-C", tmp, *args])), \
                    mock.patch("sys.stdin", io.StringIO(scanner_line(rng, number, "deny-list", "<r>"))), \
                    redirect_stdout(out):
                leak_hits.main(["--diff", rng])
            self.assertEqual(out.getvalue().strip(), "1 hit: [deny-list] src/x.py:3")


if __name__ == "__main__":
    unittest.main()
