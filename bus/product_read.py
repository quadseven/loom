"""A brain's product watch read: one GET of a goal home's product API.

    python3 -m bus.product_read stuck [--param key=value ...]
    python3 -m bus.product_read health

The read goes to `<base>/api/v2/stuck` or `<base>/api/v2/health`, where the
base is the product's own address with its path prefix (the world the API
serves), never the Loom dashboard's. The base comes from, in order:

1. `LOOM_PRODUCT_URL`;
2. the first `LOOM_HEALTH_URLS` entry that ends in `/api/v2/health`, with
   that suffix taken off (the station's own health read, docs/HEALTH.md);
3. the SSM parameter `LOOM_PRODUCT_URL_SSM` names (default
   `/muse/product_url`, which the Muse VM's identity may read).

With `LOOM_TAILNET_PROXY` set, the GET goes through that proxy and only
that one; the SSM read keeps the shell's own proxy.

A read that fails says which URL it asked and what came back: the HTTP
status, the redirect target, or that the body was not JSON (loom#2481: a
tick wrote "the stuck read returned 404" for four ticks while it was asking
the dashboard's host, which serves no product API, and nothing it printed
named that host).

Exit codes: 0 read, 1 the read failed, 2 no usable base.
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlencode, urlsplit

from .github import _NoRedirect, read_ssm

PATHS = {"stuck": "/api/v2/stuck", "health": "/api/v2/health"}
HEALTH_SUFFIX = "/api/v2/health"
DEFAULT_SSM = "/muse/product_url"
TIMEOUT = 20.0
MAX_BODY = 4 * 1024 * 1024

Fetch = Callable[[str], Tuple[int, Dict[str, str], bytes]]


class BaseError(ValueError):
    """No usable product base URL."""


class ReadError(OSError):
    """The GET did not answer 200 with JSON; the text names URL and result."""


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def check_base(base: str, env: Dict[str, str]) -> str:
    """The base with no trailing slash, or BaseError saying why it is unusable."""
    base = base.strip().rstrip("/")
    parts = urlsplit(base)
    if parts.scheme != "https" or not parts.hostname:
        raise BaseError("the product base is not an https URL")
    if parts.query or parts.fragment:
        raise BaseError("the product base carries a query or fragment")
    if parts.path.endswith("/api") or "/api/" in parts.path + "/":
        raise BaseError("the product base names an /api path; give the base above /api/v2")
    dashboard = env.get("LOOM_TRANSCRIPT_URL", "").strip()
    if dashboard and _host(dashboard) == parts.hostname.lower():
        raise BaseError("the product base %s is the Loom dashboard's host, which serves "
                        "no product API; set the product's own base" % base)
    return base


def _from_health(health_urls: str) -> str:
    for entry in health_urls.split():
        _, _, url = entry.partition("=")
        url = url.strip().rstrip("/")
        if url.endswith(HEALTH_SUFFIX):
            return url[:-len(HEALTH_SUFFIX)]
    return ""


def resolve_base(env: Dict[str, str],
                 ssm: Callable[[str, str], str] = read_ssm) -> Tuple[str, str]:
    """(base, where it came from), or BaseError naming every source tried."""
    explicit = env.get("LOOM_PRODUCT_URL", "").strip()
    if explicit:
        return check_base(explicit, env), "LOOM_PRODUCT_URL"
    derived = _from_health(env.get("LOOM_HEALTH_URLS", ""))
    if derived:
        return check_base(derived, env), "LOOM_HEALTH_URLS"
    name = env.get("LOOM_PRODUCT_URL_SSM", "").strip() or DEFAULT_SSM
    region = env.get("LOOM_AWS_REGION", "").strip() or "us-east-1"
    try:
        value = ssm(name, region)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise BaseError("no product base: LOOM_PRODUCT_URL and LOOM_HEALTH_URLS are unset "
                        "and SSM %s did not read (%s)" % (name, exc)) from None
    return check_base(value, env), "SSM " + name


def read_url(base: str, what: str, params: Sequence[Tuple[str, str]] = ()) -> str:
    """The URL one read asks: base + the read's path, with its query."""
    url = base.rstrip("/") + PATHS[what]
    return url + ("?" + urlencode(list(params)) if params else "")


def urllib_fetch(proxy: str = "") -> Fetch:
    """A GET that never follows a redirect, through proxy when one is given."""
    handlers: List[urllib.request.BaseHandler] = [_NoRedirect()]
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"https": proxy, "http": proxy}))
    opener = urllib.request.build_opener(*handlers)

    def fetch(url: str) -> Tuple[int, Dict[str, str], bytes]:
        req = urllib.request.Request(url, method="GET", headers={  # noqa: S310
            "User-Agent": "loom-brain", "Accept": "application/json"})
        try:
            with opener.open(req, timeout=TIMEOUT) as resp:  # noqa: S310 - https only
                return resp.status, dict(resp.headers.items()), resp.read(MAX_BODY)
        except urllib.error.HTTPError as err:
            headers = dict(err.headers.items()) if err.headers else {}
            err.close()
            return err.code, headers, b""
    fetch.opener = opener  # type: ignore[attr-defined] - read by the tests
    return fetch


def read(url: str, fetch: Fetch) -> object:
    """The JSON body of a 200, or ReadError naming the URL and what it got."""
    if not url.startswith("https://"):
        raise ReadError("refusing GET %s: not https" % url)
    try:
        status, headers, raw = fetch(url)
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise ReadError("GET %s did not connect: %s" % (url, reason)) from None
    if 300 <= status < 400:
        where = next((v for k, v in headers.items() if k.lower() == "location"), "")
        raise ReadError("GET %s answered HTTP %d to %s: the base misses the path prefix "
                        "the API is served under" % (url, status, where or "no Location"))
    if status != 200:
        raise ReadError("GET %s answered HTTP %d" % (url, status))
    try:
        return json.loads(raw)
    except ValueError:
        raise ReadError("GET %s answered HTTP 200 but not JSON (%d bytes): the base is "
                        "not the product API's" % (url, len(raw))) from None


def _param(text: str) -> Tuple[str, str]:
    key, sep, value = text.partition("=")
    if not sep or not key:
        raise argparse.ArgumentTypeError("a param is key=value")
    return key, value


def main(argv: Optional[Sequence[str]] = None, env: Optional[Dict[str, str]] = None,
         fetch: Optional[Fetch] = None,
         ssm: Callable[[str, str], str] = read_ssm) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m bus.product_read",
                                     description="One GET of a goal home's product API.")
    parser.add_argument("what", choices=sorted(PATHS))
    parser.add_argument("--param", action="append", type=_param, default=[],
                        help="a query parameter, key=value (repeatable)")
    args = parser.parse_args(argv)
    env = dict(os.environ) if env is None else env
    try:
        base, source = resolve_base(env, ssm)
    except BaseError as exc:
        print("product read: %s" % exc, file=sys.stderr)
        return 2
    url = read_url(base, args.what, args.param)
    if fetch is None:
        fetch = urllib_fetch(env.get("LOOM_TAILNET_PROXY", "").strip())
    try:
        body = read(url, fetch)
    except ReadError as exc:
        print("product read failed (base from %s): %s" % (source, exc), file=sys.stderr)
        return 1
    print("product read: GET %s answered HTTP 200 (base from %s)" % (url, source))
    print(json.dumps(body, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
