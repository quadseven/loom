"""bus.goal_consolidate: a consolidation plan run against an in-memory tracker."""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock
from urllib.parse import unquote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bus import __main__ as cli  # noqa: E402
from bus import goal_consolidate as gc  # noqa: E402
from bus.github import GitHubError  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

APP = "acme/app"
LIB = "acme/lib"


def ref(repo, number):
    return "%s#%d" % (repo, number)


class Resp:
    def __init__(self, data):
        self.data = data
        self.headers = {}


class Tracker:
    """Issues by (repo, number), answering the calls the module makes.
    refuse_links lists parents whose sub-issue link GitHub refuses;
    refuse_patch lists issue numbers whose body edit fails."""

    def __init__(self):
        self.issues = {}
        self.writes = []
        self.refuse_links = set()
        self.refuse_patch = set()

    def add(self, repo, number, labels=(), state="open", body="", parent=None):
        self.issues[(repo, number)] = {
            "number": number, "id": 1000 + number, "state": state, "body": body,
            "labels": [{"name": name} for name in labels],
            "repository_url": "https://api.github.com/repos/" + repo,
            "parent_issue_url": ("https://api.github.com/repos/%s/issues/%d" % parent) if parent else None}

    def _at(self, path):
        match = re.fullmatch(r"/repos/([^/]+/[^/]+)/issues/(\d+)(/\w+)?", path)
        key = (match.group(1), int(match.group(2)))
        return key, match.group(3) or ""

    def _by_id(self, wanted):
        return next(i for i in self.issues.values() if i["id"] == wanted)

    def _search(self, path):
        text = unquote(path)
        repo = re.search(r"repo:(\S+)", text).group(1)
        return {"items": [i for (r, _), i in self.issues.items() if r == repo
                          and any(n["name"] == "loom:ticket" for n in i["labels"])]}

    def rest(self, method, path, body=None):
        if path.startswith("/search/issues"):
            return Resp(self._search(path))
        key, tail = self._at(path)
        if method == "GET":
            if key not in self.issues:
                raise GitHubError("missing", status=404)
            return Resp(self.issues[key])
        self.writes.append((method, ref(*key) + tail, body))
        if tail == "/sub_issues":
            return self._link(key, body)
        if tail == "/comments":
            return Resp({})
        if key[1] in self.refuse_patch:
            raise GitHubError("refused", status=500)
        self.issues[key].update(body)
        return Resp(self.issues[key])

    def _link(self, key, body):
        if key in self.refuse_links:
            raise GitHubError("full", status=422)
        child = self._by_id(body["sub_issue_id"])
        child["parent_issue_url"] = "https://api.github.com/repos/%s/issues/%d" % key
        return Resp({})

    def paginate(self, path):
        key = self._at(path.partition("?")[0].replace("/sub_issues", ""))[0]
        url = "https://api.github.com/repos/%s/issues/%d" % key
        return [i for i in self.issues.values() if i.get("parent_issue_url") == url]


def plan(*homes, top=(ref(APP, 1),)):
    return gc.parse_plan({"top": list(top), "homes": list(homes)})


def move(home, action="sub-goal", parent=None):
    return {"home": home, "action": action, "parent": parent or ref(APP, 1)}


class TrackerCase(unittest.TestCase):
    def setUp(self):
        self.gh = Tracker()
        self.gh.add(APP, 1, ["loom:goal"])

    def run_plan(self, *homes, top=(ref(APP, 1),)):
        with contextlib.redirect_stderr(io.StringIO()):
            return gc.apply(self.gh, gc.survey(self.gh, plan(*homes, top=top)))


