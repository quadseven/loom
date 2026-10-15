#!/usr/bin/env python3
"""Read-only HTTP access to explicitly configured log sources and SQL queries."""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from bus import text  # noqa: E402

MAX_ITEMS = 200
MAX_BYTES = 64 * 1024
MAX_SINCE_HOURS = 168
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
PARAM_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SQL_SELECT_RE = re.compile(r"^\s*SELECT\b", re.IGNORECASE)
SQL_FORBIDDEN_RE = re.compile(r";|--|/\*|\*/", re.IGNORECASE)
VALUE_FORBIDDEN_RE = re.compile(r"[;\\\"'`$|&<>()[\]{}*?!\r\n]")


def _valid_name(value: str) -> bool:
    return bool(NAME_RE.fullmatch(value))


def _read_config(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as source:
        config = json.load(source)
    if not isinstance(config, dict) or not isinstance(config.get("logs"), dict) or not isinstance(
            config.get("queries"), dict):
        raise ValueError("evidence config must contain logs and queries maps")
    return config


def _read_ssm(name: str, region: str) -> str:
    result = subprocess.run(
        ["aws", "ssm", "get-parameter", "--name", name, "--with-decryption",
         "--query", "Parameter.Value", "--output", "text", "--region", region],
        check=True, capture_output=True, text=True, timeout=30)
    token = result.stdout.strip()
    if not token:
        raise ValueError("evidence bearer token is empty")
    return token


def _require_tailnet_address(bind_host: str, tailscale_bin: str) -> None:
    result = subprocess.run([tailscale_bin, "ip", "-4"], check=True,
                            capture_output=True, text=True, timeout=10)
    addresses = {line.strip() for line in result.stdout.splitlines()}
    if bind_host not in addresses:
        raise ValueError("evidence bind address is not assigned to the tailnet")


def _safe_parameter(value: str) -> bool:
    return not VALUE_FORBIDDEN_RE.search(value) and len(value) <= 256


def _audit_parameters(params: dict[str, str]) -> dict[str, str]:
    return {key: value if _safe_parameter(value) and not text.SECRET_RE.search(value)
            else "<redacted>" for key, value in params.items()}


def _log_lines(spec: dict[str, Any], hours: int, match: str) -> list[str]:
    argv = spec.get("command")
    if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) for arg in argv):
        raise ValueError("log source command must be a non-empty argument list")
    if "{since_hours}" not in argv:
        raise ValueError("log source command must use the since_hours argument")
    if any("{" in arg or "}" in arg for arg in argv if arg not in ("{since_hours}",)):
        raise ValueError("log source has an unsupported command placeholder")
    command = [arg.replace("{since_hours}", str(hours)) for arg in argv]
    completed = subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
    return [line for line in completed.stdout.splitlines() if match in line]


def _query_rows(spec: dict[str, Any], supplied: dict[str, str]) -> list[list[Any]]:
    sql = spec.get("sql")
    declarations = spec.get("params")
    database = spec.get("database")
    if (not isinstance(database, str) or not isinstance(sql, str) or
            not SQL_SELECT_RE.match(sql) or SQL_FORBIDDEN_RE.search(sql) or
            not isinstance(declarations, dict)):
        raise ValueError("query must declare a database, parameters, and one SELECT")
    if set(supplied) != set(declarations):
        raise ValueError("query parameters do not match the declaration")
    typed: dict[str, Any] = {}
    for key, kind in declarations.items():
        if not PARAM_RE.fullmatch(key) or ":" + key not in sql:
            raise ValueError("query parameter declaration is invalid")
        raw = supplied[key]
        if not _safe_parameter(raw):
            raise ValueError("query parameter contains a forbidden character")
        if kind == "str":
            typed[key] = raw
        elif kind == "int":
            typed[key] = int(raw)
        elif kind == "float":
            typed[key] = float(raw)
        elif kind == "bool" and raw in ("true", "false"):
            typed[key] = raw == "true"
        else:
            raise ValueError("query parameter has an unsupported type or value")
    uri = Path(database).resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        connection.execute("PRAGMA query_only=ON")
        return [list(row) for row in connection.execute(sql, typed).fetchmany(MAX_ITEMS + 1)]
    finally:
        connection.close()


def _safe_items(items: list[Any]) -> tuple[list[Any], int]:
    accepted: list[Any] = []
    dropped = 0
    for item in items:
        try:
            serialized = json.dumps(item, ensure_ascii=True, separators=(",", ":"))
            text.check(serialized)
            if text.SECRET_RE.search(serialized):
                raise ValueError("text contains a credential-shaped value")
        except (TypeError, ValueError):
            dropped += 1
            continue
        accepted.append(item)
    return accepted, dropped


