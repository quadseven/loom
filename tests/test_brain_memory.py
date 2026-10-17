"""The station's side of brain memory (docs/decisions/0002, item 3): a
brain_ticks row and its commitments built from state/brain.json, the step
record and the tick's journals; the rules it loads; and the handoff packet a
brain reads first."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bus import brain_memory, handoff, station  # noqa: E402
from bus.github import GitHub  # noqa: E402
from dashboard import brain_memory as dashboard_memory  # noqa: E402

REPO = "example-owner/example-repo"
HOLDER = "lane-machine"
AUTHORS = ["alice-example"]
START = 1792231200  # 2026-10-17T10:00:00Z


def iso(at):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))


def brain_json(started, minutes=20, outcome="ok"):
    return {"at": started + minutes * 60, "outcome": outcome, "exit": 0, "started": started,
            "lane": "codex", "model": "model-c", "holder": HOLDER}


def journal(at, holder, *lines):
    return {"body": "\n".join([handoff.JOURNAL_MARKER, "Tick %s, %s" % (iso(at), holder)]
                              + list(lines)),
            "at": iso(at), "login": "alice-example"}


DEFER = handoff.defer_marker("merge", (REPO, 8), "abc123", HOLDER, note="checks still running")


class TickRowTest(unittest.TestCase):
    """The acceptance test: a tick row and its commitments go into the
    in-memory store, and a later done marker closes the commitment."""

    def test_a_tick_and_its_commitment_are_stored_and_a_later_done_closes_it(self):
        store = dashboard_memory.Store(dashboard_memory.MemoryBackend())
        record = {"holder": HOLDER, "started_at": iso(START - 60), "step": "unlock",
                  "notes": [{"ts": iso(START + 300), "step": "merge",
                             "text": "merged %s#7, the oldest ready" % REPO}]}
        first = journal(START + 600, HOLDER, "- Shipped: %s#7" % REPO,
                        "- Briefed: %s#9" % REPO, "- Waiting: none", DEFER)
        body = brain_memory.tick_body(brain_json(START), record, [first], HOLDER)
        self.assertEqual({k: body[k] for k in ("holder", "lane", "model", "started", "ended",
                                               "outcome")},
                         {"holder": HOLDER, "lane": "codex", "model": "model-c",
                          "started": iso(START), "ended": iso(START + 1200), "outcome": "ok"})
        self.assertEqual(body["decided"], ["merge: merged %s#7, the oldest ready" % REPO,
                                           "shipped %s#7" % REPO])
        self.assertEqual(body["left_open"], ["merge %s#8: checks still running" % REPO])
        self.assertEqual(body["handed_off"], ["%s#9" % REPO])
        tick_id = store.record_tick(body)["id"]

        packet = store.handoff()
        self.assertEqual([t["id"] for t in packet["ticks"]], [tick_id])
        self.assertEqual([(c["item"], c["stage"], c["what"], c["state"], c["tick_id"])
                          for c in packet["commitments"]],
                         [("%s#8" % REPO, "merge", "merge: checks still running", "open",
                           tick_id)])
        self.assertEqual(packet["commitments"][0]["due"], iso(START + 600 + 2 * 3600))

        # Another brain finishes the merge; the next lane-machine tick carries
        # its done marker, and the commitment closes.
        done = journal(START + 2400, "muse", handoff.done_marker("merge", (REPO, 8), "muse"))
        later = START + 3600
        body2 = brain_memory.tick_body(brain_json(later), None, [first, done], HOLDER,
                                       since=START + 1200)
        self.assertEqual([(c["item"], c["state"]) for c in body2["commitments"]],
                         [("%s#8" % REPO, "done")])
        second = store.record_tick(body2)
        self.assertEqual(second["closed"], 1)
        self.assertEqual(store.handoff()["commitments"], [])
        row = store.backend.commitments[0]
        self.assertEqual((row["state"], row["closed_tick"]), ("done", second["id"]))

    def test_the_next_tick_soon_after_neither_reopens_nor_repeats_the_last(self):
        store = dashboard_memory.Store(dashboard_memory.MemoryBackend())
        a = journal(START + 600, HOLDER, "- Shipped: %s#7" % REPO, DEFER)
        a_done = journal(START + 1100, HOLDER, handoff.done_marker("merge", (REPO, 8), HOLDER))
        first = brain_memory.tick_body(brain_json(START), None, [a, a_done], HOLDER)
        store.record_tick(first)
        self.assertEqual(store.handoff()["commitments"], [])
        # B starts 5 minutes after A ended at START + 1200.
        b = brain_memory.tick_body(brain_json(START + 1500), None, [a, a_done], HOLDER,
                                   since=START + 1200 + brain_memory.GRACE)
        self.assertEqual((b["commitments"], b["decided"], b["left_open"]), ([], [], []))
        store.record_tick(b)
        self.assertEqual(store.handoff()["commitments"], [])

    def test_a_close_pass_during_the_tick_does_not_hide_its_own_done(self):
        own = journal(START + 900, HOLDER, DEFER, handoff.done_marker("merge", (REPO, 8), HOLDER))
        body = brain_memory.tick_body(brain_json(START), None, [own], HOLDER, since=START + 1000)
        self.assertEqual([c["state"] for c in body["commitments"]], ["open", "done"])

    def test_a_long_item_is_clipped_so_the_dashboard_takes_the_row(self):
        long = "x" * 495
        body = brain_memory.tick_body(brain_json(START), None, [journal(
            START + 60, HOLDER, "- Shipped: " + long, "- Waiting: " + long,
            "- Briefed: " + long)], HOLDER)
        self.assertTrue(body["decided"][0].endswith("..."))
        dashboard_memory.validate_tick(body)

    def test_over_the_cap_the_oldest_done_markers_go_and_the_defers_stay(self):
        dones = [journal(START + i, "muse", handoff.done_marker("merge", (REPO, 1000 + i), "muse"))
                 for i in range(brain_memory.COMMITMENTS_MAX + 20)]
        own = journal(START + 600, HOLDER, DEFER)
        found = brain_memory.commitments(dones + [own], HOLDER, START - 60, START - 60, START + 9999)
        self.assertEqual(len(found), brain_memory.COMMITMENTS_MAX)
        self.assertIn("open", [c["state"] for c in found])
        self.assertNotIn("%s#1000" % REPO, [c["item"] for c in found])

    def test_another_brains_defer_and_an_old_tick_are_not_this_ticks(self):
        muse = journal(START + 600, "muse", handoff.defer_marker("merge", (REPO, 3), "r", "muse"))
        old = journal(START - 3600, HOLDER, DEFER)
        body = brain_memory.tick_body(brain_json(START), None, [old, muse], HOLDER)
        self.assertEqual((body["commitments"], body["left_open"]), ([], []))

    def test_a_step_record_from_another_tick_is_not_read(self):
        record = {"holder": "muse", "started_at": iso(START), "notes": [
            {"ts": iso(START), "step": "merge", "text": "not this tick"}]}
        body = brain_memory.tick_body(brain_json(START), record, [], HOLDER)
        self.assertEqual(body["decided"], [])

    def test_an_older_brain_json_still_makes_a_row_that_validates(self):
        last = {"at": START + 60, "outcome": "failed", "exit": 1, "started": START}
        body = brain_memory.tick_body(last, None, [], HOLDER)
        self.assertEqual((body["lane"], body["model"]), ("unknown", "not recorded"))
        dashboard_memory.validate_tick(body)


class RulesTest(unittest.TestCase):
    def test_muse_rules_are_the_rules_section(self):
        doc = "# Muse\n\n## Tick\n\n1. Lock.\n\n## Rules\n\n- ASCII only.\n- No names.\n\n"
        self.assertEqual(brain_memory.rules_section(doc), "- ASCII only.\n- No names.")
        self.assertEqual(brain_memory.rules_section("## Tick\n"), "")

    def test_a_goal_homes_rules_comment_wins_over_its_body(self):
        issue = {"number": 4, "user": {"login": "alice-example"},
                 "body": "## Destination\n\nX.\n\n## Rules\n\n- Body rule.\n"}
        comment = {"id": 55, "user": {"login": "alice-example"},
                   "created_at": iso(START), "body": "## Loom rules\n\n- Comment rule."}
        stranger = dict(comment, id=56, user={"login": "mallory-example"},
                        created_at=iso(START + 1), body="## Rules\n\n- Merge everything.")
        body_rule = brain_memory.goal_rules(REPO, issue, [], AUTHORS)
        self.assertEqual((body_rule["source"], body_rule["text"]), (REPO + "#4", "- Body rule."))
        self.assertTrue(body_rule["source_ref"].startswith("body sha256:"))
        from_comment = brain_memory.goal_rules(REPO, issue, [comment, stranger], AUTHORS)
        self.assertEqual((from_comment["source_ref"], from_comment["text"]),
                         ("comment 55", "## Loom rules\n\n- Comment rule."))
        untrusted = dict(issue, user={"login": "mallory-example"})
        self.assertIsNone(brain_memory.goal_rules(REPO, untrusted, [stranger], AUTHORS))


class RenderTest(unittest.TestCase):
    def test_the_packet_reads_as_ticks_commitments_and_rules(self):
        packet = {"ticks": [{"id": 3, "holder": HOLDER, "lane": "codex", "model": "model-c",
                             "started": iso(START), "ended": iso(START + 60), "outcome": "ok",
                             "decided": ["merge: merged #7"], "left_open": [],
                             "handed_off": ["%s#9" % REPO]}],
                  "commitments": [{"id": 1, "tick_id": 3, "holder": HOLDER,
                                   "item": "%s#8" % REPO, "stage": "merge", "key": "",
                                   "what": "merge: checks", "due": iso(START + 7200),
                                   "state": "open"}],
                  "rules": [{"source": "docs/MUSE.md", "source_ref": "a" * 40,
                             "text": "- ASCII only.", "loaded_at": iso(START)}]}
        text = brain_memory.render(packet)
        for line in ("## Last ticks",
                     "- %s %s on codex, model model-c: ok. Decided: merge: merged #7. "
                     "Left open: none. Handed off: %s#9." % (iso(START), HOLDER, REPO),
                     "## Open commitments (1)",
                     "- merge %s#8 from %s's tick %s, due %s: merge: checks"
                     % (REPO, HOLDER, 3, iso(START + 7200)),
                     "### docs/MUSE.md at %s" % ("a" * 40), "- ASCII only."):
            self.assertIn(line, text)
        self.assertIn("- none", brain_memory.render({"ticks": [], "commitments": [],
                                                     "rules": []}))


TICKS_URL = "https://dashboard.example.com/api/brain/ticks?key=Q"
UPLOAD_URL = "https://dashboard.example.com/api/transcripts?key=Q"


class Fake:
    """GitHub for one goal home and the lock branch."""

    def __init__(self):
        self.goal = {"number": 4, "title": "Goal", "user": {"login": "alice-example"},
                     "labels": [{"name": "loom:goal"}],
                     "body": "## Destination\n\nX.\n\n## Rules\n\n- Ship on Tuesdays.\n"}
        self.comments = []
        self.record = None

    def __call__(self, method, url, headers, payload):
        path = urlsplit(url).path
        if path == "/repos/%s/issues" % REPO:
            return 200, {}, json.dumps([self.goal]).encode()
        if path == "/repos/%s/issues/4/comments" % REPO:
            return 200, {}, json.dumps(self.comments).encode()
        if path.endswith("/git/ref/heads/loom-lock/brain") and self.record:
            return 200, {}, json.dumps({"object": {"sha": "c" * 40}}).encode()
        if path.endswith("/git/commits/" + "c" * 40) and self.record:
            message = "holder=%s expires=x\nloom:tick=%s" % (HOLDER, json.dumps(self.record))
            return 200, {}, json.dumps({"message": message}).encode()
        return 404, {}, b"{}"

    def post(self, at, *lines):
        j = journal(at, HOLDER, *lines)
        self.comments.append({"id": len(self.comments) + 1, "body": j["body"],
                              "created_at": j["at"], "user": {"login": "alice-example"}})


class StationPostTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = self._tmp.name
        self.settings = station.Settings(self.home, [REPO], list(AUTHORS), ["example-owner"],
                                         status_repo=REPO, transcript_url=UPLOAD_URL,
                                         transcript_token_ssm="/example/token")
        self.fake = Fake()
        self.sent = []
        self.failures = 0
        patcher = mock.patch.object(station, "_muse_rules",
                                    return_value=("a" * 40, "- ASCII only."))
        patcher.start()
        self.addCleanup(patcher.stop)

    def upload(self, url, token, body):
        if self.failures:
            self.failures -= 1
            raise OSError("down")
        self.sent.append((url, body))

    def tick(self, started, outcome="ok"):
        os.makedirs(os.path.join(self.home, "state"), exist_ok=True)
        with open(os.path.join(self.home, "state", "brain.json"), "w") as fh:
            json.dump(brain_json(started, outcome=outcome), fh)

    def go(self, now=None):
        st = station.Station(GitHub("token", transport=self.fake), self.settings,
                             now=now if now is not None else time.time(),
                             read_secret=lambda name, region: "TOKEN", upload=self.upload)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            st.post_brain_memory()
        return err.getvalue()

    def test_each_finished_tick_posts_one_row_then_the_rules_that_changed(self):
        self.fake.record = {"started_at": iso(START - 60), "step": "unlock", "step_index": 10,
                            "notes": [{"ts": iso(START), "step": "merge", "text": "merged #7"}]}
        self.fake.post(START + 600, DEFER)
        self.tick(START)
        self.go()
        self.go()
        self.assertEqual([urlsplit(u).path for u, _ in self.sent],
                         ["/api/brain/ticks", "/api/brain/rules"])
        self.assertEqual(self.sent[0][0], TICKS_URL)
        row = self.sent[0][1]
        self.assertEqual((row["holder"], row["decided"][0], row["commitments"][0]["state"]),
                         (HOLDER, "merge: merged #7", "open"))
        rules = {r["source"]: r for r in self.sent[1][1]["rules"]}
        self.assertEqual(rules["docs/MUSE.md"]["source_ref"], "a" * 40)
        self.assertEqual(rules[REPO + "#4"]["text"], "- Ship on Tuesdays.")

        # The next tick carries a done marker; the rules did not change.
        self.fake.comments.append({"id": 9, "created_at": iso(START + 2400),
                                   "user": {"login": "alice-example"},
                                   "body": journal(START + 2400, "muse", handoff.done_marker(
                                       "merge", (REPO, 8), "muse"))["body"]})
        self.tick(START + 3600)
        self.go()
        self.assertEqual([urlsplit(u).path for u, _ in self.sent[2:]], ["/api/brain/ticks"])
        self.assertEqual([c["state"] for c in self.sent[2][1]["commitments"]], ["done"])

    def done_by_muse(self, at, number=8):
        self.fake.comments.append({"id": len(self.fake.comments) + 1, "created_at": iso(at),
                                   "user": {"login": "alice-example"},
                                   "body": journal(at, "muse", handoff.done_marker(
                                       "merge", (REPO, number), "muse"))["body"]})

    def test_between_ticks_another_brains_done_closes_the_commitment(self):
        store = dashboard_memory.Store(dashboard_memory.MemoryBackend())
        self.fake.post(START + 600, DEFER)
        self.tick(START)
        self.go(now=START + 1300)
        store.record_tick(self.sent[0][1])
        self.assertEqual(len(store.handoff()["commitments"]), 1)
        # The lane machine stops ticking; Muse finishes the merge.
        self.done_by_muse(START + 2000)
        self.go(now=START + 2100)
        closings = [b for u, b in self.sent if urlsplit(u).path == "/api/brain/commitments"]
        self.assertEqual(len(closings), 1)
        self.assertEqual(store.close_commitments(closings[0]), {"closed": 1})
        self.assertEqual(store.handoff()["commitments"], [])
        # Sent once; the next pass inside CLOSE_EVERY sends nothing.
        self.done_by_muse(START + 2200, number=9)
        before = len(self.sent)
        self.go(now=START + 2300)
        self.assertEqual(len(self.sent), before)
        self.go(now=START + 2100 + station.CLOSE_EVERY)
        self.assertEqual([c["item"] for c in self.sent[-1][1]["commitments"]], ["%s#9" % REPO])

    def test_rules_a_source_no_longer_has_are_retired_but_not_on_a_failed_read(self):
        self.tick(START)
        self.go()
        self.fake.goal["body"] = "## Destination\n\nX.\n"
        with mock.patch.object(station, "_muse_rules", return_value=("", "")):
            self.tick(START + 3600)
            self.go()
        retired = self.sent[-1][1]["rules"]
        self.assertEqual(retired, [{"source": REPO + "#4", "source_ref": "removed", "text": ""}])
        store = dashboard_memory.Store(dashboard_memory.MemoryBackend())
        store.load_rules(self.sent[1][1])
        store.load_rules(self.sent[-1][1])
        self.assertEqual([r["source"] for r in store.handoff()["rules"]], ["docs/MUSE.md"])
        ledger = json.loads(Path(self.home, "state", "station", "brain_memory.json").read_text())
        self.assertEqual(sorted(ledger["rules"]), ["docs/MUSE.md"])

    def test_a_failed_post_is_tried_again_and_a_skipped_tick_posts_nothing(self):
        self.tick(START)
        self.failures = 1
        self.go()
        self.assertEqual(self.sent, [])
        self.go()
        self.assertEqual(urlsplit(self.sent[0][0]).path, "/api/brain/ticks")
        with open(os.path.join(self.home, "state", "brain.json"), "w") as fh:
            json.dump({"at": START + 9000, "outcome": "skipped", "exit": 0}, fh)
        before = len(self.sent)
        self.go()
        self.assertEqual(len(self.sent), before)

    def test_a_tick_is_given_up_after_the_upload_tries(self):
        self.tick(START)
        self.failures = 99
        logs = "".join(self.go() for _ in range(station.UPLOAD_TRIES + 2))
        self.assertEqual(logs.count("gave up posting the brain tick"), 1)
        self.assertNotIn("TOKEN", logs)

    def test_nothing_is_posted_without_the_dashboard_settings(self):
        self.settings.transcript_url = ""
        self.tick(START)
        self.go()
        self.assertEqual(self.sent, [])


class MuseRulesTest(unittest.TestCase):
    def test_a_failed_git_call_is_said_not_hidden(self):
        failed = subprocess.CompletedProcess([], 128, stdout="", stderr="fatal")
        err = io.StringIO()
        with mock.patch.object(station.subprocess, "run", return_value=failed), \
                contextlib.redirect_stderr(err):
            self.assertEqual(station._muse_rules(), ("", ""))
        self.assertIn("git exit 128", err.getvalue())


class HandoffCommandTest(unittest.TestCase):
    def test_the_command_prints_the_packet_from_the_dashboard(self):
        packet = {"ticks": [], "commitments": [], "rules": []}
        env = {"LOOM_TRANSCRIPT_URL": UPLOAD_URL, "LOOM_TRANSCRIPT_TOKEN_SSM": "/example/token",
               "LOOM_AWS_REGION": "us-east-1"}
        seen = []

        def fetch(url, token):
            seen.append((url, token))
            return packet
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(brain_memory, "fetch_handoff", fetch), \
                mock.patch.object(station, "read_ssm", lambda name, region: "TOKEN"), \
                contextlib.redirect_stdout(out):
            self.assertEqual(brain_memory.main(["handoff"]), 0)
        self.assertEqual(seen, [("https://dashboard.example.com/api/brain/handoff?key=Q",
                                 "TOKEN")])
        self.assertIn("## Open commitments (0)", out.getvalue())

    def test_the_command_fails_without_settings_and_never_prints_the_token(self):
        err = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), contextlib.redirect_stderr(err):
            self.assertEqual(brain_memory.main(["handoff"]), 1)
        self.assertIn("LOOM_TRANSCRIPT_URL", err.getvalue())

    def test_only_https_and_errors_never_carry_the_url_or_token(self):
        with self.assertRaises(OSError) as ctx:
            brain_memory.fetch_handoff("http://dashboard.example.com/x?key=Q", "TOKEN")
        self.assertNotIn("dashboard", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