class PlanShape(unittest.TestCase):
    def refused(self, data, word):
        with self.assertRaisesRegex(ValueError, word):
            gc.parse_plan(data)

    def test_more_than_ten_top_level_homes_is_refused(self):
        self.refused({"top": [ref(APP, n) for n in range(1, 12)], "homes": []}, "10 or fewer")

    def test_a_home_listed_twice_is_refused(self):
        entry = move(ref(APP, 2))
        self.refused({"top": [ref(APP, 1)], "homes": [entry, entry]}, "more than once")

    def test_a_top_level_home_cannot_also_move(self):
        self.refused({"top": [ref(APP, 1)], "homes": [move(ref(APP, 1), parent=ref(APP, 2))]},
                     "more than once")

    def test_a_loop_of_parents_is_refused(self):
        self.refused({"top": [ref(APP, 1)],
                      "homes": [move(ref(APP, 2), parent=ref(APP, 3)),
                                move(ref(APP, 3), parent=ref(APP, 2))]}, "reaches no top-level")

    def test_a_parent_outside_the_plan_is_refused(self):
        self.refused({"top": [ref(APP, 1)], "homes": [move(ref(APP, 2), parent=ref(APP, 9))]},
                     "reaches no top-level")

    def test_a_merge_into_another_repo_is_refused(self):
        self.refused({"top": [ref(APP, 1), ref(LIB, 1)],
                      "homes": [move(ref(APP, 2), "merge", ref(LIB, 1))]}, "same repo")

    def test_an_unknown_action_is_refused(self):
        self.refused({"top": [ref(APP, 1)], "homes": [move(ref(APP, 2), "shelve")]}, "action")

    def test_a_malformed_ref_is_refused(self):
        self.refused({"top": ["app"], "homes": []}, "OWNER/NAME#N")


class Merge(TrackerCase):
    def setUp(self):
        super().setUp()
        self.gh.add(APP, 10, ["epic"])
        self.gh.add(APP, 11, ["epic"], body="Part of #1", parent=(APP, 1))

    def merge(self):
        return self.run_plan(move(ref(APP, 11), parent=ref(APP, 1)),
                             move(ref(APP, 10), "merge", ref(APP, 11)))

    def test_open_tickets_of_a_merged_home_move_to_the_survivor(self):
        self.gh.add(APP, 20, ["loom:ticket"], body="## Why\nx\n\nPart of #10\n", parent=(APP, 10))
        self.gh.add(APP, 21, ["loom:ticket"], body="Part of #10")
        self.gh.add(LIB, 22, ["loom:ticket"], body="Part of " + ref(APP, 10), parent=(APP, 10))
        result = self.merge()
        issues = self.gh.issues
        self.assertEqual(issues[(APP, 20)]["body"], "## Why\nx\n\nPart of #11\n")
        self.assertTrue(issues[(APP, 20)]["parent_issue_url"].endswith("/issues/11"))
        self.assertIn("Part of #11", issues[(APP, 21)]["body"])
        self.assertEqual(issues[(LIB, 22)]["body"], "Part of " + ref(APP, 11))
        self.assertEqual(issues[(APP, 10)]["state"], "closed")
        self.assertEqual(issues[(APP, 10)]["state_reason"], "duplicate")
        self.assertEqual(result.moved, 3)

    def test_a_closed_ticket_stays_where_it_was(self):
        self.gh.add(APP, 20, ["loom:ticket"], state="closed", body="Part of #10", parent=(APP, 10))
        self.merge()
        self.assertEqual(self.gh.issues[(APP, 20)]["body"], "Part of #10")

    def test_a_failed_ticket_move_leaves_the_home_open(self):
        self.gh.add(APP, 20, ["loom:ticket"], body="Part of #10", parent=(APP, 10))
        self.gh.refuse_patch.add(20)
        with self.assertRaises(GitHubError):
            self.merge()
        self.assertEqual(self.gh.issues[(APP, 10)]["state"], "open")

    def test_a_survivor_at_the_link_limit_still_gets_the_ticket(self):
        self.gh.add(APP, 20, ["loom:ticket"], body="Part of #10", parent=(APP, 10))
        self.gh.refuse_links.add((APP, 11))
        result = self.merge()
        self.assertEqual(self.gh.issues[(APP, 20)]["body"], "Part of #11")
        self.assertEqual(result.moved, 1)
        self.assertEqual(self.gh.issues[(APP, 10)]["state"], "closed")

    def test_a_merged_home_says_where_it_went(self):
        self.merge()
        comments = [body for _, where, body in self.gh.writes if where.endswith("/comments")]
        self.assertEqual(len(comments), 1)
        self.assertIn(ref(APP, 11), comments[0]["body"])


