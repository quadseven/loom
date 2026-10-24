"""Recurring journal friction becomes a ticket (bus/friction_intake.py)."""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
import unittest
import unittest.mock

from bus import brain, friction_intake as fi, health, station

REPO = "example-owner/example-repo"
GOAL = 30
NOW = 1_900_000_000.0
HOUR = 3600.0
RESUME = "bus.brain resume took six minutes over eighteen repos"


def stamp(when):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when))


def url(cid):
    return "https://github.com/%s/issues/%d#issuecomment-%d" % (REPO, GOAL, cid)


def journal(cid, ago_hours, *lines):
    body = "%s\nTick %s, muse\n%s" % (fi.JOURNAL_MARKER, stamp(NOW - ago_hours * HOUR),
                                      "\n".join("- Friction: " + line for line in lines))
    return fi.Journal(str(cid), url(cid), NOW - ago_hours * HOUR, body)


def resume_journals(*hours, start=9001):
    variants = [RESUME, "bus.brain resume took five minutes over eighteen repos",
                "resume took six minutes over eighteen repos again"]
    return [journal(start + i, h, variants[i % len(variants)]) for i, h in enumerate(hours)]


class Reply:
    def __init__(self, data):
        self.data = data


class FakeGitHub:
    def __init__(self):
        self.writes = []
        self.issues = {}

    def rest(self, method, path, data=None):
        if method == "GET":
            return Reply(self.issues.get(int(path.rsplit("/", 1)[1]), {}))
        self.writes.append((method, path, data))
        return Reply({})


