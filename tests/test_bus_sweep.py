"""The Muse sweep step (bus/sweep.py, bus.brain sweep, docs/MUSE.md step 9):
after triage only, under the tick's time budget, at most 10 actions and 5
claims, every claim released, claimed / wait:* / open-PR issues skipped,
and one report line."""
from __future__ import annotations

import json
import os
import sys
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain, invariants, sweep  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401
from bus.github import GitHubError  # noqa: E402

LOCK_REPO = "quadseven/loom"
REPO = "quadseven/loom"
START = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)


def stamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeGH:
    """GitHub in memory: open issues with labels, open pull requests, the
    repo's labels and the brain's lock branch. Tracks how many claim:muse
    labels are on issues at once."""

    def __init__(self, issues: Dict[int, Dict[str, Any]], prs: Optional[List[Dict[str, Any]]] = None,
                 label_exists: bool = True, holder: str = "muse",
                 tick: Optional[Dict[str, Any]] = None):
        self.issues = issues
        self.prs = prs or []
        self.repo_labels = {sweep.CLAIM_LABEL} if label_exists else set()
        self.calls: List[tuple] = []
        self.max_claims = 0
        self.fail_close: Dict[int, BaseException] = {}
        self.fail_claim: Dict[int, BaseException] = {}
        message = "holder=%s expires=%s" % (holder, stamp(START + timedelta(hours=1)))
        if tick is not None:
            message += "\n" + brain.STEP_MARKER + json.dumps(tick)
        self.commits = {"c0": {"message": message, "tree": {"sha": "tree"}}}
        self.head = "c0"

    def claims(self) -> int:
        return sum(1 for i in self.issues.values()
                   if sweep.CLAIM_LABEL in [x["name"] for x in i["labels"]])

    def paginate(self, path: str) -> List[Any]:
        self.calls.append(("PAGINATE", path))
        if "/issues?" in path:
            # Only quadseven/loom's issues live in this fake.
            if not path.startswith("/repos/%s/" % REPO):
                return []
            return [dict(i) for i in self.issues.values()
                    if i["state"] == "open" and sweep.CLAIM_LABEL in [x["name"] for x in i["labels"]]]
        return list(self.prs)

    def writes(self) -> List[tuple]:
        """Every call but a read."""
        return [c for c in self.calls if c[0] not in ("GET", "PAGINATE")]

    def rest(self, method: str, path: str, body: Any = None, ok: tuple = (), **_: Any):
        self.calls.append((method, path, body))
        out = self._rest(method, path, body)
        self.max_claims = max(self.max_claims, self.claims())
        return out

    def _rest(self, method: str, path: str, body: Any):
        if "/git/" in path:
            return self._git(method, path, body)
        if "/issues/" not in path:
            return self._repo_label(method, path, body)
        number = int(path.split("/issues/")[1].split("/")[0])
        if "/labels" in path:
            return self._issue_label(method, path, body, self.issues[number], self.fail_claim)
        if path.endswith("/comments"):
            return reply({})
        if method == "PATCH":
            if number in self.fail_close:
                raise self.fail_close[number]
            self.issues[number].update(body)
            return reply({})
        return reply(dict(self.issues[number]))

    def _git(self, method: str, path: str, body: Any):
        if method == "GET" and "/git/ref/heads/" in path:
            return reply({"object": {"sha": self.head}})
        if method == "GET":
            sha = path.rsplit("/", 1)[1]
            return reply(self.commits[sha]) if sha in self.commits else reply({}, 404)
        if method == "POST":
            sha = "c%d" % len(self.commits)
            self.commits[sha] = {"message": body["message"], "tree": {"sha": body["tree"]}}
            return reply({"sha": sha})
        self.head = body["sha"]
        return reply({})

    def _repo_label(self, method: str, path: str, body: Any):
        if method == "POST":
            self.repo_labels.add(body["name"])
            return reply({})
        return reply({}) if label_of(path) in self.repo_labels else reply({}, 404)

    @staticmethod
    def _issue_label(method: str, path: str, body: Any, issue: Dict[str, Any],
                     fail_claim: Dict[int, BaseException]):
        names = [x["name"] for x in issue["labels"]]
        if method == "POST":
            issue["labels"] += [{"name": n} for n in body["labels"] if n not in names]
            if issue["number"] in fail_claim:
                # The label applied, but the answer was an error.
                raise fail_claim[issue["number"]]
            return reply({})
        if label_of(path) not in names:
            return reply({}, 404)
        issue["labels"] = [x for x in issue["labels"] if x["name"] != label_of(path)]
        return reply({})


