"""Bench a model for everyone after repeated failure (loom#969).

Nothing used to retire a model: a provider and model that failed most of its
calls kept getting picked. This module decides, from the inference gateway's
shared call records (hits, ok and limit per target and model), which pairs to
bench, for how long, and when a benched pair gets one trial call.

- A pair is benched when, over at least MIN_SAMPLE calls that were not limit
  refusals, at least THRESHOLD of them failed. A failure is a call that was
  neither ok nor a limit refusal (nor a client error, the caller's own bad
  request): an error, a timeout, an unparseable or an
  empty answer. A limit refusal is allowance, not failure, and counts toward
  neither side.
- A bench lasts a cooldown that doubles each time the pair is benched again
  (COOLDOWN, 2 * COOLDOWN, ... up to MAX_COOLDOWN).
- When the cooldown ends the pair is on trial: it is not benched, so it can
  be picked. The first non-limit calls after the trial starts decide: all ok
  returns the pair (it is forgiven the failures it already had, so old calls
  in the rolling window cannot bench it again); any failure benches it again
  for twice as long.
- Every bench, trial start, return and re-bench is an event, kept for the
  dashboard and the station's digest.

evaluate() is pure. The dashboard runs it each background pass over the
gateway's 24 hour stats and serves the result at GET /api/bench; the station
reads that list and skips benched pairs (bus/factory.py Factory.benched).
A pair is matched to a Loom lane by the gateway target's name (the lane or
its numbered slot's lane, labels.base_lane).
"""
from __future__ import annotations

import copy
import os
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import labels

# At least this share of the non-limit calls failed.
THRESHOLD = 0.5
# Non-limit calls needed before a rate means anything.
MIN_SAMPLE = 10
# The first cooldown, then doubled on each repeat, up to MAX_COOLDOWN.
COOLDOWN = 3600
MAX_COOLDOWN = 86400
# A pair that stayed back for longer than MAX_COOLDOWN starts again at COOLDOWN.
EVENTS_KEPT = 100
# The station ignores a list fetched longer ago than this, so a dashboard it
# cannot reach never benches a model for ever.
LIST_TTL = 6 * 3600

# The dashboard's read of the list (dashboard/server.py).
LIST_PATH = "/api/bench"
BENCHED, TRIAL = "benched", "trial"
EVENT_KINDS = ("bench", "trial", "return", "rebench")


class Config:
    """The thresholds, from LOOM_BENCH_* settings; a setting that is not a
    positive number keeps its default."""

    def __init__(self, threshold: float = THRESHOLD, min_sample: int = MIN_SAMPLE,
                 cooldown: int = COOLDOWN, max_cooldown: int = MAX_COOLDOWN) -> None:
        self.threshold = threshold
        self.min_sample = min_sample
        self.cooldown = cooldown
        self.max_cooldown = max(cooldown, max_cooldown)

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Config":
        env = os.environ if env is None else env

        def number(name: str, default: float, top: Optional[float] = None) -> float:
            try:
                value = float(str(env.get(name, "")).strip())
            except ValueError:
                return default
            return value if value > 0 and (top is None or value <= top) else default

        return cls(number("LOOM_BENCH_THRESHOLD", THRESHOLD, 1.0),
                   int(number("LOOM_BENCH_MIN_SAMPLE", MIN_SAMPLE)),
                   int(number("LOOM_BENCH_COOLDOWN_SECONDS", COOLDOWN)),
                   int(number("LOOM_BENCH_MAX_COOLDOWN_SECONDS", MAX_COOLDOWN)))


def empty_state() -> Dict[str, Any]:
    return {"models": {}, "forgiven": {}, "history": {}, "events": []}


def key_of(provider: str, model: str) -> str:
    return "%s|%s" % (provider, model)


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value) if value > 0 else 0


def counts(row: Mapping[str, Any]) -> Tuple[int, int]:
    """(calls, failed) of one gateway row. Calls leave out limit refusals
    (allowance) and client errors (the caller's bad request, not the
    model's failure); failed are the calls that were not ok."""
    hits, ok = _count(row.get("hits")), _count(row.get("ok"))
    calls = max(0, hits - _count(row.get("limit")) - _count(row.get("client_error")))
    return calls, max(0, calls - ok)


def _index(rows: Sequence[Any]) -> Dict[str, Tuple[str, str, int, int]]:
    out: Dict[str, Tuple[str, str, int, int]] = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("target") or not row.get("model"):
            continue
        provider, model = str(row["target"]), str(row["model"])
        calls, failed = counts(row)
        out[key_of(provider, model)] = (provider, model, calls, failed)
    return out


