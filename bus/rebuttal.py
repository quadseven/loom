"""Does a lane's answer to a review finding hold on the pull request head? (loom#2081)

A lane may answer a review finding instead of changing the code. The station
used to resolve such a thread on the answer's word alone, and the brain's
merge gate then read it as settled. On loom#1948 the answer said a dead
function had a caller; on the head it had none, and the dead code merged.

`refusal` is the one check both sides use: the station before it resolves a
thread on an answer, and the brain's merge gate for a thread already resolved
on one. It returns "" when the answer holds on the head, else why not:

- "Fixed in <sha>": the sha must be the head or an ancestor of it.
- Any other answer must cite something the head can show: a file path (with
  an optional :line or :start-end), a line of the thread's file ("line 12",
  "lines 3-5"), or a backticked symbol. Every file must exist on the head,
  every line must be inside its file, and at least one cited symbol must
  appear in a cited file or the thread's file (a library name such as
  `socket.timeout` need not). An answer that cites nothing checkable fails.
- "`f` is called by `g`" (or "it is called from <file:line>"): the head must
  show that call, inside g's body or on that line. The subject of "it" is the
  finding's first backticked symbol.
- A dead-code finding is answered only by a call of its symbol on the head.

The reads go through two callables, so the rules are tested without GitHub:
`read(path)` returns a file's text on the head or None, and
`ancestor(sha)` says whether sha is the head or one of its ancestors.
"""

from __future__ import annotations

import base64
import re
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

Read = Callable[[str], Optional[str]]
Ancestor = Callable[[str], bool]

FIXED_RE = re.compile(r"^\s*Fixed in `?([0-9a-fA-F]{7,40})`?", re.I)
# A repo file path: a known source or text extension (so `mod.NAME` reads as
# a symbol, not a file), optional :line or :start-end.
_EXTS = ("py|pyi|go|sh|bash|js|mjs|cjs|ts|jsx|tsx|rs|rb|java|kt|c|h|cc|cpp|hpp|cs|swift|"
         "md|txt|rst|yml|yaml|json|toml|ini|cfg|conf|sql|html|css|j2|tf|lua|php")
_PATH_RE = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)*[\w-]+(?:\.[\w-]+)*\.(?:%s))"
                      r"(?::(\d+)(?:-(\d+))?)?(?![\w/])" % _EXTS)
_LINE_RE = re.compile(r"\blines?\s+(\d+)(?:\s*(?:-|to|through)\s*(\d+))?", re.I)
_TICKED = re.compile(r"`([^`\n]+)`")
_SYMBOL = re.compile(r"[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)*")
# "`f` is called by `g`" or "it is called from path:line": the caller must be
# backticked or a path, so "it is used in the tests" claims nothing to check.
_CALLED = re.compile(
    r"(?:`([A-Za-z_][\w.]*)(?:\(\))?`|\b(it|this|that)\b)\s+is\s+(?:called|used|invoked)"
    r"\s+(?:by|from|in)\s+(?:`([A-Za-z_][\w.]*)(?:\(\))?`|`?((?:[\w.-]+/)*[\w.-]+\.(?:%s)"
    r"(?::\d+)?)`?)" % _EXTS, re.I)
_DEF_RE = r"^(\s*)(?:async\s+)?(?:def|func|function)\s+%s\b"


def fixed_sha(answer: str) -> str:
    """The sha of a "Fixed in <sha>" answer, else ""."""
    m = FIXED_RE.match(answer or "")
    return m.group(1).lower() if m else ""


def _symbols(text: str) -> List[str]:
    """The backticked symbols in text that are not file paths."""
    out = []
    for raw in _TICKED.findall(text or ""):
        word = raw.strip().removesuffix("()")
        if _SYMBOL.fullmatch(word) and not _PATH_RE.fullmatch(word):
            out.append(word)
    return out


def finding_symbol(finding: str) -> str:
    """The finding's first backticked symbol (its subject), else ""."""
    found = _symbols(finding)
    return found[0] if found else ""


def _kind(finding: str) -> str:
    first = (finding or "").split("\n", 1)[0]
    m = re.search(r"`([a-z]+(?:-[a-z]+)+)`", first)
    return m.group(1) if m else ""


