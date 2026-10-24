"""The Tiers and allowances page (/tiers, loom#968).

One page for the whole tier system: Loom's tiers and model ladder (from
bus/factory.py) and Grug's review order; the gateway's calls per target and
model and Loom's runs per lane and model, each row with its allowance state
and its hits, success rate and median latency over 24 hours and 7 days; and
what runs now in Loom, the inference gateway and Grug.

Everything here is pure: it renders the SourceState values
dashboard/tiers_sources.py keeps and reads nothing itself. A source with no
answer leaves its cells unknown, never zero; a stale source makes no current
claim (its allowance cells read "unknown (stale since ...)"), and its panel
says so with its last good read time.
"""
from __future__ import annotations

import html
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from bus import bench as bench_lib
from bus import labels, ladders

from . import usage
from .tiers_sources import SourceState

KIND_WORDS = {"own-hardware": "own hardware", "free": "free", "subscription": "subscription",
              "never-paid": "never paid"}
# The gateway's row kinds: a Spark served the call, or a free provider did.
GATEWAY_KINDS = {"spark": "own-hardware", "remote": "free"}
# The keys Grug's endpoint may list its review order under, first found
# wins. Speculative until grug#1286 ships its contract; pin them to it then.
GRUG_ORDER_KEYS = ("tiers", "review_order", "order")
# Grug's Live now lists Elder's model calls only.
GRUG_PERSONA = "elder"
TEXT_MAX = 200
UNKNOWN = {"state": "unknown", "text": "unknown", "until": None}


# -- the tier list ----------------------------------------------------------

def tier_list(env: Optional[Mapping[str, str]] = None,
              model_of: Callable[..., Optional[str]] = ladders.lane_model) -> List[Dict[str, Any]]:
    """Loom's tiers in order (easy, medium, hard), then the model ladder:
    [{name, title, entries: [{lane, model, kind}]}]. A hard entry runs the
    lane's HARD_MODELS model when it has one; any other runs the lane's
    configured model (LOOM_<LANE>_MODEL), None when this process has none.
    Ladder entries carry their rung."""
    env = os.environ if env is None else env
    out = []
    for name, lanes in ladders.TIERS.items():
        entries = []
        for lane in lanes:
            model = ladders.HARD_MODELS.get(lane) if name == "hard" else None
            entries.append({"lane": lane, "model": model or model_of(lane, env), "kind": ladders.lane_kind(lane)})
        out.append({"name": name, "title": name.title(), "entries": entries})
    ladder = [{"rung": rung, "lane": lane, "model": model, "kind": ladders.lane_kind(lane)}
              for rung, (lanes, model) in enumerate(ladders.LADDER, 1) for lane in lanes]
    out.append({"name": "ladder", "title": "Model ladder", "entries": ladder})
    return out


def grug_order(grug: SourceState) -> Optional[List[Dict[str, str]]]:
    """Grug's review order from its endpoint, or None when it has not said:
    each entry a model name, or an object naming the model and its backend."""
    data = grug.data
    if not isinstance(data, dict):
        return None
    listed = next((data[key] for key in GRUG_ORDER_KEYS if isinstance(data.get(key), list)), None)
    out = []
    for item in listed or []:
        if isinstance(item, str):
            out.append({"model": _text(item), "backend": ""})
        elif isinstance(item, dict) and (item.get("model") or item.get("name")):
            out.append({"model": _text(item.get("model") or item.get("name")),
                        "backend": _text(item.get("backend") or item.get("provider") or "")})
    return out or None


# -- shared helpers -----------------------------------------------------------

def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _count(value: Any) -> int:
    number = _number(value)
    return int(number) if number is not None and number > 0 else 0


def _text(value: Any) -> str:
    return str(value)[:TEXT_MAX]


def _time(value: Any) -> Optional[datetime]:
    number = _number(value)
    if number is not None:
        return datetime.fromtimestamp(number, timezone.utc)
    return usage.parse_time(value) if isinstance(value, str) else None


def _stale_allowance(states: Sequence[SourceState]) -> Dict[str, Any]:
    """No current claim: the newest last good read the sources had."""
    reads = [t for t in (_time(s.last_good_at) for s in states) if t is not None]
    since = usage.clock(max(reads)) if reads else "never read"
    return dict(UNKNOWN, text="unknown (stale since %s)" % since)


# -- the bench (loom#969) ---------------------------------------------------

