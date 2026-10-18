"""Read a free lane's provider meter with the key the lane already holds.

    printf '%s\\n%s\\n' "$API_KEY" "$ACCOUNT" | python3 -m bus.meter <lane> <out.json> [url]

The key (and, for workersai, the account id) arrive on stdin, so they reach no
process's argv and no log. The lane runner calls this after it has read its
key (lanes/run_lane.sh, lane_meter); bus/usage.py turns the file it writes
into Usage readings. Both reads are free metadata reads that spend no quota:

  openrouter  GET /api/v1/key: the key's free-model requests in the current
              UTC day (free_model_daily_requests) and its credit limit.
  workersai   the GraphQL Analytics API, dataset aiInferenceAdaptiveGroups:
              the account's Neurons today, against the free daily allocation
              (10,000 a day, reset 00:00 UTC; LOOM_WORKERSAI_DAILY_NEURONS).
              The token needs Account Analytics read.

A read that fails still writes the file, with an "error" that says why, so
the Usage view can say it instead of a bare unknown.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

OPENROUTER_URL = "https://openrouter.ai/api/v1/key"
CLOUDFLARE_GRAPHQL_URL = "https://api.cloudflare.com/client/v4/graphql"
WORKERSAI_FREE_NEURONS = 10000.0
# A lane passes often; its provider's meter is read at most this often.
METER_EVERY = 300
TIMEOUT = 15

NEURONS_QUERY = (
    "query($account: string!, $date: Date!) { viewer { accounts(filter: {accountTag: $account}) {"
    " aiInferenceAdaptiveGroups(filter: {date: $date}, limit: 1) { sum { totalNeurons } } } } }")

Fetch = Callable[[urllib.request.Request], Any]


class MeterError(Exception):
    """A read that answered, but not with a meter. Its text is shown on Usage."""


def _number(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def next_utc_midnight(now: float) -> str:
    day = datetime.fromtimestamp(now, timezone.utc).date() + timedelta(days=1)
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc).isoformat()


def _window(window: str, used: Optional[float], limit: Optional[float], unit: str,
            resets_at: Optional[str], remaining: Optional[float] = None,
            minutes: Optional[int] = None) -> Dict[str, Any]:
    if remaining is None and used is not None and limit is not None:
        remaining = max(0.0, limit - used)
    pct = None
    if used is not None and limit:
        pct = min(100.0, max(0.0, used / limit * 100))
    elif used is not None and limit == 0:
        pct = 100.0
    return {"window": window, "used": used, "limit": limit, "remaining": remaining,
            "used_pct": pct, "unit": unit, "resets_at": resets_at, "window_minutes": minutes}


def openrouter_windows(payload: Any, now: float) -> List[Dict[str, Any]]:
    """The meters in a GET /api/v1/key answer: free-model requests today, and
    the key's credit when it has a limit."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise MeterError("the key endpoint answered without a data object")
    windows = []
    daily = data.get("free_model_daily_requests")
    if isinstance(daily, dict) and _number(daily.get("limit")) is not None:
        windows.append(_window("daily", _number(daily.get("used")), _number(daily.get("limit")),
                               "requests", next_utc_midnight(now), _number(daily.get("remaining")), 1440))
    limit = _number(data.get("limit"))
    if limit is not None:
        remaining = _number(data.get("limit_remaining"))
        used = limit - remaining if remaining is not None else None
        windows.append(_window("credit", used, limit, "USD", None, remaining))
    if not windows:
        raise MeterError("the key endpoint gave no free-model request count and the key has no credit limit")
    return windows


def workersai_windows(payload: Any, now: float, allowance: float) -> List[Dict[str, Any]]:
    """Today's Neurons from a GraphQL Analytics answer, against the free
    daily allocation."""
    if not isinstance(payload, dict):
        raise MeterError("the analytics API answered with something other than an object")
    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        first = errors[0] if isinstance(errors[0], dict) else {}
        message = str(first.get("message") or "error")
        if re.search(r"not authori[sz]ed|permission|forbidden|access", message, re.I):
            raise MeterError("the token cannot read account analytics (it needs Account Analytics read): "
                             + message)
        raise MeterError("the analytics API answered: " + message)
    try:
        accounts = payload["data"]["viewer"]["accounts"]
    except (KeyError, TypeError):
        raise MeterError("the analytics API answered without viewer.accounts") from None
    if not isinstance(accounts, list) or not accounts:
        raise MeterError("the analytics API knows no such account for this token")
    groups = accounts[0].get("aiInferenceAdaptiveGroups") if isinstance(accounts[0], dict) else None
    if not isinstance(groups, list):
        raise MeterError("the analytics API answered without aiInferenceAdaptiveGroups")
    used = 0.0
    for group in groups:
        total = _number(((group or {}).get("sum") or {}).get("totalNeurons")) if isinstance(group, dict) else None
        used += total or 0.0
    return [_window("daily", used, allowance, "neurons", next_utc_midnight(now), minutes=1440)]