def _calls(text: str, name: str) -> bool:
    return bool(re.search(r"(?<![\w])%s\s*\(" % re.escape(name.split(".")[-1]), text))


def _body(text: str, name: str) -> Optional[str]:
    """The body of the function `name` in text (indent-delimited), else None."""
    lines = text.splitlines()
    pattern = re.compile(_DEF_RE % re.escape(name.split(".")[-1]))
    for i, line in enumerate(lines):
        m = pattern.match(line)
        if not m:
            continue
        indent = len(m.group(1))
        out = []
        for later in lines[i + 1:]:
            if later.strip() and len(later) - len(later.lstrip()) <= indent:
                break
            out.append(later)
        return "\n".join(out)
    return None


def _called_outside_def(text: str, name: str) -> bool:
    short = re.escape(name.split(".")[-1])
    defs = re.compile(_DEF_RE % short)
    return any(_calls(line, name) for line in text.splitlines() if not defs.match(line))


class _Head:
    """The head's files, each read once."""

    def __init__(self, read: Read) -> None:
        self._read = read
        self._files: Dict[str, Optional[str]] = {}

    def text(self, path: str) -> Optional[str]:
        if path not in self._files:
            self._files[path] = self._read(path)
        return self._files[path]


def _cited(answer: str, thread_path: str) -> List[Tuple[str, int, int]]:
    """(path, first line, last line) for each file the answer cites; a bare
    "line N" belongs to the last cited file, else the thread's own file."""
    cited = [(m.group(1), int(m.group(2) or 0), int(m.group(3) or m.group(2) or 0))
             for m in _PATH_RE.finditer(answer)]
    last = cited[-1][0] if cited else thread_path
    if last:
        cited += [(last, int(m.group(1)), int(m.group(2) or m.group(1)))
                  for m in _LINE_RE.finditer(answer)]
    return cited


def _files_refusal(cited: List[Tuple[str, int, int]], head: _Head) -> str:
    for path, start, end in cited:
        text = head.text(path)
        if text is None:
            return "%s does not exist on the head" % path
        if max(start, end) > len(text.splitlines()):
            return "%s has no line %d on the head" % (path, max(start, end))
    return ""


def _symbols_refusal(symbols: List[str], head: _Head, where: List[str]) -> str:
    """Refuse only when none of the cited symbols is on the head: an answer
    may name a library symbol (`socket.timeout`) its cited files never spell,
    and one symbol found there is enough to anchor it. Caller claims and
    dead-code findings are checked strictly elsewhere."""
    def found(symbol: str) -> bool:
        word = re.compile(r"(?<![\w])%s(?![\w])" % re.escape(symbol))
        return any(word.search(head.text(p) or "") for p in where)

    if not symbols or any(found(s) for s in symbols):
        return ""
    return "none of %s is in %s on the head" % (
        ", ".join("`%s`" % s for s in symbols), ", ".join(where) or "any cited file")


def _claims_refusal(answer: str, subject: str, head: _Head, where: List[str]) -> str:
    for m in _CALLED.finditer(answer):
        called = m.group(1) or subject
        if not called:
            return "the answer names a caller but not what it calls"
        reason = _call_refusal(called, m.group(3) or m.group(4), head, where)
        if reason:
            return reason
    return ""


def _dead_code_refusal(finding: str, subject: str, head: _Head, where: List[str]) -> str:
    if _kind(finding) != "dead-code" or not subject:
        return ""
    if any(_called_outside_def(head.text(p) or "", subject) for p in where):
        return ""
    return "no call of `%s` in %s on the head" % (subject, ", ".join(where))


def refusal(answer: str, read: Read, ancestor: Ancestor, thread_path: str = "",
            finding: str = "") -> str:
    """"" when the answer holds on the head, else the reason it does not."""
    answer = (answer or "").strip()
    sha = fixed_sha(answer)
    if sha:
        return "" if ancestor(sha) else "Fixed in %s names a commit that is not on the head" % sha
    cited, symbols = _cited(answer, thread_path), _symbols(answer)
    if not cited and not symbols:
        return "the answer cites no file, line or symbol to check on the head"
    head = _Head(read)
    reason = _files_refusal(cited, head)
    if reason:
        return reason
    where = [p for p in dict.fromkeys([c[0] for c in cited] + [thread_path])
             if p and head.text(p) is not None]
    subject = finding_symbol(finding)
    return (_symbols_refusal(symbols, head, where)
            or _claims_refusal(answer, subject, head, where)
            or _dead_code_refusal(finding, subject, head, where))


