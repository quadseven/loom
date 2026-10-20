"""The background reads behind the Tiers and allowances page (loom#968).

Each source is read once a refresh pass (dashboard/github.py
TicketStore.refresh, every LOOM_DASHBOARD_REFRESH_SECONDS), never by a page
request. A source keeps its last good answer and the time it was read; a read
that fails, times out or answers 503 marks it stale with the error and keeps
that last answer, so the page shows old figures as old and never a missing
figure as zero.

- The inference gateway, at LOOM_GATEWAY_URL: `/ui/stats?window=24h` and
  `?window=7d` (per provider and model counts, and the breaker lanes cooling
  now) and `/ui/state` (the calls in flight now). The dashboard reaches it
  inside its own cluster, with no token.
- Grug's stats endpoint, at LOOM_GRUG_STATS_URL with the optional bearer
  token LOOM_GRUG_STATS_TOKEN: its in-flight reviews and its review order.
- Loom's own lane runs per lane and model, from the transcript store
  (dashboard/transcripts.py Store.model_outcomes).
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Mapping, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

LOG = logging.getLogger(__name__)

# Seconds one read may take. The page never waits on it: a slow source
# delays only the background pass, by at most this long (the reads run side
# by side).
TIMEOUT = 5.0
# A last good answer older than this is stale even if no read failed: the
# refresh loop itself stopped.
STALE_AFTER = 600
# The most bytes one answer may carry.
MAX_BYTES = 4 * 1024 * 1024
WINDOWS = ("24h", "7d")
NAMES = {"gateway_24h": "Gateway stats, 24 hours", "gateway_7d": "Gateway stats, 7 days",
         "gateway_state": "Gateway in-flight calls", "grug": "Grug stats",
         "loom_runs": "Loom run outcomes"}


class SourceError(Exception):
    """A read that answered, but not with usable data."""


class _NoRedirects(HTTPRedirectHandler):
    """Installed in fetch_json's opener: urllib calls redirect_request on a
    3xx answer, and refusing it keeps the Grug token from following a
    redirect to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SourceError("answered %d, a redirect; redirects are not followed" % code)


def fetch_json(url: str, token: str = "", timeout: float = TIMEOUT) -> Dict[str, Any]:
    """GET url and return its JSON object. Any status but 200, a redirect, a
    timeout or an answer that is not a JSON object raises, with the reason; a
    503's own `error` field is part of the reason."""
    if urlsplit(url).scheme not in ("http", "https"):
        raise SourceError("only http and https URLs are read")
    headers = {"Accept": "application/json", "User-Agent": "loom-dashboard"}
    if token:
        headers["Authorization"] = "Bearer " + token
    opener = build_opener(_NoRedirects)
    try:
        # S310: the scheme is checked above, so no file: or custom URL opens.
        with opener.open(Request(url, headers=headers), timeout=timeout) as response:  # noqa: S310
            raw = response.read(MAX_BYTES + 1)
    except HTTPError as exc:
        raise SourceError("answered %d%s" % (exc.code, _error_field(exc))) from None
    except (socket.timeout, TimeoutError):
        raise SourceError("timed out after %gs" % timeout) from None
    except URLError as exc:
        reason = exc.reason
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise SourceError("timed out after %gs" % timeout) from None
        raise SourceError("unreachable: %s" % reason) from None
    if len(raw) > MAX_BYTES:
        raise SourceError("answer larger than %d bytes" % MAX_BYTES)
    try:
        value = json.loads(raw)
    except ValueError:
        raise SourceError("unexpected answer: not JSON") from None
    if not isinstance(value, dict):
        raise SourceError("unexpected answer: not a JSON object")
    return value


def _error_field(exc: HTTPError) -> str:
    """': <error>' from an HTTP error's JSON body, or ''."""
    try:
        body = json.loads(exc.read(64 * 1024) or b"{}")
    except (OSError, ValueError):
        return ""
    finally:
        exc.close()
    error = body.get("error") if isinstance(body, dict) else None
    return ": %s" % str(error)[:300] if error else ""