class Refusals(TrackerCase):
    def problems(self, *homes, top=(ref(APP, 1),)):
        return gc.survey(self.gh, plan(*homes, top=top)).problems

    def test_a_closed_survivor_would_orphan_the_tickets(self):
        self.gh.add(APP, 10, ["epic"], state="closed")
        self.gh.add(APP, 11, ["epic"])
        self.assertRegex("\n".join(self.problems(move(ref(APP, 10), parent=ref(APP, 1)),
                                                 move(ref(APP, 11), "merge", ref(APP, 10)))),
                         "closed")

    def test_a_backlog_home_never_merges_because_onboard_would_reopen_one(self):
        self.gh.add(APP, 10, ["loom:goal", "loom:backlog"])
        self.gh.add(APP, 11, ["epic"])
        found = self.problems(move(ref(APP, 11), parent=ref(APP, 1)),
                              move(ref(APP, 10), "merge", ref(APP, 11)))
        self.assertRegex("\n".join(found), "backlog")

    def test_a_queue_must_carry_the_backlog_label(self):
        self.gh.add(APP, 10, ["loom:goal"])
        self.assertRegex("\n".join(self.problems(move(ref(APP, 10), "queue"))), "loom:backlog")

    def test_a_missing_or_closed_top_level_home_is_a_problem(self):
        self.gh.add(APP, 2, ["loom:goal"], state="closed")
        found = self.problems(top=(ref(APP, 1), ref(APP, 2), ref(APP, 3)))
        self.assertEqual(len(found), 2)

    def test_an_open_home_cannot_go_under_a_closed_parent(self):
        self.gh.add(APP, 10, ["epic"], state="closed")
        self.gh.add(APP, 11)
        found = self.problems(move(ref(APP, 10), parent=ref(APP, 1)),
                              move(ref(APP, 11), parent=ref(APP, 10)))
        self.assertRegex("\n".join(found), "closed")

    def test_any_problem_stops_every_write(self):
        self.gh.add(APP, 10, ["loom:goal"])
        self.gh.add(APP, 11)
        survey = gc.survey(self.gh, plan(move(ref(APP, 10), "queue"), move(ref(APP, 11))))
        with self.assertRaisesRegex(ValueError, "problem"):
            gc.apply(self.gh, survey)
        self.assertEqual(self.gh.writes, [])

    def test_a_repo_in_shadow_stops_every_write(self):
        self.gh.add(APP, 11)
        survey = gc.survey(self.gh, plan(move(ref(APP, 11))))
        with mock.patch.object(gc.shadow, "active", return_value=True):
            with self.assertRaisesRegex(ValueError, "shadow"):
                gc.apply(self.gh, survey)
        self.assertEqual(self.gh.writes, [])