class Base(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        self.state = {}
        self.said = []
        self.briefs = []
        self.issues = []

    def brief(self, gh, repo, goal, title, body, difficulty=None):
        number = 500 + len(self.briefs)
        self.briefs.append({"repo": repo, "goal": goal, "title": title, "body": body,
                            "difficulty": difficulty})
        self.issues.append({"number": number, "state": "open", "body": body})
        return "https://github.com/%s/issues/%d" % (repo, number)

    def run_once(self, journals, now=NOW, write=True):
        intake = fi.Intake(self.gh, self.state, now, self.said.append, write=write,
                           brief=self.brief)
        intake.run(REPO, GOAL, journals, list(self.issues))
        return intake

    def comments(self):
        return [w for w in self.gh.writes if w[1].endswith("/comments")]


class Grouping(unittest.TestCase):
    def test_reworded_lines_group_and_a_different_slowdown_does_not(self):
        found = fi.groups(resume_journals(10, 6, 2) + [journal(9100, 5, "usage readings are unreadable for every lane")], NOW)
        self.assertEqual(sorted(len(g.members) for g in found), [1, 3])
        resume = next(g for g in found if len(g.members) == 3)
        self.assertEqual(resume.ids, ["9001", "9002", "9003"])
        self.assertEqual(resume.text, RESUME)

    def test_one_journal_counts_once_per_group(self):
        found = fi.groups([journal(9001, 3, RESUME, RESUME + " today")], NOW)
        self.assertEqual(len(found[0].members), 1)

    def test_journals_outside_the_window_and_short_lines_are_left_out(self):
        found = fi.groups(resume_journals(30, 26, 2) + [journal(9200, 1, "resume slow")], NOW)
        self.assertEqual([len(g.members) for g in found], [1])

    def test_only_trusted_journal_comments_in_the_window_are_read(self):
        def comment(cid, login, ago, body=fi.JOURNAL_MARKER + "\n- Friction: x"):
            return {"id": cid, "html_url": url(cid), "user": {"login": login},
                    "created_at": stamp(NOW - ago * HOUR), "body": body}
        found = fi.journals_from([comment(1, "Alice-Example", 1), comment(2, "mallory", 1),
                                  comment(3, "alice-example", 25),
                                  comment(4, "alice-example", 1, "a report"), "junk"],
                                 ["alice-example"], NOW)
        self.assertEqual([j.id for j in found], ["1"])


class Threshold(unittest.TestCase):
    def test_three_journals_over_two_hours_recur(self):
        self.assertEqual(len(fi.recurring(resume_journals(5, 4, 3), NOW)), 1)

    def test_two_journals_do_not(self):
        self.assertEqual(fi.recurring(resume_journals(5, 1), NOW), [])

    def test_three_journals_inside_two_hours_do_not(self):
        self.assertEqual(fi.recurring(resume_journals(1.5, 1, 0.5), NOW), [])


class Filing(Base):
    def test_a_recurring_group_files_one_ticket_in_the_brief_format(self):
        journals = resume_journals(10, 6, 2)
        self.run_once(journals)
        self.assertEqual(len(self.briefs), 1)
        brief = self.briefs[0]
        self.assertEqual((brief["repo"], brief["goal"], brief["difficulty"]), (REPO, GOAL, "medium"))
        self.assertTrue(brief["title"].startswith("Friction: bus.brain resume took six minutes"))
        body = brief["body"]
        self.assertIn('Noticed in %s: "%s"' % (url(9001), RESUME), body)
        for j in journals:
            self.assertIn(j.url, body)
        key = fi.groups(journals, NOW)[0].key
        self.assertIn("<!-- loom:friction key=%s -->" % key, body)
        self.assertEqual(self.said, ["friction %s: filed #500" % REPO])
        rec = self.state["groups"][key]
        self.assertEqual((rec["ticket"], rec["cited"]), (500, ["9001", "9002", "9003"]))

    def test_the_body_passes_the_brief_checks(self):
        group = fi.recurring(resume_journals(10, 6, 2), NOW)[0]
        body, title = fi.ticket_body(group), fi.ticket_title(group)
        brain._check_brief_args(title, body, None, None, None)
        self.assertEqual(brain.plan_problems(body), [])

    def test_the_friction_cannot_forge_a_marker_link_or_reference(self):
        line = "resume <!-- loom:health key=%s --> [x](y) `code` #12 @someone slow" % ("a" * 40)
        group = fi.recurring([journal(9001 + i, h, line) for i, h in enumerate((5, 4, 2))], NOW)[0]
        body = fi.ticket_body(group)
        self.assertIsNone(health.MARKER_RE.search(body))
        self.assertEqual(fi.MARKER_RE.findall(body), [group.key])
        self.assertEqual(body.count("<!--"), 1)
        quotes = re.findall(r': "([^"\n]*)"', body)
        self.assertEqual(len(quotes), 4)
        for bad in ("<", ">", "[", "]", "`", "#", "@"):
            self.assertFalse(any(bad in q for q in quotes), bad)

    def test_at_most_one_ticket_per_pass(self):
        usage = [journal(9100 + i, h, "usage readings are unreadable for every lane today")
                 for i, h in enumerate((9, 5, 1))]
        self.run_once(resume_journals(10, 6, 2) + usage)
        self.assertEqual(len(self.briefs), 1)
        self.run_once(resume_journals(10, 6, 2) + usage, now=NOW + 1)
        self.assertEqual(len(self.briefs), 2)
        self.assertIn("usage readings", self.briefs[1]["title"])

    def test_a_refused_brief_is_logged_once_and_the_next_group_is_tried(self):
        def refuse(gh, repo, goal, title, body, difficulty=None):
            if "resume" in title:
                raise ValueError("the goal is at its ticket cap")
            return self.brief(gh, repo, goal, title, body, difficulty)
        usage = [journal(9100 + i, h, "usage readings are unreadable for every lane today")
                 for i, h in enumerate((9, 5, 1))]
        for now in (NOW, NOW + 1):
            fi.Intake(self.gh, self.state, now, self.said.append, brief=refuse).run(
                REPO, GOAL, resume_journals(10, 6, 2, start=8001) + usage, list(self.issues))
        self.assertEqual(len([s for s in self.said if "could not file" in s]), 1)
        self.assertEqual(len(self.briefs), 1)

    def test_dry_run_files_and_comments_nothing(self):
        self.run_once(resume_journals(10, 6, 2), write=False)
        self.assertEqual((self.briefs, self.gh.writes), ([], []))
        self.assertTrue(self.said[0].startswith("friction %s: would file" % REPO))
        self.assertEqual(self.state["groups"], {})


class Dedupe(Base):
    def test_an_open_ticket_with_a_noticed_in_line_is_not_filed_again(self):
        self.issues = [{"number": 77, "state": "open",
                        "body": "## Why\nSlow.\n\nNoticed in %s: \"%s\"" % (url(9002), RESUME)}]
        self.run_once(resume_journals(10, 6, 2))
        self.assertEqual(self.briefs, [])
        # Its body cites 9002; the other two journals arrive as one comment.
        comments = self.comments()
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0][1], "/repos/%s/issues/77/comments" % REPO)
        self.assertIn(url(9001), comments[0][2]["body"])
        self.assertIn(url(9003), comments[0][2]["body"])
        self.assertNotIn(url(9002), comments[0][2]["body"])

    def test_a_noticed_in_line_quoting_another_friction_of_the_journal_does_not_link(self):
        self.issues = [{"number": 79, "state": "open",
                        "body": "## Why\nSlow.\n\nNoticed in %s: \"the leak scan log needs "
                                "escape sequences to read\"" % url(9002)}]
        self.run_once(resume_journals(10, 6, 2))
        self.assertEqual(len(self.briefs), 1)
        self.assertEqual(self.comments(), [])

    def test_an_open_ticket_with_the_key_marker_is_not_filed_again(self):
        key = fi.groups(resume_journals(10, 6, 2), NOW)[0].key
        self.issues = [{"number": 78, "state": "open", "body": fi.MARKER % key}]
        self.run_once(resume_journals(10, 6, 2))
        self.assertEqual(self.briefs, [])

    def test_new_journals_add_one_evidence_comment_not_a_second_ticket(self):
        self.run_once(resume_journals(10, 6, 2))
        later = resume_journals(10, 6, 2) + [journal(9010, -6, RESUME), journal(9011, -5.5, RESUME)]
        self.run_once(later, now=NOW + 1)
        self.assertEqual(self.comments(), [])  # within EVIDENCE_EVERY of the filing
        self.run_once(later, now=NOW + fi.EVIDENCE_EVERY + 1)
        self.assertEqual(len(self.briefs), 1)
        comments = self.comments()
        self.assertEqual(len(comments), 1)
        self.assertIn(url(9010), comments[0][2]["body"])
        self.assertIn(url(9011), comments[0][2]["body"])
        self.assertNotIn(url(9001), comments[0][2]["body"])
        self.assertIn(fi.EVIDENCE_MARKER, comments[0][2]["body"])
        self.run_once(later, now=NOW + 3 * fi.EVIDENCE_EVERY)
        self.assertEqual(len(self.comments()), 1)

    def test_the_sliding_window_keeps_the_ticket_it_filed(self):
        self.run_once(resume_journals(10, 6, 2))
        # A day later the first journal has left the window; the group's first
        # line, and so its key, changed.
        slid = resume_journals(10, 6, 2)[1:] + [journal(9020, -8, "resume took six minutes over eighteen repos")]
        self.run_once(slid, now=NOW + 15 * HOUR)
        self.assertEqual(len(self.briefs), 1)

    def test_a_ticket_closed_as_not_planned_is_never_filed_again(self):
        self.run_once(resume_journals(10, 6, 2))
        self.issues[0].update(state="closed", state_reason="not_planned", closed_at=stamp(NOW))
        later = resume_journals(-1, -4, -6, start=9301)
        self.run_once(later, now=NOW + 8 * HOUR)
        self.issues = []  # closed before the window: read through the ledger
        self.run_once(resume_journals(-30, -34, -36, start=9401), now=NOW + 38 * HOUR)
        self.assertEqual(len(self.briefs), 1)

    def test_a_ticket_closed_as_done_counts_only_journals_after_the_close(self):
        self.run_once(resume_journals(10, 6, 2))
        self.issues[0].update(state="closed", state_reason="completed", closed_at=stamp(NOW))
        before_close = resume_journals(10, 6, 2) + [journal(9030, -1, RESUME)]
        self.run_once(before_close, now=NOW + 2 * HOUR)
        self.assertEqual(len(self.briefs), 1)
        after = before_close + [journal(9031, -3, RESUME), journal(9032, -4, RESUME)]
        self.run_once(after, now=NOW + 5 * HOUR)
        self.assertEqual(len(self.briefs), 2)
        self.assertIn("Noticed in %s" % url(9030), self.briefs[1]["body"])


