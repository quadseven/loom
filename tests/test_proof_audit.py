"""Audits of lane-made proofs (bus/audit.py, brain.audit_proof)."""
from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import datetime, timezone
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import audit, brain, proof  # noqa: E402
from bus.github import GitHub  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401 - shadow off: an audit fail reopens

REPO = "example-owner/example-repo"
NOW = datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc)
ENV = {"LOOM_BRIEF_AUTHORS": "station-bot"}
BODY = "## Acceptance criteria\n- [x] A test passes\n- [x] Live: the daily line shows it\n"


def at(hours_ago):
    return datetime.fromtimestamp(NOW.timestamp() - hours_ago * 3600, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def proof_comment(hours_ago, verified_by="zen/opencode/model-free", login="station-bot"):
    record = {"kind": "docs", "revision": "abc1234", "verified_by": verified_by,
              "observed": {"start": "2030-01-01T00:00:00Z", "end": "2030-01-01T01:00:00Z"},
              "sources": ["https://example.com/logs/1"],
              "criteria": [{"criterion": "A test passes", "result": "pass", "evidence": "ci"},
                           {"criterion": "Live: the daily line shows it", "result": "pass",
                            "evidence": "seen at 2030-01-02T01:00Z"}]}
    return {"body": proof.render(record, BODY), "created_at": at(hours_ago),
            "user": {"login": login}}


def audit_comment(hours_ago, result="fail", lane="zen", login="station-bot"):
    return {"body": audit.marker(result, lane) + "\nAudit.", "created_at": at(hours_ago),
            "user": {"login": login}}


class Tracker:
    """A stateful issues API for one repo: issues by number, their comments,
    labels and open or closed state."""

    def __init__(self):
        self.issues = {}
        self.comments = {}
        self.calls = []

    def add(self, number, labels, comments, state="open"):
        self.issues[number] = {"number": number, "title": "T%d" % number, "state": state,
                               "body": BODY,
                               "labels": [{"name": x} for x in ["loom:ticket"] + list(labels)]}
        self.comments[number] = list(comments)

    def names(self, number):
        return [x["name"] for x in self.issues[number]["labels"]]

    def __call__(self, method, url, headers, body):
        parts = urlsplit(url)
        data = json.loads(body) if body else None
        self.calls.append((method, parts.path + "?" + parts.query, data))
        rest = parts.path[len("/repos/%s/" % REPO):]
        if rest == "issues":
            label = dict(x.split("=", 1) for x in parts.query.split("&"))["labels"]
            found = [i for i in self.issues.values() if label in self.names(i["number"])]
            return 200, {}, json.dumps(found).encode()
        number, _, sub = rest[len("issues/"):].partition("/")
        return self._issue(method, int(number), sub, data)

    def _issue(self, method, number, sub, data):
        issue = self.issues[number]
        if method == "GET":
            return 200, {}, json.dumps(self.comments[number] if sub else issue).encode()
        if sub == "comments":
            self.comments[number].append({"body": data["body"], "created_at": at(0),
                                          "user": {"login": "station-bot"}})
        elif sub == "labels":
            have = self.names(number) if method == "POST" else []
            issue["labels"] = [{"name": x} for x in have + [x for x in data["labels"] if x not in have]]
        else:
            issue.update(data)
        return 200, {}, b"{}"

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


class AuditProofTest(unittest.TestCase):
    def setUp(self):
        self.fake = Tracker()
        self.gh = GitHub("t", transport=self.fake)

    def run_audit(self, result, note="the station log for 2030-01-02 has no daily line"):
        return brain.audit_proof(self.gh, REPO, 5, result, note, env=ENV)

    def test_missing_evidence_moves_the_ticket_back_to_shipped_and_holds_the_lane(self):
        self.fake.add(5, ["loom:verified", "lane:codex"], [proof_comment(3)], state="closed")
        self.run_audit("fail")
        issue = self.fake.issues[5]
        self.assertEqual(issue["state"], "open")
        self.assertEqual(sorted(self.fake.names(5)),
                         ["lane:codex", audit.LABEL, "loom:shipped", "loom:ticket"])
        note = self.fake.comments[5][-1]["body"]
        self.assertEqual(audit.read(note), ("fail", "zen"))
        self.assertIn("the station log for 2030-01-02 has no daily line", note)
        self.assertIn("`zen/opencode/model-free`", note)
        self.assertEqual(audit.held(self.gh, [REPO], ["station-bot"], NOW.timestamp() + 60),
                         {"zen": NOW.timestamp() + audit.HOLD})
        self.assertEqual(audit.held(self.gh, [REPO], ["station-bot"],
                                    NOW.timestamp() + audit.HOLD + 60), {})

    def test_a_proof_that_holds_is_marked_audited_and_left_as_it_is(self):
        self.fake.add(5, ["loom:verified"], [proof_comment(3)])
        self.run_audit("pass", note="re-read the CI run and the daily line at 2030-01-02T01:00Z")
        self.assertEqual(self.fake.names(5), ["loom:ticket", "loom:verified"])
        self.assertEqual([c[0] for c in self.fake.writes()], ["POST"])
        self.assertEqual(audit.read(self.fake.comments[5][-1]["body"]), ("pass", "zen"))
        self.assertEqual(audit.due(self.gh, [REPO], ["station-bot"], NOW.timestamp()), [])

    def test_only_an_unaudited_lane_proof_from_a_trusted_login_is_audited(self):
        cases = {
            "brain proof": [proof_comment(3, verified_by="brain")],
            "untrusted proof": [proof_comment(3, login="someone-else")],
            "already audited": [proof_comment(3), audit_comment(2, "pass")],
            "no proof": [],
        }
        for why, comments in cases.items():
            with self.subTest(why=why):
                self.fake.add(5, ["loom:verified"], comments)
                with self.assertRaisesRegex(ValueError, "lane proof"):
                    self.run_audit("fail")
                self.assertEqual(self.fake.writes(), [])

    def test_an_audit_needs_a_verified_ticket_and_a_note(self):
        self.fake.add(5, ["loom:shipped"], [proof_comment(3)])
        with self.assertRaisesRegex(ValueError, "verified"):
            self.run_audit("fail")
        self.fake.add(5, ["loom:verified"], [proof_comment(3)])
        with self.assertRaisesRegex(ValueError, "note"):
            self.run_audit("fail", note=" ")
        self.assertEqual(self.fake.writes(), [])


class AuditDueTest(unittest.TestCase):
    def setUp(self):
        self.fake = Tracker()
        self.gh = GitHub("t", transport=self.fake)

    def test_the_oldest_unaudited_lane_proof_of_the_last_day_comes_first(self):
        self.fake.add(1, ["loom:verified"], [proof_comment(2)])
        self.fake.add(2, ["loom:verified"], [proof_comment(5)], state="closed")
        self.fake.add(3, ["loom:verified"], [proof_comment(30)])
        self.fake.add(4, ["loom:verified"], [proof_comment(8, verified_by="brain")])
        self.fake.add(6, ["loom:verified"], [proof_comment(9), audit_comment(7, "pass")])
        self.fake.add(7, ["loom:shipped"], [proof_comment(10)])
        due = audit.due(self.gh, [REPO], ["station-bot"], NOW.timestamp())
        self.assertEqual([(repo, issue["number"]) for _, repo, issue, _ in due],
                         [(REPO, 2), (REPO, 1)])

    def test_a_proof_newer_than_a_failed_audit_is_due_again(self):
        self.fake.add(1, ["loom:verified"],
                      [proof_comment(9), audit_comment(7), proof_comment(2, verified_by="zai/glm")])
        due = audit.due(self.gh, [REPO], ["station-bot"], NOW.timestamp())
        self.assertEqual([record["verified_by"] for _, _, _, record in due], ["zai/glm"])

    def test_resume_lists_one_audit_with_the_command_to_record_it(self):
        self.fake.add(1, ["loom:verified"], [proof_comment(2)])
        self.fake.add(2, ["loom:verified"], [proof_comment(5)])
        lines = brain._audit_resume_lines(self.gh, [REPO], NOW, ENV)
        found = [x for x in lines if x.startswith("- resume audit")]
        self.assertEqual(len(found), 1)
        self.assertIn("%s#2 proof by `zen/opencode/model-free` 5h ago" % REPO, found[0])
        self.assertIn("python3 -m bus.brain audit %s#2 --result pass|fail --note" % REPO, found[0])

    def test_resume_says_none_when_no_lane_proof_is_due(self):
        self.assertEqual(brain._audit_resume_lines(self.gh, [REPO], NOW, ENV)[-1], "- none")


class HeldTest(unittest.TestCase):
    def setUp(self):
        self.fake = Tracker()
        self.gh = GitHub("t", transport=self.fake)

    def test_only_a_trusted_failed_audit_of_the_last_day_holds_its_lane(self):
        self.fake.add(1, [audit.LABEL, "loom:shipped"], [
            audit_comment(30, lane="zen"), audit_comment(2, lane="zai"),
            audit_comment(1, "pass", lane="workersai"),
            audit_comment(1, lane="openrouter", login="someone-else")])
        held = audit.held(self.gh, [REPO], ["station-bot"], NOW.timestamp())
        self.assertEqual(held, {"zai": NOW.timestamp() - 2 * 3600 + audit.HOLD})

    def test_a_failed_audit_after_the_proof_unproves_the_ticket(self):
        comments = [proof_comment(9), audit_comment(7)]
        self.assertFalse(audit.proven(comments, ["station-bot"]))
        self.assertTrue(audit.proven(comments + [proof_comment(1)], ["station-bot"]))
        self.assertTrue(audit.proven([proof_comment(9), audit_comment(7, "pass")], ["station-bot"]))


if __name__ == "__main__":
    unittest.main()