class Reparent(TrackerCase):
    def test_a_home_gets_a_parent_goal_line_and_keeps_its_body(self):
        self.gh.add(LIB, 5, ["loom:goal"], body="Goal text\n")
        self.run_plan(move(ref(LIB, 5)))
        self.assertEqual(self.gh.issues[(LIB, 5)]["body"],
                         "Goal text\n\nParent goal: %s\n" % ref(APP, 1))

    def test_an_existing_parent_goal_line_is_replaced(self):
        self.gh.add(APP, 2, ["loom:goal"], body="Parent goal: #3\nText\n")
        self.gh.add(APP, 3, ["loom:goal"])
        self.run_plan(move(ref(APP, 2), parent=ref(APP, 1)), move(ref(APP, 3)))
        self.assertEqual(self.gh.issues[(APP, 2)]["body"], "Parent goal: %s\nText\n" % ref(APP, 1))

    def test_a_queue_keeps_its_labels_and_stays_open(self):
        self.gh.add(APP, 2, ["loom:goal", "loom:backlog"], body="Intake")
        self.run_plan(move(ref(APP, 2), "queue"))
        issue = self.gh.issues[(APP, 2)]
        self.assertEqual(issue["state"], "open")
        self.assertEqual([n["name"] for n in issue["labels"]], ["loom:goal", "loom:backlog"])
        self.assertIn("Parent goal: " + ref(APP, 1), issue["body"])

    def test_a_closed_home_still_gets_its_pointer(self):
        self.gh.add(APP, 2, state="closed", body="Old")
        self.run_plan(move(ref(APP, 2)))
        self.assertIn("Parent goal:", self.gh.issues[(APP, 2)]["body"])
        self.assertEqual(self.gh.issues[(APP, 2)]["state"], "closed")

    def test_an_epic_moves_by_its_part_of_line_because_the_page_ignores_parent_lines(self):
        self.gh.add(APP, 2, ["loom:goal"])
        self.gh.add(APP, 3, ["epic"], body="Part of #2\n", parent=(APP, 2))
        self.run_plan(move(ref(APP, 2)), move(ref(APP, 3)))
        issue = self.gh.issues[(APP, 3)]
        self.assertEqual(issue["body"], "Part of #1\n")
        self.assertTrue(issue["parent_issue_url"].endswith("/issues/1"))

    def test_an_epic_under_a_goal_in_another_repo_names_the_repo(self):
        self.gh.add(LIB, 3, ["epic"], body="Text")
        self.run_plan(move(ref(LIB, 3)))
        self.assertIn("Part of %s" % ref(APP, 1), self.gh.issues[(LIB, 3)]["body"])

    def test_a_refused_epic_link_leaves_the_part_of_line_in_charge(self):
        self.gh.add(APP, 3, ["epic"], body="Text")
        self.gh.refuse_links.add((APP, 1))
        result = self.run_plan(move(ref(APP, 3)))
        self.assertIn("Part of #1", self.gh.issues[(APP, 3)]["body"])
        self.assertEqual(len(result.notes), 1)

    def test_a_second_run_writes_nothing(self):
        self.gh.add(LIB, 5, ["loom:goal"], body="Goal")
        self.gh.add(APP, 3, ["epic"], body="Text")
        homes = (move(ref(LIB, 5)), move(ref(APP, 3)))
        self.run_plan(*homes)
        self.gh.writes.clear()
        self.run_plan(*homes)
        self.assertEqual(self.gh.writes, [])

    def test_a_missing_home_is_reported_and_skipped(self):
        survey = gc.survey(self.gh, plan(move(ref(APP, 44))))
        self.assertEqual(survey.problems, [])
        self.assertTrue(any("no such issue" in line for line in survey.lines()))

    def test_an_open_ticket_under_a_closed_home_is_named(self):
        self.gh.add(APP, 2, state="closed")
        self.gh.add(APP, 3, ["loom:ticket"], parent=(APP, 2))
        survey = gc.survey(self.gh, plan(move(ref(APP, 2))))
        self.assertTrue(any("1 open ticket" in line for line in survey.lines()))


class Command(TrackerCase):
    def invoke(self, *extra):
        data = {"top": [ref(APP, 1)], "homes": [move(ref(LIB, 5))]}
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "plan.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = cli._run(cli._parser().parse_args(["consolidate-goals", "--file", path, *extra]),
                                self.gh)
        return code, out.getvalue()

    def test_without_apply_it_only_prints_the_steps(self):
        self.gh.add(LIB, 5, ["loom:goal"])
        code, out = self.invoke()
        self.assertEqual(code, 0)
        self.assertIn(ref(LIB, 5), out)
        self.assertEqual(self.gh.writes, [])

    def test_with_apply_it_writes(self):
        self.gh.add(LIB, 5, ["loom:goal"])
        self.assertEqual(self.invoke("--apply")[0], 0)
        self.assertIn("Parent goal:", self.gh.issues[(LIB, 5)]["body"])

    def test_a_plan_with_a_problem_exits_1(self):
        self.gh.issues[(APP, 1)]["state"] = "closed"
        self.gh.add(LIB, 5, ["loom:goal"])
        self.assertEqual(self.invoke()[0], 1)


if __name__ == "__main__":
    unittest.main()
