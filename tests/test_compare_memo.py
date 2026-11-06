"""Compare reads are asked once per (repo, commit, revision) and audit
listings reuse one URL an hour, so the ETag cache can answer them (loom#2357)."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import audit, proof, station  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

REPO = "example-owner/example-repo"
COMMIT = "a" * 40
REVISION = "b" * 40


class Reply:
    def __init__(self, status, data):
        self.status = status
        self.data = data


class CompareGitHub:
    """Answers compare reads with one status per path and records each ask."""

    def __init__(self, answers=None):
        self.answers = dict(answers or {})
        self.asked = []

    def rest(self, method, path, data=None, ok=()):
        self.asked.append(path)
        status, body = self.answers.get(path, (200, {"status": "ahead"}))
        return Reply(status, body)


def compare(commit=COMMIT, revision=REVISION):
    return "/repos/%s/compare/%s...%s" % (REPO, commit, revision)


class StationCarriesMemoTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.settings = station.Settings(os.path.join(tmp.name, "loom"), [REPO], ["bot"],
                                         ["owner"], disk_free_min_gb=0)

    def runner(self, gh):
        return station.Station(gh, self.settings)

    def test_a_repeated_triple_makes_one_request(self):
        gh = CompareGitHub()
        runner = self.runner(gh)
        self.assertTrue(runner._carries(REPO, COMMIT, REVISION))
        self.assertTrue(runner._carries(REPO, COMMIT, REVISION))
        self.assertEqual(gh.asked, [compare()])

    def test_the_next_pass_reads_the_saved_answer(self):
        self.runner(CompareGitHub())._carries(REPO, COMMIT, REVISION)
        gh = CompareGitHub()
        self.assertTrue(self.runner(gh)._carries(REPO, COMMIT, REVISION))
        self.assertEqual(gh.asked, [])

    def test_a_commit_the_revision_does_not_carry_is_remembered_too(self):
        gh = CompareGitHub({compare(): (200, {"status": "diverged"})})
        runner = self.runner(gh)
        self.assertFalse(runner._carries(REPO, COMMIT, REVISION))
        self.assertFalse(runner._carries(REPO, COMMIT, REVISION))
        self.assertEqual(len(gh.asked), 1)

    def test_a_404_is_asked_again(self):
        gh = CompareGitHub({compare(): (404, {"message": "Not Found"})})
        runner = self.runner(gh)
        self.assertFalse(runner._carries(REPO, COMMIT, REVISION))
        gh.answers.clear()
        self.assertTrue(runner._carries(REPO, COMMIT, REVISION))
        self.assertEqual(len(gh.asked), 2)

    def test_an_answer_with_no_status_is_asked_again(self):
        gh = CompareGitHub({compare(): (200, {})})
        runner = self.runner(gh)
        self.assertFalse(runner._carries(REPO, COMMIT, REVISION))
        self.assertFalse(runner._carries(REPO, COMMIT, REVISION))
        self.assertEqual(len(gh.asked), 2)

    def test_a_branch_name_is_asked_every_time(self):
        gh = CompareGitHub()
        runner = self.runner(gh)
        runner._carries(REPO, COMMIT, "main")
        runner._carries(REPO, COMMIT, "main")
        self.assertEqual(len(gh.asked), 2)

    def test_an_exhausted_cap_stops_before_any_read(self):
        gh = CompareGitHub()
        pin = {"number": 4, "merged_at": "2030-01-01T00:00:00Z"}
        self.assertFalse(self.runner(gh)._ship_published(REPO, "abc1234", REPO, pin, [0]))
        self.assertEqual(gh.asked, [])


def stamp(at):
    return datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ListingGitHub:
    """Verified tickets that honor since=, and a lane proof comment each."""

    def __init__(self, tickets):
        self.tickets = tickets  # number -> (updated, proof posted)
        self.listed = []

    def paginate(self, path):
        parts = urlsplit(path)
        if parts.path.endswith("/comments"):
            number = int(parts.path.split("/")[-2])
            return [self.proof(self.tickets[number][1])]
        self.listed.append(path)
        since = parse_qs(parts.query)["since"][0]
        return [{"number": n, "labels": [{"name": "loom:ticket"}, {"name": "loom:verified"}]}
                for n, (updated, _) in sorted(self.tickets.items()) if stamp(updated) >= since]

    @staticmethod
    def proof(posted):
        record = {"kind": "docs", "revision": "abc1234", "verified_by": "zen/opencode/model",
                  "observed": {"start": "2030-01-01T00:00:00Z", "end": "2030-01-01T01:00:00Z"},
                  "sources": ["https://example.com/logs/1"],
                  "criteria": [{"criterion": "A test passes", "result": "pass", "evidence": "ci"}]}
        body = proof.render(record, "## Acceptance criteria\n- [x] A test passes\n")
        return {"body": body, "created_at": stamp(posted), "user": {"login": "bot"}}


class AuditSinceTest(unittest.TestCase):
    NOW = datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc).timestamp()

    def test_two_calls_in_one_hour_build_the_same_url(self):
        gh = ListingGitHub({})
        audit._labelled(gh, REPO, "loom:verified", self.NOW + 60)
        audit._labelled(gh, REPO, "loom:verified", self.NOW + 59 * 60)
        self.assertEqual(len(set(gh.listed)), 1, gh.listed)
        self.assertIn("since=2030-01-02T12:00:00Z", gh.listed[0])

    def test_due_returns_the_same_tickets_as_an_exact_since(self):
        now = self.NOW + 40 * 60
        gh = ListingGitHub({
            1: (now - 3600, now - 3600),
            # Updated inside the widened hour, proof older than a day: not due.
            2: (now - audit.WINDOW - 20 * 60, now - audit.WINDOW - 20 * 60),
            3: (now - 5 * 3600, now - 5 * 3600)})
        due = audit.due(gh, [REPO], ["bot"], now)
        self.assertEqual([issue["number"] for _, _, issue, _ in due], [3, 1])
        self.assertEqual(len(audit._labelled(gh, REPO, "loom:verified", now - audit.WINDOW)), 3)


if __name__ == "__main__":
    unittest.main()
