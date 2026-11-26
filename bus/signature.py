"""The signature a lane puts on everything it writes to GitHub.

Every lane shares one GitHub App identity, so the author of a pull request or
comment does not say which lane or model wrote it. A lane therefore signs its
own text: one final line, `Signed: <lane name>, model <model id>`, on each
pull request body and comment, and four trailers on each commit,
`Loom-Engine`, `Loom-Model`, `Loom-Lane` and `Loom-Run`.

The lane name is `lane-<slot>` (lane-spark). The model is the one the lane
calls: the run's own model when the caller has it, else LOOM_<SLOT>_MODEL from
the environment, never a literal. Signing is idempotent: a body that already
ends in a signature has it replaced, not repeated, so an edited body carries
exactly one.

Command line, for lanes/run_lane.sh:
  python3 -m bus.signature sign --lane SLOT [--model ID]     stdin -> stdout
  python3 -m bus.signature strip FILE                        rewrite FILE
  python3 -m bus.signature message --lane SLOT [--model ID] SUBJECT
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Mapping, Optional, Sequence

PREFIX = "Signed: "
UNKNOWN_MODEL = "unknown"
# A signature line as this module writes it, on a line of its own.
_LINE = re.compile(r"^Signed: [^\n]*, model [^\n]*$")


def lane_name(slot: str) -> str:
    slot = str(slot or "").strip()
    return slot if slot.startswith("lane-") else "lane-" + slot


def _clean(value: str) -> str:
    """One ASCII line: no line breaks and no em or en dashes."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = text.replace("\u2014", "-").replace("\u2013", "-")
    return text.encode("ascii", "replace").decode("ascii")


def model_for(slot: str, env: Optional[Mapping[str, str]] = None) -> str:
    """The model a slot calls, from LOOM_<SLOT>_MODEL, else the lane's family
    LOOM_<LANE>_MODEL (opencode-go-3 falls back to opencode-go)."""
    env = os.environ if env is None else env
    parts = lane_name(slot)[len("lane-"):].upper().replace("-", "_").split("_")
    while parts:
        value = str(env.get("LOOM_%s_MODEL" % "_".join(parts)) or "").strip()
        if value:
            return value
        parts.pop()
    return ""


def line(slot: str, model: Optional[str] = None) -> str:
    chosen = _clean(model or model_for(slot)) or UNKNOWN_MODEL
    return "%s%s, model %s" % (PREFIX, _clean(lane_name(slot)), chosen)


_COMMENT = re.compile(r"^\s*<!--.*-->\s*$")


def strip(text: str) -> str:
    """The text without its trailing signature line(s). HTML comment lines
    (markers) after the signature are kept, and the text is returned as it
    came when it holds no signature."""
    whole = str(text or "").replace("\r\n", "\n").rstrip()
    lines = whole.split("\n")
    marks: list = []
    while lines and (not lines[-1].strip() or _COMMENT.match(lines[-1])):
        line = lines.pop()
        if line.strip():
            marks.insert(0, line.strip())
    cut = False
    while lines and _LINE.match(lines[-1].rstrip()):
        lines.pop()
        cut = True
        while lines and not lines[-1].strip():
            lines.pop()
    if not cut:
        return whole
    body = "\n".join(lines)
    return body + ("\n\n" + "\n".join(marks) if marks else "")


def sign(text: str, slot: str, model: Optional[str] = None) -> str:
    """The text with exactly one final signature line."""
    body = strip(text)
    return (body + "\n\n" if body else "") + line(slot, model)


def engine_for(slot: str, env: Optional[Mapping[str, str]] = None) -> str:
    """The engine a slot runs on, from LOOM_<SLOT>_ENGINE, else the lane's
    family LOOM_<LANE>_ENGINE, the same fall-back as model_for."""
    env = os.environ if env is None else env
    parts = lane_name(slot)[len("lane-"):].upper().replace("-", "_").split("_")
    while parts:
        value = str(env.get("LOOM_%s_ENGINE" % "_".join(parts)) or "").strip()
        if value:
            return value
        parts.pop()
    return ""


def trailers(slot: str, model: Optional[str] = None, engine: Optional[str] = None,
             run: Optional[str] = None) -> str:
    """The four lane trailers: Loom-Engine, Loom-Model, Loom-Lane, Loom-Run.
    An engine or run the caller does not know is written as `unknown`; the
    run falls back to LOOM_RUN_ID in the environment."""
    chosen = _clean(model or model_for(slot)) or UNKNOWN_MODEL
    eng = _clean(engine or engine_for(slot)) or UNKNOWN_MODEL
    rid = _clean(run or os.environ.get("LOOM_RUN_ID", "")) or UNKNOWN_MODEL
    return "Loom-Engine: %s\nLoom-Model: %s\nLoom-Lane: %s\nLoom-Run: %s" % (
        eng, chosen, _clean(lane_name(slot)), rid)


def commit_message(subject: str, slot: str, model: Optional[str] = None,
                   engine: Optional[str] = None, run: Optional[str] = None) -> str:
    """The message with the lane trailers last, once."""
    kept = [x for x in str(subject or "").rstrip().split("\n")
            if not x.startswith(("Loom-Lane: ", "Loom-Model: ", "Loom-Engine: ", "Loom-Run: "))]
    head = "\n".join(kept).rstrip()
    return "%s\n\n%s\n" % (head, trailers(slot, model, engine, run))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m bus.signature")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("sign", "message"):
        p = sub.add_parser(name)
        p.add_argument("--lane", required=True)
        p.add_argument("--model", default="")
        p.add_argument("--engine", default="")
        p.add_argument("--run", default="")
        if name == "message":
            p.add_argument("subject")
    p = sub.add_parser("strip")
    p.add_argument("file")
    args = parser.parse_args(argv)
    if args.command == "sign":
        sys.stdout.write(sign(sys.stdin.read(), args.lane, args.model) + "\n")
    elif args.command == "message":
        sys.stdout.write(commit_message(args.subject, args.lane, args.model, args.engine, args.run))
    else:
        with open(args.file, encoding="utf-8") as fh:
            old = fh.read()
        with open(args.file, "w", encoding="utf-8") as fh:
            fh.write(strip(old))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
