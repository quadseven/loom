"""Pick the model of one OpenCode Go or Zen run.

Free models come and go without notice, so no slot pins one: each run reads
the live lists and takes the first free model that answers. Paid Go models
serve build work: a medium or hard ticket starts on the strong models, an easy
ticket only when no free model works. All paid spending follows the plan's
own usage figures, evenly to the renewal (on 2026-10-23 one pinned paid model
spent the monthly allowance in hours).

A model that fails is paused for every slot until its reset, so no slot asks
it again. Two pauses cover a whole group: the plan's limit covers every paid
Go model, and Zen's free limit covers every Zen model. The runner
(lanes/run_lane.sh) calls this module; the key arrives on stdin, never in
argv. docs/LANES.md explains the rules.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

GO_LIST = "https://opencode.ai/zen/go/v1/models"
ZEN_LIST = "https://opencode.ai/zen/v1/models"
# The catalogue the opencode CLI reads. It hides a model marked deprecated
# here even while the provider still lists it.
STATUS_LIST = "https://models.dev/api.json"
GO_PROBE = "https://opencode.ai/zen/go/v1/chat/completions"
USAGE_URL = "https://opencode.ai/zen/go/v1/usage"
ZEN_PROBE = "https://opencode.ai/zen/v1/chat/completions"
USER_AGENT = "loom-lanes/1.0"
NET_TIMEOUT = 20
# Some models refuse fewer than 16 output tokens with a 400.
PROBE_TOKENS = 16

LIST_FRESH = 300
LIST_STALE = 6 * 3600
# Seconds a pause lasts when the provider names no reset.
PAUSE_LIMIT = 900
PAUSE_GONE = 86400
PAUSE_FAILED = 1800
PAUSE_SILENT = 600
PAUSE_UNSETTLED = 3600
PAUSE_MOST = 32 * 86400
RETRY_NOTHING = 900

GROUP_PAID = "go-paid"
MODEL_KEY = "model:"
TIER_EASY, TIER_STRONG, TIER_NONE = "easy", "strong", "none"
GROUP_ZEN = "zen-free"

# The paid models a Go slot may fall back to, cheapest for the plan first,
# with their published requests per 5 hours, week and month on the Go plan
# (https://opencode.ai/v2/docs/console/go/#usage-limits, 2026-11-01). Any
# other model is refused, whatever the live list offers. DeepSeek V4.1 Flash
# is left out: its hidden reasoning spent 8,192 tokens on one empty answer.
GO_PAID: Dict[str, Tuple[int, int, int]] = {
    "opencode-go/gpt-6-luna": (5_640, 14_080, 28_170),
    "opencode-go/claude-haiku-5-5": (5_133, 12_830, 25_640),
    "opencode-go/mimo-v2.6-flash": (32_608, 81_470, 162_930),
    "opencode-go/muse-spark-1.3-contributor": (49_075, 122_740, 245_480),
}
# The models a medium or hard ticket starts on, best first. Measured on 2026-11-01
# with the real lane prompt on one real ticket: GPT 6 Luna finished in 92 steps
# with its tests green, and Claude Haiku 5.5 finished with 6 tests failing.
# Kimi K3.1 Pro leads the Artificial Analysis index but cost five times as much
# and had not finished at 28 steps. MiMo-V2.6-Pro edited nothing in 33 steps.
GO_STRONG = ("opencode-go/gpt-6-luna", "opencode-go/claude-haiku-5-5")
# Models that take easy tickets only: Beam Free's scorecard is 4 merged of 11.
EASY_ONLY = ("beam-free",)
WINDOWS = (5 * 3600, 7 * 86400, 30 * 86400)
# The plan guard reads the provider's usage figures, which count every caller
# of the key. Paid runs stop at USAGE_STOP percent of a window, and the weekly
# and monthly windows also follow the calendar: USAGE_STOP times the part of
# the window gone, plus PACE_SLACK points, so the plan lasts to the renewal.
# Usage that cannot be read stops paid runs.
# One strong run spent about 10 rolling points and 2 monthly points on
# 2026-11-01, so the rolling stop leaves room for the four slots' runs in flight.
USAGE_STOP = {"rolling": 60.0, "weekly": 80.0, "monthly": 90.0}
PACED = {"weekly": 7 * 86400.0, "monthly": 30.4 * 86400.0}
PACE_SLACK = 5.0
# The most one model may spend of its own published limit in any window,
# counted from this machine's runs, so no model burns the plan in hours.
MODEL_SHARE = 0.5
# Requests a paid run holds from its start, so slots starting together cannot
# overrun the budget. A run that dies keeps its hold.
RUN_RESERVE = 500
# The output tokens the published limits assume per request. Hidden reasoning
# bills as output, so a step spending more counts as more requests.
REQUEST_OUTPUT = 300

# Tried first, in this order, when listed; any other free model follows, so a
# new model is used with no change here. Measured on a real ticket 2026-10-23.
FREE_ORDER = ("beam-free", "deepseek-v4.1-flash-free", "glm-5.3-flash-free",
              "nemotron-3.5-super-vl-free", "mimo-v2.6-flash-free")

GONE_RE = re.compile(r"ProviderModelNotFound|model not found|model is unavailable|deprecated", re.I)
LIMIT_RE = re.compile(r"\b429\b|rate.?limit|too many requests|usage.?limit", re.I)

Opener = Callable[[urllib.request.Request, int], Optional[Tuple[int, Mapping[str, str], bytes]]]


@dataclass(frozen=True)
class Pick:
    """The model of a run, or None with the time to ask again."""
    model: Optional[str]
    kind: str = ""
    note: str = ""
    until: float = 0


def usage_dir(home: Path) -> Path:
    return Path(home) / "state" / "usage"


def pause_file(home: Path) -> Path:
    return usage_dir(home) / "model-pause.json"


def budget_file(home: Path) -> Path:
    return usage_dir(home) / "go-budget.jsonl"


def unsettled_file(home: Path) -> Path:
    """Written by the runner when a paid run's spend could not be recorded."""
    return usage_dir(home) / "go-budget.unsettled"


