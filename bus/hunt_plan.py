"""The Hunt Plan: the review bot's static checks on a pull request body.

The review bot's Chief holds a pull request whose body fails its plan
checks. A lane's body is generated from the ticket brief, so Loom runs the
same checks itself: when a brief is filed (its Size becomes a letter the
gate accepts, or the brief is refused), and on every body the runner
generates, before the pull request is opened. A body that passes here passes
Chief's four blocking body checks.

Mirrors services/_shared/personas/tpm/dor_checks.py in the review bot's
public repo (grug, at b85894af): check_why, check_acceptance, check_estimate
and check_scope_fence. The issue-link check is advisory there, and the two
linked-issue checks read other issues, not this body. When the gate changes,
change the patterns here in the same way.

The gate counts three non-empty bullets under Acceptance criteria. A ticket
may state fewer (the factory files a follow-up ticket with the one criterion
a lane left not built), so pad_criteria adds the standard bullets the gate
wants: a body the gate refuses holds the pull request until the plan-check
invariant fires.

    python3 -m bus.hunt_plan size WORD    print the letter for a size, or fail
    python3 -m bus.hunt_plan brief-size FILE  print a brief's size as the runner writes it
    python3 -m bus.hunt_plan check FILE   list what a body lacks ("-" = stdin)
"""
from __future__ import annotations

import argparse
import re
import sys
from typing import List, Optional

# Patterns as in the gate (see the module docstring for the source).
_BULLET = re.compile(r"^[ \t]*-[ \t]+(?:\[[ x]\][ \t]+\S|(?!\[)\S)", re.MULTILINE)
_SECTION = re.compile(r"^##(?!#)\s+(.+?)\s*$", re.MULTILINE)
_SIZE = re.compile(r"(?:^|[^A-Za-z])Size[:\s\*_]+(XS|S|M|L|XL)\b", re.IGNORECASE)

# The sizes the gate accepts. XL it refuses ("split into multiple PRs").
SIZES = ("XS", "S", "M", "L")
_SIZE_WORDS = {
    "xs": "XS", "extra small": "XS", "extra-small": "XS", "tiny": "XS",
    "s": "S", "small": "S",
    "m": "M", "medium": "M",
    "l": "L", "large": "L",
}
_TOO_BIG = {"xl", "extra large", "extra-large", "huge"}

# The gate counts the bullets under Acceptance criteria and wants three
# non-empty ones ("what good look like"). A body it refuses holds the pull
# request until the plan-check invariant fires.
MIN_BULLETS = 3
# The standard bullets a body carries when the ticket states fewer than the
# gate wants. They hold of every lane pull request: the runner checks the
# scope and the validation contract before it opens one. They are not the
# ticket's criteria, so the factory's criterion bookkeeping ignores them.
FILLER_BULLETS = (
    "- [x] The pull request changes only what the ticket asks for",
    "- [x] The full test suite passes as CI runs it",
)

# A ticket states its size as a "## Size" section or as one line of its own,
# "Size: S" (any case, optionally bold: "**Size:** S", "**Size: S**").
_SIZE_HEADING = re.compile(r"^##[ \t]+size[ \t#]*$", re.IGNORECASE)
_SIZE_LINE = re.compile(r"^[ \t]*(?:\*\*|__)?size(?:\*\*|__)?[ \t]*:[ \t]*(?:\*\*|__)?"
                        r"[ \t]*(\S+?)[ \t]*(?:\*\*|__)?[ \t]*$", re.IGNORECASE)
SIZE_FORMS = "a ## Size section or a Size: XS|S|M|L line (or Small, Medium, Large)"


# Markdown a ticket wraps its size in ("**Size:** S", "`M`"), a list or quote
# mark before it ("- S"), and the label a line may repeat ("Size: S",
# "Size - M"). size_letter drops all three before it reads the size.
_SIZE_MARKUP = re.compile(r"[*_`]")
_SIZE_LEAD = re.compile(r"^[\s>#-]+")
_SIZE_LABEL = re.compile(r"^size\b[\s:=-]*", re.IGNORECASE)


def size_letter(text: str) -> str:
    """The gate's letter for a size written as a letter or a word, with or
    without markdown or a label: "Medium", "m", "Size: Small", "**Size:** S",
    "**S**", "S (small)", "M - medium". The one size normalizer: every
    reader (stated_size, brief_size, with_size_letter, the runner, PR repair)
    goes through it. ValueError for XL (too big for one pull request) and
    for anything else."""
    value = _SIZE_MARKUP.sub("", str(text or "")).strip()
    value = _SIZE_LABEL.sub("", _SIZE_LEAD.sub("", value)).strip().lower()
    value = value.rstrip(":.,;").strip()
    for candidate in (value, value.split(" (")[0].strip(), value.split(" - ")[0].strip(),
                      " ".join(value.split()[:2]), (value.split() or [""])[0]):
        candidate = candidate.rstrip(":.,;")
        if candidate in _TOO_BIG:
            raise ValueError("Size XL is too big for one pull request: split the ticket")
        if candidate in _SIZE_WORDS:
            return _SIZE_WORDS[candidate]
    raise ValueError("Size must be XS, S, M or L (or Small, Medium, Large), not %r"
                     % (str(text or "").strip()[:40]))


