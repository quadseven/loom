from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain, journal, metrics, proof, station  # noqa: E402
from bus.github import GitHub, GitHubError, Response  # noqa: E402

REPO = "example-owner/example-repo"
DEFAULT_BRANCH = "main"
AUTHOR = "test-author"
SHA = "0123456789abcdef0123456789abcdef01234567"
T0 = 1_800_000_000.0


def stamp(at: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))


def iso_time(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeGitHub:
    def __init__(self):
        self.issues = {}
        self.comments = {}
        self.labels = {}
        self.events = {}
        self.calls = []
        self.requests = 0

    def paginate(self, path: str):
        self.calls.append(("GET", path))
        m = re.match(r"/repos/([^/]+/[^/]+)/issues/(\d+)/comments", path)
        if m:
            return list(self.comments.get((m.group(1), int(m.group(2))), []))
        m = re.match(r"/repos/([^/]+/[^/]+)/issues/(\d+)/events", path)
        if m:
            return list(self.events.get((m.group(1), int(m.group(2))), []))
        m = re.match(r"/repos/([^/]+/[^/]+)/issues", path)
        if m:
            repo = m.group(1)
            found = []
            for (r, n), issue in self.issues.items():
                if r != repo:
                    continue
                issue_labels = [label["name"] for label in issue.get("labels", [])]
                if "labels=loom:verified" in path and "loom:verified" not in issue_labels:
                    continue
                if "labels=loom%3Aticket" in path and "loom:ticket" not in issue_labels:
                    continue
                found.append(dict(issue))
            return found
        return []

    def rest(self, method: str, path: str, body: dict = None, ok: tuple = ()):
        self.calls.append((method, path, body))
        self.requests += 1
        m_issue = re.fullmatch(r"/repos/([^/]+/[^/]+)/issues/(\d+)", path)
        if m_issue:
            key = (m_issue.group(1), int(m_issue.group(2)))
            if method == "GET":
                if key not in self.issues:
                    raise GitHubError("Not Found", 404)
                return Response(200, dict(self.issues[key]))
            if method == "PATCH":
                if key not in self.issues:
                    raise GitHubError("Not Found", 404)
                self.issues[key].update(body)
                return Response(200, dict(self.issues[key]))

        m_comments = re.fullmatch(r"/repos/([^/]+/[^/]+)/issues/(\d+)/comments", path)
        if m_comments and method == "POST":
            key = (m_comments.group(1), int(m_comments.group(2)))
            comment = {
                "id": len(self.comments.get(key, [])) + 1,
                "body": body["body"],
                "created_at": stamp(T0 + len(self.comments.get(key, [])) * 60),
                "user": {"login": AUTHOR},
            }
            self.comments.setdefault(key, []).append(comment)
            return Response(201, comment)

        m_labels = re.fullmatch(r"/repos/([^/]+/[^/]+)/issues/(\d+)/labels", path)
        if m_labels and method == "PUT":
            key = (m_labels.group(1), int(m_labels.group(2)))
            new_labels = [{"name": name} for name in body.get("labels", [])]
            if key in self.issues:
                self.issues[key]["labels"] = new_labels
            return Response(200, new_labels)

        m_repo = re.fullmatch(r"/repos/([^/]+/[^/]+)", path)
        if m_repo and method == "GET":
            return Response(200, {"default_branch": DEFAULT_BRANCH, "private": False})

        m_cmp = re.match(r"/repos/([^/]+/[^/]+)/compare/(.*)", path)
        if m_cmp and method == "GET":
            return Response(200, {"status": "identical"})

        return Response(200, {})


def sample_brief_body() -> str:
    return """## Why
Need proof verification.

## What
Test proof verification.

## Acceptance criteria
- [ ] Criterion one: live endpoint returns 200
- [ ] Criterion two: log shows success

## Size
S

## Out of scope
Everything else.
"""


def sample_proof_record(verified_by: str = None) -> dict:
    rec = {
        "kind": "docs",
        "revision": SHA,
        "observed": {"start": "2030-01-01T00:00:00Z", "end": "2030-01-01T01:00:00Z"},
        "sources": ["https://example.com/status"],
        "criteria": [
            {
                "criterion": "Criterion one: live endpoint returns 200",
                "result": "pass",
                "evidence": "https://example.com/status",
            },
            {
                "criterion": "Criterion two: log shows success",
                "result": "pass",
                "evidence": "/path/to/logfile.log",
            },
        ],
    }
    if verified_by is not None:
        rec["verified_by"] = verified_by
    return rec


class ProofAuditTests(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        self.issue_body = sample_brief_body()
        self.issue_number = 460
        self.key = (REPO, self.issue_number)
        self.gh.issues[self.key] = {
            "number": self.issue_number,
            "title": "A lane-made proof says which lane made it",
            "body": self.issue_body,
            "state": "open",
            "labels": [{"name": "loom:ticket"}, {"name": "loom:shipped"}],
            "created_at": stamp(T0),
            "updated_at": stamp(T0),
            "user": {"login": AUTHOR},
        }

    def test_proof_applied_from_verify_run_carries_lane_and_model(self):
        # A proof applied from a verify run carries verified_by with lane and model
        record = sample_proof_record()
        # Verify run on lane "agy" with model "gemini-2.5-pro"
        record["verified_by"] = "agy/gemini-2.5-pro"
        brain.move(self.gh, REPO, self.issue_number, "verified", record=record)

        comments = self.gh.comments.get(self.key, [])
        self.assertTrue(len(comments) >= 1)
        proof_comment = comments[-1]["body"]

        self.assertIn("Verified by: agy/gemini-2.5-pro", proof_comment)
        parsed = proof.read(proof_comment)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.get("verified_by"), "agy/gemini-2.5-pro")

    def test_brain_proof_carries_brain(self):
        # A proof made by the brain carries verified_by: "brain"
        record = sample_proof_record()  # No verified_by provided
        brain.move(self.gh, REPO, self.issue_number, "verified", record=record)

        comments = self.gh.comments.get(self.key, [])
        self.assertTrue(len(comments) >= 1)
        proof_comment = comments[-1]["body"]

        self.assertIn("Verified by: brain", proof_comment)
        parsed = proof.read(proof_comment)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.get("verified_by"), "brain")

    def test_audit_missing_evidence_moves_to_shipped_and_holds_lane(self):
        # Apply a lane-made proof
        record = sample_proof_record(verified_by="agy/gemini-2.5-pro")
        brain.move(self.gh, REPO, self.issue_number, "verified", record=record)

        # Confirm issue has loom:verified
        labels = [l["name"] for l in self.gh.issues[self.key]["labels"]]
        self.assertIn("loom:verified", labels)

        # Audit with missing evidence
        with tempfile.TemporaryDirectory() as temp_home:
            now = datetime.fromtimestamp(T0 + 3600, timezone.utc)

            # check_fn returns False to simulate missing evidence
            def check_fn(evidence: str):
                return False, "evidence missing or endpoint down"

            res = brain.audit_one_proof(
                self.gh, [REPO], check_fn=check_fn, now=now, home=temp_home
            )
            self.assertIsNotNone(res)
            self.assertEqual(res["result"], "failed")
            self.assertEqual(res["lane"], "agy")

            # Ticket is moved back to shipped
            current_labels = [l["name"] for l in self.gh.issues[self.key]["labels"]]
            self.assertIn("loom:shipped", current_labels)
            self.assertNotIn("loom:verified", current_labels)

            # Note naming what failed was posted
            comments = self.gh.comments.get(self.key, [])
            last_comment = comments[-1]["body"]
            self.assertIn("Audit failed", last_comment)
            self.assertIn("evidence missing or endpoint down", last_comment)

            # Lane verify runs are held for 24 hours
            self.assertTrue(station.verify_lane_held(temp_home, "agy", now.timestamp()))
            # After 24h, the hold expires
            future = now.timestamp() + 24 * 3600 + 1
            self.assertFalse(station.verify_lane_held(temp_home, "agy", future))

    def test_audit_valid_evidence_passes_and_marks_audited(self):
        record = sample_proof_record(verified_by="zen/opencode-glm")
        brain.move(self.gh, REPO, self.issue_number, "verified", record=record)

        with tempfile.TemporaryDirectory() as temp_home:
            now = datetime.fromtimestamp(T0 + 3600, timezone.utc)

            def check_fn(evidence: str):
                return True, ""

            res = brain.audit_one_proof(
                self.gh, [REPO], check_fn=check_fn, now=now, home=temp_home
            )
            self.assertIsNotNone(res)
            self.assertEqual(res["result"], "pass")
            self.assertEqual(res["lane"], "zen")

            # Ticket stays verified
            current_labels = [l["name"] for l in self.gh.issues[self.key]["labels"]]
            self.assertIn("loom:verified", current_labels)

            # Audit marker comment posted
            comments = self.gh.comments.get(self.key, [])
            last_comment = comments[-1]["body"]
            self.assertIn(brain.AUDIT_MARKER, last_comment)
            self.assertIn("Audit passed", last_comment)

            # Second audit call finds no unaudited proofs
            res2 = brain.audit_one_proof(
                self.gh, [REPO], check_fn=check_fn, now=now, home=temp_home
            )
            self.assertIsNone(res2)

    def test_audit_oldest_first(self):
        # Create second ticket
        key2 = (REPO, 461)
        self.gh.issues[key2] = {
            "number": 461,
            "title": "Second ticket",
            "body": sample_brief_body(),
            "state": "open",
            "labels": [{"name": "loom:ticket"}, {"name": "loom:shipped"}],
            "created_at": stamp(T0 + 100),
            "updated_at": stamp(T0 + 100),
            "user": {"login": AUTHOR},
        }

        # Apply proof to 460 at T0 + 100
        rec1 = sample_proof_record(verified_by="lane-old/model-1")
        brain.move(self.gh, REPO, self.issue_number, "verified", record=rec1)

        # Apply proof to 461 at T0 + 500
        rec2 = sample_proof_record(verified_by="lane-new/model-2")
        brain.move(self.gh, REPO, 461, "verified", record=rec2)

        # Set comment timestamps so 460 is older than 461
        self.gh.comments[self.key][-1]["created_at"] = stamp(T0 + 100)
        self.gh.comments[key2][-1]["created_at"] = stamp(T0 + 500)

        with tempfile.TemporaryDirectory() as temp_home:
            now = datetime.fromtimestamp(T0 + 1000, timezone.utc)

            def check_fn(evidence: str):
                return True, ""

            # First audit picks the oldest (460)
            res1 = brain.audit_one_proof(
                self.gh, [REPO], check_fn=check_fn, now=now, home=temp_home
            )
            self.assertIsNotNone(res1)
            self.assertEqual(res1["number"], self.issue_number)
            self.assertEqual(res1["lane"], "lane-old")

            # Second audit picks the next oldest (461)
            res2 = brain.audit_one_proof(
                self.gh, [REPO], check_fn=check_fn, now=now, home=temp_home
            )
            self.assertIsNotNone(res2)
            self.assertEqual(res2["number"], 461)
            self.assertEqual(res2["lane"], "lane-new")

    def test_throughput_metric_splits_lane_and_brain(self):
        total = metrics.Totals()
        total.merged_by_repo[REPO] = 10
        total.merged_tickets = 10
        total.verified = 6
        total.verified_lane = 4
        total.verified_brain = 2
        total.goals = 1
        total.goal_repos = {REPO}

        num = metrics.throughput_number(total)
        self.assertEqual(num.name, "Throughput")
        self.assertIn("verified 6/10 (4 lane, 2 brain)", num.value)
        self.assertTrue(num.passed)

    def test_journal_renders_audited_line(self):
        text = journal.render_journal(
            "2030-01-01T12:00:00Z",
            "muse",
            shipped=["repo#1"],
            briefed=["repo#2"],
            waiting=["none"],
            asks=["none"],
            requests=50,
            minutes=5,
            audited="quadseven/loom#460 pass",
        )
        lines = text.splitlines()
        self.assertIn("- Audited: quadseven/loom#460 pass", lines)
        self.assertIn("- Cost: 50 requests, 5 minutes", lines)


if __name__ == "__main__":
    unittest.main()