def _cache_file(home: Path) -> Path:
    return usage_dir(home) / "model-lists.json"


@contextlib.contextmanager
def _locked(home: Path) -> Iterator[None]:
    """One slot at a time reads and writes the shared pause and budget files."""
    usage_dir(home).mkdir(parents=True, exist_ok=True)
    with open(usage_dir(home) / "model-pick.lock", "w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: Mapping[str, Any]) -> None:
    tmp = path.with_name("%s.%d.tmp" % (path.name, os.getpid()))
    tmp.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _open(request: urllib.request.Request, timeout: int) -> Optional[Tuple[int, Mapping[str, str], bytes]]:
    """The answer to a request; a refusal is an answer, silence is None."""
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - _request checks the scheme
            return response.getcode() or 200, dict(response.headers), response.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), err.read()
    except (OSError, ValueError):
        return None


def _request(url: str, headers: Mapping[str, str], data: Optional[bytes] = None
             ) -> Optional[urllib.request.Request]:
    """A request to an http(s) URL. Any other scheme gets none: these URLs
    come from settings, and urllib would open a local file."""
    if not url.startswith(("https://", "http://")):
        return None
    return urllib.request.Request(url, data=data, headers=dict(headers))  # noqa: S310 - scheme checked above


def _list_source(url: str, opener: Opener) -> Optional[Any]:
    request = _request(url, {"User-Agent": USER_AGENT})
    answer = opener(request, NET_TIMEOUT) if request else None
    if answer is None or answer[0] != 200:
        return None
    try:
        return json.loads(answer[2].decode("utf-8"))
    except ValueError:
        return None


def _ids(doc: Any) -> Optional[List[str]]:
    data = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(data, list):
        return None
    return [m["id"] for m in data if isinstance(m, dict) and isinstance(m.get("id"), str)]


def _live_models(doc: Any) -> Optional[Dict[str, List[str]]]:
    """Each provider's models the CLI still offers."""
    if not isinstance(doc, dict):
        return None
    out = {}
    for provider in ("opencode-go", "opencode"):
        models = doc.get(provider, {}).get("models") if isinstance(doc.get(provider), dict) else None
        if not isinstance(models, dict):
            return None
        out[provider] = [k for k, v in models.items()
                         if not (isinstance(v, dict) and v.get("status") == "deprecated")]
    return out


