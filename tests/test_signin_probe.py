"""The sign-in probe (loom#3572): lanes/signin_probe.sh proves a subscription
lane's sign-in in place under the lanes' run locks, and the station's
bus/signin.py Watch alerts, asks and closes from what it records.

A fake codex CLI on PATH fails, then passes; a fake GitHub holds the asks."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bus import lane_state, signin, state_store, station  # noqa: E402
from bus.github import Response  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

ROOT = Path(__file__).resolve().parent.parent
PROBE = ROOT / "lanes" / "signin_probe.sh"
RUNNER = ROOT / "lanes" / "run_lane.sh"
REPO = "example/factory"
STATION = "loom-station[bot]"
LANES = "loom-lanes[bot]"


def make_watch(*args, **kw):
    """A Watch run as the station, trusting the lanes App as Loom's own."""
    return signin.Watch(*args, own=STATION, trusted=[LANES], **kw)

# Each call appends what it saw of the locks, then answers as FAKE_MODE says:
# the first line of the file FAKE_MODES is used and dropped.
FAKE_CODEX = r"""#!/usr/bin/env bash
mode="$(head -1 "$FAKE_MODES")"; tail -n +2 "$FAKE_MODES" > "$FAKE_MODES.next"; mv "$FAKE_MODES.next" "$FAKE_MODES"
held=""
for s in $FAKE_SLOTS; do held="$held $s=$(cat "$LOOM_HOME/run/$s.lock/pid" 2>/dev/null)"; done
printf 'codex home=%s at=%s held=%s args=%s\n' "$CODEX_HOME" "$(date +%s)" "$held" "$*" >> "$FAKE_LOG"
[ -z "${FAKE_SLEEP:-}" ] || sleep "$FAKE_SLEEP"
case "$mode" in
  fail) echo "Error: unexpected status 401 Unauthorized: token=abcdefghijklmnop1234 refresh failed"; exit 1 ;;
  limit) echo "You've hit your usage limit. Try again in 3 hours."; exit 1 ;;
  *) echo ok ;;
esac
"""


class FakeGitHub:
    """Issues and comments on one repo, as the Watch reads and writes them."""

    def __init__(self):
        self.issues = {}
        self.comments = {}
        self.next_id = 1000
        self.writes = []
        self.requests = 0

    def add_issue(self, number, title, body, labels=("needs:human",), user=LANES):
        self.issues[number] = {"number": number, "title": title, "body": body, "state": "open",
                               "labels": [{"name": n} for n in labels], "user": {"login": user}}
        self.comments.setdefault(number, [])

    def add_comment(self, number, body, user=LANES):
        self.next_id += 1
        self.comments.setdefault(number, []).append({"id": self.next_id, "body": body,
                                                     "user": {"login": user}})
        return self.next_id

    def rest(self, method, path, body=None, **_kw):
        self.requests += 1
        base = "/repos/%s/issues" % REPO
        if method == "GET" and path.startswith(base + "?"):
            return Response(200, [dict(i) for i in self.issues.values() if i["state"] == "open"])
        if method == "POST" and path == base:
            number = max(self.issues, default=0) + 1
            self.add_issue(number, body["title"], body["body"], body.get("labels", ()), user=STATION)
            self.writes.append(("create", number))
            return Response(201, {"number": number})
        rest = path[len(base) + 1:]
        if rest.startswith("comments/"):
            ident = int(rest.split("/")[1])
            for found in self.comments.values():
                for c in found:
                    if c["id"] == ident:
                        c["body"] = body["body"]
                        self.writes.append(("edit-comment", ident))
                        return Response(200, c)
            return Response(404, None)
        number = int(rest.split("/")[0].split("?")[0])
        if rest.split("?")[0].endswith("/comments"):
            if method == "GET":
                return Response(200, list(self.comments.get(number, [])))
            ident = self.add_comment(number, body["body"], user=STATION)
            self.writes.append(("comment", number))
            return Response(201, {"id": ident})
        if method == "GET":
            return Response(200, dict(self.issues[number]))
        if method == "PATCH":
            self.issues[number].update(body)
            self.writes.append(("close", number, body.get("state_reason")))
            return Response(200, self.issues[number])
        raise AssertionError("unexpected %s %s" % (method, path))


class ProbeCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.home = root / "loom"
        (self.home / "run").mkdir(parents=True)
        self.bin = root / "bin"
        self.bin.mkdir()
        codex = self.bin / "codex"
        codex.write_text(FAKE_CODEX)
        codex.chmod(0o755)
        self.modes = root / "modes"
        self.modes.write_text("")
        self.log = root / "calls.log"
        self.log.touch()
        self.codex_home = root / "codex-home"
        self.codex_home.mkdir()
        self.state = root / "state"
        self.state.mkdir()
        previous = state_store.active()
        self.addCleanup(state_store.use, previous)
        state_store.use(state_store.FileStore())

    def env(self, slots, **extra):
        return {"PATH": "%s:/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin" % self.bin,
                "HOME": self._tmp.name, "LOOM_HOME": str(self.home),
                "CODEX_HOME": str(self.codex_home), "LOOM_CODEX_MODEL": "gpt-test",
                "FAKE_MODES": str(self.modes), "FAKE_LOG": str(self.log),
                "FAKE_SLOTS": " ".join(slots), "LOOM_SIGNIN_PROBE_POLL": "1", **extra}

    def probe(self, mode, slots=("codex", "codex-2"), **extra):
        self.modes.write_text(mode + "\n")
        return subprocess.run(["bash", str(PROBE), *slots], env=self.env(slots, **extra),
                              capture_output=True, text=True, timeout=60)

    def calls(self):
        return self.log.read_text().splitlines()


