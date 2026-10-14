from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import sys
import tempfile
import time
import unittest
import unittest.mock
from urllib.parse import unquote, urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain, rewrite, station, text, tickets  # noqa: E402
from bus.github import GitHub  # noqa: E402

REPO = "example-owner/example-repo"
GOAL = 1
SECTIONS = ("## Why\nUsers wait.\n\n## What\nCache the list.\n\n"
            "## Acceptance criteria\n- [ ] The list loads in under a second.\n\n"
            "## Size\nS\n\n## Out of scope\n- Paging.")
BRIEF = {"action": "brief", "title": "Cache the issue list", "difficulty": "easy",
         "body": SECTIONS}
HUMAN = {"action": "human", "ask": "Buy a test phone for the mobile build."}

# The fake agent: saves the prompt it was given and the environment it ran
# in, then prints the answer file (with some prose around it, as agents do).
AGENT = """
import json, os, sys
answer, seen = sys.argv[1], sys.argv[2]
with open(seen, "a") as fh:
    fh.write(json.dumps({"prompt": sys.stdin.read(), "cwd": os.getcwd(),
                         "files": os.listdir("."),
                         "token": os.environ.get("GITHUB_TOKEN")}) + "\\n")
with open(answer) as fh:
    data = fh.read()
if data == "EXIT":
    sys.exit(3)
print("Here is my answer:")
print(data)
"""


class Repo:
    """A tiny stateful issues API for one repo. Records every call as
    (method, path, payload); any call it does not know fails the test, so a
    PATCH (close, retitle, edit) or a PUT of labels can never pass unseen."""

    def __init__(self, private=False):
        self.private = private
        self.issues = {GOAL: {"number": GOAL, "id": 1001, "state": "open", "title": "Backlog: x",
                              "labels": [{"name": "loom:goal"}], "body": ""}}
        self.comments = {GOAL: []}
        self.subs = []
        self.calls = []
        self.next = 500

    def add(self, number, body="It is slow.", labels=("loom:triage",), comments=()):
        self.issues[number] = {"number": number, "id": 1000 + number, "state": "open",
                               "title": "Old issue %d" % number, "body": body,
                               "created_at": "2030-01-01T00:00:%02dZ" % (number % 60),
                               "labels": [{"name": x} for x in labels]}
        self.comments[number] = [{"user": {"login": "someone"}, "body": c} for c in comments]

    def names(self, number):
        return [x["name"] for x in self.issues[number]["labels"]]

    def writes(self, number=None):
        prefix = "/repos/%s/issues/%d/" % (REPO, number) if number else ""
        return [c for c in self.calls if c[0] != "GET"
                and (number is None or c[1].startswith(prefix)
                     or c[1] == prefix.rstrip("/"))]

    def __call__(self, method, url, headers, body):
        parts = urlsplit(url)
        path, query = parts.path, parts.query
        payload = json.loads(body) if body else None
        self.calls.append((method, path, payload))
        base = "/repos/%s" % REPO
        if method == "GET" and path == base:
            return self.reply(200, {"private": self.private})
        rest = path[len(base) + 1:]
        if method == "GET" and rest == "issues" and "labels=loom:triage" in query:
            found = [i for n, i in sorted(self.issues.items())
                     if i["state"] == "open" and "loom:triage" in self.names(n)]
            return self.reply(200, found)
        if method == "POST" and rest == "issues":
            return self.create(payload)
        return self.issue_call(method, rest, path, payload)

    def issue_call(self, method, rest, path, payload):
        parts = rest.split("/")
        if len(parts) < 2 or parts[0] != "issues" or not parts[1].isdigit():
            raise AssertionError("unexpected %s %s" % (method, path))
        n, sub = int(parts[1]), "/".join(parts[2:])
        if method == "GET" and sub == "":
            return self.reply(200, self.issues[n])
        if method == "GET" and sub == "comments":
            return self.reply(200, self.comments[n])
        if method == "GET" and sub == "sub_issues" and n == GOAL:
            return self.reply(200, self.subs)
        if method == "POST" and sub == "sub_issues" and n == GOAL:
            self.subs.append(next(i for i in self.issues.values()
                                  if i["id"] == payload["sub_issue_id"]))
            return self.reply(201, {})
        if method == "POST" and sub == "comments":
            self.comments[n].append({"body": payload["body"]})
            return self.reply(201, {})
        return self.label_call(method, path, n, sub, payload)

    def label_call(self, method, path, n, sub, payload):
        if method == "POST" and sub == "labels":
            for name in payload["labels"]:
                if name not in self.names(n):
                    self.issues[n]["labels"].append({"name": name})
            return self.reply(200, [])
        if method == "DELETE" and sub.startswith("labels/"):
            name = unquote(sub[len("labels/"):])
            self.issues[n]["labels"] = [x for x in self.issues[n]["labels"] if x["name"] != name]
            return self.reply(200, [])
        raise AssertionError("unexpected %s %s" % (method, path))

    def create(self, payload):
        self.next += 1
        issue = {"number": self.next, "id": 1000 + self.next, "state": "open",
                 "title": payload["title"], "body": payload["body"],
                 "labels": [{"name": x} for x in payload["labels"]],
                 "html_url": "https://github.com/%s/issues/%d" % (REPO, self.next)}
        self.issues[self.next] = issue
        self.comments[self.next] = []
        return self.reply(201, issue)

    @staticmethod
    def reply(status, data):
        return status, {}, json.dumps(data).encode()