def _lists(home: Path, now: float, env: Mapping[str, str], opener: Opener) -> Dict[str, Any]:
    """The live lists, each cached LIST_FRESH seconds and used up to
    LIST_STALE seconds when the source does not answer."""
    sources = (("go", env.get("LOOM_OPENCODE_GO_MODELS_URL") or GO_LIST, _ids),
               ("zen", env.get("LOOM_ZEN_MODELS_URL") or ZEN_LIST, _ids),
               ("live", env.get("LOOM_MODELS_STATUS_URL") or STATUS_LIST, _live_models))
    cache = _read_json(_cache_file(home))
    out: Dict[str, Any] = {}
    for name, url, read in sources:
        held = cache.get(name) if isinstance(cache.get(name), dict) else {}
        age = now - float(held.get("at") or 0)
        if age >= LIST_FRESH:
            got = read(_list_source(url, opener))
            if got is not None:
                held = {"at": now, "value": got}
                age = 0
                cache[name] = held
        if age < LIST_STALE:
            out[name] = held.get("value")
    with _locked(home):
        _write_json(_cache_file(home), cache)
    return out


def _cached_lists(home: Path, now: float) -> Dict[str, Any]:
    cache = _read_json(_cache_file(home))
    return {name: held.get("value") for name, held in cache.items()
            if isinstance(held, dict) and now - float(held.get("at") or 0) < LIST_STALE}


def _free_key(model: str) -> Tuple[bool, bool, int, str]:
    """Go's free models first (Zen has its own free limit), then those that
    do not train on prompts."""
    raw = model.split("/", 1)[1]
    place = FREE_ORDER.index(raw) if raw in FREE_ORDER else len(FREE_ORDER)
    return (model.startswith("opencode/"), "contributor" in raw, place, model)


def _trains(model: str) -> bool:
    return model.startswith("opencode/") or "contributor" in model


def candidates(lane: str, private: bool, lists: Mapping[str, Any]) -> Tuple[List[str], List[str]]:
    """The free models and the paid models this run may use, best first. A
    model must be on its provider's list and on the CLI's. With neither list
    of a provider there are no models of it: a stale pin is never a guess."""
    live = lists.get("live")
    if not isinstance(live, dict):
        return [], []

    def offered(provider: str, source: str) -> List[str]:
        ids = lists.get(source) or []
        return ["%s/%s" % (provider, m) for m in ids if m in (live.get(provider) or [])]

    go, zen = offered("opencode-go", "go"), offered("opencode", "zen")
    free = [m for m in zen if m.endswith("-free")]
    paid: List[str] = []
    if lane == "opencode-go":
        free += [m for m in go if m.endswith("-free")]
        if private:
            free = [m for m in free if not _trains(m)]
        paid = [m for m in GO_PAID if m in go and not (private and _trains(m))]
    return sorted(free, key=_free_key), paid


def _timestamp(text: Any) -> Optional[float]:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def read_usage(key: str, env: Mapping[str, str], opener: Opener) -> Optional[Dict[str, Tuple[float, float]]]:
    """Each usage window as (percent, reset epoch), or None unless the provider
    answered for all of them."""
    url = env.get("LOOM_OPENCODE_GO_USAGE_URL", USAGE_URL)
    request = _request(url, {"User-Agent": USER_AGENT, "Authorization": "Bearer %s" % key})
    answer = opener(request, NET_TIMEOUT) if request else None
    if answer is None or answer[0] != 200:
        return None
    try:
        usage = json.loads(answer[2].decode("utf-8"))["usage"]
        out = {}
        for name in USAGE_STOP:
            percent, reset = float(usage[name]["percent"]), _timestamp(usage[name]["resetsAt"])
            if reset is None:
                return None
            out[name] = (percent, reset)
    except (KeyError, TypeError, ValueError):
        return None
    return out


def plan_stop(usage: Optional[Mapping[str, Tuple[float, float]]], now: float
              ) -> Optional[Tuple[float, str]]:
    """When paid runs may start again and why, or None while the plan has room.
    Unreadable usage stops them."""
    if usage is None:
        return now + RETRY_NOTHING, "plan usage could not be read"
    for name, stop in USAGE_STOP.items():
        percent, reset = usage[name]
        if percent >= stop:
            return reset, "%s usage %.0f%% is at its %.0f%% stop" % (name, percent, stop)
        if name in PACED:
            gone = min(1.0, max(0.0, 1 - (reset - now) / PACED[name]))
            allowed = stop * gone + PACE_SLACK
            if percent >= allowed:
                return now + RETRY_NOTHING, "%s usage %.0f%% is ahead of its %.0f%% pace" % (name, percent, allowed)
    return None