class ProbeFailsThenPassesTest(ProbeCase):
    def watch(self, gh, alerts, now, beats=None):
        beats = beats or {}
        return make_watch(gh, REPO, str(self.home), str(self.state), now,
                            lambda line: alerts.append(line) or True, lambda _line: None,
                            beats.get)

    def test_one_alert_one_recovery_and_the_ask_opened_then_closed(self):
        gh, alerts = FakeGitHub(), []
        done = self.probe("fail")
        self.assertEqual(done.returncode, 0, done.stderr)
        record = signin.read(str(self.home))["codex"]
        self.assertEqual((record.result, record.slots), ("fail", ("codex", "codex-2")))
        # In place: the lane's own Codex home, not a copy.
        self.assertIn("home=%s " % self.codex_home, self.calls()[0])
        self.assertIn("--model gpt-test", self.calls()[0])

        self.watch(gh, alerts, record.at + 5).tend()
        self.assertEqual(len(alerts), 1)
        self.assertIn("lane codex (codex codex-2) cannot sign in", alerts[0])
        self.assertIn("401 Unauthorized", alerts[0])
        self.assertNotIn("abcdefghijklmnop1234", alerts[0])
        [(kind, number)] = [w for w in gh.writes if w[0] == "create"]
        ask = gh.issues[number]
        self.assertEqual(ask["title"], signin.ask_title("codex"))
        self.assertEqual([lab["name"] for lab in ask["labels"]], ["needs:human"])
        self.assertIn(signin.KEY_ASK_MARKER, ask["body"])
        # The public ask names nothing the CLI said.
        self.assertNotIn("401", ask["body"])

        # The same failure, read again, and a later failure alert nobody again.
        self.watch(gh, alerts, record.at + 65).tend()
        # Give each later probe its own scheduled timestamp. CLI records use
        # whole seconds, so back-to-back calls can look like the same probe.
        signin.record(str(self.home), "fail", record.slots, record.error, now=record.at + 1800)
        self.watch(gh, alerts, record.at + 1900).tend()
        self.assertEqual(len(alerts), 1)
        self.assertEqual([w for w in gh.writes if w[0] == "create"], [(kind, number)])
        self.assertEqual(gh.issues[number]["state"], "open")

        signin.record(str(self.home), "pass", record.slots, "", now=record.at + 3600)
        self.watch(gh, alerts, record.at + 3800).tend()
        self.assertEqual(len(alerts), 2)
        self.assertIn("lane codex (codex codex-2) signs in again", alerts[1])
        self.assertEqual(gh.issues[number]["state"], "closed")
        self.assertIn(("close", number, "completed"), gh.writes)
        self.assertIn("sign-in probe of lane codex passed",
                      gh.comments[number][-1]["body"])
        # Nothing more once settled.
        writes = list(gh.writes)
        self.watch(gh, alerts, record.at + 3860).tend()
        self.assertEqual((len(alerts), gh.writes), (2, writes))

    def test_a_failure_adds_to_the_ask_a_lane_already_filed(self):
        gh, alerts = FakeGitHub(), []
        gh.add_issue(41, signin.ask_title("codex"),
                     signin.KEY_ASK_MARKER + "\n\nLane codex-3 could not use the provider key it reads.")
        self.probe("fail")
        self.watch(gh, alerts, time.time()).tend()
        self.assertEqual([w for w in gh.writes if w[0] == "create"], [])
        self.assertIn("Lanes seen: codex", gh.comments[41][-1]["body"])
        self.assertTrue(gh.comments[41][-1]["body"].startswith(signin.KEY_REPEAT_MARKER))

    def test_a_forged_repeat_comment_is_neither_edited_nor_counted(self):
        gh, alerts = FakeGitHub(), []
        gh.add_issue(41, signin.ask_title("codex"),
                     signin.KEY_ASK_MARKER + "\n\nLane codex-3 could not use the provider key it reads.")
        forged_body = (signin.KEY_REPEAT_MARKER + "\nSign-in probe failed again: 999 times since "
                       "2020-01-01T00:00:00Z, last at 2020-01-01T00:00:00Z.\n\nLanes seen: zen\n")
        forged = gh.add_comment(41, forged_body, user="a-stranger")
        self.probe("fail")
        self.watch(gh, alerts, time.time()).tend()
        self.assertNotIn(("edit-comment", forged), gh.writes)
        self.assertEqual(gh.comments[41][0]["body"], forged_body)
        [mine] = [c for c in gh.comments[41] if c["user"]["login"] == STATION]
        self.assertIn("Sign-in probe failed again: 1 times", mine["body"])
        self.assertIn("Lanes seen: codex", mine["body"])
        self.assertNotIn("zen", mine["body"])
        # Its "Lanes seen" names no lane either: only the ask's own lane counts.
        self.assertEqual(signin.lanes_of_ask(gh.issues[41]["body"], gh.comments[41], [STATION]),
                         ["codex", "codex-3"])

    def test_an_ask_a_stranger_opened_is_not_counted_on_or_closed(self):
        gh, alerts = FakeGitHub(), []
        gh.add_issue(41, signin.ask_title("codex"), signin.KEY_ASK_MARKER, user="a-stranger")
        self.probe("pass")
        self.watch(gh, alerts, time.time()).tend()
        self.assertEqual(gh.issues[41]["state"], "open")
        time.sleep(1.1)  # a later probe, a new record
        self.probe("fail")
        self.watch(gh, alerts, time.time() + 60).tend()
        self.assertEqual(len([w for w in gh.writes if w[0] == "create"]), 1)
        self.assertEqual(gh.comments[41], [])

    def test_a_limit_says_nothing_of_the_sign_in(self):
        gh, alerts = FakeGitHub(), []
        self.probe("limit")
        self.assertEqual(signin.read(str(self.home))["codex"].result, "unknown")
        self.watch(gh, alerts, time.time()).tend()
        self.assertEqual((alerts, [w for w in gh.writes if w[0] != "close"]), ([], []))

    def test_an_alert_that_did_not_go_is_sent_on_the_next_pass(self):
        gh, sent = FakeGitHub(), []
        self.probe("fail")
        now = time.time()
        make_watch(gh, REPO, str(self.home), str(self.state), now, lambda line: False,
                     lambda _line: None, {}.get).tend()
        self.assertEqual([w for w in gh.writes if w[0] == "create"], [])
        self.watch(gh, sent, now + 60).tend()
        self.assertEqual(len(sent), 1)
        self.assertEqual(len([w for w in gh.writes if w[0] == "create"]), 1)


