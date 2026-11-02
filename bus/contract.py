"""Read a repo's .loom/validation.conf in any of the forms repos use, and
print it as the "key: value" lines lanes/run_lane.sh runs.

Three forms are in use (2026-10-16):
- key: value lines (interpreter, submodules, setup, check, affected), which
  pass through unchanged;
- one "<kind> | <command> | <workflow> | <step>" line per command;
- "[kind]" or "[kind.name]" tables, or "[[kind]]" array tables (one per
  command), with a command = "..." key.
For the last two the commands, in lint, format, type, test order, become one
check joined with " && ", and the interpreter is uv when a command starts
with uv or uvx (the check sandbox reads only the interpreter's directory
under HOME), else the first command's program. A contract that lists no
command checks nothing: "check: true". Before this, the runner refused the
other two forms as "an unknown line", and infra-public's lane tickets failed
setup four times without the agent running.

An "affected" command (an `affected:` line, an `affected | <command>` line or
an `[affected]` table) is not part of the check: the runner runs it after the
check with the pull request's changed files on stdin, so a repo can run the
per-directory suites its path-scoped CI jobs run (loom#1773). In the last two
forms the affected commands become one `affected:` line joined with " && ".
"""
from __future__ import annotations

import re
import shlex
import sys
from typing import Dict, List, Tuple

KINDS = ("lint", "format", "type", "test")
AFFECTED = "affected"
LEGACY = re.compile(r"^(interpreter|submodules|setup|check|affected)\s*:")
TABLE = re.compile(r"^(?:\[([a-z]+)(?:\.[A-Za-z0-9_-]+)*\]|\[\[([a-z]+)(?:\.[A-Za-z0-9_-]+)*\]\])$")
COMMAND = re.compile(r'^command\s*=\s*(?:"((?:[^"\\]|\\.)*)"|(.*\S))$')


def _lines(text: str) -> List[str]:
    return [line.strip() for line in text.replace("\r\n", "\n").split("\n")
            if line.strip() and not line.strip().startswith("#")]


def _ordered(found: List[Tuple[str, str]]) -> List[str]:
    for kind, _ in found:
        if kind not in KINDS and kind != AFFECTED:
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
            kind = table.group(1) or table.group(2)
            continue
        cmd = COMMAND.match(line)
        if cmd and kind:
            if cmd.group(1) is not None:
                found.append((kind, cmd.group(1).replace('\\"', '"').replace("\\\\", "\\")))
            else:
                found.append((kind, cmd.group(2)))
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
    found = _from_tables(lines) if tables else _from_pipes(lines)
    commands = _ordered(found)
    affected = [cmd for kind, cmd in found if kind == AFFECTED and cmd]
    tail = "affected: %s\n" % " && ".join(affected) if affected else ""
    if not commands:
        return "check: true\n" + tail
    return "interpreter: %s\ncheck: %s\n%s" % (_interpreter(commands + affected),
                                               " && ".join(commands), tail)


def fields(text: str) -> Dict[str, str]:
    """The key: value lines of a contract in any form, as a dict (the station
    reads the check and affected commands from it, bus/ci_gap.py). Raises
    ValueError like normalize."""
    found: Dict[str, str] = {}
    for line in normalize(text).splitlines():
        key, _, value = line.partition(":")
        found[key.strip()] = value.strip()
    return found


def main() -> int:
    try:
        sys.stdout.write(normalize(sys.stdin.read()))
    except ValueError as err:
        sys.stdout.write("%s\n" % err)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