def reply(data: Any, status: int = 200) -> SimpleNamespace:
    return SimpleNamespace(status=status, data=data)


def label_of(path: str) -> str:
    return path.rsplit("/", 1)[1].replace("%3A", ":")


def issue(number: int, *labels: str) -> Dict[str, Any]:
    return {"number": number, "state": "open", "title": "Issue %d" % number,
            "body": "", "labels": [{"name": x} for x in labels]}


def close(number: int, repo: str = REPO, action: str = "done") -> Dict[str, Any]:
    return {"repo": repo, "number": number, "action": action,
            "evidence": "merged in PR #9 (read this tick)"}


def at_triage(minutes: float = 5) -> Dict[str, Any]:
    return {"step": "triage", "started_at": stamp(START)}, (lambda: START + timedelta(minutes=minutes))


def no_rewrite(*_: Any) -> str:
    raise AssertionError("no rewrite planned")


class TickStepsTest(unittest.TestCase):
    def test_sweep_sits_after_triage_and_before_journal(self):
        steps = brain.TICK_STEPS
        self.assertEqual(steps.index("sweep"), steps.index("triage") + 1)
        self.assertEqual(steps.index("journal"), steps.index("sweep") + 1)
        for first in ("merge", "verify", "failed"):
            self.assertLess(steps.index(first), steps.index("sweep"))
        self.assertIn("sweep", invariants.STEPS)

    def test_step_sweep_is_accepted_and_takes_a_note_under_budget(self):
        tick = {"started_at": stamp(START), "step": "triage", "step_index": 8, "notes": []}
        gh = FakeGH({}, tick=tick)
        saved = brain.tick_step(gh, LOCK_REPO, "sweep", "swept 0", "muse",
                                START + timedelta(minutes=10))
        self.assertEqual((saved["step"], saved["step_index"]), ("sweep", 9))
        self.assertEqual(saved["notes"][-1]["text"], "swept 0")
        self.assertEqual(saved["sweep"], "ok")

    def test_step_sweep_past_twenty_minutes_is_skipped_and_says_so(self):
        tick = {"started_at": stamp(START), "step": "triage", "step_index": 8, "notes": []}
        gh = FakeGH({}, tick=tick)
        saved = brain.tick_step(gh, LOCK_REPO, "sweep", holder="muse",
                                now=START + timedelta(minutes=21))
        self.assertIn("skipped: the tick used 21 of its 25 minutes", saved["notes"][-1]["text"])
        # The journal step still runs after a skipped sweep.
        later = brain.tick_step(gh, LOCK_REPO, "journal", holder="muse",
                                now=START + timedelta(minutes=22))
        self.assertEqual(later["step"], "journal")

    def test_sweep_after_the_journal_is_refused(self):
        for step in ("journal", "unlock"):
            gh = FakeGH({1: issue(1)})
            result = sweep.run(gh, [close(1)], {"step": step, "started_at": stamp(START)},
                               brain.TICK_STEPS, no_rewrite,
                               clock=lambda: START + timedelta(minutes=2))
            self.assertIn("before the journal", result.stopped, step)
            self.assertEqual(gh.writes(), [], step)
        tick = {"started_at": stamp(START), "step": "journal", "step_index": 10, "notes": []}
        with self.assertRaisesRegex(ValueError, "only after the triage step"):
            brain.tick_step(FakeGH({}, tick=tick), LOCK_REPO, "sweep", holder="muse",
                            now=START + timedelta(minutes=2))

    def test_step_sweep_before_triage_is_refused(self):
        tick = {"started_at": stamp(START), "step": "merge", "step_index": 3, "notes": []}
        gh = FakeGH({}, tick=tick)
        with self.assertRaisesRegex(ValueError, "only after the triage step"):
            brain.tick_step(gh, LOCK_REPO, "sweep", holder="muse", now=START + timedelta(minutes=2))
        # Merge, verify and failed are recorded as before, sweep or not.
        for step in ("merge", "verify", "failed"):
            self.assertEqual(brain.tick_step(gh, LOCK_REPO, step, holder="muse",
                                             now=START + timedelta(minutes=23))["step"], step)


