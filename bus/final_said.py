"""What an agent said last, for a run that committed nothing (loom#1799).

A run that commits nothing and writes no summary parks with the agent's own
final words. The tail of the run's output is a poor source for them: when the
agent's last actions printed a file or a command's output, that text became
the reason. On 2026-10-25 five such parks quoted a source comment, a git
config, a test assertion, a colored permission prompt, and only once the
agent's real explanation.

So the reason comes, in order, from:
  1. the runner's final-message file (codex --output-last-message),
  2. the last assistant text event in the run's JSON events (opencode),
  3. the output tail, with ANSI escapes removed, keeping only the prose that
     follows the last line that looks like a tool's output.
When the tail holds only tool output, the run "ended without explaining why".

Usage: python3 -m bus.final_said [--message FILE] [--events FILE] < tail
Prints one ASCII line and exits 0 when the agent said something, exits 3
(printing nothing) when the tail was only tool output, and prints nothing and
exits 0 when there was nothing at all.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from bus.text import LOCAL_PATH_RE

# Exit status when the only text was a tool's output.
DUMP = 3
# CSI sequences (colors, cursor moves), OSC sequences (titles, links) and the
# two-byte escapes. Removed before the ASCII filter: that filter drops only
# the ESC byte, and "[31m" then reached the reason.
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
# A line that reads like a file or a command's output, not like prose: an INI
# section or key = value line (a git config), a code line or source comment,
# a test assertion, a call standing alone, a diff or traceback line, or a lone
# bracket. A line indented by a tab or four spaces is code or a file's body.
TOOL_LINE_RE = re.compile(
    r"^\[[A-Za-z0-9 ._\"-]+\]$"
    r"|^[A-Za-z_][\w.-]*\s*=\s*\S"
    r"|^(?:def|class|import|return|raise|assert|async def|function|const|let|var|func|fn)\b"
    r"|^from\s+\S+\s+import\b"
    r"|^(?:if|for|while|elif|else|try|except|with)\b.*:$"
    r"|^self\.assert\w*\(|^expect\(|^assert\w*\("
    r"|^(?:#!|# [a-z]|//|/\*|\*/)"
    r"|^(?:\+\+\+|---|@@) |^Traceback \(most recent call last\)|^File \""
    r"|^[\w.]+\(.*\)[;,]?$"
    r"|[{;]$|^[\[\](){}]+[;,]?$")
INDENTED_RE = re.compile(r"^(?:\t| {4})\s*(?![-*] |[0-9]+\. )\S")


def clean(text: str) -> str:
    """Text without escape sequences or non-ASCII characters."""
    text = ANSI_RE.sub("", text)
    return "".join(c for c in text if c in "\t\n" or " " <= c <= "~")


def one_line(lines: Iterable[str]) -> str:
    """The lines as one line, local paths read <path>."""
    line = " ".join(" ".join(lines).split())
    return LOCAL_PATH_RE.sub("<path>", line)


def looks_like_tool_output(line: str) -> bool:
    return bool(INDENTED_RE.match(line) or TOOL_LINE_RE.search(line.strip()))


def from_file(path: Optional[str]) -> str:
    """The runner's final-message file, cleaned; empty when it is absent."""
    if not path:
        return ""
    try:
        return one_line(clean(Path(path).read_text(encoding="utf-8", errors="replace")).splitlines())
    except OSError:
        return ""


def from_events(path: Optional[str]) -> str:
    """The last non-blank assistant text event in an opencode JSON events file."""
    if not path:
        return ""
    last = ""
    try:
        with open(path, encoding="utf-8", errors="replace") as events:
            for raw in events:
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(event, dict) or event.get("type") != "text":
                    continue
                part = event.get("part") if isinstance(event.get("part"), dict) else {}
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    last = text
    except OSError:
        return ""
    return one_line(clean(last).splitlines())


def from_tail(tail: str) -> Tuple[str, bool]:
    """The prose that ends the tail, and whether the tail held only tool output."""
    lines: List[str] = [l for l in clean(tail).splitlines() if l.strip()]
    if not lines:
        return "", False
    cut = max((i for i, l in enumerate(lines) if looks_like_tool_output(l)), default=-1)
    prose = lines[cut + 1:]
    if not prose:
        return "", True
    return one_line(prose), False


def choose(message: Optional[str], events: Optional[str], tail: str) -> Tuple[str, bool]:
    """What the agent said last, and whether the run ended on tool output only."""
    for said in (from_file(message), from_events(events)):
        if said:
            return said, False
    return from_tail(tail)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--message")
    parser.add_argument("--events")
    args = parser.parse_args(argv)
    said, dump = choose(args.message, args.events, sys.stdin.read())
    if dump:
        return DUMP
    sys.stdout.write(said)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
