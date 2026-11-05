"""Text for the comments a lane edits in place instead of posting again.

A lane repeats the same facts: a fix round on one pull request, a factory
incident that is still failing, an expired key that is still expired. Each
repeat as a new comment emailed the author and every participant (about 60 a
day on pull requests, 13 on one incident). A lane keeps one comment per
subject, found by its marker, and this module builds that comment's next
body from the last one. The runner (lanes/run_lane.sh) reads and writes the
comments with gh; nothing here touches the network.

    python3 -m bus.sticky rounds  --marker M --now T --old FILE < entry
    python3 -m bus.sticky repeat  --marker M --now T --old FILE --headline H
                                  --items-label L --item I < detail

Each prints the new body on stdout. FILE holds the current comment body and
may be missing or empty, which starts a new comment.
"""
from __future__ import annotations

import argparse
import re
import sys
from typing import List, Optional, Sequence, Tuple

# GitHub refuses a comment over 65536 characters; stay well under it.
MAX_BODY = 60000
MAX_ENTRY = 4000
MAX_ROUNDS = 12
MAX_ITEMS = 30
MAX_DETAIL = 1500

ROUND_RE = re.compile(r"^<!-- loom:round ([0-9]+) -->$", re.M)
REPEAT_RE = re.compile(r"^(?P<headline>.*?): (?P<count>[0-9]+) times since (?P<first>\S+), "
                       r"last at (?P<last>\S+)\.$", re.M)
ITEMS_RE = re.compile(r"^(?P<label>[^:\n]+): (?P<items>.*)$", re.M)
DROPPED_RE = re.compile(r"^Earlier entries not listed: ([0-9]+)\.$", re.M)
DETAIL_HEAD = "Latest detail:"


def _clip(text: str, limit: int) -> str:
    # Entry text comes from a run; it must not open a marker of its own.
    text = text.replace("<!--", "<!- -").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " ..."


def _rounds_of(old: str) -> List[Tuple[int, str]]:
    """(number, text) for each round in a rounds comment, newest first."""
    parts = ROUND_RE.split(old)
    return [(int(parts[i]), parts[i + 1].strip("\n")) for i in range(1, len(parts) - 1, 2)]


def merge_round(old: Optional[str], marker: str, entry: str, now: str) -> str:
    """The rounds comment with this round on top: one heading per round,
    newest first. Older rounds past MAX_ROUNDS (or the size bound) fall off
    the end and are counted, so the list never outgrows the comment."""
    rounds = _rounds_of(old or "")
    number = max([n for n, _ in rounds], default=0) + 1
    rounds.insert(0, (number, "### Round %d, %s\n\n%s" % (number, now, _clip(entry, MAX_ENTRY))))
    kept = rounds[:MAX_ROUNDS]

    def render(shown: Sequence[Tuple[int, str]]) -> str:
        lines = [marker, "Lane rounds on this pull request, newest first.", ""]
        for n, text in shown:
            lines += ["<!-- loom:round %d -->" % n, text, ""]
        if number > len(shown):
            lines.append("Earlier rounds not shown: %d." % (number - len(shown)))
        return "\n".join(lines).rstrip("\n") + "\n"

    body = render(kept)
    while len(body) > MAX_BODY and len(kept) > 1:
        kept = kept[:-1]
        body = render(kept)
    return body


def _items_of(old: str, label: str) -> List[str]:
    for found in ITEMS_RE.finditer(old):
        if found.group("label") == label:
            return [x.strip() for x in found.group("items").split(",") if x.strip()]
    return []


def merge_repeat(old: Optional[str], marker: str, headline: str, items_label: str,
                 item: str, detail: str, now: str) -> str:
    """The repeat comment after one more repeat: a running count, when it was
    first and last seen, the items it was seen on (each once) and the latest
    detail. The count and first time come from the old body, so a run that
    reads a comment it did not write keeps counting."""
    old = old or ""
    count, first = 0, now
    found = REPEAT_RE.search(old)
    if found and found.group("headline") == headline:
        count, first = int(found.group("count")), found.group("first")
    items = _items_of(old, items_label)
    dropped = int(next(iter(DROPPED_RE.findall(old)), 0))
    if item and item not in items:
        items.append(item)
    if len(items) > MAX_ITEMS:
        dropped += len(items) - MAX_ITEMS
    shown = items[-MAX_ITEMS:]
    lines = [marker, "%s: %d times since %s, last at %s." % (headline, count + 1, first, now), ""]
    lines.append("%s: %s" % (items_label, ", ".join(shown)))
    if dropped:
        lines.append("Earlier entries not listed: %d." % dropped)
    if detail.strip():
        lines += ["", DETAIL_HEAD, "", _clip(detail, MAX_DETAIL)]
    return "\n".join(lines) + "\n"


def _read(path: Optional[str]) -> str:
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except FileNotFoundError:
        return ""


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="bus.sticky")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("rounds", "repeat"):
        p = sub.add_parser(name)
        p.add_argument("--marker", required=True)
        p.add_argument("--now", required=True)
        p.add_argument("--old")
        if name == "repeat":
            p.add_argument("--headline", required=True)
            p.add_argument("--items-label", required=True)
            p.add_argument("--item", default="")
    args = parser.parse_args(argv)
    text = sys.stdin.read()
    old = _read(args.old)
    if args.command == "rounds":
        out = merge_round(old, args.marker, text, args.now)
    else:
        out = merge_repeat(old, args.marker, args.headline, args.items_label,
                           args.item, text, args.now)
    sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
