"""bus.brain status finishes a repo the size of quadseven/loom inside its
per-repo budget (loom#2078).

After loom#2058 and loom#2067, status still ran past its 120 s budget on
quadseven/loom (41 open pull requests). A profile of one run showed 270
reads, 95 of them single-issue reads of issues the run had already listed:
each lane pull request's ticket, read again by the merge refusals and the
goal-home rule, and again under every first comments page, which reads its
issue for the body's status section. These tests count the reads status
makes on a fake GitHub sized like loom and hold them under a bound that fits
the budget."""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import unittest
import unittest.mock
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain
from bus.github import GitHub
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

REPO = "example-owner/example-repo"
NOW = datetime(2030, 1, 10, 14, 0, tzinfo=timezone.utc)
WRITER = {"login": "example-writer"}
GRUG = {"__typename": "Bot", "login": "grug-tribe"}
ISSUE = re.compile(r"^/repos/[^/]+/[^/]+/issues/(\d+)$")
COMMENTS = re.compile(r"^/repos/[^/]+/[^/]+/issues/(\d+)/comments")
EVENTS = re.compile(r"^/repos/[^/]+/[^/]+/issues/(\d+)/events")
SUBS = re.compile(r"^/repos/[^/]+/[^/]+/issues/(\d+)/sub_issues")
PULL = re.compile(r"^/repos/[^/]+/[^/]+/pulls/(\d+)$")

# Sized like quadseven/loom on 2026-10-27: 41 open lane pull requests, each
# with its ticket at loom:pr, and 24 more tickets stuck at loom:failed.
PULL_COUNT = 41
STUCK_COUNT = 24
# A profile of one status pass of quadseven/loom (2026-10-27, 38 open pull
# requests) counted 270 reads, about 95 s of reads run six at a time, and the
# remote brain's pass of it ran past the 120 s budget. After loom#2078 the
# same pass reads 159 times. This fake makes 276 reads before and 157 after;
# the bound holds a loom-sized pass to about 60 percent of the reads that ran
# past the budget.
READ_BOUND = 180


def _issue(number, labels, body="", **extra):
    out = {"number": number, "id": 5000 + number, "state": "open",
           "title": "Issue %d" % number, "labels": [{"name": n} for n in labels],
           "body": body, "user": WRITER, "comments": 2,
           "created_at": "2030-01-01T00:00:00Z", "updated_at": "2030-01-0%dT00:00:00Z" % (
               1 + number % 9)}
    out.update(extra)
    return out


GOAL = _issue(1, ["loom:goal"], "# Goal\n\nShip it.\n")
EPICS = [_issue(n, ["epic"], "Part of #1\n", sub_issues_summary={"total": 1}) for n in (2, 3)]
PR_TICKETS = [_issue(100 + n, ["loom:ticket", "loom:pr", "lane:claude"],
                     "Part of #2\n\n## Acceptance criteria\n\n- [ ] it works\n")
              for n in range(PULL_COUNT)]
STUCK = [_issue(300 + n, ["loom:ticket", "loom:failed", "lane:claude"], "Part of #3\n")
         for n in range(STUCK_COUNT)]
ISSUES = [GOAL] + EPICS + PR_TICKETS + STUCK
PULLS = [{"number": 1000 + i, "title": "PR for #%d" % t["number"],
          "head": {"ref": "loom/%d-work" % t["number"], "sha": "%040d" % i},
          "body": "Refs #%d" % t["number"], "labels": [], "user": {"login": "example-lane"},
          "created_at": "2030-01-01T00:00:00Z"} for i, t in enumerate(PR_TICKETS)]


def _events(number):
    """A long, stuck history: briefed, claimed and failed again and again."""
    out = []
    for day in range(1, 8):
        for name in ("loom:briefed", "loom:claimed", "loom:failed"):
            out.append({"event": "labeled", "label": {"name": name},
                        "created_at": "2030-01-0%dT0%d:00:00Z" % (day, len(out) % 9)})
    return out


def _comments(number):
    return [{"id": 9000 + number * 10 + k, "user": WRITER, "body": "note %d" % k,
             "created_at": "2030-01-02T00:00:00Z", "updated_at": "2030-01-02T00:00:00Z"}
            for k in range(2)]


def _node():
    return {"commits": {"nodes": [{"commit": {"committedDate": "2030-01-01T12:45:00Z",
                                              "statusCheckRollup": {"state": "SUCCESS"}}}]},
            "reviews": {"nodes": [{"author": GRUG, "state": "APPROVED",
                                   "submittedAt": "2030-01-01T13:00:00Z"}]},
            "reviewThreads": {"nodes": []}}


