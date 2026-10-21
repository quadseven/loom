"""Path filters of a repo's deploy workflows (LOOM_SHIP_PATH_WORKFLOWS).

A repo that deploys through many workflows, each scoped to its own paths,
has no single workflow whose success means "shipped". The station reads each
workflow's `on.push.paths` filter and asks, per merged pull request, which
workflows that pull request's files should have triggered.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

WORKFLOWS_DIR = ".github/workflows"
_KEY = re.compile(r"""^(\s*)(?:"([^"]+)"|'([^']+)'|([\w-]+))\s*:\s*(.*)$""")


def _strip(text: str) -> str:
    """A scalar without its trailing comment and quotes."""
    text = re.sub(r"\s+#.*$", "", text.strip())
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _inline_list(text: str) -> Optional[List[str]]:
    text = re.sub(r"\s+#.*$", "", text.strip())
    if not (text.startswith("[") and text.endswith("]")):
        return None
    return [_strip(p) for p in text[1:-1].split(",") if p.strip()]


def push_paths(workflow: str) -> List[str]:
    """The `paths` filter of the workflow's push trigger; [] when it has no
    push trigger or no paths filter (such a workflow is not path-scoped)."""
    lines = workflow.splitlines()
    on_at = push_at = None
    for index, line in enumerate(lines):
        key = _KEY.match(line)
        if key and not key.group(1) and (key.group(2) or key.group(3) or key.group(4)) in ("on", "true"):
            on_at = index
            break
    if on_at is None:
        return []
    on_indent = 0
    push_indent = 0
    for index in range(on_at + 1, len(lines)):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= on_indent:
            break
        key = _KEY.match(line)
        if key and (key.group(2) or key.group(3) or key.group(4)) == "push":
            push_at, push_indent = index, indent
            break
    if push_at is None:
        return []
    found: List[str] = []
    paths_at = None
    for index in range(push_at + 1, len(lines)):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= push_indent:
            break
        key = _KEY.match(line)
        if key and (key.group(2) or key.group(3) or key.group(4)) == "paths":
            inline = _inline_list(key.group(5))
            if inline is not None:
                return inline
            paths_at = (index, indent)
            break
    if paths_at is None:
        return []
    for line in lines[paths_at[0] + 1:]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        item = re.match(r"^\s*-\s+(.*)$", line)
        if not item or indent < paths_at[1]:
            break
        found.append(_strip(item.group(1)))
    return found


def _glob(pattern: str) -> "re.Pattern[str]":
    """A GitHub Actions path glob as a regex: * and ? stay inside a path
    segment, ** crosses them."""
    out, i = [], 0
    while i < len(pattern):
        ch = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        if ch == "*":
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        elif ch == "[" and "]" in pattern[i + 1:]:
            end = pattern.index("]", i + 1)
            body = pattern[i + 1:end]
            out.append("[" + ("^" if body.startswith("!") else "") + body.lstrip("!").replace("\\", "\\\\") + "]")
            i = end
        else:
            out.append(re.escape(ch))
        i += 1
    return re.compile("".join(out))


def matches(patterns: List[str], path: str) -> bool:
    """Whether a changed file passes a paths filter: the last pattern it
    matches decides, and a `!` pattern excludes."""
    hit = False
    for pattern in patterns:
        negated = pattern.startswith("!")
        if _glob(pattern[1:] if negated else pattern).fullmatch(path):
            hit = not negated
    return hit


def scoped_workflows(files: Dict[str, str]) -> Dict[str, List[str]]:
    """Workflow file name to its push paths filter, for each workflow text
    (by file name) that has one."""
    found = {}
    for name, text in files.items():
        patterns = push_paths(text)
        if patterns:
            found[name] = patterns
    return found
