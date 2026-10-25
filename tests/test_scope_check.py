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

    def test_a_new_top_level_module_the_change_imports_is_in_scope(self):
        # A repo that keeps its modules at the top level: a lane's new module
        # was refused in 5 runs on 2026-10-24 because no brief can name it
        # ahead of the agent (loom#1705).
        self.write("app.py", "import os\n")
        self.write("notes.txt", "x\n")
        self.base = self.commit("flat")
        self.write("app.py", "import os\nfrom helper_server import serve\n")
        self.write("helper_server.py", "def serve():\n    pass\n")
        self.write("other_feed.py", "z = 1\n")
        self.write("README.md", "readme\nSee other_feed.py.\n")
        self.commit("work")
        self.assertEqual(self.stray(), [])

    def test_only_an_edited_tracked_file_vouches_for_a_new_top_level_file(self):
        self.write("app.py", "import os\n")
        self.base = self.commit("flat")
        # New files naming each other, a standard-library name, an auto-run
        # name, a hidden name and a name inside a longer one all stay stray.
        self.write("first_new.py", "import second_new\n")
        self.write("second_new.py", "import first_new\n")
        self.write("json.py", "x = 1\n")
        self.write("conftest.py", "x = 1\n")
        self.write(".hidden.py", "x = 1\n")
        self.write("a.py", "x = 1\n")
        self.write("app.py", "import os\nimport json\nimport conftest\n"
                             "# .hidden.py data.py\n")
        self.commit("work")
        self.assertEqual(sorted(self.stray()), [".hidden.py", "a.py", "conftest.py",
                                                "first_new.py", "json.py", "second_new.py"])

    def test_a_new_top_level_file_nothing_changed_wires_in_stays_stray(self):
        self.write("app.py", "import os\n")
        self.base = self.commit("flat")
        self.write("scratch_server.py", "x = 1\n")
        self.write("app.py", "import os\n# scratch_server is not imported\n")
        self.write("unused.txt", "x\n")
        self.write("tool.sh", "echo\n")
        self.write("bus/station.py", "x = 1\n# see tool.sh\n")
        self.commit("work")
        self.assertEqual(sorted(self.stray()), ["scratch_server.py", "tool.sh", "unused.txt"])

    def test_scratch_rules_only_select_new_unnamed_files(self):
        self.write("test_debug.py", "print('scratch')\n")
        self.write("module_debug.txt", "scratch\n")
        self.write("artifact.o", "object\n")
        self.write("build_test/result.o", "object\n")
        self.write("nested/result.o", "object\n")
        self.write(".loom/test.txt", "probe\n")
        self.write(".loom/validation.conf", "check: true\n")
        self.write(".github/workflows/extra.yml", "on: push\n")
        self.commit("work")
        found = scope_check.scratch_files(str(self.tree), self.base, "HEAD", "")
        self.assertEqual(sorted(found), [".loom/test.txt", "artifact.o",
                                         "build_test/result.o", "module_debug.txt",
                                         "nested/result.o", "test_debug.py"])

    def test_build_output_tool_temp_files_and_patches_are_scratch(self):
        # Ten lane commits on 2026-10-21/22 were refused for a Gradle daemon
        # cache, a build/ output, a shell's here-document temp file or a patch
        # file the agent left behind; each cost the run.
        self.write(".gradle/daemon/8.13/daemon-1.out.log", "log\n")
        self.write("build/WindowContaminationTest", "out\n")
        self.write("sh-thd-3211296841", "tmp\n")
        self.write("T762.patch", "--- a\n+++ b\n")
        self.write("IMPLEMENTATION_SUMMARY.md", "notes\n")
        self.write("redistribute_epics.py", "pass\n")
        self.commit("work")
        found = scope_check.scratch_files(str(self.tree), self.base, "HEAD", "")
        self.assertEqual(sorted(found), [".gradle/daemon/8.13/daemon-1.out.log", "T762.patch",
                                         "build/WindowContaminationTest", "sh-thd-3211296841"])

    def test_a_build_folder_the_base_tracks_is_not_scratch(self):
        self.write("build/keep.txt", "tracked\n")
        self.base = self.commit("base with build")
        self.write("build/new.txt", "new\n")
        self.commit("work")
        self.assertEqual(scope_check.scratch_files(str(self.tree), self.base, "HEAD", ""), [])

    def test_referenced_top_level_test_file_is_kept(self):
        self.write("test_probe.py", "print('probe')\n")
        self.write("README.md", "Run test_probe.py to verify this change.\n")
        self.commit("work")
        self.assertEqual(scope_check.scratch_files(str(self.tree), self.base, "HEAD", ""), [])

    def test_referenced_scratch_file_with_glob_character_is_kept(self):
        self.write("test_reference.py", "Run test_*.py to verify this change.\n")
        self.base = self.commit("reference")
        self.write("test_*.py", "print('work')\n")
        self.commit("work")
        self.assertEqual(scope_check.scratch_files(str(self.tree), self.base, "HEAD", ""), [])
        self.assertEqual(self.stray(), ["test_*.py"])

    def test_scratch_file_with_newline_in_name_is_kept_for_scope_check(self):
        self.write("test_\nprobe.py", "print('scratch')\n")
        self.commit("work")
        self.assertEqual(scope_check.scratch_files(str(self.tree), self.base, "HEAD", ""), [])
        self.assertEqual(self.stray(), ["test_\nprobe.py"])

    def test_scratch_file_with_carriage_return_in_name_is_kept_for_scope_check(self):
        self.write("test_\rprobe.py", "print('scratch')\n")
        self.commit("work")
        self.assertEqual(scope_check.scratch_files(str(self.tree), self.base, "HEAD", ""), [])
        self.assertEqual(self.stray(), ["test_\rprobe.py"])

    def test_a_new_file_in_a_tracked_hidden_subdirectory_is_in_scope(self):
        # A test beside a tracked .github script was refused (2026-10-19).
        self.write(".github/scripts/deploy.py", "x = 1\n")
        self.write(".github/scripts/tests/test_deploy.py", "pass\n")
        self.base = self.commit("scripts")
        self.write(".github/scripts/tests/test_multiarch.py", "pass\n")
        self.write(".github/scripts/helper.py", "y = 2\n")
        self.write(".github/actions/new/action.yml", "runs: {}\n")
        self.write(".github/newdir/file.txt", "x\n")
        self.write(".loom/test.txt", "x\n")
        self.commit("work")
        self.assertEqual(sorted(self.stray()), [".github/actions/new/action.yml",
                                                ".github/newdir/file.txt", ".loom/test.txt"])

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
