"""Read a repo's .loom/validation.conf in any of the forms repos use, and
print it as the "key: value" lines lanes/run_lane.sh runs.

Three forms are in use (2026-10-16):
- key: value lines (interpreter, submodules, setup, check), which pass
  through unchanged;
- one "<kind> | <command> | <workflow> | <step>" line per command;
- "[kind]" or "[kind.name]" tables with a command = "..." key.
For the last two the commands, in lint, format, type, test order, become one
check joined with " && ", and the interpreter is uv when a command starts
with uv or uvx (the check sandbox reads only the interpreter's directory
under HOME), else the first command's program. A contract that lists no
command checks nothing: "check: true". Before this, the runner refused the
other two forms as "an unknown line", and infra-public's lane tickets failed
setup four times without the agent running.
"""
from __future__ import annotations

import re
import shlex
import sys
from typing import List, Tuple

KINDS = ("lint", "format", "type", "test")
LEGACY = re.compile(r"^(interpreter|submodules|setup|check)\s*:")
TABLE = re.compile(r"^\[([a-z]+)(?:\.[A-Za-z0-9_-]+)*\]$")
COMMAND = re.compile(r'^command\s*=\s*"((?:[^"\\]|\\.)*)"$')


def _lines(text: str) -> List[str]:
    return [line.strip() for line in text.replace("\r\n", "\n").split("\n")
            if line.strip() and not line.strip().startswith("#")]


def _ordered(found: List[Tuple[str, str]]) -> List[str]:
    for kind, _ in found:
        if kind not in KINDS:
            raise ValueError("unknown kind %r (expected one of %s)" % (kind, ", ".join(KINDS)))
    return [cmd for kind in KINDS for k, cmd in found if k == kind and cmd]


def _interpreter(commands: List[str]) -> str:
    programs = []
    for cmd in commands:
        try:
            words = shlex.split(cmd)
        except ValueError:
            raise ValueError("a command does not parse: %s" % cmd[:60]) from None
        programs.append(words[0] if words else "")
    if any(p in ("uv", "uvx") for p in programs):
        return "uv"
    first = programs[0] if programs else ""
    return first if first and "=" not in first and first != "cd" else "bash"


def _key_value(lines: List[str]) -> str:
    for line in lines:
        if not LEGACY.match(line):
            raise ValueError("has an unknown line '%s'" % line)
    return "\n".join(lines) + "\n"


def _from_tables(lines: List[str]) -> List[Tuple[str, str]]:
    found: List[Tuple[str, str]] = []
    kind = ""
    for line in lines:
        table = TABLE.match(line)
        if table:
            kind = table.group(1)
            continue
        cmd = COMMAND.match(line)
        if cmd and kind:
            found.append((kind, cmd.group(1).replace('\\"', '"').replace("\\\\", "\\")))
        elif not re.match(r"^(workflow|step)\s*=", line):
            raise ValueError("has an unknown line '%s'" % line)
    return found


def _from_pipes(lines: List[str]) -> List[Tuple[str, str]]:
    found: List[Tuple[str, str]] = []
    for line in lines:
        fields = [f.strip() for f in line.split("|")]
        if len(fields) < 2 or not fields[1]:
            raise ValueError("has an unknown line '%s'" % line)
        found.append((fields[0], fields[1]))
    return found


def normalize(text: str) -> str:
    lines = _lines(text)
    if not lines:
        if text.strip():
            # Only comments: a contract that says the repo has nothing to run.
            return "check: true\n"
        raise ValueError("has no check: line")
    if any(LEGACY.match(line) for line in lines):
        return _key_value(lines)
    tables = any(TABLE.match(line) for line in lines)
    commands = _ordered(_from_tables(lines) if tables else _from_pipes(lines))
    if not commands:
        return "check: true\n"
    return "interpreter: %s\ncheck: %s\n" % (_interpreter(commands), " && ".join(commands))


def main() -> int:
    try:
        sys.stdout.write(normalize(sys.stdin.read()))
    except ValueError as err:
        sys.stdout.write("%s\n" % err)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
