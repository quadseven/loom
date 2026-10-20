"""A small GitHub REST and GraphQL client: standard library only.

It counts every request (304s included) by endpoint kind, supports ETags,
remembers the rate limit headers of the last response, and takes an
injectable transport so tests never touch the network. With a cache folder
set, every GET sends the ETag of the last answer to the same URL and a 304
returns that answer: GitHub does not count a 304 against the hourly budget.

With the GitHub App settings present (auth_from_env), requests carry an App
installation token instead of a personal token, so they spend the
installation's own hourly budget. The token is minted at run time from the
App's private key, read from SSM, kept in memory only, and refreshed before
it expires.
"""
from __future__ import annotations

import base64
import calendar
import hashlib
import http.client
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from . import proof, status

DEFAULT_API = "https://api.github.com"
TIMEOUT = 30
# A read that failed on the wire is tried again after these pauses (seconds).
RETRY_DELAYS = (2.0, 6.0)
RETRY_STATUSES = (502, 503, 504)
# An installation token is minted again this many seconds before it expires.
APP_TOKEN_MARGIN = 300
# The App's JWT: issued a minute in the past (clock drift), valid 9 minutes.
APP_JWT_BACKDATE = 60
APP_JWT_LIFETIME = 540
APP_SETTINGS = ("LOOM_GITHUB_APP_ID", "LOOM_GITHUB_APP_INSTALLATION_ID",
                "LOOM_GITHUB_APP_KEY_SSM")
# The App's private key as a mounted file (a Kubernetes secret), in place of
# the parameter-store setting; it wins when both are set.
APP_KEY_FILE = "LOOM_GITHUB_APP_KEY_FILE"

Transport = Callable[[str, str, Dict[str, str], Optional[bytes]],
                     Tuple[int, Dict[str, str], bytes]]

# A PATCH to one of these that sets state=closed is an issue close; every
# programmatic close in the factory funnels through GitHub.rest, so this is
# the choke point the close-completeness gate watches.
_ISSUE_PATH = re.compile(r"^/repos/[^/]+/[^/]+/issues/[0-9]+$")

# Labels that let an issue close as completed with unchecked boxes, mirroring
# the close-completeness guard's escape hatches.
_FORCE_CLOSE_LABELS = ("force-close", "wontfix")


class GitHubError(Exception):
    """A failed GitHub call: the HTTP status and the response body text."""

    def __init__(self, message: str, status: int = 0, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class RateLimited(GitHubError):
    """GitHub refused a call for budget: a 403 or 429 with no requests left in
    the window, or its secondary rate limit. `until` is when the window
    resets (epoch seconds). A GitHubError, so every caller that handles one
    keeps working; the station ends its pass on it (loom#666)."""

    def __init__(self, message: str, status: int, body: str, until: int) -> None:
        super().__init__(message, status, body)
        self.until = until


# GitHub's words for a budget refusal, primary or secondary, when the
# x-ratelimit-remaining header alone does not say so.
RATE_LIMIT_RE = re.compile(r"\b(?:secondary )?rate limit\b", re.I)


def _utc(stamp: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp))


def rate_limit_until(status: int, headers: Dict[str, str], text: str,
                     now: float) -> Optional[int]:
    """When a refused call may be tried again, if GitHub refused it for
    budget, else None. A 403 also means a missing grant, so only the rate
    limit headers or GitHub's rate limit message make it a budget refusal."""
    if status not in (403, 429):
        return None
    remaining = _int(headers.get("x-ratelimit-remaining"))
    if remaining != 0 and not RATE_LIMIT_RE.search(text or ""):
        return None
    reset = _int(headers.get("x-ratelimit-reset"))
    after = _int(headers.get("retry-after"))
    if after is not None and after >= 0:
        return int(now) + after
    if reset is not None and remaining == 0 and reset > now:
        return reset
    # GitHub asks for at least a minute's wait when it names no time.
    return int(now) + 60