def _demoted(home: Path, brief_line: str) -> Optional[List[str]]:
    """Models the scorecard demoted (bus/scorecard.py), from its file and the
    brief's Demoted-Models line, which reaches a runner without the file. None
    when the file cannot be read, the station could not read it, or neither
    source exists: then only free models run."""
    line = brief_line.split()
    if line == ["unreadable"]:
        return None
    path = Path(home) / "state" / "station" / "scorecard.json"
    if not path.exists():
        return [m for m in line if m != "none"] if line else None
    try:
        demoted = json.loads(path.read_text(encoding="utf-8")).get("demoted")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(demoted, dict):
        return None
    return [k[len(MODEL_KEY):] for k in demoted if k.startswith(MODEL_KEY)] + [m for m in line if m != "none"]


def _pauses(home: Path, now: float) -> Dict[str, Dict[str, Any]]:
    return {k: v for k, v in _read_json(pause_file(home)).items()
            if isinstance(v, dict) and float(v.get("until") or 0) > now}


def pause(home: Path, key: str, until: float, why: str, now: float) -> None:
    """Pause a model or group for every slot; a longer pause stands."""
    with _locked(home):
        held = _pauses(home, now)
        if float(held.get(key, {}).get("until") or 0) < until:
            held[key] = {"until": until, "why": why}
        _write_json(pause_file(home), held)


def _group(model: str) -> Optional[str]:
    if model in GO_PAID:
        return GROUP_PAID
    return GROUP_ZEN if model.startswith("opencode/") else None


def _paused(held: Mapping[str, Mapping[str, Any]], model: str) -> Optional[Mapping[str, Any]]:
    group = _group(model)
    return held.get(model) or (held.get(group) if group else None)


def _retry_after(headers: Mapping[str, str]) -> Optional[int]:
    for name, value in headers.items():
        if name.lower() == "retry-after" and str(value).strip().isdigit():
            return min(int(value), PAUSE_MOST)
    return None


def _probe_verdict(model: str, answer: Optional[Tuple[int, Mapping[str, str], bytes]]
                   ) -> Optional[Tuple[str, int, str]]:
    """What a probe's answer pauses: (key, seconds, why), or None for a model
    that may run. Any other refusal says nothing of the model: the CLI may
    reach what a plain request cannot."""
    if answer is None:
        return model, PAUSE_SILENT, "no answer to the probe"
    code, headers, raw = answer
    body = raw.decode("utf-8", "replace")
    if code == 429:
        seconds = _retry_after(headers) or PAUSE_LIMIT
        if "GoUsageLimitError" in body and _group(model) == GROUP_PAID:
            return GROUP_PAID, seconds, "the Go plan's limit (429 GoUsageLimitError)"
        if "FreeUsageLimitError" in body and _group(model) == GROUP_ZEN:
            return GROUP_ZEN, seconds, "Zen's free limit (429 FreeUsageLimitError)"
        return model, seconds, "429 from the provider"
    if code in (400, 404) and GONE_RE.search(body):
        return model, PAUSE_GONE, "the provider no longer serves it"
    return None


def _probe(model: str, slot: str, key: str, env: Mapping[str, str],
           opener: Opener) -> Optional[Tuple[str, int, str]]:
    zen = model.startswith("opencode/")
    url = env.get("LOOM_ZEN_PROBE_URL", ZEN_PROBE) if zen else env.get("LOOM_OPENCODE_GO_PROBE_URL", GO_PROBE)
    if not url:
        return None
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT,
               env.get("LOOM_OPENCODE_GO_PROBE_HEADER") or "x-opencode-session":
               "loom-probe-%s-%d" % (slot, os.getpid())}
    if key and not zen:
        headers["Authorization"] = "Bearer %s" % key
    body = {"model": model.split("/", 1)[1], "max_tokens": PROBE_TOKENS,
            "messages": [{"role": "user", "content": "ping"}]}
    request = _request(url, headers, json.dumps(body).encode("utf-8"))
    return _probe_verdict(model, opener(request, NET_TIMEOUT) if request else None)


