"""The narrow evidence close (loom#2325), driven through the public close
seam, Station.close_done_tickets.

Each refusal test runs a qualifying twin in the same pass, so it fails on a
station without this close: the twin stays open there.
"""
from __future__ import annotations

import base64
import io
import json
import os
import sys
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import shadow, station, status  # noqa: E402
from bus.github import GitHub, ShadowHeld  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

import test_bus_station as tbs  # noqa: E402

REPO = tbs.REPO
LABEL = "closed-unproven"
BODY = ("Difficulty: easy\n\n## Why\nBecause.\n\n## Acceptance criteria\n"
        "- [x] A test passes\n"
        "- [ ] Live: the status page shows the new count\n")
PATCH = "@@ -1 +1,3 @@\n+def made_here():\n+    return 1\n old = 1\n"
SOURCE = "def made_here():\n    return 1\n"


class Case(tbs.Base):
    def setUp(self):
        super().setUp()
        self.fake.private = False
        # Issues the station files take numbers above every ticket here.
        self.fake.issues[500] = {"number": 500, "state": "closed", "labels": []}

    def ticket(self, number, body=BODY, merged=True, conclusion="success", files=None,
               labels=(), on_main=True):
        """A shipped ticket with its merged pull request, checks and files."""
        self.fake.add(number, body=body, state="shipped")
        for name in labels:
            self.fake.issues[number]["labels"].append({"name": name})
        if not merged:
            return
        sha = "%040x" % (1000 + number)
        pull = 100 + number
        self.fake.pulls.append({
            "number": pull, "state": "closed", "merged_at": "2030-01-02T00:00:00Z",
            "merge_commit_sha": sha, "body": "Refs %s#%d" % (REPO, number),
            "head": {"ref": "loom/%d-fix" % number}})
        self.fake.check_runs[sha] = [{
            "name": "tests", "status": "completed" if conclusion else "in_progress",
            "conclusion": conclusion, "completed_at": "2030-01-02T00:01:00Z"}]
        if on_main:
            self.fake.carried.add(sha)
        self.fake.pull_files[pull] = files if files is not None else [
            {"filename": "bus/shared.py", "status": "modified", "patch": PATCH}]
        self.fake.head_files.setdefault("bus/shared.py", SOURCE)

    def close_pass(self, now=None):
        self.fake.version += 1
        st = station.Station(GitHub("token", transport=self.fake), self.settings,
                             now=now if now is not None else time.time())
        with mock.patch.object(station, "_say") as said:
            st.close_done_tickets()
        return [" ".join(str(a) for a in call.args) for call in said.call_args_list]

    def assert_twin(self, broken=2):
        """Ticket 1 qualifies and closes; `broken` stays open."""
        self.close_pass()
        self.assertEqual(self.fake.issues[1]["state"], "closed")
        self.assertIn(LABEL, self.fake.names(1))
        self.assertEqual(self.fake.issues[broken]["state"], "open")
        self.assertNotIn(LABEL, self.fake.names(broken))

    def proof_issues(self):
        return [i for i in self.fake.issues.values()
                if {"name": "loom:proof-due"} in i["labels"]]