def _send(fetch: Fetch, request: urllib.request.Request) -> Any:
    try:
        with fetch(request) as response:
            body = response.read()
    except urllib.error.HTTPError as err:
        if err.code in (401, 403):
            raise MeterError("the provider refused the key (HTTP %d)" % err.code) from None
        raise MeterError("the provider answered HTTP %d" % err.code) from None
    except (urllib.error.URLError, OSError) as err:
        raise MeterError("no answer from the provider (%s)" % type(err).__name__) from None
    try:
        return json.loads(body)
    except ValueError:
        raise MeterError("the provider answered with something other than JSON") from None


def _urlopen(request: urllib.request.Request) -> Any:
    return urllib.request.urlopen(request, timeout=TIMEOUT)  # noqa: S310 - fixed https URLs


def read(lane: str, key: str, account: str, now: float, env: Dict[str, str],
         url: Optional[str] = None, fetch: Fetch = _urlopen) -> Dict[str, Any]:
    """The meter file's contents for one read of LANE's provider."""
    record: Dict[str, Any] = {"lane": lane, "at": now, "windows": [], "error": None}
    try:
        if not key:
            raise MeterError("the lane has no key to read the meter with")
        if lane == "openrouter":
            record["source"] = "OpenRouter GET /api/v1/key"
            request = urllib.request.Request(url or OPENROUTER_URL, headers={
                "Authorization": "Bearer " + key, "Accept": "application/json"})
            record["windows"] = openrouter_windows(_send(fetch, request), now)
        elif lane == "workersai":
            record["source"] = "Cloudflare GraphQL Analytics (aiInferenceAdaptiveGroups)"
            if not re.fullmatch(r"[A-Za-z0-9]+", account or ""):
                raise MeterError("the lane has no Cloudflare account id")
            allowance = WORKERSAI_FREE_NEURONS
            try:
                allowance = float(env.get("LOOM_WORKERSAI_DAILY_NEURONS") or WORKERSAI_FREE_NEURONS)
            except ValueError:
                pass
            body = json.dumps({"query": NEURONS_QUERY, "variables": {
                "account": account,
                "date": datetime.fromtimestamp(now, timezone.utc).date().isoformat()}}).encode()
            request = urllib.request.Request(url or CLOUDFLARE_GRAPHQL_URL, data=body, method="POST", headers={
                "Authorization": "Bearer " + key, "Content-Type": "application/json",
                "Accept": "application/json"})
            record["windows"] = workersai_windows(_send(fetch, request), now, allowance)
        else:
            raise MeterError("no meter for lane %s" % lane)
    except MeterError as err:
        # Every message above is fixed words, an HTTP code, or the provider's
        # own error message; a provider that echoes the key gets it blanked.
        record["error"] = str(err).replace(key, "[key]") if len(key) >= 8 else str(err)
        record["windows"] = []
    return record


def fresh(path: str, now: float) -> bool:
    """Whether the file at PATH was read less than METER_EVERY seconds ago."""
    try:
        with open(path, encoding="utf-8") as stream:
            at = json.load(stream).get("at")
    except (OSError, ValueError, AttributeError):
        return False
    return isinstance(at, (int, float)) and 0 <= now - at < METER_EVERY


def write(path: str, record: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as stream:
        json.dump(record, stream, indent=2)
        stream.write("\n")
    os.replace(tmp, path)


def main(argv: Optional[List[str]] = None, stdin: Any = None, now: Optional[float] = None,
         fetch: Fetch = _urlopen) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) not in (2, 3):
        print("usage: python3 -m bus.meter <lane> <out.json> [url]", file=sys.stderr)
        return 2
    lane, out = args[0], args[1]
    url = args[2] if len(args) == 3 and args[2] else None
    now = time.time() if now is None else now
    if fresh(out, now):
        return 0
    lines = (stdin or sys.stdin).read().splitlines()
    key = lines[0].strip() if lines else ""
    account = lines[1].strip() if len(lines) > 1 else ""
    write(out, read(lane, key, account, now, dict(os.environ), url, fetch))
    return 0


if __name__ == "__main__":
    sys.exit(main())
