from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
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
            assert data["parents"] == []
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
        replies = [reply(200, existing)] + [reply(201, {})] * 40
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
        t = Scripted(reply(200, existing), *[reply(201, {})] * 40)
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
        self.assertNotIn("heads/loom-lock/brain", g.refs)
        self.assertFalse(lock.release(gh, REPO, "tick-1"))

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
        t = Scripted(reply(201, {}))
        gh = GitHub("tok", transport=t)
        with self.assertRaises(ValueError):
            journal.post(gh, REPO, 7, "hello")
        self.assertEqual(gh.requests, 0)
        journal.post(gh, REPO, 7, journal.JOURNAL_MARKER + "\nx")
        self.assertTrue(t.calls[0][1].endswith("/repos/%s/issues/7/comments" % REPO))

    def test_post_accepts_a_reply_to_an_operator_note(self):
        t = Scripted(reply(201, {}))
        gh = GitHub("tok", transport=t)
        for bad in ("<!-- loom:reply v1 discord= -->\nx", "<!-- loom:reply v1 -->\nx"):
            with self.assertRaises(ValueError):
                journal.post(gh, REPO, 7, bad)
        journal.post(gh, REPO, 7, "<!-- loom:reply v1 discord=123456789 -->\nDone.")
        self.assertEqual(gh.requests, 1)


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


if __name__ == "__main__":
    unittest.main()
