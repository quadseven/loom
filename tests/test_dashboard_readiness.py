"""A fresh dashboard replica is ready once its tickets are read, not after the
whole first pass, and /readyz says why it is not ready when it is not."""
import os
import tempfile
import threading
import unittest
from unittest import mock

from bus.github import GitHub
from dashboard import github as dashboard_github
from dashboard import server
from dashboard.github import TicketStore


def _transport(method, url, headers, body):
    return 200, {"X-RateLimit-Remaining": "4000", "X-RateLimit-Limit": "5000",
                 "X-RateLimit-Reset": "2000000000"}, b"[]"


class ReadinessTest(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.TemporaryDirectory()
        self.addCleanup(self.cache.cleanup)
        env = mock.patch.dict(os.environ, {"LOOM_DASHBOARD_REFRESH_SECONDS": "1",
                                           "LOOM_TRANSCRIPTS_MEMORY": "1"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("LOOM_BACKLOG_SOURCE", None)
        gh = GitHub("", api="https://api.github.invalid", cache_dir=self.cache.name, transport=_transport)
        self.store = TicketStore(["example-owner/sample"], gh=gh, budget_floor=5)
        self.addCleanup(self.store.stop_background)

    def test_ready_once_tickets_are_read_while_a_later_stage_is_still_running(self):
        in_stage, release = threading.Event(), threading.Event()

        def slow_waits(snapshot, now):
            in_stage.set()
            release.wait(15)

        with mock.patch.object(self.store, "_refresh_waits", side_effect=slow_waits):
            self.store.start_background()
            self.addCleanup(release.set)
            self.assertTrue(in_stage.wait(15), "the pass never reached the waits stage")
            self.assertTrue(self.store.ready)
            self.assertEqual(self.store.not_ready_reason(), "")
            release.set()

    def test_not_ready_reason_names_the_stage_and_the_read_error(self):
        self.assertIn("has not started", self.store.not_ready_reason())
        self.store._pass_begun = self.store._stage_since = 0.0
        self.store._stage = "reading tickets"
        self.store._snapshot = dict(self.store._snapshot, errors={"example-owner/sample": "403: rate limited"})
        reason = self.store.not_ready_reason()
        self.assertIn("reading tickets", reason)
        self.assertIn("403: rate limited", reason)

    def test_readyz_logs_the_reason_once_per_interval(self):
        self.store._pass_begun = self.store._stage_since = 1.0
        server._not_ready_logged[0] = 0.0
        with self.assertLogs("dashboard.server", level="WARNING") as logs:
            self.assertTrue(server._log_not_ready(self.store, now=100.0))
            self.assertFalse(server._log_not_ready(self.store, now=100.0 + server.NOT_READY_LOG_SECONDS - 1))
            self.assertTrue(server._log_not_ready(self.store, now=100.0 + server.NOT_READY_LOG_SECONDS))
        self.assertEqual(len(logs.output), 2)
        self.assertIn("readyz answered 503", logs.output[0])
        self.assertIn("first refresh pass", logs.output[0])


if __name__ == "__main__":
    unittest.main()