def stated_size(body: str) -> Optional[str]:
    """The size a ticket body states, as written: the first non-empty line of
    its ## Size section ("" when that section is empty), else the letter of
    its first inline Size: line. None when the body states no size in either
    form. Triage, brief validation, the rewrite check, the runner and PR
    repair all read the size through this one function."""
    lines = str(body or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for i, line in enumerate(lines):
        if not _SIZE_HEADING.match(line.strip()):
            continue
        for following in lines[i + 1:]:
            if following.startswith("#"):
                break
            if following.strip():
                return following.strip()
        return ""
    for line in lines:
        m = _SIZE_LINE.match(line)
        if m:
            try:
                return size_letter(m.group(1))
            except ValueError:
                continue
    return None


def brief_size(brief: str) -> str:
    """A brief's size as the runner writes it in the pull request body: the
    gate's letter (Medium -> M); a size it cannot read stays as its first
    word, colon dropped; "" when the brief states none."""
    stated = stated_size(brief) or ""
    try:
        return size_letter(stated)
    except ValueError:
        words = _SIZE_LABEL.sub("", _SIZE_MARKUP.sub("", stated).strip()).split()
        return words[0].rstrip(":") if words else ""


def _heading_matches(heading: str, name: str) -> bool:
    h, n = heading.lower().strip(), name.lower().strip()
    return h == n or (h.startswith(n) and len(h) > len(n) and not h[len(n)].isalnum())


def _section_text(body: str, *names: str) -> Optional[str]:
    sections = list(_SECTION.finditer(body))
    for name in names:
        for i, m in enumerate(sections):
            if _heading_matches(m.group(1), name):
                end = sections[i + 1].start() if i + 1 < len(sections) else len(body)
                return body[m.end():end].strip()
    return None


def _demoted_heading(body: str, name: str) -> Optional[str]:
    """The first heading for name below level two, if the brief has one."""
    for line in body.splitlines():
        match = re.match(r"^(#{3,})[ \t]+(.+?)[ \t#]*$", line)
        if match and _heading_matches(match.group(2), name):
            return "%s %s" % (match.group(1), match.group(2).strip())
    return None


def bullets(section: str) -> List[str]:
    """The non-empty bullets of a section, as the gate counts them."""
    return _BULLET.findall(str(section or ""))


def problems(body: str) -> List[str]:
    """What the body lacks for the gate's blocking body checks; empty when
    it passes."""
    body = str(body or "")
    out: List[str] = []
    why = _section_text(body, "Why", "Summary")
    if why is None:
        demoted = _demoted_heading(body, "Why")
        if demoted:
            out.append("no ## Why heading (found %s)" % demoted)
        else:
            out.append("missing ## Why (or ## Summary) section")
    elif len(why.split()) < 5:
        out.append("## Why has %d words; need >=5" % len(why.split()))
    criteria = _section_text(body, "Acceptance criteria")
    name = "Acceptance criteria"
    if criteria is None:
        criteria, name = _section_text(body, "Test plan"), "Test plan"
    if criteria is None:
        out.append("missing ## Acceptance criteria (or ## Test plan) section")
    elif len(bullets(criteria)) < MIN_BULLETS:
        out.append("## %s has %d non-empty bullets; need >=%d"
                   % (name, len(bullets(criteria)), MIN_BULLETS))
    size = _SIZE.search(body)
    if not size:
        out.append("no Size: XS/S/M/L/XL in body")
    elif size.group(1).upper() == "XL":
        out.append("Size XL -- split into multiple PRs before review")
    fence = _section_text(body, "Out of scope")
    if fence is None:
        out.append("missing ## Out of scope section")
    elif not fence.split():
        out.append("## Out of scope is empty")
    return out


def pad_criteria(section: str) -> str:
    """An acceptance criteria section with the bullets the gate needs: the
    ticket's own, plus the standard ones when it states fewer than three.
    The gate counts only non-empty bullets, so an empty placeholder does not
    stand in for one. A section with no criterion of its own is returned as
    it is and keeps the gate's refusal: a body with nothing to build is not
    a body to merge."""
    lines = [line for line in str(section or "").split("\n") if line.strip()]
    have = len(bullets("\n".join(lines)))
    if not have:
        return str(section or "")
    out = list(lines)
    for filler in FILLER_BULLETS:
        if have >= MIN_BULLETS:
            break
        out.append(filler)
        have += 1
    return "\n".join(out)


def with_size_letter(brief: str) -> str:
    """The brief with the first line of its ## Size section as the gate's
    letter; ValueError when that line is not a size the gate accepts. A brief
    without a ## Size section is returned as it is (its other checks report
    that)."""
    lines = brief.split("\n")
    for i, line in enumerate(lines):
        if not _SIZE_HEADING.match(line.strip()):
            continue
        for j in range(i + 1, len(lines)):
            if lines[j].startswith("#"):
                break
            if lines[j].strip():
                lines[j] = size_letter(lines[j])
                return "\n".join(lines)
        raise ValueError("## Size is empty: write XS, S, M or L")
    return brief


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m bus.hunt_plan")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("size").add_argument("word", nargs="+")
    sub.add_parser("check").add_argument("file")
    sub.add_parser("brief-size").add_argument("file")
    args = parser.parse_args(argv)
    if args.cmd == "brief-size":
        with open(args.file, encoding="utf-8") as fh:
            print(brief_size(fh.read()))
        return 0
    if args.cmd == "size":
        try:
            print(size_letter(" ".join(args.word)))
        except ValueError as exc:
            print("error: %s" % exc, file=sys.stderr)
            return 1
        return 0
    if args.file == "-":
        body = sys.stdin.read()
    else:
        with open(args.file, encoding="utf-8") as fh:
            body = fh.read()
    found = problems(body)
    for line in found:
        print(line)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
