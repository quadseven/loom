"""Regression test for contract loading after a fix-round default merge."""

import re
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "lanes" / "run_lane.sh"
DEPTH = 4


def run_git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def shell_function(script, name):
    match = re.search(r"^%s\(\) \{\n.*?^\}\n" % name, script, re.S | re.M)
    if not match:
        raise AssertionError("missing shell function: %s" % name)
    return match.group(0)


class FixRoundContractFetchTest(unittest.TestCase):
    def test_contract_load_preserves_the_fix_round_scan_range(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            origin = root / "origin.git"
            source = root / "source"
            tree = root / "tree"
            run_git(root, "init", "--bare", str(origin))
            run_git(root, "init", str(source))
            run_git(source, "config", "user.name", "Test User")
            run_git(source, "config", "user.email", "test@example.com")
            (source / ".loom").mkdir()
            (source / ".loom" / "validation.conf").write_text(
                "interpreter: python3\ncheck: true\n", encoding="utf-8"
            )
            for number in range(12):
                (source / "history.txt").write_text("%s\n" % number, encoding="utf-8")
                run_git(source, "add", ".")
                run_git(source, "commit", "-m", "default %s" % number)
            run_git(source, "branch", "-M", "main")
            run_git(source, "remote", "add", "origin", str(origin))
            run_git(source, "push", "-u", "origin", "main")
            run_git(source, "checkout", "-b", "fix")
            (source / "fix.txt").write_text("fix\n", encoding="utf-8")
            run_git(source, "add", "fix.txt")
            run_git(source, "commit", "-m", "fix change")
            run_git(source, "push", "origin", "fix")
            run_git(source, "checkout", "main")
            (source / "history.txt").write_text("later default\n", encoding="utf-8")
            run_git(source, "commit", "-am", "later default")
            run_git(source, "push", "origin", "main")

            subprocess.run(
                ["git", "clone", "--depth", str(DEPTH), "--branch", "fix",
                 origin.as_uri(), str(tree)], check=True, capture_output=True, text=True
            )
            run_git(tree, "config", "user.name", "Test User")
            run_git(tree, "config", "user.email", "test@example.com")
            run_git(tree, "fetch", "--depth", str(DEPTH), "origin",
                    "+refs/heads/main:refs/remotes/origin/main")
            run_git(tree, "merge", "--no-edit", "origin/main")
            upstream = run_git(tree, "rev-parse", "origin/main^{commit}")

            script = RUNNER.read_text(encoding="utf-8")
            functions = "\n".join(
                shell_function(script, name)
                for name in ("fetch_base_for_contract", "load_contract")
            )
            shell = """set -e
TREE={tree}
BASE=HEAD
FIX_PR=1
BASE_BRANCH=main
CONTRACT_FILE=.loom/validation.conf
NET_TIMEOUT=10
LOG={log}
LOOM_ROOT={root}
safe_git() {{ git -C "$TREE" "$@"; }}
note_factory_incident() {{ return 0; }}
parse_contract() {{ return 0; }}
{functions}
load_contract
git -C "$TREE" rev-list --right-only {upstream}...HEAD
""".format(
                tree=subprocess.list2cmdline([str(tree)]),
                log=subprocess.list2cmdline([str(root / "fetch.log")]),
                root=subprocess.list2cmdline([str(ROOT)]),
                functions=functions,
                upstream=upstream,
            )
            result = subprocess.run(["bash", "-c", shell], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            commits = result.stdout.splitlines()
            self.assertEqual(len(commits), 2, result.stdout)
            self.assertEqual(commits[0], run_git(tree, "rev-parse", "HEAD"))
            self.assertEqual(commits[1], run_git(tree, "rev-parse", "HEAD^"))


if __name__ == "__main__":
    unittest.main()