def _percent(part: int, whole: int) -> str:
    return "%d%%" % round(100.0 * part / whole) if whole else "0%"


def _event(kind: str, entry: Mapping[str, Any], now: float) -> Dict[str, Any]:
    return {"at": int(now), "kind": kind, "provider": entry["provider"], "model": entry["model"],
            "reason": entry.get("reason", ""), "until": entry.get("until"),
            "level": entry.get("level", 1)}


def _cooldown(cfg: Config, level: int) -> int:
    return int(min(cfg.cooldown * (2 ** max(0, level - 1)), cfg.max_cooldown))


def _bench(provider: str, model: str, level: int, reason: str, now: float, cfg: Config,
           calls: int, failed: int) -> Dict[str, Any]:
    wait = _cooldown(cfg, level)
    return {"provider": provider, "model": model, "status": BENCHED, "since": int(now),
            "until": int(now) + wait, "cooldown": wait, "level": level, "reason": reason,
            "seen_calls": calls, "seen_failed": failed, "trial_calls": 0, "trial_failed": 0}


def _clean(new: Dict[str, Any]) -> Dict[str, Any]:
    """The state with every section the passes read present and shaped."""
    for name in ("models", "forgiven", "history"):
        if not isinstance(new.get(name), dict):
            new[name] = {}
    if not isinstance(new.get("events"), list):
        new["events"] = []
    return new


def _trial_pass(state: Dict[str, Any], seen: Optional[Mapping[str, Tuple[str, str, int, int]]],
                now: float, cfg: Config) -> List[Dict[str, Any]]:
    """Move each benched pair whose cooldown ended to its trial, and decide
    each pair on trial from the calls made since the trial began."""
    models, events = state["models"], []
    for key in list(models):
        entry = models[key]
        if entry.get("status") == BENCHED and now >= entry.get("until", 0):
            entry.update(status=TRIAL, trial_at=int(now), trial_calls=0, trial_failed=0, marked=False)
            events.append(_event("trial", entry, now))
        if entry.get("status") != TRIAL or seen is None:
            continue
        row = seen.get(key)
        calls, failed = (row[2], row[3]) if row else (0, 0)
        if not entry.get("marked"):
            # Calls made during the cooldown (by a caller that ignored the
            # bench) are not the trial's: count from the first read after it.
            entry["seen_calls"], entry["seen_failed"], entry["marked"] = calls, failed, True
            continue
        # Count what is new since the last pass; a rolling window that shrank
        # adds nothing and moves the mark down.
        entry["trial_calls"] = entry.get("trial_calls", 0) + max(0, calls - entry.get("seen_calls", 0))
        entry["trial_failed"] = entry.get("trial_failed", 0) + max(0, failed - entry.get("seen_failed", 0))
        entry["seen_calls"], entry["seen_failed"] = calls, failed
        if entry["trial_failed"] > 0:
            level = entry.get("level", 1) + 1
            reason = "failed its trial call (%d of %d failed)" % (entry["trial_failed"], entry["trial_calls"])
            models[key] = _bench(entry["provider"], entry["model"], level, reason, now, cfg, calls, failed)
            state["history"][key] = {"level": level, "at": int(now)}
            events.append(_event("rebench", models[key], now))
        elif entry["trial_calls"] > 0:
            state["forgiven"][key] = {"calls": calls, "failed": failed}
            state["history"][key] = {"level": entry.get("level", 1), "at": int(now)}
            events.append(_event("return", dict(entry, reason="its trial call succeeded"), now))
            del models[key]
    return events


