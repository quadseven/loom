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


def _changed_identifiers(patch: str) -> Set[str]:
    changed = set()
    lines = patch.splitlines()
    for index, line in enumerate(lines):
        if not line or line[0] not in "+-" or line.startswith(("+++", "---")):
            continue
        text = line[1:]
        match = re.match(r"\s*(?:async\s+def|def|class)\s+([A-Za-z_]\w*)", text)
        if not match:
            match = re.match(r"\s*([A-Za-z_]\w*)\s*(?::[^=]+)?=", text)
        if match:
            changed.add(match.group(1))
        if line[0] in "+-":
            for context in reversed(lines[max(0, index - 8):index]):
                if context.startswith("@@"):
                    break
                if context.startswith(" "):
                    assignment = re.match(r"\s+([A-Za-z_]\w*)\s*=\s*[\[{(]", context)
                    if assignment:
                        changed.add(assignment.group(1))
                        break
    return changed


def fixture_users(test_source: str, patch: str) -> List[str]:
    """Tests whose source references a fixture or table changed in the patch."""
    changed = _changed_identifiers(patch)
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
                  code_patches: Optional[Dict[str, str]] = None) -> List[str]:
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
            for name in fixture_users(source, test_patches.get(path, "")):
                findings.append("changes a fixture used by test %s" % name)
    return findings


def pull_findings(gh: object, repo: str, number: int, head: str,
                  ticket_body: str = "") -> List[str]:
    """Read changed Python files from a pull request and inspect their source."""
    entries = []
    for page in range(1, 31):
        data = gh.rest("GET", "/repos/%s/pulls/%d/files?per_page=100&page=%d"
                       % (repo, number, page)).data or []
        batch = [item for item in data if isinstance(item, dict)]
        entries.extend(batch)
        if len(batch) < 100:
            break
    code = [str(item.get("filename") or "") for item in entries
            if str(item.get("filename") or "").endswith(".py")
            and not is_test_path(str(item.get("filename") or ""))]
    tests = [item for item in entries if is_test_path(str(item.get("filename") or ""))]
    if not code:
        return []
    code_sources = _read_sources(gh, repo, code, head)
    code_patches = {str(item.get("filename") or ""): str(item.get("patch") or "")
                    for item in entries if str(item.get("filename") or "") in code}
    test_paths = [str(item.get("filename") or "") for item in tests]
    test_sources = _read_sources(gh, repo, test_paths, head)
    patches = {str(item.get("filename") or ""): str(item.get("patch") or "")
               for item in tests}
    return diff_findings(code_sources, test_sources, patches, ticket_body, code_patches)


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


def _test_names(path: str, source: str) -> List[str]:
    module = path[:-3].replace("/", ".") if path.endswith(".py") else ""
    if not module or any(not part.isidentifier() for part in module.split(".")):
        return []
    tree = ast.parse(source)
    names = []
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
            try:
                run = subprocess.run([sys.executable, "-B", "-m", "unittest", name],
                                     cwd=checkout, capture_output=True, text=True,
                                     timeout=max(0.01, deadline - time.monotonic()),
                                     check=False)
            except subprocess.TimeoutExpired:
                timed_out.extend(names[index:])
                break
            (failed if run.returncode else passed).append(name)
    return BaseTestResults(passed, failed, timed_out)


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
        return BaseTestResults([], [], [])
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
    return 1 if result.passed_on_base or result.timed_out else 0


if __name__ == "__main__":
    raise SystemExit(main())