class GuardTest(unittest.TestCase):
    def test_runs_only_after_triage(self):
        for step in ("lock", "status", "merge", "verify", "failed", "asks", "brief"):
            record = {"step": step, "started_at": stamp(START)}
            gh = FakeGH({1: issue(1)})
            result = sweep.run(gh, [close(1)], record, brain.TICK_STEPS, no_rewrite,
                               clock=lambda: START + timedelta(minutes=1))
            self.assertIn("only after the triage step", result.stopped, step)
            self.assertEqual(gh.writes(), [], step)

    def test_skipped_at_twenty_minutes_and_says_so(self):
        record, _ = at_triage()
        gh = FakeGH({1: issue(1)})
        result = sweep.run(gh, [close(1)], record, brain.TICK_STEPS, no_rewrite,
                           clock=lambda: START + timedelta(minutes=20))
        self.assertEqual(gh.writes(), [])
        self.assertEqual(sweep.report_line(result),
                         "swept 0 (closed 0, rewritten 0, skipped 0): skipped, the tick used "
                         "20 of its 25 minutes (the sweep needs under 20)")

    def test_under_budget_it_runs(self):
        record, clock = at_triage(19)
        gh = FakeGH({1: issue(1)})
        result = sweep.run(gh, [close(1)], record, brain.TICK_STEPS, no_rewrite, clock=clock)
        self.assertEqual(result.closed, 1)
        self.assertEqual(gh.issues[1]["state"], "closed")

    def test_stops_mid_sweep_when_the_budget_runs_out(self):
        record, _ = at_triage()
        times = iter([5, 6, 21, 21, 21])
        gh = FakeGH({n: issue(n) for n in (1, 2, 3)})
        result = sweep.run(gh, [close(1), close(2), close(3)], record, brain.TICK_STEPS,
                           no_rewrite, clock=lambda: START + timedelta(minutes=next(times)))
        self.assertEqual(result.closed, 1)
        self.assertTrue(result.stopped.startswith("stopped, the tick used 21"))
        self.assertEqual(gh.claims(), 0)


class CapsTest(unittest.TestCase):
    def test_stops_at_ten_actions(self):
        record, clock = at_triage()
        gh = FakeGH({n: issue(n) for n in range(1, 13)})
        result = sweep.run(gh, [close(n) for n in range(1, 13)], record, brain.TICK_STEPS,
                           no_rewrite, clock=clock)
        self.assertEqual(result.closed, 10)
        self.assertEqual([n for n, i in gh.issues.items() if i["state"] == "open"], [11, 12])
        self.assertEqual(result.stopped, "stopped at the cap of 10 actions")

    def test_never_holds_more_than_five_claims_and_releases_them_all(self):
        record, clock = at_triage()
        gh = FakeGH({n: issue(n) for n in range(1, 11)})
        sweep.run(gh, [close(n) for n in range(1, 11)], record, brain.TICK_STEPS,
                  no_rewrite, clock=clock)
        self.assertEqual(gh.max_claims, 5)
        self.assertEqual(gh.claims(), 0)

    def test_releases_every_claim_when_an_action_raises(self):
        record, clock = at_triage()
        gh = FakeGH({n: issue(n) for n in range(1, 6)})
        gh.fail_close[2] = RuntimeError("boom")
        with self.assertRaisesRegex(RuntimeError, "boom"):
            sweep.run(gh, [close(n) for n in range(1, 6)], record, brain.TICK_STEPS,
                      no_rewrite, clock=clock)
        self.assertEqual(gh.claims(), 0)
        self.assertEqual(gh.max_claims, 5)

    def test_a_refused_action_is_skipped_and_the_sweep_goes_on(self):
        record, clock = at_triage()
        gh = FakeGH({n: issue(n) for n in (1, 2)})
        gh.fail_close[1] = GitHubError("PATCH refused: 422")
        result = sweep.run(gh, [close(1), close(2)], record, brain.TICK_STEPS,
                           no_rewrite, clock=clock)
        self.assertEqual((result.closed, result.skipped), (1, 1))
        self.assertEqual(gh.claims(), 0)

    def test_creates_the_claim_label_when_missing(self):
        record, clock = at_triage()
        gh = FakeGH({1: issue(1)}, label_exists=False)
        sweep.run(gh, [close(1)], record, brain.TICK_STEPS, no_rewrite, clock=clock)
        made = [c for c in gh.calls if c[0] == "POST" and c[1] == "/repos/%s/labels" % REPO]
        self.assertEqual(len(made), 1)
        self.assertEqual(made[0][2]["name"], "claim:muse")
        claimed = [i for i, c in enumerate(gh.calls) if c[0] == "POST" and c[1].endswith("/1/labels")]
        closed = [i for i, c in enumerate(gh.calls) if c[0] == "PATCH"]
        self.assertLess(claimed[0], closed[0], "claimed before touched")


