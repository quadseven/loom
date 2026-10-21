"""Every new comment Loom posts goes through bus/notify.py (loom#1390).

Loom posted 9,738 new comments on the quadseven repos in the 7 days to
2026-10-21, and a new comment emails the operator. Each routine kind was moved
to the status comment by its own ticket, but a new raw POST to an issue's
comments could still appear and grow a new kind. This module fails on one.
"""

import ast
import re
import unittest
from pathlib import Path

from bus import notify, station, status, tickets
from tests import test_bus_station as fixtures

ROOT = Path(__file__).resolve().parent.parent
# Where a post could live. tests/ is not a place Loom runs.
SCANNED = ("bus", "dashboard", "dispatch", "lanes")
GATEWAY = "bus/notify.py"

# Direct posts that remain, as {(file, command): (count, why)}. The list only
# shrinks: a count here that is higher than what the scan finds fails the test
# too, so the entry is lowered or removed in the pull request that routes the
# post through bus/notify.py.
ALLOWED = {
    # Lane-side comments run in the lane's own shell, which has no station
    # client; loom#1390 leaves them to a separate ticket.
    ("lanes/run_lane.sh", "gh issue comment"): (
        2, "the answer to a ticket, posted by a lane that has no station client"),
}

_SHELL_COMMENT = re.compile(r"""gh\s+(issue|pr)\s+comment\b|["'](issue|pr)["']\s*,\s*["']comment["']""")


def _is_comment_post(node):
    """A call that passes "POST" and a path that names an issue's comments."""
    if not isinstance(node, ast.Call):
        return False
    pieces = list(node.args)
    if not any(isinstance(a, ast.Constant) and a.value == "POST" for a in pieces):
        return False
    for arg in pieces:
        for part in ast.walk(arg):
            if (isinstance(part, ast.Constant) and isinstance(part.value, str)
                    and "comments" in part.value and "/pulls/" not in part.value):
                return True
    return False


def _python_posts(path, rel):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []

    def walk(node, scope):
        for child in ast.iter_child_nodes(node):
            inner = scope
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = scope + [child.name]
            if _is_comment_post(child):
                found.append((rel, ".".join(scope) or "<module>"))
            walk(child, inner)

    walk(tree, [])
    return found


def scan():
    """Direct posts to an issue's comments: python calls as (file, scope) and
    shell or subprocess uses of `gh issue comment` as (file, command)."""
    posts, commands = [], []
    for top in SCANNED:
        for path in sorted((ROOT / top).rglob("*")):
            rel = path.relative_to(ROOT).as_posix()
            if not path.is_file() or rel == GATEWAY or "__pycache__" in rel:
                continue
            if path.suffix == ".py":
                posts.extend(_python_posts(path, rel))
            if path.suffix in (".py", ".sh"):
                for line in path.read_text(encoding="utf-8").splitlines():
                    for match in _SHELL_COMMENT.finditer(line):
                        word = match.group(1) or match.group(2)
                        commands.append((rel, "gh %s comment" % word))
    return posts, commands


class RawCommentPostTest(unittest.TestCase):
    def test_no_module_posts_to_an_issues_comments_outside_the_gateway(self):
        posts, commands = scan()
        self.assertEqual(
            posts, [],
            "a direct POST to an issue's comments: post it with bus/notify.post and "
            "a kind (docs/BRAIN.md, 'Where a new comment is posted'), or edit the "
            "status comment with status.add for a routine event")
        counts = {}
        for key in commands:
            counts[key] = counts.get(key, 0) + 1
        allowed = {key: count for key, (count, _) in ALLOWED.items()}
        self.assertEqual(
            counts, allowed,
            "a `gh issue comment` or `gh pr comment` outside the allowlist, or an "
            "allowlist entry above the count found: the list only shrinks")

    def test_every_allowlist_entry_says_why(self):
        for key, (count, why) in ALLOWED.items():
            self.assertGreater(count, 0, key)
            self.assertGreater(len(why.split()), 3, key)

    def test_the_scan_sees_a_raw_post(self):
        # The scan itself must catch the shapes Loom writes, or the test above
        # passes by looking at nothing.
        for source in (
                'gh.rest("POST", "/repos/%s/issues/%d/comments" % (repo, n), {"body": b})',
                'self.gh.rest("POST", "/repos/%s/issues/%d" % (r, n) + "/comments", {})',
                'self._write(gh, "POST", path + "/comments", {"body": b})'):
            tree = ast.parse(source).body[0].value
            self.assertTrue(_is_comment_post(tree), source)
        for source in (
                'gh.rest("PATCH", "/repos/%s/issues/comments/%d" % (r, n), {})',
                'gh.rest("POST", "/repos/%s/issues/%d/labels" % (r, n), {})',
                'gh.rest("POST", "/repos/%s/pulls/%d/comments/%d/replies" % (r, n, c), {})',
                'gh.rest("GET", "/repos/%s/issues/%d/comments" % (r, n))'):
            tree = ast.parse(source).body[0].value
            self.assertFalse(_is_comment_post(tree), source)
        self.assertTrue(_SHELL_COMMENT.search('gh issue comment "$N" --body x'))
        self.assertTrue(_SHELL_COMMENT.search('subprocess.run(["gh", "pr", "comment", "3"])'))


