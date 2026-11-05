"""A repo's read-only live-evidence adapter, run by the station (loom#1770).

The station's live proof (bus/live_proof.py) reads a shipped ticket's Live
criteria itself. Before this module it could read only one log command per
repo, set in the station's own environment, and no SQL at all, so a product
whose live data sits behind its own read-only routes (a log archive with
per-workload reports, a read-only database user) kept every Live box
unproven until a person read those routes by hand.

A target repo declares its adapter in `.loom/live-evidence.json` on its
default branch (a `.loom/` file, so only the operator changes it):

    {
      "version": 1,
      "log": {"argv": [".loom/live-evidence/log", "{source}", "{since}", "{needle}"],
              "sources": ["web", "worker"], "timeout": 120},
      "sql": {"argv": [".loom/live-evidence/sql", "{sql}"], "timeout": 60}
    }

Each argv is run directly, never through a shell. Its first item is either a
bare command name found on the station's PATH or a path under `.loom/` in the
repo, read from the default branch and run from a private temporary copy.
Every other item is a literal with no braces or exactly one placeholder:
`{source}`, `{since}` (UTC, 2030-01-02T03:04:05Z), `{since_epoch}` and
`{needle}` for log, `{sql}` for sql. A log argv must name `{needle}` and
`{source}`, a sql argv `{sql}`, so a ticket's text only ever arrives as one
whole argument.

The command prints its answer on stdout, one line each: the matching log
lines or result rows, and optionally `loom-evidence-window-hours: N` (the
hours of log it read in full) and `loom-evidence-count: N` (the matches it
counted, for an archive that reports counts). It exits 0, or 1 for "nothing
matched"; any other exit, a timeout or more than MAX_BYTES of output is no
answer, with the reason. stderr is never read into a ticket.

Guards, in the order they apply:

- the station runs an adapter only for repos the operator lists in
  LOOM_LIVE_EVIDENCE_REPOS;
- a SQL statement is one SELECT or SHOW (live_proof.sql_refusal), checked
  when the step is parsed and again here, just before it runs;
- the child gets a minimal environment (PATH, HOME, LANG, TMPDIR) plus only
  the variables the operator names in LOOM_LIVE_EVIDENCE_PASS_ENV, such as a
  read-only kubeconfig, never the station's GitHub or cloud credentials;
- a timeout (default DEFAULT_TIMEOUT, at most MAX_TIMEOUT seconds) kills the
  whole process group, and output stops at MAX_BYTES and MAX_LINES;
- the routes behind it are the adapter's to keep read-only (a SELECT-only
  database user, a log reader with no exec): Loom limits what it asks and
  what it keeps, not what the target system lets a reader do.

Loom knows nothing about any product: the repo supplies the adapter, and a
ticket's Live line names a step in the templated form live_proof.step_of
reads (`log:<source>` with strings, `sql:<SELECT ...>`).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from typing import Callable, Dict, List, Mapping, NamedTuple, Optional, Sequence, Tuple

from . import evidence, live_proof

MANIFEST = ".loom/live-evidence.json"
DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 300
MAX_BYTES = 64 * 1024
MAX_LINES = 200
MAX_SCRIPT_BYTES = 256 * 1024
COUNT_RE = re.compile(r"^loom-evidence-count:\s*([0-9]+)\s*$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}$")
_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
_PLACEHOLDER_RE = re.compile(r"^\{[a-z_]+\}$")
PLACEHOLDERS = {
    "log": frozenset(("{source}", "{since}", "{since_epoch}", "{needle}")),
    "sql": frozenset(("{sql}",)),
}
REQUIRED = {"log": ("{source}", "{needle}"), "sql": ("{sql}",)}
_BASE_ENV = ("PATH", "HOME", "LANG", "TMPDIR")
# Bare commands that would run ticket text as code, or run another command:
# an adapter that needs a shell ships it as a `.loom/` script instead.
_RUNS_CODE = frozenset((
    "sh", "bash", "zsh", "dash", "ksh", "csh", "tcsh", "fish", "env", "sudo", "doas", "su",
    "xargs", "eval", "exec", "nohup", "timeout", "nice", "script", "python", "python2",
    "python3", "perl", "ruby", "node", "deno", "bun", "php", "lua", "osascript", "awk",
    "gawk", "mawk", "sed", "find", "ssh", "make", "git", "docker", "kubectl-exec"))
# The kubectl verbs a bare kubectl adapter may use: reads only.
_KUBECTL_READS = frozenset(("get", "logs", "describe", "top"))


class Command(NamedTuple):
    """One declared adapter command: its argv template and timeout."""
    argv: Tuple[str, ...]
    timeout: int


class Adapter(NamedTuple):
    """A repo's parsed adapter: its log command and the sources it serves,
    its SQL command (either may be None), and the text of each `.loom/`
    script an argv runs, read from the default branch."""
    log: Optional[Command]
    sources: Tuple[str, ...]
    sql: Optional[Command]
    scripts: Dict[str, str]


def opted_in(raw: str) -> List[str]:
    """LOOM_LIVE_EVIDENCE_REPOS: the owner/name repos, comma or space
    separated, whose adapters the station may run."""
    return [r for r in re.split(r"[\s,]+", raw or "") if re.match(r"^[\w.-]+/[\w.-]+$", r)]


def passed_env(raw: str, env: Mapping[str, str]) -> Dict[str, str]:
    """LOOM_LIVE_EVIDENCE_PASS_ENV: the names, comma or space separated, of
    station variables an adapter is handed, with their values; a name that is
    not one or is unset is left out."""
    names = [n for n in re.split(r"[\s,]+", raw or "") if _ENV_NAME_RE.match(n)]
    return {n: env[n] for n in names if n in env}


def _script_path(path: str) -> bool:
    parts = path.split("/")
    return (path.startswith(".loom/") and len(path) <= 200 and path.isascii()
            and all(p and p not in (".", "..") and _NAME_RE.match(p) for p in parts[1:])
            and len(parts) >= 2)


def _argv_shape(kind: str, argv: object) -> str:
    """Why an argv is not a short list of short ASCII strings, or ""."""
    if not (isinstance(argv, list) and argv and len(argv) <= 32
            and all(isinstance(a, str) and a.isascii() and 0 < len(a) <= 200 for a in argv)):
        return "`%s.argv` is not a list of short ASCII strings" % kind
    return ""


def _program_refusal(kind: str, argv: List[str]) -> str:
    """Why argv[0] (and, for a bare command, its verb) may not run, or ""."""
    first = argv[0]
    if not (_NAME_RE.match(first) or _script_path(first)):
        return ("`%s.argv[0]` must be a bare command name or a path under .loom/, "
                "not `%s`" % (kind, first))
    if first.startswith(".loom/"):
        return ""
    if first in _RUNS_CODE or re.match(r"^python[0-9.]*$", first):
        return "`%s` runs other code; ship the adapter as a .loom/ script instead" % first
    # The verb is the manifest's, never a ticket's.
    if len(argv) < 2 or _PLACEHOLDER_RE.match(argv[1]):
        return "`%s.argv[1]` must be a literal, so a ticket never picks the verb" % kind
    if first == "kubectl" and argv[1] not in _KUBECTL_READS:
        return "`kubectl %s` is not a read; only %s run" % (
            argv[1], ", ".join(sorted(_KUBECTL_READS)))
    return ""


def _item_refusal(kind: str, item: str) -> str:
    """Why one argv item after the first may not be used, or ""."""
    if item == "exec":
        return "`%s.argv` names exec; an adapter only reads" % kind
    if _PLACEHOLDER_RE.match(item):
        return "" if item in PLACEHOLDERS[kind] else "`%s` is not a %s placeholder" % (item, kind)
    if "{" in item or "}" in item:
        return ("`%s` puts a placeholder inside a literal; a placeholder must be "
                "a whole argument" % item)
    return ""


def _timeout(kind: str, raw: Dict[str, object]) -> Tuple[Optional[int], str]:
    timeout = raw.get("timeout", DEFAULT_TIMEOUT)
    if not (isinstance(timeout, int) and not isinstance(timeout, bool)
            and 0 < timeout <= MAX_TIMEOUT):
        return None, "`%s.timeout` must be whole seconds from 1 to %d" % (kind, MAX_TIMEOUT)
    return timeout, ""


def _command(kind: str, raw: object) -> Tuple[Optional[Command], str]:
    """(one declared command, or None; why it may not run)."""
    if not isinstance(raw, dict):
        return None, "`%s` is not an object" % kind
    argv = raw.get("argv")
    why = _argv_shape(kind, argv) or _program_refusal(kind, argv) or next(
        (w for w in (_item_refusal(kind, item) for item in argv[1:]) if w), "")
    if why:
        return None, why
    missing = [p for p in REQUIRED[kind] if p not in argv[1:]]
    if missing:
        return None, "`%s.argv` must name %s" % (kind, ", ".join(missing))
    timeout, why = _timeout(kind, raw)
    if timeout is None:
        return None, why
    return Command(tuple(argv), timeout), ""


def _manifest(text: Optional[str]) -> Tuple[Optional[Dict[str, object]], str]:
    """(the manifest's JSON object, or None; why it is not a version 1 one)."""
    if text is None:
        return None, "the repo has no %s" % MANIFEST
    try:
        data = json.loads(text)
    except ValueError as err:
        return None, "%s is not JSON (%s)" % (MANIFEST, err)
    if not isinstance(data, dict) or data.get("version") != 1:
        return None, "%s is not a version 1 object" % MANIFEST
    return data, ""


def _sources(raw: object) -> Tuple[Optional[Tuple[str, ...]], str]:
    if not (isinstance(raw, list) and raw and all(
            isinstance(s, str) and live_proof.SOURCE_RE.match(s) for s in raw)):
        return None, "`log.sources` must list the log source names it serves"
    return tuple(raw), ""


def _scripts(commands: Sequence[Optional[Command]],
             read: Callable[[str], Optional[str]]) -> Tuple[Optional[Dict[str, str]], str]:
    """(the text of each `.loom/` script the commands run, or None; why)."""
    scripts: Dict[str, str] = {}
    for path in {c.argv[0] for c in commands if c is not None and c.argv[0].startswith(".loom/")}:
        body = read(path)
        if body is None:
            return None, "`%s` is not on the default branch" % path
        if len(body.encode("utf-8")) > MAX_SCRIPT_BYTES:
            return None, "`%s` is larger than %d bytes" % (path, MAX_SCRIPT_BYTES)
        scripts[path] = body
    return scripts, ""


def parse(text: Optional[str],
          read: Callable[[str], Optional[str]]) -> Tuple[Optional[Adapter], str]:
    """(the adapter a manifest declares, or None; why there is none). `read`
    returns a repo file's text from the default branch, None when absent."""
    data, why = _manifest(text)
    if data is None:
        return None, why
    commands: Dict[str, Optional[Command]] = {"log": None, "sql": None}
    for kind in [k for k in commands if k in data]:
        commands[kind], why = _command(kind, data[kind])
        if why:
            return None, why
    if commands["log"] is None and commands["sql"] is None:
        return None, "%s declares neither `log` nor `sql`" % MANIFEST
    sources: Optional[Tuple[str, ...]] = ()
    if commands["log"] is not None:
        sources, why = _sources(data["log"].get("sources"))
        if sources is None:
            return None, why
    scripts, why = _scripts(list(commands.values()), read)
    if scripts is None:
        return None, why
    return Adapter(commands["log"], sources, commands["sql"], scripts), ""


def _read_capped(stream, out: List[bytes], over: threading.Event) -> None:
    total = 0
    while True:
        chunk = stream.read(4096)
        if not chunk:
            return
        if total + len(chunk) > MAX_BYTES:
            out.append(chunk[:MAX_BYTES - total])
            over.set()
            return
        out.append(chunk)
        total += len(chunk)


def _kill(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


class Runner:
    """Runs a parsed adapter's commands, read-only and bounded. `env` is the
    station's environment the minimal child environment is taken from;
    `extra` the variables the operator passes through."""

    def __init__(self, adapter: Adapter, env: Optional[Mapping[str, str]] = None,
                 extra: Optional[Mapping[str, str]] = None,
                 popen: Callable[..., subprocess.Popen] = subprocess.Popen) -> None:
        self.adapter = adapter
        base = os.environ if env is None else env
        self.env = {k: base[k] for k in _BASE_ENV if k in base}
        self.env.setdefault("PATH", "/usr/bin:/bin")
        self.env.setdefault("LANG", "C.UTF-8")
        self.env.update(extra or {})
        self.popen = popen

    def log(self, source: str, literal: str, since: float) -> live_proof.Answer:
        """The `source` log's lines naming `literal` since the epoch `since`."""
        command = self.adapter.log
        if command is None:
            return live_proof.Answer(None, error="the adapter declares no log command")
        if source not in self.adapter.sources:
            return live_proof.Answer(None, error="the adapter serves no `%s` log (it serves %s)"
                                     % (source, ", ".join(self.adapter.sources)))
        if not evidence._eligible(literal):
            return live_proof.Answer(None, error="the string is not short printable ASCII")
        return self._run(command, {
            "{source}": source, "{needle}": literal,
            "{since}": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since)),
            "{since_epoch}": "%d" % int(since)})

    def query(self, statement: str) -> live_proof.Answer:
        """The rows one read-only statement answers; refused unrun otherwise."""
        command = self.adapter.sql
        if command is None:
            return live_proof.Answer(None, error="the adapter declares no sql command")
        why = live_proof.sql_refusal(statement)
        if why:
            return live_proof.Answer(None, error="refused: %s" % why)
        return self._run(command, {"{sql}": statement.strip().rstrip(";").strip()})

    def _executable(self, first: str, workdir: str) -> Tuple[Optional[str], str]:
        if first.startswith(".loom/"):
            path = os.path.join(workdir, os.path.basename(first))
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self.adapter.scripts[first])
            os.chmod(path, 0o700)
            return path, ""
        found = shutil.which(first, path=self.env.get("PATH"))
        return (found, "") if found else (None, "`%s` is not installed on the station" % first)

    def _run(self, command: Command, values: Dict[str, str]) -> live_proof.Answer:
        workdir = tempfile.mkdtemp(prefix="loom-live-evidence-")
        try:
            program, why = self._executable(command.argv[0], workdir)
            if program is None:
                return live_proof.Answer(None, error=why)
            argv = [program] + [values.get(a, a) if _PLACEHOLDER_RE.match(a) else a
                                for a in command.argv[1:]]
            return self._spawn(argv, command.timeout, workdir)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _spawn(self, argv: Sequence[str], timeout: int, workdir: str) -> live_proof.Answer:
        try:
            process = self.popen(list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, env=dict(self.env), cwd=workdir,
                                 start_new_session=True)
        except OSError as err:
            return live_proof.Answer(None, error="the adapter could not start (%s)"
                                     % type(err).__name__)
        try:
            return self._answer(process, timeout)
        finally:
            if process.stdout is not None:
                process.stdout.close()

    def _answer(self, process: subprocess.Popen, timeout: int) -> live_proof.Answer:
        chunks: List[bytes] = []
        over = threading.Event()
        reader = threading.Thread(target=_read_capped, args=(process.stdout, chunks, over),
                                  daemon=True)
        reader.start()
        reader.join(timeout)
        if reader.is_alive() or over.is_set():
            _kill(process)
            process.wait()
            reader.join(5)
            if over.is_set():
                return live_proof.Answer(None, error="the adapter printed more than %d bytes"
                                         % MAX_BYTES)
            return live_proof.Answer(None, error="the adapter timed out after %d seconds"
                                     % timeout)
        try:
            code = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _kill(process)
            process.wait()
            return live_proof.Answer(None, error="the adapter timed out after %d seconds"
                                     % timeout)
        if code not in (0, 1):
            return live_proof.Answer(None, error="the adapter exited %d" % code)
        lines: List[str] = []
        hours: Optional[float] = None
        count: Optional[int] = None
        for line in b"".join(chunks).decode("utf-8", "replace").splitlines():
            window = evidence.WINDOW_RE.match(line)
            counted = COUNT_RE.match(line)
            if window:
                hours = float(window.group(1))
            elif counted:
                count = int(counted.group(1))
            elif len(lines) < MAX_LINES:
                lines.append(line)
        return live_proof.Answer(lines, hours=hours, count=count)