class SkipTest(unittest.TestCase):
    def test_skips_claimed_waiting_and_open_pr_issues(self):
        record, clock = at_triage()
        issues = {1: issue(1, "claim:ccqw"), 2: issue(2, "wait:ci"), 3: issue(3),
                  4: issue(4, "loom:running"), 5: issue(5, "epic"), 6: issue(6),
                  7: issue(7, "bug")}
        prs = [{"number": 40, "title": "Fix it", "body": "Refs #3", "head": {"ref": "x"}},
               {"number": 41, "title": "", "body": "", "head": {"ref": "loom/6-thing"}}]
        gh = FakeGH(issues, prs)
        result = sweep.run(gh, [close(n) for n in range(1, 8)], record, brain.TICK_STEPS,
                           no_rewrite, clock=clock)
        self.assertEqual((result.closed, result.skipped), (1, 6))
        self.assertEqual([n for n, i in gh.issues.items() if i["state"] == "closed"], [7])
        for n in range(1, 7):
            self.assertFalse(any(c[0] == "POST" and c[1].endswith("/issues/%d/labels" % n)
                                 for c in gh.calls), "never claimed #%d" % n)
        text = "\n".join(result.lines)
        for why in ("held (claim:ccqw)", "held (wait:ci)", "open pull request #40",
                    "open pull request #41", "held (loom:running)", "an epic"):
            self.assertIn(why, text)

    def test_a_close_shadow_mode_holds_is_skipped_and_released(self):
        record, clock = at_triage()
        gh = FakeGH({1: issue(1)})
        with unittest.mock.patch.object(sweep.shadow, "guard", return_value={"new": True}):
            result = sweep.run(gh, [close(1)], record, brain.TICK_STEPS, no_rewrite, clock=clock)
        self.assertEqual((result.closed, result.skipped), (0, 1))
        self.assertEqual(gh.issues[1]["state"], "open")
        self.assertFalse(any(c[1].endswith("/comments") for c in gh.calls), "no note on a held close")
        self.assertEqual(gh.claims(), 0)

    def test_skips_person_asks_holds_and_parents_and_never_claims_them(self):
        record, clock = at_triage()
        groups = ["loom:human", "needs:quadseven", "loom:shipped", "loom:backlog",
                  "awaiting-live-check", "handoff:claude", "needs-operator", "loom:triage"]
        issues = {n: issue(n, label) for n, label in enumerate(groups, 1)}
        parent = issue(len(groups) + 1)
        parent["sub_issues_summary"] = {"total": 1, "completed": 0}
        issues[parent["number"]] = parent
        gh = FakeGH(issues)
        result = sweep.run(gh, [close(n) for n in issues], record, brain.TICK_STEPS,
                           no_rewrite, clock=clock)
        self.assertEqual((result.closed, result.skipped), (0, len(issues)))
        self.assertEqual(gh.writes(), [], "nothing claimed, closed or commented")
        text = "\n".join(result.lines)
        for label in groups:
            self.assertIn("held (%s)" % label, text)
        self.assertIn("#%d: an epic" % parent["number"], text)

    def test_a_dead_ticks_claim_is_released_at_the_start(self):
        record, clock = at_triage()
        gh = FakeGH({1: issue(1, "claim:muse"), 2: issue(2)})
        result = sweep.run(gh, [close(2)], record, brain.TICK_STEPS, no_rewrite, clock=clock)
        self.assertEqual(gh.claims(), 0)
        self.assertEqual(gh.issues[1]["state"], "open", "released, not acted on")
        self.assertIn("released quadseven/loom#1: a claim left by an earlier tick", result.lines)
        self.assertEqual(result.closed, 1)

    def test_a_dead_ticks_claim_is_released_even_when_the_sweep_is_refused(self):
        gh = FakeGH({1: issue(1, "claim:muse")})
        sweep.run(gh, [], {"step": "journal", "started_at": stamp(START)}, brain.TICK_STEPS,
                  no_rewrite, clock=lambda: START + timedelta(minutes=1))
        self.assertEqual(gh.claims(), 0)

    def test_a_claim_whose_post_raised_is_still_released(self):
        record, clock = at_triage()
        gh = FakeGH({1: issue(1), 2: issue(2)})
        gh.fail_claim[2] = GitHubError("POST answered 502")
        with self.assertRaises(GitHubError):
            sweep.run(gh, [close(1), close(2)], record, brain.TICK_STEPS, no_rewrite, clock=clock)
        self.assertEqual(gh.claims(), 0)
        self.assertEqual(gh.issues[1]["state"], "open", "nothing acted before the claims held")

    def test_only_loom_and_grug(self):
        record, clock = at_triage()
        gh = FakeGH({1: issue(1)})
        result = sweep.run(gh, [close(1, repo="quadseven/infra")], record, brain.TICK_STEPS,
                           no_rewrite, clock=clock)
        self.assertEqual((result.closed, result.skipped), (0, 1))
        self.assertEqual(gh.writes(), [])

    def test_a_close_needs_one_line_of_evidence(self):
        record, clock = at_triage()
        gh = FakeGH({1: issue(1)})
        result = sweep.run(gh, [{"repo": REPO, "number": 1, "action": "done"}], record,
                           brain.TICK_STEPS, no_rewrite, clock=clock)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(gh.issues[1]["state"], "open")

    def test_issue_number_match_is_exact(self):
        self.assertFalse(sweep.names_issue({"title": "#12", "body": "", "head": {}}, 1))
        self.assertTrue(sweep.names_issue({"title": "", "body": "Part of quadseven/loom#1",
                                           "head": {}}, 1))


