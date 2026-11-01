"""Every close and reopen goes through bus/closing.py (loom#2444).

A state write outside it can close a thread with no reason, which is what
this module exists to fail on. It reads the code rather than running it, so a
new site fails here whether or not a test reaches it.
"""

import ast
import re
import unittest
from pathlib import Path
from unittest import mock

from bus import shadow

ROOT = Path(__file__).resolve().parent.parent
# Where a state write could live. tests/ is not a place Loom runs.
SCANNED = ("bus", "dashboard", "dispatch", "lanes")
GATEWAY = "bus/closing.py"
# Names the shadow classifier matches on; it sends nothing.
SHADOW = "bus/shadow.py"

# The callers trusted to have put the reason in the thread themselves, as
# {file: count of calls passing said=}. Each posts or edits a note just before.
SAID = {
    "bus/brain.py": 1,        # settle_source: its marked note
    "bus/close_audit.py": 1,  # the refusal, kept across a failed reopen
    "bus/factory.py": 1,      # the split note on the ticket
    "bus/invariants.py": 4,   # the notes edited in place, and an earlier fold or clear
    "bus/station.py": 1,      # the settle-claimed evidence comment
}

_STATE_CALLS = ("close", "close_pull", "reopen")
_MUTATION = re.compile(r"\b(closeIssue|reopenIssue|closePullRequest|reopenPullRequest)\b")
# A shell close: gh issue/pr close or reopen, or a state field on a gh api or curl.
_SHELL_VERB = re.compile(r"\bgh\s+(?:issue|pr)\s+(?:close|reopen)\b")
_SHELL_FIELD = re.compile(r"""(?:\bstate=(?:closed|open)\b|["']?state["']?\s*:\s*["']?(?:closed|open)\b)""")


def _names_state(expr, assigned, seen=None):
    """Whether `expr`, following a name to what its scope assigned, builds a
    body that sets "state". Each name is followed once."""
    seen = set() if seen is None else seen
    for part in ast.walk(expr):
        if isinstance(part, ast.Dict) and any(
                isinstance(k, ast.Constant) and k.value == "state" for k in part.keys):
            return True
        if isinstance(part, ast.Call) and any(k.arg == "state" for k in part.keywords):
            return True
        if isinstance(part, ast.Name) and part.id not in seen:
            seen.add(part.id)
            if any(_names_state(value, assigned, seen) for value in assigned.get(part.id, ())):
                return True
    return False


def _assigned(scope):
    assigned = {}
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assigned.setdefault(target.id, []).append(node.value)
    return assigned


def _scopes(tree):
    """Each function with the module's names under its own, so a body built
    at module level is still followed."""
    top = _assigned(tree)
    functions = [n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for function in functions:
        yield function, {**top, **_assigned(function)}
    yield tree, top


def direct_writes(source):
    """The lines of `source` that write a state with no bus/closing.py between."""
    tree = ast.parse(source)
    found = set()
    for scope, assigned in _scopes(tree):
        for node in ast.walk(scope):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr in ("rest", "_rest") \
                    and any(_names_state(arg, assigned) for arg in node.args[2:]):
                found.add(node.lineno)
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant)
                    and t.slice.value == "state" for t in node.targets) and _writes_body(node):
                found.add(node.lineno)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and "mutation" in node.value and _MUTATION.search(node.value):
                found.add(node.lineno)
    return sorted(found)


def _writes_body(node):
    return isinstance(node.value, ast.Constant) and node.value.value in ("closed", "open")


def shell_writes(text):
    """The lines of a shell file that close or reopen with no --comment, or
    set a state field directly. gh posts --comment first and stops when that
    post fails, the same order as bus/closing.py."""
    found = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        if _SHELL_FIELD.search(line) and re.search(r"\b(gh\s+api|curl)\b", line):
            found.append(index + 1)
        if _SHELL_VERB.search(line):
            command = line
            at = index
            while command.rstrip().endswith("\\") and at + 1 < len(lines):
                at += 1
                command = command.rstrip()[:-1] + " " + lines[at]
            if not re.search(r"""--comment\s+["'][^"']""", command):
                found.append(index + 1)
    return found


def said_calls(source):
    return sum(1 for node in ast.walk(ast.parse(source))
               if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
               and node.func.attr in _STATE_CALLS
               and any(k.arg == "said" for k in node.keywords))


def _files():
    for folder in SCANNED:
        for path in sorted((ROOT / folder).rglob("*")):
            if path.is_file():
                yield path, path.relative_to(ROOT).as_posix()


def _python():
    for path, name in _files():
        if path.suffix == ".py":
            yield name, path.read_text()


def _shell():
    for path, name in _files():
        if path.suffix == ".sh" or (not path.suffix and path.read_bytes()[:2] == b"#!"):
            yield name, path.read_text(errors="replace")


