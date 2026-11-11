"""Bounded, append-only baseline flake events beside the lane run records.

These are private run evidence, not GitHub text. The ordinary encrypted
runner report carries the sidecar back to the station, alongside validation
records. Counting and safe ticket publication are separate station work.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import time
import uuid

EXCERPT_BYTES = 4096
EXCERPT_LINES = 20


def bounded_excerpt(text: str) -> str:
    """Keep a UTF-8 tail bounded by both bytes and lines."""
    raw = text.encode("utf-8", errors="replace")[-EXCERPT_BYTES:]
    return "\n".join(raw.decode("utf-8", errors="ignore").splitlines()[-EXCERPT_LINES:])


def write_event(path: Path, repo: str, base_commit: str, command: str,
                first_exit: int, excerpt: str) -> None:
    """Append one retry's evidence; every occurrence keeps its own identity."""
    event = {"version": 1, "id": uuid.uuid4().hex, "at": time.time(),
             "repo": repo, "base_commit": base_commit, "command": command,
             "first_exit": first_exit, "excerpt": bounded_excerpt(excerpt)}
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=True) + "\n")


def main(argv: list[str]) -> int:
    """Write lane evidence, reporting a local write failure to the run log."""
    try:
        path, repo, base_commit, command, first_exit, excerpt = argv
        write_event(Path(path), repo, base_commit, command, int(first_exit), excerpt)
    except (OSError, ValueError) as err:
        print("could not write base flake event: %s: %s" % (type(err).__name__, err),
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
