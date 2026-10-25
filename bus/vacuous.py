"""A vacuous-test gate: a lane pull request whose code bends a lookup so its
own test cannot fail is not ready (loom#1681).

`findings` reads a pull request's changed files (path -> new source text) and
reports one message per finding, each naming the construct that makes a test
vacuous:

* a class that subclasses a builtin container (dict, list, set, ...) and
  overrides a lookup or membership method (__getitem__, __contains__,
  __missing__, get) so a missing key does not raise;
* a test's expected table or fixture edited in the same pull request that
  changes the code under test, unless the ticket asks for it;
* a changed test that passes against the base commit's code (the fix never
  made it fail).

The factory calls it before it marks a pull request ready: a flagged pull
request is sent back as a fix round instead of landing.
"""
from __future__ import annotations

import re
from typing import Callable, Dict, List, Optional, Tuple

# Builtin containers a subclass may bend so a missing key avoids KeyError.
BUILTIN_CONTAINERS = ("dict", "list", "set", "frozenset", "tuple", "str",
                      "bytes", "bytearray")

# Methods whose override lets a missing key or member avoid an error.
LOOKUP_METHODS = ("__getitem__", "__contains__", "__missing__", "get")

_CLASS_RE = re.compile(r"^\s*class\s+([A-Za-z_]\w*)\s*\(\s*([A-Za-z_]\w*)\s*\)")
_DEF_RE = re.compile(r"^\s*def\s+(__getitem__|__contains__|__missing__|get)\b")
_TEST_RE = re.compile(r"^\s*def\s+(test_\w+)\b")
_FIXTURE_RE = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*=\s*[\[{]", re.M)
_CODE_EXTS = (".py", ".js", ".ts", ".tsx", ".jsx", ".css", ".cpp", ".h",
              ".hpp", ".go", ".rs", ".sh", ".lua", ".sql")
_TEST_FOLDERS = {"test", "tests", "t", "spec", "__tests__"}
_TEST_NAME_RE = re.compile(r"^test_|_test\.|\.test\.|\.spec\.")


def _container_overrides(source: str) -> List[Tuple[str, str, str]]:
    """(class, container, method) for each class subclassing a builtin
    container that overrides a lookup or membership method, so a missing key
    does not raise."""
    found: List[Tuple[str, str, str]] = []
    lines = (source or "").splitlines()
    for index, line in enumerate(lines):
        match = _CLASS_RE.match(line)
        if not match or match.group(2) not in BUILTIN_CONTAINERS:
            continue
        cls_name, container = match.group(1), match.group(2)
        base_indent = len(line) - len(line.lstrip())
        for later in lines[index + 1:]:
            stripped = later.lstrip()
            if not stripped:
                continue
            indent = len(later) - len(stripped)
            if indent <= base_indent:
                break
            method = _DEF_RE.match(later)
            if method:
                found.append((cls_name, container, method.group(1)))
    return found


def _fixture_tests(source: str) -> List[str]:
    """The test function names in one test source that holds an expected table
    or fixture (an all-caps constant assigned a mapping or list)."""
    if not _FIXTURE_RE.search(source or ""):
        return []
    return [match.group(1) for match in _TEST_RE.finditer(source or "")]


def _test_names(source: str) -> List[str]:
    """The top-level test function names defined in one test file."""
    return [match.group(1) for match in _TEST_RE.finditer(source or "")]


def _is_code(path: str) -> bool:
    return path.lower().endswith(_CODE_EXTS)


def _is_test(path: str) -> bool:
    folders, _, name = path.lower().rpartition("/")
    return (bool(_TEST_FOLDERS & set(folders.split("/")))
            or name.startswith("test")
            or bool(_TEST_NAME_RE.search(name)))


def findings(changed: Dict[str, str], ticket_body: str = "", pr_body: str = "",
             run_on_base: Optional[Callable[[str], bool]] = None) -> List[str]:
    """Findings for one pull request's changed files.

    `changed` maps a changed file path to its new source text. Returns a
    message per finding, each naming the construct that makes a test vacuous;
    empty when the change is not vacuous. `run_on_base`, when given, is called
    with each changed test file path and must return True when the test passes
    against the base commit's code (so the fix never made it fail).
    """
    out: List[str] = []
    code_files = [path for path in changed if _is_code(path)]
    test_files = [path for path in changed if _is_test(path)]
    ticket_asks = bool(re.search(r"\bfixture|expected|table\b", ticket_body or "", re.I))
    # 1. A subclass of a builtin container that overrides lookup/membership.
    for path in code_files:
        for cls_name, container, method in _container_overrides(changed[path]):
            out.append("subclasses builtin %s and overrides %s so a missing key "
                       "does not raise (class %s in %s)"
                       % (container, method, cls_name, path))
    # 2. A test's expected table or fixture edited alongside the code under
    #    test, unless the ticket asks for it.
    if code_files and test_files and not ticket_asks:
        for path in test_files:
            for name in _fixture_tests(changed[path]):
                out.append("edits the expected table or fixture of test %s in "
                           "the same pull request that changes the code under "
                           "test (%s)" % (name, path))
    # 3. A changed test that passes against the base commit's code.
    if run_on_base is not None and test_files:
        for path in test_files:
            for name in _test_names(changed[path]):
                try:
                    if run_on_base(path):
                        out.append("test %s passes against the base commit's "
                                   "code, so the fix never made it fail (%s)"
                                   % (name, path))
                except OSError:
                    # A test that cannot run against base is not flagged here.
                    continue
    return out
