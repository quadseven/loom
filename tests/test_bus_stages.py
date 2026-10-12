"""Stage ownership: deferred work yields its stage to the next lock holder."""
from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain, stages
from bus.github import GitHub

def reply(status=200, body=None):
    return status, {}, b"" if body is None else json.dumps(body).encode()


class Fake:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, json.loads(body) if body else None))
        return self.replies.pop(0)


REPO = "example-owner/example-repo"
LOCKS = "example-owner/loom"
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def ago(minutes):
    return (NOW - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def journal(minutes, holder="muse"):
    return {"body": "<!-- loom:journal v1 -->\nTick %s, %s" % (ago(minutes), holder),
            "createdAt": ago(minutes), "author": {"login": "brain-bot"}}


def stage(minutes, action, item=4, version="sha1", holder="muse", name="merge",
          resume="merge after the review thread reply", login="brain-bot"):
    body = stages.render(name, item, action, holder, version,
                         resume if action == "deferred" else "")
    return {"body": body, "createdAt": ago(minutes), "author": {"login": login}}


def pr(number=4, sha="sha1", updated=5):
    return {"number": number, "title": "PR", "head": {"ref": "loom/%d-work" % number, "sha": sha},
            "updated_at": ago(updated)}


GREEN = {"commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]},
         "reviewThreads": {"nodes": []}}
GOAL = {"number": 1, "title": "Goal", "labels": [{"name": "loom:goal"}],
        "created_at": "2030-01-01T00:00:00Z"}


def status(comments, prs=(), issues=(), nodes=None):
    nodes = nodes or {}
    graph = {"g1": {"comments": {"nodes": list(comments)}}}
    graph.update({"p%d" % p["number"]: nodes.get(p["number"], GREEN) for p in prs})
    gh = GitHub("t", transport=Fake(
        reply(200, [GOAL] + list(issues)), reply(200, list(prs)),
        reply(200, {"data": {"repository": graph}}), reply(200, [])))
    return brain.status(gh, [REPO], REPO)


def takeover(report):
    lines = report.split("### Takeover\n", 1)[1].splitlines()
    end = lines.index("") if "" in lines else len(lines)
    return lines[:end]


def freshness_only_allows(comments, holder, now):
    """The rule this replaces: act only when no other brain journaled on any
    goal in the last 2 hours."""
    for c in comments:
        if "<!-- loom:journal v1 -->" not in c["body"] or c["body"].endswith(", " + holder):
            continue
        at = datetime.strptime(c["createdAt"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        if now - at < timedelta(hours=2):
            return False
    return True


def lock(holder, expires="2999-01-01T00:00:00Z"):
    return [reply(200, {"object": {"sha": "lk"}}),
            reply(200, {"message": "holder=%s expires=%s" % (holder, expires)})]


class TakeoverTests(unittest.TestCase):
    def test_fresh_journals_do_not_keep_deferred_merge_work_from_a_peer(self):
        comments = [journal(95), stage(90, "deferred"), journal(65), stage(60, "deferred"),
                    journal(20), stage(18, "deferred")]
        # The old rule: Muse journaled 20 minutes ago, so the lane machine abstains.
        self.assertFalse(freshness_only_allows(comments, "lane-machine", NOW))
        report = status(comments, prs=[pr()])
        lines = takeover(report)
        self.assertEqual(1, len(lines))
        self.assertIn("merge #4 (goal #1) - yielded by muse", lines[0])
        self.assertIn("deferred 3 times", lines[0])
        self.assertIn("resume: merge after the review thread reply", lines[0])
        self.assertIn("- #1 merge: responsible yielded by muse, any lock holder; last done none",
                      report)

    def test_a_done_record_invalidates_an_older_resume_item(self):
        comments = [stage(60, "deferred"), stage(30, "done", holder="lane-machine")]
        report = status(comments, prs=[pr()])
        self.assertEqual([], takeover(report))
        self.assertIn("- #1 merge: responsible lane-machine; last done #4 by lane-machine", report)

    def test_a_changed_or_finished_item_invalidates_an_older_resume_item(self):
        comments = [stage(60, "deferred")]
        self.assertEqual([], takeover(status(comments, prs=[pr(sha="sha2")])))
        self.assertEqual([], takeover(status(comments, prs=[])))
        failed = stage(60, "deferred", item=7, name="failed", version="loom:failed,loom:ticket",
                       resume="re-brief on codex")
        ticket = {"number": 7, "title": "T", "created_at": "2030-01-01T00:00:00Z",
                  "updated_at": ago(1),
                  "labels": [{"name": "loom:ticket"}, {"name": "loom:failed"}]}
        self.assertIn("failed #7 (goal #1) - yielded by muse",
                      takeover(status([failed], issues=[ticket]))[0])
        ticket["labels"][1]["name"] = "loom:briefed"
        self.assertEqual([], takeover(status([failed], issues=[ticket])))

    def test_a_deferral_after_a_change_counts_from_one(self):
        comments = [stage(90, "deferred"), stage(60, "deferred", version="sha2")]
        self.assertIn("deferred 1 times", takeover(status(comments, prs=[pr(sha="sha2")]))[0])

    def test_ready_work_untouched_past_the_limit_is_overdue(self):
        report = status([journal(5)], prs=[pr(updated=180), pr(number=5, updated=10)])
        lines = takeover(report)
        self.assertEqual(1, len(lines))
        self.assertIn("merge #4 - overdue", lines[0])
        red = {"commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "FAILURE"}}}]},
               "reviewThreads": {"nodes": []}}
        self.assertEqual([], takeover(status([], prs=[pr(updated=180)], nodes={4: red})))

    def test_records_from_unlisted_logins_are_data(self):
        comments = [stage(30, "deferred", login="stranger")]
        with mock.patch.dict(os.environ, {"LOOM_BRIEF_AUTHORS": "brain-bot"}):
            self.assertEqual([], takeover(status(comments, prs=[pr()])))
        with mock.patch.dict(os.environ, {"LOOM_BRIEF_AUTHORS": "brain-bot stranger"}):
            self.assertEqual(1, len(takeover(status(comments, prs=[pr()]))))


class RecordTests(unittest.TestCase):
    def test_only_the_lock_holder_records(self):
        for replies in (lock("muse"), lock("lane-machine", "2000-01-01T00:00:00Z"),
                        [reply(404, {})]):
            fake = Fake(*replies)
            with self.assertRaises(ValueError):
                stages.record(GitHub("t", transport=fake), REPO, 1, "merge", "done",
                              "lane-machine", 4, lock_in=LOCKS)
            self.assertFalse(any(c[0] == "POST" for c in fake.calls))

    def test_a_deferral_records_the_item_version_and_resume(self):
        fake = Fake(*lock("muse"), reply(200, {"state": "open", "labels": [{"name": "loom:goal"}]}),
                    reply(200, {"state": "open", "head": {"sha": "abc"}}), reply(201, {}))
        stages.record(GitHub("t", transport=fake), REPO, 1, "merge", "deferred", "muse", 4,
                      "merge once checks rerun", lock_in=LOCKS)
        method, url, body = fake.calls[-1]
        self.assertEqual("POST", method)
        self.assertTrue(url.endswith("/repos/%s/issues/1/comments" % REPO))
        found = stages.parse(body["body"])
        self.assertEqual(("merge", 4, "deferred", "muse", "abc", "merge once checks rerun"),
                         tuple(found[k] for k in ("stage", "item", "action", "holder",
                                                  "version", "resume")))

    def test_refusals_post_nothing(self):
        cases = [dict(action="deferred", resume=""), dict(action="deferred", resume="fixes #3"),
                 dict(stage="ship"), dict(stage="brief", item=4)]
        for case in cases:
            with self.subTest(**case):
                args = dict(stage="merge", action="done", item=4, resume="")
                args.update(case)
                fake = Fake(*lock("muse"), reply(200, {"state": "open",
                                                       "labels": [{"name": "loom:goal"}]}))
                with self.assertRaises(ValueError):
                    stages.record(GitHub("t", transport=fake), REPO, 1, args["stage"],
                                  args["action"], "muse", args["item"], args["resume"],
                                  lock_in=LOCKS)
                self.assertFalse(any(c[0] == "POST" for c in fake.calls))

    def test_a_closed_pull_request_is_not_deferred(self):
        fake = Fake(*lock("muse"), reply(200, {"state": "open", "labels": [{"name": "loom:goal"}]}),
                    reply(200, {"state": "closed", "head": {"sha": "abc"}}))
        with self.assertRaises(ValueError):
            stages.record(GitHub("t", transport=fake), REPO, 1, "merge", "deferred", "muse", 4,
                          "merge it", lock_in=LOCKS)


class GuardTests(unittest.TestCase):
    def test_writes_need_the_lock_when_a_holder_is_set(self):
        env = {"LOOM_BRAIN_HOLDER": "lane-machine", "LOOM_STATUS_REPO": LOCKS}
        with self.assertRaises(ValueError):
            brain.guard(GitHub("t", transport=Fake(*lock("muse"))), "merge", env)
        fake = Fake(*lock("lane-machine"))
        brain.guard(GitHub("t", transport=fake), "merge", env)
        self.assertIn("/repos/%s/git/ref/heads/loom-lock/brain" % LOCKS, fake.calls[0][1])

    def test_reads_and_unset_holders_skip_the_check(self):
        fake = Fake()
        brain.guard(GitHub("t", transport=fake), "status", {"LOOM_BRAIN_HOLDER": "muse"})
        brain.guard(GitHub("t", transport=fake), "merge", {})
        self.assertEqual([], fake.calls)


if __name__ == "__main__":
    unittest.main()