def _entries(home: Path) -> Iterable[Mapping[str, Any]]:
    try:
        lines = budget_file(home).read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        with contextlib.suppress(ValueError):
            entry = json.loads(line)
            if isinstance(entry, dict):
                yield entry


def spent(home: Path, now: float, model: str, only: Optional[str] = None) -> Tuple[float, ...]:
    """The share spent in each window by the plan, or by the model `only`, in
    units of each entry's own model's limit; `model`'s limits stand in for a
    model since removed."""
    shares = [0.0] * len(WINDOWS)
    for entry in _entries(home):
        if only and entry.get("model") != only:
            continue
        limits = GO_PAID.get(str(entry.get("model")), GO_PAID[model])
        age = now - float(entry.get("at") or 0)
        for i, span in enumerate(WINDOWS):
            if age < span:
                shares[i] += float(entry.get("requests") or 0) / limits[i]
    return tuple(shares)


def _unsettled(home: Path, now: float) -> bool:
    """A recent failed settlement: the ledger may undercount, so no paid run."""
    try:
        return now - float(unsettled_file(home).read_text(encoding="utf-8").strip()) < PAUSE_UNSETTLED
    except OSError:
        return False
    except ValueError:
        return True


def run_reserve(model: str) -> int:
    """The requests a paid run holds: a low-limit model holds a quarter of its
    5 hour limit, so one run still fits under MODEL_SHARE."""
    return min(RUN_RESERVE, GO_PAID[model][0] // 4)


def _has_room(home: Path, now: float, model: str) -> bool:
    """One model's guard, from the runs this machine recorded."""
    if _unsettled(home, now):
        return False
    reserve = run_reserve(model)
    return all(share + reserve / GO_PAID[model][i] <= MODEL_SHARE
               for i, share in enumerate(spent(home, now, model, only=model)))


def _spend(home: Path, now: float, model: str, requests: int) -> None:
    usage_dir(home).mkdir(parents=True, exist_ok=True)
    with open(budget_file(home), "a", encoding="utf-8") as out:
        out.write(json.dumps({"at": now, "model": model, "requests": requests}) + "\n")


def _reserve(home: Path, now: float, model: str) -> bool:
    with _locked(home):
        if not _has_room(home, now, model):
            return False
        _spend(home, now, model, run_reserve(model))
    return True


def effective_requests(events: Iterable[str]) -> int:
    """A run's requests from its JSON events: one per step, more when a step's
    output and hidden reasoning exceed what the published limits assume."""
    total = 0
    for line in events:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "step_finish":
            continue
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        tokens = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
        output = int(tokens.get("output") or 0) + int(tokens.get("reasoning") or 0)
        total += max(1, math.ceil(output / REQUEST_OUTPUT))
    return total


def settle(home: Path, now: float, model: str, events: Iterable[str],
           reserve: Optional[int] = None) -> None:
    """Replace a paid run's hold with what it used. Events that show no step
    leave the hold in place."""
    if model not in GO_PAID:
        return
    used = effective_requests(events)
    if used:
        with _locked(home):
            _spend(home, now, model, used - (run_reserve(model) if reserve is None else reserve))


def pick(lane: str, slot: str, private: bool, key: str, home: Path, now: float,
         env: Mapping[str, str], opener: Opener = _open, tier: str = TIER_NONE,
         brief_demoted: str = "") -> Pick:
    """The model of one run, by tier. TIER_STRONG: a strong model the guards
    allow, else a free model other than EASY_ONLY. TIER_EASY: a free model,
    else a paid flash model the guards allow. TIER_NONE: free models only. No
    model: None and when to ask again."""
    free, paid = candidates(lane, private, _lists(home, now, env, opener))
    demoted = _demoted(home, brief_demoted)
    known = demoted is not None
    demoted = demoted or []
    free, paid = [m for m in free if m not in demoted], [m for m in paid if m not in demoted]
    strong = tier == TIER_STRONG
    if not known:
        paid = []
    if strong:
        free = [m for m in free if m.split("/", 1)[1] not in EASY_ONLY]
        paid = [m for m in paid if m in GO_STRONG]
    else:
        paid = [m for m in paid if tier == TIER_EASY and m not in GO_STRONG]
    held = _pauses(home, now)
    skipped: List[str] = [] if known else ["paid models stop: demotions could not be read"]
    waits: List[float] = []

    def usable(model: str) -> bool:
        pause_now = _paused(held, model)
        if pause_now:
            skipped.append("%s paused: %s" % (model, pause_now.get("why")))
            waits.append(float(pause_now.get("until") or 0))
            return False
        verdict = _probe(model, slot, key, env, opener)
        if verdict is None:
            return True
        target, seconds, why = verdict
        pause(home, target, now + seconds, why, now)
        held[target] = {"until": now + seconds, "why": why}
        skipped.append("%s: %s" % (model, why))
        waits.append(now + seconds)
        return False

    def run_paid() -> Optional[Pick]:
        stop = plan_stop(read_usage(key, env, opener), now) if paid else None
        if stop:
            skipped.append("paid models stop: %s" % stop[1])
            waits.append(stop[0])
            return None
        for model in paid:
            if usable(model):
                if _reserve(home, now, model):
                    why = "strong tier" if strong else "no free model works"
                    return Pick(model, "paid", "; ".join([why] + skipped))
                skipped.append("%s: its guard is reached" % model)
        return None

    def run_free() -> Optional[Pick]:
        for model in free:
            if usable(model):
                return Pick(model, "free", "; ".join(skipped) or "first free model")
        return None

    for step in (run_paid, run_free) if strong else (run_free, run_paid):
        chosen = step()
        if chosen:
            return chosen
    if not free and not paid:
        skipped.append("no usable model on the live lists")
    return Pick(None, note="; ".join(skipped), until=min(waits) if waits else now + RETRY_NOTHING)


def status(lane: str, home: Path, now: float) -> Optional[Tuple[float, str]]:
    """When nothing can run: the time to ask again and why, from what is
    already known, with no request. None while a model may run or the lists
    were never read."""
    lists = _cached_lists(home, now)
    if not lists:
        return None
    free, paid = candidates(lane, False, lists)
    models = free + paid
    held = _pauses(home, now)
    pauses = [_paused(held, m) for m in models]
    if not models or not all(pauses):
        return None
    last = min(pauses, key=lambda p: float(p.get("until") or 0))
    return float(last.get("until") or 0), str(last.get("why"))


def failed(home: Path, now: float, model: str, text: str) -> str:
    """Pause a model after a failed run, by what the CLI printed. The CLI shows
    no reset, so a limit lasts PAUSE_LIMIT and the next probe reads the real one."""
    key = model
    if "GoUsageLimitError" in text and _group(model) == GROUP_PAID:
        key, seconds, why = GROUP_PAID, PAUSE_LIMIT, "the Go plan's limit"
    elif ("FreeUsageLimitError" in text or "Free usage limit" in text) and _group(model) == GROUP_ZEN:
        key, seconds, why = GROUP_ZEN, PAUSE_LIMIT, "Zen's free limit"
    elif GONE_RE.search(text):
        seconds, why = PAUSE_GONE, "the CLI no longer offers it"
    elif LIMIT_RE.search(text):
        seconds, why = PAUSE_LIMIT, "a usage limit"
    else:
        seconds, why = PAUSE_FAILED, "a failed run"
    pause(home, key, now + seconds, why, now)
    return "%s paused for %d s: %s" % (model, seconds, why)


def main(argv: Sequence[str]) -> int:
    home = Path(os.environ.get("LOOM_HOME") or os.path.expanduser("~/loom"))
    now = time.time()
    command = argv[0] if argv else ""
    if command == "pick" and len(argv) == 6:
        key = sys.stdin.readline().strip()
        got = pick(argv[1], argv[2], argv[3] == "true", key, home, now, os.environ, tier=argv[4],
                   brief_demoted=argv[5])
        if got.model is None:
            print("\t%d\t%s" % (got.until, got.note))
            return 3
        print("%s\t%s\t%s" % (got.model, got.kind, got.note))
        return 0
    if command == "status" and len(argv) == 2:
        out = status(argv[1], home, now)
        if out is None:
            return 0
        print("%d\t%s" % out)
        return 3
    if command == "failed" and len(argv) == 2:
        print(failed(home, now, argv[1], sys.stdin.read()))
        return 0
    if command == "settle" and len(argv) == 2:
        settle(home, now, argv[1], sys.stdin)
        return 0
    print("usage: python3 -m bus.model_pick pick <lane> <slot> <private> <tier> <demoted> | status <lane> "
          "| failed <model> | settle <model>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
