"""Read a lane's bounded, plain-text decision request from its scratch file."""

import json
import os
import stat
import sys

from . import text
from .kinds import HUMAN_KINDS

MAX_BYTES = 4096
OPERATOR_KINDS = frozenset(HUMAN_KINDS) | {"host", "policy"}


def line(value: object, limit: int) -> str:
    if not isinstance(value, str) or not 0 < len(value.strip()) <= limit:
        raise ValueError("missing or too long")
    value = value.strip(" ")
    if not value.isascii() or not value.isprintable() or text.SECRET_RE.search(value):
        raise ValueError("unsafe text")
    # The packet is evidence from an agent, so it cannot waive its own scan.
    if "<!--" in value or "leak-guard-allow" in value.casefold():
        raise ValueError("unsafe text")
    text.check(value)
    return text.scrub_mentions(value, allow=())


def request(path: str) -> list[str]:
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("not a regular scratch file")
        data = file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("too large")
    body = json.loads(data)
    if not isinstance(body, dict):
        raise ValueError("expected an object")
    kind = body.get("kind")
    if isinstance(kind, str) and kind in OPERATOR_KINDS and body.keys() == {"kind", "question"}:
        try:
            question = line(body["question"], 200)
        except ValueError:
            question = "What operator action is needed? The runner withheld the original question."
        return [kind, question]
    if kind != "ambiguity" or body.keys() != {"kind", "question", "context", "options", "answer"}:
        raise ValueError("unknown request")
    options, answer = body["options"], body["answer"]
    if not isinstance(options, list) or len(options) != 2 or type(answer) is not int or answer not in (1, 2):
        raise ValueError("expected two choices and one recommendation")
    first, second = (line(option, 140) for option in options)
    if first.casefold() == second.casefold():
        raise ValueError("choices must be distinct")
    return [kind, line(body["question"], 200), line(body["context"], 1200), first, second, str(answer)]


if __name__ == "__main__":
    try:
        print("\n".join(request(sys.argv[1])))
    except (OSError, TypeError, ValueError, RecursionError):
        sys.exit("invalid lane decision request")