class Response:
    def __init__(self, status: int, data: Any = None,
                 etag: Optional[str] = None,
                 headers: Optional[Dict[str, str]] = None) -> None:
        self.status = status
        self.data = data
        self.etag = etag
        self.headers = headers if headers is not None else {}


def rest_login(author: Optional[Dict[str, Any]]) -> str:
    """A GraphQL author's login in the form REST, and so LOOM_BRIEF_AUTHORS,
    uses: GraphQL names a GitHub App "<slug>" and REST "<slug>[bot]". Only an
    author whose __typename is Bot gains the suffix, so a person who takes the
    bare slug as a user login is never read as the App."""
    author = author or {}
    login = str(author.get("login") or "")
    if login and author.get("__typename") == "Bot" and not login.lower().endswith("[bot]"):
        login += "[bot]"
    return login


def unreadable(err: GitHubError) -> Optional[str]:
    """Why GitHub will not show a repo or item at all, as one line: it moved
    (301, a rename or transfer), is gone (404, 410), or is forbidden (403
    that is not a rate limit), or a GraphQL NOT_FOUND. None for any other
    failure (5xx, a rate limit, a dropped connection), which must still end
    the command: only these are safe to skip and name."""
    body = err.body or ""
    parsed = _json_or_none(body)
    message = str(parsed.get("message") or "") if isinstance(parsed, dict) else ""
    if err.status in (301, 302, 307, 308):
        return "HTTP %d %s: renamed or transferred" % (err.status, message or "moved")
    if err.status in (404, 410):
        return "HTTP %d %s" % (err.status, message or "not found")
    lowered = body.lower()
    if err.status == 403 and "rate limit" not in lowered and "abuse" not in lowered:
        return "HTTP 403 %s" % (message or "forbidden")
    if str(err) == "graphql errors" and '"NOT_FOUND"' in body:
        return "GraphQL NOT_FOUND"
    return None


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
    except (urllib.error.URLError, OSError, http.client.HTTPException) as err:
        # HTTPException covers IncompleteRead: a connection GitHub closed in
        # the middle of a body, which is not an OSError and used to escape
        # every caller's GitHubError handling.
        raise GitHubError("request failed: %r" % err, 0, "") from None


def _json_or_none(text: str) -> Any:
    """The parsed body, or None for an empty or non-JSON one."""
    if not text.strip():
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _proxy_env_for_aws() -> Dict[str, str]:
    """Return an environment dict for the AWS CLI with only https_proxy set.

    The AWS CLI fails when multiple proxy variables are present. It works with
    only https_proxy (or HTTPS_PROXY). Lowercase https_proxy is preferred when
    both are set.
    """
    env = os.environ.copy()
    # Remove all proxy variables first
    for key in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY",
                "all_proxy", "ALL_PROXY"):
        env.pop(key, None)
    # Restore only https_proxy (lowercase preferred)
    lower = os.environ.get("https_proxy")
    upper = os.environ.get("HTTPS_PROXY")
    if lower is not None:
        env["https_proxy"] = lower
    elif upper is not None:
        env["HTTPS_PROXY"] = upper
    return env


def read_ssm(name: str, region: str) -> str:
    """A SecureString parameter, read the way lanes/run_lane.sh reads keys.
    The read is bounded by TIMEOUT, so a hung call cannot stall a pass. The
    error never carries the value."""
    out = subprocess.run(
        ["aws", "ssm", "get-parameter", "--region", region, "--name", name,
         "--with-decryption", "--query", "Parameter.Value", "--output", "text"],
        capture_output=True, text=True, timeout=TIMEOUT, env=_proxy_env_for_aws())
    if out.returncode != 0 or not out.stdout.strip():
        raise OSError("could not read the parameter from SSM")
    return out.stdout.strip()


def read_key_file(path: str) -> str:
    """The App's private key from a mounted file, read at each mint."""
    with open(path, encoding="ascii") as fh:
        return fh.read()


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class SigningError(GitHubError):
    """openssl_sign failed; its text never carries the key or openssl's output."""


