"""A small GitHub REST and GraphQL client: standard library only.

It counts every request (304s included), supports ETags, and takes an
injectable transport so tests never touch the network.
"""
from __future__ import annotations

import json
import re
import subprocess
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


def urllib_transport(method: str, url: str, headers: Dict[str, str],
                     body: Optional[bytes]) -> Tuple[int, Dict[str, str], bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as err:
        try:
            return err.code, dict(err.headers.items()), err.read()
        finally:
            err.close()
    except (urllib.error.URLError, OSError) as err:
        raise GitHubError("request failed: %s" % err, 0, "") from None


class GitHub:
    def __init__(self, token: str, api: str = DEFAULT_API,
                 transport: Optional[Transport] = None) -> None:
        self._token = token
        self.api = api.rstrip("/")
        self.transport = transport or urllib_transport
        self.requests = 0

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return self.api + path

    def rest(self, method: str, path: str, body: Any = None,
             etag: Optional[str] = None, ok: Tuple[int, ...] = ()) -> Response:
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
        if etag:
            headers["If-None-Match"] = etag
        self.requests += 1
        status, raw_headers, raw = self.transport(method, self._url(path),
                                                  headers, payload)
        resp_headers = {str(k).lower(): v for k, v in (raw_headers or {}).items()}
        text = (raw or b"").decode("utf-8", "replace")
        if not (200 <= status < 300 or status == 304 or status in ok):
            raise GitHubError("%s %s -> %d" % (method, path, status), status, text)
        data = None
        if text.strip():
            try:
                data = json.loads(text)
            except ValueError:
                data = None
        return Response(status, data, resp_headers.get("etag"), resp_headers)

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
