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
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set

from . import change_class

# A definition added in a source file, per language: python, go, shell,
# javascript and typescript.
_DEFS = (
    re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\("),
    re.compile(r"^func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\("),
    re.compile(r"^\s*(?:function\s+)?([A-Za-z_][\w-]*)\s*\(\)\s*\{"),
    re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\("),
)
_SKIP_NAMES = {"main", "setUp", "tearDown", "init", "__init__"}
_CODE_EXTS = (".py", ".go", ".sh", ".js", ".ts", ".jsx", ".tsx")

Search = Callable[[str], Optional[Sequence[str]]]


def _added(patch: str) -> List[str]:
    return [x[1:] for x in patch.splitlines()
            if x.startswith("+") and not x.startswith("+++")]


def _removed(patch: str) -> List[str]:
    return [x for x in patch.splitlines() if x.startswith("-") and not x.startswith("---")]


def _defined(lines: Sequence[str]) -> Dict[str, str]:
    """name -> its defining line, for each definition among the added lines."""
    found: Dict[str, str] = {}
    for line in lines:
        for rx in _DEFS:
            match = rx.match(line)
            if match and match.group(1) not in _SKIP_NAMES \
                    and not match.group(1).startswith("test"):
                found[match.group(1)] = line
                break
    return found


def unbuilt_reason(files: Sequence[Mapping[str, Any]], search: Optional[Search] = None) -> str:
    """Why a merged diff left its ticket unbuilt, or "" when it did not.
    `files` are the pull request's files (filename, patch); `search(name)`
    returns the paths of repository files that mention a name, or None when it
    cannot tell."""
    paths = [str(f.get("filename") or "") for f in files]
    if not paths:
        return ""
    if all(change_class.is_docs(p) for p in paths):
        return "it changed only documentation"
    logic = [f for f in files if not change_class.is_docs(str(f.get("filename") or ""))
             and not change_class.is_test(str(f.get("filename") or ""))]
    if not logic:
        return "it changed only documentation and tests"
    code = [f for f in logic if str(f.get("filename") or "").lower().endswith(_CODE_EXTS)]
    if len(code) != len(logic) or any(not f.get("patch") for f in code):
        return ""
    if any(_removed(str(f["patch"])) for f in code):
        return ""
    adds = [x for f in code for x in _added(str(f["patch"]))]
    defs = _defined(adds)
    if not defs:
        return ""
    non_def = [x for x in adds if x not in defs.values()]
    uncalled: Set[str] = set()
    for name in sorted(defs):
        word = re.compile(r"(?<![\w-])%s(?![\w-])" % re.escape(name))
        if any(word.search(x) for x in non_def):
            return ""
        hits = search(name) if search else None
        if hits is None or any(not change_class.is_test(h) and h not in {
                str(f.get("filename")) for f in code} for h in hits):
            return ""
        uncalled.add(name)
    # Every statement the diff adds outside a definition is a call only to a
    # name already checked, so a diff of other top-level lines is not judged.
    if any(x.strip() and not x.lstrip().startswith(("#", "//", "@", "import", "from"))
           and not x.startswith((" ", "\t", "}", ")")) and x not in defs.values()
           for x in non_def):
        return ""
    return "it only added code nothing calls (%s)" % ", ".join(sorted(uncalled))