class ProbeWaitsForTheLaneLockTest(ProbeCase):
    def test_the_probe_waits_for_a_busy_slot_and_then_holds_every_lock(self):
        holder = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: (holder.kill(), holder.wait()))
        busy = self.home / "run" / "codex-2.lock"
        busy.mkdir()
        (busy / "pid").write_text("%d\n" % holder.pid)
        self.modes.write_text("pass\n")
        proc = subprocess.Popen(["bash", str(PROBE), "codex", "codex-2"],
                                env=self.env(("codex", "codex-2")),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        time.sleep(3)
        self.assertEqual(self.calls(), [], "the probe ran beside a lane run")
        self.assertIsNone(proc.poll())
        released = time.time()
        import shutil
        shutil.rmtree(busy)
        proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0)
        [call] = self.calls()
        ran_at = int(call.split(" at=")[1].split()[0])
        self.assertGreaterEqual(ran_at, int(released))
        held = dict(p.split("=") for p in call.split(" held=")[1].split(" args=")[0].split())
        self.assertEqual(held["codex"], held["codex-2"])
        self.assertNotEqual(held["codex"], str(holder.pid))
        # Both locks are let go when the probe ends.
        self.assertFalse((self.home / "run" / "codex.lock").exists())
        self.assertFalse(busy.exists())

    def test_a_probe_that_waits_too_long_records_nothing(self):
        holder = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: (holder.kill(), holder.wait()))
        busy = self.home / "run" / "codex.lock"
        busy.mkdir()
        (busy / "pid").write_text("%d\n" % holder.pid)
        done = self.probe("pass", LOOM_SIGNIN_PROBE_WAIT="2")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.calls(), [])
        self.assertEqual(signin.read(str(self.home)), {})
        self.assertEqual((busy / "pid").read_text().strip(), str(holder.pid))

    def test_a_lane_runner_that_starts_while_the_probe_holds_its_lock_exits(self):
        self.modes.write_text("pass\n")
        proc = subprocess.Popen(["bash", str(PROBE), "codex", "codex-2"],
                                env=self.env(("codex", "codex-2"), FAKE_SLEEP="5"),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        deadline = time.monotonic() + 10
        while not self.calls():
            self.assertLess(time.monotonic(), deadline, "the probe never ran")
            time.sleep(0.1)
        pid = (self.home / "run" / "codex-2.lock" / "pid").read_text().strip()
        runner = subprocess.run(["bash", str(RUNNER), "codex-2"],
                                env=self.env(("codex-2",)), capture_output=True, text=True,
                                timeout=30)
        self.assertEqual(runner.returncode, 0, runner.stderr)
        self.assertEqual((self.home / "run" / "codex-2.lock" / "pid").read_text().strip(), pid)
        proc.communicate(timeout=30)
        self.assertEqual(signin.read(str(self.home))["codex"].result, "pass")


class OffLaneAskTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        previous = state_store.active()
        self.addCleanup(state_store.use, previous)
        state_store.use(state_store.FileStore())

    def test_an_ask_from_lanes_with_no_heartbeat_for_a_day_is_closed(self):
        now = 1_800_000_000
        gh = FakeGitHub()
        body = signin.KEY_ASK_MARKER + "\n\nLane codex-8 could not use the provider key it reads."
        gh.add_issue(3, signin.ask_title("codex"), body)
        gh.add_comment(3, signin.KEY_REPEAT_MARKER + "\nCould not use the key again: 2 times "
                       "since 2026-11-22T21:30:32Z, last at 2026-11-22T21:30:33Z.\n\n"
                       "Lanes seen: codex-8, codex-7\n")
        # The station runs the sweep once a lane here records probes.
        signin.record(self._tmp.name, "pass", ["claude"], "ok", now=now - 60)
        # Seen by a lane that still beats: it stays open.
        gh.add_issue(4, "Provider key expired: renew LOOM_ZEN_KEY_SSM",
                     signin.KEY_ASK_MARKER + "\n\nLane zen could not use the provider key it reads.")
        # Not a key-ask: left alone whatever its lanes.
        gh.add_issue(5, "Something else", "Lane codex-8 could not use a thing.")
        beats = {"codex-8": now - 25 * 3600, "zen": now - 120}
        said = []
        make_watch(gh, REPO, self._tmp.name, self._tmp.name, now, lambda _l: True,
                     said.append, beats.get).tend()
        self.assertEqual(gh.issues[3]["state"], "closed")
        self.assertEqual(gh.issues[3]["state_reason"], "not_planned")
        self.assertIn("lanes codex-7, codex-8 have had no heartbeat for 24 hours",
                      gh.comments[3][-1]["body"])
        self.assertEqual((gh.issues[4]["state"], gh.issues[5]["state"]), ("open", "open"))
        # The sweep runs at most hourly.
        gh.add_issue(6, signin.ask_title("codex"),
                     signin.KEY_ASK_MARKER + "\n\nLane codex-6 could not use the provider key it reads.")
        make_watch(gh, REPO, self._tmp.name, self._tmp.name, now + 600, lambda _l: True,
                     said.append, beats.get).tend()
        self.assertEqual(gh.issues[6]["state"], "open")
        make_watch(gh, REPO, self._tmp.name, self._tmp.name, now + 3600, lambda _l: True,
                     said.append, beats.get).tend()
        self.assertEqual(gh.issues[6]["state"], "closed")

    def test_a_station_with_no_probe_records_reads_nothing(self):
        gh = FakeGitHub()
        make_watch(gh, REPO, self._tmp.name, self._tmp.name, 1_800_000_000, lambda _l: True,
                     lambda _l: None, {}.get).tend()
        self.assertEqual(gh.requests, 0)

    def test_an_ask_for_a_lane_whose_probe_fails_stays_open(self):
        now = 1_800_000_000
        gh = FakeGitHub()
        gh.add_issue(3, signin.ask_title("codex"),
                     signin.KEY_ASK_MARKER + "\n\nLane codex-8 could not use the provider key it reads.")
        signin.record(self._tmp.name, "fail", ["codex"], "error: 401", now=now - 60)
        make_watch(gh, REPO, self._tmp.name, self._tmp.name, now, lambda _l: True,
                     lambda _l: None, {"codex": now - 30}.get).tend()
        self.assertEqual(gh.issues[3]["state"], "open")


class LaneTableTest(unittest.TestCase):
    def test_the_lane_status_table_shows_the_last_probe_per_lane(self):
        with tempfile.TemporaryDirectory() as home:
            at = 1_800_000_000
            signin.record(home, "pass", ["codex", "codex-2"], "ok", now=at)
            runner = station.Station(FakeGitHub(), station.Settings(home, [REPO], [], []), now=at)
            with mock.patch.object(runner, "_spend_shown", return_value=None), \
                    mock.patch.object(runner, "_scorecard_lines", return_value=[]), \
                    mock.patch.object(runner, "_remote_claim_count", return_value=0), \
                    mock.patch.object(runner, "_disk_status_line", return_value=""):
                body = runner._lanes_body([("codex", "serving", "-", "Accepting work"),
                                           ("codex-2", "busy", "-", "Run in progress"),
                                           ("claude", "serving", "-", "Accepting work")])
        stamp = time.strftime("%Y-%m-%d %H:%MZ", time.gmtime(at))
        self.assertIn("| lane | state | heartbeat (UTC) | reason | sign-in (UTC) |", body)
        rows = {r["lane"]: r for r in lane_state.published_rows(body)}
        self.assertEqual(rows["codex"]["signin"], "pass " + stamp)
        self.assertEqual(rows["codex-2"]["signin"], "pass " + stamp)
        self.assertEqual(rows["claude"]["signin"], "-")
        self.assertEqual(rows["codex"]["reason"], "Accepting work")

    def test_a_table_from_before_the_column_still_reads(self):
        rows = lane_state.published_rows("| codex | serving | - | Accepting work |")
        self.assertEqual(rows, [{"lane": "codex", "available": True, "heartbeat": "-",
                                 "state": "serving", "reason": "Accepting work"}])


class RecordTest(unittest.TestCase):
    def test_the_record_masks_keys_and_paths(self):
        with tempfile.TemporaryDirectory() as home:
            signin.record(home, "fail", ["claude"],
                          "line one\n\nError: api_key=sk-abcdefghijklmnopqrstuv invalid at "
                          "/srv/lane/.claude/x\n", now=5)
            probe = signin.read(home)["claude"]
        self.assertNotIn("sk-abc", probe.error)
        self.assertNotIn("/srv/lane", probe.error)
        self.assertIn("line one | Error:", probe.error)

    def test_slots_of_two_lanes_are_refused(self):
        with tempfile.TemporaryDirectory() as home, self.assertRaises(ValueError):
            signin.record(home, "pass", ["codex", "claude"], "")


if __name__ == "__main__":
    unittest.main()
