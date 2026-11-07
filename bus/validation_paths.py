"""Bounded, literal display of paths whose contract coverage is unknown."""
from __future__ import annotations

import json
import sys
from typing import Iterable

EXCERPT_LIMIT = 800
PATH_LIMIT = 160
PATHS_LIMIT = 8


def literal_path(path: str) -> str:
    """Code-format a path, escaping controls and code-span delimiters."""
    value = json.dumps(path, ensure_ascii=True)[1:-1].replace("`", r"\u0060")
    if len(value) > PATH_LIMIT:
        value = value[:PATH_LIMIT] + "... [path truncated]"
    return "`" + value + "`"


def path_excerpt(paths: Iterable[str]) -> str:
    """Count every path but retain only a small excerpt for public messages."""
    shown = []
    count = 0
    used = 0
    for path in paths:
        count += 1
        if len(shown) >= PATHS_LIMIT:
            continue
        item = literal_path(path)
        if used + len(item) + 2 > EXCERPT_LIMIT:
            continue
        shown.append(item)
        used += len(item) + 2
    omitted = count - len(shown)
    suffix = "; %d more paths omitted (coverage unknown)" % omitted if omitted else ""
    return ", ".join(shown) + suffix


def main() -> None:
    # git diff -z preserves embedded newlines and avoids Git's quoted-name
    # syntax. Decode arbitrary Git bytes without dropping a filename.
    paths = sys.stdin.buffer.read().decode("utf-8", errors="surrogateescape").split("\0")
    sys.stdout.write(path_excerpt(path for path in paths if path))


if __name__ == "__main__":
    main()
