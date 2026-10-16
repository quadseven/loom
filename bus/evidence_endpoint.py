"""The evidence endpoint: service logs and database rows, read-only, for the
remote brain (docs/LANES.md, The evidence endpoint).

    python3 -m bus.evidence_endpoint     (launchd: com.loom.evidence)

    GET /evidence                                       the names it serves
    GET /evidence/logs?source=<name>&match=<text>&since=<hours>
    GET /evidence/query?name=<name>&<param>=<value>...

Every name comes from a private JSON config file (LOOM_EVIDENCE_CONFIG): a log
source is a fixed command (an argv list, never a shell line), a query a fixed
SELECT with typed parameters fed on stdin to a fixed command. A caller picks a
name and supplies values that pass their type check; nothing else runs.
Answers hold at most MAX_LINES lines or rows and MAX_BYTES bytes, and a line
that fails evidence.safe_line (credentials, private hosts, addresses, local
paths) is dropped and counted in the call's log line, never in the answer,
so a caller cannot learn whether a withheld line matched. Every call needs
the bearer token read from SSM at start and is logged to stderr.
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import AbstractSet, Any, Callable, Dict, List, Mapping, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

from . import evidence, station

MAX_LINES = 200
MAX_BYTES = 64 * 1024
# A command's output is spooled to a temporary file and at most this much of
# it is read: the newest end of a log, the start of a query's rows.
MAX_READ = 16 * 1024 * 1024
MAX_RUNNING = 2
DEFAULT_SINCE_HOURS = 24
MAX_SINCE_HOURS = 168
MIN_TOKEN = 24
DEFAULT_PORT = 7341
NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
PARAM_RE = re.compile(r"[a-z][a-z0-9_]{0,30}")
INT_RE = re.compile(r"[0-9]{1,18}")
# A text value has no quote, semicolon, backslash, comment opener, shell or
# glob character, so it can neither leave the single-quoted SQL literal it is
# put in nor mean anything to a shell. "--" and a leading "-" (which a
# command would read as an option when {match} is a whole argument) are
# refused on their own below.
SAFE_TEXT_RE = re.compile(r"[A-Za-z0-9 ._:@/+=,-]{1,120}")
TYPES = ("int", "text")
READ_RE = re.compile(r"\s*(?:select|with)\b", re.IGNORECASE)
WRITE_RE = re.compile(r"\b(?:insert|update|delete|merge|upsert|drop|alter|create|truncate|grant"
                      r"|revoke|copy|call|execute|vacuum|lock|into)\b", re.IGNORECASE)
# :name, but not the second colon of a ::type cast or a colon inside a word.
PLACEHOLDER_RE = re.compile(r"(?<![:\w]):([a-z][a-z0-9_]*)")
# Tailscale's address ranges: the service never binds a wildcard or a LAN address.
TAILNET = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))
# At most MAX_RUNNING commands at once; a call beyond that answers 503.
_SLOTS = threading.BoundedSemaphore(MAX_RUNNING)


class CallError(Exception):
    """A call answered with an error status and a message for the caller."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Query:
    command: Tuple[str, ...]
    sql: str
    params: Dict[str, str]


@dataclass(frozen=True)
class Config:
    logs: Dict[str, Tuple[str, ...]]
    queries: Dict[str, Query]


def load_config(path: str) -> Config:
    with open(path, encoding="utf-8") as fh:
        return parse_config(json.load(fh))


def parse_config(data: Any) -> Config:
    """The checked config; ValueError names the first entry that is wrong."""
    if not isinstance(data, dict) or set(data) - {"logs", "queries"}:
        raise ValueError("the evidence config is an object with only logs and queries")
    logs = {name: _command(name, spec, {"command"}) for name, spec in _named(data, "logs").items()}
    queries = {name: _query(name, spec) for name, spec in _named(data, "queries").items()}
    return Config(logs, queries)


def _named(data: Dict[str, Any], key: str) -> Dict[str, Any]:
    section = data.get(key, {})
    if not isinstance(section, dict):
        raise ValueError("%s must be an object of named entries" % key)
    for name in section:
        if not NAME_RE.fullmatch(name):
            raise ValueError("%s: %r is not a name of lowercase letters, digits, - and _" % (key, name))
    return section


