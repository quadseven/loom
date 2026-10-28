"""bus.brain resume finishes a repo the size of quadseven/loom inside the
brain's per-repo budget (loom#2078).

After loom#2080, the brain's resume scoped to loom and grug printed grug's
section and then timed out on loom at its 55 s budget. A read-only profile of
resume on live GitHub found the loom section read its five parts (goal
handoff items, sensitive pull requests, finished epics, verify due, lane
proof audit) one after another, and the finished-epics part alone read one
sub-issue list per epic, 25 of them for loom, 12 for epics that were already
closed and can never be finished. These tests hold the loom section of a
loom-sized fake to a read bound, check the five parts run side by side, and
check the text is the same as reading every epic's sub-issues on its own."""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import unittest
import unittest.mock
from collections import Counter
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import audit, brain, handoff
from bus.github import GitHub

REPO = "example-owner/example-repo"
LOCK_REPO = "example-owner/loom"
NOW = datetime(2030, 1, 10, 14, 0, tzinfo=timezone.utc)
WRITER = {"login": "example-writer"}
ENV = {"LOOM_BRIEF_AUTHORS": WRITER["login"]}
ISSUE = re.compile(r"^/repos/[^/]+/[^/]+/issues/(\d+)$")
COMMENTS = re.compile(r"^/repos/[^/]+/[^/]+/issues/(\d+)/comments$")
EVENTS = re.compile(r"^/repos/[^/]+/[^/]+/issues/(\d+)/events$")
SUBS = re.compile(r"^/repos/[^/]+/[^/]+/issues/(\d+)/sub_issues$")

# Sized like quadseven/loom's resume section on 2026-10-27: two goals, 26
# epics (12 closed, the standing repairs epic among the open ones), 19
# shipped tickets waiting for verify, and handoff items on both goal homes.
GOALS = (1, 2)
EPICS_PER_GOAL = 13
CLOSED_PER_GOAL = 6
SHIPPED = 19
# On main this fake's resume makes 56 reads, 26 of them sub-issue lists, one
# per epic, and runs its five parts one after another; on live loom that was
# 107 reads and the section ran past the brain's 55 s budget. After loom#2078
# it reads the open epics' sub-issue states in one GraphQL query: 32 reads
# here, and live the loom section took 3.2 s instead of 14.8 s on the same
# machine.
READ_BOUND = 36


