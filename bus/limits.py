"""Per-lane-type usage-limit detection for usage-aware routing (loom#1101).

Each lane type detects its current usage vs limit in its own way; detect()
normalizes every answer into one payload:

    {"remaining": 0.0-1.0 | None,   # fraction of capacity left; None = unknown
     "reset_at": epoch | None,      # when the current window resets
     "expires_at": epoch | None,    # when prepaid credits expire
     "unit": "credits" | "usd" | "gpu" | "requests" | None,
     "source": "<where the figure came from>",
     "stale": bool}                 # the reading outlived its window

The lane runner writes the payload as the heartbeat's "limits" member; the
dispatcher prefers lanes whose credits expire soonest, then by remaining
capacity. The payload carries fractions and epochs only, never keys,
balances, or account identifiers.

Sources mirror docs/USAGE_ROUTING.md: OpenRouter's key endpoint, nvidia-smi
for the GPU lanes, the lane's own limit holds for the subscription CLIs
(which expose no usage API), spend-vs-budget for the Anthropic API lane,
and the remote heartbeat alone for the muse lane (no usage reaches Loom).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from . import spend as _spend

FIELDS = ("remaining", "reset_at", "expires_at", "unit", "source", "stale")

# A meter read younger than this is reused instead of hitting the provider.
METER_TTL_S = 300

# openrouter.ai/api/v1/key rate_limit intervals, e.g. "1m", "1h", "1d".
_INTERVAL_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd])\s*$", re.IGNORECASE)
_INTERVAL_S = {"s": 1, "m": 60, "h": 3600, "d": 86400}


# Sources embed verbatim into the shell-written heartbeat JSON; strip the
# characters that would break it (the runner cannot re-escape them).
_UNSAFE_SOURCE = re.compile(r'["\\\\]')


def _payload(remaining: Optional[float], reset_at: Optional[float],
             expires_at: Optional[float], unit: Optional[str],
             source: str, stale: bool = False) -> Dict[str, Any]:
    if remaining is not None:
        remaining = max(0.0, min(1.0, float(remaining)))
    source = _UNSAFE_SOURCE.sub("", str(source))[:300]
    return {"remaining": remaining,
            "reset_at": float(reset_at) if reset_at is not None else None,
            "expires_at": float(expires_at) if expires_at is not None else None,
            "unit": unit, "source": source, "stale": bool(stale)}


def _unknown(source: str) -> Dict[str, Any]:
    return _payload(None, None, None, None, source)


def _home_default() -> str:
    return os.environ.get("LOOM_HOME") or str(Path.home() / "loom")


def _run(cmd: List[str]) -> str:
    """A subprocess call, one seam the tests replace."""
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=15, check=True).stdout


def _http_json(url: str, headers: Mapping[str, str]) -> Any:
    """A GET returning parsed JSON, one seam the tests replace.
    
    Only HTTPS URLs are permitted to prevent arbitrary file access.
    """
    if not url.startswith("https://"):
        raise ValueError("only https URLs are allowed")
    req = urllib.request.Request(url, headers=dict(headers))
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        row = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return row if isinstance(row, dict) else None


def _beat(home: str, lane: str) -> Optional[Dict[str, Any]]:
    return _read_json(Path(home) / "state" / "usage" / (lane + ".json"))


def _cache_path(home: str, lane: str) -> Path:
    return Path(home) / "state" / "usage" / (lane + ".limits-cache.json")


def _cached(home: str, lane: str, now: float) -> Optional[Dict[str, Any]]:
    """A fresh meter read from the cache, or None."""
    row = _read_json(_cache_path(home, lane))
    if not isinstance(row, dict):
        return None
    at = row.get("at")
    if not isinstance(at, (int, float)) or isinstance(at, bool):
        return None
    if now - float(at) > METER_TTL_S:
        return None
    cached = row.get("payload")
    if not isinstance(cached, dict) or tuple(sorted(cached)) != tuple(sorted(FIELDS)):
        return None
    return cached


def _cacheable(home: str, lane: str, cached: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The newest cached read regardless of age, for stale fallback."""
    row = _read_json(_cache_path(home, lane)) if cached is None else {"payload": cached}
    if isinstance(row, dict) and isinstance(row.get("payload"), dict):
        return row["payload"]
    return None


def _store(home: str, lane: str, payload: Dict[str, Any], now: float) -> None:
    path = _cache_path(home, lane)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps({"at": now, "payload": payload}) + "\n")
        os.replace(tmp, path)
    except OSError:
        pass


def _metered(home: str, lane: str, now: float, probe: str,
             read: "callable") -> Dict[str, Any]:
    """Run a provider meter read, caching it; serve a stale cache when the
    read fails so one failed probe does not blank the heartbeat."""
    fresh = _cached(home, lane, now)
    if fresh is not None:
        return fresh
    try:
        payload = read()
    except Exception as err:  # the probe must never break the heartbeat
        old = _cacheable(home, lane, None)
        if old is not None:
            old = dict(old)
            old["stale"] = True
            old["source"] = "%s (cached; %s probe failed: %s)" % (
                old.get("source"), probe, str(err)[:80])
            return old
        return _unknown("%s probe failed: %s" % (probe, str(err)[:80]))
    _store(home, lane, payload, now)
    return payload


def _interval_seconds(value: Any) -> Optional[float]:
    m = _INTERVAL_RE.match(str(value or ""))
    if not m:
        return None
    return float(m.group(1)) * _INTERVAL_S[m.group(2).lower()]