def _bench_pass(state: Dict[str, Any], seen: Mapping[str, Tuple[str, str, int, int]], now: float,
                cfg: Config) -> List[Dict[str, Any]]:
    """Bench each pair whose calls since it last returned fail over the
    threshold."""
    models, forgiven, history, events = state["models"], state["forgiven"], state["history"], []
    for key, (provider, model, calls, failed) in seen.items():
        # A forgiven pair is judged on calls made since it returned; the
        # forgiven mark follows a window that shrank.
        past = forgiven.get(key) or {"calls": 0, "failed": 0}
        past = {"calls": min(past["calls"], calls), "failed": min(past["failed"], failed)}
        if past["calls"] or past["failed"]:
            forgiven[key] = past
        else:
            forgiven.pop(key, None)
        if key in models:
            continue
        fresh_calls, fresh_failed = calls - past["calls"], failed - past["failed"]
        if fresh_calls < cfg.min_sample or fresh_failed < cfg.threshold * fresh_calls:
            continue
        last = history.get(key) or {}
        repeat = bool(last) and now - last.get("at", 0) < cfg.max_cooldown
        level = last.get("level", 0) + 1 if repeat else 1
        reason = "failed %d of %d calls (%s), threshold %s" % (
            fresh_failed, fresh_calls, _percent(fresh_failed, fresh_calls), "%d%%" % round(100 * cfg.threshold))
        models[key] = _bench(provider, model, level, reason, now, cfg, calls, failed)
        history[key] = {"level": level, "at": int(now)}
        events.append(_event("bench", models[key], now))
    return events


def evaluate(rows: Optional[Sequence[Any]], state: Optional[Mapping[str, Any]], now: float,
             cfg: Optional[Config] = None) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """(new state, the events of this pass). `rows` are the gateway's rows
    for one window ({target, model, hits, ok, limit}); None when the gateway
    has no current answer, when only the clock moves a benched pair to its
    trial. The input state is not changed."""
    cfg = cfg or Config()
    new = _clean(copy.deepcopy(dict(state)) if state else empty_state())
    seen = _index(rows) if rows is not None else None
    events = _trial_pass(new, seen, now, cfg)
    if seen is not None:
        events += _bench_pass(new, seen, now, cfg)
    new["events"] = (new["events"] + events)[-EVENTS_KEPT:]
    return new, events


def snapshot(state: Optional[Mapping[str, Any]], now: float) -> Dict[str, Any]:
    """The published list (GET /api/bench): the pairs benched now, the pairs
    on trial, and the recent events. Grug and the gateway read this too, so
    one bench applies everywhere."""
    state = state or empty_state()
    shown = ("provider", "model", "status", "since", "until", "level", "reason")
    entries = sorted(({k: e.get(k) for k in shown} for e in (state.get("models") or {}).values()),
                     key=lambda e: (e["provider"], e["model"]))
    return {"generated_at": int(now),
            "benched": [e for e in entries if e["status"] == BENCHED and (e["until"] or 0) > now],
            "trial": [e for e in entries if e["status"] == TRIAL or (e["status"] == BENCHED and (e["until"] or 0) <= now)],
            "events": list(state.get("events") or [])[-EVENTS_KEPT:]}


def matches(provider: str, lane: str) -> bool:
    """Whether a benched provider (a gateway target) is this lane: the lane
    itself, or the lane a numbered slot belongs to."""
    return provider in (lane, labels.base_lane(lane))


def benched_pairs(published: Any, now: float, ttl: int = LIST_TTL) -> List[Tuple[str, str]]:
    """The (provider, model) pairs to skip, from a published list read with
    its `fetched_at` stamp; none when the list is older than ttl or malformed.
    A pair whose cooldown has ended is on trial and is not skipped."""
    if not isinstance(published, dict):
        return []
    stamp = published.get("fetched_at", published.get("generated_at"))
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not 0 <= now - stamp <= ttl:
        return []
    out = []
    for item in published.get("benched") or []:
        if not isinstance(item, dict) or not item.get("provider") or not item.get("model"):
            continue
        until = item.get("until")
        if isinstance(until, (int, float)) and not isinstance(until, bool) and until <= now:
            continue
        out.append((str(item["provider"]), str(item["model"])))
    return out


def is_benched(pairs: Sequence[Tuple[str, str]], lane: str, model: Optional[str]) -> bool:
    """Whether this model is benched on this lane."""
    return bool(model) and any(m == model and matches(p, lane) for p, m in pairs)


def digest_lines(published: Any, since: float, now: float) -> List[str]:
    """One line per bench event after `since`, for the station's digest."""
    if not isinstance(published, dict):
        return []
    words = {"bench": "benched", "rebench": "benched again", "trial": "on trial", "return": "back"}
    out = []
    for event in published.get("events") or []:
        if not isinstance(event, dict) or event.get("kind") not in words:
            continue
        at = event.get("at")
        if isinstance(at, bool) or not isinstance(at, (int, float)) or not since < at <= now:
            continue
        line = "- %s %s: %s" % (event.get("provider"), event.get("model"), words[event["kind"]])
        if event["kind"] in ("bench", "rebench") and event.get("reason"):
            line += " (%s)" % event["reason"]
        out.append(line)
    return out
