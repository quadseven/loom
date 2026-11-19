"""bus/runners.py: a lost cluster runner (loom#3268). When a cluster run
ends with no report and a job annotation says the self-hosted runner lost
communication with the server, the parked reason names the loss and how
long the run lasted, instead of the bare no-report text. Two such losses
in a row pause cluster dispatch for an hour, noted once in the station
log. A fake GitHub answers; no report artifact is ever offered."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bus import runners  # noqa: E402
from bus.github import Response  # noqa: E402
from tests.test_runners import APP, BRIEF, REPO, TICKET, FakeGitHub  # noqa: E402

LOST = "The self-hosted runner lost communication with the server."
SLOT = "opencode-go-2"


class LostGitHub(FakeGitHub):
    """FakeGitHub plus the run's jobs and their annotations."""

    def __init__(self, notes=None):
        super().__init__()
        self.notes = list(notes if notes is not None else
                          [{"annotation_level": "failure", "title": "Run the lane once",
                            "message": LOST}])
        self.jobs = {}          # run id -> [job]

    def rest(self, method, path, body=None, etag=None, ok=()):
        if method == "GET" and "/actions/runs/" in path and path.endswith("/jobs?per_page=100"):
            run = int(path.split("/actions/runs/")[1].split("/")[0])
            jobs = self.jobs.get(run, [{"id": 55, "check_run_url": "https://x/check-runs/55"}])
            return Response(200, {"jobs": jobs})
        if method == "GET" and "/check-runs/" in path and "/annotations" in path:
            return Response(200, list(self.notes))
        return super().rest(method, path, body, etag, ok)

    def paginate(self, path, **kwargs):
        if "/annotations" in path:
            return list(self.notes)
        raise AssertionError("unexpected paginate %s" % path)


class RunnerLostTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home = self.root / "loom"
        self.inbox = self.home / "briefs" / SLOT
        self.inbox.mkdir(parents=True)
        self.gh = LostGitHub()
        self.said = []
        self.now = 1_800_000_000.0
        self.env = {"LOOM_RUNNER_SLOTS": SLOT, "LOOM_OPENCODE_GO_KEY_SSM": "/x/oc",
                    "LOOM_OPENCODE_GO_2_MODEL": "opencode-go/beam-free",
                    "LOOM_GITHUB_APP_KEY_SSM": "/station/app", "LOOM_LANES_GITHUB_APP_ID": "42",
                    "LOOM_REPO_OWNERS": "owner", "HOME": "/nowhere"}

    def tearDown(self):
        self._tmp.cleanup()

    def runner(self, now=None):
        return runners.Runners(self.gh, str(self.home), REPO, [SLOT], self.env,
                               lambda repo: True, self.said.append,
                               actor=APP, now=self.now if now is None else now)

    def drop_brief(self, name=TICKET):
        (self.inbox / (name + ".md")).write_text(BRIEF)

    def failure_line(self, name=TICKET):
        return (self.home / "logs" / SLOT / (name + ".log")).read_text().splitlines()[-1]

    def collect_lost(self, name, run, minutes=27, now=None):
        """Feed _collect a finished run with no report artifact, as the
        brief requires: the entry is finished, its run has the lost
        annotation, and no artifact is offered for it."""
        self.drop_brief(name)
        at = (self.now if now is None else now) - minutes * 60
        entry = {"slot": SLOT, "ticket": name, "at": at, "state": "finished",
                 "conclusion": "failure", "run": run, "run_id": "ab" * 16}
        r = self.runner(now=now)
        ledger = r.load()
        r._collect(ledger, entry)
        return r, ledger

    def test_a_lost_runner_is_named_in_the_parked_reason(self):
        self.collect_lost(TICKET, run=901)
        self.assertTrue((self.inbox / "failed" / (TICKET + ".md")).exists())
        line = self.failure_line()
        self.assertIn("runner lost communication", line)
        self.assertIn("the cluster runner was lost after 27 minutes "
                      "(runner lost communication)", line)
        self.assertNotIn("with no report", line)

    def test_two_losses_in_a_row_pause_cluster_dispatch(self):
        r, ledger = self.collect_lost("owner.app-7", run=901)
        r2, ledger = self.collect_lost("owner.app-8", run=902)
        self.assertEqual(self.said.count("cluster dispatch paused: 2 runner losses in a row"), 1)
        # Dispatch is paused: a fresh brief waits in the inbox, unsent.
        self.drop_brief("owner.app-9")
        r2.tend()
        self.assertEqual(self.gh.posts("/dispatches"), [])
        self.assertTrue((self.inbox / "owner.app-9.md").exists())
        self.assertFalse((self.inbox / "failed" / "owner.app-9.md").exists())

    def test_a_no_report_run_without_the_annotation_keeps_the_old_reason(self):
        self.gh.notes = [{"annotation_level": "failure", "title": "x", "message": "boom"}]
        self.collect_lost(TICKET, run=903)
        self.assertIn("the runner run ended failure with no report", self.failure_line())
        self.assertNotIn("cluster dispatch paused", " ".join(self.said))


if __name__ == "__main__":
    unittest.main()