class LoomSizedFake:
    """A transport answering like GitHub for a repo the size of loom, by
    path, counting each request."""

    def __init__(self):
        self.lock = threading.Lock()
        self.calls = Counter()
        self.by_number = {i["number"]: i for i in ISSUES}

    def __call__(self, method, url, headers, body):
        path = url.split("api.github.com")[-1]
        with self.lock:
            self.calls[(method, path)] += 1
        if method != "GET" and path != "/graphql":
            raise AssertionError("status wrote: %s %s" % (method, path))
        if path == "/graphql":
            nodes = {"p%d" % pr["number"]: _node() for pr in PULLS}
            return self._reply({"data": {"repository": nodes}})
        bare = path.split("?", 1)[0]
        for pattern, answer in ((ISSUE, self.by_number.get), (COMMENTS, _comments),
                                (EVENTS, _events), (SUBS, self._subs)):
            found = pattern.match(bare)
            if found:
                value = answer(int(found.group(1)))
                return self._reply(value) if value is not None else (
                    404, {}, b'{"message": "Not Found"}')
        if bare == "/repos/%s/issues" % REPO:
            if "labels=loom:ticket" in path:
                return self._reply([i for i in ISSUES if "loom:ticket" in brain._labels(i)])
            if "labels=epic" in path:
                return self._reply(EPICS)
            return self._reply(ISSUES)
        if bare == "/repos/%s/pulls" % REPO:
            return self._reply(PULLS)
        if bare.endswith("/files"):
            return self._reply([{"filename": "bus/a.py", "patch": "+x"}])
        if PULL.match(bare):
            return self._reply({"state": "open", "mergeable": True, "mergeable_state": "clean"})
        return self._reply([])

    def _subs(self, number):
        return {2: PR_TICKETS[:1], 3: STUCK[:1]}.get(number, [])

    @staticmethod
    def _reply(body):
        return 200, {}, json.dumps(body).encode()

    def reads(self):
        return sum(n for (m, _), n in self.calls.items() if m == "GET" or _ == "/graphql")

    def issue_reads(self):
        return {int(ISSUE.match(p).group(1)): n for (m, p), n in self.calls.items()
                if m == "GET" and ISSUE.match(p)}


def _client(fake):
    gh = GitHub("t", transport=fake)
    # As bus.brain main sets it: the body's status section reads as events.
    gh.status_in_body = True
    gh.status_authors |= {WRITER["login"]}
    return gh


def _status(fake):
    return brain.status(_client(fake), [REPO], REPO, env={}, authors=(WRITER["login"],),
                        now=NOW)


class StatusReadsALoomSizedRepoInBudget(unittest.TestCase):
    def test_the_repo_section_reads_under_the_bound(self):
        fake = LoomSizedFake()
        report = _status(fake)
        self.assertNotIn("timed out", report)
        # Every lane pull request reached the READY checks.
        self.assertEqual(sum(1 for x in report.splitlines()
                             if re.match(r"- #1\d{3} PR for #\d+ - READY", x)), PULL_COUNT)
        self.assertLessEqual(fake.reads(), READ_BOUND,
                             "status read %d times for a loom-sized repo" % fake.reads())

    def test_a_listed_issue_is_never_read_again_on_its_own(self):
        fake = LoomSizedFake()
        _status(fake)
        self.assertEqual(fake.issue_reads(), {})

    def test_each_comments_and_events_list_is_read_once(self):
        fake = LoomSizedFake()
        _status(fake)
        repeated = {p: n for (m, p), n in fake.calls.items()
                    if m == "GET" and (COMMENTS.match(p) or EVENTS.match(p)) and n > 1}
        self.assertEqual(repeated, {})

    def test_the_text_is_the_same_as_reading_every_time(self):
        once = _status(LoomSizedFake())
        uncached = LoomSizedFake()
        with unittest.mock.patch.object(brain.issue_reads, "once", lambda gh: gh):
            every = _status(uncached)
        self.assertGreater(uncached.reads(), READ_BOUND)
        self.assertEqual(once.splitlines(), every.splitlines())


class ResumeReadsListedItemsOnce(unittest.TestCase):
    def test_an_open_item_resume_checks_is_answered_from_the_listing(self):
        from bus import issue_reads
        fake = LoomSizedFake()
        gh = issue_reads.once(_client(fake))
        gh.paginate("/repos/%s/issues?state=open" % REPO)
        self.assertEqual(brain.item_revision(gh, REPO, 100), "pr-c2")
        self.assertEqual(fake.issue_reads(), {})
        # An item the listing does not hold is read, and a 404 is not kept.
        self.assertIsNone(brain.item_revision(gh, REPO, 9999))
        self.assertIsNone(brain.item_revision(gh, REPO, 9999))
        self.assertEqual(fake.issue_reads(), {9999: 2})


if __name__ == "__main__":
    unittest.main()
