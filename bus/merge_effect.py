"""Did a merged pull request change behavior? (loom#1570)

A pull request that changed only docs, or only added functions nothing calls,
left its ticket's work unbuilt, yet the station labelled the ticket
loom:merged. `unbuilt_reason` reads the merged diff and returns why the
ticket is not built, or "" when the change reaches behavior. Best effort: any
doubt (no patch text, a search that failed) reads as built, so a check that
cannot tell never holds a real change back.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from . import change_class

# A definition added in a source file, per language: python, go, shell,
# javascript and typescript.
_DEFS = (
    re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\("),
    re.compile(r"^func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\("),
    re.compile(r"^\s*(?:function\s+)?([A-Za-z_][\w-]*)\s*\(\)\s*\{"),
    re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\("),
)
# Also a symbol a later change can undo: a class or a module constant.
_SYMBOLS = _DEFS + (
    re.compile(r"^\s*(?:export\s+)?class\s+([A-Za-z_]\w*)"),
    re.compile(r"^([A-Z][A-Z0-9_]+)\s*(?::[^=]+)?=[^=]"),
)
_SKIP_NAMES = {"main", "setUp", "tearDown", "init", "__init__"}
_CODE_EXTS = (".py", ".go", ".sh", ".js", ".ts", ".jsx", ".tsx")

Search = Callable[[str], Optional[Sequence[str]]]


def _added(patch: str) -> List[str]:
    return [x[1:] for x in patch.splitlines()
            if x.startswith("+") and not x.startswith("+++")]


def _removed(patch: str) -> List[str]:
    return [x for x in patch.splitlines() if x.startswith("-") and not x.startswith("---")]


def _defined(lines: Sequence[str], defs: Sequence[Any] = _DEFS) -> Dict[str, str]:
    """name -> its defining line, for each definition among the added lines."""
    found: Dict[str, str] = {}
    for line in lines:
        for rx in defs:
            match = rx.match(line)
            if match and match.group(1) not in _SKIP_NAMES \
                    and not match.group(1).startswith("test"):
                found[match.group(1)] = line
                break
    return found


def is_code(path: str) -> bool:
    """A source file whose symbols this module can read."""
    return path.endswith(_CODE_EXTS)


def symbols_in(text: str) -> Set[str]:
    """The symbols a source file's text defines."""
    return set(_defined(text.splitlines(), _SYMBOLS))


def changed_symbols(patch: str) -> Tuple[Set[str], Set[str]]:
    """(added, removed): the symbols a patch adds, and the ones it removes
    and does not add back."""
    added = set(_defined(_added(patch), _SYMBOLS))
    removed = set(_defined([x[1:] for x in _removed(patch)], _SYMBOLS))
    return added, removed - added


def _name(f: Mapping[str, Any]) -> str:
    return str(f.get("filename") or "")


def _non_doc_test_reason(files: Sequence[Mapping[str, Any]]) -> str:
    """The docs-only / docs-and-tests-only reason, or "" when logic changed."""
    if all(change_class.is_docs(_name(f)) for f in files):
        return "it changed only documentation"
    if all(change_class.is_docs(_name(f)) or change_class.is_test(_name(f)) for f in files):
        return "it changed only documentation and tests"
    return ""


def _logic_code_files(files: Sequence[Mapping[str, Any]]) -> Optional[List[Mapping[str, Any]]]:
    """The changed code files when every logic file is code with a patch and
    nothing was removed; None when the diff cannot be judged."""
    logic = [f for f in files
             if not change_class.is_docs(_name(f)) and not change_class.is_test(_name(f))]
    code = [f for f in logic if _name(f).lower().endswith(_CODE_EXTS)]
    if len(code) != len(logic) or any(not f.get("patch") for f in code):
        return None
    if any(_removed(str(f["patch"])) for f in code):
        return None
    return code


def _is_called(name: str, non_def: Sequence[str], code_names: Set[str],
               search: Optional[Search]) -> bool:
    """True when a name is, or may be, called: added elsewhere in the diff, or
    mentioned by a non-test file outside it, or the search could not tell."""
    word = re.compile(r"(?<![\w-])%s(?![\w-])" % re.escape(name))
    if any(word.search(x) for x in non_def):
        return True
    hits = search(name) if search else None
    if hits is None:
        return True
    return any(not change_class.is_test(h) and h not in code_names for h in hits)


def _has_top_level_statement(non_def: Sequence[str], defs: Mapping[str, str]) -> bool:
    """True when the diff adds a top-level line that is not a definition,
    comment, decorator, import or a continuation."""
    return any(x.strip() and not x.lstrip().startswith(("#", "//", "@", "import", "from"))
               and not x.startswith((" ", "\t", "}", ")")) and x not in defs.values()
               for x in non_def)


def unbuilt_reason(files: Sequence[Mapping[str, Any]], search: Optional[Search] = None) -> str:
    """Why a merged diff left its ticket unbuilt, or "" when it did not.
    `files` are the pull request's files (filename, patch); `search(name)`
    returns the paths of repository files that mention a name, or None when it
    cannot tell."""
    if not files:
        return ""
    reason = _non_doc_test_reason(files)
    if reason:
        return reason
    code = _logic_code_files(files)
    if code is None:
        return ""
    adds = [x for f in code for x in _added(str(f["patch"]))]
    defs = _defined(adds)
    if not defs:
        return ""
    non_def = [x for x in adds if x not in defs.values()]
    code_names = {str(f.get("filename")) for f in code}
    if any(_is_called(n, non_def, code_names, search) for n in sorted(defs)):
        return ""
    # Every statement the diff adds outside a definition is a call only to a
    # name already checked, so a diff of other top-level lines is not judged.
    if _has_top_level_statement(non_def, defs):
        return ""
    return "it only added code nothing calls (%s)" % ", ".join(sorted(defs))
