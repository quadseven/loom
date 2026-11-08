"""The ask block: what a brain writes on an ask ticket once it has vetted it.

The daily Discord digest lists only vetted asks and tags the operator only for
them (docs/ASK_BLOCK.md). A block is a comment that begins with MARKER and
holds three lines:

    <!-- loom:ask v1 -->
    Ask: one question for the person
    Recommend: the answer the brain advises
    Why: one line of reason

An ask that is station, lane or invariant upkeep also needs `Needs person: yes`.
The newest block by a trusted login wins, so a brain revises its block by
posting a new one.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Mapping, NamedTuple, Optional, Sequence

MARKER = "<!-- loom:ask v1 -->"
FIELD_RE = re.compile(r"^[ \t]*(Ask|Recommend|Why|Needs person)[ \t]*:[ \t]*(\S.*?)[ \t]*$",
                      re.M | re.I)
# Upkeep: a factory fault, or a title the station and invariants file under.
UPKEEP_LABELS = ("loom:factory",)
UPKEEP_TITLE_RE = re.compile(r"^\s*(station|invariant)\s*:", re.I)


class Block(NamedTuple):
    ask: str
    recommend: str
    why: str
    needs_person: bool
    at: str


def parse(body: Any, at: str = "") -> Optional[Block]:
    """The block a comment body holds, or None when it is not a complete one."""
    text = str(body or "")
    if not text.startswith(MARKER):
        return None
    found: Dict[str, str] = {}
    for key, value in FIELD_RE.findall(text[len(MARKER):]):
        found.setdefault(key.lower(), value)
    if not all(found.get(k) for k in ("ask", "recommend", "why")):
        return None
    return Block(found["ask"], found["recommend"], found["why"],
                 found.get("needs person", "").lower().startswith("y"), at)


def is_upkeep(issue: Mapping[str, Any], labels: Sequence[str]) -> bool:
    return (any(label in labels for label in UPKEEP_LABELS)
            or bool(UPKEEP_TITLE_RE.match(str(issue.get("title") or ""))))


def newest(comments: Sequence[Any], trusted: Sequence[str], login) -> Optional[Block]:
    """The newest complete block written by a trusted login."""
    allowed = {t.lower() for t in trusted}
    for c in reversed(list(comments)):
        if isinstance(c, dict) and login(c.get("user")) in allowed:
            block = parse(c.get("body"), str(c.get("created_at") or ""))
            if block:
                return block
    return None


def line(ref: str, block: Block, question_max: int = 160, answer_max: int = 160) -> str:
    """`repo#N: <question> Recommend: <answer>.`"""
    def clip(text: str, size: int) -> str:
        text = " ".join(text.split()).replace("@", "@\u200b")
        return text if len(text) <= size else text[:size - 3].rstrip(" \\") + "..."
    answer = clip(block.recommend.rstrip().rstrip("."), answer_max)
    return "%s: %s Recommend: %s." % (ref, clip(block.ask, question_max), answer)
