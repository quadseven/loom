"""A small GitHub REST and GraphQL client: standard library only.

It counts every request (304s included) by endpoint kind, supports ETags,
remembers the rate limit headers of the last response, and takes an
injectable transport so tests never touch the network. With a cache folder
set, every GET sends the ETag of the last answer to the same URL and a 304
returns that answer: GitHub does not count a 304 against the hourly budget.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

DEFAULT_API = "https://api.github.com"
TIMEOUT = 30

Transport = Callable[[str, str, Dict[str, str], Optional[bytes]],
                     Tuple[int, Dict[str, str], bytes]]


class GitHubError(Exception):
    """A failed GitHub call: the HTTP status and the response body text."""

    def __init__(self, message: str, status: int = 0, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class Response:
    def __init__(self, status: int, data: Any = None,
                 etag: Optional[str] = None,
                 headers: Optional[Dict[str, str]] = None) -> None:
        self.status = status
        self.data = data
        self.etag = etag
        self.headers = headers if headers is not None else {}


def request_kind(method: str, url: str) -> str:
    """The endpoint kind a request is counted under in the station's log."""
    path = url.split("://", 1)[-1].split("/", 1)[-1].split("?", 1)[0]
    path = "/" + path.rstrip("/")
    if path == "/graphql":
        return "graphql"
    if method != "GET":
        return "writes"
    if "/check-runs" in path or re.search(r"/commits/[^/]+/status$", path):
        return "check runs"
    if path.endswith("/comments"):
        return "comments"
    if path.endswith("/events"):
        return "events"
    if re.search(r"^/repos/[^/]+/[^/]+/issues$", path):
        return "issues list"
    if re.search(r"^/repos/[^/]+/[^/]+/pulls$", path):
        return "pulls list"
    return "other"


