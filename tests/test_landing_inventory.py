"""docs/LANDING.md must name every function in bus/ and lanes/ that calls
GitHub's pull request merge endpoint.

The inventory is the base the shared landing decision builds on, so a merge
call added outside it would land work through gates nobody listed. This test
scans the Python under bus/ and lanes/ with ast and the shell under lanes/
line by line, finds each call to the merge endpoint (the REST merge and
merge-async paths, the GraphQL merge mutations, and `gh pr merge`), and fails
by name when the function that makes it is not in the doc's endpoint table.
It also fails when the table names a function that no longer makes one.
"""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "LANDING.md"
SCANNED = ("bus", "lanes")

# A REST path ending in the merge endpoint, written as a format string or
# literal: /repos/<repo>/pulls/<n>/merge or .../merge-async.
REST_PATH = re.compile(r"/pulls/[^/\s\"']*/merge(?:-async)?$")
MUTATION = re.compile(r"\bmutation\b[\s\S]*\b(?:mergePullRequest|enablePullRequestAutoMerge)\s*\(")
SHELL_CALL = re.compile(r"\bgh\s+pr\s+merge\b|/pulls/[^/\s]*/merge(?:-async)?\b")
SHELL_FUNC = re.compile(r"^\s*(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\(\)\s*\{?")
TABLE_ENTRY = re.compile(r"^\|\s*`([^`]+::[^`]+)`")


def _strings(node: ast.AST) -> list[str]:
    """Every string constant inside an expression, so `"..%d" % (a, b)`
    yields its format string; an f-string yields its text with `{}` where
    each field stands."""
    found = []
    for n in ast.walk(node):
        if isinstance(n, ast.JoinedStr):
            found.append("".join(v.value if isinstance(v, ast.Constant) else "{}"
                                 for v in n.values))
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            found.append(n.value)
    return found


def _merge_call(call: ast.Call) -> bool:
    """A call with an argument that names the merge endpoint's path or
    carries a merge mutation."""
    parts = list(call.args) + [k.value for k in call.keywords]
    return any(REST_PATH.search(s) or MUTATION.search(s)
               for p in parts for s in _strings(p))


def python_merge_calls(source: str, rel: str) -> set[str]:
    """`rel::function` for each function whose body calls the merge
    endpoint; a call at module level is `rel::<module>`."""
    tree = ast.parse(source)
    found: set[str] = set()

    def visit(node: ast.AST, owner: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = owner
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = child.name if owner == "<module>" else owner + "." + child.name
            elif isinstance(child, ast.ClassDef):
                name = child.name
            if isinstance(child, ast.Call) and _merge_call(child):
                found.add("%s::%s" % (rel, owner))
            visit(child, name)

    visit(tree, "<module>")
    return found


def shell_merge_calls(source: str, rel: str) -> set[str]:
    """`rel::function` for each shell function with a merge endpoint call
    on a non-comment line; a call outside a function is `rel::<script>`."""
    found: set[str] = set()
    owner = "<script>"
    for line in source.splitlines():
        head = SHELL_FUNC.match(line)
        if head:
            owner = head.group(1)
        elif line.startswith("}"):
            owner = "<script>"
        if not line.lstrip().startswith("#") and SHELL_CALL.search(line):
            found.add("%s::%s" % (rel, owner))
    return found


def tree_merge_calls() -> set[str]:
    found: set[str] = set()
    for top in SCANNED:
        for path in sorted((ROOT / top).rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            rel = path.relative_to(ROOT).as_posix()
            if path.suffix == ".py":
                found |= python_merge_calls(path.read_text(encoding="utf-8"), rel)
            elif path.suffix == ".sh":
                found |= shell_merge_calls(path.read_text(encoding="utf-8"), rel)
    return found


def documented_calls(doc: str) -> set[str]:
    """The `path::function` first cells of the doc's endpoint table."""
    section = doc.split("## Merge endpoint calls", 1)
    if len(section) < 2:
        return set()
    body = section[1].split("\n## ", 1)[0]
    return {m.group(1) for m in map(TABLE_ENTRY.match, body.splitlines()) if m}


class ScannerTest(unittest.TestCase):
    def test_finds_a_rest_merge_call_in_a_function(self):
        src = ("def land(gh, repo, n):\n"
               "    gh.rest('PUT', '/repos/%s/pulls/%d/merge' % (repo, n), {})\n")
        self.assertEqual(python_merge_calls(src, "bus/x.py"), {"bus/x.py::land"})

    def test_finds_an_async_merge_call_in_a_method(self):
        src = ("class Lander:\n"
               "    def go(self, n):\n"
               "        self.gh.rest('PUT', f'/repos/r/pulls/{n}/merge-async')\n")
        self.assertEqual(python_merge_calls(src, "bus/x.py"), {"bus/x.py::Lander.go"})

    def test_finds_a_graphql_merge_mutation(self):
        src = ("def land(gh, node):\n"
               "    gh.graphql('mutation($id:ID!) { mergePullRequest(input:{pullRequestId:$id})"
               " { clientMutationId } }', {'id': node})\n")
        self.assertEqual(python_merge_calls(src, "bus/x.py"), {"bus/x.py::land"})

    def test_ignores_a_read_of_the_pull_request_and_a_path_pattern(self):
        src = ("import re\n"
               "PATH = re.compile(r'^/repos/([^/]+/[^/]+)/pulls/([0-9]+)/(?:merge|merge-async)$')\n"
               "def read(gh, n):\n"
               "    return gh.rest('GET', '/repos/r/pulls/%d' % n)\n")
        self.assertEqual(python_merge_calls(src, "bus/x.py"), set())

    def test_finds_gh_pr_merge_in_a_shell_function_and_skips_comments(self):
        src = ("# gh pr merge is the runner's, never a lane's\n"
               "land() {\n"
               "  gh pr merge \"$PR\" --squash\n"
               "}\n")
        self.assertEqual(shell_merge_calls(src, "lanes/x.sh"), {"lanes/x.sh::land"})

    def test_reads_only_the_endpoint_table(self):
        doc = ("# Landing\n\n## Merge endpoint calls\n\n| Function | Gates |\n|---|---|\n"
               "| `bus/x.py::land` | CI |\n\n## Callers\n\n| `bus/y.py::other` | x |\n")
        self.assertEqual(documented_calls(doc), {"bus/x.py::land"})


class LandingInventoryTest(unittest.TestCase):
    def test_every_merge_endpoint_call_is_in_the_inventory(self):
        missing = tree_merge_calls() - documented_calls(DOC.read_text(encoding="utf-8"))
        self.assertFalse(missing, "docs/LANDING.md does not name these merge endpoint "
                                  "calls: %s" % ", ".join(sorted(missing)))

    def test_the_inventory_names_no_call_that_is_gone(self):
        stale = documented_calls(DOC.read_text(encoding="utf-8")) - tree_merge_calls()
        self.assertFalse(stale, "docs/LANDING.md names merge endpoint calls the tree no "
                                "longer makes: %s" % ", ".join(sorted(stale)))

    def test_the_tree_has_a_merge_call_to_check(self):
        # A scanner that finds nothing would pass the inventory vacuously.
        self.assertIn("bus/brain.py::merge", tree_merge_calls())


if __name__ == "__main__":
    unittest.main()
