"""What the dashboard keeps in memory between refreshes (#690)."""
import json
import os
import unittest
from unittest.mock import patch

from dashboard import results, views
from dashboard.github import TicketStore
from dashboard.ticket_data import MAX_KEPT, TicketDetails


REPO = "example-owner/sample"
PULLS = 300
URLS = 80


def _repo_object(name):
    # The shape GitHub embeds in each pull request's base and head: about 80
    # URL and count fields, most of a pull request list item's size.
    out = {"id": 1, "full_name": name, "name": name.split("/")[1], "private": False,
           "owner": _user("example-owner")}
    out.update({"field_%02d_url" % i: "https://api.example.com/repos/%s/%02d{/x}" % (name, i)
                for i in range(URLS)})
    return out


def _user(login, kind="User"):
    out = {"login": login, "id": 7, "type": kind, "site_admin": False}
    out.update({"link_%02d_url" % i: "https://api.example.com/users/%s/%02d" % (login, i)
                for i in range(18)})
    return out


def _pull(number):
    return {"number": number, "title": "Change %d" % number, "state": "closed", "draft": False,
            "html_url": "https://github.com/%s/pull/%d" % (REPO, number),
            "body": "Refs #%d\n\nLoom ticket %s#%d, lane codex, model gpt-5." % (number, REPO, number),
            "created_at": "2026-10-01T00:00:00Z", "updated_at": "2026-10-02T00:00:00Z",
            "merged_at": "2026-10-02T00:00:00Z", "closed_at": "2026-10-02T00:00:00Z",
            "user": _user("example-bot", "Bot"), "labels": [{"name": "x", "description": "y" * 80}],
            "head": {"ref": "loom/%d-change" % number, "sha": "%040d" % number,
                     "repo": _repo_object(REPO), "user": _user("example-owner")},
            "base": {"ref": "main", "sha": "f" * 40, "repo": _repo_object(REPO),
                     "user": _user("example-owner")},
            "_links": {k: {"href": "https://api.example.com/%s/%d" % (k, number)}
                       for k in ("self", "html", "issue", "comments", "commits", "statuses")}}


def _comment(number, review=False):
    out = {"id": number, "body": "Lane codex opened a pull request #%d" % number,
           "created_at": "2026-10-10T00:00:00Z", "updated_at": "2026-10-10T00:00:00Z",
           "html_url": "https://github.com/%s/issues/%d#c" % (REPO, number),
           "user": _user("example-bot", "Bot"), "node_id": "N" * 30,
           "reactions": {k: 0 for k in ("+1", "-1", "laugh", "hooray", "confused", "heart")},
           "author_association": "NONE"}
    if review:
        out.update({"path": "bus/a.py", "diff_hunk": "@@ -1,9 +1,9 @@\n" + "+line\n" * 40,
                    "pull_request_url": "https://api.example.com/repos/%s/pulls/%d" % (REPO, number),
                    "_links": {"self": {"href": "https://api.example.com/c/%d" % number}}})
    else:
        out["issue_url"] = "https://api.example.com/repos/%s/issues/%d" % (REPO, number)
    return out


class RawGitHub:
    rate = {}

    def paginate(self, path):
        if "/issues/comments?" in path:
            return [_comment(n) for n in range(PULLS)]
        if "/pulls/comments?" in path:
            return [_comment(n, review=True) for n in range(PULLS)]
        return []

    def paginate_parallel(self, path, workers=8):
        if "/pulls?" in path:
            return [_pull(n) for n in range(1, PULLS + 1)]
        return self.paginate(path)

    def rest(self, *args, **kwargs):
        raise OSError("not in this fixture")


def _size(rows):
    return len(json.dumps(rows))


class SnapshotMemoryTest(unittest.TestCase):
    def refreshed(self):
        env = {"LOOM_IMPROVEMENT_HOMES": "", "LOOM_STATUS_REPO": "", "LOOM_METRICS_GOAL": "",
               "LOOM_BRIEF_AUTHORS": ""}
        with patch.dict(os.environ, env):
            store = TicketStore([REPO], gh=RawGitHub(), budget_floor=0)
            store._waits_refreshed_at = float("inf")
            store.refresh()
        return store.response()

    def test_pull_requests_keep_only_what_the_views_read(self):
        prs = self.refreshed()["pull_requests"]
        self.assertEqual(len(prs), PULLS)
        raw = [_pull(n) for n in range(1, PULLS + 1)]
        # The raw items are about 17 KB of JSON each; a kept one is the
        # fields the views read and its body.
        self.assertLess(_size(prs) * 10, _size(raw))
        self.assertEqual(prs[0]["head"], {"ref": "loom/1-change", "sha": "%040d" % 1})
        self.assertEqual(prs[0]["base"], {"ref": "main", "repo": {"full_name": REPO}})
        self.assertEqual(prs[0]["user"], {"login": "example-bot", "type": "Bot"})
        # Every view that reads pull requests reads the same from either.
        for read in (views.footer_models, views.footer_runs, views.build_scoreboard):
            self.assertEqual(read(prs), read(raw))
        self.assertEqual(results._week_by_repo(prs, [], results._EPOCH),
                         results._week_by_repo(raw, [], results._EPOCH))

    def test_comments_keep_only_what_the_views_read(self):
        payload = self.refreshed()
        issues, reviews = payload["issue_comments"], payload["review_comments"]
        self.assertEqual((len(issues), len(reviews)), (PULLS, PULLS))
        self.assertLess(_size(issues) * 4, _size([_comment(n) for n in range(PULLS)]))
        self.assertLess(_size(reviews) * 4, _size([_comment(n, True) for n in range(PULLS)]))
        self.assertEqual(issues[1], {
            "id": 1, "body": "Lane codex opened a pull request #1",
            "created_at": "2026-10-10T00:00:00Z", "updated_at": "2026-10-10T00:00:00Z",
            "html_url": "https://github.com/%s/issues/1#c" % REPO,
            "issue_url": "https://api.example.com/repos/%s/issues/1" % REPO,
            "user": {"login": "example-bot", "type": "Bot"}, "repo": REPO})
        self.assertEqual(reviews[1]["path"], "bus/a.py")
        self.assertNotIn("diff_hunk", reviews[1])


class EmptyGitHub:
    def paginate(self, path):
        return []


class TicketDetailsBoundTest(unittest.TestCase):
    def test_keeps_at_most_its_cap_of_tickets(self):
        details = TicketDetails(EmptyGitHub(), ttl=3600, clock=lambda: 0.0)
        for number in range(MAX_KEPT + 40):
            details.get(REPO, number)
        self.assertEqual(len(details._cache), MAX_KEPT)

    def test_drops_expired_tickets(self):
        now = [0.0]
        details = TicketDetails(EmptyGitHub(), ttl=90, clock=lambda: now[0])
        for number in range(5):
            details.get(REPO, number)
        now[0] = 100.0
        details.get(REPO, 99)
        self.assertEqual(list(details._cache), [(REPO, 99)])


if __name__ == "__main__":
    unittest.main()
