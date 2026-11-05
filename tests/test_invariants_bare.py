"""loom#2526: a report carrying loom:invariant alone (its triage label came
off and no ticket was made of it) had no close path once its invariant
cleared, except the settle rules of lane-pr-stalled and epic-over-cap. Such
reports stayed open for weeks. Each kind the filer writes now clears like a
triage report, within the per-pass cap, unless something holds it."""
from __future__ import annotations

import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import invariants  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401 - shadow off: these tests close
from test_invariants import AUTHOR, GOAL, REPO, T0, FakeGH, stamp  # noqa: E402

# Every kind the filer writes but the SETTLES ones: none had a close path for
# a cleared report that carries loom:invariant alone.
NO_SETTLE_RULE = (
    "brain-failing", "brain-silent", "metrics-cadence", "report-cadence", "lane-no-success",
    "lane-failing-streak", "lane-held", "ticket-outside-epic", "goal-epics-over-cap",
    "closed-epic-open-tickets", "epic-title-shared", "gate-hold", "plan-check-failed",
    "remote-silent", "silent-lane", "repeated-failure", "ticket-waiting", "untrusted-labeler",
    "roll-pin-off", "merged-not-shipped", "review-overdue", "guard-no-check",
    "guard-unreadable", "queued-ticket-no-headers")
QUIET = T0 - invariants.CLEAR_AFTER - 60
HELD = (("claim:someone",), ("needs:someone",), ("loom:human",), ("awaiting-live-check",),
        ("loom:running",), ("loom:pr",), ("loom:ready",), ("loom:merged",))


def kinds_in_source():
    with open(invariants.__file__, encoding="utf-8") as fh:
        text = fh.read()
    named = {"EPIC_OVER_CAP": invariants.EPIC_OVER_CAP}
    return (set(re.findall(r'Breach\("([a-z-]+)"', text))
            | {named[n] for n in re.findall(r"Breach\(\s*([A-Z_]+),", text)})


class BareReportTest(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGH()
        self.said = []
        patcher = mock.patch.dict(os.environ, {"LOOM_BRIEF_AUTHORS": AUTHOR})
        patcher.start()
        self.addCleanup(patcher.stop)

    def report(self, invariant, n, labels=(), created=QUIET):
        """An older report of the loom#1009 shape: its subject in its title,
        loom:invariant its only label (plus any in `labels`)."""
        number = 700 + n
        subject = "%s#%d" % (REPO, 600 + n)
        key = invariants.Breach(invariant, subject, "t", (), T0).key
        self.gh.issues[number] = {
            "number": number, "id": 9000 + number, "state": "open", "closed_at": None,
            "title": "Report about #%d" % (600 + n), "created_at": stamp(created),
            "labels": [{"name": x} for x in (invariants.LABEL,) + tuple(labels)],
            "body": "%s\nWhat broke.\n\nPart of #%d" % (invariants.MARKER % key, GOAL)}
        return self.gh.issues[number], invariants.Breach(invariant, subject, "t", ("x",), T0)

    def run_filer(self, breaches=(), now=T0):
        filer = invariants.Filer(self.gh, REPO, GOAL, {}, now, self.said.append)
        filer.run(list(breaches))
        return filer

    def notes(self, issue):
        return [c["body"] for c in self.gh.comments.get((REPO, issue["number"]), ())]

    def test_the_kinds_listed_are_every_kind_the_filer_writes_but_the_settled_ones(self):
        self.assertEqual(set(NO_SETTLE_RULE) | set(invariants.SETTLES), kinds_in_source())
        self.assertFalse(set(NO_SETTLE_RULE) & set(invariants.SETTLES))

    def test_a_cleared_bare_report_of_a_settled_kind_whose_subject_cannot_settle_closes(self):
        # A subject the settled callback cannot read (a private repo, no
        # callback) left these open too: 13 lane-pr-stalled reports on 2026-11-02.
        for n, kind in enumerate(invariants.SETTLES):
            self.gh = FakeGH()
            issue, _ = self.report(kind, n)
            filer = self.run_filer()
            self.assertEqual(issue["state"], "closed", (kind, self.said))
            self.assertEqual(len(self.notes(issue)), 1, kind)
            self.assertLessEqual(filer.writes, invariants.WRITES_MAX)

    def test_many_cleared_bare_reports_close_a_few_a_pass_within_the_cap(self):
        issues = [self.report("brain-silent", n)[0] for n in range(3)]
        issues += [self.report("plan-check-failed", n)[0] for n in range(3, 6)]
        filer = self.run_filer()
        self.assertLessEqual(filer.writes, invariants.WRITES_MAX)
        self.assertLessEqual(filer.folds, invariants.FOLD_MAX)
        first = sum(i["state"] == "closed" for i in issues)
        self.assertGreater(first, 0)
        self.assertLess(first, len(issues))
        for _ in range(len(issues)):
            filer = self.run_filer()
            self.assertLessEqual(filer.writes, invariants.WRITES_MAX)
        self.assertEqual([i["state"] for i in issues], ["closed"] * len(issues), self.said)
        for issue in issues:
            self.assertEqual(len(self.notes(issue)), 1, issue["number"])

    def test_a_bare_report_still_in_violation_stays_open(self):
        # A ROLLED kind files no report per violation: its live subject is a
        # line of the tracking issue (RolledKindTest), so it is left out here.
        found = [self.report(kind, n) for n, kind in enumerate(NO_SETTLE_RULE)
                 if kind not in invariants.ROLLED]
        for _ in range(3):
            self.run_filer([breach for _, breach in found])
        for issue, breach in found:
            self.assertEqual(issue["state"], "open", breach.invariant)
            self.assertEqual(self.notes(issue), [], breach.invariant)

    def test_a_bare_report_broken_again_inside_the_clear_wait_stays_open(self):
        issue, _ = self.report("brain-silent", 1, created=T0 - 30 * 24 * 3600)
        self.gh.comment(issue["number"], T0 - 3600, "%s\n%s evidence" % (
            invariants.BROKEN_MARKER, invariants.BROKEN_TEXT))
        self.run_filer()
        self.assertEqual(issue["state"], "open")
        self.run_filer(now=T0 + invariants.CLEAR_AFTER)
        self.assertEqual(issue["state"], "closed", self.said)

    def test_a_held_report_stays_open_whatever_its_kind(self):
        issues = []
        for n, labels in enumerate(HELD):
            issues.append(self.report("brain-silent", n, labels)[0])
            issues.append(self.report("plan-check-failed", 50 + n, labels)[0])
        for _ in range(3):
            self.run_filer()
        self.assertEqual([i["state"] for i in issues], ["open"] * len(issues), self.said)
        self.assertEqual(self.gh.comments, {})


def _closes(kind, n):
    def test(self):
        issue, _ = self.report(kind, n)
        filer = self.run_filer()
        self.assertEqual(issue["state"], "closed", self.said)
        self.assertEqual(issue.get("state_reason"), "not_planned")
        (note,) = self.notes(issue)
        self.assertTrue(invariants.CLEARED_TEXT in note or "no longer files one issue" in note,
                        note)
        self.assertLessEqual(filer.writes, invariants.WRITES_MAX)
        self.assertLessEqual(filer.folds, invariants.FOLD_MAX)
    test.__doc__ = "A cleared %s report carrying loom:invariant alone closes." % kind
    return test


for _n, _kind in enumerate(NO_SETTLE_RULE):
    setattr(BareReportTest, "test_a_cleared_bare_%s_report_closes" % _kind.replace("-", "_"),
            _closes(_kind, _n))


if __name__ == "__main__":
    unittest.main()