class TheCodeHasNoDirectStateWrite(unittest.TestCase):
    def test_python_writes_state_only_in_the_gateway(self):
        found = {name: lines for name, text in _python()
                 if name not in (GATEWAY, SHADOW)
                 for lines in [direct_writes(text)] if lines}
        self.assertEqual(found, {}, "close or reopen through bus/closing.py")

    def test_the_gateway_is_found_by_the_same_scan(self):
        # bus/closing.py sends its change as a name: the scan must follow it,
        # or an empty result above proves nothing.
        text = (ROOT / GATEWAY).read_text()
        self.assertIn("PATCH", text)
        self.assertEqual(direct_writes(
            'def f(gh, path):\n    change = {"state": "closed"}\n'
            '    gh.rest("PATCH", path, change)\n'), [3])

    def test_shell_closes_say_why(self):
        found = {name: lines for name, text in _shell()
                 for lines in [shell_writes(text)] if lines}
        self.assertEqual(found, {}, "a shell close passes a --comment reason")

    def test_the_shell_scan_sees_the_one_shell_close(self):
        closes = [name for name, text in _shell() if _SHELL_VERB.search(text)]
        self.assertEqual(closes, ["lanes/run_lane.sh"])

    def test_only_the_listed_callers_pass_said(self):
        found = {name: count for name, text in _python()
                 if name != GATEWAY
                 for count in [said_calls(text)] if count}
        self.assertEqual(found, SAID)


class AnAgentsGhClose(unittest.TestCase):
    """The brain rung's model picks its own gh arguments, and bus.shadow
    gh-guard stands in front of its gh, with shadow on or off."""

    def guard(self, *argv):
        with mock.patch.object(shadow, "active", return_value=False), \
                mock.patch("sys.stderr"):
            return shadow.gh_guard(".", list(argv))

    def test_with_no_reason_is_refused(self):
        for argv in (("issue", "close", "5"), ("pr", "close", "5", "--repo", "o/r"),
                     ("issue", "reopen", "5"), ("pr", "reopen", "5"),
                     ("issue", "close", "5", "--comment", " "), ("pr", "close", "5", "-c")):
            self.assertEqual(self.guard(*argv), 1, argv)

    def test_with_a_reason_goes_on(self):
        for argv in (("issue", "close", "5", "--comment", "Done."),
                     ("pr", "close", "5", "-c", "Superseded."),
                     ("issue", "close", "5", "--comment=Done."),
                     ("issue", "list"), ("pr", "view", "5")):
            self.assertEqual(self.guard(*argv), 0, argv)


class TheScanCatches(unittest.TestCase):
    def test_a_direct_patch(self):
        for body in ('{"state": "closed"}', '{"state": "open"}', '{"state": want}',
                     'dict(state="closed")', '{"state": "closed", "body": text}'):
            source = 'def f(gh, want, text):\n    gh.rest("PATCH", "/x", %s)\n' % body
            self.assertEqual(direct_writes(source), [2], body)

    def test_a_body_built_first(self):
        source = ('def f(gh):\n    body = {"title": "t"}\n    body["state"] = "closed"\n'
                  '    gh.rest("PATCH", "/x", body)\n')
        self.assertEqual(direct_writes(source), [3])

    def test_a_body_from_module_level(self):
        source = 'CLOSE = {"state": "closed"}\ndef f(gh):\n    gh.rest("PATCH", "/x", CLOSE)\n'
        self.assertEqual(direct_writes(source), [3])

    def test_a_graphql_mutation(self):
        source = 'Q = """mutation($id: ID!) { closeIssue(input: {issueId: $id}) { clientMutationId } }"""\n'
        self.assertEqual(direct_writes(source), [1])

    def test_a_shell_close_with_no_reason(self):
        self.assertEqual(shell_writes('gh issue close 1\n'), [1])
        self.assertEqual(shell_writes('gh pr reopen 1 --repo o/r\n'), [1])
        self.assertEqual(shell_writes('gh issue close 1 --comment ""\n'), [1])
        self.assertEqual(shell_writes('gh api -X PATCH repos/o/r/issues/1 -f state=closed\n'), [1])

    def test_a_shell_close_with_a_reason(self):
        self.assertEqual(shell_writes('gh issue close 1 \\\n  --comment "Cleared." >>log\n'), [])

    def test_not_a_read_or_a_label(self):
        source = ('def f(gh, item):\n    gh.rest("PATCH", "/x", {"body": "state"})\n'
                  '    gh.rest("GET", "/x?state=closed")\n'
                  '    return item.get("state") == "closed"\n')
        self.assertEqual(direct_writes(source), [])


if __name__ == "__main__":
    unittest.main()
