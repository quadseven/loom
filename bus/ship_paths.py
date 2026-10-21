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
MANUAL_PATHS_FILE = ".loom/manual-paths"
# A Helm values file: a chart installed by hand deploys nothing when it merges.
HELM_VALUES = re.compile(r"^values(?:[-_.][\w.-]*)?\.ya?ml$")
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


def _content(lines: List[str], start: int):
    """(index, indent, line) of each non-blank, non-comment line from start."""
    for index in range(start, len(lines)):
        line = lines[index]
        if line.strip() and not line.lstrip().startswith("#"):
            yield index, len(line) - len(line.lstrip()), line


def _name(line: str):
    """(key name, rest of line) of a `key: value` line, or (None, "")."""
    key = _KEY.match(line)
    if not key:
        return None, ""
    return key.group(2) or key.group(3) or key.group(4), key.group(5)


def _child(lines: List[str], parent_at: int, parent_indent: int, names):
    """(index, indent, name, rest) of the first key in `names` inside the
    block that starts after line parent_at, or None."""
    for index, indent, line in _content(lines, parent_at + 1):
        if indent <= parent_indent:
            return None
        name, rest = _name(line)
        if name in names:
            return index, indent, name, rest
    return None


def push_paths(workflow: str) -> List[str]:
    """The `paths` filter of the workflow's push trigger; [] when it has no
    push trigger or no paths filter (such a workflow is not path-scoped).
    A paths-ignore filter comes back inverted, as the equivalent paths
    filter: every path counts unless a pattern ignores it."""
    lines = workflow.splitlines()
    on_at = next((i for i, indent, line in _content(lines, 0)
                  if not indent and _name(line)[0] in ("on", "true")), None)
    push = _child(lines, on_at, 0, ("push",)) if on_at is not None else None
    paths = _child(lines, push[0], push[1], ("paths", "paths-ignore")) if push else None
    if not paths:
        return []
    index, indent, name, rest = paths
    found = _inline_list(rest)
    if found is None:
        found = []
        for _, item_indent, line in _content(lines, index + 1):
            item = re.match(r"^\s*-\s+(.*)$", line)
            if not item or item_indent < indent:
                break
            found.append(_strip(item.group(1)))
    return _inverted(found) if name == "paths-ignore" and found else found


def _inverted(ignored: List[str]) -> List[str]:
    """A paths-ignore list as the equivalent paths filter."""
    return ["**"] + [p[1:] if p.startswith("!") else "!" + p for p in ignored]


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
        try:
            compiled = _glob(pattern[1:] if negated else pattern)
        except re.error:
            # A pattern this reader cannot compile counts the workflow as
            # needed, so a ticket never ships past a deploy it cannot rule out.
            return True
        if compiled.fullmatch(path):
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


def listed_manual(text: str) -> List[str]:
    """The path patterns of a repo's .loom/manual-paths: one per line, blank
    lines and # comments ignored. Paths the repo applies by hand."""
    return [line.strip() for line in text.replace("\r\n", "\n").split("\n")
            if line.strip() and not line.strip().startswith("#")]


def manual_files(files: List[str], filters: Dict[str, List[str]],
                 listed: List[str]) -> List[str]:
    """The changed files that merging does not deploy: those the repo's
    manual-paths list matches, and Helm values files no deploy workflow's
    paths filter covers (the chart is installed by hand)."""
    return sorted(f for f in files
                  if (listed and matches(listed, f))
                  or (HELM_VALUES.match(f.rsplit("/", 1)[-1])
                      and not any(matches(pats, f) for pats in filters.values())))
