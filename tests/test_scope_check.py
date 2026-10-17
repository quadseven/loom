"""bus/scope_check.py on real git repos: which added files are stray."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bus import scope_check  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def git(tree, *args):
    return subprocess.run(["git", "-C", str(tree), "-c", "user.name=t",
                           "-c", "user.email=t@example.invalid", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


class ScopeCheckTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tree = Path(self._tmp.name) / "repo"
        git(self._tmp.name, "init", "-q", "-b", "main", str(self.tree))
        self.write("README.md", "readme\n")
        self.write("bus/station.py", "x = 1\n")
        self.write("tests/test_station.py", "pass\n")
        self.write(".github/workflows/ci.yml", "on: push\n")
        self.base = self.commit("base")

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, path, text):
        target = self.tree / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def commit(self, message):
        git(self.tree, "add", "-A")
        git(self.tree, "commit", "-qm", message)
        return git(self.tree, "rev-parse", "HEAD")

    def stray(self, brief="", also=()):
        return scope_check.stray_files(str(self.tree), self.base, "HEAD", brief, also)

    def test_new_files_under_tracked_top_level_directories_are_in_scope(self):
        self.write("bus/new_module.py", "y = 2\n")
        self.write("tests/test_new_module.py", "pass\n")
        self.write("bus/sub/deep.py", "z = 3\n")
        self.commit("work")
        self.assertEqual(self.stray(), [])

    def test_top_level_new_and_hidden_directory_files_are_stray(self):
        self.write(".agents/hooks.json", "{}\n")
        self.write(".github/workflows/extra.yml", "on: push\n")
        self.write("bin/test.txt", "x\n")
        self.write("test_hook.sh", "echo\n")
        self.commit("work")
        self.assertEqual(sorted(self.stray()), [".agents/hooks.json", ".github/workflows/extra.yml",
                                                "bin/test.txt", "test_hook.sh"])

    def test_a_file_or_directory_the_brief_names_is_in_scope(self):
        self.write("test_hook.sh", "echo\n")
        self.write(".github/workflows/extra.yml", "on: push\n")
        self.write("bin/tool", "x\n")
        self.commit("work")
        brief = ("Add `test_hook.sh`. The lint job lives in .github/workflows/ci.yml.\n"
                 "Put the helper under bin/.\n")
        self.assertEqual(self.stray(brief), [])

    def test_a_name_inside_a_longer_path_or_word_does_not_count(self):
        self.write("bin/tool", "x\n")
        self.write("hook.sh", "echo\n")
        self.write("fix.txt", "fixed\n")
        self.commit("work")
        brief = "Run /usr/bin/env, see old/hook.sh, and keep fix.txt.bak around.\n"
        self.assertEqual(sorted(self.stray(brief)), ["bin/tool", "fix.txt", "hook.sh"])

    def test_a_name_at_the_end_of_a_sentence_counts(self):
        self.write("fix.txt", "fixed\n")
        self.commit("work")
        self.assertEqual(self.stray("Change fix.txt."), [])

    def test_edits_and_deletions_of_tracked_files_are_never_stray(self):
        self.write("README.md", "changed\n")
        (self.tree / ".github/workflows/ci.yml").unlink()
        self.commit("work")
        self.assertEqual(self.stray(), [])

    def test_a_rename_out_of_scope_counts_as_an_added_file(self):
        git(self.tree, "mv", "bus/station.py", "station.py")
        self.commit("work")
        self.assertEqual(self.stray(), ["station.py"])

    def test_files_an_extra_revision_holds_are_not_the_agents(self):
        git(self.tree, "checkout", "-q", "-b", "upstream")
        self.write("main.txt", "main moved\n")
        self.write("newdir/x.py", "x\n")
        upstream = self.commit("main moves")
        git(self.tree, "checkout", "-q", self.base)
        git(self.tree, "merge", "-q", "--no-edit", upstream)
        self.write("newdir/y.py", "y\n")
        self.write("stray.txt", "s\n")
        self.commit("work")
        self.assertEqual(self.stray(also=[upstream]), ["stray.txt"])
        self.assertEqual(sorted(self.stray()), ["main.txt", "newdir/x.py", "newdir/y.py", "stray.txt"])

    def test_script_prints_printable_paths_and_fails_loudly_on_a_bad_revision(self):
        self.write("bad\tname.txt", "x\n")
        self.commit("work")
        brief = Path(self._tmp.name) / "brief.md"
        brief.write_text("Nothing named.\n")
        run = [sys.executable, "-B", "-m", "bus.scope_check", str(self.tree)]
        ok = subprocess.run(run + [self.base, "HEAD", str(brief)], cwd=ROOT,
                            capture_output=True, text=True)
        self.assertEqual((ok.returncode, ok.stdout), (0, "bad?name.txt\n"))
        bad = subprocess.run(run + ["0" * 40, "HEAD", str(brief)], cwd=ROOT,
                             capture_output=True, text=True)
        self.assertEqual((bad.returncode, bad.stdout), (2, ""))
        self.assertIn("scope_check:", bad.stderr)
        missing = subprocess.run(run + [self.base, "HEAD", str(brief) + ".gone"], cwd=ROOT,
                                 capture_output=True, text=True)
        self.assertEqual(missing.returncode, 2)


if __name__ == "__main__":
    unittest.main()
