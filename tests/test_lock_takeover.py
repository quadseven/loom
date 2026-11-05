"""Stale lock takeover is compare-and-swap (loom#2579, loom#2474).

A brain that finds an expired lock writes its lock commit with the stale
commit as parent and moves the ref by a fast-forward PATCH, never by a
delete and a fresh create. Two takers of one expired lock leave exactly one
holder: GitHub refuses the second PATCH because the branch no longer points
at the stale commit. An attempt that finds a live peer lock writes nothing,
so a waiting brain leaves no orphan commit behind."""
from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain, lock  # noqa: E402
from bus.github import GitHubError  # noqa: E402

REPO = "example/loom"
NOW = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
LOCK_REF = "heads/" + lock.LOCK_BRANCH


def stamp(value: datetime) -> str:
    return value.strftime(lock.TIME_FORMAT)


def reply(data: Any, status: int = 200) -> SimpleNamespace:
    return SimpleNamespace(status=status, data=data)


class RefGH:
    """Refs and commits in memory, with GitHub's ref rules: creating a ref
    that exists answers 422, and a PATCH with force false answers 422 unless
    the new commit descends from the ref's head. `hook` runs before each call
    and may change the state, as a peer acting in between would."""

    def __init__(self) -> None:
        self.commits: Dict[str, Dict[str, Any]] = {
            "m0": {"message": "init", "tree": {"sha": "t0"}, "parents": []}}
        self.refs: Dict[str, str] = {"heads/main": "m0"}
        self.calls: List[tuple] = []
        self.hook: Optional[Callable[[str, str], None]] = None
        # Ref reads still to answer with an older head, as a lagging replica does.
        self.lagging: List[str] = []

    def seed(self, holder: str, expires: datetime, extra: str = "", parents=()) -> str:
        sha = "s%d" % len(self.commits)
        self.commits[sha] = {"message": "holder=%s expires=%s acquired=%s%s" % (
            holder, stamp(expires), stamp(expires - timedelta(minutes=15)), extra),
            "tree": {"sha": "t0"}, "parents": list(parents)}
        self.refs[LOCK_REF] = sha
        return sha

    def rest(self, method: str, path: str, body: Any = None, ok: tuple = (), **_: Any):
        if self.hook is not None:
            self.hook(method, path)
        self.calls.append((method, path))
        resp = self._git(method, path.split("/repos/%s/" % REPO, 1)[1], body)
        if resp.status >= 400 and resp.status not in ok:
            raise GitHubError("%s %s: %d" % (method, path, resp.status), resp.status)
        return resp

    def _git(self, method: str, path: str, body: Any):
        if method == "GET" and path.startswith("git/ref/"):
            ref = path[len("git/ref/"):]
            if ref == LOCK_REF and self.lagging:
                return reply({"object": {"sha": self.lagging.pop(0)}})
            if ref not in self.refs:
                return reply({"message": "Not Found"}, 404)
            return reply({"object": {"sha": self.refs[ref]}})
        if method == "GET" and path.startswith("git/commits/"):
            sha = path.rsplit("/", 1)[1]
            return reply(self.commits[sha]) if sha in self.commits else reply({}, 404)
        if method == "POST" and path == "git/commits":
            sha = "n%d" % len(self.commits)
            self.commits[sha] = {"message": body["message"], "tree": {"sha": body["tree"]},
                                 "parents": list(body["parents"])}
            return reply({"sha": sha}, 201)
        if method == "POST" and path == "git/refs":
            ref = body["ref"][len("refs/"):]
            if ref in self.refs:
                return reply({"message": "Reference already exists"}, 422)
            self.refs[ref] = body["sha"]
            return reply({}, 201)
        if method == "DELETE" and path.startswith("git/refs/"):
            self.refs.pop(path[len("git/refs/"):], None)
            return reply({}, 204)
        assert method == "PATCH" and path.startswith("git/refs/"), (method, path)
        ref = path[len("git/refs/"):]
        if ref not in self.refs:
            return reply({"message": "Reference does not exist"}, 422)
        if not body.get("force") and not self.descends(body["sha"], self.refs[ref]):
            return reply({"message": "Update is not a fast forward"}, 422)
        self.refs[ref] = body["sha"]
        return reply({"object": {"sha": body["sha"]}})

    def descends(self, sha: str, ancestor: str) -> bool:
        todo = [sha]
        while todo:
            at = todo.pop()
            if at == ancestor:
                return True
            todo += self.commits[at]["parents"]
        return False

    def head(self) -> str:
        return self.refs[LOCK_REF]

    def message(self) -> str:
        return self.commits[self.head()]["message"]

    def writes(self) -> List[tuple]:
        return [c for c in self.calls if c[0] in ("POST", "PATCH", "DELETE", "PUT")]