def _command(name: str, spec: Any, keys: AbstractSet[str]) -> Tuple[str, ...]:
    if not isinstance(spec, dict) or set(spec) - keys:
        raise ValueError("%s may only have the keys %s" % (name, ", ".join(sorted(keys))))
    command = spec.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(a, str) and a for a in command):
        raise ValueError("%s needs a command: a list of argument strings, run without a shell" % name)
    return tuple(command)


def _query(name: str, spec: Any) -> Query:
    command = _command(name, spec, {"command", "sql", "params"})
    sql, params = spec.get("sql"), spec.get("params", {})
    if not isinstance(sql, str) or not READ_RE.match(sql) or ";" in sql or WRITE_RE.search(sql):
        raise ValueError("query %s must be one SELECT, with no semicolon and no write keyword" % name)
    if not isinstance(params, dict) or any(not PARAM_RE.fullmatch(p) or p == "name" or kind not in TYPES
                                           for p, kind in params.items()):
        raise ValueError("query %s: params maps names (not name) to int or text" % name)
    if set(PLACEHOLDER_RE.findall(sql)) != set(params):
        raise ValueError("query %s must use each declared param as :param, and no other" % name)
    return Query(command, sql, dict(params))


def catalog(config: Config) -> Dict[str, Any]:
    """The names a caller may use, never a command or SQL."""
    return {"logs": {name: {"since": _takes_since(command)} for name, command in config.logs.items()},
            "queries": {name: dict(query.params) for name, query in config.queries.items()}}


def _takes_since(command: Tuple[str, ...]) -> bool:
    return any("{since}" in arg for arg in command)


def answer(config: Config, path: str, query: str) -> Tuple[int, Dict[str, Any], int]:
    """The status and JSON body for an authorized GET, and how many unsafe
    lines were dropped (for the log line only)."""
    try:
        args = _args(query)
        if path == "/evidence":
            return 200, catalog(config), 0
        if path == "/evidence/logs":
            return (200,) + read_logs(config, args)
        if path == "/evidence/query":
            return (200,) + read_query(config, args)
        raise CallError(404, "no such endpoint; GET /evidence lists what is served")
    except CallError as err:
        return err.status, {"error": str(err)}, 0


def _args(query: str) -> Dict[str, str]:
    try:
        parsed = parse_qs(query, keep_blank_values=True, max_num_fields=20)
    except ValueError as err:
        raise CallError(400, "too many parameters") from err
    if any(len(values) > 1 for values in parsed.values()):
        raise CallError(400, "a parameter appears more than once")
    return {key: values[0] for key, values in parsed.items()}


def _only(args: Dict[str, str], required: AbstractSet[str],
          optional: AbstractSet[str] = frozenset()) -> None:
    missing = sorted(required - set(args))
    if missing:
        raise CallError(400, "missing parameter: %s" % missing[0])
    if set(args) - required - optional:
        raise CallError(400, "unknown parameter; allowed: %s" % ", ".join(sorted(required | optional)))


def _text(value: str, label: str) -> str:
    if not SAFE_TEXT_RE.fullmatch(value) or "--" in value:
        raise CallError(400, "%s may hold only letters, digits, spaces and . _ : @ / + = , - "
                             "(1 to 120 characters); a SQL or shell metacharacter is refused" % label)
    if value.startswith("-"):
        raise CallError(400, "%s may not start with -, which a command reads as an option" % label)
    return value


def _int(value: str, label: str) -> str:
    if not INT_RE.fullmatch(value):
        raise CallError(400, "%s must be a whole number" % label)
    return value


def _hours(value: str) -> int:
    if not INT_RE.fullmatch(value) or not 1 <= int(value) <= MAX_SINCE_HOURS:
        raise CallError(400, "since must be whole hours from 1 to %d" % MAX_SINCE_HOURS)
    return int(value)


