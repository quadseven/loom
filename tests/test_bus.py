from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import __main__ as cli  # noqa: E402
from bus import journal, labels, lock  # noqa: E402
from bus.github import GitHub, GitHubError  # noqa: E402

REPO = "example-owner/example-repo"
NOW = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def reply(status=200, body=None, headers=None):
    raw = b"" if body is None else json.dumps(body).encode()
    return status, headers or {}, raw


class Scripted:
    """Fake transport: records calls, returns queued replies in order."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        return self.replies.pop(0)


class FakeGit:
    """A tiny stateful git API: main, commits and refs."""

    def __init__(self):
        self.refs = {"heads/main": "c0"}
        self.commits = {"c0": {"message": "init", "tree": {"sha": "t0"}}}
        self.calls = []
        self.n = 0

    def seed_lock(self, holder, expires):
        self.commits["held"] = {"message": "holder=%s expires=%s" % (holder, expires),
                                "tree": {"sha": "t0"}}
        self.refs["heads/loom-lock/brain"] = "held"

    def __call__(self, method, url, headers, body):
        path = url.split("api.github.com/repos/%s/" % REPO)[1]
        self.calls.append((method, path))
        data = json.loads(body) if body else None
        if method == "GET" and path.startswith("git/ref/"):
            ref = path[len("git/ref/"):]
            if ref not in self.refs:
                return reply(404, {"message": "Not Found"})
            return reply(200, {"object": {"sha": self.refs[ref]}})
        if method == "GET" and path.startswith("git/commits/"):
            return reply(200, self.commits[path.split("/")[-1]])
        if method == "POST" and path == "git/commits":
            assert len(data["parents"]) <= 1
            self.n += 1
            sha = "n%d" % self.n
            self.commits[sha] = {"message": data["message"], "tree": {"sha": data["tree"]}}
            return reply(201, {"sha": sha})
        if method == "POST" and path == "git/refs":
            ref = data["ref"][len("refs/"):]
            if ref in self.refs:
                return reply(422, {"message": "Reference already exists"})
            self.refs[ref] = data["sha"]
            return reply(201, {})
        if method == "DELETE" and path.startswith("git/refs/"):
            self.refs.pop(path[len("git/refs/"):], None)
            return reply(204)
        # Like GitHub: a ref is read at git/ref/<ref> but updated at
        # git/refs/<ref>; a PATCH to the read path answers 404.
        if method == "PATCH" and path.startswith("git/ref/"):
            return reply(404, {"message": "Not Found"})
        if method == "PATCH" and path.startswith("git/refs/"):
            ref = path[len("git/refs/"):]
            self.refs[ref] = data["sha"]
            return reply(200, {})
        raise AssertionError("unexpected call %s %s" % (method, path))

    def lock_message(self):
        return self.commits[self.refs["heads/loom-lock/brain"]]["message"]


def stamp(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class ClientTests(unittest.TestCase):
    def test_headers_and_counting(self):
        t = Scripted(reply(200, {"a": 1}, {"ETag": '"x"'}), reply(201, {}))
        gh = GitHub("tok", transport=t)
        r = gh.rest("GET", "/user")
        gh.rest("POST", "/thing", {"k": "v"})
        self.assertEqual(gh.requests, 2)
        self.assertEqual(r.data, {"a": 1})
        self.assertEqual(r.etag, '"x"')
        method, url, h, body = t.calls[0]
        self.assertEqual(url, "https://api.github.com/user")
        self.assertEqual(h["Authorization"], "Bearer tok")
        self.assertEqual(h["Accept"], "application/vnd.github+json")
        self.assertEqual(h["X-GitHub-Api-Version"], "2022-11-28")
        self.assertEqual(h["User-Agent"], "loom-bus")
        self.assertEqual(json.loads(t.calls[1][3]), {"k": "v"})

    def test_etag_and_304(self):
        t = Scripted(reply(304))
        gh = GitHub("tok", transport=t)
        r = gh.rest("GET", "/x", etag='"abc"')
        self.assertEqual(r.status, 304)
        self.assertIsNone(r.data)
        self.assertEqual(t.calls[0][2]["If-None-Match"], '"abc"')
        self.assertEqual(gh.requests, 1)

    def test_a_repeated_get_sends_the_kept_etag_and_a_304_is_the_kept_answer(self):
        path = "/repos/%s/issues?labels=loom:pr&state=open&per_page=100" % REPO
        t = Scripted(reply(200, [{"number": 1}], {"ETag": '"v1"'}),
                     reply(304, None, {"ETag": '"v1"'}))
        with tempfile.TemporaryDirectory() as d:
            gh = GitHub("tok", transport=t, cache_dir=d)
            first = gh.rest("GET", path)
            again = gh.rest("GET", path)
        self.assertNotIn("If-None-Match", t.calls[0][2])
        self.assertEqual(t.calls[1][2]["If-None-Match"], '"v1"')
        self.assertEqual(first.data, [{"number": 1}])
        self.assertEqual((again.status, again.data), (304, [{"number": 1}]))
        # Both are requests; only the first counted against the budget.
        self.assertEqual((gh.requests, gh.not_modified), (2, 1))
        self.assertEqual(gh.kinds, {"issues list": 1})

    def test_a_changed_list_replaces_the_kept_answer(self):
        t = Scripted(reply(200, [1], {"ETag": '"v1"'}), reply(200, [1, 2], {"ETag": '"v2"'}),
                     reply(304))
        with tempfile.TemporaryDirectory() as d:
            gh = GitHub("tok", transport=t, cache_dir=d)
            gh.rest("GET", "/x")
            gh.rest("GET", "/x")
            self.assertEqual(gh.rest("GET", "/x").data, [1, 2])
        self.assertEqual([c[2].get("If-None-Match") for c in t.calls], [None, '"v1"', '"v2"'])

    def test_pages_are_kept_with_their_next_links(self):
        nxt = '<https://api.github.com/p?page=2>; rel="next"'
        t = Scripted(reply(200, [1, 2], {"ETag": '"a"', "Link": nxt}),
                     reply(200, [3], {"ETag": '"b"'}), reply(304), reply(304))
        with tempfile.TemporaryDirectory() as d:
            gh = GitHub("tok", transport=t, cache_dir=d)
            self.assertEqual(gh.paginate("/p"), [1, 2, 3])
            self.assertEqual(gh.paginate("/p"), [1, 2, 3])
        self.assertEqual(t.calls[3][1], "https://api.github.com/p?page=2")
        self.assertEqual(t.calls[3][2]["If-None-Match"], '"b"')
        self.assertEqual(gh.not_modified, 2)

    def test_a_kept_full_page_still_reads_the_page_added_after_it(self):
        # 2026-10-14: an issue's comments grew past 100 while page 1 stayed
        # the same, so page 1 kept answering 304 with its old (empty) link
        # and the station never read comment 101 on, posting the daily
        # metrics line again every pass.
        t = Scripted(reply(200, [1, 2], {"ETag": '"a"'}),   # first read: full, no next
                     reply(304),                              # later: page 1 unchanged
                     reply(200, [3], {"ETag": '"b"'}),        # the page added since
                     reply(304), reply(304))                  # next read: both kept
        with tempfile.TemporaryDirectory() as d:
            gh = GitHub("tok", transport=t, cache_dir=d)
            self.assertEqual(gh.paginate("/p?per_page=2"), [1, 2])
            self.assertEqual(gh.paginate("/p?per_page=2"), [1, 2, 3])
            self.assertEqual(t.calls[2][1], "https://api.github.com/p?per_page=2&page=2")
            self.assertEqual(gh.paginate("/p?per_page=2"), [1, 2, 3])
        # Page 2 is not full, so the third read stops there: five requests.
        self.assertEqual(len(t.calls), 5)

    def test_a_read_survives_a_dropped_connection_and_a_write_is_never_repeated(self):
        # 2026-10-14: GitHub closed connections in the middle of a status
        # burst (IncompleteRead) and one dropped read failed the whole tick.
        import http.client
        drops = []

        def dropping(method, url, headers, body):
            drops.append(method)
            if len(drops) == 1:
                raise GitHubError("request failed: %r" % http.client.IncompleteRead(b""), 0, "")
            if len(drops) == 2:
                return 503, {}, b""
            return 200, {}, b'[{"number": 1}]'

        slept = []
        gh = GitHub("tok", transport=dropping, sleep=slept.append)
        self.assertEqual(gh.rest("GET", "/x").data, [{"number": 1}])
        self.assertEqual(len(drops), 3)
        self.assertEqual(slept, [2.0, 6.0])
        self.assertEqual(gh.requests, 3)
        drops.clear()
        with self.assertRaises(GitHubError):
            gh.rest("POST", "/x", {"a": 1})
        self.assertEqual(drops, ["POST"])  # a write is tried once

    def test_incomplete_read_from_the_wire_is_a_github_error(self):
        import http.client
        from unittest import mock
        from bus import github as gh_mod
        with mock.patch.object(gh_mod._OPENER, "open",
                               side_effect=http.client.IncompleteRead(b"x", 10)):
            with self.assertRaises(GitHubError) as caught:
                gh_mod.urllib_transport("GET", "https://api.github.com/x", {}, None)
        self.assertEqual(caught.exception.status, 0)

    def test_without_a_cache_no_etag_is_sent(self):
        t = Scripted(reply(200, [1], {"ETag": '"v1"'}), reply(200, [1], {"ETag": '"v1"'}))
        gh = GitHub("tok", transport=t)
        gh.rest("GET", "/x")
        gh.rest("GET", "/x")
        self.assertNotIn("If-None-Match", t.calls[1][2])

    def test_requests_are_counted_by_kind(self):
        t = Scripted(*[reply(200, [])] * 7)
        gh = GitHub("tok", transport=t)
        for method, path in (("GET", "/repos/%s/issues?state=open" % REPO),
                             ("GET", "/repos/%s/pulls?state=open" % REPO),
                             ("GET", "/repos/%s/issues/3/comments" % REPO),
                             ("GET", "/repos/%s/commits/abc/check-runs?per_page=100" % REPO),
                             ("GET", "/repos/%s/commits/abc/status" % REPO),
                             ("POST", "/graphql"),
                             ("POST", "/repos/%s/issues/3/comments" % REPO)):
            gh.rest(method, path, {"query": "q"} if path == "/graphql" else None)
        self.assertEqual(gh.kinds, {"issues list": 1, "pulls list": 1, "comments": 1,
                                    "check runs": 2, "graphql": 1, "writes": 1})

    def test_the_rate_limit_is_read_even_from_a_refusal(self):
        limit = {"X-RateLimit-Remaining": "0", "X-RateLimit-Limit": "5000",
                 "X-RateLimit-Reset": "1900000000", "X-RateLimit-Resource": "core"}
        gh = GitHub("tok", transport=Scripted(reply(403, {"message": "rate limit"}, limit)))
        with self.assertRaises(GitHubError):
            gh.rest("GET", "/x")
        self.assertEqual(gh.rate, {"core": (0, 5000, 1900000000)})

    def test_unused_kept_answers_are_pruned(self):
        t = Scripted(reply(200, [1], {"ETag": '"v1"'}))
        with tempfile.TemporaryDirectory() as d:
            gh = GitHub("tok", transport=t, cache_dir=d)
            gh.rest("GET", "/x")
            self.assertEqual(gh.prune_cache(3600), 0)
            self.assertEqual(gh.prune_cache(3600, now=os.stat(
                os.path.join(d, os.listdir(d)[0])).st_mtime + 3601), 1)
            self.assertEqual(os.listdir(d), [])

    def test_error_and_ok_statuses(self):
        gh = GitHub("tok", transport=Scripted(reply(404, {"message": "no"}), reply(422, {})))
        with self.assertRaises(GitHubError) as cm:
            gh.rest("GET", "/x")
        self.assertEqual(cm.exception.status, 404)
        self.assertIn("no", cm.exception.body)
        self.assertEqual(gh.rest("POST", "/x", ok=(422,)).status, 422)

    def test_paginate_follows_next(self):
        nxt = '<https://api.github.com/p?page=2>; rel="next", <https://api.github.com/p?page=2>; rel="last"'
        t = Scripted(reply(200, [1, 2], {"Link": nxt}), reply(200, [3]))
        gh = GitHub("tok", transport=t)
        self.assertEqual(gh.paginate("/p"), [1, 2, 3])
        self.assertEqual(t.calls[1][1], "https://api.github.com/p?page=2")
        self.assertEqual(gh.requests, 2)

    def test_graphql(self):
        t = Scripted(reply(200, {"data": {"a": 1}}), reply(200, {"errors": [{"message": "bad"}]}))
        gh = GitHub("tok", transport=t)
        self.assertEqual(gh.graphql("query { a }", {"v": 1}), {"a": 1})
        self.assertEqual(t.calls[0][1], "https://api.github.com/graphql")
        self.assertEqual(json.loads(t.calls[0][3])["variables"], {"v": 1})
        with self.assertRaises(GitHubError):
            gh.graphql("query { a }", {})


class TokenScopeTests(unittest.TestCase):
    def test_the_token_never_goes_to_a_url_outside_the_api(self):
        fake = Scripted(reply(200, []))
        gh = GitHub("secret-token", transport=fake)
        for url in ("https://example.com/repos/x", "http://api.github.com/repos/x",
                    "https://api.github.com.example.com/x"):
            with self.assertRaises(GitHubError):
                gh.rest("GET", url)
        self.assertEqual(fake.calls, [])

    def test_pagination_follows_next_links_on_the_api_host(self):
        fake = Scripted(
            reply(200, [1], {"Link": '<https://api.github.com/repositories/1/labels?page=2>; rel="next"'}),
            reply(200, [2]))
        self.assertEqual(GitHub("t", transport=fake).paginate("/repos/a/b/labels"), [1, 2])


class TransportTests(unittest.TestCase):
    def test_the_default_transport_opens_https_only(self):
        from bus.github import urllib_transport
        for url in ("file:///etc/hosts", "http://api.github.com/x", "ftp://x/y"):
            with self.assertRaises(GitHubError):
                urllib_transport("GET", url, {}, None)

    def test_the_default_transport_never_follows_a_redirect(self):
        from bus.github import _NoRedirect
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://elsewhere.example/x"))


class LabelTests(unittest.TestCase):
    def test_the_lane_status_label_is_in_the_set(self):
        self.assertIn("loom:lanes", labels.LABELS)


    def test_set_shape(self):
        for lane in labels.LANES:
            self.assertIn("lane:" + lane, labels.LABELS)
        self.assertIn("loom:urgent", labels.LABELS)
        for name, v in labels.LABELS.items():
            self.assertRegex(v["color"], r"^[0-9a-f]{6}$")
            self.assertTrue(v["description"].isascii(), name)
            self.assertLessEqual(len(v["description"]), 100)

    def test_create_update_and_prefix_rule(self):
        want = labels.LABELS
        existing = [
            {"name": "loom:goal", "color": "000000", "description": ""},        # update
            {"name": "loom:ticket", "color": want["loom:ticket"]["color"],
             "description": want["loom:ticket"]["description"]},                # same
            {"name": "needs-operator", "color": "000000", "description": "x"},  # foreign: untouched
            {"name": "bug", "color": "ffffff", "description": None},            # foreign
        ]
        replies = [reply(200, existing)] + [reply(201, {})] * (len(labels.LABELS) + 5)
        t = Scripted(*replies)
        gh = GitHub("tok", transport=t)
        changed = labels.ensure_labels(gh, REPO)
        self.assertIn("loom:goal", changed)
        self.assertNotIn("loom:ticket", changed)
        self.assertNotIn("needs-operator", changed)
        self.assertIn("loom:queued", changed)
        patches = [c for c in t.calls if c[0] == "PATCH"]
        self.assertEqual(len(patches), 1)
        self.assertTrue(patches[0][1].endswith("/labels/loom%3Agoal"))
        for method, url, _, _body in t.calls[1:]:
            self.assertIn(method, ("POST", "PATCH"))
            self.assertNotIn("/bug", url)
            self.assertNotIn("needs-operator", url)
        created = [json.loads(c[3])["name"] for c in t.calls if c[0] == "POST"]
        self.assertEqual(len(changed), len(patches) + len(created))

    def test_foreign_label_with_other_prefix_never_patched(self):
        existing = [{"name": "needs-operator", "color": "111111", "description": "mine"}]
        t = Scripted(reply(200, existing), *[reply(201, {})] * (len(labels.LABELS) + 5))
        labels.ensure_labels(GitHub("tok", transport=t), REPO)
        self.assertFalse([c for c in t.calls if c[0] == "PATCH"])


class LockTests(unittest.TestCase):
    def test_acquire_free(self):
        g = FakeGit()
        gh = GitHub("tok", api="https://api.github.com", transport=g)
        self.assertTrue(lock.acquire(gh, REPO, "tick-1", 15, NOW))
        self.assertEqual(g.lock_message(),
                         "holder=tick-1 expires=" + stamp(NOW + timedelta(minutes=15)))

    def test_held_by_other(self):
        g = FakeGit()
        g.seed_lock("other", stamp(NOW + timedelta(minutes=5)))
        gh = GitHub("tok", transport=g)
        self.assertFalse(lock.acquire(gh, REPO, "tick-1", 15, NOW))
        self.assertIn("holder=other", g.lock_message())
        self.assertNotIn(("DELETE", "git/refs/heads/loom-lock/brain"), g.calls)

    def test_expired_takeover(self):
        g = FakeGit()
        g.seed_lock("other", stamp(NOW - timedelta(minutes=1)))
        gh = GitHub("tok", transport=g)
        self.assertTrue(lock.acquire(gh, REPO, "tick-1", 15, NOW))
        self.assertIn("holder=tick-1", g.lock_message())
        self.assertIn(("DELETE", "git/refs/heads/loom-lock/brain"), g.calls)

    def test_release_by_holder_and_wrong_holder(self):
        g = FakeGit()
        gh = GitHub("tok", transport=g)
        lock.acquire(gh, REPO, "tick-1", 15, NOW)
        self.assertFalse(lock.release(gh, REPO, "tick-2"))
        self.assertIn("heads/loom-lock/brain", g.refs)
        self.assertTrue(lock.release(gh, REPO, "tick-1"))
        self.assertIn("heads/loom-lock/brain", g.refs)
        self.assertFalse(lock.release(gh, REPO, "tick-1"))

    def test_release_with_unreadable_expiry_returns_false(self):
        g = FakeGit()
        g.seed_lock("tick-1", "invalid")
        gh = GitHub("tok", transport=g)
        self.assertFalse(lock.release(gh, REPO, "tick-1"))
        self.assertIn("heads/loom-lock/brain", g.refs)

    def test_holder_validation(self):
        gh = GitHub("tok", transport=FakeGit())
        for bad in ("", "a b", "a=b", "a\nb", "x/y"):
            with self.assertRaises(ValueError):
                lock.acquire(gh, REPO, bad)
            with self.assertRaises(ValueError):
                lock.release(gh, REPO, bad)
        self.assertEqual(gh.requests, 0)


class JournalTests(unittest.TestCase):
    def test_render_journal(self):
        text = journal.render_journal("2030-01-01T12:00:00Z", "tick-1", ["a"], [],
                                      ["b", "c"], [], 42, 3)
        self.assertEqual(text.splitlines(), [
            "<!-- loom:journal v1 -->",
            "Tick 2030-01-01T12:00:00Z, tick-1",
            "- Shipped: a",
            "- Briefed: none",
            "- Waiting: b, c",
            "- Asks: none",
            "- Cost: 42 requests, 3 minutes",
        ])

    def test_render_journal_with_a_measure(self):
        lines = journal.render_journal(NOW, "tick-1", [], [], [], [], 1, 1,
                                       measure="p95 2s").splitlines()
        self.assertEqual(lines[2], "- Measure: p95 2s")

    def test_render_report(self):
        lines = journal.render_report(NOW, "tick-1", "m", [], "f", "none", "n").splitlines()
        self.assertEqual(lines[0], journal.REPORT_MARKER)
        self.assertEqual(lines[1], "Report 2030-01-01T12:00:00Z, tick-1")
        self.assertEqual(len(lines), 7)
        self.assertTrue(lines[3].startswith("- Shipped: none"))

    def test_post_marker_check(self):
        # The writer reads its login and the day's comments, then posts.
        t = Scripted(reply(200, {"login": "me"}), reply(200, []), reply(201, {}))
        gh = GitHub("tok", transport=t)
        with self.assertRaises(ValueError):
            journal.post(gh, REPO, 7, "hello")
        self.assertEqual(gh.requests, 0)
        journal.post(gh, REPO, 7, journal.JOURNAL_MARKER + "\nTick 2030-01-01T12:00:00Z, muse\nx")
        self.assertEqual(t.calls[2][0], "POST")
        self.assertTrue(t.calls[2][1].endswith("/repos/%s/issues/7/comments" % REPO))

    def test_a_journal_without_its_tick_line_is_refused_unless_a_holder_adds_it(self):
        # No brain's journal carried a Tick line on 2026-10-16, so each read its
        # own last tick as a peer's and stayed to resume items for hours.
        gh = GitHub("tok", transport=Scripted())
        with self.assertRaisesRegex(ValueError, "Tick <UTC time>, <holder>"):
            journal.post(gh, REPO, 7, journal.JOURNAL_MARKER + "\nLane machine tick 12:00Z.")
        with self.assertRaisesRegex(ValueError, "names muse, not lane-machine"):
            journal.post(gh, REPO, 7, journal.JOURNAL_MARKER + "\nTick 2030-01-01T12:00:00Z, muse\nx",
                         "lane-machine")
        self.assertEqual(gh.requests, 0)
        text = journal.with_tick(journal.JOURNAL_MARKER + "\n\nLane machine tick.", "lane-machine", NOW)
        self.assertEqual(text.splitlines()[:3], [journal.JOURNAL_MARKER,
                                                 "Tick 2030-01-01T12:00:00Z, lane-machine",
                                                 "Lane machine tick."])

    def test_a_stamped_journal_is_the_holders_own_not_a_peers(self):
        from bus import handoff
        body = journal.with_tick(journal.JOURNAL_MARKER + "\nbody", "lane-machine", NOW)
        found = [{"body": body, "at": "2030-01-01T12:00:00Z"}]
        self.assertEqual(handoff.peer_journals(found, "lane-machine", NOW), [])
        self.assertEqual(len(handoff.peer_journals(found, "muse", NOW)), 1)

    def test_post_accepts_a_reply_to_an_operator_note(self):
        t = Scripted(reply(201, {}))
        gh = GitHub("tok", transport=t)
        for bad in ("<!-- loom:reply v1 discord= -->\nx", "<!-- loom:reply v1 -->\nx"):
            with self.assertRaises(ValueError):
                journal.post(gh, REPO, 7, bad)
        journal.post(gh, REPO, 7, "<!-- loom:reply v1 discord=123456789 -->\nDone.")
        self.assertEqual(gh.requests, 1)

    # The station's standup goes out as the Loom App; on 2026-10-18 a brain
    # that did not trust the App's login posted its own report 8 minutes later
    # on the goal home (and 7 to 70 minutes later on twelve others).
    APP = {"login": "example-loom[bot]", "type": "Bot"}

    @staticmethod
    def report_comment(user, minutes_before, body=None):
        at = NOW - timedelta(minutes=minutes_before)
        return {"user": user, "created_at": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "body": body or journal.REPORT_MARKER + "\nNothing moved since the last report."}

    def test_a_report_8_minutes_after_the_apps_report_is_refused_and_not_posted(self):
        t = Scripted(reply(200, [self.report_comment(self.APP, 8)]))
        gh = GitHub("tok", transport=t)
        with self.assertRaises(journal.ReportTooSoon) as held:
            journal.post(gh, REPO, 7, journal.REPORT_MARKER + "\nReport", trusted=["example-operator"],
                         now=NOW)
        self.assertEqual([c[0] for c in t.calls], ["GET"])
        self.assertIn("/repos/%s/issues/7/comments?since=2030-01-01T10:30:00Z" % REPO, t.calls[0][1])
        self.assertIn("example-loom[bot] posted a report on this goal home at 2030-01-01T11:52:00Z, "
                      "8 minutes ago", str(held.exception))
        self.assertIn("next may go out at 2030-01-01T13:22:00Z", str(held.exception))

    def test_a_trusted_login_or_any_author_without_a_trust_list_holds_a_report(self):
        operator = {"login": "Example-Operator", "type": "User"}
        stranger = {"login": "stranger", "type": "User"}
        for comments, trusted, refused in (
                ([self.report_comment(operator, 89)], ["example-operator"], True),
                ([self.report_comment(stranger, 30)], ["example-operator"], False),
                ([self.report_comment(stranger, 30)], [], True),
                # A journal, or a marker quoted inside another comment, is no report.
                ([self.report_comment(self.APP, 5, journal.JOURNAL_MARKER + "\nTick x, y"),
                  self.report_comment(self.APP, 5, "quoting " + journal.REPORT_MARKER)], [], False)):
            with self.subTest(comments=comments, trusted=trusted):
                t = Scripted(reply(200, comments), reply(201, {}))
                gh = GitHub("tok", transport=t)
                if refused:
                    with self.assertRaises(journal.ReportTooSoon):
                        journal.post(gh, REPO, 7, journal.REPORT_MARKER + "\nR", trusted=trusted, now=NOW)
                else:
                    journal.post(gh, REPO, 7, journal.REPORT_MARKER + "\nR", trusted=trusted, now=NOW)
                self.assertEqual([c[0] for c in t.calls], ["GET"] if refused else ["GET", "POST"])

    def test_a_report_91_minutes_after_the_last_one_posts(self):
        t = Scripted(reply(200, [self.report_comment(self.APP, 91)]), reply(201, {}))
        gh = GitHub("tok", transport=t)
        journal.post(gh, REPO, 7, journal.REPORT_MARKER + "\nReport", trusted=["example-operator"], now=NOW)
        self.assertEqual([c[0] for c in t.calls], ["GET", "POST"])
        self.assertEqual(json.loads(t.calls[1][3])["body"], journal.REPORT_MARKER + "\nReport")

    def test_a_journal_is_never_held_by_the_report_gap(self):
        # A journal reads only its login and its own day's comments (to edit
        # the day's journal in place), never the report gap's window.
        t = Scripted(reply(200, {"login": "me"}), reply(200, []), reply(201, {}))
        gh = GitHub("tok", transport=t)
        journal.post(gh, REPO, 7, journal.JOURNAL_MARKER + "\nTick 2030-01-01T12:00:00Z, muse\nx", now=NOW)
        self.assertEqual([c[0] for c in t.calls], ["GET", "GET", "POST"])
        self.assertIn("since=2030-01-01T00:00:00Z", t.calls[1][1])


class CliTests(unittest.TestCase):
    def run_cli(self, argv, transport):
        gh = GitHub("tok", transport=transport)
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = cli.main(argv, gh=gh)
        return code, err.getvalue()

    def test_lock_exit_codes(self):
        code, err = self.run_cli(["lock", REPO, "--holder", "h1"], FakeGit())
        self.assertEqual(code, 0)
        self.assertIn("requests: ", err)
        g = FakeGit()
        g.seed_lock("other", stamp(datetime.now(timezone.utc) + timedelta(hours=1)))
        self.assertEqual(self.run_cli(["lock", REPO, "--holder", "h1"], g)[0], 1)

    def test_unlock_exit_codes(self):
        g = FakeGit()
        g.seed_lock("h1", stamp(datetime.now(timezone.utc) + timedelta(hours=1)))
        self.assertEqual(self.run_cli(["unlock", REPO, "--holder", "h2"], g)[0], 1)
        self.assertEqual(self.run_cli(["unlock", REPO, "--holder", "h1"], g)[0], 0)

    def test_journal_command(self):
        t = Scripted(reply(201, {}))
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "j.md")
            with open(path, "w") as fh:
                fh.write(journal.JOURNAL_MARKER + "\nTick x, y\n")
            code, err = self.run_cli(["journal", REPO + "#5", "--file", path], t)
        self.assertEqual(code, 0)
        self.assertIn("requests: 1", err)

    def test_journal_command_holder_adds_the_tick_line(self):
        t = Scripted(reply(200, {"login": "me"}), reply(200, []), reply(201, {}))
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "j.md")
            with open(path, "w") as fh:
                fh.write(journal.JOURNAL_MARKER + "\nMoved: nothing.\n")
            code, _ = self.run_cli(["journal", REPO + "#5", "--file", path,
                                    "--holder", "lane-machine"], t)
        self.assertEqual(code, 0)
        posted = json.loads(t.calls[2][3])["body"].splitlines()
        self.assertRegex(posted[1], r"^Tick \S+Z, lane-machine$")
        self.assertEqual(posted[2], "Moved: nothing.")

    def test_journal_command_says_not_posted_for_a_report_held_by_the_gap(self):
        just = (datetime.now(timezone.utc) - timedelta(minutes=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
        t = Scripted(reply(200, [{"user": {"login": "example-loom[bot]", "type": "Bot"},
                                  "created_at": just, "body": journal.REPORT_MARKER + "\nx"}]))
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "r.md")
            with open(path, "w") as fh:
                fh.write(journal.REPORT_MARKER + "\nReport\n")
            gh = GitHub("tok", transport=t)
            with mock.patch.dict(os.environ, {"LOOM_BRIEF_AUTHORS": "example-operator"}), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = cli.main(["journal", REPO + "#5", "--file", path], gh=gh)
        self.assertEqual(code, 0)
        self.assertEqual([c[0] for c in t.calls], ["GET"])
        self.assertTrue(out.getvalue().startswith("not posted: example-loom[bot] posted a report"))


if __name__ == "__main__":
    unittest.main()