class TakeoverTest(unittest.TestCase):
    def test_two_takers_of_one_expired_lock_leave_one_holder(self):
        gh = RefGH()
        stale = gh.seed("old", NOW - timedelta(minutes=1))
        results: Dict[str, lock.Attempt] = {}

        # The first taker has read the expired lock; before its first write
        # lands, the second taker reads the same lock and takes it.
        def peer_first(method: str, path: str) -> None:
            if method in ("PATCH", "DELETE") and "loom-lock" in path and gh.hook:
                gh.hook = None
                results["codex"] = lock.attempt(gh, REPO, "codex", 30, NOW)

        gh.hook = peer_first
        results["muse"] = lock.attempt(gh, REPO, "muse", 30, NOW)
        self.assertTrue(results["codex"].acquired)
        self.assertFalse(results["muse"].acquired)
        self.assertFalse(results["muse"].already)
        self.assertEqual("codex", results["muse"].holder.name)
        self.assertTrue(results["muse"].line(NOW).startswith("lock held by codex"),
                        results["muse"].line(NOW))
        self.assertIn("holder=codex", gh.message().splitlines()[0])
        self.assertEqual([stale], gh.commits[gh.head()]["parents"])
        self.assertFalse([c for c in gh.calls if c[0] == "DELETE"])

    def test_a_single_takeover_fast_forwards_from_the_stale_lock(self):
        gh = RefGH()
        yielded = lock.YIELD_MARKER + json.dumps(
            {"spare": {"at": stamp(NOW - timedelta(minutes=5)), "to": "old"}},
            separators=(",", ":"))
        stale = gh.seed("old", NOW - timedelta(minutes=1), "\n" + yielded)
        got = lock.attempt(gh, REPO, "muse", 30, NOW, run="r1")
        self.assertTrue(got.acquired)
        self.assertFalse(got.already)
        self.assertEqual(("muse", "r1"), (got.holder.name, got.holder.run))
        self.assertTrue(gh.message().startswith("holder=muse expires=%s" % stamp(
            NOW + timedelta(minutes=30))))
        self.assertEqual([stale], gh.commits[gh.head()]["parents"])
        self.assertIn("spare", lock.yields_from(gh.message()))
        self.assertEqual(["POST", "PATCH"], [c[0] for c in gh.writes()])
        self.assertFalse([c for c in gh.calls if c[0] == "DELETE"])

    def test_a_takeover_refused_on_a_lagging_read_takes_it_from_a_fresh_read(self):
        # GitHub served a head one step commit old: the PATCH is refused,
        # and the fresh read still finds the lock expired, so the taker
        # builds again on the real head.
        gh = RefGH()
        old = gh.seed("old", NOW - timedelta(minutes=1))
        newer = gh.seed("old", NOW - timedelta(minutes=1), parents=[old])
        gh.lagging = [old]
        waits: List[float] = []
        got = lock.attempt(gh, REPO, "muse", 30, NOW, sleep=waits.append)
        self.assertTrue(got.acquired)
        self.assertEqual([newer], gh.commits[gh.head()]["parents"])
        self.assertEqual([lock.WRITE_RETRY_WAITS[0]], waits)

    def test_a_free_lock_lost_to_a_peers_create_is_held(self):
        gh = RefGH()

        def peer_creates(method: str, path: str) -> None:
            if method == "POST" and path.endswith("/git/refs"):
                gh.hook = None
                gh.seed("codex", NOW + timedelta(minutes=30))

        gh.hook = peer_creates
        got = lock.attempt(gh, REPO, "muse", 30, NOW)
        self.assertFalse(got.acquired)
        self.assertEqual("codex", got.holder.name)
        self.assertIn("holder=codex", gh.message())

    def test_a_free_lock_is_created_with_no_parent(self):
        gh = RefGH()
        got = lock.attempt(gh, REPO, "muse", 30, NOW)
        self.assertTrue(got.acquired)
        self.assertEqual([], gh.commits[gh.head()]["parents"])
        self.assertEqual(["POST", "POST"], [c[0] for c in gh.writes()])


class LiveLockTest(unittest.TestCase):
    def test_a_waiting_attempt_against_a_live_peer_lock_writes_nothing(self):
        gh = RefGH()
        held = gh.seed("codex", NOW + timedelta(minutes=40))
        clock = [NOW]

        def sleep(seconds: float) -> None:
            clock[0] += timedelta(seconds=seconds)

        got = lock.attempt_waiting(gh, REPO, "muse", 30, wait=3,
                                   clock=lambda: clock[0], sleep=sleep)
        self.assertFalse(got.acquired)
        self.assertEqual("codex", got.holder.name)
        self.assertEqual([], gh.writes())
        self.assertEqual(held, gh.head())
        self.assertEqual(2, len(gh.commits))

    def test_the_callers_own_live_lock_is_already_held_with_no_write(self):
        gh = RefGH()
        gh.seed("muse", NOW + timedelta(minutes=10))
        got = lock.attempt(gh, REPO, "muse", 30, NOW)
        self.assertTrue(got.acquired)
        self.assertTrue(got.already)
        self.assertEqual([], gh.writes())


class AfterTakeoverTest(unittest.TestCase):
    def test_steps_and_release_write_on_the_taken_over_lock(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        gh = RefGH()
        stale = gh.seed("old", now - timedelta(minutes=1))
        self.assertTrue(lock.attempt(gh, REPO, "muse", 30, now).acquired)
        taken = gh.head()
        brain.tick_step(gh, REPO, "lock", holder="muse", now=now, sleep=lambda _: None)
        brain.tick_step(gh, REPO, "status", holder="muse", now=now + timedelta(minutes=1),
                        sleep=lambda _: None)
        self.assertTrue(gh.descends(gh.head(), taken))
        self.assertTrue(gh.descends(gh.head(), stale))
        self.assertEqual("status", lock.read_holder(gh, REPO).step)
        self.assertTrue(lock.release(gh, REPO, "muse", sleep=lambda _: None))
        self.assertFalse(lock.read_holder(gh, REPO).live(
            datetime.now(timezone.utc) + timedelta(seconds=1)))
        self.assertTrue(all(c[0] != "DELETE" for c in gh.calls))


if __name__ == "__main__":
    unittest.main()