class Source:
    """One upstream read and its last good answer."""

    def __init__(self, name: str, fetch: Optional[Callable[[], Any]], missing: str = "",
                 secrets: Sequence[str] = ()) -> None:
        self.name = name
        self.fetch = fetch
        # Values never written into an error, a log line or the page (the
        # Grug bearer token), whatever an exception's text carries.
        self.secrets = tuple(secret for secret in secrets if secret)
        self.missing = missing if fetch is None else ""
        self.data: Any = None
        self.last_good_at: Optional[float] = None
        self.read_at: Optional[float] = None
        self.error = "not read yet"
        # The refresh thread writes while page requests read.
        self._lock = threading.Lock()

    def refresh(self, now: Optional[float] = None) -> None:
        if self.fetch is None:
            return
        try:
            data = self.fetch()
        # Broad on purpose: this runs on the background refresh thread, and
        # one source's failure must neither end the loop nor touch the others.
        except Exception as exc:  # noqa: BLE001
            error = self._redact(str(exc) or type(exc).__name__)
            LOG.warning("%s read failed: %s", self.name, error)
            with self._lock:
                self.error, self.read_at = error, time.time() if now is None else now
            return
        with self._lock:
            at = time.time() if now is None else now
            self.data, self.last_good_at, self.read_at, self.error = data, at, at, ""

    def _redact(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, "[redacted]")
        return text

    def envelope(self, now: Optional[float] = None) -> Dict[str, Any]:
        """{name, configured, missing, data, last_good_at, read_at, stale,
        error}: data is the last good answer (None before one), and stale says
        it cannot be taken as current."""
        now = time.time() if now is None else now
        with self._lock:
            data, last_good_at, read_at, error = self.data, self.last_good_at, self.read_at, self.error
        configured = self.fetch is not None
        error = error if configured else ""
        if configured and not error and last_good_at is not None and now - last_good_at > STALE_AFTER:
            error = "no read since %ds ago" % int(now - last_good_at)
        return {"name": self.name, "configured": configured, "missing": self.missing, "data": data,
                "last_good_at": last_good_at, "read_at": read_at,
                "stale": configured and bool(error), "error": error}


def _gateway_stats(base: str, window: str, timeout: float) -> Callable[[], Dict[str, Any]]:
    def fetch() -> Dict[str, Any]:
        value = fetch_json("%s/ui/stats?window=%s" % (base, window), timeout=timeout)
        if not isinstance(value.get("rows"), list):
            raise SourceError("unexpected answer: no rows list")
        return value
    return fetch


def _gateway_state(base: str, timeout: float) -> Callable[[], Dict[str, Any]]:
    def fetch() -> Dict[str, Any]:
        value = fetch_json(base + "/ui/state", timeout=timeout)
        if not isinstance(value.get("inflight", []), list):
            raise SourceError("unexpected answer: inflight is not a list")
        return value
    return fetch


def _base_url(value: str) -> str:
    """The configured URL without its trailing slash, or "" when it is not an
    http(s) URL with a host."""
    value = value.strip().rstrip("/")
    parsed = urlsplit(value)
    return value if parsed.scheme in ("http", "https") and parsed.hostname else ""


class TierSources:
    """Every source of the Tiers and allowances page, read side by side."""

    def __init__(self, env: Optional[Mapping[str, str]] = None,
                 runs_store: Optional[Callable[[], Any]] = None, timeout: float = TIMEOUT) -> None:
        env = os.environ if env is None else env
        base = _base_url(env.get("LOOM_GATEWAY_URL", ""))
        gateway_missing = ("LOOM_GATEWAY_URL is not set" if not env.get("LOOM_GATEWAY_URL", "").strip()
                           else "LOOM_GATEWAY_URL is not an http(s) URL")
        grug_url = _base_url(env.get("LOOM_GRUG_STATS_URL", ""))
        grug_token = env.get("LOOM_GRUG_STATS_TOKEN", "").strip()

        def gateway(fetch: Callable[[], Any]) -> Optional[Callable[[], Any]]:
            return fetch if base else None

        def runs() -> Any:
            if runs_store is None:
                raise SourceError("no transcript store")
            return runs_store().model_outcomes()

        self.sources: Dict[str, Source] = {
            "gateway_24h": Source(NAMES["gateway_24h"], gateway(_gateway_stats(base, "24h", timeout)),
                                  missing=gateway_missing),
            "gateway_7d": Source(NAMES["gateway_7d"], gateway(_gateway_stats(base, "7d", timeout)),
                                 missing=gateway_missing),
            "gateway_state": Source(NAMES["gateway_state"], gateway(_gateway_state(base, timeout)),
                                    missing=gateway_missing),
            "grug": Source(NAMES["grug"],
                           (lambda: fetch_json(grug_url, token=grug_token, timeout=timeout)) if grug_url else None,
                           missing="not configured yet: LOOM_GRUG_STATS_URL is %s"
                           % ("not an http(s) URL" if env.get("LOOM_GRUG_STATS_URL", "").strip() else "not set"),
                           secrets=(grug_token,)),
            "loom_runs": Source(NAMES["loom_runs"], runs),
        }

    def refresh(self, now: Optional[float] = None) -> None:
        """Read every configured source once, side by side."""
        now = time.time() if now is None else now
        with ThreadPoolExecutor(max_workers=len(self.sources)) as pool:
            list(pool.map(lambda source: source.refresh(now), self.sources.values()))

    def snapshot(self, now: Optional[float] = None) -> Dict[str, Dict[str, Any]]:
        return {name: source.envelope(now) for name, source in self.sources.items()}


def empty_snapshot(env: Optional[Mapping[str, str]] = None) -> Dict[str, Dict[str, Any]]:
    """What a dashboard with no background reads shows: every configured
    source not read yet."""
    return TierSources(env=env).snapshot()
