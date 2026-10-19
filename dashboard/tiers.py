"""The Tiers and allowances page (/tiers, loom#968).

One page for the whole tier system: Loom's tiers and model ladder (from
bus/factory.py) and Grug's review order; one row per provider and model with
its allowance state and its hits, success rate and median latency over 24
hours and 7 days; and what runs now in Loom, the inference gateway and Grug.

Everything here is pure: it renders the envelopes dashboard/tiers_sources.py
keeps (`{name, configured, missing, data, last_good_at, stale, error}`) and
reads nothing itself. A source with no answer leaves its cells unknown, never
zero, and a stale source says so with its last good read time.
"""
from __future__ import annotations

import html
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from bus import factory
from bus.brain import _lane_model

from . import usage

KIND_WORDS = {"own-hardware": "own hardware", "free": "free", "subscription": "subscription",
              "never-paid": "never paid"}
# The gateway's row kinds: a Spark served the call, or a free provider did.
GATEWAY_KINDS = {"spark": "own-hardware", "remote": "free"}
# Breaker reasons that are allowance, not failure: the lane cools down and
# reopens on its own.
LIMIT_REASONS = ("rate_limited", "rate_limit", "limit", "limited", "quota", "quota_exceeded", "cooldown")
# The keys Grug's endpoint may list its review order under, first found wins.
GRUG_ORDER_KEYS = ("tiers", "review_order", "order")
TEXT_MAX = 200
Envelope = Mapping[str, Any]


# -- the tier list ----------------------------------------------------------

def tier_list(env: Optional[Mapping[str, str]] = None,
              lane_model: Callable[..., Optional[str]] = _lane_model) -> List[Dict[str, Any]]:
    """Loom's tiers in order (easy, medium, hard), then the model ladder:
    [{name, title, entries: [{lane, model, kind}]}]. A hard entry runs the
    lane's HARD_MODELS model when it has one; any other runs the lane's
    configured model (LOOM_<LANE>_MODEL), None when this process has none.
    Ladder entries carry their rung."""
    env = os.environ if env is None else env
    out = []
    for name, lanes in factory.TIERS.items():
        entries = []
        for lane in lanes:
            model = factory.HARD_MODELS.get(lane) if name == "hard" else None
            entries.append({"lane": lane, "model": model or lane_model(lane, env),
                            "kind": factory.lane_kind(lane)})
        out.append({"name": name, "title": name.title(), "entries": entries})
    ladder = [{"rung": rung, "lane": lane, "model": model, "kind": factory.lane_kind(lane)}
              for rung, (lanes, model) in enumerate(factory.LADDER, 1) for lane in lanes]
    out.append({"name": "ladder", "title": "Model ladder", "entries": ladder})
    return out


def grug_order(grug: Envelope) -> Optional[List[Dict[str, str]]]:
    """Grug's review order from its endpoint, or None when it has not said:
    each entry a model name, or an object naming the model and its backend."""
    data = grug.get("data") if isinstance(grug, Mapping) else None
    if not isinstance(data, dict):
        return None
    listed = next((data[key] for key in GRUG_ORDER_KEYS if isinstance(data.get(key), list)), None)
    if not listed:
        return None
    out = []
    for item in listed:
        if isinstance(item, str):
            out.append({"model": _text(item), "backend": ""})
        elif isinstance(item, dict):
            model = item.get("model") or item.get("name")
            if model:
                out.append({"model": _text(model), "backend": _text(item.get("backend") or item.get("provider") or "")})
    return out or None


# -- one row per provider and model -----------------------------------------

def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _count(value: Any) -> int:
    number = _number(value)
    return int(number) if number is not None and number > 0 else 0


def _text(value: Any) -> str:
    return str(value)[:TEXT_MAX]


def _data(envelope: Optional[Envelope]) -> Any:
    return envelope.get("data") if isinstance(envelope, Mapping) else None


