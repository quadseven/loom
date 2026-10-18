"""The naming standard (docs/NAMING.md): kind labels, kind words, clipped
generated titles, and the places that show a title without its kind word."""
from __future__ import annotations

import os
import sys
import types
import unittest
from unittest import mock
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain, health, naming, station  # noqa: E402
from dashboard import server, tickets_table  # noqa: E402

REPO = "example-owner/example-repo"
LONG = ("Make every lane report its spend and its limits to the station on each run so "
        "the dashboard can show them")


def labelled(*names, title="Goal: ship it"):
    return {"number": 3, "title": title, "labels": [{"name": n} for n in names]}


class KindWords(unittest.TestCase):
    def test_each_old_and_new_kind_word_is_stripped(self):
        for title in ("Goal: Ship it", "Loom goal: Ship it", "Goal home: Ship it",
                      "Backlog: Ship it", "Wayfinder: Ship it", "Map: Ship it",
                      "Epic: Ship it", "[EPIC] Ship it", "[Epic]: Ship it",
                      "goal:   Ship   it", "Goal home: Wayfinder: Ship it"):
            self.assertEqual(naming.strip_kind(title), "Ship it", title)

    def test_a_title_without_a_kind_word_is_kept(self):
        for title in ("Ship it", "Goals for the year", "Mapping the lanes: a plan",
                      "Epically slow pages load fast"):
            self.assertEqual(naming.strip_kind(title), title)

    def test_a_title_that_is_only_a_kind_word_is_kept(self):
        self.assertEqual(naming.strip_kind("Goal:"), "Goal:")
        self.assertEqual(naming.strip_kind(None), "")


class BacklogHome(unittest.TestCase):
    def test_the_backlog_label_marks_a_backlog_home_whatever_its_title(self):
        self.assertTrue(naming.is_backlog_home(labelled("loom:goal", "loom:backlog")))
        self.assertTrue(naming.is_backlog_home({"title": "x", "labels": ["loom:backlog"]}))

    def test_the_old_title_prefix_still_counts_during_migration(self):
        self.assertTrue(naming.is_backlog_home(labelled("loom:goal", title="Backlog: every open issue")))

    def test_another_goal_home_is_not_a_backlog_home(self):
        self.assertFalse(naming.is_backlog_home(labelled("loom:goal", title="Goal: backlog zero")))
        self.assertFalse(naming.is_backlog_home({"title": None, "labels": None}))

    def test_the_backlog_title_fits_the_standard(self):
        self.assertEqual(brain.backlog_title(REPO),
                         "Goal: Loom or a person works every open %s issue" % REPO)
        long_owner = "an-owner-with-a-long-name/" + "r" * 20
        self.assertEqual(brain.backlog_title(long_owner),
                         "Goal: Loom or a person works every open %s issue" % ("r" * 20))
        self.assertEqual(brain.backlog_title("o/" + "r" * 60), brain.BACKLOG_TITLE_BARE)
        for repo in (REPO, long_owner, "o/" + "r" * 60):
            title = brain.backlog_title(repo)
            self.assertLessEqual(len(title), naming.TITLE_MAX)
            self.assertTrue(title.startswith(naming.GOAL_WORD))

    def test_the_backlog_label_is_one_of_the_managed_labels(self):
        from bus import labels
        self.assertIn(naming.BACKLOG_LABEL, labels.LABELS)


class RepoGoal(unittest.TestCase):
    def test_repairs_go_to_the_oldest_goal_home_that_is_not_labelled_backlog(self):
        gh = mock.Mock()
        gh.paginate.return_value = [
            labelled("loom:goal", "loom:backlog", title="Goal: Loom or a person works it"),
            dict(labelled("loom:goal", title="Goal: ship the thing"), number=7)]
        self.assertEqual(brain.repo_goal(gh, REPO), 7)


class Clipping(unittest.TestCase):
    def test_a_short_title_is_kept(self):
        self.assertEqual(naming.generated("Health", "Search is slow"), "Health: Search is slow")
        self.assertEqual(naming.generated("Land or supersede #9", ""), "Land or supersede #9")

    def test_a_long_title_is_cut_on_a_word_with_the_lead_whole(self):
        title = naming.generated("Land or supersede #12345", LONG)
        self.assertLessEqual(len(title), naming.TITLE_MAX)
        self.assertTrue(title.startswith("Land or supersede #12345: Make every lane"))
        self.assertTrue(title.endswith("..."))
        self.assertIn(title[:-3].rsplit(" ", 1)[1], LONG.split())

    def test_one_long_word_is_cut_mid_word(self):
        self.assertEqual(len(naming.clip("x" * 200)), naming.TITLE_MAX)


class GeneratedTitles(unittest.TestCase):
    def test_an_adoption_title_fits_72_with_its_lead_phrase(self):
        title = station.Station._adoption_title(REPO, {"number": 4321, "title": LONG})
        self.assertLessEqual(len(title), naming.TITLE_MAX)
        self.assertTrue(title.startswith("Land or supersede #4321: Make every lane"))

    def test_a_health_title_fits_72_with_its_lead_phrase(self):
        title = health.ticket_title(types.SimpleNamespace(title=LONG))
        self.assertLessEqual(len(title), naming.TITLE_MAX)
        self.assertTrue(title.startswith("Health: Make every lane"))

    def test_a_ticket_title_over_100_characters_is_refused(self):
        body = "\n".join(brain.SECTIONS)
        with self.assertRaisesRegex(ValueError, "at most 100 characters"):
            brain._check_brief_args("x" * 101, body, None, None, None)


class Display(unittest.TestCase):
    def test_a_tickets_goal_group_drops_the_kind_word(self):
        out = tickets_table._group(
            REPO + "#3", [], goal_titles={REPO + "#3": "Loom goal: every lane run leaves a transcript"},
            entered={}, now=0.0, ticket_href=lambda row: "", short_ref=lambda ref: ref, linked=False)
        self.assertIn(">every lane run leaves a transcript<", out)
        self.assertNotIn("Loom goal:", out)

    def test_a_goal_card_drops_kind_words_from_the_goal_its_epics_and_smaller_goals(self):
        goal, child = REPO + "#3", REPO + "#4"
        data = {goal: {"title": "Backlog: every open issue is worked",
                       "epics": [{"ref": REPO + "#5", "title": "[EPIC] Lanes report spend",
                                  "open": True, "tickets": 1, "in_flight": 0}]},
                child: {"title": "Goal home: the dashboard is fast"}}
        out = server._goal_card(goal, [child], {}, data, "not read",
                                datetime(2030, 1, 1, tzinfo=timezone.utc))
        self.assertIn("<h2>every open issue is worked</h2>", out)
        self.assertIn(">Lanes report spend<", out)
        self.assertIn(">the dashboard is fast<", out)
        for word in ("Backlog:", "[EPIC]", "Goal home:"):
            self.assertNotIn(word, out)

    def test_a_backlog_epic_row_drops_the_kind_word(self):
        out = server._epic_row({"repo": REPO, "number": 5, "title": "Epic: Lanes report spend",
                                "html_url": "https://github.com/%s/issues/5" % REPO})
        self.assertIn("Lanes report spend", out)
        self.assertNotIn("Epic:", out)


if __name__ == "__main__":
    unittest.main()
