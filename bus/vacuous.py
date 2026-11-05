"""Checks for vacuous lookup changes and changed tests that pass on base."""
from __future__ import annotations

import ast
import base64
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from io import BytesIO
from typing import Dict, List, NamedTuple, Optional, Sequence, Set
from pathlib import PurePosixPath, PureWindowsPath
from urllib.parse import quote

BUILTIN_CONTAINERS = frozenset(("dict", "list", "set", "tuple"))
LOOKUP_METHODS = frozenset(("__getitem__", "__contains__", "__missing__", "get"))


class BaseTestResults(NamedTuple):
    passed_on_base: List[str]
    failed_on_base: List[str]
    timed_out: List[str]
    # Changed tests the gate could not run on base (a module-level test that
    # takes fixtures where pytest is absent): the gate fails closed on them.
    not_run: List[str]


# Runs one module-level test function: through pytest when the tree has it,
# else by calling it (awaiting a coroutine). Exit NOT_RUN when neither works.
NOT_RUN = 99
NOT_RUN_CODES = frozenset((4, 5, NOT_RUN))  # pytest usage error, no tests collected
_FUNCTION_RUNNER = """
import asyncio, importlib, importlib.util, inspect, sys
module, name = sys.argv[1], sys.argv[2]
if importlib.util.find_spec("pytest") is not None:
    import pytest
    raise SystemExit(int(pytest.main(["-q", "-p", "no:cacheprovider",
                                      module.replace(".", "/") + ".py::" + name])))
func = getattr(importlib.import_module(module), name)
if inspect.signature(func).parameters:
    raise SystemExit(%d)
result = func()
if inspect.iscoroutine(result):
    asyncio.run(result)
""" % NOT_RUN


def is_test_path(path: str) -> bool:
    """Recognize conventional Python test paths."""
    name = path.rsplit("/", 1)[-1]
    return "tests" in path.split("/") or name.startswith("test_") or name.endswith("_test.py")