class AgentCase(unittest.TestCase):
    """A real command line, run through rewrite.run_agent, whose answer each
    test sets."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        script = os.path.join(self.tmp, "agent.py")
        with open(script, "w") as fh:
            fh.write(AGENT)
        self.answer_path = os.path.join(self.tmp, "answer.txt")
        self.seen_path = os.path.join(self.tmp, "seen.jsonl")
        self.command = " ".join(shlex.quote(x) for x in (
            sys.executable, script, self.answer_path, self.seen_path))
        self.answer(BRIEF)

    def answer(self, value):
        with open(self.answer_path, "w") as fh:
            fh.write(value if isinstance(value, str) else json.dumps(value))

    def seen(self):
        try:
            with open(self.seen_path) as fh:
                return [json.loads(line) for line in fh]
        except FileNotFoundError:
            return []


class RewriteTests(AgentCase):
    def rewrite(self, repo, number):
        gh = GitHub("t", transport=repo)
        issue = repo.issues[number]
        return rewrite.rewrite(gh, REPO, GOAL, issue, self.command)

    def assert_original_untouched(self, repo, number):
        # Only a comment, labels added and loom:triage removed: never a PATCH
        # (close, retitle, edit) and never a PUT replacing its labels.
        for method, path, _ in repo.writes(number):
            if method == "POST":
                self.assertTrue(path.endswith(("/comments", "/labels")), path)
            else:
                self.assertEqual((method, path),
                                 ("DELETE", "/repos/%s/issues/%d/labels/loom%%3Atriage"
                                  % (REPO, number)))
        self.assertEqual(repo.issues[number]["title"], "Old issue %d" % number)
        self.assertEqual(repo.issues[number]["state"], "open")

    def test_a_valid_brief_files_a_queued_ticket_that_refs_the_original(self):
        repo = Repo()
        repo.add(7, comments=["It got worse."])
        outcome = self.rewrite(repo, 7)
        self.assertEqual(outcome.action, "brief")
        ticket = repo.issues[repo.next]
        self.assertEqual(ticket["title"], "Cache the issue list")
        self.assertEqual(sorted(repo.names(repo.next)), ["loom:queued", "loom:ticket"])
        self.assertTrue(ticket["body"].startswith("Difficulty: easy\n\n## Why"))
        self.assertTrue(ticket["body"].endswith("\n\nRefs #7\n"))
        self.assertIn(ticket, repo.subs)
        self.assertEqual(repo.names(7), ["loom:rewritten"])
        self.assertIn("Rewritten as ticket #%d" % repo.next, repo.comments[7][-1]["body"])
        self.assert_original_untouched(repo, 7)
        # The prompt carried the issue, its comments and the ticket format.
        prompt = self.seen()[0]["prompt"]
        for part in ("Old issue 7", "It is slow.", "It got worse.", "loom:triage",
                     "## Acceptance criteria", '"action": "human"'):
            self.assertIn(part, prompt)

    def test_a_human_answer_labels_the_issue_with_the_ask(self):
        repo = Repo()
        repo.add(8)
        self.answer(HUMAN)
        outcome = self.rewrite(repo, 8)
        self.assertEqual(outcome.action, "human")
        self.assertEqual(repo.names(8), ["loom:human"])
        self.assertEqual(repo.comments[8][-1]["body"],
                         brain.HUMAN_MARKER + "\n" + HUMAN["ask"])
        self.assertFalse(any(c[0] == "POST" and c[1].endswith("/repos/%s/issues" % REPO)
                             for c in repo.calls))
        self.assert_original_untouched(repo, 8)

    def test_skip_writes_nothing(self):
        repo = Repo()
        repo.add(9)
        self.answer({"action": "skip", "reason": "A question, not work."})
        self.assertEqual(self.rewrite(repo, 9).action, "skip")
        self.assertEqual(repo.writes(), [])

    def test_invalid_answers_write_nothing(self):
        cases = {
            "not json": "I could not decide.",
            "broken json": '{"action": "brief", "title": ',
            "missing section": dict(BRIEF, body=SECTIONS.replace("## Size\nS\n\n", "")),
            "no checkboxes": dict(BRIEF, body=SECTIONS.replace("- [ ] The", "The")),
            "bad difficulty": dict(BRIEF, difficulty="trivial"),
            "header in body": dict(BRIEF, body="Model: x\n\n" + SECTIONS),
            "field line": dict(BRIEF, body=SECTIONS + "\nRepo: other/repo"),
            "closing keyword": dict(BRIEF, body=SECTIONS + "\nThis fixes #9."),
            "bot mention": dict(BRIEF, body=SECTIONS + "\n@" + text.REVIEW_BOT_SHORT_NAME),
            "two-line title": dict(BRIEF, title="One\nTwo"),
            "long title": dict(BRIEF, title="x" * (rewrite.TITLE_MAX + 1)),
            "unknown action": {"action": "close"},
            "empty ask": {"action": "human", "ask": " "},
        }
        for name, value in cases.items():
            with self.subTest(name):
                repo = Repo()
                repo.add(10)
                self.answer(value)
                self.assertEqual(self.rewrite(repo, 10).action, "invalid")
                self.assertEqual(repo.writes(), [])
                self.assertEqual(repo.names(10), ["loom:triage"])

    def test_a_failing_command_writes_nothing(self):
        repo = Repo()
        repo.add(11)
        self.answer("EXIT")
        outcome = self.rewrite(repo, 11)
        self.assertEqual(outcome.action, "error")
        self.assertIn("exited 3", outcome.detail)
        self.assertEqual(repo.writes(), [])

    def test_the_agent_runs_in_an_empty_directory_without_the_github_token(self):
        repo = Repo()
        repo.add(12)
        with unittest.mock.patch.dict(os.environ, {"GITHUB_TOKEN": "x" * 20}):
            self.rewrite(repo, 12)
        seen = self.seen()[0]
        self.assertIsNone(seen["token"])
        self.assertEqual(seen["files"], [])
        self.assertNotEqual(os.path.realpath(seen["cwd"]), os.path.realpath(os.getcwd()))


class AnswerTests(unittest.TestCase):
    def test_the_last_answer_object_wins_over_prose_and_fences(self):
        out = 'Thinking {"a": 1}\n```json\n%s\n```\n' % json.dumps(BRIEF)
        self.assertEqual(rewrite.parse_answer(out), BRIEF)

    def test_a_brief_check_passes_the_body_brain_brief_would_file(self):
        checked = rewrite.check_answer(BRIEF, 7)
        self.assertEqual(checked["difficulty"], "easy")
        self.assertEqual(brain.missing_sections(checked["body"]), [])
        tickets.parse(checked["body"])

    def test_the_brief_format_comes_from_the_docs(self):
        fmt = rewrite.brief_format()
        self.assertIn("## Acceptance criteria\n- [ ] ...", fmt)
        self.assertTrue(fmt.startswith("Difficulty:"))


class StationRewriteTests(AgentCase):
    def setUp(self):
        super().setUp()
        self.home = os.path.join(self.tmp, "loom")
        self.start = time.time()
        self.settings = station.Settings(self.home, [REPO], ["alice-example"],
                                         ["example-owner"], rewrite_cmd=self.command)

    def run_step(self, repo, offset=0, backlogs=None):
        st = station.Station(GitHub("t", transport=repo), self.settings,
                             now=self.start + offset)
        st._backlogs = {"%s#%d" % (REPO, GOAL): (REPO, GOAL)} if backlogs is None else backlogs
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            st.rewrite_backlogs()
        return err.getvalue()

    def tickets_filed(self, repo):
        return [c for c in repo.calls if c[0] == "POST" and c[1] == "/repos/%s/issues" % REPO]

    def test_a_public_repo_uses_the_public_command(self):
        repo = Repo(private=False)
        repo.add(20)
        err = self.run_step(repo)
        self.assertEqual(repo.names(20), ["loom:rewritten"])
        self.assertIn("rewrite %s#20: brief: https://github.com/" % REPO, err)

    def test_a_private_repo_with_an_empty_private_command_is_skipped(self):
        repo = Repo(private=True)
        repo.add(21)
        err = self.run_step(repo)
        self.assertEqual(self.seen(), [])
        self.assertEqual(repo.writes(), [])
        self.assertIn("LOOM_REWRITE_PRIVATE_CMD is empty", err)
        # Said once, not on every pass.
        self.assertNotIn("LOOM_REWRITE_PRIVATE_CMD", self.run_step(repo, 60))

    def test_a_private_repo_uses_only_the_private_command(self):
        repo = Repo(private=True)
        repo.add(22)
        self.settings.rewrite_cmd = "false"
        self.settings.rewrite_private_cmd = self.command
        self.run_step(repo)
        self.assertEqual(len(self.seen()), 1)
        self.assertEqual(repo.names(22), ["loom:rewritten"])

    def test_at_most_five_per_goal_per_hour_oldest_first(self):
        repo = Repo(private=False)
        for n in range(30, 38):
            repo.add(n)
        self.answer({"action": "skip", "reason": "Not work."})
        for minute in range(10):
            self.run_step(repo, minute * 60)
        prompts = [s["prompt"] for s in self.seen()]
        self.assertEqual(len(prompts), station.REWRITE_PER_HOUR)
        self.assertEqual([next(n for n in range(30, 38) if "Old issue %d\n" % n in p)
                          for p in prompts], [30, 31, 32, 33, 34])
        # The hour after, the rest are tried.
        self.run_step(repo, 3600 + 300)
        self.assertEqual(len(self.seen()), station.REWRITE_PER_HOUR + 1)

    def test_one_pass_runs_at_most_rewrite_per_pass(self):
        repo = Repo(private=False)
        for n in range(40, 44):
            repo.add(n)
        self.answer({"action": "skip", "reason": "Not work."})
        backlogs = {"%s#%d" % (REPO, GOAL): (REPO, GOAL), "%s#99" % REPO: (REPO, GOAL)}
        self.run_step(repo, backlogs=backlogs)
        self.assertEqual(len(self.seen()), station.REWRITE_PER_PASS)

    def test_a_failed_issue_is_not_retried_for_a_day(self):
        repo = Repo(private=False)
        repo.add(50)
        self.answer("not json")
        err = self.run_step(repo)
        self.assertIn("rewrite %s#50: invalid: no JSON object" % REPO, err)
        self.run_step(repo, 3600 + 60)
        self.assertEqual(len(self.seen()), 1)
        path = os.path.join(self.home, "state", "station", "rewrite.json")
        with open(path) as fh:
            self.assertEqual(json.load(fh)["issues"]["%s#50" % REPO]["outcome"], "invalid")
        self.answer(BRIEF)
        self.run_step(repo, station.REWRITE_RETRY + 60)
        self.assertEqual(len(self.seen()), 2)
        self.assertEqual(repo.names(50), ["loom:rewritten"])

    def test_a_goal_at_its_cap_runs_no_agent(self):
        repo = Repo(private=False)
        repo.add(60)
        repo.subs = [{"state": "open", "labels": [{"name": "loom:ticket"}, {"name": "loom:queued"}]}]
        with unittest.mock.patch.dict(os.environ, {"LOOM_GOAL_CAP": "1"}):
            err = self.run_step(repo)
        self.assertEqual(self.seen(), [])
        self.assertIn("at its cap", err)
        self.assertEqual(repo.writes(), [])

    def test_below_the_budget_floor_no_rewrite_runs(self):
        repo = Repo(private=False)
        repo.add(70)
        path = os.path.join(self.home, "state", "station", "budget.json")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as fh:
            json.dump({"rates": {"core": [100, 5000, int(self.start) + 1800]}}, fh)
        self.run_step(repo)
        self.assertEqual(self.seen(), [])

    def test_with_no_command_set_nothing_is_read(self):
        repo = Repo(private=False)
        repo.add(80)
        self.settings.rewrite_cmd = ""
        self.run_step(repo)
        self.assertEqual(repo.calls, [])

    def test_the_commands_come_from_the_environment(self):
        s = station.Settings.from_env({"LOOM_REWRITE_CMD": " codex exec ",
                                       "LOOM_REWRITE_PRIVATE_CMD": ""})
        self.assertEqual((s.rewrite_cmd, s.rewrite_private_cmd), ("codex exec", ""))


if __name__ == "__main__":
    unittest.main()
