"""Retire the brain's roll trees (loom#2438).

A brain tick runs in $LOOM_HOME/brain, and the trees it makes for ships and
rolls go in $LOOM_HOME/brain/work. Nothing removed them: on 2026-11-01 that
folder held 16 roll trees from 2026-10-13 to 2026-10-18, each with one commit
on no remote branch, and the operator's worktree cleanup kept each one every
pass as an unpushed tree.

lanes/mac/brain.sh runs `python3 -m bus.brain_trees prune` once a tick, after
the agent and before the unlock, so no tick of this brain is using the folder.
Every tree gets one line in the brain log, removed or kept with its reason,
and a closing `keeps total=N reason=count ...` line, as the operator's
worktree janitor writes its own (scripts/worktree-janitor in the operator's
tools). A tree is removed when:

  * no process has its cwd inside it, it is clean, it has been idle for
    MIN_IDLE_HOURS, and its work landed: HEAD is on the default branch, or
    merging HEAD into the default branch changes nothing (a squash merge);
  * or every commit on it is on a remote branch and it has been idle for
    STALE_DAYS.

A tree in use, changed in the last MIN_IDLE_HOURS, dirty, or not a git tree
is kept and named. Of the idle clean trees with commits on no remote, the
newest KEEP are kept and named with their count of unpushed commits; an older
one is retired: its unpushed commits are saved first to a git bundle in
$LOOM_HOME/brain/retired (the line names the bundle), and a tree whose bundle
cannot be written is kept. A bundle older than RETIRED_DAYS goes.

Every git call runs with hooks, fsmonitor and external diff tools off, since
a tree's repo config is the agent's. The default branch is fetched once per
repo first, so a roll that landed since the tree's last fetch reads as landed.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

# Unlanded idle trees kept with commits on no remote; older ones are bundled
# and removed.
KEEP = 8
# A tree changed this recently is the tick's own, or an operator's: kept.
MIN_IDLE_HOURS = 6.0
# A tree whose commits are all on a remote branch goes after this long idle.
STALE_DAYS = 3.0
# A retired tree's bundle is kept this long.
RETIRED_DAYS = 30.0
GIT_TIMEOUT = 120
LSOF = shutil.which("lsof") or "/usr/sbin/lsof"

GIT_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",
    "LC_ALL": "C",
}
GIT_SAFE = ["-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
            "-c", "diff.external=", "-c", "core.pager=cat"]

Say = Callable[[str], None]


def _say(line: str) -> None:
    print("[%s] brain-trees: %s" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), line),
          flush=True)


def git(args: Sequence[str], cwd: str,
        extra_env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
    env = {**os.environ, **GIT_ENV, **(extra_env or {})}
    return subprocess.run(["git", *GIT_SAFE, *args], cwd=cwd, env=env, capture_output=True,
                          text=True, timeout=GIT_TIMEOUT)


def git_out(args: Sequence[str], cwd: str) -> Optional[str]:
    try:
        done = git(args, cwd)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def git_ok(args: Sequence[str], cwd: str) -> bool:
    try:
        return git(args, cwd).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def process_cwds() -> Optional[List[str]]:
    """Real paths of every visible process's cwd, or None when they cannot be
    listed (then every tree counts as in use)."""
    try:
        done = subprocess.run([LSOF, "-a", "-d", "cwd", "-F", "n"], capture_output=True,
                              text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    cwds = [os.path.realpath(line[1:]) for line in done.stdout.splitlines()
            if line.startswith("n") and len(line) > 1]
    return cwds or None


def reason_key(reason: str) -> str:
    """A keep reason without its per-tree detail, as the worktree janitor
    counts them: `active-idle-39.3h` -> `active-idle`,
    `unpushed-commits=2 head=...` -> `unpushed-commits`."""
    return re.sub(r"(=\d+|-[\d.]+h)$", "", reason.split(" ", 1)[0])


def linked_worktrees(path: str) -> int:
    """How many linked worktrees the checkout at path has; a failed read
    counts as one, so the checkout is kept."""
    out = git_out(["worktree", "list", "--porcelain"], path)
    if out is None:
        return 1
    return max(len([l for l in out.splitlines() if l.startswith("worktree ")]) - 1, 0)


def newest_mtime(path: str) -> float:
    """The newest mtime of the tree's files, and of its git index and HEAD.
    The rest of the git dir is left out: this prune's own fetch writes there
    (FETCH_HEAD, refs, packs), as does git's background maintenance, and
    either would keep every tree looking fresh, tick after tick."""
    newest = os.lstat(path).st_mtime
    for top, dirs, files in os.walk(path):
        if top == path:
            dirs[:] = [d for d in dirs if d != ".git"]
        for name in dirs + files:
            try:
                newest = max(newest, os.lstat(os.path.join(top, name)).st_mtime)
            except OSError:
                continue
    for name in ("index", "HEAD"):
        try:
            newest = max(newest, os.lstat(os.path.join(path, ".git", name)).st_mtime)
        except OSError:
            continue
    return newest


@dataclass
class Tree:
    name: str
    path: str
    idle_h: float
    unpushed: int = 0
    head: str = ""


@dataclass
class Pruner:
    work: str
    retired: str
    now: float
    keep: int = KEEP
    min_idle_hours: float = MIN_IDLE_HOURS
    stale_days: float = STALE_DAYS
    say: Say = _say
    cwds: Optional[List[str]] = None
    keep_reasons: Counter = field(default_factory=Counter)
    removed: int = 0
    fetched: Dict[str, bool] = field(default_factory=dict)

    def keep_tree(self, name: str, reason: str) -> None:
        self.keep_reasons[reason_key(reason)] += 1
        self.say("keep %s reason=%s" % (name, reason))

    def in_use(self, path: str) -> bool:
        if self.cwds is None:
            return True
        real = os.path.realpath(path)
        return any(c == real or c.startswith(real + os.sep) for c in self.cwds)

    def default_ref(self, path: str) -> Optional[str]:
        # main and master win over origin/HEAD, which is set once at clone time.
        for cand in ("refs/remotes/origin/main", "refs/remotes/origin/master"):
            if git_ok(["show-ref", "--verify", "--quiet", cand], path):
                return cand
        sym = git_out(["symbolic-ref", "-q", "refs/remotes/origin/HEAD"], path)
        return sym.strip() if sym and sym.strip() else None

    def fetch(self, path: str) -> None:
        """Fetch origin once per repo, so a roll that landed since reads as landed.
        A failed fetch leaves the refs as they were: a landed tree then reads as
        not landed, which only keeps it."""
        common = git_out(["rev-parse", "--path-format=absolute", "--git-common-dir"], path)
        key = (common or path).strip()
        if key in self.fetched:
            return
        ok = git_ok(["fetch", "-q", "--no-tags", "origin"], path)
        self.fetched[key] = ok
        if not ok:
            self.say("fetch-failed %s: its landed check reads the refs it had"
                     % os.path.basename(path))

    def content_landed(self, path: str, default: str) -> bool:
        """Whether merging HEAD into default changes nothing. The merge's objects
        go to a throwaway directory, so the repo is not written to."""
        tree = git_out(["rev-parse", default + "^{tree}"], path)
        objects = git_out(["rev-parse", "--path-format=absolute", "--git-path", "objects"], path)
        if not tree or not objects:
            return False
        with tempfile.TemporaryDirectory(prefix="brain-trees-") as tmp:
            env = {"GIT_ALTERNATE_OBJECT_DIRECTORIES": objects.strip(), "GIT_OBJECT_DIRECTORY": tmp}
            try:
                done = git(["merge-tree", "--write-tree", default, "HEAD"], path, extra_env=env)
            except (OSError, subprocess.SubprocessError):
                return False
        return done.returncode == 0 and done.stdout.split()[:1] == [tree.strip()]

    def landed(self, path: str) -> Optional[str]:
        default = self.default_ref(path)
        if not default:
            return None
        if git_ok(["merge-base", "--is-ancestor", "HEAD", default], path):
            return "merged"
        if self.content_landed(path, default):
            return "squash-merged"
        return None

    def remove(self, tree: Tree, reason: str) -> bool:
        common = git_out(["rev-parse", "--path-format=absolute", "--git-common-dir"], tree.path)
        linked = os.path.isfile(os.path.join(tree.path, ".git"))
        shutil.rmtree(tree.path, ignore_errors=True)
        if os.path.lexists(tree.path):
            self.keep_tree(tree.name, "remove-failed")
            return False
        if linked and common and os.path.isdir(common.strip()):
            git_ok(["--git-dir", common.strip(), "worktree", "prune"], self.work)
        self.removed += 1
        self.say("remove %s reason=%s" % (tree.name, reason))
        return True

    def retire(self, tree: Tree) -> None:
        """Save the tree's unpushed commits to a bundle, then remove it. A tree
        whose bundle cannot be written is kept."""
        os.makedirs(self.retired, exist_ok=True)
        bundle = os.path.join(self.retired, "%s-%s.bundle" % (tree.name, tree.head[:12]))
        try:
            done = git(["bundle", "create", "-q", bundle, "HEAD", "--not", "--remotes"], tree.path)
            ok = done.returncode == 0 and os.path.isfile(bundle)
        except (OSError, subprocess.SubprocessError):
            ok = False
        if not ok:
            self.keep_tree(tree.name, "bundle-failed unpushed-commits=%d" % tree.unpushed)
            return
        self.remove(tree, "over-keep-%d unpushed-commits=%d head=%s bundle=%s"
                    % (self.keep, tree.unpushed, tree.head[:12], bundle))

    def judge(self, name: str) -> Optional[Tree]:
        """Remove or keep one tree. Returns it when it is idle and clean with
        commits on no remote: the caller keeps the newest KEEP of those."""
        path = os.path.join(self.work, name)
        try:
            st = os.lstat(path)
        except OSError:
            return None
        if stat.S_ISLNK(st.st_mode):
            self.keep_tree(name, "path-is-symlink")
            return None
        if not stat.S_ISDIR(st.st_mode) or not os.path.lexists(os.path.join(path, ".git")):
            self.keep_tree(name, "not-a-git-tree")
            return None
        if self.in_use(path):
            self.keep_tree(name, "in-use-cwd")
            return None
        status = git_out(["status", "--porcelain", "--untracked-files=all"], path)
        if status is None:
            self.keep_tree(name, "git-status-failed")
            return None
        if status.strip():
            self.keep_tree(name, "dirty")
            return None
        if os.path.isdir(os.path.join(path, ".git")) and linked_worktrees(path) > 0:
            # A main checkout whose linked trees are elsewhere: removing it
            # would orphan them.
            self.keep_tree(name, "holds-linked-worktrees")
            return None
        idle_h = (self.now - newest_mtime(path)) / 3600.0
        if idle_h < self.min_idle_hours:
            self.keep_tree(name, "active-idle-%.1fh" % max(idle_h, 0.0))
            return None
        self.fetch(path)
        how = self.landed(path)
        if how:
            self.remove(Tree(name, path, idle_h), how)
            return None
        count = git_out(["rev-list", "--count", "HEAD", "--not", "--remotes"], path)
        head = git_out(["rev-parse", "HEAD"], path)
        if count is None or not count.strip().isdigit() or not head:
            self.keep_tree(name, "rev-list-failed")
            return None
        unpushed = int(count.strip())
        tree = Tree(name, path, idle_h, unpushed, head.strip())
        if unpushed == 0:
            if idle_h >= self.stale_days * 24:
                self.remove(tree, "stale-%dd-pushed" % int(idle_h // 24))
            else:
                self.keep_tree(name, "pushed-idle-%.1fh" % idle_h)
            return None
        return tree

    def prune_bundles(self) -> None:
        try:
            names = os.listdir(self.retired)
        except OSError:
            return
        for name in names:
            path = os.path.join(self.retired, name)
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if name.endswith(".bundle") and stat.S_ISREG(st.st_mode) and \
                    self.now - st.st_mtime > RETIRED_DAYS * 86400:
                os.unlink(path)
                self.say("remove bundle %s reason=older-than-%dd" % (name, RETIRED_DAYS))

    def run(self) -> int:
        try:
            names = sorted(os.listdir(self.work))
        except FileNotFoundError:
            names = []
        except OSError as err:
            self.say("cannot list %s: %s" % (self.work, err))
            return 1
        if names and self.cwds is None:
            self.say("cannot list process working directories, so every tree counts as in use")
        unpushed: List[Tree] = []
        for name in names:
            tree = self.judge(name)
            if tree is not None:
                unpushed.append(tree)
        # The newest KEEP are kept and named; older ones are bundled and removed.
        unpushed.sort(key=lambda t: t.idle_h)
        for i, tree in enumerate(unpushed):
            if i < self.keep:
                self.keep_tree(tree.name, "unpushed-commits=%d head=%s idle=%.1fh"
                               % (tree.unpushed, tree.head[:12], tree.idle_h))
            else:
                self.retire(tree)
        self.prune_bundles()
        kept = sum(self.keep_reasons.values())
        self.say("keeps total=%d %s removed=%d" % (kept, " ".join(
            "%s=%d" % kv for kv in sorted(self.keep_reasons.items(), key=lambda kv: (-kv[1], kv[0]))),
            self.removed))
        return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m bus.brain_trees")
    p.add_argument("command", choices=["prune"])
    p.add_argument("--home", default=os.environ.get("LOOM_HOME") or os.path.expanduser("~/loom"))
    p.add_argument("--keep", type=int, default=KEEP)
    args = p.parse_args(argv)
    brain = os.path.join(args.home, "brain")
    pruner = Pruner(work=os.path.join(brain, "work"), retired=os.path.join(brain, "retired"),
                    now=time.time(), keep=max(args.keep, 0), cwds=process_cwds())
    return pruner.run()


if __name__ == "__main__":
    sys.exit(main())