def _at(days):
    return (NOW - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _issue(number, labels, title=None, state="open", body=""):
    return {"number": number, "id": 7000 + number, "state": state,
            "title": title or "Issue %d" % number, "labels": [{"name": n} for n in labels],
            "body": body, "user": WRITER, "comments": 1,
            "created_at": _at(30), "updated_at": _at(3)}


def _epics(goal):
    out = []
    for k in range(EPICS_PER_GOAL):
        number = goal * 100 + k
        title = brain.REPAIRS_TITLE if k == EPICS_PER_GOAL - 1 else "Epic: part %d" % number
        state = "closed" if k < CLOSED_PER_GOAL else "open"
        out.append(_issue(number, ["epic"], title, state, "Part of #%d\n" % goal))
    return out


GOAL_ISSUES = [_issue(g, ["loom:goal"], "Goal %d" % g) for g in GOALS]
EPICS = {g: _epics(g) for g in GOALS}
# The first open epic of goal 1 has every ticket closed: resume names it.
FINISHED = EPICS[1][CLOSED_PER_GOAL]["number"]
SHIPPED_TICKETS = [_issue(500 + n, ["loom:ticket", "loom:shipped"],
                          body="Part of #2\n\n## Acceptance criteria\n\n- [ ] Live: it holds\n")
                   for n in range(SHIPPED)]
OPEN_ISSUES = GOAL_ISSUES + [e for g in GOALS for e in EPICS[g] if e["state"] == "open"] \
    + SHIPPED_TICKETS


def _subs(number):
    """Three tickets under each epic; all closed under the finished one."""
    if number in GOALS:
        return EPICS[number]
    state = "closed" if number == FINISHED else "open"
    return [_issue(number * 10 + k, ["loom:ticket"], state="closed" if k else state)
            for k in range(3)]


def _home_comments(goal):
    """A journal deferring a closed and an open item, then one finishing a third."""
    body = "\n".join([handoff.JOURNAL_MARKER, "Tick %s, muse" % _at(0.1),
                      handoff.defer_marker("failed", (REPO, 500 + goal), "shipped-c1", "muse"),
                      handoff.defer_marker("failed", (REPO, 900 + goal), "failed-c1", "muse")])
    return [{"id": 8000 + goal, "body": body, "user": WRITER,
             "created_at": _at(0.1), "updated_at": _at(0.1)}]


def _events(number):
    return [{"event": "labeled", "label": {"name": "loom:shipped"},
             "created_at": _at(1 + number % 7)}]


class LoomSizedResume:
    """A transport answering like GitHub for resume on a loom-sized repo, by
    path, counting each request; it refuses every write."""

    def __init__(self, sub_pages=None):
        self.lock = threading.Lock()
        self.calls = Counter()
        self.by_number = {i["number"]: i for i in OPEN_ISSUES}
        self.sub_pages = sub_pages or {}
        self.queries = []

    def __call__(self, method, url, headers, body):
        path = url.split("api.github.com")[-1]
        with self.lock:
            self.calls[(method, path)] += 1
        if path == "/graphql":
            return self._graphql(json.loads(body)["query"])
        if method != "GET":
            raise AssertionError("resume wrote: %s %s" % (method, path))
        bare = path.split("?", 1)[0]
        if bare == "/repos/%s/issues" % REPO:
            return self._reply(self._listing(path))
        for pattern, answer in ((ISSUE, self._single), (COMMENTS, self._comments),
                                (EVENTS, _events), (SUBS, self._sub_list)):
            found = pattern.match(bare)
            if found:
                value = answer(int(found.group(1)))
                return self._reply(value) if value is not None else (
                    404, {}, b'{"message": "Not Found"}')
        return self._reply([])

    def _graphql(self, query):
        with self.lock:
            self.queries.append(query)
        if query.lstrip().startswith("mutation"):
            raise AssertionError("resume wrote through GraphQL")
        return self._reply({"data": {"repository": self._states(query)}})

    def _single(self, number):
        """An open issue as listed; a closed ticket below 900; gone above."""
        return self.by_number.get(number) or (
            _issue(number, ["loom:ticket"], state="closed") if number < 900 else None)

    @staticmethod
    def _comments(number):
        return _home_comments(number) if number in GOALS else []

    @staticmethod
    def _listing(path):
        if "labels=epic" in path:
            return [e for g in GOALS for e in EPICS[g]]
        if "labels=loom:ticket" in path:
            return SHIPPED_TICKETS
        return [] if "labels=" in path else OPEN_ISSUES

    def _sub_list(self, number):
        return self.sub_pages.get(number) or _subs(number)

    def _states(self, query):
        out = {}
        for number in map(int, re.findall(r"issue\(number:(\d+)\)", query)):
            subs = self._sub_list(number)
            out["e%d" % number] = {"subIssues": {"totalCount": len(subs), "nodes": [
                {"number": s["number"], "state": s["state"].upper()} for s in subs[:100]]}}
        return out

    @staticmethod
    def _reply(body):
        return 200, {}, json.dumps(body).encode()

    def reads(self):
        return sum(self.calls.values())

    def sub_issue_lists(self):
        return sorted(int(SUBS.match(p.split("?")[0]).group(1))
                      for (m, p) in self.calls if m == "GET" and SUBS.match(p.split("?")[0]))


def _resume(fake):
    with unittest.mock.patch.object(brain, "_lock_holder", lambda *a, **k: None):
        return brain.resume(GitHub("t", transport=fake), [REPO], "lane-machine", LOCK_REPO,
                            NOW, dict(ENV))


def _every_epic_read(gh, repo, goal):
    """The tree as goal_tree reads it: every epic's sub-issues, through REST."""
    return brain.goal_tree(gh, repo, goal)[1]


class ResumeReadsALoomSizedRepoInBudget(unittest.TestCase):
    def test_the_loom_section_reads_under_the_bound(self):
        fake = LoomSizedResume()
        report = _resume(fake)
        self.assertNotIn("timed out", report)
        self.assertLessEqual(fake.reads(), READ_BOUND,
                             "resume read %d times for a loom-sized repo" % fake.reads())

    def test_no_epic_sub_issue_list_is_read_on_its_own(self):
        # The goals' own sub-issue lists name the epics; the epics' tickets
        # come in the one GraphQL query, and a closed or standing epic's
        # tickets are not read at all.
        fake = LoomSizedResume()
        _resume(fake)
        self.assertEqual(fake.sub_issue_lists(), list(GOALS))
        asked = " ".join(fake.queries)
        for goal in GOALS:
            for epic in EPICS[goal]:
                wanted = epic["state"] == "open" and not brain.standing_epic(epic)
                self.assertEqual("issue(number:%d)" % epic["number"] in asked, wanted)

    def test_the_text_is_the_same_as_reading_every_epic(self):
        fast = _resume(LoomSizedResume())
        slow_fake = LoomSizedResume()
        with unittest.mock.patch.object(brain, "_finishable_tree", _every_epic_read):
            slow = _resume(slow_fake)
        self.assertGreater(slow_fake.reads(), READ_BOUND)
        self.assertIn("- resume close-epic %s#%d" % (REPO, FINISHED), fast)
        self.assertEqual(1, fast.count("resume close-epic"))
        self.assertIn("- resume failed %s#501 (deferred by muse" % REPO, fast)
        self.assertIn("- dropped failed %s#901" % REPO, fast)
        self.assertEqual(fast.splitlines(), slow.splitlines())


class ResumePartsReadSideBySide(unittest.TestCase):
    """One repo's five parts ran one after another; on the remote brain that
    added up past the budget for loom. Four parts that wait for each other
    only finish when they run side by side."""

    def test_the_parts_of_one_repo_read_side_by_side(self):
        meet = threading.Barrier(4, timeout=5)

        def part(value):
            def read(*args, **kwargs):
                meet.wait()
                return value
            return read
        patches = [unittest.mock.patch.object(brain, "_ready_sensitive_in", part([])),
                   unittest.mock.patch.object(brain, "_finished_epic_lines", part(["- epic"])),
                   unittest.mock.patch.object(brain, "_verify_due_in", part([])),
                   unittest.mock.patch.object(audit, "due", part([]))]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        got = brain._resume_repo(None, REPO, [], {}, {}, NOW, "Grug - Elder", ())
        self.assertEqual(got, {"lines": [], "sensitive": [], "epics": ["- epic"],
                               "verify": [], "audit": []})

    def test_the_first_failing_part_in_order_is_raised(self):
        def fails(message):
            def read(*args, **kwargs):
                raise RuntimeError(message)
            return read
        with unittest.mock.patch.object(brain, "_ready_sensitive_in", fails("sensitive")), \
                unittest.mock.patch.object(brain, "_finished_epic_lines", lambda *a: []), \
                unittest.mock.patch.object(brain, "_verify_due_in", fails("verify")), \
                unittest.mock.patch.object(audit, "due", lambda *a: []):
            with self.assertRaisesRegex(RuntimeError, "sensitive"):
                brain._resume_repo(None, REPO, [], {}, {}, NOW, "Grug - Elder", ())


class InOrder(LoomSizedResume):
    """The fake, keeping the order of its requests."""

    def __init__(self):
        super().__init__()
        self.order = []

    def __call__(self, method, url, headers, body):
        with self.lock:
            self.order.append((method, url.split("api.github.com")[-1]))
        return super().__call__(method, url, headers, body)


class TheListingIsWarmBeforeThePartsStart(unittest.TestCase):
    """The goal part and the verify part both read the open issue listing,
    side by side. They do not race for it: the goal journals read it first,
    before any part starts, through the same once-per-pass client, so both
    parts are answered from that read and each open handoff item from the
    listing (loom#2088 review)."""

    def test_the_open_listing_is_read_once_before_any_part_reads(self):
        fake = InOrder()
        report = _resume(fake)
        self.assertIn("- resume failed %s#501 (deferred by muse" % REPO, report)
        listing = [k for k, (m, p) in enumerate(fake.order)
                   if p.split("?", 1)[0] == "/repos/%s/issues" % REPO and "labels=" not in p]
        self.assertEqual(len(listing), 1)
        parts = [k for k, (m, p) in enumerate(fake.order)
                 if SUBS.match(p.split("?", 1)[0]) or p == "/graphql" or "labels=" in p]
        self.assertTrue(parts)
        self.assertLess(listing[0], min(parts))
        single = [p for (m, p) in fake.order if ISSUE.match(p)
                  and int(ISSUE.match(p).group(1)) in fake.by_number]
        self.assertEqual(single, [])


class SubIssueStates(unittest.TestCase):
    def test_an_epic_past_one_page_of_sub_issues_is_read_through_rest(self):
        many = [_issue(10000 + k, ["loom:ticket"], state="closed") for k in range(150)]
        fake = LoomSizedResume(sub_pages={FINISHED: many})
        got = brain._sub_issue_states(GitHub("t", transport=fake), REPO, [FINISHED, FINISHED + 1])
        self.assertEqual([s["number"] for s in got[FINISHED]], [s["number"] for s in many])
        self.assertEqual(fake.sub_issue_lists(), [FINISHED])
        self.assertEqual({s["state"] for s in got[FINISHED + 1]}, {"open", "closed"})


if __name__ == "__main__":
    unittest.main()