def read_logs(config: Config, args: Dict[str, str]) -> Tuple[Dict[str, Any], int]:
    """The newest lines of a named log source that contain match."""
    _only(args, {"source", "match"}, {"since"})
    template = config.logs.get(args["source"])
    if template is None:
        raise CallError(404, "no log source has that name; GET /evidence lists them")
    match = _text(args["match"], "match")
    since = _hours(args.get("since", str(DEFAULT_SINCE_HOURS)))
    argv = [arg.replace("{since}", str(since)).replace("{match}", match) for arg in template]
    lines, cut = _run(argv, None, newest=True)
    base = {"source": args["source"], "match": match,
            "since_hours": since if _takes_since(template) else None, "output_cut": cut}
    return capped([line for line in lines if match in line], base, "lines", newest=True)


def read_query(config: Config, args: Dict[str, str]) -> Tuple[Dict[str, Any], int]:
    """The first rows a named query answers, with its typed params bound."""
    query = config.queries.get(args.get("name", ""))
    if query is None:
        raise CallError(404, "no query has that name; GET /evidence lists them")
    _only(args, {"name"} | set(query.params))
    values = {p: (_int if kind == "int" else _text)(args[p], p) for p, kind in query.params.items()}
    sql = PLACEHOLDER_RE.sub(lambda m: _literal(query.params[m.group(1)], values[m.group(1)]), query.sql)
    rows, cut = _run(list(query.command), sql, newest=False)
    return capped(rows, {"query": args["name"], "params": values, "output_cut": cut}, "rows", newest=False)


def _literal(kind: str, value: str) -> str:
    # A checked text value holds no quote, so quoting it is enough.
    return value if kind == "int" else "'%s'" % value


def _run(argv: List[str], stdin: Optional[str], newest: bool) -> Tuple[List[str], bool]:
    """The command's output lines (see _read) and whether some were left
    unread. Its stderr never reaches the caller."""
    if not _SLOTS.acquire(blocking=False):
        raise CallError(503, "%d reads are already running; retry in a minute" % MAX_RUNNING)
    try:
        with tempfile.TemporaryFile() as spool:
            _check(argv, stdin, spool)
            return _read(spool, newest)
    finally:
        _SLOTS.release()


def _check(argv: List[str], stdin: Optional[str], spool: Any) -> None:
    try:
        done = evidence.run(argv, stdin=stdin, stdout=spool)
    except subprocess.TimeoutExpired as err:
        raise CallError(504, "the command timed out after %d seconds" % evidence.TIMEOUT) from err
    except OSError as err:
        raise CallError(502, "the command could not start (%s)" % type(err).__name__) from err
    # grep's "no match": exit 1 with nothing on stderr.
    no_match = done.returncode == 1 and not done.stderr.strip()
    if done.returncode != 0 and not no_match:
        raise CallError(502, "the command exited %d" % done.returncode)


def _read(spool: Any, newest: bool) -> Tuple[List[str], bool]:
    """At most MAX_READ bytes of the spooled output, as lines: the last bytes
    when newest, else the first; and whether any were left unread. The line
    the cut goes through is left out, never quoted in part."""
    size = spool.seek(0, os.SEEK_END)
    cut = size > MAX_READ
    spool.seek(size - MAX_READ if cut and newest else 0)
    lines = spool.read(MAX_READ).decode("utf-8", "replace").splitlines()
    if cut and lines:
        lines = lines[1:] if newest else lines[:-1]
    return lines, cut


def capped(lines: List[str], base: Dict[str, Any], key: str,
           newest: bool) -> Tuple[Dict[str, Any], int]:
    """base plus the safe lines under key, at most MAX_LINES and an encoded
    body of at most MAX_BYTES (the newest lines when newest, else the first),
    and whether safe lines were left out; and the count of unsafe lines
    dropped. The count stays out of the body: with match it would tell a
    caller whether a withheld line holds a guessed value."""
    safe = [line for line in lines if evidence.safe_line(line)]
    body = dict(base, truncated=False, **{key: []})
    budget = MAX_BYTES - len(encode(body))
    kept: List[str] = []
    for line in reversed(safe) if newest else safe:
        cost = len(json.dumps(line)) + 2
        if len(kept) == MAX_LINES or cost > budget:
            break
        kept.append(line)
        budget -= cost
    body[key] = kept[::-1] if newest else kept
    body["truncated"] = len(kept) < len(safe)
    return body, len(lines) - len(safe)


