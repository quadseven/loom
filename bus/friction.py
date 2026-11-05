"""The slowdowns a tick journal names (its "- Friction:" lines).

The brains write what slowed the factory in each tick journal on the goal
homes (docs/BRAIN.md, Journal). friction_lines reads them out of a journal
body; the dashboard's Improving page shows them, and the station's friction
intake (bus/friction_intake.py) files a ticket for one that recurs.
fingerprint gives each line a stable key: the same slowdown written a little
differently from tick to tick shares most of its words.
"""
from __future__ import annotations

import hashlib
import re
from typing import FrozenSet, Iterable, Tuple


# "none", "None.", "nothing new observed this tick", "no new slowdown":
# a journal saying there was no friction this tick.
_NO_FRICTION = re.compile(r"(?:none|n a)(?: (?:this tick|new|today))?$|nothing\b"
                          r"|no new (?:slowdowns?|friction|blockers?)\b")


def _is_none(text: str) -> bool:
    return _NO_FRICTION.match(clean_text(text)) is not None


def clean_text(text: str) -> str:
    """Lower-case words and digits, one space apart."""
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


# The headings brains write a slowdown under (docs/BRAIN.md, Journal: the
# canonical one is "- Friction:"). Read from the loom#30 journals: "What
# slowed the factory [down] [this tick]", "Factory note(s)", "Factory
# friction", "Improving Loom itself" and a bare "Factory:" carry most of
# them; "Factory health:" is a status line and is not one. A heading may
# carry a qualifier before its colon: "(for the next brief)", "this tick",
# "for the next brain".
_FRICTION_HEAD = re.compile(
    r"^([ \t]*)((?:[-*+]|\d+[.)])[ \t]+)?(?:\*\*)?"
    r"(?:friction|blockers?|blocked|improving[ \t]+loom[ \t]+itself"
    r"|(?:what[ \t]+)?slowed[ \t]+the[ \t]+factory(?:[ \t]+down)?"
    r"|factory(?:[ \t]+(?:notes?|friction|observations?|lessons?))?)"
    r"(?:[ \t]*\([^)\n]*\))?(?:[ \t]+(?:this[ \t]+tick|for[ \t]+the[ \t]+[a-z ]+?))?"
    r"(?:[ \t]*\([^)\n]*\))?[ \t]*(?:\*\*)?[ \t]*:(?:\*\*)?[ \t]*(.*)$", re.I)
_LIST_ITEM = re.compile(r"^([ \t]*)(?:[-*+]|\d+[.)]|\(\d+\))[ \t]+(.*)$")
# Another journal line ("- Reports: ...", "Cap+8 rule: ...") ends a
# hard-wrapped friction line.
_STATUS_LINE = re.compile(r"^[ \t]*(?:[-*+][ \t]+)?(?:\*\*)?[A-Za-z][A-Za-z0-9+' -]{0,40}:(?:\*\*)?(?:[ \t]|$)")


def _ends_line(text: str) -> bool:
    return not text or text.rstrip()[-1:] in ".!?;)" or text.rstrip().endswith(".\"")


def _joined(text, lines, at):
    """text plus the hard-wrapped lines that continue it from lines[at],
    and the index after them."""
    text = text.strip()
    while (at < len(lines) and not _ends_line(text) and lines[at].strip()
           and not _LIST_ITEM.match(lines[at]) and not _STATUS_LINE.match(lines[at])
           and not _FRICTION_HEAD.match(lines[at])
           and not lines[at].lstrip().startswith("<!--")):
        text += " " + lines[at].strip()
        at += 1
    return text, at


def friction_lines(body):
    """Each slowdown a journal names, in order. A heading with text after
    its colon is one friction, with any hard-wrapped lines that continue it;
    a heading with nothing after it takes the list under it (a bulleted
    heading only its nested items) or the paragraph that follows. Lines that
    say there was none are left out."""
    lines = str(body or "").splitlines()
    found, at = [], 0
    while at < len(lines):
        head = _FRICTION_HEAD.match(lines[at])
        at += 1
        if not head:
            continue
        indent, bulleted, rest = head.group(1), head.group(2), head.group(3)
        if rest.strip():
            text, at = _joined(rest, lines, at)
            found.append(text)
            continue
        if at < len(lines) and lines[at].strip() and not _LIST_ITEM.match(lines[at]) \
                and not _FRICTION_HEAD.match(lines[at]):
            text, at = _joined(lines[at], lines, at + 1)
            found.append(text)
            continue
        while at < len(lines) and not _FRICTION_HEAD.match(lines[at]):
            item = _LIST_ITEM.match(lines[at])
            if not item or (bulleted and len(item.group(1)) <= len(indent)):
                break
            text, at = _joined(item.group(2), lines, at + 1)
            found.append(text)
    return [text for text in found if not _is_none(text)]

# Words of three letters or fewer ("the", "for", "PRs") carry no topic.
WORD_MIN = 4


def words(text: str) -> FrozenSet[str]:
    """The friction's topic words: cleaned words longer than three letters."""
    return frozenset(w for w in clean_text(text).split() if len(w) >= WORD_MIN)


def fingerprint(text: str) -> Tuple[FrozenSet[str], str]:
    """(topic words, key): the key is the sha1 of the sorted words joined by
    spaces, so the same words in any order and case share a key."""
    found = words(text)
    # An id for a marker, not a security control.
    key = hashlib.sha1(" ".join(sorted(found)).encode("utf-8"),
                       usedforsecurity=False).hexdigest()
    return found, key


def jaccard(left: Iterable[str], right: Iterable[str]) -> float:
    """Shared words over all words of the two sets; 0 when both are empty."""
    a, b = set(left), set(right)
    return len(a & b) / len(a | b) if a | b else 0.0