class Closes(Case):
    def test_a_qualifying_ticket_closes_with_the_label_and_the_guard_hatch(self):
        self.ticket(1)
        self.close_pass()
        issue = self.fake.issues[1]
        self.assertEqual((issue["state"], issue["state_reason"]), ("closed", "completed"))
        self.assertIn(LABEL, self.fake.names(1))
        self.assertIn("force-close", self.fake.names(1))
        self.assertNotIn("needs:human", self.fake.names(1))
        said = [c["body"] for c in self.fake.comments[1] if status.is_status(c.get("body"))]
        self.assertIn("unproven", " ".join(said))

    def test_a_neutral_review_check_is_green(self):
        self.ticket(1)
        self.fake.check_runs["%040x" % 1001].append({
            "name": "review", "status": "completed", "conclusion": "neutral",
            "completed_at": "2030-01-02T00:01:00Z"})
        self.close_pass()
        self.assertEqual(self.fake.issues[1]["state"], "closed")

    def test_live_boxes_land_in_one_rolling_issue_per_repo(self):
        self.ticket(1)
        self.ticket(2, body=BODY.replace("new count", "second count"))
        self.close_pass()
        self.ticket(3, body=BODY.replace("new count", "third count"))
        self.close_pass()
        [issue] = self.proof_issues()
        self.assertTrue(issue["title"].startswith("Proof due"))
        for number, words in ((1, "new count"), (2, "second count"), (3, "third count")):
            self.assertIn("#%d" % number, issue["body"])
            self.assertIn(words, issue["body"])

    def test_no_close_when_the_list_cannot_be_written(self):
        self.ticket(1)
        with mock.patch.object(station.Station, "_proof_due_issue",
                               side_effect=station.GitHubError("refused")):
            self.close_pass()
        self.assertEqual(self.fake.issues[1]["state"], "open")


class Refuses(Case):
    def test_no_merged_pull_request(self):
        self.ticket(1)
        self.ticket(2, merged=False)
        self.assert_twin()

    def test_red_ci_on_the_merge_commit(self):
        self.ticket(1)
        self.ticket(2, conclusion="failure")
        self.assert_twin()

    def test_no_ci_on_the_merge_commit(self):
        self.ticket(1)
        self.ticket(2)
        self.fake.check_runs["%040x" % 1002] = []
        self.assert_twin()

    def test_ci_still_running_waits_with_no_ask(self):
        no_string = BODY  # a Live line with nothing to search asks the operator
        self.ticket(1)
        self.ticket(2, body=no_string, conclusion=None)
        self.assert_twin()
        self.assertNotIn("needs:human", self.fake.names(2))

    def test_merge_commit_not_on_main(self):
        self.ticket(1)
        self.ticket(2, on_main=False)
        self.assert_twin()

    def test_an_added_symbol_gone_from_main(self):
        self.ticket(1)
        self.ticket(2, files=[{"filename": "bus/gone.py", "status": "modified", "patch": PATCH}])
        self.fake.head_files["bus/gone.py"] = "def replaced():\n    return 2\n"
        self.assert_twin()

    def test_a_removed_symbol_back_on_main(self):
        # A later pull request restored code this one removed.
        self.ticket(1)
        self.ticket(2, files=[{"filename": "bus/back.py", "status": "modified",
                               "patch": "@@ -1,2 +1 @@\n-def dropped():\n-    return 1\n"}])
        self.fake.head_files["bus/back.py"] = "def dropped():\n    return 1\n"
        self.assert_twin()

    def test_a_changed_file_with_no_patch_proves_nothing(self):
        self.ticket(1)
        self.ticket(2, files=[{"filename": "bus/huge.py", "status": "modified"}])
        self.fake.head_files["bus/huge.py"] = SOURCE
        self.assert_twin()

    def test_an_unticked_code_box(self):
        self.ticket(1)
        self.ticket(2, body=BODY.replace("- [x] A test", "- [ ] A test"))
        self.assert_twin()

    def test_an_unticked_operator_box(self):
        self.ticket(1)
        self.ticket(2, body=BODY + "- [ ] The operator signs off\n")
        self.assert_twin()

    def test_a_ticket_with_no_live_box(self):
        self.ticket(1)
        self.ticket(2, body=BODY.replace("- [ ] Live: the status page shows the new count\n", ""),
                    labels=("loom:urgent",))
        self.assert_twin()

    def test_a_regression_label(self):
        self.ticket(1)
        self.ticket(2, labels=("loom:regression",))
        self.assert_twin()

    def test_tier_deploy_config_and_above(self):
        self.ticket(1)
        self.ticket(2, files=[{"filename": "deploy/web.yaml", "status": "modified",
                               "patch": "@@ -1 +1 @@\n-a: 1\n+a: 2\n"}])
        self.ticket(3, files=[{"filename": ".github/workflows/ci.yml", "status": "modified",
                               "patch": "@@ -1 +1 @@\n-a: 1\n+a: 2\n"}])
        self.assert_twin()
        self.assertEqual(self.fake.issues[3]["state"], "open")

    def test_an_unmeetable_live_box(self):
        self.ticket(1)
        self.ticket(2, body=BODY.replace("shows the new count", "prints `never printed`"))
        self.fake.code_absent.add("never printed")
        self.assert_twin()

    def test_a_live_box_whose_lookup_failed(self):
        # A failed lookup proves nothing, so the box is not known meetable.
        self.ticket(1)
        self.ticket(2, body=BODY.replace("shows the new count", "prints `maybe printed`"))
        self.fake.code_absent.add("maybe printed")
        with mock.patch.object(station.Station, "_code_search_hits", return_value=None):
            self.close_pass()
        self.assertEqual(self.fake.issues[2]["state"], "open")
        self.assertEqual(self.fake.issues[1]["state"], "closed")

    def test_an_open_ask(self):
        self.ticket(1)
        self.ticket(2, labels=("needs:human",))
        self.assert_twin()

    def test_a_waiting_ticket(self):
        self.ticket(1)
        self.ticket(2, labels=("wait:live-event",))
        self.assert_twin()

    def test_a_box_loom_can_check_itself(self):
        self.ticket(1)
        self.ticket(2, body=BODY.replace("the status page shows the new count",
                                         "`https://status.example.com/healthz` serves `ok`"))
        with mock.patch("bus.criteria_gate._fetch", return_value=(500, "down", "")):
            self.assert_twin()

    def test_an_open_time_window(self):
        self.ticket(1)
        self.ticket(2, body=BODY.replace(
            "shows the new count", "shows the count on or after 2026-01-01 and before 2099-12-31"))
        self.assert_twin()

    def test_a_ticket_already_reopened_from_this_close(self):
        self.ticket(1)
        self.ticket(2, labels=(LABEL,))
        self.fake.issues[2]["labels"].append({"name": "force-close"})
        self.close_pass()
        self.assertEqual(self.fake.issues[1]["state"], "closed")
        self.assertEqual(self.fake.issues[2]["state"], "open")