def openssl_sign(data: bytes, key: str) -> bytes:
    """The RS256 (RSASSA-PKCS1-v1_5, SHA-256) signature of data.

    The standard library cannot sign with RSA, so the openssl CLI does: the
    private key goes in on stdin and the data on a pipe, so neither is on
    the command line or on disk. openssl's own error text is never passed on."""
    out: Optional[subprocess.CompletedProcess] = None
    failure = ""
    read_fd, write_fd = os.pipe()
    try:
        try:
            # The JWT's header and claims fit in the pipe's buffer.
            os.write(write_fd, data)
        finally:
            os.close(write_fd)
        out = subprocess.run(
            ["openssl", "dgst", "-sha256", "-binary", "-sign", "/dev/stdin",
             "/dev/fd/%d" % read_fd],
            input=key.encode("utf-8"), capture_output=True, pass_fds=(read_fd,),
            timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError) as err:
        failure = "openssl could not run (%s)" % type(err).__name__
    finally:
        os.close(read_fd)
    if out is None or out.returncode != 0 or not out.stdout:
        raise SigningError(failure or "openssl failed (exit %d)" % (out.returncode if out else -1))
    return out.stdout


def _expiry(value: Any) -> float:
    """expires_at ("2026-10-17T18:00:00Z") as epoch seconds."""
    try:
        return float(calendar.timegm(time.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError, OverflowError):
        pass
    raise GitHubError("the installation token answer has no readable expires_at")


class AppToken:
    """A GitHub App installation token, minted from the App's private key.

    The key is read through read_key each time a token is minted and dropped
    after signing; the token is kept in memory until APP_TOKEN_MARGIN seconds
    before its expires_at. Neither is written to disk, and no error or log
    line carries either. Thread safe: parallel reads share one token."""

    def __init__(self, app_id: str, installation_id: str,
                 read_key: Callable[[], str],
                 sign: Callable[[bytes, str], bytes] = openssl_sign,
                 clock: Callable[[], float] = time.time) -> None:
        if not (str(app_id).isdigit() and str(installation_id).isdigit()):
            raise GitHubError("the GitHub App id and installation id must be numbers")
        self.app_id = str(app_id)
        self.installation_id = str(installation_id)
        self._read_key = read_key
        self._sign = sign
        self._clock = clock
        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._expires = 0.0
        self.minted = 0
        # Where the key is read from: "ssm" (the aws CLI) or "file".
        self.key_source = "ssm"
        self.key_file = ""

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None,
                 read_key: Optional[Callable[[str, str], str]] = None
                 ) -> Optional["AppToken"]:
        """The App token the settings name, or None when any is unset. The
        key comes from LOOM_GITHUB_APP_KEY_FILE when that is set, else from
        LOOM_GITHUB_APP_KEY_SSM."""
        env = os.environ if env is None else env
        app_id, installation_id, key_ssm = (env.get(n, "").strip() for n in APP_SETTINGS)
        key_file = env.get(APP_KEY_FILE, "").strip()
        if not (app_id and installation_id and (key_ssm or key_file)):
            return None
        if key_file:
            app = cls(app_id, installation_id, lambda: read_key_file(key_file))
            app.key_source, app.key_file = "file", key_file
            return app
        region = env.get("LOOM_AWS_REGION", "").strip() or "us-east-1"
        reader = read_key or read_ssm
        return cls(app_id, installation_id, lambda: reader(key_ssm, region))

    def jwt(self) -> str:
        """The App's RS256 JWT, signed with the key read just now."""
        now = int(self._clock())
        header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"},
                                    separators=(",", ":")).encode("utf-8"))
        claims = _b64url(json.dumps({"iat": now - APP_JWT_BACKDATE,
                                     "exp": now + APP_JWT_LIFETIME,
                                     "iss": self.app_id},
                                    separators=(",", ":")).encode("utf-8"))
        signing_input = (header + "." + claims).encode("ascii")
        # Only the failures a key read or a signing can have are caught: the
        # reader's OSError (SSM unreadable, aws missing) or SubprocessError
        # (timeout), and the signer's SigningError, OSError or SubprocessError.
        # They are reported by kind, since their text could carry the key, and
        # raised outside the except blocks so none is chained as context. Any
        # other exception is a bug and propagates as itself.
        failure = ""
        try:
            key = self._read_key()
        except (OSError, subprocess.SubprocessError) as err:
            failure = "could not read the GitHub App private key: %s" % type(err).__name__
        if failure:
            raise GitHubError(failure)
        try:
            signature = self._sign(signing_input, key)
        except (SigningError, OSError, subprocess.SubprocessError) as err:
            failure = "could not sign the GitHub App JWT: %s" % (
                str(err) if isinstance(err, SigningError) else type(err).__name__)
        finally:
            del key
        if failure:
            raise GitHubError(failure)
        return signing_input.decode("ascii") + "." + _b64url(signature)

    def token(self, mint: Callable[[str, str], Tuple[str, Any]]) -> str:
        """The cached installation token, minted again through mint(path,
        jwt) -> (token, expires_at) when none is held or it expires within
        APP_TOKEN_MARGIN seconds."""
        with self._lock:
            if self._token is None or self._clock() >= self._expires - APP_TOKEN_MARGIN:
                path = "/app/installations/%s/access_tokens" % self.installation_id
                token, expires_at = mint(path, self.jwt())
                self._token, self._expires = token, _expiry(expires_at)
                self.minted += 1
            return self._token

    def invalidate(self) -> None:
        """Forget the token (GitHub refused it): the next request mints one."""
        with self._lock:
            self._token = None
            self._expires = 0.0

    def __repr__(self) -> str:
        return "AppToken(installation)"