def _openrouter(home: str, lane: str, env: Mapping[str, str],
                now: float) -> Dict[str, Any]:
    key = (env.get("LOOM_OPENROUTER_API_KEY") or "").strip()
    if not key:
        return _unknown("openrouter: set LOOM_OPENROUTER_API_KEY to read the key meter")

    def read() -> Dict[str, Any]:
        body = _http_json("https://openrouter.ai/api/v1/key",
                          {"Authorization": "Bearer " + key})
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            raise ValueError("no data in key response")
        limit = data.get("limit")
        used = data.get("usage")
        remaining = None
        if (isinstance(limit, (int, float)) and not isinstance(limit, bool)
                and limit > 0 and isinstance(used, (int, float))
                and not isinstance(used, bool)):
            remaining = 1.0 - used / limit
        rate = data.get("rate_limit") if isinstance(data.get("rate_limit"), dict) else {}
        reset_at = None
        window = _interval_seconds(rate.get("interval"))
        if window:
            reset_at = now + window
        expires_at = None
        raw_expire = (env.get("LOOM_OPENROUTER_CREDITS_EXPIRE") or "").strip()
        if raw_expire:
            try:
                expires_at = _spend.parse_when(raw_expire)
            except ValueError:
                expires_at = None
        return _payload(remaining, reset_at, expires_at, "credits",
                        "openrouter key endpoint")
    return _metered(home, lane, now, "openrouter", read)


def _sparks(home: str, lane: str, now: float) -> Dict[str, Any]:
    def read() -> Dict[str, Any]:
        out = _run(["nvidia-smi", "--query-gpu=utilization.gpu",
                    "--format=csv,noheader,nounits"])
        util = float(out.split()[0])
        return _payload(1.0 - util / 100.0, None, None, "gpu", "nvidia-smi")
    return _metered(home, lane, now, "nvidia-smi", read)


def _hold_reset(home: str, lane: str, now: float) -> Optional[float]:
    """The epoch a provider limit hold lifts, from the lane's own heartbeat
    (run_lane.sh writes available:false with retry_at while held)."""
    beat = _beat(home, lane)
    if not isinstance(beat, dict) or beat.get("available") is not False:
        return None
    reason = beat.get("reason")
    if not isinstance(reason, str) or not reason.startswith("limit:"):
        return None
    retry = beat.get("retry_at")
    if isinstance(retry, bool) or not isinstance(retry, (int, float)):
        return None
    retry = float(retry)
    return retry if retry > now else None


def _subscription(home: str, lane: str, now: float) -> Dict[str, Any]:
    """Subscription CLI lanes expose no usage API; the only firm signal is
    the runner's own limit hold."""
    reset_at = _hold_reset(home, lane, now)
    if reset_at is not None:
        return _payload(0.0, reset_at, None, None,
                        "%s: provider limit hold" % lane)
    return _unknown("%s: subscription exposes no usage API; "
                    "no active limit hold" % lane)


def _muse(home: str, lane: str, now: float) -> Dict[str, Any]:
    return _unknown("muse: the remote lane reports only a heartbeat; "
                    "no usage or limit reaches Loom")


def _anthropic(home: str, lane: str, env: Mapping[str, str],
               now: float) -> Dict[str, Any]:
    try:
        budget = _spend.Budget.from_env(env)
    except ValueError as err:
        return _unknown("anthropic: %s" % err)
    if budget.usd is None:
        return _unknown("anthropic: set LOOM_ANTHROPIC_BUDGET_USD to the "
                        "console's balance to track spend vs budget")
    spent = _spend.summarize(home, budget, now).spent
    return _payload(1.0 - spent / budget.usd, None, budget.expires, "usd",
                    "anthropic spend vs budget")


def _opencode_go(home: str, lane: str, now: float) -> Dict[str, Any]:
    row = _read_json(Path(home) / "state" / "usage" / (lane + ".provider-limit.json"))
    if isinstance(row, dict):
        retry = row.get("retry_after")
        if (isinstance(retry, (int, float)) and not isinstance(retry, bool)
                and float(retry) > now):
            return _payload(0.0, float(retry), None, None,
                            "opencode-go: provider limit probe")
    return _unknown("opencode-go: lane probe shows no active limit")


_DETECTORS = {
    "openrouter": _openrouter,
    "sparks": _sparks,
    "sparks-qwen": _sparks,
    "claude": _subscription,
    "codex": _subscription,
    "agy": _subscription,
    "muse": _muse,
    "anthropic": _anthropic,
    "opencode-go": _opencode_go,
}


def detect(lane: str, home: Optional[str] = None,
           env: Optional[Mapping[str, str]] = None,
           now: Optional[float] = None) -> Dict[str, Any]:
    """The lane's current usage vs limit as a limits payload (see FIELDS)."""
    home = home if home is not None else _home_default()
    env = env if env is not None else os.environ
    now = time.time() if now is None else now
    detector = _DETECTORS.get(lane)
    if detector is None:
        return _unknown("unknown lane type: %s" % lane)
    if lane in ("openrouter", "anthropic"):
        return detector(home, lane, env, now)
    return detector(home, lane, now)


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m bus.limits")
    p.add_argument("lane", help="lane slot name, e.g. codex")
    args = p.parse_args(argv)
    payload = detect(args.lane)
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
