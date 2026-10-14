"""The revert pass (bus/revert.py) over recorded check states and a fake GitHub."""

import contextlib
import io
import json
import os
import re
import tempfile
import unittest
from datetime import datetime, timezone

from bus import factory, revert, station
from bus.github import Response

REPO = "example-org/example-repo"
NOW = 2_000_000_000.0
ME = {"login": "alice-example"}
STRANGER = {"login": "mallory-example"}
CI = "unittest"


def iso(seconds_ago):
    return datetime.fromtimestamp(NOW - seconds_ago, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha(name):
    """A 40-hex commit id for a short name."""
    return (name.encode().hex() + "0" * 40)[:40]


def run(state, ago=3600, name=CI, run_id=1):
    """A check run: "green", "red", "running" or "cancelled", finished `ago`
    seconds before NOW."""
    if state == "running":
        return {"id": run_id, "name": name, "status": "in_progress", "conclusion": None}
    conclusion = {"green": "success", "red": "failure", "cancelled": "cancelled"}[state]
    return {"id": run_id, "name": name, "status": "completed", "conclusion": conclusion,
            "completed_at": iso(ago)}


class FakeGitHub:
    def __init__(self):
        self.commits = []      # newest first
        self.runs = {}
        self.pulls = []        # closed pull requests
        self.issues = {}
        self.comments = {}
        self.reverts = []
        self.filed = []
        self.writes = []
        self.reads = []

    # -- recorded history ------------------------------------------------
    def commit(self, name, *runs, ago=3600):
        """Add a commit on top of the default branch, with its check runs."""
        self.commits.insert(0, {"sha": sha(name), "commit": {"committer": {"date": iso(ago)}}})
        self.runs[sha(name)] = list(runs)

    def merge(self, name, pr, ticket, *runs, ago=3600, head_runs=(run("green"),), user=ME):
        """A lane pull request squash-merged as commit `name`."""
        self.commit(name, *runs, ago=ago)
        head = sha("head-%d" % pr)
        self.runs[head] = list(head_runs)
        self.pulls.append({"number": pr, "title": "Change %s" % name, "user": user,
                           "body": "", "merged_at": iso(ago), "merge_commit_sha": sha(name),
                           "head": {"ref": "loom/%d-change" % ticket, "sha": head},
                           "base": {"ref": "main"}})
        self.issues[ticket] = {"number": ticket, "state": "open", "user": ME,
                               "body": "Difficulty: medium\n\n## Why\nx\n",
                               "labels": [{"name": "loom:ticket"}, {"name": "loom:merged"},
                                          {"name": "lane:codex"}]}
        self.comments.setdefault(ticket, [])

    # -- the client surface ----------------------------------------------
    def paginate(self, path):
        self.reads.append(path)
        if "labels=loom:revert&state=all" in path:
            return list(self.reverts)
        if "labels=loom:ticket&state=open" in path:
            return [i for i in self.issues.values() if i["state"] == "open"]
        m = re.search(r"/issues/(\d+)/comments$", path)
        if m:
            return list(self.comments.get(int(m.group(1)), []))
        raise AssertionError("unexpected paginate %s" % path)

    def rest(self, method, path, body=None, etag=None, ok=()):
        if method != "GET":
            return self._write(method, path, body)
        self.reads.append(path)
        if path.startswith("/repos/%s/pulls?state=closed" % REPO):
            # One page, the most recently closed first.
            return Response(200, list(reversed(self.pulls))[:100])
        m = re.search(r"/commits/([0-9a-f]{40})/pulls$", path)
        if m:
            return Response(200, [p for p in self.pulls if p.get("merge_commit_sha") == m.group(1)])
        if path == "/repos/%s" % REPO:
            return Response(200, {"default_branch": "main", "private": True})
        if path == "/repos/%s/commits?sha=main&per_page=%d" % (REPO, revert.COMMITS):
            return Response(200, list(self.commits))
        m = re.search(r"/commits/([0-9a-f]{40})/check-runs\?per_page=100$", path)
        if m:
            return Response(200, {"check_runs": self.runs.get(m.group(1), [])})
        m = re.search(r"/issues/(\d+)$", path)
        if m:
            return Response(200, self.issues[int(m.group(1))])
        raise AssertionError("unexpected GET %s" % path)

    def _write(self, method, path, body):
        self.writes.append((method, path, body))
        if method == "POST" and path.endswith("/issues"):
            made = dict(body, number=900 + len(self.filed), user=ME, state="open",
                        labels=[{"name": x} for x in body["labels"]])
            self.filed.append(made)
            return Response(201, made)
        m = re.search(r"/issues/(\d+)/comments$", path)
        if m:
            self.comments.setdefault(int(m.group(1)), []).append(
                {"body": body["body"], "user": ME, "created_at": iso(0)})
        return Response(200, {})


class Base(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        self.state = tempfile.mkdtemp()
        self.moved = []

    def go(self, now=NOW):
        def move(gh, repo, number, state, lane=None, note=None):
            self.moved.append((number, state, lane))
            gh.comments.setdefault(number, []).append({"body": note, "user": ME})
        lanes = lambda: factory.Factory(self.gh, [REPO], now, trusted=["Alice-Example"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            revert.Reverts(self.gh, [REPO], now, trusted=["Alice-Example"],
                           review_check=factory.DEFAULT_REVIEW_CHECK, lanes=lanes,
                           state_dir=self.state, move=move).run()
        return out.getvalue()

    def green_base(self):
        self.gh.commit("base", run("green", ago=8000), ago=8000)


class Bisect(Base):
    def test_green_to_red_across_three_merges_reverts_the_first_red_one(self):
        self.green_base()
        self.gh.merge("m1", 41, 4, run("green", ago=4000), ago=4200)
        self.gh.merge("m2", 51, 5, run("red", ago=3000), ago=3300)
        self.gh.merge("m3", 61, 6, run("red", ago=2400), ago=2600)
        self.gh.merge("m4", 71, 7, run("red", ago=2000), ago=2100)
        log = self.go()
        self.assertEqual(len(self.gh.filed), 1)
        made = self.gh.filed[0]
        self.assertEqual(made["title"], "Revert %s: Change m2" % sha("m2")[:12])
        self.assertIn("git revert --no-edit %s" % sha("m2"), made["body"])
        self.assertIn("- [ ] The check `unittest` passes on this pull request.", made["body"])
        self.assertIn("Reverted ticket: #5", made["body"])
        self.assertIn("Class: logic", made["body"])
        names = [x["name"] for x in made["labels"]]
        self.assertEqual(names, ["loom:ticket", "loom:briefed", "lane:spark", "loom:revert",
                                 "class:logic"])
        # The reverted ticket hears which check and which revert, and waits
        # for the revert to land before it is re-briefed.
        note = self.gh.comments[5][-1]["body"]
        self.assertIn("check `unittest` fails", note)
        self.assertIn("Revert ticket #900", note)
        self.assertEqual(self.moved, [])
        self.assertEqual(log.count("\n"), 1)
        self.assertIn("briefed revert #900 on lane spark", log)

    def test_a_scheduled_check_that_never_ran_on_the_pull_request_reverts_nothing(self):
        # 2026-10-14: a nightly check failing for days attached to the newest
        # merge on the branch head and briefed a revert of a good deploy.
        self.green_base()
        self.gh.merge("m1", 41, 4, run("green", ago=4000),
                      run("red", ago=3500, name="nightly invariants", run_id=2), ago=4200)
        log = self.go()
        self.assertEqual(self.gh.filed, [])
        self.assertIn("only from checks that never ran on #41", log)

    def test_a_check_that_ran_on_the_pull_request_still_reverts(self):
        self.green_base()
        self.gh.merge("m1", 41, 4, run("red", ago=4000),
                      run("red", ago=3500, name="nightly invariants", run_id=2), ago=4200)
        self.go()
        self.assertEqual(len(self.gh.filed), 1)
        self.assertIn("check `unittest` fails", self.gh.comments[4][-1]["body"])
        self.assertNotIn("nightly invariants", self.gh.comments[4][-1]["body"])

    def test_a_red_a_later_commit_fixes_within_30_minutes_reverts_nothing(self):
        self.green_base()
        self.gh.merge("m1", 41, 4, run("red", ago=1500), ago=1600)
        # Red for 25 minutes: it waits for a newer commit to turn it green.
        log = self.go()
        self.assertEqual(self.gh.filed, [])
        self.assertIn("for 25 minutes", log)
        self.gh.merge("m2", 51, 5, run("green", ago=600), ago=900)
        # Long after, the branch is green: m1's red is never reverted.
        self.assertEqual(self.go(now=NOW + 7200), "")
        self.assertEqual(self.gh.filed, [])

    def test_a_red_commit_no_lane_merged_reverts_nothing_and_is_said_once(self):
        self.green_base()
        self.gh.merge("m1", 41, 4, run("green", ago=5000), ago=5200)
        self.gh.commit("by-hand", run("red", ago=4000), ago=4100)
        self.gh.merge("m2", 51, 5, run("red", ago=3000), ago=3100)
        first = self.go()
        second = self.go()
        self.assertEqual(self.gh.filed, [])
        self.assertIn("%s, which no lane merged" % sha("by-hand")[:12], first)
        self.assertEqual(second, "")

    def test_a_lane_merge_past_the_first_page_of_closed_pulls_is_still_found(self):
        self.green_base()
        self.gh.merge("m1", 41, 4, run("red", ago=4000), ago=4100)
        for n in range(100):
            self.gh.pulls.append({"number": 100 + n, "user": STRANGER, "merged_at": None,
                                  "head": {"ref": "stale-%d" % n}, "base": {"ref": "main"}})
        self.gh.merge("m2", 51, 5, run("red", ago=3000), ago=3100)
        self.assertNotIn("which no lane merged", self.go())
        self.assertIn(sha("m1"), self.gh.filed[0]["body"])

    def test_a_lane_pull_request_by_a_stranger_is_not_a_lane_merge(self):
        self.green_base()
        self.gh.merge("m1", 41, 4, run("red", ago=4000), ago=4100, user=STRANGER)
        self.gh.merge("m2", 51, 5, run("red", ago=3000), ago=3100)
        self.assertIn("which no lane merged", self.go())
        self.assertEqual(self.gh.filed, [])

    def test_a_second_red_while_a_revert_is_in_flight_waits(self):
        self.green_base()
        self.gh.merge("m1", 41, 4, run("red", ago=4000), ago=4100)
        self.go()
        self.assertEqual(len(self.gh.filed), 1)
        self.gh.reverts = list(self.gh.filed)
        # A re-run clears m1, and a later merge breaks the branch on its own
        # while m1's revert is still in flight.
        self.gh.runs[sha("m1")].append(run("green", ago=3500, run_id=2))
        self.gh.merge("m5", 81, 8, run("red", ago=3000), ago=3100)
        log = self.go()
        self.assertEqual(len(self.gh.filed), 1)
        self.assertIn("it waits for revert #900, still in flight", log)

    def test_a_red_commit_already_named_by_a_revert_is_not_reverted_again(self):
        self.green_base()
        self.gh.merge("m1", 41, 4, run("red", ago=4000), ago=4100)
        self.go()
        self.gh.reverts = [dict(self.gh.filed[0], labels=[{"name": "loom:revert"},
                                                          {"name": "loom:closed-unmerged"}])]
        self.assertIn("revert #900 already names it", self.go())
        self.assertEqual(len(self.gh.filed), 1)

    def test_a_revert_the_label_list_does_not_show_yet_is_not_filed_twice(self):
        self.green_base()
        self.gh.merge("m1", 41, 4, run("red", ago=4000), ago=4100)
        self.go()
        self.go()
        self.assertEqual(len(self.gh.filed), 1)

    def test_a_failed_redo_does_not_stop_the_bisect(self):
        self.green_base()
        self.gh.merge("m1", 41, 4, run("red", ago=4000), ago=4100)
        self.gh.reverts = [{"number": 700, "state": "open", "user": ME, "updated_at": iso(60),
                            "body": "Reverted ticket: #404\n",
                            "labels": [{"name": "loom:merged"}, {"name": "loom:revert"}]}]
        self.gh.comments[404] = []
        log = self.go()
        self.assertIn("revert #700 reverted failed", log)
        self.assertEqual(len(self.gh.filed), 1)

    def test_a_commit_with_no_runs_yet_is_waited_on(self):
        self.green_base()
        self.gh.merge("m1", 41, 4, ago=4100)  # CI has not started on it
        self.gh.merge("m2", 51, 5, run("red", ago=3000), ago=3100)
        self.assertIn("waiting on the checks of %s" % sha("m1")[:12],
                      self.go(now=NOW - 4100 + 600))
        self.assertEqual(self.gh.filed, [])

    def test_a_commit_ci_never_ran_on_is_skipped(self):
        self.green_base()
        self.gh.merge("m1", 41, 4, run("cancelled", ago=4000), ago=4100)
        self.gh.merge("m2", 51, 5, run("red", ago=3000), ago=3100)
        self.go()
        self.assertIn(sha("m2"), self.gh.filed[0]["body"])

    def test_a_merge_whose_pull_request_head_was_not_green_is_left(self):
        self.green_base()
        self.gh.merge("m1", 41, 4, run("red", ago=4000), ago=4100,
                      head_runs=(run("red", ago=4500),))
        self.assertIn("#41's head was not green", self.go())
        self.assertEqual(self.gh.filed, [])

    def test_a_rerun_that_passed_replaces_the_failed_run(self):
        self.green_base()
        self.gh.merge("m1", 41, 4, run("red", ago=4000, run_id=1), run("green", ago=3000, run_id=2),
                      ago=4100)
        self.assertEqual(self.go(), "")
        self.assertEqual(self.gh.filed, [])

    def test_a_head_still_running_is_not_judged(self):
        self.green_base()
        self.gh.merge("m1", 41, 4, run("red", ago=4000), ago=4100)
        self.gh.merge("m2", 51, 5, run("running"), ago=300)
        self.assertEqual(self.go(), "")
        self.assertEqual(self.gh.filed, [])

    def test_a_repo_with_no_recent_lane_merge_is_not_read(self):
        self.green_base()
        self.gh.merge("m1", 41, 4, run("red", ago=revert.RECENT + 100), ago=revert.RECENT + 200)
        self.go()
        self.assertFalse([p for p in self.gh.reads if "/commits" in p])

    def test_the_bisect_reads_one_check_run_page_per_commit_and_no_checkout(self):
        self.green_base()
        self.gh.merge("m1", 41, 4, run("green", ago=4000), ago=4200)
        self.gh.merge("m2", 51, 5, run("red", ago=3000), ago=3300)
        self.gh.merge("m3", 61, 6, run("red", ago=2400), ago=2600)
        self.go()
        pages = [p for p in self.gh.reads if "/check-runs" in p]
        self.assertEqual(len(pages), len(set(pages)))
        self.assertTrue(all(p.endswith("/check-runs?per_page=100") for p in pages))
        # Head back to the last green one, and m2's pull request head.
        self.assertEqual(len(pages), 4)


class Redo(Base):
    def landed_revert(self, state="merged"):
        self.green_base()
        self.gh.merge("m1", 41, 4, run("red", ago=4000), ago=4100)
        self.go()
        made = self.gh.filed[0]
        made["labels"] = [{"name": "loom:ticket"}, {"name": "loom:" + state},
                          {"name": "loom:revert"}]
        made["updated_at"] = iso(60)
        self.gh.reverts = [made]
        self.gh.merge("r", 91, 900, run("green", ago=60), ago=120)
        self.gh.issues[900] = made

    def test_the_reverted_ticket_goes_back_to_its_lane_once_the_revert_lands(self):
        self.landed_revert()
        log = self.go()
        self.assertEqual(self.moved, [(4, "briefed", "codex")])
        self.assertIn(revert.REDO_MARKER % 900, self.gh.comments[4][-1]["body"])
        self.assertIn("re-briefed #4 on lane codex", log)
        self.go()
        self.assertEqual(self.moved, [(4, "briefed", "codex")])

    def test_a_revert_still_in_flight_re_briefs_nothing(self):
        self.landed_revert(state="pr")
        self.go()
        self.assertEqual(self.moved, [])


class StationStep(Base):
    def station(self, factory_on):
        home = tempfile.mkdtemp()
        settings = station.Settings(home, [REPO], ["alice-example"], ["example-org"],
                                    factory_on=factory_on)
        return station.Station(self.gh, settings, now=NOW)

    def beat(self, st, lane, available=True, reason=""):
        path = os.path.join(st.s.home, "state", "usage")
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, lane + ".json"), "w") as fh:
            json.dump({"available": available, "at": NOW, "reason": reason}, fh)

    def test_the_station_reverts_with_the_factory_on_and_not_with_it_off(self):
        self.green_base()
        self.gh.merge("m1", 41, 4, run("red", ago=4000), ago=4100)
        off, on = self.station(False), self.station(True)
        self.beat(on, "opencode-go")
        with contextlib.redirect_stdout(io.StringIO()):
            off.watch_default_branches()
            self.assertEqual((self.gh.filed, self.gh.reads), ([], []))
            on.watch_default_branches()
        self.assertEqual(len(self.gh.filed), 1)
        made = self.gh.filed[0]
        self.assertTrue(made["title"].startswith("Revert %s:" % sha("m1")[:12]))
        self.assertIn("lane:opencode-go", [x["name"] for x in made["labels"]])
        self.assertTrue(made["body"].startswith("Difficulty: easy\n"))

    def test_with_no_easy_lane_up_the_next_tier_takes_the_revert(self):
        self.green_base()
        self.gh.merge("m1", 41, 4, run("red", ago=4000), ago=4100)
        on = self.station(True)
        self.beat(on, "spark", available=False, reason="limit: hot")
        self.beat(on, "codex-2")
        with contextlib.redirect_stdout(io.StringIO()):
            on.watch_default_branches()
        made = self.gh.filed[0]
        self.assertIn("lane:codex-2", [x["name"] for x in made["labels"]])
        self.assertTrue(made["body"].startswith("Difficulty: medium\n"))

    def test_with_no_lane_up_the_revert_waits(self):
        self.green_base()
        self.gh.merge("m1", 41, 4, run("red", ago=4000), ago=4100)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.station(True).watch_default_branches()
        self.assertEqual(self.gh.filed, [])
        self.assertIn("no lane is up to take its revert", out.getvalue())


class States(unittest.TestCase):
    def test_bisect(self):
        cases = [
            ([("a", "green")], ("green", None)),
            ([("a", "none"), ("b", "green")], ("green", None)),
            ([("a", "pending"), ("b", "red")], ("running", "a")),
            ([("a", "red"), ("b", "red"), ("c", "green")], ("red", "b")),
            ([("a", "red"), ("b", "pending"), ("c", "green")], ("wait", "b")),
            ([("a", "red"), ("b", "none"), ("c", "red"), ("d", "green")], ("red", "c")),
            ([("a", "red"), ("b", "red")], ("unknown", None)),
        ]
        for states, want in cases:
            self.assertEqual(revert.bisect(states), want, states)

    def test_the_review_check_does_not_make_a_commit_red(self):
        runs = [run("green"), run("red", name=factory.DEFAULT_REVIEW_CHECK, run_id=2)]
        self.assertEqual(revert.commit_state(runs, 0, factory.DEFAULT_REVIEW_CHECK), "green")


if __name__ == "__main__":
    unittest.main()