class ReportTest(unittest.TestCase):
    def test_report_line_format(self):
        result = sweep.Result(closed=3, rewritten=2, skipped=1)
        self.assertEqual(sweep.report_line(result), "swept 6 (closed 3, rewritten 2, skipped 1)")
        self.assertEqual(sweep.report_line(), "swept 0 (closed 0, rewritten 0, skipped 0)")

    def test_close_reasons_and_rewrite(self):
        record, clock = at_triage()
        gh = FakeGH({n: issue(n) for n in (1, 2, 3)})
        rewrites = []

        def rewrite(repo, number, epic, title, body):
            rewrites.append((repo, number, epic, title))
            return "ticket %s#%d" % (repo, number)

        plan = [close(1, action="duplicate"),
                {"repo": REPO, "number": 2, "action": "wontdo", "evidence": "no use left"},
                {"repo": REPO, "number": 3, "action": "rewrite", "epic": 30,
                 "title": "One exact ask", "body": "## Why\nx"}]
        result = sweep.run(gh, plan, record, brain.TICK_STEPS, rewrite, clock=clock)
        self.assertEqual(sweep.report_line(result), "swept 3 (closed 2, rewritten 1, skipped 0)")
        self.assertEqual(gh.issues[1]["state_reason"], "duplicate")
        self.assertEqual(gh.issues[2]["state_reason"], "not_planned")
        self.assertEqual(rewrites, [(REPO, 3, 30, "One exact ask")])
        self.assertEqual(gh.claims(), 0)

    def test_run_sweep_records_the_report_line_as_the_step_note(self):
        tick = {"started_at": stamp(START), "step": "triage", "step_index": 8, "notes": []}
        gh = FakeGH({1: issue(1)}, tick=tick)
        line, _ = brain.run_sweep(gh, LOCK_REPO, "muse", [close(1)],
                                  clock=lambda: START + timedelta(minutes=8))
        self.assertEqual(line, "swept 1 (closed 1, rewritten 0, skipped 0)")
        record = brain.tick_step(gh, LOCK_REPO)
        self.assertEqual(record["step"], "sweep")
        self.assertEqual(record["notes"][-1]["text"], line)

    def test_run_sweep_refuses_a_non_holder(self):
        gh = FakeGH({1: issue(1)}, holder="other")
        with self.assertRaisesRegex(ValueError, "does not hold the lock"):
            brain.run_sweep(gh, LOCK_REPO, "muse", [close(1)],
                            clock=lambda: START + timedelta(minutes=8))


if __name__ == "__main__":
    unittest.main()