def _int(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib would follow a redirect with the Authorization header still
    attached, to whatever host it names; hand the 3xx back instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def urllib_transport(method: str, url: str, headers: Dict[str, str],
                     body: Optional[bytes]) -> Tuple[int, Dict[str, str], bytes]:
    if not url.startswith("https://"):
        raise GitHubError("refusing a non-https URL: %s" % url.split("?")[0])
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        # https only (checked above), never file: or custom schemes.
        with _OPENER.open(req, timeout=TIMEOUT) as resp:  # noqa: S310
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as err:
        try:
            return err.code, dict(err.headers.items()), err.read()
        finally:
            err.close()
    except (urllib.error.URLError, OSError) as err:
        raise GitHubError("request failed: %s" % err, 0, "") from None


def _json_or_none(text: str) -> Any:
    """The parsed body, or None for an empty or non-JSON one."""
    if not text.strip():
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


class GitHub:
    def __init__(self, token: str, api: str = DEFAULT_API,
                 transport: Optional[Transport] = None,
                 cache_dir: Optional[str] = None) -> None:
        self._token = token
        self.api = api.rstrip("/")
        self.transport = transport or urllib_transport
        # Where GET answers are kept for conditional requests; None sends none.
        self.cache_dir = cache_dir
        self.requests = 0
        self.not_modified = 0
        # Requests by request_kind, 304s left out: what the budget counted.
        self.kinds: Dict[str, int] = {}
        # resource (core, graphql, ...) -> (remaining, limit, reset epoch),
        # from the newest response that carried the headers.
        self.rate: Dict[str, Tuple[int, int, int]] = {}

    def _url(self, path: str) -> str:
        # Absolute URLs (pagination's next links) must stay on the API host:
        # the token goes with every request.
        if "://" in path:
            if not path.startswith(self.api + "/"):
                raise GitHubError("refusing to send the token outside %s: %s"
                                  % (self.api, path.split("?")[0]))
            return path
        if not path.startswith("/"):
            raise GitHubError("API paths start with /: %s" % path)
        return self.api + path

    def rest(self, method: str, path: str, body: Any = None,
             etag: Optional[str] = None, ok: Tuple[int, ...] = ()) -> Response:
        url = self._url(path)
        # A caller's own ETag means the caller keeps the answer; otherwise
        # the cache does, when there is one.
        cached = self._cached(url) if method == "GET" and not etag else None
        headers, payload = self._request(body, etag or (cached or {}).get("etag"))
        status, resp_headers, text = self._send(method, url, headers, payload)
        if not (200 <= status < 300 or status == 304 or status in ok):
            raise GitHubError("%s %s -> %d" % (method, path, status), status, text)
        if status == 304 and cached is not None:
            return self._from_cache(url, cached, resp_headers)
        data = _json_or_none(text)
        if method == "GET" and not etag:
            self._keep_answer(url, status, data, resp_headers)
        return Response(status, data, resp_headers.get("etag"), resp_headers)

    def _request(self, body: Any,
                 if_none_match: Optional[str]) -> Tuple[Dict[str, str], Optional[bytes]]:
        """The headers and encoded body for one request."""
        headers = {
            "Authorization": "Bearer " + self._token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "loom-bus",
        }
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if if_none_match:
            headers["If-None-Match"] = if_none_match
        return headers, payload

    def _send(self, method: str, url: str, headers: Dict[str, str],
              payload: Optional[bytes]) -> Tuple[int, Dict[str, str], str]:
        """One request on the wire, counted: status, lower-cased headers, text."""
        self.requests += 1
        status, raw_headers, raw = self.transport(method, url,
                                                  headers, payload)
        resp_headers = {str(k).lower(): v for k, v in (raw_headers or {}).items()}
        # Read before any error is raised: a 403 at the limit carries them.
        self._note_rate(resp_headers)
        self._count(method, url, status)
        return status, resp_headers, (raw or b"").decode("utf-8", "replace")

    def _keep_answer(self, url: str, status: int, data: Any,
                     resp_headers: Dict[str, str]) -> None:
        """Keep a full GET answer that carries an ETag for the next read."""
        if status == 200 and resp_headers.get("etag"):
            self._keep(url, {"etag": resp_headers["etag"], "data": data,
                             "link": resp_headers.get("link", "")})

    def _count(self, method: str, url: str, status: int) -> None:
        """Tally one answer: a 304 apart, anything else by its kind."""
        if status == 304:
            self.not_modified += 1
            return
        kind = request_kind(method, url)
        self.kinds[kind] = self.kinds.get(kind, 0) + 1

    def _from_cache(self, url: str, cached: Dict[str, Any],
                    resp_headers: Dict[str, str]) -> Response:
        """A 304 answered from the kept entry, with its pagination link."""
        self._touch(url)
        resp_headers.setdefault("link", cached.get("link") or "")
        return Response(304, cached.get("data"), cached["etag"], resp_headers)

    # -- the rate limit ------------------------------------------------
    def _note_rate(self, headers: Dict[str, str]) -> None:
        remaining = _int(headers.get("x-ratelimit-remaining"))
        limit = _int(headers.get("x-ratelimit-limit"))
        reset = _int(headers.get("x-ratelimit-reset"))
        if remaining is None or limit is None or reset is None or limit <= 0:
            return
        resource = str(headers.get("x-ratelimit-resource") or "core").strip() or "core"
        self.rate[resource] = (remaining, limit, reset)

    # -- the conditional request cache ---------------------------------
    def _cache_path(self, url: str) -> str:
        assert self.cache_dir
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:40]
        return os.path.join(self.cache_dir, key + ".json")

    def _cached(self, url: str) -> Optional[Dict[str, Any]]:
        """The kept answer for this URL, or None. A lost or unreadable entry
        only costs one full read."""
        if not self.cache_dir:
            return None
        try:
            with open(self._cache_path(url), encoding="utf-8") as fh:
                entry = json.load(fh)
        except (OSError, ValueError):
            return None
        if not isinstance(entry, dict) or not isinstance(entry.get("etag"), str) \
                or not entry["etag"] or entry.get("url") != url:
            return None
        return entry

    def _keep(self, url: str, entry: Dict[str, Any]) -> None:
        if not self.cache_dir:
            return
        path = self._cache_path(url)
        tmp = "%s.%d.tmp" % (path, os.getpid())
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(dict(entry, url=url), fh)
            os.replace(tmp, path)
        except (OSError, TypeError, ValueError):
            # A cache that cannot be written costs a full read next time.
            try:
                os.remove(tmp)
            except OSError:
                pass

    def _touch(self, url: str) -> None:
        try:
            os.utime(self._cache_path(url))
        except OSError:
            pass

    def prune_cache(self, keep: float, now: Optional[float] = None) -> int:
        """Remove kept answers no request has used for `keep` seconds (a
        finished pull request's check runs, say); returns how many."""
        if not self.cache_dir:
            return 0
        now = time.time() if now is None else now
        try:
            names = os.listdir(self.cache_dir)
        except OSError:
            return 0
        removed = 0
        for name in names:
            path = os.path.join(self.cache_dir, name)
            try:
                if now - os.stat(path).st_mtime > keep:
                    os.remove(path)
                    removed += 1
            except OSError:
                continue
        return removed

    def paginate(self, path: str) -> List[Any]:
        if "per_page=" not in path:
            path += ("&" if "?" in path else "?") + "per_page=100"
        items: List[Any] = []
        url: Optional[str] = path
        while url:
            resp = self.rest("GET", url)
            if isinstance(resp.data, list):
                items.extend(resp.data)
            url = _next_link(resp.headers.get("link", ""))
        return items

    def graphql(self, query: str, variables: Optional[Dict[str, Any]] = None) -> Any:
        resp = self.rest("POST", "/graphql",
                         {"query": query, "variables": variables or {}})
        data = resp.data if isinstance(resp.data, dict) else {}
        if data.get("errors"):
            raise GitHubError("graphql errors", resp.status, json.dumps(data["errors"]))
        return data.get("data")


def _next_link(header: str) -> Optional[str]:
    for part in header.split(","):
        m = re.match(r'\s*<([^>]+)>\s*;(.*)', part)
        if m and re.search(r'rel\s*=\s*"?next"?', m.group(2)):
            return m.group(1)
    return None


def token_from_env() -> str:
    import os
    for name in ("LOOM_GITHUB_TOKEN", "GITHUB_TOKEN"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True,
                             text=True, timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        out = None
    if out is not None and out.returncode == 0 and out.stdout.strip():
        return out.stdout.strip()
    raise GitHubError("no GitHub token: set LOOM_GITHUB_TOKEN or GITHUB_TOKEN, "
                      "or run `gh auth login`")
