"""A contract's `autoformat:` command runs on the lane's own changed files
after the scope and contract checks and before the final check, host or
remote, so a run whose only defect is formatting is not thrown away
(loom#3577)."""

import re
import subprocess
from pathlib import Path

from tests import test_run_lane as base

# A stand-in formatter: records its arguments, touches check.py (a file the
# lane never changes) and appends `formatted` to each file it is given. In
# fail mode it edits fix.txt and exits 1.
FAKE_FMT = """#!/bin/sh
printf '%s\\n' "$*" >> ARGS_LOG
echo touched >> check.py
if [ "MODE" = fail ]; then echo formatted >> fix.txt; exit 1; fi
for f in "$@"; do grep -qx formatted "$f" || echo formatted >> "$f"; done
"""

# A check that fails once the lane wrote `fixed` to fix.txt, until it is
# formatted (the base's fix.txt passes).
FMT_CHECK = """#!/bin/sh
grep -qx fixed fix.txt 2> /dev/null || exit 0
grep -qx formatted fix.txt
"""

CONTRACT = "interpreter: loom-test-python\nsubmodules: yes\ncheck: loom-test-python check.py\n"


class ContractAutoformatTest(base.LaneRunnerTest):
    def fake_tools(self, mode="ok"):
        self.args_log = Path(self._tmp.name) / "fmt-args"
        self.write_tool(self.bin / "sandbox-exec", '#!/bin/sh\nshift 2\nexec "$@"\n')
        self.write_tool(self.bin / "loom-fmt",
                        FAKE_FMT.replace("ARGS_LOG", str(self.args_log)).replace("MODE", mode))
        self.write_tool(self.bin / "loom-fmt-check", FMT_CHECK)

    def host_lane(self, contract, mode="ok", **env):
        self.fake_tools(mode)
        self.contract_repo(contract=contract)
        self.drop_brief()
        result = self.run_contract_lane(**env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.lane_log()

    def branch_file(self, path, ref="loom/T042-fix-the-thing"):
        return subprocess.run(["git", "-C", str(self.remote), "show", f"{ref}:{path}"],
                              capture_output=True, text=True, check=True).stdout

    def branch_subjects(self, ref="loom/T042-fix-the-thing"):
        return subprocess.run(["git", "-C", str(self.remote), "log", "--format=%s", ref],
                              capture_output=True, text=True, check=True).stdout

    def test_a_remote_check_run_checks_and_pushes_the_formatted_commit(self):
        self.fake_tools()
        _, dispatched = self.fake_station()
        log = self.run_remote(contract=CONTRACT + "autoformat: loom-fmt\n",
                              FAKE_GH_CHECKS=self.GREEN_CHECKS)
        self.assertNotIn("failed:", log)
        self.assertIn("autoformat: formatted 1 of the lane's changed files", log)
        self.assertEqual(len(dispatched), 1)
        sha = dispatched[0]["sha"]
        shown = subprocess.run(["git", "-C", str(self.remote), "show", f"{sha}:fix.txt"],
                               capture_output=True, text=True, check=True).stdout
        self.assertEqual(shown, "fixed\nformatted\n")
        subject = subprocess.run(["git", "-C", str(self.remote), "log", "-1", "--format=%s", sha],
                                 capture_output=True, text=True, check=True).stdout
        self.assertTrue(subject.startswith("format:"), subject)
        self.assertEqual(self.branch_file("fix.txt"), "fixed\nformatted\n")

    def test_a_host_run_is_formatted_before_its_first_check_run(self):
        log = self.host_lane("interpreter: loom-test-python\ncheck: loom-fmt-check\nautoformat: loom-fmt\n")
        self.assertTrue((self.inbox / "done" / "T042-fix-the-thing.md").exists(), log)
        finals = [r for r in self.records() if r["phase"] == "final"]
        self.assertEqual([r["exit"] for r in finals], [0])
        self.assertIn("format:", self.branch_subjects())

    def test_a_file_the_lane_did_not_change_is_never_formatted(self):
        log = self.host_lane(CONTRACT + "autoformat: loom-fmt\n")
        self.assertTrue((self.inbox / "done" / "T042-fix-the-thing.md").exists(), log)
        self.assertEqual(self.args_log.read_text(), "fix.txt\n")
        self.assertEqual(self.branch_file("check.py"), base.CHECK_PY)
        self.assertEqual(self.branch_file("fix.txt"), "fixed\nformatted\n")

    def test_a_failing_formatter_does_not_fail_the_run(self):
        log = self.host_lane(CONTRACT + "autoformat: loom-fmt\n", mode="fail")
        self.assertTrue((self.inbox / "done" / "T042-fix-the-thing.md").exists(), log)
        self.assertIn("autoformat: the formatter exited 1; files restored, run goes on unformatted", log)
        self.assertEqual(self.branch_file("fix.txt"), "fixed\n")
        self.assertEqual(self.branch_file("check.py"), base.CHECK_PY)
        self.assertNotIn("format:", self.branch_subjects())

    def test_a_missing_formatter_does_not_fail_the_run(self):
        log = self.host_lane(CONTRACT + "autoformat: loom-no-such-formatter\n")
        self.assertTrue((self.inbox / "done" / "T042-fix-the-thing.md").exists(), log)
        self.assertIn("autoformat: the formatter exited 127", log)
        self.assertEqual(self.branch_file("fix.txt"), "fixed\n")

    def test_a_contract_without_autoformat_runs_no_formatter(self):
        log = self.host_lane(CONTRACT)
        self.assertTrue((self.inbox / "done" / "T042-fix-the-thing.md").exists(), log)
        self.assertFalse(self.args_log.exists())
        self.assertNotIn("autoformat", log)
        self.assertEqual(self.branch_file("fix.txt"), "fixed\n")

    def test_a_run_that_edits_the_contract_cannot_name_its_own_formatter(self):
        log = self.host_lane(CONTRACT + "autoformat: loom-fmt\n", FAKE_AGENT="contract-edit",
                             FAKE_CONTRACT_TEXT=CONTRACT + "autoformat: loom-fmt --other\n")
        self.assertTrue((self.inbox / "done" / "T042-fix-the-thing.md").exists(), log)
        self.assertFalse(self.args_log.exists())
        self.assertIn("autoformat: skipped, the run changed .loom/validation.conf", log)
        self.assertEqual(self.branch_file("fix.txt"), "fixed\n")

    def test_an_untracked_file_skips_autoformat_without_touching_it(self):
        tree = Path(self._tmp.name) / "tree"
        tree.mkdir()
        def git(*args):
            return subprocess.run(["git", "-C", str(tree), *args],
                                  capture_output=True, text=True, check=True)
        git("init", "-q")
        (tree / "fix.txt").write_text("base\n")
        git("add", "fix.txt")
        git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "base")
        base_sha = git("rev-parse", "HEAD").stdout.strip()
        (tree / "fix.txt").write_text("fixed\n")
        git("add", "fix.txt")
        git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "fix")
        (tree / "scratch.txt").write_text("keep\n")
        func = re.search(r"^contract_autoformat\(\) \{\n.*?^\}\n",
                         base.RUNNER.read_text(), re.S | re.M).group(0)
        log = Path(self._tmp.name) / "autoformat.log"
        ran = Path(self._tmp.name) / "ran"
        script = (
            'safe_git() { git -C "$TREE" "$@"; }\n'
            'tracked_changes() { safe_git diff --name-only; }\n'
            'lane_changed_files() { printf "fix.txt\\n"; }\n'
            f'contract_run() {{ touch "{ran}"; return 0; }}\n'
            'commit_msg() { printf "%s" "$1"; }\n'
            + func +
            'contract_autoformat\n'
        )
        env = {"PATH": "/usr/bin:/bin", "TREE": str(tree), "LOG": str(log),
               "BASE": base_sha, "CONTRACT_FILE": ".loom/validation.conf",
               "C_AUTOFORMAT": "loom-fmt"}
        result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("autoformat: skipped, the tree has untracked files", log.read_text())
        self.assertFalse(ran.exists())
        self.assertEqual((tree / "scratch.txt").read_text(), "keep\n")
        self.assertEqual((tree / "fix.txt").read_text(), "fixed\n")


for _name in dir(base.LaneRunnerTest):
    if _name.startswith("test_"):
        setattr(ContractAutoformatTest, _name, None)