def _line_call_refusal(subject: str, path: str, line: str, text: str) -> str:
    if not line:
        return "" if _called_outside_def(text, subject) else (
            "%s does not call `%s` on the head" % (path, subject))
    lines = text.splitlines()
    n = int(line)
    if 1 <= n <= len(lines) and _calls(lines[n - 1], subject):
        return ""
    return "%s:%s does not call `%s` on the head" % (path, line, subject)


def _call_refusal(subject: str, target: str, head: _Head, where: List[str]) -> str:
    path, _, line = target.partition(":")
    if _PATH_RE.fullmatch(path):
        text = head.text(path)
        if text is None:
            return "%s does not exist on the head" % path
        return _line_call_refusal(subject, path, line, text)
    for p in where:
        body = _body(head.text(p) or "", target)
        if body is not None:
            return "" if _calls(body, subject) else (
                "`%s` does not call `%s` on the head" % (target, subject))
    return "`%s` is not defined in %s on the head" % (target, ", ".join(where) or "any cited file")


def quoted(reply: str) -> str:
    """The lane's own words from the station's reply: the quoted lines after
    its first line, with the quote marks taken off."""
    lines = (reply or "").split("\n")[1:]
    return "\n".join(x[1:].lstrip() if x.startswith(">") else x for x in lines).strip()


def _get(gh: Any, path: str) -> Any:
    """One API read; a failure is None. A rate limit is raised, not read as
    a missing file: the brain would reopen a sound thread on it."""
    from .github import GitHubError, RateLimited
    try:
        return gh.rest("GET", path, ok=(404,)).data
    except RateLimited:
        raise
    except GitHubError:
        return None


def _decoded(raw: Any) -> Optional[str]:
    if not raw:
        return None
    try:
        return base64.b64decode(raw).decode("utf-8", errors="replace")
    except ValueError:
        return None


def head_reader(gh: Any, repo: str, head: str) -> Tuple[Read, Ancestor]:
    """read and ancestor over the GitHub API at the head commit. A failed
    read is a file the head cannot show, so a check that cannot read keeps
    the thread open (the safe side)."""

    def read(path: str) -> Optional[str]:
        data = _get(gh, "/repos/%s/contents/%s?ref=%s" % (repo, quote(path, safe="/"), head))
        if not isinstance(data, dict) or data.get("type", "file") != "file":
            return None
        raw = data.get("content")
        if not raw and data.get("sha"):
            # Files over 1 MB come back without content; the blob has it.
            blob = _get(gh, "/repos/%s/git/blobs/%s" % (repo, data["sha"]))
            raw = blob.get("content") if isinstance(blob, dict) else None
        return _decoded(raw)

    def ancestor(sha: str) -> bool:
        data = _get(gh, "/repos/%s/compare/%s...%s" % (repo, sha, head))
        return isinstance(data, dict) and data.get("status") in ("ahead", "identical")

    return read, ancestor


def commit_files(gh: Any, repo: str, sha: str) -> Tuple[str, List[str]]:
    """(full sha, files it changed) of a commit; ("", []) when GitHub cannot
    show it. GitHub lists a commit's first 300 files here, so a file past
    those reads as unchanged and the thread stays open (the safe side)."""
    data = _get(gh, "/repos/%s/commits/%s" % (repo, sha))
    if not isinstance(data, dict):
        return "", []
    return (str(data.get("sha") or ""),
            [str(f.get("filename")) for f in data.get("files") or [] if isinstance(f, dict)])


def check_on_head(gh: Any, repo: str, head: str, answer: str, thread_path: str = "",
                  finding: str = "") -> str:
    """refusal() with reads from GitHub at the head commit; no head, no pass."""
    if not head:
        return "the pull request head is unknown"
    read, ancestor = head_reader(gh, repo, head)
    return refusal(answer, read, ancestor, thread_path, finding)

