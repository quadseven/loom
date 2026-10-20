"""Snapshot behavior with a GitHub boundary that controls request latency."""
import contextlib
import os
import json
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from bus.github import GitHub, GitHubError
from dashboard.github import TicketStore


REPOS = ["example-owner/one", "example-owner/two", "example-owner/three"]


class SlowGitHub:
    def __init__(self, failing=None, delay=0.04):
        self.rate = {}
        self.failing = failing
        self.delay = delay
        self.active = 0
        self.max_active = 0
        self.request_time = 0.0
        self.lock = threading.Lock()

    def paginate(self, path):
        started = time.monotonic()
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(self.delay)
            repo = next(repo for repo in REPOS if "/" + repo + "/" in path)
            if self.failing and repo in self.failing and "labels=loom%3Aticket" in path:
                raise GitHubError("ticket read failed")
            if "labels=loom%3Aticket&state=all" in path:
                return [{"number": REPOS.index(repo) + 1, "title": repo,
                         "labels": [{"name": "loom:ticket"}]}]
            if "/pulls?" in path:
                return [{"number": REPOS.index(repo) + 11}]
            if "/issues/comments?" in path:
                return [{"id": REPOS.index(repo) + 21, "user": {"login": "bot"}}]
            if "/pulls/comments?" in path:
                return [{"id": REPOS.index(repo) + 31}]
            return []
        finally:
            with self.lock:
                self.active -= 1
                self.request_time += time.monotonic() - started

    def paginate_parallel(self, path, workers=8):
        return self.paginate(path)


class HomeGitHub(SlowGitHub):
    """SlowGitHub plus a status home with a goal journal and a lanes comment,
    and a lock branch with no tick record, counting every request."""

    def __init__(self):
        super().__init__(delay=0)
        self.requests = 0

    def paginate(self, path):
        self.requests += 1
        if "labels=loom%3Agoal" in path:
            return [{"number": 5, "title": "Goal"}]
        if path.endswith("/issues/5/comments"):
            return [{"id": 41, "body": "<!-- loom:journal v1 -->\n- Friction: slow reads",
                     "user": {"login": "bot"}, "created_at": "2030-01-01T00:00:00Z"},
                    {"id": 42, "body": "<!-- loom:lanes v1 -->\n| codex | idle |",
                     "user": {"login": "bot"}, "created_at": "2030-01-01T00:00:00Z"}]
        return super().paginate(path)

    def rest(self, method, path, **kwargs):
        self.requests += 1

        class Missing:
            status, data, headers = 404, {}, {}
        return Missing()