def _delegates_lookup(method: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    body = list(method.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body.pop(0)
    if len(body) != 1:
        return False
    statement = body[0]
    if isinstance(statement, ast.Raise):
        exc = statement.exc
        return isinstance(exc, ast.Name) and exc.id == "KeyError"
    if not isinstance(statement, ast.Return) or not isinstance(statement.value, ast.Call):
        return False
    call = statement.value
    return (isinstance(call.func, ast.Attribute)
            and call.func.attr == method.name
            and isinstance(call.func.value, ast.Call)
            and isinstance(call.func.value.func, ast.Name)
            and call.func.value.func.id == "super"
            and not call.func.value.args)


def container_lookup_overrides(source: str, patch: str = "") -> List[str]:
    """Name builtin container subclasses that override lookup behavior."""
    tree = ast.parse(source)
    found = []
    changed_lines = _added_lines(patch) if patch else set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        inherits = any(isinstance(base, ast.Name) and base.id in BUILTIN_CONTAINERS
                       for base in node.bases)
        if not inherits:
            continue
        class_changed = node.lineno in changed_lines
        for member in node.body:
            if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and member.name in LOOKUP_METHODS \
                    and not _delegates_lookup(member) \
                    and (not patch or any(member.lineno <= line <=
                                          getattr(member, "end_lineno", member.lineno)
                                          for line in changed_lines)
                         or class_changed):
                found.append("%s.%s" % (node.name, member.name))
    return sorted(found)


def _changed_identifiers(source: str, patch: str) -> Set[str]:
    """Return changed names declared at module or non-test class scope."""
    changed_lines = _added_lines(patch)
    tree = ast.parse(source)
    changed: Set[str] = set()
    if not changed_lines or 0 in changed_lines:
        for line in patch.splitlines():
            if not line.startswith(("+", "-")) or line.startswith(("+++", "---")):
                continue
            match = re.match(r"[+-](?:async\s+def|def)\s+([A-Za-z_]\w*)", line)
            if not match:
                match = re.match(r"[+-]([A-Za-z_]\w*)\s*(?::[^=]+)?=", line)
            if match and not match.group(1).startswith("test"):
                changed.add(match.group(1))

    def target_names(target: ast.expr) -> Set[str]:
        if isinstance(target, ast.Name):
            return {target.id}
        if isinstance(target, (ast.Tuple, ast.List)):
            return set().union(*(target_names(item) for item in target.elts))
        return set()

    def declaration_names(node: ast.stmt) -> Set[str]:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return {node.name} if not node.name.startswith("test") else set()
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            return set().union(*(target_names(item) for item in targets))
        return set()

    for node in tree.body:
        is_test_class = isinstance(node, ast.ClassDef) and any(
            isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
            and member.name.startswith("test") for member in node.body)
        if (not is_test_class and any(
                node.lineno <= line <= getattr(node, "end_lineno", node.lineno)
                for line in changed_lines)):
            changed.update(declaration_names(node))
        if not isinstance(node, ast.ClassDef):
            continue
        is_test_class = any(isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                            and member.name.startswith("test") for member in node.body)
        if is_test_class:
            continue
        for member in node.body:
            if any(member.lineno <= line <= getattr(member, "end_lineno", member.lineno)
                   for line in changed_lines):
                changed.update(declaration_names(member))
    return changed


def fixture_users(test_source: str, patch: str) -> List[str]:
    """Tests whose source references a fixture or table changed in the patch."""
    changed = _changed_identifiers(test_source, patch)
    if not changed:
        return []
    tree = ast.parse(test_source)
    users = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                or not node.name.startswith("test"):
            continue
        refs = {item.id for item in ast.walk(node) if isinstance(item, ast.Name)}
        refs.update(item.arg for item in ast.walk(node) if isinstance(item, ast.arg))
        if refs & changed:
            users.append(node.name)
    return sorted(set(users))


def diff_findings(code_sources: Dict[str, str], test_sources: Dict[str, str],
                  test_patches: Dict[str, str], ticket_body: str = "",
                  code_patches: Optional[Dict[str, str]] = None,
                  added_test_paths: Optional[Set[str]] = None) -> List[str]:
    """Find lookup overrides and changed fixtures used by changed tests."""
    findings = []
    for path, source in code_sources.items():
        findings.extend("defines %s" % name for name in container_lookup_overrides(
            source, (code_patches or {}).get(path, "")))
    asks_for_fixture = re.search(
        r"\b(?:change|edit|update|add|remove|correct)\b[^\n]{0,100}"
        r"\b(?:fixture|expected table|expected values)\b", ticket_body, re.I)
    if code_sources and not asks_for_fixture:
        for path, source in sorted(test_sources.items()):
            patch = test_patches.get(path, "")
            if path in (added_test_paths or set()) or re.search(
                    r"^@@ -0,0 \+", patch, re.M):
                continue
            for name in fixture_users(source, patch):
                findings.append("changes a fixture used by test %s" % name)
    return findings


def _filename(item: Dict[str, object]) -> str:
    return str(item.get("filename") or "")


def _pull_files(gh: object, repo: str, number: int) -> List[Dict[str, object]]:
    """Every changed-file entry of a pull request, up to 30 pages."""
    entries: List[Dict[str, object]] = []
    for page in range(1, 31):
        data = gh.rest("GET", "/repos/%s/pulls/%d/files?per_page=100&page=%d"
                       % (repo, number, page)).data or []
        batch = [item for item in data if isinstance(item, dict)]
        entries.extend(batch)
        if len(batch) < 100:
            break
    return entries


def pull_findings(gh: object, repo: str, number: int, head: str,
                  ticket_body: str = "") -> List[str]:
    """Read changed Python files from a pull request and inspect their source."""
    files = _pull_files(gh, repo, number)
    patches = {_filename(item): str(item.get("patch") or "") for item in files}
    added_tests = {_filename(item) for item in files
                   if item.get("status") == "added" and is_test_path(_filename(item))}
    code = [path for path in patches if path.endswith(".py") and not is_test_path(path)]
    if not code:
        return []
    tests = [path for path in patches if is_test_path(path)]
    return diff_findings(_read_sources(gh, repo, code, head),
                         _read_sources(gh, repo, tests, head),
                         {path: patches[path] for path in tests}, ticket_body,
                         {path: patches[path] for path in code}, added_tests)


def _read_sources(gh: object, repo: str, paths: Sequence[str], head: str) -> Dict[str, str]:
    found = {}
    for path in paths:
        response = gh.rest("GET", "/repos/%s/contents/%s?ref=%s" % (
            repo, quote(path, safe="/"), quote(head, safe="")), ok=(404,))
        data = response.data
        content = data.get("content") if isinstance(data, dict) else None
        if isinstance(content, str):
            found[path] = base64.b64decode(content).decode("utf-8", "replace")
    return found


def safe_test_path(path: str) -> str:
    """Validate a repository-relative test path before using it on disk."""
    posix = PurePosixPath(path)
    windows = PureWindowsPath(path)
    if (not path or posix.is_absolute() or windows.is_absolute()
            or ".." in path.replace("\\", "/").split("/")
            or "\x00" in path):
        raise ValueError("unsafe test path")
    return str(posix)


def _module_name(path: str) -> str:
    module = path[:-3].replace("/", ".") if path.endswith(".py") else ""
    if not module or any(not part.isidentifier() for part in module.split(".")):
        return ""
    return module


def _top_level_names(path: str, source: str) -> List[str]:
    """Module-level test functions (pytest style), as module.function."""
    module = _module_name(path)
    if not module:
        return []
    return sorted({"%s.%s" % (module, node.name) for node in ast.parse(source).body
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and node.name.startswith("test")})


def _test_names(path: str, source: str) -> List[str]:
    module = _module_name(path)
    if not module:
        return []
    tree = ast.parse(source)
    names = _top_level_names(path, source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for member in node.body:
            if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and member.name.startswith("test"):
                names.append("%s.%s.%s" % (module, node.name, member.name))
    return sorted(set(names))


def _added_lines(patch: str) -> Set[int]:
    lines = patch.splitlines()
    added: Set[int] = set()
    current = 0
    for line in lines:
        if line.startswith("@@"):
            match = re.search(r"\+(\d+)(?:,(\d+))?", line)
            current = int(match.group(1)) if match else 0
        elif line.startswith("+++") or line.startswith("---"):
            continue
        elif line.startswith("+"):
            added.add(current)
            current += 1
        elif line.startswith("-"):
            added.add(current)
        elif line.startswith(" "):
            current += 1
    return added


def changed_test_names(path: str, source: str, patch: str) -> List[str]:
    """Name changed tests and tests that use a changed fixture in a file."""
    module = path[:-3].replace("/", ".")
    tree = ast.parse(source)
    changed_lines = _added_lines(patch)
    names = []
    for node in ast.walk(tree):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name.startswith("test")
                and any(node.lineno <= line <= getattr(node, "end_lineno", node.lineno)
                        for line in changed_lines)):
            parents = [parent for parent in ast.walk(tree)
                       if isinstance(parent, ast.ClassDef)
                       and node in parent.body]
            names.append("%s.%s.%s" % (module, parents[0].name, node.name)
                         if parents else "%s.%s" % (module, node.name))
    # Fixture edits can affect an otherwise unchanged test. Include only
    # functions that reference a changed fixture identifier.
    for name in fixture_users(source, patch):
        matches = [target for target in _test_names(path, source)
                   if target.endswith("." + name)]
        names.extend(matches)
    return sorted(set(names))


def _overlay(root: str, sources: Dict[str, str]) -> List[str]:
    names = []
    for raw_path, source in sorted(sources.items()):
        path = safe_test_path(raw_path)
        target = os.path.join(root, *path.split("/"))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(source)
        names.extend(_test_names(path, source))
    return sorted(set(names))


def base_test_results(base_tree: str, changed_tests: Dict[str, str],
                      timeout: int = 120,
                      test_patches: Optional[Dict[str, str]] = None) -> BaseTestResults:
    """Overlay PR test files onto base code and run each test independently."""
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    for path in changed_tests:
        safe_test_path(path)
    passed: List[str] = []
    failed: List[str] = []
    timed_out: List[str] = []
    not_run: List[str] = []
    top_level = {name for path, source in changed_tests.items()
                 for name in _top_level_names(path, source)}
    with tempfile.TemporaryDirectory(prefix="loom-base-tests-") as scratch:
        checkout = os.path.join(scratch, "checkout")
        shutil.copytree(base_tree, checkout,
                        ignore=shutil.ignore_patterns(".git", "__pycache__"))
        _overlay(checkout, changed_tests)
        names = sorted({name for path, source in changed_tests.items()
                        for name in (changed_test_names(path, source,
                                     (test_patches or {}).get(path, ""))
                                     if test_patches is not None else _test_names(path, source))})
        deadline = time.monotonic() + timeout
        for index, name in enumerate(names):
            if name in top_level:
                module, _, function = name.rpartition(".")
                command = [sys.executable, "-B", "-c", _FUNCTION_RUNNER, module, function]
            else:
                command = [sys.executable, "-B", "-m", "unittest", name]
            try:
                run = subprocess.run(command, cwd=checkout, capture_output=True, text=True,
                                     timeout=max(0.01, deadline - time.monotonic()),
                                     check=False)
            except subprocess.TimeoutExpired:
                timed_out.extend(names[index:])
                break
            if name in top_level and run.returncode in NOT_RUN_CODES:
                not_run.append(name)
            else:
                (failed if run.returncode else passed).append(name)
    return BaseTestResults(passed, failed, timed_out, not_run)


def _git(tree: str, *args: str, timeout: int = 30) -> bytes:
    result = subprocess.run(["git", "-C", tree, *args], capture_output=True,
                            check=False, timeout=timeout)
    if result.returncode:
        raise RuntimeError("git command failed: %s" % result.stderr.decode(
            "utf-8", "replace").strip())
    return result.stdout


def _commit(tree: str, revision: str) -> str:
    result = _git(tree, "rev-parse", "--verify", "--end-of-options",
                  revision + "^{commit}")
    value = result.decode("ascii").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", value):
        raise ValueError("revision did not resolve to a commit")
    return value


def _archive(base_tree: str, tree: str, revision: str) -> None:
    raw = _git(tree, "archive", "--format=tar", revision, timeout=60)
    with tarfile.open(fileobj=BytesIO(raw), mode="r:") as archive:
        for member in archive.getmembers():
            path = safe_test_path(member.name)
            target = os.path.join(base_tree, *path.split("/"))
            if member.isdir():
                os.makedirs(target, exist_ok=True)
                continue
            if not member.isfile():
                raise ValueError("base archive contains a non-file entry")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("base archive file could not be read")
            with open(target, "xb") as output:
                shutil.copyfileobj(stream, output)


def git_base_test_results(tree: str, base: str, head: str,
                          timeout: int = 120) -> BaseTestResults:
    """Run only changed PR tests on base code, with each test isolated by name."""
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    base_sha, head_sha = _commit(tree, base), _commit(tree, head)
    raw_paths = _git(tree, "diff", "--no-ext-diff", "--name-only", "-z",
                     "--diff-filter=ACMRT", base_sha, head_sha)
    paths = [safe_test_path(raw.decode("utf-8", "surrogateescape"))
             for raw in raw_paths.split(b"\0") if raw]
    changed_tests: Dict[str, str] = {}
    patches: Dict[str, str] = {}
    for path in paths:
        if not path.endswith(".py") or not is_test_path(path):
            continue
        entry = _git(tree, "ls-tree", "-z", head_sha, "--", path).split(b"\0", 1)[0]
        mode = entry.split(b" ", 1)[0] if entry else b""
        if mode not in (b"100644", b"100755"):
            raise ValueError("changed test path is not a regular file at the head")
        changed_tests[path] = _git(tree, "show", head_sha + ":" + path).decode(
            "utf-8", "strict")
        patches[path] = _git(tree, "diff", "--no-ext-diff", "--no-color",
                             "--unified=0", base_sha, head_sha, "--", path).decode(
                                 "utf-8", "replace")
    if not changed_tests:
        return BaseTestResults([], [], [], [])
    with tempfile.TemporaryDirectory(prefix="loom-vacuous-base-") as scratch:
        checkout = os.path.join(scratch, "base")
        os.mkdir(checkout)
        _archive(checkout, tree, base_sha)
        return base_test_results(checkout, changed_tests, timeout, patches)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI for the lane runner's sandboxed post-check gate."""
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("tree")
    parser.add_argument("base")
    parser.add_argument("head")
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args(argv)
    result = git_base_test_results(args.tree, args.base, args.head, args.timeout)
    for name in result.passed_on_base:
        print("base-pass: " + name)
    for name in result.timed_out:
        print("base-timeout: " + name)
    for name in result.not_run:
        print("base-unrunnable: " + name)
    return 1 if result.passed_on_base or result.timed_out or result.not_run else 0


if __name__ == "__main__":
    raise SystemExit(main())