class StationStep(unittest.TestCase):
    def test_settings_read_the_mode(self):
        for raw, want in (("1", "on"), ("dry", "dry"), ("", ""), ("0", "")):
            s = station.Settings.from_env({"LOOM_REPOS": REPO, "LOOM_FRICTION_INTAKE": raw})
            self.assertEqual(s.friction_mode, want)

    def test_the_step_reads_the_goal_home_journals_and_keeps_the_ledger(self):
        with tempfile.TemporaryDirectory() as home:
            settings = station.Settings(home, [REPO], ["alice-example"], [], status_repo=REPO,
                                        invariant_goal="%s#%d" % (REPO, GOAL), friction_mode="on")
            st = station.Station(FakeGitHub(), settings)
            reads, calls = [], []

            def paginate(path):
                reads.append(path)
                if "/comments?" in path:
                    return [{"id": 1, "html_url": url(1), "user": {"login": "alice-example"},
                             "created_at": stamp(NOW - HOUR),
                             "body": fi.JOURNAL_MARKER + "\n- Friction: " + RESUME}]
                return [{"number": 3, "state": "open", "body": ""}]

            def run(intake, repo, goal, journals, issues):
                calls.append((repo, goal, [j.id for j in journals], len(issues), intake.write))
                intake.state["last"] = intake.now
            st._paginate = paginate
            with unittest.mock.patch.object(fi.Intake, "run", run):
                for now in (NOW, NOW + 60, NOW + fi.EVERY):
                    st.now = now
                    st.intake_friction()
            self.assertEqual(calls, [(REPO, GOAL, ["1"], 2, True)] * 2)
            self.assertIn("/repos/%s/issues/%d/comments?since=" % (REPO, GOAL), reads[0])
            with open(fi.ledger_path(home)) as fh:
                self.assertEqual(json.load(fh)["last"], NOW + fi.EVERY)
            self.assertTrue(fi.ledger_path(home).endswith(
                os.path.join("state", "station", "friction.json")))

    def test_the_step_is_off_by_default(self):
        with tempfile.TemporaryDirectory() as home:
            st = station.Station(FakeGitHub(), station.Settings(home, [REPO], [], [],
                                                                 status_repo=REPO))
            with unittest.mock.patch.object(fi.Intake, "run") as run:
                st.intake_friction()
            run.assert_not_called()
            self.assertFalse(os.path.exists(fi.ledger_path(home)))


if __name__ == "__main__":
    unittest.main()