class SnapshotTest(unittest.TestCase):
    def test_a_pass_below_the_floor_keeps_every_view_source_of_the_last_read(self):
        # Emptying them showed false zeros while the budget stayed low:
        # Improving 0/0/0/0, Lanes "no loom:lanes comment", Brain "no goal
        # journal found", Results "Lane pull requests merged 0" (loom#779).
        gh = HomeGitHub()
        with patch.dict(os.environ, {"LOOM_STATUS_REPO": REPOS[0]}, clear=True):
            store = TicketStore(REPOS, gh=gh, budget_floor=500)
            store._waits_refreshed_at = float("inf")
            store.refresh()
            first = store.response()
            store._last_remaining, store._reset_at = 10, int(time.time()) + 3600
            before = gh.requests
            store.refresh()
            second = store.response()
        self.assertEqual(gh.requests, before)
        for key in ("improving", "lanes", "pull_requests", "issue_comments", "review_comments",
                    "brain_step"):
            self.assertTrue(first[key], key)
            self.assertEqual(second[key], first[key], key)
        self.assertTrue(first["improving"]["journals"])
        self.assertEqual(second["footer_runs"], first["footer_runs"])
        self.assertEqual(second["brain_step_error"], "")
        self.assertEqual(second["hot_spots_error"], "")
        self.assertEqual(set(first["source_read_at"]),
                         {"improving", "lanes", "pull_requests", "brain_step", "comments"})
        self.assertEqual(second["source_read_at"], first["source_read_at"])
        self.assertEqual(first["sources_carried"], [])
        self.assertEqual(set(second["sources_carried"]), set(first["source_read_at"]))
        self.assertIn("request budget ran low", second["dashboard_errors"][0])

    def test_a_first_pass_below_the_floor_records_no_read_time(self):
        gh = HomeGitHub()
        with patch.dict(os.environ, {"LOOM_STATUS_REPO": REPOS[0]}, clear=True):
            store = TicketStore(REPOS, gh=gh, budget_floor=500)
            store._waits_refreshed_at = float("inf")
            store._last_remaining, store._reset_at = 10, int(time.time()) + 3600
            store.refresh()
            payload = store.response()
        self.assertEqual(payload["source_read_at"], {})
        self.assertEqual(payload["sources_carried"], [])
        self.assertEqual(payload["pull_requests"], [])
        self.assertIn("request budget ran low", payload["hot_spots_error"])

    def test_pull_request_pages_overlap_within_a_repo_and_keep_page_order(self):
        repo = REPOS[0]
        active = 0
        max_active = 0
        first_started = None
        last_finished = None
        request_time = 0.0
        lock = threading.Lock()

        def transport(method, url, headers, body):
            nonlocal active, max_active, first_started, last_finished, request_time
            path = urlsplit(url).path
            if path != "/repos/%s/pulls" % repo:
                return 200, {}, b"[]"
            page = int(parse_qs(urlsplit(url).query).get("page", ["1"])[0])
            started = time.monotonic()
            with lock:
                first_started = first_started or started
                active += 1
                max_active = max(max_active, active)
            try:
                time.sleep(0.08)
                links = "" if page == 8 else (
                    '<https://api.github.com/repos/%s/pulls?state=all&per_page=100&page=%d>; rel="next", '
                    '<https://api.github.com/repos/%s/pulls?state=all&per_page=100&page=8>; rel="last"'
                    % (repo, page + 1, repo))
                return 200, {"Link": links}, json.dumps([{"number": page}]).encode()
            finally:
                with lock:
                    active -= 1
                    last_finished = time.monotonic()
                    request_time += last_finished - started

        gh = GitHub("", transport=transport)
        with patch.dict(os.environ, {}, clear=True):
            store = TicketStore([repo], gh=gh)
            store.refresh()
        self.assertGreater(max_active, 1)
        self.assertLess(last_finished - first_started, request_time * 0.7)
        self.assertEqual([row["number"] for row in store.response()["pull_requests"]],
                         [1, 2, 3, 4, 5, 6, 7, 8])

    def test_repo_reads_overlap_and_keep_input_order(self):
        gh = SlowGitHub()
        with patch.dict(os.environ, {}, clear=True):
            store = TicketStore(REPOS, gh=gh)
            started = time.monotonic()
            store.refresh()
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, gh.request_time * 0.7)
        self.assertGreater(gh.max_active, 1)
        self.assertEqual(list(store.data), REPOS)
        self.assertEqual([row["repo"] for row in store.response()["issue_comments"]], REPOS)

    def test_one_repo_error_keeps_other_repo_data(self):
        with patch.dict(os.environ, {}, clear=True):
            store = TicketStore(REPOS, gh=SlowGitHub(failing=REPOS[1]))
            store.refresh()
        self.assertEqual(store.errors, {REPOS[1]: "ticket read failed"})
        self.assertEqual([store.data[repo][0]["number"] for repo in (REPOS[0], REPOS[2])], [1, 3])
        self.assertEqual([row["number"] for row in store.response()["pull_requests"]], [11, 12, 13])

    def test_pull_request_page_error_stays_with_its_repo(self):
        repos = REPOS[:2]

        def transport(method, url, headers, body):
            path = urlsplit(url).path
            if "/pulls" not in path or "/pulls/comments" in path:
                return 200, {}, b"[]"
            repo = next(repo for repo in repos if "/repos/" + repo + "/pulls" == path)
            page = int(parse_qs(urlsplit(url).query).get("page", ["1"])[0])
            if repo == repos[1] and page == 2:
                raise GitHubError("pull page failed", status=403)
            links = ("<https://api.github.com/repos/%s/pulls?state=all&per_page=100&page=2>; rel=\"last\""
                     % repo) if page == 1 else ""
            return 200, {"Link": links}, json.dumps([{"repo": repo, "number": page}]).encode()

        with patch.dict(os.environ, {}, clear=True):
            store = TicketStore(repos, gh=GitHub("", transport=transport))
            store.refresh()
        self.assertEqual(store.response()["pull_requests"],
                         [{"repo": repos[0], "number": 1}, {"repo": repos[0], "number": 2}])
        self.assertIn("%s pull requests: pull page failed" % repos[1],
                      store.response()["dashboard_errors"])

    def test_empty_snapshot_is_not_ready(self):
        with patch.dict(os.environ, {}, clear=True):
            store = TicketStore([], gh=SlowGitHub())
            self.assertFalse(store.ready)
            store.refresh()
        self.assertFalse(store.ready)

    def test_unreadable_snapshot_is_not_ready(self):
        with patch.dict(os.environ, {}, clear=True):
            store = TicketStore(REPOS, gh=SlowGitHub(failing=REPOS))
            store.refresh()
        self.assertFalse(store.ready)
        self.assertEqual(list(store.errors), REPOS)

    def test_first_ready_snapshot_logs_its_wall_time_once(self):
        with patch.dict(os.environ, {}, clear=True):
            store = TicketStore(REPOS, gh=SlowGitHub(delay=0.001))
            with self.assertLogs("dashboard.github", level="INFO") as logs:
                store.refresh()
                store.refresh()
        self.assertEqual(len(logs.output), 1)
        self.assertRegex(logs.output[0], r"first dashboard snapshot ready in [0-9.]+s")

    def test_open_issue_reads_overlap_and_an_error_stays_with_its_repo(self):
        # The /free page's open issues are read in the first pass too, one
        # repo after another: 12.4 s of a cold 320 s first snapshot (loom#433).
        class OpenIssues(SlowGitHub):
            def paginate(self, path):
                found = super().paginate(path)
                repo = next(repo for repo in REPOS if "/" + repo + "/" in path)
                if repo == REPOS[1]:
                    raise GitHubError("open issues failed")
                return [{"number": REPOS.index(repo) + 1, "title": repo},
                        {"number": 99, "pull_request": {}}] if "issues?state=open" in path else found

        gh = OpenIssues(delay=0.05)
        with patch.dict(os.environ, {}, clear=True):
            store = TicketStore(REPOS, gh=gh)
            store.open_issues[REPOS[1]] = [{"number": 7}]
            started = time.monotonic()
            store._read_open_issues(10000.0)
            elapsed = time.monotonic() - started
        self.assertGreater(gh.max_active, 1)
        self.assertLess(elapsed, gh.request_time * 0.7)
        self.assertEqual({repo: [row["number"] for row in rows] for repo, rows in store.open_issues.items()},
                         {REPOS[0]: [1], REPOS[1]: [7], REPOS[2]: [3]})
        self.assertEqual(list(store.open_issues), REPOS)
        self.assertEqual(store.open_issues_error, "%s: open issues failed" % REPOS[1])
        self.assertEqual(store.open_issues_read_at, 10000.0)

    def test_comment_feed_pages_overlap_within_a_repo_and_keep_page_order(self):
        # One repo's two-week comment feed was read one page after another:
        # 32 s of a cold first snapshot (loom#433).
        repo = REPOS[0]
        clock = Overlap()

        def transport(method, url, headers, body):
            path = urlsplit(url).path
            if path != "/repos/%s/issues/comments" % repo:
                return 200, {}, b"[]"
            page = int(parse_qs(urlsplit(url).query).get("page", ["1"])[0])
            with clock.request():
                links = "" if page == 6 else (
                    '<https://api.github.com%s?per_page=100&page=%d>; rel="next", '
                    '<https://api.github.com%s?per_page=100&page=6>; rel="last"' % (path, page + 1, path))
                # The last page repeats one comment, as a page shift does.
                found = [{"id": page, "user": {"login": "bot"}}] + (
                    [{"id": 5, "user": {"login": "bot"}}] if page == 6 else [])
                return 200, {"Link": links}, json.dumps(found).encode()

        with patch.dict(os.environ, {}, clear=True):
            store = TicketStore([repo], gh=GitHub("", transport=transport))
            data = {}
            store._read_hot_spot_comments(data)
        clock.assert_overlapped(self)
        self.assertEqual([row["id"] for row in data["issue_comments"]], [1, 2, 3, 4, 5, 6])
        self.assertEqual(data["hot_spots_error"], "")

    def test_claim_check_reads_overlap_and_an_error_stays_with_its_pull_request(self):
        clock = Overlap()
        repos = ["example-owner/r%d" % n for n in range(6)]

        class Checks:
            rate = {}

            def rest(self, method, path, body=None):
                with clock.request():
                    if "/r3/" in path:
                        raise GitHubError("checks failed")
                    return type("R", (), {"data": {"check_runs": [
                        {"name": path, "status": "completed", "conclusion": "success"}]}})()

        items = [{"linked": [{"repo": repo, "sha": "%040d" % n, "state": "open"}]}
                 for n, repo in enumerate(repos)]
        with patch.dict(os.environ, {}, clear=True):
            store = TicketStore(repos, gh=Checks())
            errors = []
            out = store._read_claim_checks(items, errors)
        clock.assert_overlapped(self)
        self.assertEqual(list(out), ["%s@%040d" % (repo, n) for n, repo in enumerate(repos) if n != 3])
        self.assertEqual(errors, ["example-owner/r3 checks: checks failed"])

    def test_ask_reads_overlap_keep_their_item_and_stop_at_the_cap(self):
        clock = Overlap()

        class Comments:
            rate = {}

            def rest(self, method, path, body=None):
                with clock.request():
                    number = int(path.split("/issues/")[1].split("/")[0])
                    if number == 2:
                        raise GitHubError("comments failed")
                    return type("R", (), {"data": [{"body": "Ask: do thing %d" % number,
                                                    "html_url": "u%d" % number}]})()

        items = [{"repo": REPOS[0], "number": n, "comments": 1, "labels": [], "updated_at": "t"}
                 for n in range(1, 8)]
        previous = [{"repo": REPOS[0], "number": 7, "updated_at": "old", "ask": "kept", "ask_url": "k"}]
        with patch.dict(os.environ, {}, clear=True), patch("dashboard.github.OPERATOR_ASK_READS", 6):
            store = TicketStore(REPOS, gh=Comments())
            errors = []
            store._read_asks(items, previous, errors)
        clock.assert_overlapped(self)
        self.assertEqual(clock.count, 6)
        self.assertEqual([(item["ask"], item["ask_url"]) for item in items],
                         [("Ask: do thing 1", "u1"), (None, ""), ("Ask: do thing 3", "u3"),
                          ("Ask: do thing 4", "u4"), ("Ask: do thing 5", "u5"),
                          ("Ask: do thing 6", "u6"), ("kept", "k")])
        self.assertEqual(errors, ["%s#2 ask: comments failed" % REPOS[0]])

    def test_goal_home_reads_overlap_and_follow_parents(self):
        clock = Overlap()
        repo = REPOS[0]

        class Goals:
            rate = {}

            def rest(self, method, path, body=None):
                with clock.request():
                    number = int(path.rsplit("/", 1)[1])
                    # Goals 1-6 roll up into goal 7, which is labelled a goal.
                    body = "Parent goal: #7" if number < 7 else ""
                    return type("R", (), {"data": {"number": number, "title": "Goal %d" % number,
                                                   "body": body, "labels": [{"name": "loom:goal"}]}})()

        rows = [{"goal": "%s#%d" % (repo, n)} for n in range(1, 7)]
        with patch.dict(os.environ, {}, clear=True):
            store = TicketStore([repo], gh=Goals())
            titles, parents = store._goal_info(rows)
        clock.assert_overlapped(self)
        self.assertEqual(clock.count, 7)
        self.assertEqual(titles, {"%s#%d" % (repo, n): "Goal %d" % n for n in range(1, 8)})
        self.assertEqual(parents, dict({"%s#%d" % (repo, n): "%s#7" % repo for n in range(1, 7)},
                                       **{"%s#7" % repo: None}))


class Overlap:
    """Counts requests that sleep, and how many ran at once."""

    def __init__(self, delay=0.05):
        self.delay = delay
        self.lock = threading.Lock()
        self.active = self.max_active = self.count = 0
        self.first_started = self.last_finished = None
        self.request_time = 0.0

    @contextlib.contextmanager
    def request(self):
        started = time.monotonic()
        with self.lock:
            self.first_started = self.first_started or started
            self.count += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(self.delay)
            yield
        finally:
            with self.lock:
                self.active -= 1
                self.last_finished = time.monotonic()
                self.request_time += self.last_finished - started

    def assert_overlapped(self, test):
        test.assertGreater(self.max_active, 1)
        test.assertLess(self.last_finished - self.first_started, self.request_time * 0.7)
