"""A box that says its work already exists must cite the code that does it.

A lane may leave an acceptance box unticked and end it "; not built: <reason>".
A lane once marked every box of a large ticket "not built: already in tree"
while the work remained, and the pull request merged (loom#2333). Nothing read
the reason, so the words were a way past the gate.

A "not built" reason that says the work is already there (CLAIM_RE) now has to
name the code: a path with a line or a symbol, in one of these forms:

    bus/proof.py:42
    bus/proof.py#L42
    bus/proof.py::criteria
    `bus/proof.py` `criteria`

The citation is checked against the pull request's base branch, not its head:
"already there" means there before this change. A file the base lacks, a line
past its end or a symbol it does not contain is no proof. One citation that
resolves is enough for its box. No model is asked: this reads text and files.
"""
from __future__ import annotations

import base64
import binascii
import re
from typing import Any, Callable, Dict, List, NamedTuple, Optional
from urllib.parse import quote

from . import proof

# The reason after "; not built:", on a box that carries one.
_REASON_RE = re.compile(r";\s*not built:\s*(\S.*)$", re.I)
CLAIM_RE = re.compile(r"\balready\b|\bin[- ]tree\b", re.I)
_PATH = r"(?:[\w.-]+/)*[\w.-]+\.[A-Za-z0-9]+"
_CITE_RE = re.compile(r"(?<![\w/.:-])(?P<path>%s)"
                      r"(?:::(?P<symbol>[A-Za-z_][\w.]*)"
                      r"|(?::|#L)(?P<line>[1-9][0-9]*)(?:-L?[0-9]+)?)?" % _PATH)
# After a path in a code span, the next code span names the symbol.
_NEXT_SPAN_RE = re.compile(r"`\s*`([A-Za-z_][\w.]*)`")

Read = Callable[[str], Optional[str]]


class Cite(NamedTuple):
    path: str
    symbol: str
    line: int


def citations(reason: str) -> List[Cite]:
    """The code a reason names: each path with a line or a symbol."""
    found = []
    for match in _CITE_RE.finditer(reason):
        symbol, line = match.group("symbol") or "", int(match.group("line") or 0)
        if not symbol and not line:
            named = _NEXT_SPAN_RE.match(reason, match.end())
            symbol = named.group(1) if named else ""
        if symbol or line:
            found.append(Cite(match.group("path"), symbol, line))
    return found


def _claims(body: str) -> List[str]:
    """Each acceptance box whose "not built" reason says the work exists."""
    found = []
    for _, text in proof.criteria(body):
        reason = _REASON_RE.search(text)
        if reason and CLAIM_RE.search(reason.group(1)):
            found.append(text)
    return found


def _reason(box: str) -> str:
    match = _REASON_RE.search(box)
    return match.group(1) if match else ""


def uncited(body: str) -> List[str]:
    """The boxes that say the work exists and name no code. Text only, for a
    reader with no base to check against."""
    return [box for box in _claims(body) if not citations(_reason(box))]


def _resolves(cite: Cite, read: Read) -> bool:
    text = read(cite.path)
    if text is None:
        return False
    if cite.line:
        return cite.line <= len(text.splitlines())
    name = re.escape(cite.symbol.rsplit(".", 1)[-1])
    return re.search(r"(?<!\w)%s(?!\w)" % name, text) is not None


def unproven(body: str, read: Read) -> List[str]:
    """The boxes that say the work exists with no citation that resolves
    through `read`, which gives a path's text on the base or None."""
    return [box for box in _claims(body)
            if not any(_resolves(cite, read) for cite in citations(_reason(box)))]


def base_reader(gh: Any, repo: str, pr: Dict[str, Any]) -> Read:
    """Read a path from the pull request's base branch, each path once. A
    missing file, a directory or a file too large for the contents API reads
    as None. Any other failed read raises, so the gate waits for a later pass
    and never passes unchecked."""
    base = pr.get("base") or {}
    ref = str(base.get("ref") or base.get("sha") or "")
    seen: Dict[str, Optional[str]] = {}

    def read(path: str) -> Optional[str]:
        if path not in seen:
            seen[path] = _decoded(gh.rest(
                "GET", "/repos/%s/contents/%s?ref=%s" % (repo, quote(path), quote(ref, safe="")),
                ok=(404,)).data)
        return seen[path]

    return read


def _decoded(data: Any) -> Optional[str]:
    if not isinstance(data, dict) or data.get("encoding") != "base64":
        return None
    try:
        return base64.b64decode(str(data.get("content") or "")).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return None