Auth = Union[str, AppToken]


class GitHub:
    def __init__(self, token: Auth, api: str = DEFAULT_API,
                 transport: Optional[Transport] = None,
                 cache_dir: Optional[str] = None,
                 sleep: Callable[[float], None] = time.sleep,
                 unsubscribe: bool = False) -> None:
        # A personal token string, or an AppToken minted on first use.
        self._token = token
        # With a personal token, leave each issue this client opens or
        # comments on: GitHub subscribes the person, and every later bot
        # comment there emailed them. An @mention still notifies.
        self.unsubscribe = unsubscribe
        self._node_ids: Dict[Tuple[str, int], str] = {}
        self.unsubscribe_failures: List[str] = []
        self.app: Optional[AppToken] = token if isinstance(token, AppToken) else None
        # What the budget lines name: an App installation or a personal token.
        self.auth = "app" if self.app is not None else "token"
        self._app_login: Optional[str] = None
        # Logins whose status comments read as events (bus/status.py): this
        # client's own App bot login is added on first use; a caller adds its
        # trusted writers (the station its LOOM_BRIEF_AUTHORS).
        self.status_authors: set = set()
        self._sleep = sleep
        self.api = api.rstrip("/")
        self.transport = transport or urllib_transport
        # Where GET answers are kept for conditional requests; None sends none.
        self.cache_dir = cache_dir
        # Reads may run on several threads (brain.status): counts change under this lock.
        self._count_lock = threading.Lock()
        self.requests = 0
        self.not_modified = 0
        # Requests by request_kind, 304s left out: what the budget counted.
        self.kinds: Dict[str, int] = {}
        self.issue_writes: set[Tuple[str, int]] = set()
        self.comment_edits = False
        # resource (core, graphql, ...) -> (remaining, limit, reset epoch),
        # from the newest response that carried the headers.
        self.rate: Dict[str, Tuple[int, int, int]] = {}
        # Set by a budget refusal: until then every call fails at once with
        # RateLimited and spends nothing (loom#666).
        self.limited_until: Optional[int] = None

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
        resp = self._rest(method, path, body, etag, ok)
        if method == "POST" and self.unsubscribe and self.app is None:
            self._leave_thread(path, resp.data)
        if method == "GET" and status.COMMENTS_RE.search(path):
            # A ticket's status comment reads as one comment per event, so
            # every reader of those events sees them as before (loom#866).
            expanded = status.expand(resp.data, self._status_logins())
            if expanded is not resp.data:
                resp = Response(resp.status, expanded, resp.etag, resp.headers)
        return resp

    def _rest(self, method: str, path: str, body: Any = None,
              etag: Optional[str] = None, ok: Tuple[int, ...] = ()) -> Response:
        self._refuse_incomplete_close(method, path, body)
        url = self._url(path)
        # A caller's own ETag means the caller keeps the answer; otherwise
        # the cache does, when there is one.
        cached = self._cached(url) if method == "GET" and not etag else None
        headers, payload = self._request(body, etag or (cached or {}).get("etag"))
        status, resp_headers, text = self._send(method, url, headers, payload)
        self._check_status(method, path, status, text, ok)
        if status == 304 and cached is not None:
            return self._from_cache(url, cached, resp_headers)
        data = _json_or_none(text)
        if method == "GET" and not etag:
            self._keep_answer(url, status, data, resp_headers)
        if method != "GET":
            self._note_write(path, data)
        return Response(status, data, resp_headers.get("etag"), resp_headers)

    def _leave_thread(self, path: str, data: Any) -> None:
        """Unsubscribe the token's person from the issue a POST opened or
        commented on. A failure is kept in unsubscribe_failures, never raised:
        the write itself already happened."""
        opened = re.match(r"^/repos/([^/]+/[^/]+)/issues$", path)
        commented = re.match(r"^/repos/([^/]+/[^/]+)/issues/(\d+)/comments$", path)
        if not (opened or commented) or not isinstance(data, dict):
            return
        try:
            if opened:
                node = str(data.get("node_id") or "")
                key = (opened.group(1), int(data.get("number") or 0))
            else:
                key = (commented.group(1), int(commented.group(2)))
                node = self._node_ids.get(key) or str(
                    (self._rest("GET", "/repos/%s/issues/%d" % key).data or {}).get("node_id") or "")
            if not node:
                raise GitHubError("no node id for %s#%d" % key)
            self._node_ids[key] = node
            self.graphql("mutation($id: ID!) { updateSubscription(input: "
                         "{subscribableId: $id, state: UNSUBSCRIBED}) "
                         "{ subscribable { viewerSubscription } } }", {"id": node})
        except (GitHubError, ValueError, TypeError) as err:
            self.unsubscribe_failures.append("%s: %s" % (path, err))

    def _check_status(self, method: str, path: str, status: int, text: str,
                      ok: Tuple[int, ...]) -> None:
        """Raise GitHubError for an answer the caller did not accept."""
        if status == 401 and self.app is not None:
            # A revoked or early-expired installation token: mint a new one next.
            self.app.invalidate()
        if not (200 <= status < 300 or status == 304 or status in ok):
            raise GitHubError("%s %s -> %d" % (method, path, status), status, text)

    def _note_write(self, path: str, data: Any) -> None:
        """Mark the issue a write changed, so its cached reads are dropped."""
        changed = re.match(r"^/repos/([^/]+/[^/]+)/issues/(\d+)(?:/|$)", path)
        if changed:
            self.issue_writes.add((changed.group(1), int(changed.group(2))))
        elif re.match(r"^/repos/[^/]+/[^/]+/issues/comments/\d+$", path):
            # An edited comment's answer names its issue: mark that one
            # changed. Only an edit that names none spoils every cached read;
            # the station edits a comment every pass (loom#722).
            owner = re.search(r"/repos/([^/]+/[^/]+)/issues/(\d+)$",
                              str((data or {}).get("issue_url") or "")
                              if isinstance(data, dict) else "")
            if owner:
                self.issue_writes.add((owner.group(1), int(owner.group(2))))
            else:
                self.comment_edits = True

    def _refuse_incomplete_close(self, method: str, path: str, body: Any) -> None:
        """Refuse a PATCH that would close an issue as completed while its
        acceptance criteria are unchecked.

        The brain's close() already refuses this, but a close that bypasses it
        -- the conducted#209 close five seconds after its PR merged is the
        shape -- used to land anyway and get reopened by the close-completeness
        guard seconds later. Every programmatic close passes through rest(),
        so refusing here makes the bad close impossible instead of merely
        caught. Closes as not_planned, force-close/wontfix labels, pull
        requests, and issues with no criteria at all are left alone, matching
        the guard's own escape hatches.
        """
        if method != "PATCH" or not _ISSUE_PATH.match(path):
            return
        if not isinstance(body, dict) or body.get("state") != "closed":
            return
        if body.get("state_reason", "completed") != "completed":
            return
        # Re-entering rest() for the verification GET is safe: the
        # method != "PATCH" check above stops any recursion. A failed read
        # raises out of rest() before the PATCH is sent, so the close is
        # refused; a 2xx read with no issue object in it refuses it too,
        # rather than reading as an issue with no criteria (loom#656).
        issue = self.rest("GET", path).data
        if not isinstance(issue, dict):
            raise GitHubError(
                "refusing to close %s as completed: cannot read the issue to "
                "check its acceptance criteria" % path)
        if "pull_request" in issue:
            return
        labels = [str(label.get("name") or "").lower()
                  for label in issue.get("labels") or []]
        if any(name in labels for name in _FORCE_CLOSE_LABELS):
            return
        unchecked = [text for checked, text in
                     proof.criteria(str(issue.get("body") or "")) if not checked]
        if unchecked:
            raise ValueError(
                "refusing to close %s as completed with unchecked acceptance "
                "criterion: %s" % (path, unchecked[0]))

    def _request(self, body: Any,
                 if_none_match: Optional[str]) -> Tuple[Dict[str, str], Optional[bytes]]:
        """The headers and encoded body for one request."""
        headers = {
            "Authorization": "Bearer " + self._bearer(),
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

    def app_token(self) -> str:
        """The App installation token this client sends, minted when none is
        held or it is about to expire; GitHubError when the client has no App."""
        if self.app is None:
            raise GitHubError("this client holds a personal token, not a GitHub App")
        return self.app.token(self._mint_app_token)

    def _bearer(self) -> str:
        """The token for the Authorization header."""
        if self.app is None:
            return str(self._token)
        return self.app.token(self._mint_app_token)

    def _mint_app_token(self, path: str, jwt: str) -> Tuple[str, Any]:
        """POST for a new installation token, authenticated with the App's
        JWT: (token, expires_at). Counted as a request under "app token";
        its rate headers are the App's own and leave self.rate alone."""
        headers = {"Authorization": "Bearer " + jwt,
                   "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "loom-bus"}
        url = self._url(path)
        status, _, raw = self._transport_with_retry("POST", url, headers, None)
        with self._count_lock:
            self.kinds["app token"] = self.kinds.get("app token", 0) + 1
        text = (raw or b"").decode("utf-8", "replace")
        data = _json_or_none(text)
        if status != 201 or not isinstance(data, dict):
            # GitHub's own reason ("Bad credentials", "Integration not found")
            # goes into the error the station logs; there is no fallback to
            # the personal token, which would drain the shared budget unseen.
            reason = data.get("message") if isinstance(data, dict) else None
            raise GitHubError("could not mint the GitHub App installation token: "
                              "POST %s -> %d%s" % (path, status,
                                                   " (%s)" % str(reason)[:200] if reason else ""),
                              status, text)
        token = data.get("token")
        if not isinstance(token, str) or not token.strip():
            raise GitHubError("the installation token answer has no token", status)
        return token.strip(), data.get("expires_at")

    def _status_logins(self) -> set:
        """Who may write status comments this client expands: its trusted
        writers and, on an App, its own bot login."""
        logins = {a.lower() for a in self.status_authors}
        if self.app is not None:
            try:
                login = self.app_login()
            except GitHubError:
                login = None
            if login:
                logins.add(login.lower())
        return logins

    def app_login(self) -> Optional[str]:
        """The App's own bot login, "<slug>[bot]", read once from GET /app
        with the App's JWT; None when this client holds a personal token.
        This is the login GitHub gives the labels and comments the App
        writes (loom#660)."""
        if self.app is None:
            return None
        if self._app_login is None:
            headers = {"Authorization": "Bearer " + self.app.jwt(),
                       "Accept": "application/vnd.github+json",
                       "X-GitHub-Api-Version": "2022-11-28",
                       "User-Agent": "loom-bus"}
            status, _, raw = self._transport_with_retry("GET", self._url("/app"), headers, None)
            with self._count_lock:
                self.kinds["app login"] = self.kinds.get("app login", 0) + 1
            data = _json_or_none((raw or b"").decode("utf-8", "replace"))
            slug = data.get("slug") if isinstance(data, dict) else None
            if status != 200 or not isinstance(slug, str) or not slug.strip():
                raise GitHubError("could not read the GitHub App's slug: GET /app -> %d" % status,
                                  status)
            self._app_login = slug.strip() + "[bot]"
        return self._app_login

    def _send(self, method: str, url: str, headers: Dict[str, str],
              payload: Optional[bytes]) -> Tuple[int, Dict[str, str], str]:
        """One request on the wire, counted: status, lower-cased headers, text.
        A budget refusal raises RateLimited, and so does every later call until
        the window resets, without a request."""
        path = urlsplit(url).path
        if self.limited_until is not None and time.time() < self.limited_until:
            raise RateLimited("%s %s: GitHub budget exhausted until %s" % (
                method, path, _utc(self.limited_until)), 403, "", self.limited_until)
        status, raw_headers, raw = self._transport_with_retry(method, url, headers, payload)
        resp_headers = {str(k).lower(): v for k, v in (raw_headers or {}).items()}
        # Read before any error is raised: a 403 at the limit carries them.
        self._note_rate(resp_headers)
        self._count(method, url, status)
        text = (raw or b"").decode("utf-8", "replace")
        until = rate_limit_until(status, resp_headers, text, time.time())
        if until is not None:
            self.limited_until = until
            raise RateLimited("%s %s -> %d: GitHub budget exhausted until %s" % (
                method, path, status, _utc(until)), status, text, until)
        return status, resp_headers, text

    def _transport_with_retry(self, method: str, url: str, headers: Dict[str, str],
                              payload: Optional[bytes]) -> Tuple[int, Dict[str, str], bytes]:
        """The transport's answer, a read retried after a dropped connection.

        GitHub closes connections in the middle of a burst of reads (2026-10-14:
        a status call of 60 to 136 requests from a remote brain died on one
        IncompleteRead, so the brain could not merge for four ticks). A read
        changes nothing, so a GET is tried again after RETRY_DELAYS when the
        connection failed (status 0) or GitHub answered 502, 503 or 504. A
        write is never repeated: it may have landed before the connection
        dropped. Every attempt is counted."""
        delays = RETRY_DELAYS if method in ("GET", "HEAD") else ()
        for attempt in range(len(delays) + 1):
            with self._count_lock:
                self.requests += 1
            try:
                status, raw_headers, raw = self.transport(method, url, headers, payload)
            except GitHubError as err:
                if err.status != 0 or attempt == len(delays):
                    raise
                self._sleep(delays[attempt])
                continue
            if status in RETRY_STATUSES and attempt < len(delays):
                self._sleep(delays[attempt])
                continue
            return status, raw_headers, raw
        raise AssertionError("unreachable")  # pragma: no cover

    def _keep_answer(self, url: str, status: int, data: Any,
                     resp_headers: Dict[str, str]) -> None:
        """Keep a full GET answer that carries an ETag for the next read."""
        if status == 200 and resp_headers.get("etag"):
            self._keep(url, {"etag": resp_headers["etag"], "data": data,
                             "link": resp_headers.get("link", "")})

    def _count(self, method: str, url: str, status: int) -> None:
        """Tally one answer: a 304 apart, anything else by its kind."""
        if status == 304:
            with self._count_lock:
                self.not_modified += 1
            return
        kind = request_kind(method, url)
        with self._count_lock:
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
        tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
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
        return self._paginate_from(path, self.rest("GET", path))

    def _paginate_from(self, path: str, first: Response) -> List[Any]:
        items: List[Any] = []
        url: Optional[str] = path
        resp = first
        while url:
            page = resp.data if isinstance(resp.data, list) else []
            items.extend(page)
            nxt = _next_link(resp.headers.get("link", ""))
            if not nxt and resp.status == 304 and page and len(page) >= _per_page(url):
                # A kept full page answers 304 with the link it was kept with.
                # GitHub's ETag for a page does not change when items are
                # added after it, so a list that grew past the page since
                # (an issue's 101st comment) would never be read beyond it:
                # ask for the following page, which has its own ETag.
                nxt = _with_page(url, _page_of(url) + 1)
            url = nxt
            if url:
                resp = self.rest("GET", url)
        return status.resort(items)

    def paginate_parallel(self, path: str, workers: int = 8) -> List[Any]:
        """Read known pages concurrently, preserving their page order."""
        if "per_page=" not in path:
            path += ("&" if "?" in path else "?") + "per_page=100"
        first = self.rest("GET", path)
        last = _last_link(first.headers.get("link", ""))
        if not last or _page_of(last) <= 1:
            return self._paginate_from(path, first)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            pages = list(pool.map(lambda page: self.rest("GET", _with_page(path, page)),
                                  range(2, _page_of(last) + 1)))
        items = list(first.data) if isinstance(first.data, list) else []
        for response in pages:
            if isinstance(response.data, list):
                items.extend(response.data)
        final = pages[-1]
        next_url = _next_link(final.headers.get("link", ""))
        if next_url:
            items.extend(self.paginate(next_url))
        elif final.status == 304 and isinstance(final.data, list) and len(final.data) >= _per_page(path):
            items.extend(self.paginate(_with_page(path, _page_of(last) + 1)))
        return status.resort(items)

    def graphql(self, query: str, variables: Optional[Dict[str, Any]] = None) -> Any:
        resp = self.rest("POST", "/graphql",
                         {"query": query, "variables": variables or {}})
        data = resp.data if isinstance(resp.data, dict) else {}
        if data.get("errors"):
            raise GitHubError("graphql errors", resp.status, json.dumps(data["errors"]))
        return data.get("data")


def _per_page(url: str) -> int:
    m = re.search(r"[?&]per_page=(\d+)", url)
    return int(m.group(1)) if m else 30


def _page_of(url: str) -> int:
    m = re.search(r"[?&]page=(\d+)", url)
    return int(m.group(1)) if m else 1


def _with_page(url: str, page: int) -> str:
    """The URL asking for that page, replacing any page= it had."""
    if re.search(r"[?&]page=\d+", url):
        return re.sub(r"([?&])page=\d+", r"\g<1>page=%d" % page, url)
    return url + ("&" if "?" in url else "?") + "page=%d" % page


def _next_link(header: str) -> Optional[str]:
    return _link(header, "next")


def _last_link(header: str) -> Optional[str]:
    return _link(header, "last")


def _link(header: str, relation: str) -> Optional[str]:
    for part in header.split(","):
        m = re.match(r'\s*<([^>]+)>\s*;(.*)', part)
        if m and re.search(r'rel\s*=\s*"?%s"?' % relation, m.group(2)):
            return m.group(1)
    return None


def auth_from_env(env: Optional[Dict[str, str]] = None) -> Auth:
    """The GitHub App installation token when LOOM_GITHUB_APP_ID,
    LOOM_GITHUB_APP_INSTALLATION_ID and LOOM_GITHUB_APP_KEY_SSM (or
    LOOM_GITHUB_APP_KEY_FILE) are all set, else the personal token
    token_from_env finds."""
    app = AppToken.from_env(env)
    return app if app is not None else token_from_env()


def token_from_env() -> str:
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
