"""Regression test for contract loading after a fix-round default merge."""

import os
import re
import shlex
import shutil
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
        self.check_scan_range("BASE_BRANCH=main")

    def test_contract_load_preserves_the_scan_range_when_the_brief_names_no_base(self):
        # The Base field of a brief is optional. update_fix_branch still
        # records the default branch it fetched, and the clone has no
        # origin/HEAD to fall back on.
        self.check_scan_range("BASE_BRANCH=\nFIX_DEFAULT=main")

    def check_scan_range(self, base_settings):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            tree, upstream = self.make_fix_tree(root)
            result = self.run_functions(root, tree, base_settings, """
load_contract
git -C "$TREE" rev-list --right-only %s...HEAD
""" % upstream)
            self.assertEqual(result.returncode, 0, result.stderr)
            commits = result.stdout.splitlines()
            self.assertEqual(len(commits), 2, result.stdout)
            self.assertEqual(commits[0], run_git(tree, "rev-parse", "HEAD"))
            self.assertEqual(commits[1], run_git(tree, "rev-parse", "HEAD^"))

    def test_a_base_fetch_that_races_another_fetch_is_tried_again(self):
        # "fatal: shallow file has changed since we read it": two fetches on
        # one shallow tree raced, and a fix round was filed as a factory
        # incident without its agent running. A full fix round fetches the
        # default branch first, so only a tree with no origin/main reaches
        # this fetch, and the retry is checked at the function level.
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            tree, _ = self.make_fix_tree(root)
            run_git(tree, "update-ref", "-d", "refs/remotes/origin/main")
            bin_dir = root / "bin"
            bin_dir.mkdir()
            count = root / "fetch-count"
            incident = root / "incident"
            real_git = shutil.which("git")
            wrapper = bin_dir / "git"
            wrapper.write_text("""#!/usr/bin/env bash
if [[ " $* " == *" fetch -q --depth 1 origin "* ]]; then
  n=$(cat "$FAKE_FETCH_COUNT" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$FAKE_FETCH_COUNT"
  if [ "$n" -eq 1 ]; then echo 'fatal: shallow file has changed since we read it' >&2; exit 128; fi
fi
exec %s "$@"
""" % shlex.quote(real_git), encoding="utf-8")
            wrapper.chmod(0o755)
            result = self.run_functions(
                root, tree, "BASE_BRANCH=main",
                'load_contract\necho "HAS_CONTRACT=$HAS_CONTRACT"\n',
                extra_env={"PATH": "%s:%s" % (bin_dir, os.environ["PATH"]),
                           "FAKE_FETCH_COUNT": str(count)},
                incident=incident)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(count.read_text().strip(), "2")
            self.assertIn("HAS_CONTRACT=1", result.stdout.splitlines())
            self.assertFalse(incident.exists())

    def make_fix_tree(self, root):
        """A depth-limited clone of a fix branch with the default branch
        merged in. Returns the tree and the default branch commit."""
        origin = root / "origin.git"
        source = root / "source"
        tree = root / "tree"
        run_git(root, "init", "--bare", str(origin))
        # A real remote's HEAD names its default branch whatever
        # init.defaultBranch is here, so a fetch of HEAD succeeds.
        run_git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
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
        return tree, run_git(tree, "rev-parse", "origin/main^{commit}")

    def run_functions(self, root, tree, base_settings, body, extra_env=None, incident=None):
        script = RUNNER.read_text(encoding="utf-8")
        functions = "\n".join(
            shell_function(script, name)
            for name in ("fetch_base_for_contract", "load_contract")
        )
        shell = """set -e
TREE={tree}
BASE=HEAD
FIX_PR=1
{base_settings}
CONTRACT_FILE=.loom/validation.conf
NET_TIMEOUT=10
LOG={log}
LOOM_ROOT={root}
safe_git() {{ git -C "$TREE" "$@"; }}
note_factory_incident() {{ echo "$*" > {incident}; return 0; }}
parse_contract() {{ return 0; }}
{functions}
{body}
""".format(
            tree=shlex.quote(str(tree)),
            log=shlex.quote(str(root / "fetch.log")),
            root=shlex.quote(str(ROOT)),
            incident=shlex.quote(str(incident or root / "incident")),
            functions=functions,
            body=body,
            base_settings=base_settings,
        )
        env = dict(os.environ, **(extra_env or {}))
        return subprocess.run(["bash", "-c", shell], capture_output=True, text=True, env=env)


if __name__ == "__main__":
    unittest.main()
