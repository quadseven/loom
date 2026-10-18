"""The dashboard's pull request read costs what changed (#686).

Each page of pulls?state=all embeds the repository, whose pushed_at moves on
every push, so one push changes every page's ETag. The fake below does the
same: any change to any pull request changes every page's ETag.
"""
import hashlib
import json
import os
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from bus.github import GitHub
from dashboard.github import TicketStore


REPO = "example-owner/big"
PAGES = 40
PER_PAGE = 100


def stamp(minute):
    """An ISO 8601 UTC timestamp `minute` (under 1440) minutes into a fixed day."""
    return "2026-01-01T%02d:%02d:00Z" % (minute // 60, minute % 60)


class FakePulls:
    """pulls?state=all for one repo, with ETags, Link headers and both sorts."""

    def __init__(self, count):
        # Pull request n was opened and last updated at minute n (oldest first).
        self.prs = {n: {"number": n, "state": "closed", "title": "pr %d" % n,
                        "created_at": stamp(n % 1400), "updated_at": stamp(n % 1400),
                        "html_url": "https://github.com/%s/pull/%d" % (REPO, n),
                        "head": {"ref": "loom/%d-x" % n, "sha": "a%d" % n},
                        "base": {"ref": "main", "repo": {"full_name": REPO,
                                                         "pushed_at": stamp(0)}}}
                    for n in range(1, count + 1)}
        self.version = 0
        self.counted = 0
        self.not_modified = 0

    def change(self, number, minute, state="open"):
        self.version += 1
        pr = self.prs[number]
        pr.update({"updated_at": stamp(minute), "state": state})
        for row in self.prs.values():
            row["base"]["repo"]["pushed_at"] = stamp(minute)

    def open(self, minute):
        self.version += 1
        number = max(self.prs) + 1
        self.prs[number] = dict(self.prs[1], number=number, title="pr %d" % number,
                                created_at=stamp(minute), updated_at=stamp(minute), state="open",
                                html_url="https://github.com/%s/pull/%d" % (REPO, number),
                                head={"ref": "loom/%d-x" % number, "sha": "b%d" % number},
                                base={"ref": "main", "repo": {"full_name": REPO,
                                                              "pushed_at": stamp(minute)}})
        return number

    def transport(self, method, url, headers, body):
        parts = urlsplit(url)
        if parts.path != "/repos/%s/pulls" % REPO:
            return 200, {}, b"[]"
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        if query.get("sort") == "updated":
            rows = sorted(self.prs.values(), key=lambda r: (r["updated_at"], r["number"]), reverse=True)
        else:
            rows = sorted(self.prs.values(), key=lambda r: r["number"], reverse=True)
        page = int(query.get("page", "1"))
        last = max(1, -(-len(rows) // PER_PAGE))
        etag = '"%s"' % hashlib.sha256(("%s|%d|%d" % (parts.query, page, self.version)).encode()).hexdigest()
        if headers.get("If-None-Match") == etag:
            self.not_modified += 1
            return 304, {"ETag": etag}, b""
        self.counted += 1
        base = "https://api.github.com/repos/%s/pulls?%s" % (
            REPO, "&".join("%s=%s" % (k, v) for k, v in query.items() if k != "page"))
        links = []
        if page < last:
            links.append('<%s&page=%d>; rel="next"' % (base, page + 1))
            links.append('<%s&page=%d>; rel="last"' % (base, last))
        data = rows[(page - 1) * PER_PAGE:page * PER_PAGE]
        return 200, {"ETag": etag, "Link": ", ".join(links)}, json.dumps(data).encode()


class IncrementalPullRequestReadTest(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.TemporaryDirectory()
        self.addCleanup(self.cache.cleanup)
        self.fake = FakePulls(PAGES * PER_PAGE)

    def store(self, cache_name="warm"):
        gh = GitHub("", transport=self.fake.transport,
                    cache_dir=os.path.join(self.cache.name, cache_name))
        return TicketStore([REPO], gh=gh)

    def refresh(self, store):
        with patch.dict(os.environ, {}, clear=True):
            store.refresh()
        return store.response()["pull_requests"]

    def full_read(self):
        """What a dashboard with nothing kept reads now."""
        return self.refresh(self.store(cache_name="cold-%d" % self.fake.version))

    def test_warm_refresh_after_one_change_costs_at_most_two_pull_requests_reads(self):
        store = self.store()
        cold = self.refresh(store)
        self.assertEqual(len(cold), PAGES * PER_PAGE)
        self.assertEqual(self.fake.counted, PAGES)
        # One old pull request (page 40 of the list) changes; the embedded
        # repo moves on every page.
        self.fake.change(7, 1430)
        self.fake.counted = 0
        self.refresh(store)
        self.assertLessEqual(self.fake.counted, 2)

    def test_unchanged_refresh_counts_nothing(self):
        store = self.store()
        self.refresh(store)
        # The first incremental pass reads the update-sorted first page once.
        self.refresh(store)
        self.fake.counted = 0
        self.refresh(store)
        self.assertEqual(self.fake.counted, 0)

    def test_incremental_result_equals_a_full_read(self):
        store = self.store()
        self.refresh(store)
        self.fake.change(7, 1430, state="open")
        self.fake.change(3999, 1431, state="closed")
        warm = self.refresh(store)
        self.assertEqual(warm, self.full_read())
        old = next(row for row in warm if row["number"] == 7)
        self.assertEqual((old["state"], old["updated_at"]), ("open", stamp(1430)))

    def test_a_newly_opened_pull_request_goes_first(self):
        store = self.store()
        self.refresh(store)
        number = self.fake.open(1432)
        self.fake.counted = 0
        warm = self.refresh(store)
        self.assertLessEqual(self.fake.counted, 2)
        self.assertEqual(warm[0]["number"], number)
        self.assertEqual(warm, self.full_read())

    def test_more_changes_than_a_page_read_further_pages(self):
        store = self.store()
        self.refresh(store)
        for i, number in enumerate(range(1, 151)):
            self.fake.change(number, 1400 + i % 30)
        self.assertEqual(self.refresh(store), self.full_read())

    def test_every_page_is_read_again_after_the_full_read_interval(self):
        store = self.store()
        self.refresh(store)
        self.fake.change(7, 1430)
        self.fake.counted = 0
        with patch("dashboard.github.PR_FULL_READ_SECONDS", 0):
            self.refresh(store)
        self.assertEqual(self.fake.counted, PAGES)


class RequestLogTest(unittest.TestCase):
    def test_spent_requests_are_logged_once_an_interval(self):
        fake = FakePulls(150)
        gh = GitHub("", transport=fake.transport)
        store = TicketStore([REPO], gh=gh)
        with patch.dict(os.environ, {}, clear=True):
            store.refresh()
            since, kinds, not_modified = store._request_mark
            store._request_mark = (since - 4000, kinds, not_modified)
            with self.assertLogs("dashboard.github", level="INFO") as logs:
                store.refresh()
        line = [out for out in logs.output if "GitHub requests in the last" in out]
        self.assertEqual(len(line), 1)
        self.assertIn("pulls list", line[0])


if __name__ == "__main__":
    unittest.main()
