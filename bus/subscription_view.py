"""Read-only subscription rows from the station's existing usage files."""

import json
import math
import re

from . import labels, pace, usage


SUBSCRIPTIONS = frozenset(("agy", "claude", "codex", "copilot", "opencode-go"))
_WINDOW = re.compile(r"[a-z0-9-]{1,24}", re.ASCII)


def _longest(records):
    selected = {}
    for record in records:
        provider = record.get("provider")
        window = record.get("window")
        minutes = pace._number(record.get("window_minutes"))
        if (not isinstance(provider, str) or provider not in SUBSCRIPTIONS
                or not isinstance(window, str) or not _WINDOW.fullmatch(window)
                or minutes is None or minutes <= 0):
            continue
        observed = usage.observation_time(record)
        rank = (minutes, observed if observed is not None else -math.inf)
        if provider not in selected or rank > selected[provider][0]:
            selected[provider] = (rank, record)
    return {provider: record for provider, (_, record) in selected.items()}


def _reading(record, now, points):
    # Meter timestamps are stored as ISO datetimes with microsecond precision.
    now = round(now, 6)
    observed = usage.observation_time(record)
    reset = usage._parse(record.get("resets_at"))
    elapsed = pace.elapsed_pct(reset, record.get("window_minutes"), now)
    used = pace._number(record.get("used_pct"))
    if (used is None or not 0 <= used <= 100 or elapsed is None or observed is None
            or not max(reset - record["window_minutes"] * 60, now - usage.STALE_AFTER)
            <= observed <= now):
        return dict.fromkeys(("used_pct", "elapsed_pct", "observed_at", "resets_at", "verdict"))
    return {"used_pct": used, "elapsed_pct": elapsed, "observed_at": usage._iso(observed),
            "resets_at": usage._iso(reset), "verdict": pace.verdict(used, elapsed, points)}


def _requested(plan, row, now):
    at = pace._number(plan.get("at"))
    if at is None or not 0 <= now - at <= usage.HEARTBEAT_FRESH or row["verdict"] is None:
        return None
    decision = pace._providers(plan).get(row["provider"], {})
    if (decision.get("window"), decision.get("used"), decision.get("verdict"),
            decision.get("open"), decision.get("max")) != (
            row["window"], row["used_pct"], row["verdict"], row["running"], row["allowed"]):
        return None
    want = decision.get("want")
    return want if type(want) is int and row["running"] < want <= row["allowed"] else None


def read(home, now, env, records=None):
    """One row per subscription, including providers without a current meter."""
    records = _longest(usage.read_snapshot(home) if records is None else records)
    groups = usage._provider_groups(labels.LANES)
    plan = pace.read(home)
    points = pace.slack(env)
    result = []
    for provider in sorted(SUBSCRIPTIONS & groups.keys()):
        record = records.get(provider, {})
        configured = groups[provider]
        reported = record.get("lanes")
        slots = [slot for slot in configured if slot in reported] if isinstance(reported, list) else []
        slots = slots or configured
        row = {"provider": provider, "window": record.get("window"),
               "running": pace._open_slots(home, slots, now),
               "allowed": pace._maximum(provider, slots, env),
               **_reading(record, now, points)}
        row["requested"] = _requested(plan, row, now)
        result.append(row)
    return result


def for_upload(records, subscriptions, now):
    """Add one summary per provider without rewriting the raw meter readings."""
    pending = {row["provider"]: {key: value for key, value in row.items() if key != "requested"}
               for row in subscriptions}
    payload = []
    for record in records:
        summary = pending.pop(record.get("provider"), None)
        payload.append({**record, "subscription_pace": summary} if summary is not None else record)
    groups = usage._provider_groups(labels.LANES)
    for provider, summary in pending.items():
        row = usage._record(provider, groups[provider], "all", None, None, None,
                            None, "subscription meter", now, "no reading")
        payload.append({**row, "subscription_pace": summary})
    return payload


def _cells(row):
    slots = "%d of %d" % (row["running"], row["allowed"])
    if row["requested"] is not None:
        slots += " (asks for %d)" % row["requested"]
    yield row["provider"]
    yield row["window"] or "-"
    if row["used_pct"] is None:
        yield from ("no reading", "-", "-", "-")
    else:
        yield f'{row["used_pct"]:.1f}'.removesuffix(".0") + "%"
        yield f'{row["elapsed_pct"]:.0f}%'
        yield row["resets_at"]
        yield row["verdict"]
    yield slots


def lines(rows):
    """Render a subscription table independently of the lane-state table."""
    return ["", "| subscription | window | used | elapsed | reset (UTC) | pace | slots |",
            "|---|---|---|---|---|---|---|",
            *("| " + " | ".join(_cells(row)) + " |" for row in rows)]


def signature(rows):
    """Publish changed readings or decisions without reacting to clock drift."""
    keys = ("provider", "window", "used_pct", "resets_at", "verdict", "running", "allowed", "requested")
    return json.dumps([[row[key] for key in keys] for row in rows])