class Shadow(Case):
    """This close alone passes through shadow."""

    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(os.environ, {"LOOM_SHADOW": "1", "LOOM_SHADOW_REPOS": REPO})
        patch.start()
        self.addCleanup(patch.stop)
        self.assertTrue(shadow.active(REPO))

    def ledger(self):
        return shadow.read(shadow.ledger_path())

    def test_the_close_passes_and_is_recorded_as_acted(self):
        self.ticket(1)
        self.close_pass()
        self.assertEqual(self.fake.issues[1]["state"], "closed")
        self.assertIn(LABEL, self.fake.names(1))
        acted = [e for e in shadow.read_acted(50) if (e["action"], e["number"]) == ("close", 1)]
        self.assertTrue(acted)

    def test_a_plain_done_close_stays_held(self):
        self.ticket(1)
        self.ticket(2, body="Difficulty: easy\n\n## Acceptance criteria\n- [ ] A test passes\n")
        self.close_pass()
        self.assertEqual(self.fake.issues[1]["state"], "closed")
        self.assertEqual(self.fake.issues[2]["state"], "open")

    def test_the_label_by_hand_lets_nothing_through(self):
        self.ticket(1, labels=(LABEL, "force-close"))
        gh = GitHub("token", transport=self.fake)
        with self.assertRaises(ShadowHeld):
            gh.rest("PATCH", "/repos/%s/issues/1" % REPO,
                    {"state": "closed", "state_reason": "completed"})
        self.assertEqual(self.fake.issues[1]["state"], "open")

    def test_the_pass_names_one_ticket_and_only_a_close(self):
        self.ticket(1)
        self.ticket(2)
        gh = GitHub("token", transport=self.fake)
        with shadow.evidence_close(REPO, 1):
            with self.assertRaises(ShadowHeld):
                gh.rest("PATCH", "/repos/%s/issues/2" % REPO, {"state": "closed"})
            with self.assertRaises(ShadowHeld):
                gh.rest("PUT", "/repos/%s/pulls/101/merge" % REPO, {})
            with self.assertRaises(ShadowHeld):
                gh.rest("PATCH", "/repos/%s/issues/1" % REPO,
                        {"state": "closed", "labels": ["loom:shipped"]})
            self.assertEqual(self.fake.issues[1]["state"], "open")
            # The named ticket's plain close is the one request that passes.
            self.fake.issues[1]["labels"].append({"name": "force-close"})
            gh.rest("PATCH", "/repos/%s/issues/1" % REPO,
                    {"state": "closed", "state_reason": "completed"})
        self.assertEqual(self.fake.issues[1]["state"], "closed")
        self.assertEqual(self.fake.issues[2]["state"], "open")