def _capped_payload(key: str, items: list[Any], dropped: int) -> dict[str, Any]:
    limited = items[:MAX_ITEMS]
    dropped += max(0, len(items) - len(limited))
    while True:
        payload = {key: limited, "dropped": dropped}
        if len(json.dumps(payload, ensure_ascii=True).encode("utf-8")) <= MAX_BYTES:
            return payload
        if not limited:
            raise ValueError("evidence response metadata exceeds the byte cap")
        limited.pop()
        dropped += 1


def make_handler(config: dict[str, Any], token: str, logger: logging.Logger,
                 audit_logger: Callable[..., None] | None = None):
    """Create an HTTP handler for one immutable allowlist and bearer token."""
    audit = audit_logger or (lambda kind, name, params, size: logger.info(
        "evidence request kind=%s name=%s params=%s size=%d", kind, name,
        json.dumps(params, sort_keys=True), size))

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status: int, payload: dict[str, Any]) -> int:
            data = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return len(data)

        def do_GET(self) -> None:  # noqa: N802
            start = time.monotonic()
            parts = urlsplit(self.path)
            query = parse_qs(parts.query, keep_blank_values=True, strict_parsing=False)
            kind, name, params = "unknown", "", {}
            status, size = 500, 0
            try:
                if self.headers.get("Authorization", "") != "Bearer " + token:
                    status, size = 401, self._reply(401, {"error": "unauthorized"})
                    return
                if parts.path == "/evidence/logs":
                    kind = "logs"
                    name = _single(query, "source")
                    match = _single(query, "match")
                    raw_hours = _single(query, "since")
                    params = {"match": match, "since": raw_hours}
                    if not _valid_name(name) or name not in config["logs"]:
                        status, size = 404, self._reply(404, {"error": "unknown source"})
                        return
                    if not _safe_parameter(match) or not raw_hours.isdigit():
                        raise ValueError("invalid log parameters")
                    hours = int(raw_hours)
                    if not 0 <= hours <= MAX_SINCE_HOURS:
                        raise ValueError("since is outside the allowed range")
                    lines = _log_lines(config["logs"][name], hours, match)
                    safe, dropped = _safe_items(lines)
                    payload = _capped_payload("lines", safe, dropped)
                elif parts.path == "/evidence/query":
                    kind = "query"
                    name = _single(query, "name")
                    params = {key: _single(query, key) for key in query if key != "name"}
                    if not _valid_name(name) or name not in config["queries"]:
                        status, size = 404, self._reply(404, {"error": "unknown query"})
                        return
                    rows = _query_rows(config["queries"][name], params)
                    safe, dropped = _safe_items(rows)
                    payload = _capped_payload("rows", safe, dropped)
                else:
                    status, size = 404, self._reply(404, {"error": "not found"})
                    return
                status, size = 200, self._reply(200, payload)
            except (ValueError, KeyError, TypeError, OverflowError):
                status, size = 400, self._reply(400, {"error": "invalid evidence request"})
            except (OSError, subprocess.SubprocessError):
                status, size = 502, self._reply(502, {"error": "evidence source failed"})
            except sqlite3.Error:
                status, size = 502, self._reply(502, {"error": "evidence source failed"})
            finally:
                safe_name = name if _valid_name(name) else "<invalid>"
                audit(kind, safe_name, _audit_parameters(params), size)
                logger.debug("evidence request completed status=%d elapsed_ms=%d", status,
                             int((time.monotonic() - start) * 1000))

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            logger.info("evidence http request completed")

    return Handler


def _single(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key, [])
    if len(values) != 1:
        raise ValueError("request parameter must appear once")
    return values[0]


def main() -> None:
    config_path = os.environ.get("LOOM_EVIDENCE_CONFIG", "")
    token_ssm = os.environ.get("LOOM_EVIDENCE_TOKEN_SSM", "")
    bind_host = os.environ.get("LOOM_EVIDENCE_BIND_HOST", "")
    tailscale_bin = os.environ.get("LOOM_TAILSCALE_BIN", "tailscale")
    region = os.environ.get("LOOM_AWS_REGION", "")
    raw_port = os.environ.get("LOOM_EVIDENCE_PORT", "7341")
    if not config_path or not token_ssm or not bind_host or not region or not raw_port.isdigit():
        sys.exit("LOOM_EVIDENCE_CONFIG, LOOM_EVIDENCE_TOKEN_SSM, LOOM_AWS_REGION, "
                 "LOOM_EVIDENCE_BIND_HOST and LOOM_EVIDENCE_PORT are required")
    if bind_host in ("0.0.0.0", "::", "127.0.0.1", "localhost") or not 0 < int(raw_port) < 65536:
        sys.exit("evidence service requires a non-loopback interface address and valid port")
    _require_tailnet_address(bind_host, tailscale_bin)
    config = _read_config(config_path)
    token = _read_ssm(token_ssm, region)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logger = logging.getLogger("loom.evidence")
    server = ThreadingHTTPServer((bind_host, int(raw_port)), make_handler(config, token, logger))
    server.serve_forever()


if __name__ == "__main__":
    main()
