"""Durable operator core REST samples, separate from the station App budget.

Only a hash binding an existing credential to the configured operator is kept.
A fresh binding allows sampling zero remaining after a station restart without
calling /user, which itself needs core quota. First observation at zero, a
rotated credential, or an expired binding remains unknown until /user succeeds.
No credential, account name, raw response or exception text is persisted.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile

from .github import GitHubError

FILE = "operator-budget.json"
EVERY = 3600
RETRY = 300
BINDING_AGE = 86400
KEEP = 169
LOW_DIVISOR = 10
MAX_CORE_LIMIT = 1_000_000_000
MAX_EPOCH = 253402300799


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value) and 0 <= value <= MAX_EPOCH
    except OverflowError:
        return False


def core_values(data):
    if not isinstance(data, dict):
        raise ValueError("invalid core sample")
    keys = ("remaining", "limit", "reset")
    if not all(isinstance(data.get(k), int) and not isinstance(data[k], bool) for k in keys):
        raise ValueError("invalid core sample")
    remaining, limit, reset = (data[k] for k in keys)
    if not 0 < limit <= MAX_CORE_LIMIT or not 0 <= remaining <= limit or not 0 < reset <= MAX_EPOCH:
        raise ValueError("invalid core sample")
    return {k: data[k] for k in keys}


def binding_valid(binding, key, now):
    return (isinstance(binding, dict) and binding.get("key") == key
            and number(binding.get("at")) and 0 <= now - binding["at"] < BINDING_AGE)


def read_core(operator, binding, now, resolve=None, client=None):
    """Read using the existing resolver; never mint or provision a credential."""
    from .github import GitHub, token_from_env
    if not isinstance(operator, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", operator):
        raise ValueError("operator identity unavailable")
    token = (resolve or token_from_env)()
    if not isinstance(token, str) or not token:
        raise ValueError("credential unavailable")
    key = hashlib.sha256((token + "\0" + operator.lower()).encode()).hexdigest()
    gh = (client or GitHub)(token)
    if not binding_valid(binding, key, now):
        user = gh.rest("GET", "/user").data
        if (not isinstance(user, dict) or user.get("type") != "User"
                or not isinstance(user.get("login"), str)
                or user["login"].lower() != operator.lower()):
            raise ValueError("operator identity unverified")
        binding = {"key": key, "at": now}
    data = gh.rest("GET", "/rate_limit").data
    resources = data.get("resources") if isinstance(data, dict) else None
    core = resources.get("core") if isinstance(resources, dict) else None
    return core_values(core), binding


def clean_record(record):
    try:
        core = core_values(record)
    except ValueError:
        return None
    if not number(record.get("at")):
        return None
    return dict(core, at=record["at"], active=record.get("active") is True)


def load(path):
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    state = {}
    for key in ("attempted_at", "next_at"):
        if number(raw.get(key)):
            state[key] = raw[key]
    binding = raw.get("binding")
    if (isinstance(binding, dict) and isinstance(binding.get("key"), str)
            and re.fullmatch(r"[0-9a-f]{64}", binding["key"]) and number(binding.get("at"))):
        state["binding"] = {"key": binding["key"], "at": binding["at"]}
    samples = raw.get("samples")
    if isinstance(samples, list):
        state["samples"] = [r for item in samples[-KEEP:] if (r := clean_record(item))]
    failures = raw.get("failures")
    if isinstance(failures, list):
        state["failures"] = [{"at": f["at"], "reason": "read-failed"}
                             for f in failures[-KEEP:] if isinstance(f, dict) and number(f.get("at"))]
    signal = clean_record(raw.get("signal"))
    if signal and signal["active"] and signal["remaining"] * LOW_DIVISOR < signal["limit"]:
        state["signal"] = signal
    return state


def save(path, state):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def flag(state, now, active):
    samples = state.get("samples", [])
    if not samples:
        return
    latest = samples[-1]
    if (active and 0 <= now - latest["at"] < EVERY and latest["reset"] > now
            and latest["remaining"] * LOW_DIVISOR < latest["limit"]):
        state["signal"] = dict(latest, active=True)


def sample(path, now, operator, active, reader, say):
    """One bounded attempt when due, even idle; called under the station lock."""
    state = load(path)
    try:
        attempted = state.get("attempted_at")
        next_at = state.get("next_at")
        if (number(attempted) and number(next_at) and 0 <= now - attempted < EVERY
                and now < next_at <= attempted + EVERY):
            flag(state, now, active)
            save(path, state)
            return state
        state.update(attempted_at=now, next_at=now + RETRY)
        save(path, state)  # A killed read still has a durable retry deadline.
        try:
            core, binding = reader(operator, state.get("binding", {}), now)
            record = dict(core_values(core), at=now, active=bool(active))
        except (GitHubError, OSError, ValueError):
            # Transport errors may contain secrets; only this fixed category is safe.
            state["failures"] = (state.get("failures", []) +
                                  [{"at": now, "reason": "read-failed"}])[-KEEP:]
            say("operator core budget read failed; identity or quota unknown; retry later")
        else:
            state["binding"] = binding
            state["samples"] = (state.get("samples", []) + [record])[-KEEP:]
            state["next_at"] = now + EVERY
            if record["remaining"] * LOW_DIVISOR >= record["limit"]:
                state.pop("signal", None)
            flag(state, now, active)
        save(path, state)
    except (OSError, ValueError):
        say("operator core budget state unavailable; sampling deferred")
    return state