class Guardrail(Case):
    def state(self):
        path = os.path.join(self.home, "state", "station", "evidence-close.json")
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def control(self, *argv):
        from bus import evidence_close
        out = io.StringIO()
        env = {"LOOM_HOME": self.home}
        with mock.patch.dict(os.environ, env), redirect_stdout(out):
            self.assertEqual(evidence_close.main(list(argv)), 0)
        return out.getvalue()

    def reopen(self, number):
        self.fake.issues[number]["state"] = "open"

    def test_the_pass_is_bounded_and_says_its_bound(self):
        for number in range(1, 6):
            self.ticket(number)
        said = self.close_pass()
        closed = [n for n in range(1, 6) if self.fake.issues[n]["state"] == "closed"]
        self.assertEqual(len(closed), 3)
        line = [s for s in said if s.startswith("evidence close:")]
        self.assertEqual(len(line), 1)
        self.assertIn("3 closed", line[0])
        self.assertIn("at most 3 closes", line[0])
        self.assertIn("2 waiting", line[0])

    def test_the_day_is_bounded(self):
        now = time.time()
        for number in range(1, 14):
            self.ticket(number)
        for step in range(5):
            self.close_pass(now + step * 300)
        closed = [n for n in range(1, 14) if self.fake.issues[n]["state"] == "closed"]
        self.assertEqual(len(closed), 10)
        self.close_pass(now + 90000)
        closed = [n for n in range(1, 14) if self.fake.issues[n]["state"] == "closed"]
        self.assertEqual(len(closed), 13)

    def test_a_reopen_above_the_limit_stops_this_close(self):
        self.ticket(1)
        self.close_pass()
        self.reopen(1)
        self.ticket(2)
        said = self.close_pass()
        self.assertEqual(self.fake.issues[2]["state"], "open")
        self.assertTrue(any("evidence close paused" in s for s in said), said)
        self.assertIn("1 of 1", self.control("status"))
        self.assertIn("Paused", self.proof_issues()[0]["body"])

    def asks(self):
        found = []
        for issue in self.proof_issues():
            for comment in self.fake.comments.get(issue["number"], []):
                if "evidence close is stopped" in str(comment.get("body") or ""):
                    found.append(comment)
        return found

    def test_a_stop_raises_one_ask_however_many_passes_see_it(self):
        self.ticket(1)
        self.ticket(2)
        self.close_pass()
        self.reopen(1)
        self.ticket(3)
        self.close_pass()
        self.reopen(2)
        self.close_pass()
        self.close_pass()
        self.assertEqual(self.fake.issues[3]["state"], "open")
        [issue] = self.proof_issues()
        self.assertIn("needs:human", self.fake.names(issue["number"]))
        found = self.asks()
        self.assertEqual(len(found), 1, found)
        body = str(found[0].get("body") or "")
        self.assertIn("closes reopened, above 5%", body)
        self.assertIn("python3 -m bus.evidence_close resume", body)

    def test_an_operators_own_pause_asks_nothing(self):
        self.ticket(1)
        self.close_pass()
        self.control("pause")
        self.ticket(2)
        self.close_pass()
        self.close_pass()
        self.assertEqual(self.fake.issues[2]["state"], "open")
        [issue] = self.proof_issues()
        self.assertNotIn("needs:human", self.fake.names(issue["number"]))
        self.assertEqual(self.asks(), [])

    def test_a_later_stop_after_a_resume_raises_a_new_one(self):
        self.ticket(1)
        self.close_pass()
        self.reopen(1)
        self.close_pass()
        self.assertEqual(len(self.asks()), 1)
        self.control("resume")
        self.ticket(2)
        self.close_pass(time.time() + 5)
        self.assertEqual(self.fake.issues[2]["state"], "closed")
        self.reopen(2)
        self.close_pass(time.time() + 10)
        self.assertEqual(len(self.asks()), 2, self.asks())

    def test_a_reopen_at_the_limit_does_not_stop_it(self):
        now = time.time()
        for number in range(1, 21):
            self.ticket(number)
        for step in range(7):
            self.close_pass(now + step * 90000)
        for number in range(1, 21):
            self.assertEqual(self.fake.issues[number]["state"], "closed", number)
        self.reopen(1)
        self.ticket(21)
        self.close_pass(now + 8 * 90000)
        self.assertEqual(self.fake.issues[21]["state"], "closed")   # 1 of 20 is 5%

    def test_a_reopen_counts_after_the_ticket_closes_again(self):
        self.ticket(1)
        self.close_pass()
        self.reopen(1)
        self.close_pass()
        self.fake.issues[1]["state"] = "closed"
        self.ticket(2)
        self.close_pass()
        self.assertEqual(self.fake.issues[2]["state"], "open")

    def test_the_operator_resumes_and_earlier_closes_stop_counting(self):
        self.ticket(1)
        self.close_pass()
        self.reopen(1)
        self.close_pass()
        self.control("resume")
        self.ticket(2)
        self.close_pass(time.time() + 5)
        self.assertEqual(self.fake.issues[2]["state"], "closed")

    def test_an_unreadable_count_stops_this_close_and_keeps_the_file(self):
        path = os.path.join(self.home, "state", "station", "evidence-close.json")
        self.write(path, "{not json")
        self.ticket(1)
        self.close_pass()
        self.assertEqual(self.fake.issues[1]["state"], "open")
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "{not json")

    def test_a_failed_reopen_read_stops_this_pass(self):
        self.ticket(1)
        self.close_pass()
        self.ticket(2)
        real = station.Station._paginate

        def refuse(st, path, *rest, **kw):
            if "labels=closed-unproven" in path:
                raise station.GitHubError("refused")
            return real(st, path, *rest, **kw)
        with mock.patch.object(station.Station, "_paginate", refuse):
            self.close_pass()
        self.assertEqual(self.fake.issues[2]["state"], "open")

    def test_the_operator_pauses(self):
        self.control("pause")
        self.ticket(1)
        self.close_pass()
        self.assertEqual(self.fake.issues[1]["state"], "open")

    def test_a_refused_ticket_is_read_again_only_after_its_cooldown(self):
        now = time.time()
        self.ticket(1, conclusion="failure")
        said = self.close_pass(now)
        self.assertEqual(sum("not closing" in s for s in said), 1)
        reads = len(self.fake.calls)
        self.assertFalse([s for s in self.close_pass(now + 300) if "not closing" in s])
        self.assertFalse([p for m, p in self.fake.calls[reads:] if "check-runs" in p])

    def test_a_ticket_that_needs_no_reads_costs_none(self):
        self.ticket(1, body=BODY.replace("- [x] A test", "- [ ] A test"))
        self.close_pass()
        self.assertFalse([p for m, p in self.fake.calls if "check-runs" in p])


if __name__ == "__main__":
    unittest.main()
