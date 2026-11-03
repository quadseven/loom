"""Lock branch writes are compare-and-swap (loom#2558): a tick step or a
release that read a lock head older than the branch head never moves the
branch over the newer step commits. GitHub can serve a lock head two step
commits old (loom#2555); the write fast-forwards from the head it read, reads
again on a refusal, and fails naming both heads when the read stays old."""
from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain, lock  # noqa: E402
from bus.github import GitHubError  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

LOCK_REPO = "example/loom"
START = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)


def stamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def reply(data: Any, status: int = 200) -> SimpleNamespace:
    return SimpleNamespace(status=status, data=data)


class GitGH:
    """The lock branch in memory, with GitHub's ref update rules: a PATCH
    with force false is refused with 422 unless the new commit descends from
    the branch head; force true moves the branch anywhere. `stale` ref reads
    answer the head from `behind` writes ago, as a lagging read replica does."""

    def __init__(self, holder: str = "muse"):
        message = "holder=%s expires=%s acquired=%s" % (
            holder, stamp(START + timedelta(hours=1)), stamp(START))
        self.commits: Dict[str, Dict[str, Any]] = {
            "c0": {"message": message, "tree": {"sha": "tree"}, "parents": []}}
        self.head = "c0"
        self.heads = ["c0"]
        self.stale = 0
        self.behind = 0
        self.patches: List[Dict[str, Any]] = []

    def rest(self, method: str, path: str, body: Any = None, ok: tuple = (), **_: Any):
        resp = self._git(method, path, body)
        if resp.status >= 400 and resp.status not in ok:
            raise GitHubError("%s %s: %d" % (method, path, resp.status), resp.status)
        return resp

    def _git(self, method: str, path: str, body: Any):
        if method == "GET" and "/git/ref/heads/" in path:
            if self.stale > 0:
                self.stale -= 1
                return reply({"object": {"sha": self.heads[-1 - self.behind]}})
            return reply({"object": {"sha": self.head}})
        if method == "GET":
            sha = path.rsplit("/", 1)[1]
            return reply(self.commits[sha]) if sha in self.commits else reply({}, 404)
        if method == "POST":
            sha = "c%d" % len(self.commits)
            self.commits[sha] = {"message": body["message"], "tree": {"sha": body["tree"]},
                                 "parents": list(body["parents"])}
            return reply({"sha": sha})
        assert method == "PATCH" and "/git/refs/heads/" in path, (method, path)
        self.patches.append(dict(body))
        if not body.get("force") and not self.descends(body["sha"], self.head):
            return reply({"message": "Update is not a fast forward"}, 422)
        self.head = body["sha"]
        self.heads.append(self.head)
        return reply({"object": {"sha": self.head}})

    def descends(self, sha: str, ancestor: str) -> bool:
        todo = [sha]
        while todo:
            at = todo.pop()
            if at == ancestor:
                return True
            todo += self.commits[at]["parents"]
        return False

    def steps(self) -> List[str]:
        """The steps the branch head's tick record holds."""
        message = self.commits[self.head]["message"]
        for line in message.splitlines():
            if line.startswith(brain.STEP_MARKER):
                return [t["step"] for t in json.loads(line[len(brain.STEP_MARKER):])["times"]]
        return []


def record_steps(gh: GitGH, *steps: str) -> None:
    for minute, step in enumerate(steps):
        brain.tick_step(gh, LOCK_REPO, step, holder="muse",
                        now=START + timedelta(minutes=minute), sleep=lambda _: None)


class TickStepWriteTest(unittest.TestCase):
    def setUp(self):
        self.gh = GitGH()
        record_steps(self.gh, "lock", "status", "merge")
        self.waits: List[float] = []

    def test_a_step_on_a_stale_read_keeps_the_newer_step_commits(self):
        newer = self.gh.head
        # The step's tick read answers the head from two writes ago.
        self.gh.stale, self.gh.behind = 1, 2
        saved = brain.tick_step(self.gh, LOCK_REPO, "verify", holder="muse",
                                now=START + timedelta(minutes=5), sleep=self.waits.append)
        self.assertTrue(self.gh.descends(self.gh.head, newer))
        self.assertEqual(self.gh.steps(), ["lock", "status", "merge", "verify"])
        self.assertEqual([t["step"] for t in saved["times"]],
                         ["lock", "status", "merge", "verify"])
        self.assertEqual(self.waits, [lock.WRITE_RETRY_WAITS[0]])
        self.assertTrue(all(p["force"] is False for p in self.gh.patches))

    def test_a_read_that_stays_old_fails_naming_both_heads(self):
        newer = self.gh.head
        old = self.gh.heads[-3]
        # Every read of every try is old; the read after the last try is not.
        self.gh.stale = 2 * (len(lock.WRITE_RETRY_WAITS) + 1)
        self.gh.behind = 2
        with self.assertRaises(lock.LockHeadMoved) as caught:
            brain.tick_step(self.gh, LOCK_REPO, "verify", holder="muse",
                            now=START + timedelta(minutes=5), sleep=self.waits.append)
        self.assertEqual((caught.exception.read, caught.exception.current), (old, newer))
        self.assertIn(old, str(caught.exception))
        self.assertIn(newer, str(caught.exception))
        self.assertEqual(self.gh.head, newer)
        self.assertEqual(self.gh.steps(), ["lock", "status", "merge"])
        self.assertEqual(self.waits, list(lock.WRITE_RETRY_WAITS))

    def test_a_fresh_read_writes_once_with_no_wait(self):
        record_steps(self.gh, "verify")
        self.assertEqual(self.gh.steps(), ["lock", "status", "merge", "verify"])
        self.assertEqual(len(self.gh.patches), 4)
        self.assertTrue(all(p["force"] is False for p in self.gh.patches))


class ReleaseWriteTest(unittest.TestCase):
    def test_a_release_on_a_stale_read_keeps_the_newer_step_commits(self):
        gh = GitGH()
        record_steps(gh, "lock", "status", "merge")
        newer = gh.head
        waits: List[float] = []
        # release reads the holder, then the ref it writes from: that one is old.
        gh.stale, gh.behind = 0, 2

        real = gh._git

        def lagging(method: str, path: str, body: Any):
            if method == "GET" and "/git/ref/heads/" in path:
                lagging.reads += 1
                if lagging.reads == 2:
                    return reply({"object": {"sha": gh.heads[-3]}})
            return real(method, path, body)
        lagging.reads = 0
        gh._git = lagging
        self.assertTrue(lock.release(gh, LOCK_REPO, "muse", sleep=waits.append))
        self.assertTrue(gh.descends(gh.head, newer))
        self.assertEqual(gh.steps(), ["lock", "status", "merge"])
        self.assertIn("expires=", gh.commits[gh.head]["message"])
        self.assertEqual(waits, [lock.WRITE_RETRY_WAITS[0]])
        self.assertTrue(all(p["force"] is False for p in gh.patches))


if __name__ == "__main__":
    unittest.main()