def encode(body: Dict[str, Any]) -> bytes:
    return json.dumps(body).encode("ascii")


def authorized(header: str, token: str) -> bool:
    supplied = header[7:] if header.startswith("Bearer ") else ""
    return bool(token) and hmac.compare_digest(supplied.encode(), token.encode())


def call_line(method: str, path: str, query: str, status: int, body: Dict[str, Any],
              dropped: int, size: int) -> str:
    """One log line per call: what was asked, the answer's status and size."""
    params = parse_qs(query, keep_blank_values=True)
    source = (params.get("source") or params.get("name") or ["-"])[0]
    lines = body.get("lines", body.get("rows", []))
    return "evidence: %s %s status=%d path=%s source=%s params=%s lines=%d dropped=%d bytes=%d" % (
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), method, status, path,
        json.dumps(source), json.dumps(params, sort_keys=True)[:500], len(lines),
        dropped, size)


def _stderr(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


class _Server6(ThreadingHTTPServer):
    address_family = socket.AF_INET6


def make_server(host: str, port: int, config: Config, token: str,
                log: Callable[[str], None] = _stderr) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        timeout = 30  # seconds a connection may sit idle

        def call(self) -> None:
            url = urlsplit(self.path)
            if not authorized(self.headers.get("Authorization", ""), token):
                status, body, dropped = 401, {"error": "unauthorized"}, 0
            elif self.command != "GET":
                status, body, dropped = 405, {"error": "the evidence endpoint is read-only: GET only"}, 0
            else:
                status, body, dropped = answer(config, url.path, url.query)
            data = encode(body)
            # Logged before the answer is sent, so a caller that hangs up is logged too.
            log(call_line(self.command, url.path, url.query, status, body, dropped, len(data)))
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            if status == 401:
                self.send_header("WWW-Authenticate", "Bearer")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        # Every method is answered here, so every call is logged.
        do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = call  # noqa: N815

        def log_message(self, format, *args) -> None:  # noqa: A002 (base signature)
            pass  # call_line logs every call instead

    server_class = _Server6 if ":" in host else ThreadingHTTPServer
    return server_class((host, port), Handler)


def tailnet_host(host: str) -> bool:
    """True for a tailnet or loopback address; never a wildcard, LAN or public one."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or any(address in net for net in TAILNET)


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    config: str
    token_ssm: str
    region: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        host = env.get("LOOM_EVIDENCE_HOST", "").strip()
        if not tailnet_host(host):
            raise ValueError("LOOM_EVIDENCE_HOST must be this machine's tailnet (or loopback) address")
        port = env.get("LOOM_EVIDENCE_PORT", "").strip() or str(DEFAULT_PORT)
        if not port.isdigit() or not 0 < int(port) < 65536:
            raise ValueError("LOOM_EVIDENCE_PORT must be a port number")
        required = {key: env.get(key, "").strip() for key in
                    ("LOOM_EVIDENCE_CONFIG", "LOOM_EVIDENCE_TOKEN_SSM", "LOOM_AWS_REGION")}
        missing = [key for key, value in required.items() if not value]
        if missing:
            raise ValueError("%s must be set" % missing[0])
        return cls(host, int(port), required["LOOM_EVIDENCE_CONFIG"],
                   required["LOOM_EVIDENCE_TOKEN_SSM"], required["LOOM_AWS_REGION"])


def main(env: Optional[Mapping[str, str]] = None,
         read_secret: Callable[[str, str], str] = station.read_ssm) -> int:
    env = os.environ if env is None else env
    try:
        settings = Settings.from_env(env)
        config = load_config(settings.config)
        token = read_secret(settings.token_ssm, settings.region)
        if len(token) < MIN_TOKEN:
            raise ValueError("the bearer token in SSM is shorter than %d characters" % MIN_TOKEN)
        server = make_server(settings.host, settings.port, config, token)
    except (ValueError, OSError, subprocess.SubprocessError) as err:
        _stderr("evidence: not serving: %s" % err)
        return 2
    _stderr("evidence: serving %s port %d: %d log sources, %d queries"
            % (settings.host, settings.port, len(config.logs), len(config.queries)))
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
