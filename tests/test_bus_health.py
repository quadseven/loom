"""Health findings become ranked tickets (bus/health.py)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
import unittest.mock

from bus import brain, health, station, tickets

REPO = "example-owner/example-repo"
URL = "https://health.example.test/api/health"
NOW = 1_900_000_000.0


def finding(key="bags-full:Ana", title="Ana carries full bags", affected=3, hours=20.0,
            evidence="3 members have 0 free slots; 41 loot rolls were passed", **extra):
    item = {"key": key, "title": title, "area": "bags", "affected": affected,
            "hours": hours, "evidence": evidence, "since": "2030-01-01T00:00:00Z"}
    item.update(extra)
    return item


class Reply:
    def __init__(self, data):
        self.data = data


class FakeGitHub:
    """Open tickets, and every write the intake makes."""

    def __init__(self):
        self.open = []
        self.closed = {}
        self.writes = []

    def paginate(self, path):
        assert path == "/repos/%s/issues?state=open&labels=loom:ticket" % REPO, path
        return list(self.open)

    def rest(self, method, path, data=None):
        if method == "GET":
            return Reply(self.closed.get(int(path.rsplit("/", 1)[1]), {"state": "open"}))
        self.writes.append((method, path, data))
        return Reply({})


class Base(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        self.state = {}
        self.said = []
        self.briefs = []
        self.doc = {"checked_at": "2030-01-02T00:00:00Z", "findings": [finding()]}

    def brief(self, gh, repo, goal, title, body, difficulty=None, priority=None):
        number = 100 + len(self.briefs)
        self.briefs.append({"repo": repo, "goal": goal, "title": title, "body": body,
                            "difficulty": difficulty, "priority": priority})
        self.gh.open.append({"number": number, "body": body, "labels": [{"name": "loom:ticket"}]})
        return "https://github.com/%s/issues/%d" % (repo, number)

    def read(self, url):
        if isinstance(self.doc, Exception):
            raise self.doc
        return self.doc

    def run_once(self, now=NOW):
        intake = health.Intake(self.gh, self.state, now, "2030-01-02T00:00:00Z",
                               self.said.append, read=self.read, brief=self.brief)
        intake.run([health.Source(REPO, 1, URL)])
        return intake

    def open_ticket(self, number, digest, priority="P1"):
        issue = {"number": number, "body": "Priority: %s\n\n## Why\nx\n\n%s"
                 % (priority, health.MARKER % digest), "labels": [{"name": "loom:ticket"}]}
        self.gh.open.append(issue)
        return issue


class Intake(Base):
    def test_a_new_finding_is_briefed_once_with_priority_from_impact(self):
        self.run_once()
        self.assertEqual(len(self.briefs), 1)
        made = self.briefs[0]
        self.assertEqual((made["goal"], made["title"], made["difficulty"], made["priority"]),
                         (1, "Ana carries full bags", "medium", "P1"))
        digest = health._finding(finding()).digest
        self.assertIn(health.MARKER % digest, made["body"])
        self.assertIn("Live: the finding is absent from the health endpoint for 24 hours",
                      made["body"])
        self.assertIn("Evidence: 3 members have 0 free slots", made["body"])
        # The ticket is open on the next pass: nothing is filed again.
        self.run_once(NOW + 900)
        self.assertEqual(len(self.briefs), 1)
        self.assertEqual(self.gh.writes, [])

    def test_the_ticket_body_passes_the_brief_and_plan_checks(self):
        body = health.ticket_body(health._finding(finding()))
        self.assertEqual(brain.plan_problems(body), [])
        for section in brain.SECTIONS + ("## Definition of done",):
            self.assertIn(section, body)
        self.assertEqual(body, body.encode("ascii").decode("ascii"))

    def test_a_band_change_moves_the_priority_line(self):
        digest = health._finding(finding()).digest
        issue = self.open_ticket(100, digest, "P1")
        self.state = {"keys": {REPO + "#1": {digest: {"ticket": 100, "band": "P1",
                                                      "posted": [3.0, finding()["evidence"]],
                                                      "commented": NOW}}}}
        self.doc["findings"] = [finding(hours=80.0)]  # 3 x 80 = 240: P0
        self.run_once(NOW + 900)
        patch = [w for w in self.gh.writes if w[0] == "PATCH"]
        self.assertEqual(len(patch), 1)
        self.assertTrue(patch[0][2]["body"].startswith("Priority: P0\n"))
        self.assertEqual(tickets.priority_of(issue), "P0")
        self.assertEqual(self.briefs, [])

    def test_a_brain_set_priority_stands_while_the_band_holds(self):
        digest = health._finding(finding()).digest
        self.open_ticket(100, digest, "P0")
        self.state = {"keys": {REPO + "#1": {digest: {"ticket": 100, "band": "P1",
                                                      "posted": [3.0, finding()["evidence"]],
                                                      "commented": NOW}}}}
        self.run_once(NOW + 900)
        self.assertEqual(self.gh.writes, [])

    def test_evidence_is_posted_at_most_every_six_hours_and_only_when_it_changed(self):
        digest = health._finding(finding()).digest
        self.open_ticket(100, digest)
        self.state = {"keys": {REPO + "#1": {digest: {"ticket": 100, "band": "P1",
                                                      "posted": [3.0, finding()["evidence"]],
                                                      "commented": NOW}}}}
        self.doc["findings"] = [finding(affected=4)]
        self.run_once(NOW + 3600)
        self.assertEqual(self.gh.writes, [])
        self.run_once(NOW + health.EVIDENCE_EVERY)
        self.assertEqual(len(self.gh.writes), 1)
        method, path, data = self.gh.writes[0]
        self.assertEqual((method, path), ("POST", "/repos/%s/issues/100/comments" % REPO))
        self.assertIn("affected 4", data["body"])
        self.run_once(NOW + 3 * health.EVIDENCE_EVERY)
        self.assertEqual(len(self.gh.writes), 1)

    def test_a_cleared_finding_gets_one_absent_note_and_is_never_closed(self):
        digest = health._finding(finding()).digest
        self.open_ticket(100, digest)
        self.doc["findings"] = []
        self.run_once()
        self.run_once(NOW + 900)
        self.assertEqual(len(self.gh.writes), 1)
        method, path, data = self.gh.writes[0]
        self.assertEqual(method, "POST")
        self.assertIn("finding absent since 2030-01-02T00:00:00Z", data["body"])
        self.assertFalse(any(w[0] == "PATCH" for w in self.gh.writes))

    def test_not_looked_files_nothing_and_logs_the_reason_once(self):
        digest = health._finding(finding()).digest
        self.open_ticket(100, digest)
        self.doc = health.NotLooked("the answer is not JSON")
        self.run_once()
        self.run_once(NOW + 900)
        self.assertEqual((self.briefs, self.gh.writes), ([], []))
        self.assertEqual(self.said, ["health %s#1 not looked: the answer is not JSON" % REPO])
        self.assertEqual(self.state["sources"][REPO + "#1"]["error"], "the answer is not JSON")

    def test_a_document_that_breaks_the_contract_is_not_looked(self):
        bad = [[], {"findings": "x"}, {"findings": [finding(affected="3")]},
               {"findings": [finding(), finding()]},
               {"findings": [finding(title="x" * 91)]},
               {"findings": [finding(evidence="see https://example.com/x")]},
               {"findings": [finding(evidence="host 10.0.0.12 is down")]},
               {"findings": [finding(evidence="the box at srv-1.example.lan")]},
               {"findings": [finding(title="caf\u00e9 is closed")]},
               {"findings": [finding(evidence="token=abc123")]}]
        for doc in bad:
            with self.subTest(doc=json.dumps(doc)[:80]):
                with self.assertRaises(health.NotLooked):
                    health.findings(doc)

    def test_three_new_tickets_a_pass_at_most(self):
        self.doc["findings"] = [finding(key="k%d" % i, title="Problem %d" % i) for i in range(5)]
        self.run_once()
        self.assertEqual(len(self.briefs), health.PER_PASS)
        self.run_once(NOW + 900)
        self.assertEqual(len(self.briefs), 5)

    def test_a_refused_brief_is_logged_once_and_tried_again(self):
        def full(*args, **kwargs):
            raise ValueError("goal already has 40 tickets in flight (cap 40)")
        self.brief = full
        self.run_once()
        self.run_once(NOW + 900)
        self.assertEqual(len([s for s in self.said if "could not brief" in s]), 1)

    def test_a_finding_whose_ticket_was_closed_not_planned_is_not_filed_again(self):
        digest = health._finding(finding()).digest
        self.state = {"keys": {REPO + "#1": {digest: {"ticket": 100, "band": "P1"}}}}
        self.gh.closed[100] = {"state": "closed", "state_reason": "not_planned"}
        self.run_once()
        self.assertEqual(self.briefs, [])
        # Closed as completed but still reported: the fix did not hold.
        self.state = {"keys": {REPO + "#1": {digest: {"ticket": 100, "band": "P1"}}}}
        self.gh.closed[100] = {"state": "closed", "state_reason": "completed"}
        self.run_once()
        self.assertEqual(len(self.briefs), 1)


class Sources(unittest.TestCase):
    def test_parse_sources_keeps_https_entries_and_names_the_rest(self):
        found, problems = health.parse_sources(
            "%s#4=%s other-owner/other-repo#2=http://plain.example.test/h bad=https://x.test"
            % (REPO, URL))
        self.assertEqual(found, [health.Source(REPO, 4, URL)])
        self.assertEqual(len(problems), 2)
        self.assertIn("not an https URL", problems[0])
        self.assertNotIn("plain.example.test", " ".join(problems))

    def test_fetch_refuses_plain_http_without_a_request(self):
        with unittest.mock.patch.object(health._OPENER, "open") as opened:
            with self.assertRaisesRegex(health.NotLooked, "not https"):
                health.fetch("http://health.example.test/h")
        opened.assert_not_called()

    def test_fetch_refuses_an_answer_over_the_size_cap(self):
        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, n):
                return b"x" * n
        with unittest.mock.patch.object(health._OPENER, "open", return_value=Resp()):
            with self.assertRaisesRegex(health.NotLooked, "over"):
                health.fetch(URL)

    def test_status_lines_read_the_ledger(self):
        state = {"sources": {REPO + "#1": {"checked_at": "2030-01-02T00:00:00Z",
                                           "findings": [{"title": "T", "area": "a",
                                                         "impact": 12.5, "priority": "P2"}]}}}
        self.assertEqual(health.status_lines(state, ""), [
            "## Health findings", "- %s#1: 1 finding (checked 2030-01-02T00:00:00Z)" % REPO,
            "  - P2 T (a, impact 12.5)"])
        self.assertEqual(health.status_lines(None, "%s#3=%s" % (REPO, URL))[1:],
                         ["- %s#3: not looked yet" % REPO])
        self.assertIn("none", health.status_lines(None, "")[1])


class StationStep(unittest.TestCase):
    def test_settings_read_the_health_urls_and_interval(self):
        s = station.Settings.from_env({"LOOM_REPOS": REPO, "LOOM_HEALTH_URLS": "%s#1=%s" % (REPO, URL),
                                       "LOOM_HEALTH_EVERY": "1800"})
        self.assertEqual((s.health_urls, s.health_every), ("%s#1=%s" % (REPO, URL), 1800))
        s = station.Settings.from_env({"LOOM_REPOS": REPO, "LOOM_HEALTH_EVERY": "5"})
        self.assertEqual((s.health_urls, s.health_every), ("", health.EVERY))

    def test_the_step_reads_at_most_once_per_interval_and_keeps_the_ledger(self):
        with tempfile.TemporaryDirectory() as home:
            settings = station.Settings(home, [REPO], ["alice-example"], [],
                                        status_repo=REPO, health_urls="%s#1=%s" % (REPO, URL))
            st = station.Station(FakeGitHub(), settings)
            calls = []

            def run(intake, sources):
                calls.append(sources)
                intake.state["last"] = intake.now
            with unittest.mock.patch.object(health.Intake, "run", run):
                st.now = NOW
                st.health_intake()
                st.now = NOW + 600
                st.health_intake()
                st.now = NOW + health.EVERY
                st.health_intake()
            self.assertEqual(calls, [[health.Source(REPO, 1, URL)]] * 2)
            with open(os.path.join(home, "state", "station", "health.json")) as fh:
                self.assertEqual(json.load(fh)["last"], NOW + health.EVERY)

    def test_the_step_is_off_without_urls(self):
        with tempfile.TemporaryDirectory() as home:
            st = station.Station(FakeGitHub(), station.Settings(home, [REPO], [], [],
                                                                 status_repo=REPO))
            with unittest.mock.patch.object(health.Intake, "run") as run:
                st.health_intake()
            run.assert_not_called()
            self.assertFalse(os.path.exists(health.ledger_path(home)))


if __name__ == "__main__":
    unittest.main()
