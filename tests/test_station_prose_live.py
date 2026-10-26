"""A prose Live box in a repo with a live-evidence adapter gets a verify run
before the station asks a person (loom#1855).

The criteria gate (bus/criteria_gate.py) can run only a URL, a `gh api` read
or a `kubectl get`; a Live box in prose used to go straight to loom:human.
In a repo that declares a read-only adapter (loom#1770) and is opted in, the
station hands the ticket to a verify run first. The run may answer with a
templated step, which the station reads through the adapter itself: the box
is proven on that read, never on the lane's prose.
"""
from __future__ import annotations

import json
import os
import sys
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import live_adapter, live_proof, station, status  # noqa: E402
from bus.github import GitHub  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

import test_bus_station as tbs  # noqa: E402

REPO = tbs.REPO
HOUR = 3600
NUMBER = 1
PROSE = "Live: the measurements on the earlier ticket still hold after the extraction"
BODY = ("Difficulty: easy\n\n## Why\nBecause.\n\n## Acceptance criteria\n"
        "- [ ] A test passes\n- [ ] %s\n" % PROSE)
STEP = "`log:web` `bridge up`"
NAME = "example-owner.example-repo-1-verify-1"


def stamp(at):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))


def adapter_runner(answer):
    """A fake adapter runner serving the `web` log and no SQL; `answer` is
    the live_proof.Answer every log read returns."""
    command = live_adapter.Command(("log", "{source}", "{since}", "{needle}"), 60)
    adapter = live_adapter.Adapter(command, ("web",), None, {})
    return types.SimpleNamespace(adapter=adapter, log=mock.Mock(return_value=answer),
                                 query=mock.Mock())


class ProseLiveTest(tbs.Base):
    def setUp(self):
        super().setUp()
        self.fake.private = False
        self.now = float(int(time.time()))
        self.fake.add(NUMBER, body=BODY, state="shipped")
        self.shipped_at = self.now - 30 * HOUR
        self.fake.events[NUMBER].append(dict(
            tbs.labeled("loom:shipped", "alice-example"), created_at=stamp(self.shipped_at)))
        self.fake.pulls.append({"number": 41, "state": "closed", "merged_at": stamp(self.shipped_at),
                                "merge_commit_sha": "%040x" % 41, "body": "Refs %s#1" % REPO,
                                "head": {"ref": "loom/1-fix"}})
        brief = ("# Brief: %s\n\nRepo: %s\nBranch: loom/verify-1\nLane: codex\nIssue: %s#1\n"
                 "Deliverable: verify\n\n# T1: Fix the thing\n" % (NAME, REPO, REPO))
        self.write(os.path.join(self.inbox("codex", "done"), NAME + ".md"), brief)

    def station(self, runner):
        self.settings.live_evidence_repos = [REPO] if runner is not None else []
        st = station.Station(GitHub("token", transport=self.fake), self.settings, now=self.now)
        patch = mock.patch.object(station.Station, "_live_adapter", return_value=runner)
        patch.start()
        self.addCleanup(patch.stop)
        return st

    def answer(self, data):
        self.write(os.path.join(self.home, "logs", "codex", NAME + ".verify.json"),
                   data if isinstance(data, str) else json.dumps(data))

    def report(self, st):
        st._report_verify("codex", "done", REPO, NUMBER, NAME)

    def asks(self):
        return [c["body"] for c in self.fake.comments[NUMBER] if "Kind: decision" in c["body"]]

    def plain_comments(self):
        return [c["body"] for c in self.fake.comments[NUMBER]
                if not status.is_status(c.get("body"))]

    def test_a_prose_box_gets_a_verify_run_and_no_ask(self):
        st = self.station(adapter_runner(live_proof.Answer([], hours=24, count=0)))
        st.close_done_tickets()
        self.assertEqual(self.asks(), [])
        self.assertNotIn("loom:human", self.fake.names(NUMBER))
        st._hand_verifies(["codex"])
        [name] = [n for n in os.listdir(self.inbox("codex")) if n.endswith(".md")]
        with open(os.path.join(self.inbox("codex"), name)) as fh:
            text = fh.read()
        self.assertIn("Deliverable: verify", text)
        self.assertIn("## Live evidence adapter", text)
        self.assertIn("`log:web`", text)
        self.assertIn('{"step": ', text)
        st.close_done_tickets()
        self.assertEqual(self.asks(), [])

    def test_a_log_step_the_adapter_matches_proves_the_box_from_the_stations_read(self):
        line = "%s web: bridge up" % stamp(self.now - 2 * HOUR)
        runner = adapter_runner(live_proof.Answer([line], hours=48, count=1))
        st = self.station(runner)
        self.answer({"step": STEP})
        self.report(st)
        runner.log.assert_called_once_with("web", "bridge up", self.shipped_at)
        issue = self.fake.issues[NUMBER]
        self.assertEqual((issue["state"], issue.get("state_reason")), ("closed", "completed"))
        [note] = [c for c in self.plain_comments() if "Every acceptance criterion has evidence" in c]
        self.assertIn("    > " + line, note)
        self.assertEqual(self.asks(), [])

    def test_a_step_the_adapter_does_not_match_raises_one_ask_naming_it(self):
        runner = adapter_runner(live_proof.Answer([], hours=48, count=0))
        st = self.station(runner)
        self.answer({"step": STEP})
        self.report(st)
        st.close_done_tickets()
        st.close_done_tickets()
        self.assertEqual(self.fake.issues[NUMBER]["state"], "open")
        [ask] = self.asks()
        self.assertIn(STEP, ask)
        self.assertIn("no `web` log line since the deploy names `bridge up`", ask)
        self.assertIn("loom:human", self.fake.names(NUMBER))

    def test_an_adapter_read_that_fails_raises_one_ask_naming_the_step_and_reason(self):
        runner = adapter_runner(live_proof.Answer(None, error="timed out"))
        st = self.station(runner)
        self.answer({"step": STEP})
        self.report(st)
        st.close_done_tickets()
        [ask] = self.asks()
        self.assertIn(STEP, ask)
        self.assertIn("timed out", ask)

    def test_an_answer_with_no_step_raises_one_ask_saying_so(self):
        st = self.station(adapter_runner(live_proof.Answer([], hours=24, count=0)))
        self.answer({"missing": "the archive read"})
        self.report(st)
        st.close_done_tickets()
        st.close_done_tickets()
        [ask] = self.asks()
        self.assertIn("the verify run returned no step", ask)

    def test_a_step_that_writes_is_refused_and_asks(self):
        runner = adapter_runner(live_proof.Answer([], hours=24, count=0))
        st = self.station(runner)
        self.answer({"step": "`sql:DELETE FROM t`"})
        self.report(st)
        st.close_done_tickets()
        runner.query.assert_not_called()
        [ask] = self.asks()
        self.assertIn("`sql:DELETE FROM t`", ask)

    def test_a_repo_without_an_adapter_gets_todays_ask_and_no_verify_run(self):
        st = self.station(None)
        st.close_done_tickets()
        st._hand_verifies(["codex"])
        self.assertEqual([n for n in os.listdir(self.inbox("codex")) if n.endswith(".md")], [])
        [ask] = self.asks()
        self.assertIn("%s (no check Loom can run)" % PROSE, ask)
        self.assertIn("loom:human", self.fake.names(NUMBER))


if __name__ == "__main__":
    unittest.main()
