"""The /improving page serves the build the background refresh already
made (loom#2248): a page request does no GitHub request and no journal-to-
ticket join of its own, and before the first background read the page says
what is missing instead of showing false zeros."""
import os
import time
import unittest
from unittest.mock import patch

from dashboard import views
from dashboard.github import TicketStore
from dashboard.server import _dispatch_get

REPO = "example-owner/one"
OTHER_TICKET = "%s#%d" % ("example-owner/one", 8)
JOURNAL = {"id": 41, "user": {"login": "bot"},
           "created_at": "2030-01-02T00:00:00Z",
           "html_url": "https://github.com/example-owner/one/issues/30#issuecomment-41",
           "body": "<!-- loom:journal v1 -->\n"
                   "- Friction: slow dashboard reads in " + OTHER_TICKET}
METRICS = {"id": 42, "user": {"login": "bot"},
           "created_at": "2030-01-02T00:00:00Z",
           "body": "<!-- loom:metrics v1 -->\n2030-01-02:\n"
                   "1. PASS Flow: median 2.3h; 0 PRs >6h\n"
                   "2. PASS Quality: 0 failures"}


class CountingGitHub:
    """One status home with a goal journal and a metrics comment, and one
    ticket; every paginate and rest counts as a GitHub request."""

    def __init__(self):
        self.rate = {}
        self.requests = 0

    def paginate(self, path):
        self.requests += 1
        if "labels=loom%3Agoal" in path:
            return [{"number": 30, "title": "Goal home"}]
        if path.endswith("/issues/30/comments"):
            return [dict(JOURNAL), dict(METRICS)]
        if "labels=loom%3Aticket" in path:
            return [{"number": 8, "title": "Fix slow reads", "state": "open",
                     "labels": [{"name": "loom:ticket"}],
                     "body": "## Why\nThe dashboard read is slow.\n",
                     "updated_at": "2030-01-02T00:00:00Z"}]
        return []

    def paginate_parallel(self, path, workers=8):
        return self.paginate(path)

    def rest(self, method, path, **kwargs):
        self.requests += 1

        class Missing:
            status, data, headers = 404, {}, {}
        return Missing()


class _Handler:
    def send_page(self, body, code=200):
        self.result = (code, body)


def _get(path, state):
    handler = _Handler()
    handler.path = path
    _dispatch_get(handler, state)
    return handler.result


class ImprovingPageTest(unittest.TestCase):
    def _refreshed_store(self):
        gh = CountingGitHub()
        with patch.dict(os.environ, {"LOOM_STATUS_REPO": REPO}, clear=True):
            store = TicketStore([REPO], gh=gh, budget_floor=500)
            store._waits_refreshed_at = float("inf")
            store.refresh()
        return gh, store

    def test_the_background_refresh_builds_the_improving_data(self):
        _, store = self._refreshed_store()
        built = store.response().get("improving_built")
        self.assertIsNotNone(built)
        self.assertEqual([row["friction"] for row in built["records"]],
                         ["slow dashboard reads in " + OTHER_TICKET])
        self.assertTrue(built["metrics_available"])

    def test_a_page_request_makes_no_github_request_and_no_join(self):
        gh, store = self._refreshed_store()
        before = gh.requests
        builds = []
        real_build = views.build_improvements

        def counting_build(*args, **kwargs):
            builds.append(1)
            return real_build(*args, **kwargs)

        with patch.object(views, "build_improvements", counting_build):
            code, body = _get("/improving", store)
        self.assertEqual(code, 200)
        self.assertEqual(gh.requests, before)
        self.assertEqual(builds, [])
        self.assertIn("slow dashboard reads in", body)
        self.assertIn("factory-card", body)

    def test_a_pass_below_the_floor_keeps_the_built_improving_data(self):
        _, store = self._refreshed_store()
        first = store.response()["improving_built"]
        store._last_remaining, store._reset_at = 10, int(time.time()) + 3600
        with patch.dict(os.environ, {"LOOM_STATUS_REPO": REPO}, clear=True):
            store.refresh()
        self.assertEqual(store.response()["improving_built"], first)

    def test_before_any_background_read_the_page_says_what_is_missing(self):
        class State:
            stale = False

            def response(self):
                return {"tickets": {}, "stale": False,
                        "improving": {"journals": [], "metrics_comments": []},
                        "source_read_at": {}, "next_refresh_at": None}

        code, body = _get("/improving", State())
        self.assertEqual(code, 200)
        self.assertIn("Improving data has not been read yet", body)
        self.assertIn("tick journals", body)
        self.assertIn("daily metrics", body)
        self.assertIn("refresh once GitHub can be reached", body)
        self.assertNotIn("No friction lines in the journals yet.", body)


if __name__ == "__main__":
    unittest.main()