def _gateway_cell(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """A window's figures for one gateway row; a window that answered without
    the row is a real zero."""
    if row is None:
        return {"hits": 0, "ok": 0, "limit": 0, "success": None, "p50_ms": None}
    hits, ok, limit = _count(row.get("hits")), _count(row.get("ok")), _count(row.get("limit"))
    success = _number(row.get("success_rate"))
    if success is None and "success_rate" not in row and hits - limit > 0:
        success = ok / (hits - limit)
    p50 = _number(row.get("p50_ms"))
    return {"hits": hits, "ok": ok, "limit": limit, "success": success,
            "p50_ms": int(p50) if p50 is not None else None}


def _gateway_index(data: Any) -> Dict[tuple, Dict[str, Any]]:
    rows = data.get("rows") if isinstance(data, dict) else None
    out = {}
    for row in rows or []:
        if isinstance(row, dict) and row.get("target") and row.get("model"):
            out[(_text(row["target"]), _text(row["model"]))] = row
    return out


def _newest_open(*envelopes: Optional[Envelope]) -> Optional[Dict[str, Any]]:
    """The gateway answer with the newest generated_at, for its open list."""
    answers = [d for d in (_data(e) for e in envelopes) if isinstance(d, dict)]
    return max(answers, key=lambda d: _number(d.get("generated_at")) or 0.0) if answers else None


def _gateway_allowance(target: str, model: str, answer: Optional[Dict[str, Any]], now: datetime) -> Dict[str, Any]:
    """Available, cooling until its breaker reopens, or benched for the
    breaker's reason, from the gateway's open list."""
    if answer is None:
        return {"state": "unknown", "text": "unknown: the gateway's stats have not been read", "until": None}
    generated = _number(answer.get("generated_at"))
    for entry in answer.get("open") or []:
        if not isinstance(entry, dict) or _text(entry.get("target") or "") != target:
            continue
        lane = entry.get("lane")
        if lane not in (None, "", "*") and _text(lane) != model:
            continue
        reason = _text(entry.get("reason") or "open")
        wait = _number(entry.get("reopens_in_s"))
        until = (datetime.fromtimestamp(generated, timezone.utc) + timedelta(seconds=wait)
                 if generated is not None and wait is not None else None)
        if until is not None and until <= now:
            continue
        if reason in LIMIT_REASONS:
            when = "until %s (%s)" % (usage._clock(until), usage._until(until, now)) if until else "reopen time unknown"
            return {"state": "cooling", "until": until, "window": "breaker",
                    "text": "cooling %s: gateway breaker, %s" % (when, reason)}
        return {"state": "benched", "until": until, "text": "benched: %s (gateway breaker)" % reason}
    return {"state": "available", "text": "available", "until": None}


def _gateway_rows(day: Envelope, week: Envelope, now: datetime) -> List[Dict[str, Any]]:
    day_data, week_data = _data(day), _data(week)
    day_rows, week_rows = _gateway_index(day_data), _gateway_index(week_data)
    answer = _newest_open(day, week)
    out = []
    for key in dict.fromkeys(list(day_rows) + list(week_rows)):
        target, model = key
        sample = day_rows.get(key) or week_rows.get(key) or {}
        out.append({"provider": target, "model": model, "source": "gateway", "unit": "calls",
                    "kind": GATEWAY_KINDS.get(str(sample.get("kind") or "")),
                    "allowance": _gateway_allowance(target, model, answer, now),
                    "w24": _gateway_cell(day_rows.get(key)) if isinstance(day_data, dict) else None,
                    "w7d": _gateway_cell(week_rows.get(key)) if isinstance(week_data, dict) else None})
    return out


def _runs_cell(groups: Sequence[Dict[str, Any]], window: str) -> Dict[str, Any]:
    hits = sum(_count(g.get("runs_" + window)) for g in groups)
    done = [g for g in groups if g.get("outcome") == "done"]
    ok = sum(_count(g.get("runs_" + window)) for g in done)
    # The median of the runs that finished, as the gateway's is of ok calls;
    # the store already merges a lane's slots, so there is one done group.
    medians = [_number(g.get("p50_%s_s" % window)) for g in done]
    p50 = next((m for m in medians if m is not None), None)
    return {"hits": hits, "ok": ok, "limit": 0, "success": ok / hits if hits else None,
            "p50_ms": int(p50 * 1000) if p50 is not None else None}


def _usage_allowance(slots: Sequence[str], readings: Any, now: datetime,
                     missing: str = "") -> Dict[str, Any]:
    """A Loom lane's allowance from the usage readings: cooling until its
    hold ends, benched for a hold with no end, the tightest fresh meter's
    amount left, or available."""
    if not isinstance(readings, list):
        return {"state": "unknown", "text": "unknown: " + (missing or "no usage reading"), "until": None}
    rows = [r for r in readings if isinstance(r, dict)]
    holds = usage._all_holds(rows)
    states = [usage.lane_state(holds.get(slot, ""), now) for slot in slots]
    if states and all(s["kind"] != "free" for s in states):
        untils = [s["until"] for s in states if s["until"] is not None]
        if untils:
            until = min(untils)
            return {"state": "cooling", "until": until, "window": "lane hold",
                    "text": "cooling until %s (%s): %s" % (usage._clock(until), usage._until(until, now),
                                                          re.sub(r" until .*$", "", states[0]["why"]))}
        return {"state": "benched", "until": None, "text": "benched: " + states[0]["why"]}
    meters = []
    for row in rows:
        if not set(row.get("lanes") or []) & set(slots):
            continue
        window = usage._window(row, now)
        if window["used"] is not None and not window["stale"]:
            meters.append(window)
    if meters:
        tight = max(meters, key=lambda w: w["used"])
        text = "%s left (%s window)" % (usage._pct(100 - tight["used"]), tight["name"].lower())
        if "usd_left" in tight:
            text += ", USD %.2f" % tight["usd_left"]
        if tight["reset"] is not None:
            text += ", resets %s" % usage._clock(tight["reset"])
        return {"state": "metered", "until": tight["reset"], "window": tight["name"], "text": text}
    return {"state": "available", "text": "available", "until": None}


def _loom_rows(runs: Envelope, usage_readings: Envelope, now: datetime) -> List[Dict[str, Any]]:
    data = _data(runs)
    if not isinstance(data, list):
        return []
    grouped: Dict[tuple, List[Dict[str, Any]]] = {}
    for item in data:
        if isinstance(item, dict) and item.get("lane") and item.get("model"):
            grouped.setdefault((_text(item["lane"]), _text(item["model"])), []).append(item)
    readings = _data(usage_readings)
    missing = str((usage_readings or {}).get("error") or "")
    out = []
    for (lane, model), groups in grouped.items():
        slots = sorted({str(s) for g in groups for s in (g.get("slots") or [lane])})
        out.append({"provider": lane, "model": model, "source": "loom", "unit": "runs",
                    "kind": factory.lane_kind(lane), "slots": slots,
                    "allowance": _usage_allowance(slots, readings, now, missing),
                    "w24": _runs_cell(groups, "24h"), "w7d": _runs_cell(groups, "7d")})
    return out


def model_rows(gateway_24h: Envelope, gateway_7d: Envelope, loom_runs: Envelope, usage_readings: Envelope,
               now: datetime) -> List[Dict[str, Any]]:
    """One row per provider and model: the gateway's (calls, per target and
    model) and Loom's own lane runs (per lane and model). Each row has
    {provider, model, source, unit, kind, allowance: {state, text, until},
    w24, w7d}; a window cell is None when its source has no answer, and its
    success is None when no hit could succeed (every hit a limit)."""
    rows = _gateway_rows(gateway_24h, gateway_7d, now) + _loom_rows(loom_runs, usage_readings, now)
    order = {kind: n for n, kind in enumerate(factory.KINDS)}
    return sorted(rows, key=lambda r: (order.get(r["kind"], len(order)), r["provider"], r["model"], r["source"]))


# -- live now ---------------------------------------------------------------

def _time(value: Any) -> Optional[datetime]:
    number = _number(value)
    if number is not None:
        return datetime.fromtimestamp(number, timezone.utc)
    if isinstance(value, str):
        return usage._parse_time(value)
    return None


def _gateway_calls(data: Any) -> List[Dict[str, Any]]:
    calls = data.get("inflight") if isinstance(data, dict) else None
    out = []
    for call in calls if isinstance(calls, list) else []:
        if not isinstance(call, dict):
            continue
        age = _number(call.get("age_s"))
        out.append({"target": _text(call.get("target") or "unknown target"),
                    "model": _text(call.get("resolved_model") or call.get("model") or "model not named"),
                    "source": _text(call.get("source") or ""), "age_s": age,
                    "cancelling": call.get("cancelling") is True})
    return out


def _grug_reviews(data: Any, now: datetime) -> List[Dict[str, Any]]:
    reviews = data.get("inflight") if isinstance(data, dict) else None
    out = []
    for review in reviews if isinstance(reviews, list) else []:
        if not isinstance(review, dict):
            continue
        started = _time(review.get("started_at"))
        pr = review.get("pr")
        out.append({"persona": _text(review.get("persona") or "a persona"),
                    "repo": _text(review.get("repo") or ""),
                    "pr": int(pr) if isinstance(pr, int) and not isinstance(pr, bool) else None,
                    "model": _text(review.get("model") or "model not named"),
                    "age_s": (now - started).total_seconds() if started else None})
    return out


def _station_runs(data: Any) -> List[Dict[str, Any]]:
    out = []
    for run in data if isinstance(data, list) else []:
        if isinstance(run, dict):
            out.append({key: run.get(key) for key in ("repo", "number", "title", "lane", "model", "age", "html_url")})
    return out


def live_now(station_runs: Envelope, gateway_state: Envelope, grug: Envelope,
             now: Optional[datetime] = None) -> Dict[str, Dict[str, Any]]:
    """What runs now: {loom, gateway, grug}, each its source's envelope with
    the items it lists (empty when the source has no answer: the envelope
    says why)."""
    now = now or datetime.now(timezone.utc)
    return {"loom": dict(station_runs, items=_station_runs(_data(station_runs))),
            "gateway": dict(gateway_state, items=_gateway_calls(_data(gateway_state))),
            "grug": dict(grug, items=_grug_reviews(_data(grug), now))}


# -- the page ---------------------------------------------------------------

def build(sources: Mapping[str, Envelope], now: datetime, env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """The page's data from every source's envelope: gateway_24h, gateway_7d,
    gateway_state, grug, loom_runs, usage and station."""
    def get(name: str) -> Envelope:
        return sources.get(name) or {"name": name, "configured": True, "missing": "", "data": None,
                                     "last_good_at": None, "stale": True, "error": "not read yet"}
    return {"now": now, "tiers": tier_list(env), "grug_order": grug_order(get("grug")), "grug": get("grug"),
            "rows": model_rows(get("gateway_24h"), get("gateway_7d"), get("loom_runs"), get("usage"), now),
            "row_sources": [get(n) for n in ("gateway_24h", "gateway_7d", "loom_runs", "usage")],
            "live": live_now(get("station"), get("gateway_state"), get("grug"), now)}


def _seconds(value: Optional[float]) -> str:
    if value is None:
        return "age unknown"
    return "%ds" % int(value) if value < 60 else usage._span(value)


def _source_note(envelope: Envelope, now: datetime) -> str:
    """Why a source cannot be taken as current, or "" when it can."""
    esc = html.escape
    name = str(envelope.get("name") or "A source")
    if not envelope.get("configured", True):
        return '<p class="tiers-note">%s: %s.</p>' % (esc(name), esc(str(envelope.get("missing") or "not configured")))
    if not envelope.get("stale"):
        return ""
    good = _time(envelope.get("last_good_at"))
    when = ("last good read %s (%s)" % (usage._clock(good), usage._ago(good, now))) if good else "never read"
    return ('<p class="stale-note tiers-stale" role="status"><strong>Stale.</strong> %s: %s; %s.</p>'
            % (esc(name), esc(str(envelope.get("error") or "no answer")), esc(when)))


def _notes(envelopes: Sequence[Envelope], now: datetime) -> str:
    return "".join(dict.fromkeys(_source_note(e, now) for e in envelopes))


def _kind_chip(kind: Optional[str]) -> str:
    word = KIND_WORDS.get(kind or "", "kind unknown")
    return '<span class="tiers-kind tiers-kind-%s">%s</span>' % (html.escape(kind or "unknown"), html.escape(word))


def _tiers_html(data: Dict[str, Any], now: datetime) -> str:
    esc = html.escape
    parts = []
    for tier in data["tiers"]:
        items = []
        for entry in tier["entries"]:
            rung = '<span class="tiers-rung">rung %d</span> ' % entry["rung"] if "rung" in entry else ""
            items.append('<li class="tiers-entry">%s<span class="tiers-lane">%s</span> '
                         '<span class="tiers-model">%s</span> %s</li>'
                         % (rung, esc(entry["lane"]), esc(entry["model"] or "lane default"),
                            _kind_chip(entry["kind"])))
        parts.append('<div class="tiers-tier" id="tier-%s"><h3>%s</h3><ol class="tiers-entries">%s</ol></div>'
                     % (esc(tier["name"]), esc(tier["title"]), "".join(items)))
    order = data["grug_order"]
    if order:
        listed = '<ol class="tiers-entries">%s</ol>' % "".join(
            '<li class="tiers-entry"><span class="tiers-model">%s</span>%s</li>'
            % (esc(item["model"]), ' <span class="machine-fact">%s</span>' % esc(item["backend"])
               if item["backend"] else "") for item in order)
    else:
        listed = "<p class=\"tiers-note\">waiting for Grug's endpoint: it has not listed its review order.</p>"
    parts.append('<div class="tiers-tier" id="tier-grug"><h3>Grug review order</h3>%s%s</div>'
                 % (_notes([data["grug"]], now) if order else "", listed))
    return ('<section class="section tiers-section" id="tiers-tiers" aria-labelledby="tiers-tiers-h">'
            '<div class="section-head"><h2 class="label" id="tiers-tiers-h">Tiers</h2></div>'
            '<p class="section-note">Loom tries a ticket\'s lanes in this order, by its difficulty, and a '
            'stuck ticket climbs the model ladder. Each lane is marked free, own hardware, subscription '
            'or never paid (a prepaid credit that is not topped up).</p>'
            '<div class="tiers-grid">%s</div></section>' % "".join(parts))


def _pct(value: Optional[float]) -> str:
    return "%d%%" % round(value * 100)


def _latency(ms: Optional[int]) -> str:
    if ms is None:
        return "n/a"
    if ms < 1000:
        return "%d ms" % ms
    if ms < 60000:
        return "%.1f s" % (ms / 1000.0)
    return usage._span(ms / 1000.0)


def _cells(cell: Optional[Dict[str, Any]], window: str, unit: str) -> str:
    esc = html.escape
    if cell is None:
        return "".join('<td class="tiers-unknown" data-label="%s %s">unknown</td>' % (window, label)
                       for label in (unit, "success", "p50"))
    success = "n/a" if cell["success"] is None else _pct(cell["success"])
    limit = ' <span class="machine-fact">%d limit</span>' % cell["limit"] if cell["limit"] else ""
    return ('<td class="num" data-label="%s %s">%d%s</td><td class="num" data-label="%s success">%s</td>'
            '<td class="num" data-label="%s p50">%s</td>'
            % (window, unit, cell["hits"], limit, window, esc(success), window, esc(_latency(cell["p50_ms"]))))


def _rows_html(data: Dict[str, Any], now: datetime) -> str:
    esc = html.escape
    notes = _notes(data["row_sources"], now)
    rows = []
    for row in data["rows"]:
        allowance = row["allowance"]
        rows.append('<tr class="tiers-row tiers-%s" data-row="%s %s"><th scope="row" data-label="Provider">%s'
                    '<span class="machine-fact"> %s</span></th><td data-label="Model">%s</td>'
                    '<td data-label="Kind">%s</td><td class="tiers-allowance" data-label="Allowance">%s</td>%s%s</tr>'
                    % (esc(allowance["state"]), esc(row["provider"]), esc(row["model"]), esc(row["provider"]),
                       esc(row["unit"]), esc(row["model"]), _kind_chip(row["kind"]), esc(allowance["text"]),
                       _cells(row["w24"], "24h", row["unit"]), _cells(row["w7d"], "7d", row["unit"])))
    if rows:
        table = ('<div class="tiers-table-wrap"><table class="tiers-table"><thead><tr><th>Provider</th><th>Model</th>'
                 '<th>Kind</th><th>Allowance</th><th>24h hits</th><th>24h success</th><th>24h p50</th>'
                 '<th>7d hits</th><th>7d success</th><th>7d p50</th></tr></thead><tbody>%s</tbody></table></div>'
                 % "".join(rows))
    else:
        table = '<p class="empty-state">No provider or model has a row: no source above has answered with one.</p>'
    return ('<section class="section tiers-section" id="tiers-models" aria-labelledby="tiers-models-h">'
            '<div class="section-head"><h2 class="label" id="tiers-models-h">Providers and models</h2>'
            '<span class="machine-fact">%d rows</span></div>'
            '<p class="section-note">The gateway counts calls per provider and model; Loom counts its lane runs '
            'per lane and model. Success is ok out of the hits that were not limits (a limit is allowance, not '
            'failure), so a row of limits only reads n/a. p50 is over successful calls or finished runs. A '
            'source with no answer leaves its cells unknown.</p>%s%s</section>'
            % (len(rows), notes, table))


def _live_block(key: str, title: str, block: Dict[str, Any], items: List[str], empty: str, now: datetime) -> str:
    note = _source_note(block, now)
    answered = block.get("configured", True) and block.get("data") is not None
    if items:
        body = '<ul class="tiers-live-list">%s</ul>' % "".join(items)
    elif answered:
        body = '<p class="tiers-note">%s</p>' % html.escape(empty)
    else:
        body = ""
    return '<div class="tiers-live" id="live-%s"><h3>%s</h3>%s%s</div>' % (key, html.escape(title), note, body)


def _live_html(data: Dict[str, Any], now: datetime) -> str:
    esc = html.escape
    live = data["live"]
    loom = []
    for run in live["loom"]["items"]:
        ref = "%s#%s" % (run.get("repo") or "?", run.get("number") or "?")
        url = str(run.get("html_url") or "")
        link = '<a href="%s">%s</a>' % (esc(url, quote=True), esc(ref)) if url.startswith("https://") else esc(ref)
        facts = " / ".join(str(v) for v in (run.get("lane") or "lane unknown", run.get("model") or "model not recorded",
                                            run.get("age") or "") if v)
        loom.append('<li>%s <span class="tiers-live-title">%s</span> <span class="machine-fact">%s</span></li>'
                    % (link, esc(_text(run.get("title") or "")), esc(facts)))
    gateway = ['<li><span class="tiers-lane">%s</span> <span class="tiers-model">%s</span> '
               '<span class="machine-fact">%s</span></li>'
               % (esc(c["target"]), esc(c["model"]),
                  esc(" / ".join(v for v in ("from " + c["source"] if c["source"] else "", _seconds(c["age_s"]),
                                             "cancelling" if c["cancelling"] else "") if v)))
               for c in live["gateway"]["items"]]
    grug = []
    for review in live["grug"]["items"]:
        ref = "%s#%d" % (review["repo"], review["pr"]) if review["repo"] and review["pr"] else (
            review["repo"] or "pull request not named")
        grug.append('<li><span class="tiers-lane">%s</span> %s <span class="tiers-model">%s</span> '
                    '<span class="machine-fact">%s</span></li>'
                    % (esc(review["persona"]), esc(ref), esc(review["model"]), esc(_seconds(review["age_s"]))))
    return ('<section class="section tiers-section" id="tiers-live" aria-labelledby="tiers-live-h">'
            '<div class="section-head"><h2 class="label" id="tiers-live-h">Live now</h2></div>'
            '<p class="section-note">Loom runs from the station, calls in flight at the gateway, and Grug '
            'reviews in flight, as of the last background read.</p><div class="tiers-live-grid">%s%s%s</div>'
            '</section>'
            % (_live_block("loom", "Loom runs", live["loom"], loom, "No Loom run is running now.", now),
               _live_block("gateway", "Gateway calls", live["gateway"], gateway, "No calls in flight now.", now),
               _live_block("grug", "Grug reviews", live["grug"], grug, "No reviews in flight now.", now)))


def render(data: Dict[str, Any]) -> str:
    """The page body: the tier list, the provider and model rows, then Live now."""
    now = data["now"]
    return ('<p class="page-lede">The whole tier system, each provider and model\'s allowance and how well it '
            'answers, and what runs now in Loom, the gateway and Grug. Figures are from the last background '
            'read; a source that does not answer shows as stale, never as zero.</p>'
            + _tiers_html(data, now) + _rows_html(data, now) + _live_html(data, now))
