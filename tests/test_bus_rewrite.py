"""Agent rewrites of loom:triage issues (T224), with a fake agent command and
a fake GitHub that records every request."""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shlex
import sys
import tempfile
import time
import unittest
from unittest import mock
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain, rewrite, station  # noqa: E402
from bus.github import GitHub  # noqa: E402

REPO = "example-owner/example-repo"
GOAL = 30
# The goal's intake epic, where rewrites file their tickets (decision 0003).
INTAKE = 31
BODY = ("## Why\nThe page is slow for every visitor.\n\n## What\nCache the query.\n\n"
        "## Acceptance criteria\n- [ ] The page loads in under a second.\n"
        "- [ ] A test fails without the cache.\n- [ ] The query runs once a minute.\n\n"
        "## Size\nS\n\n## Out of scope\n- Other pages.\n")
BRIEF = {"action": "brief", "title": "Cache the page query", "difficulty": "easy",
         "body": BODY}


def reply(status=200, body=None):
    return status, {}, b"" if body is None else json.dumps(body).encode()


class FakeGitHub:
    def __init__(self, private=False):
        self.private = private
        self.requests = []
        self.triage = {}
        self.created = []
        self.comments = []
        self.labels = []
        self.removed = []
        self.linked = []

    @staticmethod
    def intake():
        return {"number": INTAKE, "id": 1000 + INTAKE, "state": "open",
                "title": brain.INTAKE_TITLE, "labels": [{"name": "epic"}],
                "body": "Done when: full.\n\nSize: S\n\nPart of #%d\n" % GOAL}

    def add_triage(self, number, created=None):
        self.triage[number] = {
            "number": number, "title": "Old issue %d" % number, "body": "It is slow.",
            "state": "open", "user": {"login": "someone"},
            "created_at": created or "2030-01-%02dT00:00:00Z" % (number % 28 + 1),
            "labels": [{"name": "loom:triage"}]}

    def __call__(self, method, url, headers, body):
        parts = urlsplit(url)
        self.requests.append((method, parts.path + ("?" + parts.query if parts.query else "")))
        data = json.loads(body) if body else None
        path = parts.path
        if path == "/repos/%s" % REPO:
            return reply(200, {"private": self.private})
        m = re.fullmatch(r"/repos/%s/(.*)" % re.escape(REPO), path)
        rest = m.group(1) if m else ""
        if method == "GET" and rest == "issues":
            return reply(200, sorted(self.triage.values(), key=lambda i: i["created_at"]))
        if method == "POST" and rest == "issues":
            self.created.append(data)
            return reply(201, {"id": 9000 + len(self.created), "number": 500 + len(self.created),
                               "html_url": "https://github.com/%s/issues/%d" % (REPO, 500)})
        return self._issue_request(method, path, rest, data)

    def _issue_request(self, method, path, rest, data):
        m = re.fullmatch(r"issues/(\d+)(?:/(\w+)(?:/(.*))?)?", rest)
        if not m:
            raise AssertionError("unexpected call %s %s" % (method, path))
        number, sub = int(m.group(1)), m.group(2)
        if number == GOAL:
            if method == "GET" and sub is None:
                return reply(200, {"number": GOAL, "state": "open", "body": "",
                                   "labels": [{"name": "loom:goal"}]})
            if sub == "sub_issues":
                return reply(201, {}) if method == "POST" else reply(200, [self.intake()])
        if number == INTAKE:
            if method == "GET" and sub is None:
                return reply(200, self.intake())
            if sub == "sub_issues":
                if method == "POST":
                    self.linked.append(data["sub_issue_id"])
                    return reply(201, {})
                return reply(200, [])
        if method == "GET" and sub is None:
            return reply(200, self.triage[number])
        if method == "GET" and sub == "comments":
            return reply(200, [])
        if method == "POST" and sub == "comments":
            self.comments.append((number, data["body"]))
            return reply(201, {})
        if method == "POST" and sub == "labels":
            self.labels.append((number, data["labels"]))
            return reply(201, [])
        if method == "DELETE" and sub == "labels":
            self.removed.append((number, m.group(3)))
            return reply(200, [])
        raise AssertionError("unexpected call %s %s" % (method, path))

    def read_comments(self):
        """The issue numbers whose comments were read, in order."""
        return [int(m.group(1)) for method, path in self.requests if method == "GET"
                for m in [re.search(r"/issues/(\d+)/comments", path)] if m]

    def writes_to(self, number):
        mine = re.compile(r"/repos/%s/issues/%d(/|$)" % (re.escape(REPO), number))
        return [(m, p) for m, p in self.requests if m != "GET" and mine.match(p)]


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = os.path.join(self._tmp.name, "loom")
        self.fake = FakeGitHub()
        self.start = time.time()

    def agent(self, answer, name="agent.py"):
        """A command line that reads stdin and prints the answer (text, or a
        dict printed as JSON); the prompt it was given goes to prompt.txt."""
        script = os.path.join(self._tmp.name, name)
        prompt = os.path.join(self._tmp.name, name + ".prompt")
        text = answer if isinstance(answer, str) else json.dumps(answer)
        with open(script, "w") as fh:
            fh.write("import sys\nopen(%r, 'a').write(sys.stdin.read())\nprint(%r)\n"
                     % (prompt, text))
        self.prompt_file = prompt
        return "%s %s" % (shlex.quote(sys.executable), shlex.quote(script))

    def gh(self):
        return GitHub("token", transport=self.fake)

    def station(self, cmd="", private_cmd="", now=0):
        settings = station.Settings(self.home, [REPO], ["a"], ["example-owner"],
                                    rewrite_cmd=cmd, rewrite_private_cmd=private_cmd)
        path = os.path.join(self.home, "state", "station", "triage.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump({"_scanned": self.start, "%s#%d" % (REPO, GOAL): self.start}, fh)
        return station.Station(self.gh(), settings, now=self.start + now)

    def run_station(self, st):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            st.rewrite_triaged()
        return err.getvalue()


class RewriteIssueTest(Base):
    def rewrite(self, answer, number=7):
        self.fake.add_triage(number)
        cmd = self.agent(answer)
        return rewrite.rewrite_issue(self.gh(), REPO, GOAL, self.fake.triage[number], cmd)

    def test_a_brief_answer_files_a_queued_ticket_that_refs_the_original(self):
        self.assertEqual(self.rewrite(BRIEF), "brief")
        (ticket,) = self.fake.created
        self.assertEqual(ticket["title"], "Cache the page query")
        self.assertEqual(ticket["labels"], ["loom:ticket", "loom:queued"])
        self.assertTrue(ticket["body"].startswith("Difficulty: easy\n"))
        self.assertIn("Rewritten from issue 7.", ticket["body"])
        # Filed under the goal's intake epic: its sub-issue, named by Part of.
        self.assertTrue(ticket["body"].endswith("\n\nPart of #%d\n" % INTAKE))
        self.assertEqual(self.fake.linked, [9001])
        self.assertEqual(self.fake.labels, [(7, ["loom:rewritten"])])
        self.assertEqual(self.fake.removed, [(7, "loom%3Atriage")])

    def test_the_prompt_holds_the_issue_and_the_brief_format(self):
        self.rewrite(BRIEF)
        with open(self.prompt_file) as fh:
            prompt = fh.read()
        self.assertIn("Old issue 7", prompt)
        self.assertIn("It is slow.", prompt)
        self.assertIn("## Acceptance criteria", prompt)
        self.assertIn("Difficulty: easy | medium | hard", prompt)
        self.assertIn("Do not write header lines such as Priority, Difficulty, After or Not before", prompt)

    def test_missing_brief_format_section_writes_nothing_and_logs_why(self):
        self.fake.add_triage(7)
        docs = os.path.join(self._tmp.name, "BRAIN.md")
        with open(docs, "w") as fh:
            fh.write("# Brain\n")
        with mock.patch.object(rewrite, "DOCS", docs):
            err = self.run_station(self.station(cmd=self.agent(BRIEF)))
        self.assertIn("docs/BRAIN.md has no '## Ticket issues' section", err)
        self.assertEqual(self.fake.created, [])
        self.assertEqual(self.fake.writes_to(7), [])

    def test_the_answer_may_come_in_a_code_fence_with_chatter(self):
        text = "Here you go:\n```json\n%s\n```\nDone." % json.dumps(BRIEF)
        self.assertEqual(self.rewrite(text), "brief")

    def test_a_human_answer_is_left_to_the_brain_with_no_comment(self):
        # 2026-10-18: the free rewrite model still labelled factory work as
        # person-only after #843; each loom:human reaches the operator's
        # digest. The model proposes, the brain decides.
        answer = {"action": "human", "kind": "money", "ask": "Buy the domain name for the site."}
        self.assertEqual(self.rewrite(answer), "human")
        self.assertEqual(self.fake.labels, [(7, ["needs-brain"])])
        self.assertEqual(self.fake.comments, [])
        self.assertEqual(self.fake.created, [])

    def test_an_issue_left_to_the_brain_is_not_rewritten_again(self):
        self.fake.add_triage(7)
        self.fake.triage[7]["labels"] = [{"name": "loom:triage"}, {"name": "needs-brain"}]
        self.assertEqual(rewrite.candidates(self.gh(), REPO), [])

    def test_a_human_answer_without_a_person_only_kind_writes_nothing(self):
        # 2026-10-18: a free model parked "Using GitHub credentials, locate the
        # 25 tickets..." and "Diagnose the station's 403..." as waiting on the
        # operator. The kind names why only a person can do it.
        self.assert_writes_nothing({"action": "human", "ask": "Diagnose the 403."})
        self.assert_writes_nothing({"action": "human", "kind": "github",
                                    "ask": "Using GitHub credentials, sort the tickets."})

    def test_the_prompt_says_what_is_not_a_human_task(self):
        prompt = rewrite.build_prompt({"title": "t", "body": "b", "labels": []}, [], "FORMAT")
        self.assertIn("Never use human for work a coding agent or the factory can do", prompt)
        self.assertIn('"kind": "money|hardware|account|credential|physical|decision"', prompt)

    def test_a_skip_answer_writes_nothing(self):
        self.assertEqual(self.rewrite({"action": "skip", "reason": "unclear"}), "skip")
        self.assertEqual(self.fake.writes_to(7), [])
        self.assertEqual(self.fake.created, [])

    def assert_writes_nothing(self, answer):
        with self.assertRaises(rewrite.Invalid):
            self.rewrite(answer)
        self.assertEqual(self.fake.created, [])
        self.assertEqual(self.fake.writes_to(7), [])

    def test_invalid_json_writes_nothing(self):
        self.assert_writes_nothing("I could not decide, sorry.")
        self.assert_writes_nothing('{"action": "brief", "title": ')

    def test_a_body_missing_a_section_writes_nothing(self):
        self.assert_writes_nothing(dict(BRIEF, body=BODY.replace("## Size\nS\n\n", "")))

    def test_a_body_without_checkboxes_writes_nothing(self):
        self.assert_writes_nothing(dict(BRIEF, body=BODY.replace("- [ ] ", "- ")))

    def test_a_prose_live_criterion_writes_nothing(self):
        prose = BODY.replace("- [ ] ", "- [ ] Live: the logs show it working after deploy\n- [ ] ", 1)
        self.assert_writes_nothing(dict(BRIEF, body=prose))

    def test_a_bad_difficulty_writes_nothing(self):
        self.assert_writes_nothing(dict(BRIEF, difficulty="impossible"))

    def test_a_body_with_header_lines_writes_nothing(self):
        self.assert_writes_nothing(dict(BRIEF, body="Model: x\n\n" + BODY))

    def test_text_the_brain_must_not_post_writes_nothing(self):
        self.assert_writes_nothing(dict(BRIEF, body=BODY + "\nThis fixes #7\n"))

    def test_an_empty_ask_writes_nothing(self):
        self.assert_writes_nothing({"action": "human", "ask": "  "})

    def test_an_unknown_action_writes_nothing(self):
        self.assert_writes_nothing({"action": "close"})

    def test_a_failing_command_writes_nothing(self):
        self.fake.add_triage(7)
        with self.assertRaises(rewrite.Invalid):
            rewrite.rewrite_issue(self.gh(), REPO, GOAL, self.fake.triage[7],
                                  "%s -c 'raise SystemExit(3)'" % shlex.quote(sys.executable))
        self.assertEqual(self.fake.writes_to(7), [])

    def test_a_failing_command_includes_its_last_stderr_line(self):
        self.fake.add_triage(7)
        cmd = "%s -c 'import sys; print(\"diagnostic reason\", file=sys.stderr); raise SystemExit(3)'" % shlex.quote(sys.executable)
        with self.assertRaises(rewrite.Invalid) as caught:
            rewrite.rewrite_issue(self.gh(), REPO, GOAL, self.fake.triage[7], cmd)
        self.assertIn("diagnostic reason", str(caught.exception))

    def test_no_request_closes_retitles_or_edits_the_original(self):
        self.rewrite(BRIEF)
        self.rewrite({"action": "human", "kind": "money", "ask": "Buy the domain name."}, number=8)
        for method, path in self.fake.requests:
            self.assertNotIn(method, ("PATCH", "PUT"), path)
            if method == "DELETE":
                self.assertRegex(path, r"/issues/\d+/labels/loom%3Atriage$")
        originals = self.fake.writes_to(7) + self.fake.writes_to(8)
        self.assertEqual({m for m, _ in originals}, {"POST", "DELETE"})
        for _, path in originals:
            self.assertRegex(path, r"/(comments|labels)(/loom%3Atriage)?$")


class StationRewriteTest(Base):
    def test_nothing_runs_without_a_command(self):
        self.fake.add_triage(7)
        self.run_station(self.station())
        self.assertEqual(self.fake.requests, [])

    def test_a_public_repo_uses_the_public_command(self):
        self.fake.add_triage(7)
        err = self.run_station(self.station(cmd=self.agent(BRIEF)))
        self.assertEqual(len(self.fake.created), 1)
        self.assertIn("rewrite %s#7: brief" % REPO, err)

    def test_a_goal_at_its_ticket_cap_runs_no_agent(self):
        # 17 of 72 rewrites ran the model only for intake to refuse the brief
        # at the goal's in-flight cap (2026-10-21).
        self.fake.add_triage(7)
        with mock.patch.object(brain, "_goal_in_flight", return_value=brain.goal_cap()):
            err = self.run_station(self.station(cmd=self.agent(BRIEF)))
        self.assertFalse(os.path.exists(self.prompt_file))
        self.assertEqual(self.fake.created, [])
        self.assertIn("tickets in flight", err)

    def test_a_private_repo_with_an_empty_private_command_is_skipped(self):
        self.fake.private = True
        self.fake.add_triage(7)
        err = self.run_station(self.station(cmd=self.agent(BRIEF)))
        self.assertEqual(self.fake.created, [])
        self.assertFalse(os.path.exists(self.prompt_file))
        self.assertIn("skipped", err)
        self.assertEqual([r for r in self.fake.requests if "labels=" in r[1]], [])

    def test_a_private_repo_uses_only_the_private_command(self):
        self.fake.private = True
        self.fake.add_triage(7)
        public = self.agent({"action": "skip", "reason": "public ran"}, "public.py")
        public_prompt = self.prompt_file
        self.run_station(self.station(cmd=public, private_cmd=self.agent(BRIEF, "private.py")))
        self.assertFalse(os.path.exists(public_prompt))
        self.assertEqual(len(self.fake.created), 1)

    def test_a_private_repo_refuses_a_training_provider_command(self):
        self.fake.private = True
        self.fake.add_triage(7)
        private = "lanes/rewrite_agent.sh - zai/glm-4.7-flash"
        err = self.run_station(self.station(private_cmd=private))
        self.assertEqual(self.fake.created, [])
        self.assertIn("skipped", err)
        self.assertEqual(self.fake.read_comments(), [])

    def test_a_repo_whose_visibility_cannot_be_read_counts_as_private(self):
        self.fake.add_triage(7)
        base = self.fake

        def transport(method, url, headers, body):
            if urlsplit(url).path == "/repos/%s" % REPO:
                return reply(500, {"message": "down"})
            return base(method, url, headers, body)

        st = self.station(cmd=self.agent(BRIEF))
        st.gh = GitHub("token", transport=transport)
        self.run_station(st)
        self.assertEqual(self.fake.created, [])

    def test_at_most_five_a_goal_an_hour_oldest_first(self):
        for n in range(1, 9):
            self.fake.add_triage(n, created="2030-01-%02dT00:00:00Z" % n)
        cmd = self.agent({"action": "skip", "reason": "later"})
        st = self.station(cmd=cmd)
        self.run_station(st)
        self.assertEqual(self.fake.read_comments(), [1, 2, 3, 4, 5])
        # A later pass in the same hour does nothing more.
        self.fake.requests.clear()
        self.run_station(self.station(cmd=cmd, now=1800))
        self.assertEqual(self.fake.requests, [])

    def test_the_station_wide_hourly_cap_binds_across_passes(self):
        # 2026-10-18: per-goal caps alone let about 80 rewrites through in ten
        # minutes across ten backlog goals, each one emailing the operator.
        for n in range(1, 9):
            self.fake.add_triage(n, created="2030-01-%02dT00:00:00Z" % n)
        cmd = self.agent({"action": "skip", "reason": "later"})
        with mock.patch.object(station, "REWRITE_STATION_PER_HOUR", 3):
            self.run_station(self.station(cmd=cmd))
            self.assertEqual(self.fake.read_comments(), [1, 2, 3])
            self.fake.requests.clear()
            self.run_station(self.station(cmd=cmd, now=600))
            self.assertEqual(self.fake.requests, [])
            self.run_station(self.station(cmd=cmd, now=3700))
            self.assertEqual(self.fake.read_comments(), [4, 5, 6])

    def test_the_next_hour_takes_the_next_five_and_skips_the_ones_just_tried(self):
        for n in range(1, 9):
            self.fake.add_triage(n, created="2030-01-%02dT00:00:00Z" % n)
        cmd = self.agent({"action": "skip", "reason": "later"})
        self.run_station(self.station(cmd=cmd))
        self.fake.requests.clear()
        self.run_station(self.station(cmd=cmd, now=3700))
        self.assertEqual(self.fake.read_comments(), [6, 7, 8])

    def test_a_failed_issue_is_not_tried_again_for_a_day(self):
        self.fake.add_triage(7)
        cmd = self.agent("not json")
        self.run_station(self.station(cmd=cmd))
        self.fake.requests.clear()
        self.run_station(self.station(cmd=cmd, now=3700))
        self.assertEqual(self.fake.read_comments(), [])
        self.run_station(self.station(cmd=cmd, now=station.REWRITE_RETRY + 10))
        self.assertEqual(self.fake.read_comments(), [7])

    def test_a_command_failure_logs_stderr_and_does_not_mark_issue_tried(self):
        self.fake.add_triage(7)
        cmd = "%s -c 'import sys; print(\"agent unavailable\", file=sys.stderr); raise SystemExit(1)'" % shlex.quote(sys.executable)
        st = self.station(cmd=cmd)
        err = self.run_station(st)
        self.assertIn("agent unavailable", err)
        with open(os.path.join(st.state, "rewrite.json")) as fh:
            ledger = json.load(fh)
        key = "%s#%d" % (REPO, GOAL)
        self.assertNotIn("7", ledger[key]["tried"])

    def test_a_backlog_nobody_triaged_is_left_alone(self):
        self.fake.add_triage(7)
        st = self.station(cmd=self.agent(BRIEF))
        os.remove(os.path.join(self.home, "state", "station", "triage.json"))
        self.run_station(st)
        self.assertEqual(self.fake.requests, [])

    def test_the_settings_read_both_commands_from_the_environment(self):
        s = station.Settings.from_env({"LOOM_REWRITE_CMD": " a b ",
                                       "LOOM_REWRITE_PRIVATE_CMD": " c "})
        self.assertEqual((s.rewrite_cmd, s.rewrite_private_cmd), ("a b", "c"))


if __name__ == "__main__":
    unittest.main()

class AnswerShapeTest(unittest.TestCase):
    """An unusable answer is described by its shape, never its words: all
    eight Live restates and 113 rewrites failed with one message on
    2026-10-19, and nothing said what the model had returned."""

    def check(self, output, want):
        with self.assertRaises(rewrite.Invalid) as ctx:
            rewrite.parse_answer(output)
        self.assertIn("the answer holds no JSON object with an action", str(ctx.exception))
        self.assertIn(want, str(ctx.exception))

    def test_each_shape_of_an_unusable_answer_is_named(self):
        self.check("", "it was empty")
        self.check("I cannot help with that request.", "32 characters of text")
        self.check("Here: {not json", "with a { but no JSON")
        self.check('{"line": "Live: x", "kind": "y"}', "a JSON object with keys kind, line")
        self.check('{"line": "Live: x", "leakedSecretName": 1}', "keys line and 1 other key(s)")
        self.check('{"zzz": 1}', "a JSON object with no expected key and 1 other key(s)")

    def test_the_words_of_the_answer_never_appear(self):
        with self.assertRaises(rewrite.Invalid) as ctx:
            rewrite.parse_answer("secret-sounding prose about token abc123")
        self.assertNotIn("abc123", str(ctx.exception))
        self.assertNotIn("prose", str(ctx.exception))
        with self.assertRaises(rewrite.Invalid) as ctx:
            rewrite.parse_answer('{"tokenabc123": 1}')
        self.assertNotIn("abc123", str(ctx.exception))