def _benched(bench: Optional[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """The pairs the bench list holds benched, each with its return time as
    a datetime (None when it names none)."""
    out = []
    for item in (bench or {}).get("benched") or []:
        if isinstance(item, dict) and item.get("provider") and item.get("model"):
            until = _time(item.get("until"))
            out.append(dict(item, until=until))
    return out


def _bench_allowance(item: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    until = item["until"]
    when = ", back %s (%s)" % (usage.clock(until), usage.time_until(until, now)) if until else ""
    return {"state": "benched", "until": until,
            "text": "benched for everyone: %s%s" % (_text(item.get("reason") or "repeated failure"), when)}


def _bench_for(benched: Sequence[Dict[str, Any]], model: str, slots: Sequence[str]) -> Optional[Dict[str, Any]]:
    """The benched pair for this model on any of these lanes or targets."""
    return next((b for b in benched if _text(b["model"]) == model
                 and any(bench_lib.matches(_text(b["provider"]), slot) for slot in slots)), None)


# -- gateway rows -----------------------------------------------------------

def _gateway_cell(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """A window's figures for one gateway row; a window that answered without
    the row is a real zero."""
    row = row or {}
    hits, ok, limit = _count(row.get("hits")), _count(row.get("ok")), _count(row.get("limit"))
    success = _number(row.get("success_rate"))
    if success is None and "success_rate" not in row and hits - limit > 0:
        success = ok / (hits - limit)
    p50 = _number(row.get("p50_ms"))
    return {"hits": hits, "ok": ok, "limit": limit, "failed": None, "success": success,
            "limits_unseparated": False, "p50_ms": int(p50) if p50 is not None else None}


def _gateway_index(data: Any) -> Dict[tuple, Dict[str, Any]]:
    rows = data.get("rows") if isinstance(data, dict) else None
    return {(_text(row["target"]), _text(row["model"])): row for row in rows or []
            if isinstance(row, dict) and row.get("target") and row.get("model")}


def _current_answer(day: SourceState, week: SourceState) -> Optional[SourceState]:
    """The fresh gateway answer with the newest generated_at, or None when
    no fresh one exists (then nothing it said is a current claim)."""
    fresh = [s for s in (day, week) if isinstance(s.data, dict) and not s.stale]
    return max(fresh, key=lambda s: _number(s.data.get("generated_at")) or 0.0) if fresh else None


def _breakers(target: str, model: str, answer: SourceState, now: datetime) -> List[Dict[str, Any]]:
    """The gateway's open breakers on this target and model (lane "*" or the
    model itself) that have not reopened by now, each with its reopen time."""
    base = _number(answer.data.get("generated_at")) or answer.last_good_at
    out = []
    for entry in answer.data.get("open") or []:
        if not isinstance(entry, dict) or _text(entry.get("target") or "") != target:
            continue
        if entry.get("lane") not in (None, "", "*") and _text(entry.get("lane")) != model:
            continue
        wait = _number(entry.get("reopens_in_s"))
        until = datetime.fromtimestamp(base, timezone.utc) + timedelta(seconds=wait) if (
            base is not None and wait is not None) else None
        if until is None or until > now:
            out.append({"entry": entry, "until": until, "wait": wait})
    return out


def _breaker_text(breaker: Dict[str, Any], now: datetime) -> str:
    entry, until = breaker["entry"], breaker["until"]
    when = "until %s (%s)" % (usage.clock(until), usage.time_until(until, now)) if until else "reopen time unknown"
    window = ", %s breaker window" % usage.span(breaker["wait"]) if breaker["wait"] else ""
    caller = ", caller %s" % _text(entry["caller"]) if entry.get("caller") else ""
    return "cooling %s%s: %s%s" % (when, window, _text(entry.get("reason") or "open"), caller)


def _gateway_allowance(target: str, model: str, day: SourceState, week: SourceState,
                       now: datetime) -> Dict[str, Any]:
    """Benched when the gateway reports the model benched; cooling until its
    breaker reopens, with the window, reason and caller, for any other open
    breaker; else available. A stale gateway claims nothing."""
    answer = _current_answer(day, week)
    if answer is None:
        return _stale_allowance([day, week])
    breakers = _breakers(target, model, answer, now)
    benched = [b for b in breakers if b["entry"].get("benched") is True or b["entry"].get("reason") == "benched"]
    if benched:
        return {"state": "benched", "until": benched[0]["until"],
                "text": "benched by the gateway: %s" % _text(benched[0]["entry"].get("reason") or "benched")}
    if not breakers:
        return {"state": "available", "text": "available", "until": None}
    latest = max(breakers, key=lambda b: b["until"] or datetime.max.replace(tzinfo=timezone.utc))
    return {"state": "cooling", "until": latest["until"], "window": "breaker", "text": _breaker_text(latest, now)}


def gateway_rows(day: SourceState, week: SourceState, now: datetime,
                 benched: Sequence[Dict[str, Any]] = ()) -> List[Dict[str, Any]]:
    """The gateway's calls per target and model; a pair on the bench list
    reads benched whatever the gateway's breakers say."""
    day_rows, week_rows = _gateway_index(day.data), _gateway_index(week.data)
    out = []
    for key in dict.fromkeys(list(day_rows) + list(week_rows)):
        target, model = key
        sample = day_rows.get(key) or week_rows.get(key) or {}
        held = _bench_for(benched, model, [target])
        out.append({"provider": target, "model": model, "source": "gateway", "unit": "calls",
                    "kind": GATEWAY_KINDS.get(str(sample.get("kind") or "")),
                    "allowance": _bench_allowance(held, now) if held else _gateway_allowance(
                        target, model, day, week, now),
                    "w24": _gateway_cell(day_rows.get(key)) if isinstance(day.data, dict) else None,
                    "w7d": _gateway_cell(week_rows.get(key)) if isinstance(week.data, dict) else None})
    return out


# -- Loom rows --------------------------------------------------------------

def _runs_cell(groups: Sequence[Dict[str, Any]], window: str) -> Dict[str, Any]:
    """A window's run counts. A failed run may have been a provider limit,
    which is allowance, not failure, and a stored run does not say which
    (the runner's [limit] line is past the transcript's text cut), so with
    any failure the success rate is unknown rather than counting limits."""
    hits = sum(_count(g.get("runs_" + window)) for g in groups)
    done = [g for g in groups if g.get("outcome") == "done"]
    ok = sum(_count(g.get("runs_" + window)) for g in done)
    failed = hits - ok
    medians = [_number(g.get("p50_%s_s" % window)) for g in done]
    p50 = next((m for m in medians if m is not None), None)
    return {"hits": hits, "ok": ok, "failed": failed, "limit": None,
            "success": 1.0 if hits and not failed else None, "limits_unseparated": failed > 0,
            "p50_ms": int(p50 * 1000) if p50 is not None else None}


def _hold_windows(rows: Sequence[Dict[str, Any]]) -> Dict[str, str]:
    """{slot: the name of the usage window whose reading holds it}."""
    out: Dict[str, str] = {}
    for row in rows:
        name = usage.WINDOW_NAMES.get(str(row.get("window") or ""), str(row.get("window") or ""))
        for slot in (row.get("holds") or {}) if isinstance(row.get("holds"), dict) else {}:
            out.setdefault(slot, name)
    return out


def _hold_allowance(slots: Sequence[str], states: Sequence[Dict[str, Any]], windows: Dict[str, str],
                    now: datetime) -> Dict[str, Any]:
    """Every slot held: cooling until the first hold ends, with its window,
    or benched for a hold with no end."""
    timed = [(s["until"], slot, s) for slot, s in zip(slots, states, strict=True) if s["until"] is not None]
    if not timed:
        return {"state": "benched", "until": None, "text": "benched: " + states[0]["why"]}
    until, slot, state = min(timed, key=lambda item: item[0])
    window = windows.get(slot) or "lane hold"
    why = state["why"].split(" until ")[0]
    return {"state": "cooling", "until": until, "window": window,
            "text": "cooling until %s (%s), %s window: %s" % (usage.clock(until), usage.time_until(until, now),
                                                            window.lower(), why)}


def _meter_allowance(slots: Sequence[str], rows: Sequence[Dict[str, Any]], now: datetime) -> Optional[Dict[str, Any]]:
    """The tightest fresh meter over these slots as an amount left, or None
    when no fresh reading meters them."""
    windows = [usage.read_window(row, now) for row in rows if set(row.get("lanes") or []) & set(slots)]
    meters = [w for w in windows if w["used"] is not None and not w["stale"]]
    if not meters:
        return None
    tight = max(meters, key=lambda w: w["used"])
    text = "%s left (%s window)" % (usage.percent(100 - tight["used"]), tight["name"].lower())
    if "usd_left" in tight:
        text += ", USD %.2f" % tight["usd_left"]
    if tight["reset"] is not None:
        text += ", resets %s" % usage.clock(tight["reset"])
    return {"state": "metered", "until": tight["reset"], "window": tight["name"], "text": text}


def _usage_allowance(slots: Sequence[str], readings: SourceState, now: datetime) -> Dict[str, Any]:
    """A Loom lane's allowance from the usage readings: cooling until its
    hold ends, benched for a hold with no end, the tightest fresh meter's
    amount left, or available. No reading, or a stale snapshot, claims
    nothing."""
    if not isinstance(readings.data, list):
        return dict(UNKNOWN, text="unknown (%s)" % (readings.error or "no usage reading"))
    if readings.stale:
        return _stale_allowance([readings])
    rows = [r for r in readings.data if isinstance(r, dict)]
    holds = usage.all_holds(rows)
    states = [usage.lane_state(holds.get(slot, ""), now) for slot in slots]
    if states and all(s["kind"] != "free" for s in states):
        return _hold_allowance(slots, states, _hold_windows(rows), now)
    return _meter_allowance(slots, rows, now) or {"state": "available", "text": "available", "until": None}


def _tier_slots() -> Dict[str, List[str]]:
    """{lane: its slots named in TIERS or LADDER}, lanes in tier order."""
    out: Dict[str, List[str]] = {}
    named = [lane for lanes in ladders.TIERS.values() for lane in lanes]
    named += [lane for lanes, _ in ladders.LADDER for lane in lanes]
    for slot in named:
        slots = out.setdefault(labels.base_lane(slot), [])
        if slot not in slots:
            slots.append(slot)
    return out


def loom_rows(runs: SourceState, readings: SourceState, now: datetime,
              env: Optional[Mapping[str, str]] = None,
              benched: Sequence[Dict[str, Any]] = ()) -> List[Dict[str, Any]]:
    """Loom's runs per lane and model: a row for every lane the tiers name,
    with no runs too (its allowance is what matters then), and for any other
    lane that ran. Cells are unknown when the run outcomes have no answer."""
    env = os.environ if env is None else env
    answered = isinstance(runs.data, list)
    grouped: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    for item in runs.data if answered else []:
        if isinstance(item, dict) and item.get("lane") and item.get("model"):
            grouped.setdefault(_text(item["lane"]), {}).setdefault(_text(item["model"]), []).append(item)
    tier_slots = _tier_slots()
    out = []
    for lane in list(tier_slots) + [lane for lane in grouped if lane not in tier_slots]:
        models = grouped.get(lane, {})
        slots = sorted(set(tier_slots.get(lane, [])) | {str(s) for groups in models.values()
                                                       for g in groups for s in (g.get("slots") or [lane])})
        allowance = _usage_allowance(slots, readings, now)
        base = {"provider": lane, "source": "loom", "unit": "runs", "kind": ladders.lane_kind(lane),
                "slots": slots, "allowance": allowance}

        def held_for(model: Optional[str], lane: str = lane, slots: List[str] = slots) -> Optional[Dict[str, Any]]:
            """The bench entry that holds this model on the lane, if any."""
            return _bench_for(benched, model or "", [lane] + slots) if model else None

        if not models:
            empty = _runs_cell([], "24h") if answered else None
            configured = ladders.lane_model(lane, env)
            held = held_for(configured)
            out.append(dict(base, model="", configured_model=configured, no_runs=answered,
                            w24=empty, w7d=empty and dict(empty),
                            **({"allowance": _bench_allowance(held, now)} if held else {})))
        for model, groups in sorted(models.items()):
            held = held_for(model)
            out.append(dict(base, model=model, no_runs=False,
                            **({"allowance": _bench_allowance(held, now)} if held else {}),
                            w24=_runs_cell(groups, "24h"), w7d=_runs_cell(groups, "7d")))
    return out


def model_rows(gateway_24h: SourceState, gateway_7d: SourceState, loom_runs: SourceState,
               readings: SourceState, now: datetime, env: Optional[Mapping[str, str]] = None,
               bench: Optional[Mapping[str, Any]] = None) -> List[Dict[str, Any]]:
    """Every provider and model row: the gateway's (source "gateway", calls
    per target and model) and Loom's (source "loom", runs per lane and
    model). Each has {provider, model, source, unit, kind, allowance:
    {state, text, until}, w24, w7d}; a window cell is None when its source
    has no answer."""
    order = {kind: n for n, kind in enumerate(factory.KINDS)}
    benched = _benched(bench)
    gateway = sorted(gateway_rows(gateway_24h, gateway_7d, now, benched),
                     key=lambda r: (order.get(r["kind"], len(order)), r["provider"], r["model"]))
    return gateway + loom_rows(loom_runs, readings, now, env, benched)


# -- live now ---------------------------------------------------------------

def _gateway_calls(data: Any) -> List[Dict[str, Any]]:
    calls = data.get("inflight") if isinstance(data, dict) else None
    return [{"target": _text(call.get("target") or "unknown target"),
             "model": _text(call.get("resolved_model") or call.get("model") or "model not named"),
             "source": _text(call.get("source") or ""), "age_s": _number(call.get("age_s")),
             "cancelling": call.get("cancelling") is True}
            for call in (calls if isinstance(calls, list) else []) if isinstance(call, dict)]


def _grug_review(review: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    started = _time(review.get("started_at"))
    pr = review.get("pr")
    return {"persona": _text(review.get("persona") or "elder"), "repo": _text(review.get("repo") or ""),
            "pr": pr if isinstance(pr, int) and not isinstance(pr, bool) else None,
            "model": _text(review.get("model") or "model not named"),
            "age_s": (now - started).total_seconds() if started else None}


def _grug_reviews(data: Any, now: datetime) -> List[Dict[str, Any]]:
    """Grug's in-flight Elder calls: an entry naming another persona is left
    out."""
    reviews = data.get("inflight") if isinstance(data, dict) else None
    return [_grug_review(review, now) for review in (reviews if isinstance(reviews, list) else [])
            if isinstance(review, dict) and str(review.get("persona") or GRUG_PERSONA).lower() == GRUG_PERSONA]


def _station_runs(data: Any) -> List[Dict[str, Any]]:
    return [{key: run.get(key) for key in ("repo", "number", "title", "lane", "model", "age", "html_url")}
            for run in (data if isinstance(data, list) else []) if isinstance(run, dict)]


def live_now(station_runs: SourceState, gateway_state: SourceState, grug: SourceState,
             now: Optional[datetime] = None) -> Dict[str, Dict[str, Any]]:
    """What runs now: {loom, gateway, grug}, each {source, items}; items are
    empty when the source has no answer (its SourceState says why)."""
    now = now or datetime.now(timezone.utc)
    return {"loom": {"source": station_runs, "items": _station_runs(station_runs.data)},
            "gateway": {"source": gateway_state, "items": _gateway_calls(gateway_state.data)},
            "grug": {"source": grug, "items": _grug_reviews(grug.data, now)}}


# -- the page ---------------------------------------------------------------

SOURCE_NAMES = ("gateway_24h", "gateway_7d", "gateway_state", "grug", "loom_runs", "usage", "station")


def build(sources: Mapping[str, SourceState], now: datetime, env: Optional[Mapping[str, str]] = None,
          bench: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """The page's data from every source's SourceState (SOURCE_NAMES); a
    source left out counts as not read yet. `bench` is the bench list
    (bus.bench.snapshot), None when the dashboard keeps none."""
    get = {name: sources.get(name) or SourceState.not_read(name) for name in SOURCE_NAMES}
    rows = model_rows(get["gateway_24h"], get["gateway_7d"], get["loom_runs"], get["usage"], now, env, bench)
    return {"now": now, "bench": bench, "tiers": tier_list(env), "grug_order": grug_order(get["grug"]), "grug": get["grug"],
            "gateway_rows": [r for r in rows if r["source"] == "gateway"],
            "loom_rows": [r for r in rows if r["source"] == "loom"],
            "gateway_sources": [get["gateway_24h"], get["gateway_7d"]],
            "loom_sources": [get["loom_runs"], get["usage"]],
            "live": live_now(get["station"], get["gateway_state"], get["grug"], now)}


def _seconds(value: Optional[float]) -> str:
    if value is None:
        return "age unknown"
    return "%ds" % int(value) if value < 60 else usage.span(value)


def _source_note(state: SourceState, now: datetime) -> str:
    """Why a source cannot be taken as current, or "" when it can."""
    esc = html.escape
    if not state.configured:
        return '<p class="tiers-note">%s: %s.</p>' % (esc(state.name), esc(state.missing or "not configured"))
    if not state.stale:
        return ""
    good = _time(state.last_good_at)
    when = ("last good read %s (%s)" % (usage.clock(good), usage.time_ago(good, now))) if good else "never read"
    return ('<p class="stale-note tiers-stale" role="status"><strong>Stale.</strong> %s: %s; %s.</p>'
            % (esc(state.name), esc(state.error or "no answer"), esc(when)))


def _notes(states: Sequence[SourceState], now: datetime) -> str:
    return "".join(dict.fromkeys(_source_note(s, now) for s in states))


def _kind_chip(kind: Optional[str]) -> str:
    word = KIND_WORDS.get(kind or "", "kind unknown")
    return '<span class="tiers-kind tiers-kind-%s">%s</span>' % (html.escape(kind or "unknown"), html.escape(word))


def _section(key: str, title: str, note: str, body: str, fact: str = "") -> str:
    return ('<section class="section tiers-section" id="tiers-%s" aria-labelledby="tiers-%s-h">'
            '<div class="section-head"><h2 class="label" id="tiers-%s-h">%s</h2>%s</div>'
            '<p class="section-note">%s</p>%s</section>'
            % (key, key, key, html.escape(title), '<span class="machine-fact">%s</span>' % html.escape(fact)
               if fact else "", note, body))


def _tier_html(tier: Dict[str, Any]) -> str:
    esc = html.escape
    items = ['<li class="tiers-entry">%s<span class="tiers-lane">%s</span> <span class="tiers-model">%s</span> %s</li>'
             % ('<span class="tiers-rung">rung %d</span> ' % e["rung"] if "rung" in e else "", esc(e["lane"]),
                esc(e["model"] or "lane default"), _kind_chip(e["kind"])) for e in tier["entries"]]
    return ('<div class="tiers-tier" id="tier-%s"><h3>%s</h3><ol class="tiers-entries">%s</ol></div>'
            % (esc(tier["name"]), esc(tier["title"]), "".join(items)))


def _tiers_html(data: Dict[str, Any], now: datetime) -> str:
    esc = html.escape
    order = data["grug_order"]
    if order:
        listed = _notes([data["grug"]], now) + '<ol class="tiers-entries">%s</ol>' % "".join(
            '<li class="tiers-entry"><span class="tiers-model">%s</span>%s</li>'
            % (esc(item["model"]), ' <span class="machine-fact">%s</span>' % esc(item["backend"])
               if item["backend"] else "") for item in order)
    else:
        listed = "<p class=\"tiers-note\">waiting for Grug's endpoint: it has not listed its review order.</p>"
    parts = [_tier_html(tier) for tier in data["tiers"]]
    parts.append('<div class="tiers-tier" id="tier-grug"><h3>Grug review order</h3>%s</div>' % listed)
    return _section("tiers", "Tiers",
                    "Loom tries a ticket's lanes in this order, by its difficulty, and a stuck ticket climbs the "
                    "model ladder. Each lane is marked free, own hardware, subscription or never paid (a prepaid "
                    "credit that is not topped up).", '<div class="tiers-grid">%s</div>' % "".join(parts))


_EVENT_WORDS = {"bench": "benched", "rebench": "benched again", "trial": "on trial", "return": "back"}


def _bench_html(data: Dict[str, Any], now: datetime) -> str:
    """The Benched models section: each pair benched or on trial with its
    reason and return time, and the recent bench and un-bench events."""
    esc = html.escape
    bench = data.get("bench")
    note = ("A provider and model whose calls fail (errors, timeouts, unparseable or empty answers) over "
            "a minimum sample is benched for everyone, Loom and Grug alike, for a cooldown that doubles "
            "on each repeat. A limit refusal is allowance, not failure, and never counts. When the "
            "cooldown ends, one trial call decides whether it returns or stays benched for twice as long.")
    if bench is None:
        return _section("bench", "Benched models", note,
                        '<p class="tiers-note">The bench list is not kept by this dashboard.</p>')
    rows = []
    for item in bench.get("benched") or []:
        until = _time(item.get("until"))
        rows.append((item, "benched", "back %s (%s)" % (usage.clock(until), usage.time_until(until, now))
                     if until else "return time unknown"))
    for item in bench.get("trial") or []:
        rows.append((item, "on trial", "one trial call decides"))
    if rows:
        body = ('<div class="tiers-table-wrap"><table class="tiers-table tiers-bench"><thead><tr>'
                '<th>Provider</th><th>Model</th><th>State</th><th>Return</th><th>Level</th><th>Reason</th></tr></thead><tbody>%s</tbody></table></div>'
                % "".join('<tr class="tiers-row tiers-%s" data-row="bench %s %s"><th scope="row" data-label="Provider">%s</th>'
                          '<td data-label="Model">%s</td><td data-label="State">%s</td><td data-label="Return">%s</td>'
                          '<td class="num" data-label="Level">%s</td><td data-label="Reason">%s</td></tr>'
                          % ("benched" if word == "benched" else "cooling", esc(_text(item.get("provider"))),
                             esc(_text(item.get("model"))), esc(_text(item.get("provider"))),
                             esc(_text(item.get("model"))), word, esc(back), esc(str(_count(item.get("level")) or 1)),
                             esc(_text(item.get("reason") or "")))
                          for item, word, back in rows))
    else:
        body = '<p class="empty-state">No model is benched.</p>'
    events = []
    for event in reversed(bench.get("events") or []):
        at = _time(event.get("at")) if isinstance(event, dict) else None
        if at is None or event.get("kind") not in _EVENT_WORDS:
            continue
        events.append('<li><span class="tiers-lane">%s</span> <span class="tiers-model">%s</span> %s '
                      '<span class="machine-fact">%s</span></li>'
                      % (esc(_text(event.get("provider"))), esc(_text(event.get("model"))),
                         _EVENT_WORDS[event["kind"]], esc(usage.time_ago(at, now))))
    log = '<h3>Recent changes</h3><ul class="tiers-live-list">%s</ul>' % "".join(events[:20]) if events else ""
    return _section("bench", "Benched models", note, body + log, "%d benched" % len(bench.get("benched") or []))


def _fraction_pct(value: float) -> str:
    return "%d%%" % round(value * 100)


def _latency(ms: Optional[int]) -> str:
    if ms is None:
        return "n/a"
    if ms < 1000:
        return "%d ms" % ms
    if ms < 60000:
        return "%.1f s" % (ms / 1000.0)
    return usage.span(ms / 1000.0)


def _success_text(cell: Dict[str, Any]) -> str:
    if cell["limits_unseparated"]:
        return "unknown (limits not separated)"
    return "n/a" if cell["success"] is None else _fraction_pct(cell["success"])


def _cells(cell: Optional[Dict[str, Any]], window: str, unit: str) -> str:
    esc = html.escape
    if cell is None:
        return "".join('<td class="tiers-unknown" data-label="%s %s">unknown</td>' % (window, label)
                       for label in (unit, "success", "p50"))
    extra = []
    if cell["limit"]:
        extra.append("%d limit" % cell["limit"])
    if cell["failed"]:
        extra.append("%d done, %d failed" % (cell["ok"], cell["failed"]))
    fact = ' <span class="machine-fact">%s</span>' % esc(", ".join(extra)) if extra else ""
    return ('<td class="num" data-label="%s %s">%d%s</td><td class="num" data-label="%s success">%s</td>'
            '<td class="num" data-label="%s p50">%s</td>'
            % (window, unit, cell["hits"], fact, window, esc(_success_text(cell)), window,
               esc(_latency(cell["p50_ms"]))))


def _row_html(row: Dict[str, Any]) -> str:
    esc = html.escape
    allowance = row["allowance"]
    if row["source"] == "loom":
        key = ("loom %s %s" % (row["provider"], row["model"])).strip()
        model = row["model"] or row.get("configured_model") or "lane default"
        if row.get("no_runs"):
            model += " (no runs in 7 days)"
    else:
        key, model = "%s %s" % (row["provider"], row["model"]), row["model"]
    return ('<tr class="tiers-row tiers-%s" data-row="%s"><th scope="row" data-label="%s">%s</th>'
            '<td data-label="Model">%s</td><td data-label="Kind">%s</td>'
            '<td class="tiers-allowance" data-label="Allowance">%s</td>%s%s</tr>'
            % (esc(allowance["state"]), esc(key), "Lane" if row["source"] == "loom" else "Target",
               esc(row["provider"]), esc(model), _kind_chip(row["kind"]), esc(allowance["text"]),
               _cells(row["w24"], "24h", row["unit"]), _cells(row["w7d"], "7d", row["unit"])))


def _table(rows: Sequence[Dict[str, Any]], first: str, unit: str, empty: str) -> str:
    if not rows:
        return '<p class="empty-state">%s</p>' % html.escape(empty)
    head = "".join("<th>%s</th>" % h for h in (first, "Model", "Kind", "Allowance", "24h " + unit, "24h success",
                                                "24h p50", "7d " + unit, "7d success", "7d p50"))
    return ('<div class="tiers-table-wrap"><table class="tiers-table"><thead><tr>%s</tr></thead>'
            '<tbody>%s</tbody></table></div>' % (head, "".join(_row_html(r) for r in rows)))


def _rows_html(data: Dict[str, Any], now: datetime) -> str:
    gateway = _section(
        "gateway", "Gateway calls by target and model",
        "Calls the inference gateway served, per target (a Spark or a free provider) and model. Success is ok "
        "out of the calls that were not limits (a limit is allowance, not failure), so a row of limits only "
        "reads n/a; p50 is over ok calls. Allowance is the gateway's open breakers now.",
        _notes(data["gateway_sources"], now) + _table(data["gateway_rows"], "Target", "calls",
                                                      "No gateway row: the gateway's stats have not answered."),
        "%d rows" % len(data["gateway_rows"]))
    loom = _section(
        "loom", "Loom runs by lane and model",
        "Loom's lane runs per lane (numbered slots with their lane) and model, every lane the tiers name "
        "included. A failed run may have been a provider limit, which the stored run does not mark, so a lane "
        "with failures reads unknown (limits not separated) rather than a rate that counts limits as failures; "
        "p50 is over finished runs. Allowance is the lane's holds and meters from the station's usage readings.",
        _notes(data["loom_sources"], now) + _table(data["loom_rows"], "Lane", "runs", "No Loom lane."),
        "%d rows" % len(data["loom_rows"]))
    return gateway + loom


def _live_block(key: str, title: str, block: Dict[str, Any], items: List[str], empty: str, now: datetime) -> str:
    state: SourceState = block["source"]
    if items:
        body = '<ul class="tiers-live-list">%s</ul>' % "".join(items)
    elif state.configured and state.data is not None:
        body = '<p class="tiers-note">%s</p>' % html.escape(empty)
    else:
        body = ""
    return ('<div class="tiers-live" id="live-%s"><h3>%s</h3>%s%s</div>'
            % (key, html.escape(title), _source_note(state, now), body))


def _loom_items(runs: Sequence[Dict[str, Any]]) -> List[str]:
    esc = html.escape
    out = []
    for run in runs:
        ref = "%s#%s" % (run.get("repo") or "?", run.get("number") or "?")
        url = str(run.get("html_url") or "")
        link = '<a href="%s">%s</a>' % (esc(url, quote=True), esc(ref)) if url.startswith("https://") else esc(ref)
        facts = [run.get("lane") or "lane unknown", run.get("model") or "model not recorded", run.get("age") or ""]
        out.append('<li>%s <span class="tiers-live-title">%s</span> <span class="machine-fact">%s</span></li>'
                   % (link, esc(_text(run.get("title") or "")), esc(" / ".join(str(f) for f in facts if f))))
    return out


def _gateway_items(calls: Sequence[Dict[str, Any]]) -> List[str]:
    esc = html.escape
    out = []
    for call in calls:
        facts = ["from " + call["source"] if call["source"] else "", _seconds(call["age_s"]),
                 "cancelling" if call["cancelling"] else ""]
        out.append('<li><span class="tiers-lane">%s</span> <span class="tiers-model">%s</span> '
                   '<span class="machine-fact">%s</span></li>'
                   % (esc(call["target"]), esc(call["model"]), esc(" / ".join(f for f in facts if f))))
    return out


def _grug_items(reviews: Sequence[Dict[str, Any]]) -> List[str]:
    esc = html.escape
    out = []
    for review in reviews:
        ref = "%s#%d" % (review["repo"], review["pr"]) if review["repo"] and review["pr"] else (
            review["repo"] or "pull request not named")
        out.append('<li><span class="tiers-lane">%s</span> %s <span class="tiers-model">%s</span> '
                   '<span class="machine-fact">%s</span></li>'
                   % (esc(review["persona"]), esc(ref), esc(review["model"]), esc(_seconds(review["age_s"]))))
    return out


def _live_html(data: Dict[str, Any], now: datetime) -> str:
    live = data["live"]
    blocks = (_live_block("loom", "Loom runs", live["loom"], _loom_items(live["loom"]["items"]),
                          "No Loom run is running now.", now)
              + _live_block("gateway", "Gateway calls", live["gateway"], _gateway_items(live["gateway"]["items"]),
                            "No calls in flight now.", now)
              + _live_block("grug", "Grug Elder reviews", live["grug"], _grug_items(live["grug"]["items"]),
                            "No reviews in flight now.", now))
    return _section("live", "Live now", "Loom runs from the station, calls in flight at the gateway, and Grug's "
                    "Elder reviews in flight, as of the last background read.",
                    '<div class="tiers-live-grid">%s</div>' % blocks)


def render(data: Dict[str, Any]) -> str:
    """The page body: the tier list, the gateway and Loom rows, then Live now."""
    now = data["now"]
    return ('<p class="page-lede">The whole tier system, each provider and model\'s allowance and how well it '
            'answers, and what runs now in Loom, the gateway and Grug. Figures are from the last background '
            'read; a source that does not answer shows as stale, never as zero.</p>'
            + _tiers_html(data, now) + _bench_html(data, now) + _rows_html(data, now) + _live_html(data, now))
