"""Tests for brain tick body caps: the station's tick_body must produce a body
that the dashboard's validate_tick accepts."""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bus import brain_memory, handoff  # noqa: E402
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


class TickBodyCapsTest(unittest.TestCase):
    """The tick body must fit the dashboard's caps."""

    def test_body_with_excess_lines_and_length_passes_validate(self):
        """60 decided lines, a 900-char left_open line, 250 commitments -> validate_tick accepts."""
        # Build 60 decided lines (over LIST_MAX=50)
        decided_lines = ["- Shipped: item-%d" % i for i in range(60)]
        # One very long left_open line (over LINE_MAX=500)
        long_line = "x" * 900
        # 250 commitments (over COMMITMENTS_MAX=200)
        commits = []
        for i in range(250):
            mark = handoff.defer_marker("merge", (REPO, 1000 + i), "k%d" % i, HOLDER,
                                        note="defer note %d" % i)
            commits.append(mark)
        j = journal(START + 600, HOLDER, *decided_lines,
                    "- Waiting: " + long_line,
                    *["- Briefed: %s#%d" % (REPO, 2000 + i) for i in range(10)])
        # Add the defer markers to the journal body
        for c in commits:
            j["body"] += "\n" + c

        last = brain_json(START)
        record = {"holder": HOLDER, "started_at": iso(START - 60), "step": "unlock",
                  "notes": []}
        found = [j]
        body = brain_memory.tick_body(last, record, found, HOLDER)

        # The body should be clipped to caps
        self.assertLessEqual(len(body["decided"]), dashboard_memory.LIST_MAX)
        self.assertLessEqual(len(body["left_open"]), dashboard_memory.LIST_MAX)
        self.assertLessEqual(len(body["handed_off"]), dashboard_memory.LIST_MAX)
        self.assertLessEqual(len(body["commitments"]), dashboard_memory.COMMITMENTS_MAX)
        for lst in (body["decided"], body["left_open"], body["handed_off"]):
            for line in lst:
                self.assertLessEqual(len(line), dashboard_memory.LINE_MAX)
        self.assertLessEqual(len(body["holder"]), dashboard_memory.NAME_MAX)
        self.assertLessEqual(len(body["lane"]), dashboard_memory.NAME_MAX)
        self.assertLessEqual(len(body["model"]), dashboard_memory.NAME_MAX)
        self.assertNotIn("\n", body["holder"])
        self.assertNotIn("\n", body["lane"])
        self.assertNotIn("\n", body["model"])
        self.assertIn(body["outcome"], dashboard_memory.OUTCOMES)

        # validate_tick must not raise
        validated = dashboard_memory.validate_tick(body)
        self.assertEqual(validated["holder"], body["holder"])
        self.assertEqual(len(validated["decided"]), len(body["decided"]))
        self.assertEqual(len(validated["left_open"]), len(body["left_open"]))
        self.assertEqual(len(validated["handed_off"]), len(body["handed_off"]))
        self.assertEqual(len(validated["commitments"]), len(body["commitments"]))

    def test_refused_upload_log_line_carries_error_text(self):
        """When UploadRefused is raised, the log includes the dashboard's error text."""
        import io
        import contextlib
        import tempfile
        import os
        import json
        from unittest import mock
        from urllib.parse import urlsplit

        from bus import station  # noqa: E402
        from bus.github import GitHub  # noqa: E402

        UPLOAD_URL = "https://dashboard.example.com/api/transcripts?key=Q"
        TICKS_URL = "https://dashboard.example.com/api/brain/ticks?key=Q"

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

        with tempfile.TemporaryDirectory() as tmp:
            home = tmp
            settings = station.Settings(home, [REPO], list(AUTHORS), ["example-owner"],
                                        status_repo=REPO, transcript_url=UPLOAD_URL,
                                        transcript_token_ssm="/example/token")
            fake = Fake()
            sent = []

            def upload(url, token, body):
                # Simulate dashboard refusing with error text
                raise station.UploadRefused(400, "invalid commitments")

            with mock.patch.object(station, "_muse_rules", return_value=("", "")):
                st = station.Station(GitHub("token", transport=fake), settings,
                                     now=time.time(),
                                     read_secret=lambda name, region: "TOKEN",
                                     upload=upload)
                # Write a finished brain tick
                os.makedirs(os.path.join(home, "state"), exist_ok=True)
                with open(os.path.join(home, "state", "brain.json"), "w") as fh:
                    json.dump(brain_json(START), fh)
                # Add a journal with a defer marker
                fake.comments.append({"id": 1, "created_at": iso(START + 600),
                                      "user": {"login": "alice-example"},
                                      "body": journal(START + 600, HOLDER,
                                          handoff.defer_marker("merge", (REPO, 8), "abc123", HOLDER))["body"]})

                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    st.post_brain_memory()
                log = err.getvalue()
                # The log line should contain the status code AND the error text
                self.assertIn("refused the brain tick", log)
                self.assertIn("400", log)
                self.assertIn("invalid commitments", log)


if __name__ == "__main__":
    unittest.main()