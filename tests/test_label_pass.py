"""One label pass removes contradictory and stale state labels (loom#2024).

Issues kept labels that said two things at once: loom:answered beside
loom:human and loom:ticket, needs-brain beside loom:human and needs:<login>,
loom:triage after the issue was handled, a lane label days after its lane
answered no change, and a body Part of #N naming another epic than the
sub-issue parent.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock
from urllib.parse import parse_qs, unquote, urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain, label_pass, shadow, station  # noqa: E402
from bus.github import GitHub  # noqa: E402

REPO = "example-owner/example-repo"
STATION = ["loom-app[bot]"]


def issue(number, labels, state="open", body="", parent=None, assignees=()):
    found = {"number": number, "title": "Issue %d" % number, "state": state,
             "body": body, "user": {"login": "someone"},
             "labels": [{"name": x} for x in labels],
             "assignees": [{"login": x} for x in assignees]}
    if parent is not None:
        found["parent_issue_url"] = "https://api.github.com/repos/%s/issues/%d" % (REPO, parent)
    return found


def labeled(name, by):
    return {"event": "labeled", "actor": {"login": by}, "label": {"name": name}}


def removed(found):
    return sorted(r.label for r in found.remove)


class Plan(unittest.TestCase):
    def test_the_state_labels_are_the_brains(self):
        self.assertEqual(tuple("loom:" + s for s in brain.STATES), label_pass.STATE_LABELS)

    def test_at_most_one_state_label_stays_the_newest_the_station_set(self):
        found = label_pass.plan(
            issue(1, ("loom:ticket", "loom:answered", "loom:queued", "loom:human")),
            [labeled("loom:answered", "loom-app[bot]"), labeled("loom:queued", "loom-app[bot]"),
             labeled("loom:answered", "a-person")],
            STATION)
        self.assertEqual(["loom:answered", "loom:human"], removed(found))
        self.assertEqual(["needs:human"], found.add)

    def test_without_a_station_event_the_newest_by_anyone_stays_then_the_furthest(self):
        both = issue(1, ("loom:pr", "loom:merged"))
        self.assertEqual(["loom:pr"], removed(label_pass.plan(
            both, [labeled("loom:pr", "a-person"), labeled("loom:merged", "a-person")], STATION)))
        self.assertEqual(["loom:pr"], removed(label_pass.plan(both, [], STATION)))

    def test_one_state_label_is_left_alone_and_needs_no_events(self):
        one = issue(1, ("loom:ticket", "loom:pr", "loom:ready"))
        self.assertFalse(label_pass.needs_events(one))
        self.assertEqual([], removed(label_pass.plan(one)))

    def test_needs_brain_goes_when_a_person_holds_the_issue(self):
        for holder in ("loom:human", "needs:example-op"):
            with self.subTest(holder=holder):
                found = label_pass.plan(issue(1, ("needs-brain", holder)))
                self.assertIn("needs-brain", removed(found))
        self.assertEqual([], removed(label_pass.plan(issue(1, ("needs-brain", "needs-operator")))))

    def test_triage_goes_once_the_issue_is_a_ticket_or_closed(self):
        self.assertEqual(["loom:triage"], removed(label_pass.plan(
            issue(1, ("loom:triage", "loom:ticket", "loom:queued")))))
        self.assertEqual(["loom:triage"], removed(label_pass.plan(
            issue(1, ("loom:triage",), state="closed"))))
        self.assertEqual([], removed(label_pass.plan(issue(1, ("loom:triage",)))))

    def test_a_lane_label_goes_when_its_lane_answered_no_change(self):
        self.assertEqual(["lane:codex"], removed(label_pass.plan(
            issue(1, ("loom:ticket", "loom:answered", "lane:codex")))))
        for state in ("loom:briefed", "loom:running", "loom:pr"):
            with self.subTest(state=state):
                self.assertEqual([], removed(label_pass.plan(
                    issue(1, ("loom:ticket", state, "lane:codex")))))

    def test_a_part_of_line_naming_another_parent_is_one_health_note(self):
        found = label_pass.plan(issue(1, ("loom:ticket",), body="Why.\n\nPart of #12\n",
                                      parent=40))
        self.assertEqual([], found.remove)
        self.assertEqual("body says Part of #12 but the sub-issue parent is #40", found.note)
        for body, parent in (("Part of #40\n", 40), ("Part of #12\n", None), ("", 40)):
            with self.subTest(body=body, parent=parent):
                self.assertEqual("", label_pass.plan(issue(1, (), body=body, parent=parent)).note)


class Fake:
    def __init__(self):
        self.open, self.closed, self.events, self.writes = {}, {}, {}, []

    def __call__(self, method, url, headers, body):
        parts = urlsplit(url)
        rest = parts.path[len("/repos/%s/" % REPO):]
        query = parse_qs(parts.query)
        if method != "GET":
            self.writes.append((method, unquote(rest)))
            return 200, {}, b"{}"
        if rest == "issues":
            pool = self.closed if query.get("state") == ["closed"] else self.open
            return 200, {}, json.dumps(list(pool.values())).encode()
        if rest.endswith("/events"):
            return 200, {}, json.dumps(self.events.get(int(rest.split("/")[1]), [])).encode()
        if rest.endswith("/comments"):
            return 200, {}, b"[]"
        raise AssertionError("unexpected %s %s" % (method, url))


class Step(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.fake = Fake()
        self.settings = station.Settings(os.path.join(tmp.name, "loom"), [REPO], ["me"],
                                         ["example-owner"], briefers=["other-op"])
        self.fake.open[1] = issue(1, ("loom:ticket", "loom:answered", "loom:queued", "lane:codex"))
        self.fake.events[1] = [labeled("loom:answered", "me"), labeled("loom:queued", "me"),
                               labeled("loom:answered", "me")]
        self.fake.open[2] = issue(2, ("needs-brain", "loom:human"))
        self.fake.open[3] = issue(3, ("needs-brain", "loom:human", "claim:other-op"))
        self.fake.open[4] = issue(4, ("loom:ticket",), body="Part of #12\n", parent=40)
        self.fake.closed[5] = issue(5, ("loom:triage",), state="closed")

    def run_step(self, shadow_on):
        env = {"LOOM_SHADOW": "1" if shadow_on else "0",
               "LOOM_SHADOW_LEDGER": os.path.join(self.tmp, "ledger.jsonl")}
        with mock.patch.dict(os.environ, env), mock.patch.object(station, "_say") as said:
            station.Station(GitHub("t", transport=self.fake), self.settings,
                            now=time.time()).clean_labels()
        return [c.args[0] for c in said.call_args_list]

    def test_the_pass_removes_each_stale_label_and_leaves_another_operators_issue(self):
        said = self.run_step(shadow_on=False)
        self.assertEqual([("DELETE", "issues/1/labels/loom:queued"),
                          ("DELETE", "issues/1/labels/lane:codex"),
                          ("POST", "issues/2/labels"),
                          ("DELETE", "issues/2/labels/needs-brain"),
                          ("DELETE", "issues/2/labels/loom:human"),
                          ("DELETE", "issues/5/labels/loom:triage")], self.fake.writes)
        notes = [line for line in said if line.startswith("health note")]
        self.assertEqual(["health note %s#4: body says Part of #12 but the sub-issue "
                          "parent is #40" % REPO], notes)

    def test_the_health_note_is_said_once(self):
        self.run_step(shadow_on=False)
        path = os.path.join(self.settings.home, "state", "station", label_pass.STATE_FILE)
        with open(path) as fh:
            state = json.load(fh)
        state["_at"] = 0
        with open(path, "w") as fh:
            json.dump(state, fh)
        said = self.run_step(shadow_on=False)
        self.assertEqual([], [line for line in said if line.startswith("health note")])

    def test_in_shadow_each_removal_is_a_ledger_line_and_none_is_sent(self):
        self.run_step(shadow_on=True)
        self.assertEqual([], self.fake.writes)
        entries = shadow.read(os.path.join(self.tmp, "ledger.jsonl"))
        self.assertEqual([(1, "loom:queued"), (1, "lane:codex"), (2, "needs:human"),
                          (2, "needs-brain"), (2, "loom:human"), (5, "loom:triage")],
                         [(e["number"], e["label"]) for e in entries])
        self.assertEqual({shadow.UNLABEL, shadow.LABEL}, {e["action"] for e in entries})
        self.assertIn("label removal", " ".join(shadow.digest_lines(
            0, time.time(), os.path.join(self.tmp, "ledger.jsonl"), {"LOOM_SHADOW": "1"})))

    def test_the_pass_runs_at_most_once_an_hour(self):
        self.run_step(shadow_on=False)
        self.fake.writes.clear()
        self.run_step(shadow_on=False)
        self.assertEqual([], self.fake.writes)


if __name__ == "__main__":
    unittest.main()
