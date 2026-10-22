"""Path filters of a repo's deploy workflows (LOOM_SHIP_PATH_WORKFLOWS).

A repo that deploys through many workflows, each scoped to its own paths,
has no single workflow whose success means "shipped". The station reads each
workflow's `on.push.paths` filter and asks, per merged pull request, which
workflows that pull request's files should have triggered.

The same filters say which changed files merging deploys at once, so a lane
pull request that changes where a workload runs (placement_lines) in such a
file is held for the operator before it merges (bus/operator_hold.py).
"""
from __future__ import annotations

import base64
import re
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import quote

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


def read_workflows(gh: Any, repo: str, branch: str) -> Optional[Dict[str, str]]:
    """Workflow file name to its text, for the repo's workflows on
    `branch`. None when the listing or any workflow file cannot be read."""
    listing = gh.rest("GET", "/repos/%s/contents/%s?ref=%s" % (
        repo, WORKFLOWS_DIR, quote(branch, safe="")), ok=(404,)).data
    if not isinstance(listing, list):
        return None
    texts: Dict[str, str] = {}
    for item in listing:
        name = str(item.get("name") or "") if isinstance(item, dict) else ""
        if not re.fullmatch(r"[\w.-]+\.ya?ml", name):
            continue
        data = gh.rest("GET", "/repos/%s/contents/%s/%s?ref=%s" % (
            repo, WORKFLOWS_DIR, name, quote(branch, safe="")), ok=(404,)).data
        try:
            texts[name] = base64.b64decode(data["content"]).decode("utf-8", "replace")
        except (KeyError, TypeError, ValueError):
            return None
    return texts


def read_filters(gh: Any, repo: str, branch: str) -> Optional[Dict[str, List[str]]]:
    """Workflow file name to its on.push.paths filter, for the repo's
    workflows on `branch` that have one. None when the listing or any
    workflow file cannot be read: with a filter missing, a changed file could
    look as if no deploy covered it."""
    texts = read_workflows(gh, repo, branch)
    return scoped_workflows(texts) if texts is not None else None


def pushes(workflow: str) -> bool:
    """Whether a push runs the workflow: `on: push`, `on: [push, ...]`, or a
    push key under `on:`."""
    lines = workflow.splitlines()
    on_at = next((i for i, indent, line in _content(lines, 0)
                  if not indent and _name(line)[0] in ("on", "true")), None)
    if on_at is None:
        return False
    rest = _name(lines[on_at])[1]
    inline = _inline_list(rest)
    if "push" in (inline if inline is not None else [_strip(rest)]):
        return True
    return _child(lines, on_at, 0, ("push",)) is not None


def push_filters(texts: Dict[str, str]) -> Dict[str, List[str]]:
    """Workflow file name to the paths a push to it covers, for every
    push-triggered workflow: its paths filter, or every path ("**") when it
    has none, since such a workflow runs on any change."""
    found = {}
    for name, text in texts.items():
        patterns = push_paths(text)
        if patterns:
            found[name] = patterns
        elif pushes(text):
            found[name] = ["**"]
    return found


def covering(files: Iterable[str], filters: Dict[str, List[str]]) -> List[str]:
    """The workflows whose filter matches any of the files."""
    files = list(files)
    return sorted(w for w, pats in filters.items() if any(matches(pats, f) for f in files))


def unlisted(files: Iterable[str], listed: List[str]) -> List[str]:
    """The files the repo's .loom/manual-paths does not list as applied by
    hand. A file no workflow covers still counts: an unscoped deploy is never
    taken for no deploy."""
    return sorted(f for f in set(files) if not (listed and matches(listed, f)))


# Keys that decide where a workload runs or what it mounts: a node pin, a
# hostname pin (a JSON patch path spells "/" as "~1"), affinity, tolerations,
# a host address, and volumes and their claims. A change to one moves pods,
# and a local volume does not follow them (2026-10-22).
PLACEMENT_RE = re.compile(
    r"nodeSelector|nodeName|[aA]ffinity|tolerations|topologySpreadConstraints"
    r"|kubernetes\.io(?:/|~1)hostname|hostIP|hostNetwork|\bvolumes\b"
    r"|volumeClaimTemplates|persistentVolumeClaim|claimName|hostPath"
    r"|storageClassName|local-path")
MANIFEST_EXTS = (".yaml", ".yml", ".json")


def _indent(text: str) -> int:
    return len(text) - len(text.lstrip(" -"))


def _code(text: str) -> str:
    """A diff line's YAML without its comment; "" for a comment or blank."""
    stripped = text.strip()
    if not stripped or stripped.startswith("#"):
        return ""
    return re.sub(r"\s+#.*$", "", text)


def _names_placement(line: str, before: List[str]) -> bool:
    """Whether a changed line sets placement: its own key names it, an
    enclosing key in the hunk does, or, for a JSON patch `value:`, the
    `path:` beside it does."""
    code = _code(line)
    if not code:
        return False
    if PLACEMENT_RE.search(code):
        return True
    level = _indent(code)
    own = level
    is_value = code.strip().startswith("value:")
    for prior in reversed(before):
        prior = _code(prior)
        if not prior:
            continue
        depth = _indent(prior)
        sibling = depth == own == level and is_value and prior.strip().startswith("path:")
        if depth < level or sibling:
            if PLACEMENT_RE.search(prior):
                return True
            if depth < level:
                level = depth
        if level == 0:
            break
    return False


def placement_lines(patch: str) -> List[str]:
    """The added or removed lines of a unified diff that change placement
    (PLACEMENT_RE), each read with the lines above it in its hunk on the
    same side. A key outside the hunk's context is not seen."""
    found: List[str] = []
    old: List[str] = []
    new: List[str] = []
    for raw in (patch or "").splitlines():
        if raw.startswith("@@"):
            old, new = [], []
            continue
        kind, text = raw[:1], raw[1:]
        if kind == "+" and _names_placement(text, new):
            found.append(raw)
        elif kind == "-" and _names_placement(text, old):
            found.append(raw)
        if kind in (" ", "-"):
            old.append(text)
        if kind in (" ", "+"):
            new.append(text)
    return found
