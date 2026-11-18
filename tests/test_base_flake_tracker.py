"""Tracker aggregation uses fake transport only, never credentials or the network."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from bus import base_flake_tracker as base_flakes
from bus import station
from shadow_env import setUpModule, tearDownModule  # noqa: F401

REPO = "example/project"
NOW = 2000000000


def event(number, at=NOW):
    return {"version": 1, "id": "%032x" % number, "at": at, "repo": REPO,
            "command": "private-check-name", "excerpt": "private-check-output"}


class Fake:
    api = "https://api.example.com"
    def __init__(self):
        self.rate = {}
        self.issues = []
        self.calls = []
        self.fail_patch = False

    def paginate(self, path):
        self.calls.append(("GET", path, None))
        return copy.deepcopy([i for i in self.issues if i["state"] == "open"])

    def rest(self, method, path, data=None):
        self.calls.append((method, path, copy.deepcopy(data)))
        if method == "GET" and path.endswith("/issues/42"):
            return SimpleNamespace(data={"number": 42, "state": "open", "labels": [], "body": ""})
        if method == "POST" and path.endswith("/issues"):
            issue = dict(data, number=len(self.issues) + 1, state="open")
            self.issues.append(issue)
            return SimpleNamespace(data=copy.deepcopy(issue))
        issue = next(i for i in self.issues if "/issues/%d" % i["number"] in path)
        if method == "PATCH":
            if self.fail_patch:
                raise base_flakes.GitHubError("write failed")
            issue.update(data)
        elif method == "POST" and path.endswith("/labels"):
            issue["labels"] += data["labels"]
        else:
            raise AssertionError((method, path))
        return SimpleNamespace(data=copy.deepcopy(issue))


class TrackerTests(unittest.TestCase):
    def test_three_unique_events_promote_once_without_comments_or_output(self):
        gh = Fake()
        for number in (1, 2, 3, 3):
            base_flakes.publish(gh, REPO, [event(number)], "codex", NOW)
        self.assertEqual(len(gh.issues), 1)
        self.assertIn("ready-for-agent", gh.issues[0]["labels"])
        self.assertEqual(sum(path.endswith('/labels') for _, path, _ in gh.calls), 1)
        text = json.dumps(gh.calls)
        self.assertNotIn("private-check", text)
        self.assertNotIn("/comments", text)

    def test_old_future_and_foreign_events_do_not_create_a_tracker(self):
        gh = Fake()
        foreign = dict(event(2), repo="example/other")
        base_flakes.publish(gh, REPO, [event(1, NOW-86401), foreign,
                                     event(3, NOW+1)], "codex", NOW)
        self.assertEqual(gh.issues, [])

    def test_duplicate_trackers_merge_before_closing_and_count_all_events(self):
        gh = Fake()
        base_flakes.publish(gh, REPO, [event(1)], "codex", NOW)
        other = Fake()
        base_flakes.publish(other, REPO, [event(2)], "claude", NOW)
        duplicate = dict(other.issues[0], number=2)
        gh.issues.append(duplicate)
        base_flakes.publish(gh, REPO, [event(3)], "codex", NOW)
        self.assertEqual([i['state'] for i in gh.issues], ['open', 'closed'])
        self.assertIn('ready-for-agent', gh.issues[0]['labels'])
        self.assertIn('%032x' % 2, gh.issues[0]['body'])

    def test_failed_canonical_write_leaves_duplicate_open(self):
        gh = Fake()
        base_flakes.publish(gh, REPO, [event(1)], "codex", NOW)
        gh.issues.append(dict(gh.issues[0], number=2))
        gh.fail_patch = True
        with self.assertRaises(base_flakes.GitHubError):
            base_flakes.publish(gh, REPO, [event(2)], "codex", NOW)
        self.assertTrue(all(i['state'] == 'open' for i in gh.issues))

    def test_station_reads_sidecar_for_the_brief_repo(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / 'logs/codex/ticket.base-flakes.jsonl'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(event(1))+'\n')
            gh = Fake()
            settings = station.Settings(home, [REPO], ['operator-example'], ['example'],
                                        disk_free_min_gb=0)
            st = station.Station(gh, settings)
            st.now = NOW
            brief = Path(home) / 'brief.md'
            brief.write_text('Issue: %s#%d\nDeliverable: verify\n' % (REPO, 42))
            self.assertTrue(st._report('codex', 'done', str(brief), 'ticket'))
            self.assertEqual(len(gh.issues), 1)

    def test_old_tracker_rows_do_not_count_toward_the_current_threshold(self):
        gh = Fake()
        base_flakes.publish(gh, REPO, [event(1)], "codex", NOW)
        base_flakes.publish(gh, REPO, [event(2, NOW + 86401), event(3, NOW + 86401)],
                            "codex", NOW + 86401)
        self.assertNotIn("ready-for-agent", gh.issues[0]["labels"])

    def test_unrecognized_tracker_body_is_not_overwritten(self):
        gh = Fake()
        gh.issues.append(dict(number=1, title=base_flakes.TRACKER_TITLE,
                              body="Operator notes", labels=["loom:factory"], state="open"))
        with self.assertRaises(base_flakes.GitHubError):
            base_flakes.publish(gh, REPO, [event(1)], "codex", NOW)
        self.assertEqual(gh.issues[0]["body"], "Operator notes")

    def test_invalid_sidecar_is_not_silently_acknowledged(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bad.jsonl"
            path.write_text("not json\n")
            with self.assertRaises(base_flakes.GitHubError):
                base_flakes.read_events(path)

    def test_next_pass_reconciles_already_reported_sidecars(self):
        with tempfile.TemporaryDirectory() as home:
            gh = Fake()
            base_flakes.publish(gh, REPO, [event(1)], "codex", NOW)
            other = Fake()
            base_flakes.publish(other, REPO, [event(2)], "claude", NOW)
            gh.issues.append(dict(other.issues[0], number=2))
            brief = Path(home) / 'briefs/codex/done/ticket.md'
            brief.parent.mkdir(parents=True)
            brief.write_text('Issue: %s#%d\nDeliverable: verify\n' % (REPO, 42))
            path = Path(home) / 'logs/codex/ticket.base-flakes.jsonl'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(event(1))+'\n')
            settings = station.Settings(home, [REPO], ['operator-example'], ['example'],
                                        disk_free_min_gb=0)
            st = station.Station(gh, settings)
            st.now = NOW
            st._reconcile_reported_flakes({'codex/ticket': 'done'})
            self.assertEqual(gh.issues[1]['state'], 'closed')
            self.assertIn('%032x' % 2, gh.issues[0]['body'])