class GatewayTest(unittest.TestCase):
    class Client:
        def __init__(self):
            self.calls = []

        def rest(self, method, path, body=None, **kw):
            self.calls.append((method, path, body))
            return "response"

    def test_a_kind_outside_the_outcome_set_is_refused_and_posts_nothing(self):
        gh = self.Client()
        for kind in ("routine", "lane-opened-a-pull-request", "", None, "FAILURE"):
            with self.assertRaises(notify.UnknownKind):
                notify.post(gh, "owner/repo", 7, "hello", kind)
        self.assertEqual(gh.calls, [])

    def test_a_known_kind_posts_one_comment_on_the_issue(self):
        for kind in notify.KINDS:
            gh = self.Client()
            self.assertEqual(notify.post(gh, "owner/repo", "7", "hello", kind), "response")
            self.assertEqual(gh.calls, [("POST", "/repos/owner/repo/issues/7/comments",
                                         {"body": "hello"})])

    def test_the_kind_constants_are_the_kinds(self):
        named = {value for name, value in vars(notify).items()
                 if name.isupper() and isinstance(value, str) and name != "KINDS"}
        self.assertEqual(named, set(notify.KINDS))

    def test_every_kind_has_a_row_in_the_brain_doc(self):
        doc = (ROOT / "docs" / "BRAIN.md").read_text(encoding="utf-8")
        section = doc.split("## Where a new comment is posted", 1)[1]
        for kind in notify.KINDS:
            self.assertIn("| `%s` |" % kind, section, kind)


class FirstPullRequestTest(fixtures.Base):
    def test_a_lanes_first_pull_request_is_a_status_event_not_a_new_comment(self):
        self.fake.add(12, state="running")
        name = tickets.ticket_name(fixtures.REPO, 12, 1)
        self.write(self.inbox("codex", "done", name + ".md"),
                   tickets.render_brief(fixtures.REPO, self.fake.issues[12], "codex", 1))
        self.fake.pulls = [{"html_url": "https://example.com/pull/99"}]
        self.run_pass()
        self.run_pass()
        self.assertEqual(self.fake.names(12), ["loom:ticket", "lane:codex", "loom:pr"])
        # One comment, the ticket's status comment, which later events edit.
        self.assertEqual([status.is_status(c["body"]) for c in self.fake.comments[12]], [True])
        self.assertEqual([text for _, text in status.parse(self.fake.comments[12][0]["body"])],
                         ["Lane codex opened a pull request: https://example.com/pull/99"])

    def test_a_first_pull_request_adds_no_comment_to_a_ticket_with_a_status_comment(self):
        self.fake.add(12, state="running")
        self.fake.comments[12] = [{"id": 1, "body": status.render(
            [("2030-01-01T00:00:00Z", "Claimed for lane codex")]),
            "user": {"login": fixtures.AUTHORS[0]}, "created_at": "2030-01-01T00:00:00Z",
            "updated_at": "2030-01-01T00:00:00Z"}]
        name = tickets.ticket_name(fixtures.REPO, 12, 1)
        self.write(self.inbox("codex", "done", name + ".md"),
                   tickets.render_brief(fixtures.REPO, self.fake.issues[12], "codex", 1))
        self.fake.pulls = [{"html_url": "https://example.com/pull/99"}]
        self.run_pass()
        self.assertEqual(len(self.fake.comments[12]), 1)
        self.assertEqual([m for m, p in self.fake.calls
                          if m == "POST" and p.endswith("/issues/12/comments")], [])


if __name__ == "__main__":
    unittest.main()
