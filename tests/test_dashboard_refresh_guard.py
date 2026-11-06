"""The dashboard refresh thread outlives a bad GitHub shape, the Floor reads
no network, and the ETag cache is pruned each pass (loom#2581)."""
import json
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
    """Every GitHub read answers an empty list, with budget to spare."""
    return 200, {"X-RateLimit-Remaining": "4000", "X-RateLimit-Limit": "5000",
                 "X-RateLimit-Reset": "2000000000"}, b"[]"


class RefreshGuardTest(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.TemporaryDirectory()
        self.addCleanup(self.cache.cleanup)
        env = mock.patch.dict(os.environ, {"LOOM_DASHBOARD_REFRESH_SECONDS": "1",
                                           "LOOM_TRANSCRIPTS_MEMORY": "1"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("LOOM_BACKLOG_SOURCE", None)
        self.gh = GitHub("", api="https://api.github.invalid", cache_dir=self.cache.name,
                         transport=_transport)
        self.store = TicketStore(["example-owner/sample"], gh=self.gh, budget_floor=5)
        self.addCleanup(self.store.stop_background)

    def test_an_exception_in_one_join_is_logged_and_the_next_pass_runs(self):
        calls = []
        second = threading.Event()

        def bench_pass(store, snapshot):
            calls.append(1)
            if len(calls) == 1:
                raise TypeError("unexpected GitHub shape")
            second.set()

        with mock.patch.object(dashboard_github.station_state, "bench_pass", side_effect=bench_pass), \
                self.assertLogs("dashboard.github", level="ERROR") as logs:
            self.store.start_background()
            self.assertTrue(second.wait(15), "no refresh ran after the failed one")
        self.assertGreaterEqual(len(calls), 2)
        self.assertTrue(any("dashboard refresh failed" in line and "TypeError" in line
                            for line in logs.output), logs.output)

    def test_a_failed_pass_marks_the_snapshot_stale(self):
        with mock.patch.object(dashboard_github.station_state, "bench_pass",
                               side_effect=KeyError("number")), \
                self.assertLogs("dashboard.github", level="ERROR"):
            self.store.start_background()
            for _ in range(150):
                if self.store.stale and "refresh failed" in self.store.response().get("stale_reason", ""):
                    break
                threading.Event().wait(0.1)
        self.assertTrue(self.store.stale)
        self.assertIn("KeyError", self.store.response()["stale_reason"])

    def test_waits_survive_a_type_error(self):
        with mock.patch.object(dashboard_github, "waits_for_repos", side_effect=TypeError("bad row")):
            self.store.refresh()
        self.assertIn("bad row", self.store.waits_error)

    def test_cache_is_pruned_each_refresh(self):
        with mock.patch.object(self.gh, "prune_cache", wraps=self.gh.prune_cache) as prune:
            self.store.refresh()
            self.store.refresh()
        self.assertEqual(prune.call_count, 2)
        self.assertEqual(prune.call_args.args[0], dashboard_github.CACHE_KEEP)

    def test_prune_removes_an_old_cache_file(self):
        old = os.path.join(self.cache.name, "stale-answer")
        with open(old, "w", encoding="utf-8") as handle:
            handle.write("{}")
        os.utime(old, (1, 1))
        self.store.refresh()
        self.assertFalse(os.path.exists(old))


class FloorBacklogTest(unittest.TestCase):
    """Rendering / never opens the backlog source over the network."""

    def setUp(self):
        self.cache = tempfile.TemporaryDirectory()
        self.addCleanup(self.cache.cleanup)
        gh = GitHub("", api="https://api.github.invalid", cache_dir=self.cache.name,
                    transport=_transport)
        self.store = TicketStore(["example-owner/sample"], gh=gh, budget_floor=5)
        env = mock.patch.dict(os.environ, {"LOOM_BACKLOG_SOURCE": "https://example.com/backlog.json",
                                           "LOOM_TRANSCRIPTS_MEMORY": "1"})
        env.start()
        self.addCleanup(env.stop)

    def no_network(self, *args, **kwargs):
        raise AssertionError("a page request read the backlog over the network")

    def render_floor(self):
        with mock.patch.object(server.socket, "getaddrinfo", side_effect=self.no_network), \
                mock.patch.object(server, "build_opener", side_effect=self.no_network):
            return server._floor(self.store)

    def test_floor_renders_without_reading_the_backlog_source(self):
        self.store.refresh()
        body = self.render_floor()
        self.assertIn("<span>backlog</span>", body)

    def test_floor_shows_the_copy_the_refresh_thread_read(self):
        fetched = []

        def fetch():
            fetched.append(1)
            return json.dumps({"tickets": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}).encode()

        self.store.backlog_fetch = fetch
        self.store.refresh()
        self.store.refresh()
        self.assertEqual(len(fetched), 1, "the source is read once per BACKLOG_SECONDS")
        body = self.render_floor()
        self.assertIn("<strong>3</strong><span>backlog</span>", body)


if __name__ == "__main__":
    unittest.main()
