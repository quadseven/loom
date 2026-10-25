"""Model catalogue discovery, allowance tracking, and model selection.

Maintains a catalogue of models from provider endpoints (such as OpenCode Go),
tracks allowance buckets opened by 429 refusals (with Retry-After and window name),
and selects models based on ticket tiers.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Set, Union

OPENCODE_GO_MODELS_URL = "https://opencode.ai/inference/go/v1/models"
DISCOVERY_INTERVAL = 3600
PROBE_TIMEOUT = 15

DEFAULT_ALLOWLIST: Set[str] = {
    "beam-free",
    "mimo-v2.6-flash",
    "deepseek-v4.1-flash",
    "muse-spark-1.3-contributor",
}

STRONG_PREFERENCE = [
    "kimi-k3",
    "kimi-k3.1-pro",
    "kimi-k2.7-code",
    "glm-5.3",
    "qwen-2.5-72b",
]

FLASH_PREFERENCE = [
    "deepseek-v4.1-flash",
    "mimo-v2.6-flash",
    "muse-spark-1.3-contributor",
]

FREE_PREFERENCE = [
    "beam-free",
    "minimax-m3.1-flash-free",
    "longcat-2.5-preview-free",
]


@dataclass(frozen=True)
class ModelEntry:
    id: str
    provider: str
    raw_id: str
    api_shape: str
    is_free: bool
    tier: str
    allowance: Optional[int] = None


@dataclass
class AllowanceBucket:
    provider: str
    model: str
    window: Optional[str] = None
    resets_at: Optional[float] = None
    calls_count: int = 0


def classify_tier(raw_id: str) -> str:
    """Classify model into tier: free, flash, or strong."""
    if raw_id.endswith("-free"):
        return "free"
    low = raw_id.lower()
    if any(k in low for k in ("flash", "mini", "small", "lite", "turbo", "contributor")):
        return "flash"
    return "strong"


def is_eligible(raw_id: str, allowlist: Optional[Set[str]], provider: str = "opencode-go") -> bool:
    """Check if model is eligible: free models or allowlisted models."""
    if raw_id.endswith("-free"):
        return True
    if allowlist is None:
        return True
    return raw_id in allowlist or ("%s/%s" % (provider, raw_id)) in allowlist


def parse_model_ids(payload: Any) -> List[str]:
    """Extract model IDs from API response payload."""
    items = []
    if isinstance(payload, dict):
        candidates = payload.get("data") or payload.get("models") or payload.get("items")
        if isinstance(candidates, list):
            items = candidates
        elif isinstance(payload.get("data"), dict):
            items = [payload["data"]]
    elif isinstance(payload, list):
        items = payload

    ids: List[str] = []
    for item in items:
        if isinstance(item, dict) and "id" in item and isinstance(item["id"], str):
            ids.append(item["id"])
        elif isinstance(item, str):
            ids.append(item)
    return ids


def fetch_remote_models(url: str, timeout: int = PROBE_TIMEOUT,
                        fetch: Optional[Callable[[Any], Any]] = None) -> List[str]:
    """Fetch model IDs from remote endpoint."""
    if not (url.startswith("http://") or url.startswith("https://")):
        sys.stderr.write("model discovery failed from %s: scheme must be http or https\n" % url)
        return []
    req = urllib.request.Request(url, headers={"User-Agent": "loom-model-discovery"})  # noqa: S310 - scheme checked above
    try:
        resp = fetch(req) if fetch is not None else urllib.request.urlopen(req, timeout=timeout)  # noqa: S310
        with resp:
            raw = resp.read()
            data = json.loads(raw.decode("utf-8"))
            return parse_model_ids(data)
    except Exception as err:
        sys.stderr.write("model discovery failed from %s: %s\n" % (url, err))
        return []


def discover_models(url: str = OPENCODE_GO_MODELS_URL,
                    allowlist: Optional[Set[str]] = None,
                    provider: str = "opencode-go",
                    fetch: Optional[Callable[[Any], Any]] = None) -> Dict[str, ModelEntry]:
    """Discover models from endpoint, applying allowlist and free filters."""
    raw_ids = fetch_remote_models(url, fetch=fetch)
    cat: Dict[str, ModelEntry] = {}
    effective_allowlist = DEFAULT_ALLOWLIST if allowlist is None else allowlist

    for raw in raw_ids:
        clean = raw.strip()
        if not clean or not is_eligible(clean, effective_allowlist, provider):
            continue
        full_id = clean if "/" in clean else ("%s/%s" % (provider, clean))
        is_free = clean.endswith("-free")
        tier = classify_tier(clean)
        cat[full_id] = ModelEntry(
            id=full_id,
            provider=provider,
            raw_id=clean,
            api_shape="openai-compatible",
            is_free=is_free,
            tier=tier,
        )
    return cat


class AllowanceTracker:
    """Tracks allowance buckets and local call counts per provider and model."""

    def __init__(self, buckets: Optional[Dict[str, AllowanceBucket]] = None):
        self._buckets: Dict[str, AllowanceBucket] = buckets or {}

    def _key(self, provider: str, model: str) -> str:
        clean_model = model.strip()
        return "%s:%s" % (provider, clean_model)

    def get_bucket(self, provider: str, model: str) -> Optional[AllowanceBucket]:
        return self._buckets.get(self._key(provider, model))

    def record_call(self, provider: str, model: str) -> None:
        key = self._key(provider, model)
        if key not in self._buckets:
            self._buckets[key] = AllowanceBucket(provider=provider, model=model)
        self._buckets[key].calls_count += 1

    def record_limit(self, provider: str, model: str, window: str,
                     retry_after: Union[int, float, str],
                     now: Optional[float] = None) -> AllowanceBucket:
        current_time = time.time() if now is None else now
        resets_at = self._compute_reset(window, retry_after, current_time)
        key = self._key(provider, model)
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = AllowanceBucket(provider=provider, model=model)
            self._buckets[key] = bucket
        bucket.window = window
        bucket.resets_at = resets_at
        return bucket

    def _compute_reset(self, window: str, retry_after: Union[int, float, str],
                       current_time: float) -> float:
        try:
            val = float(retry_after)
            if val > current_time:
                return val
            if val > 0:
                return current_time + val
        except (ValueError, TypeError):
            pass

        if window == "5-hour":
            return current_time + 5 * 3600
        if window == "weekly":
            return current_time + 7 * 86400
        if window == "monthly":
            return current_time + 30 * 86400
        return current_time + 3600

    def is_limited(self, provider: str, model: str, now: Optional[float] = None) -> bool:
        current_time = time.time() if now is None else now
        bucket = self.get_bucket(provider, model)
        if bucket is None or bucket.resets_at is None:
            return False
        return current_time < bucket.resets_at

    def save(self, path: Path) -> None:
        data = {}
        for k, b in self._buckets.items():
            data[k] = asdict(b)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: Path) -> AllowanceTracker:
        if not path.is_file():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            buckets = {}
            for k, v in raw.items():
                if isinstance(v, dict):
                    buckets[k] = AllowanceBucket(
                        provider=v.get("provider", "opencode-go"),
                        model=v.get("model", ""),
                        window=v.get("window"),
                        resets_at=v.get("resets_at"),
                        calls_count=v.get("calls_count", 0),
                    )
            return cls(buckets)
        except (OSError, ValueError):
            return cls()


def _model_sort_key(entry: ModelEntry) -> int:
    """Sort models within a tier in preference order."""
    raw = entry.raw_id
    if entry.tier == "strong":
        return STRONG_PREFERENCE.index(raw) if raw in STRONG_PREFERENCE else 100
    if entry.tier == "flash":
        return FLASH_PREFERENCE.index(raw) if raw in FLASH_PREFERENCE else 100
    if entry.tier == "free":
        return FREE_PREFERENCE.index(raw) if raw in FREE_PREFERENCE else 100
    return 100


def select_model(tier: str, catalogue: Dict[str, ModelEntry],
                 tracker: AllowanceTracker, now: Optional[float] = None,
                 pinned_model: Optional[str] = None,
                 public_only: bool = False,
                 preferred_model: Optional[str] = None) -> Optional[ModelEntry]:
    """Select model for ticket tier with available allowance. Brief Model: line wins."""
    current_time = time.time() if now is None else now

    if pinned_model:
        clean_pinned = pinned_model.strip()
        if clean_pinned in catalogue:
            return catalogue[clean_pinned]
        raw = clean_pinned.split("/", 1)[-1]
        return ModelEntry(
            id=clean_pinned,
            provider="opencode-go",
            raw_id=raw,
            api_shape="openai-compatible",
            is_free=raw.endswith("-free"),
            tier=classify_tier(raw),
        )

    if preferred_model:
        clean_pref = preferred_model.strip()
        pref_entry = catalogue.get(clean_pref)
        if pref_entry is not None:
            if (public_only or "contributor" not in pref_entry.raw_id) and \
                    not tracker.is_limited(pref_entry.provider, pref_entry.id, now=current_time):
                return pref_entry

    tier_order = _tier_stages(tier)
    for stage in tier_order:
        candidates = [m for m in catalogue.values() if m.tier == stage]
        candidates.sort(key=_model_sort_key)
        for cand in candidates:
            if not public_only and "contributor" in cand.raw_id:
                continue
            if not tracker.is_limited(cand.provider, cand.id, now=current_time):
                return cand
    return None


def _tier_stages(tier: str) -> List[str]:
    norm = tier.lower()
    if norm in ("hard", "strong"):
        return ["strong", "flash", "free"]
    if norm in ("easy", "medium", "flash"):
        return ["flash", "free"]
    return ["free"]


def tier_candidates(tier: str, catalogue: Dict[str, ModelEntry],
                    public_only: bool = False,
                    preferred_model: Optional[str] = None) -> List[str]:
    """Get ordered list of candidate model IDs for a tier."""
    stages = _tier_stages(tier)
    result = []
    if preferred_model:
        clean_pref = preferred_model.strip()
        if clean_pref in catalogue:
            pref_entry = catalogue[clean_pref]
            if public_only or "contributor" not in pref_entry.raw_id:
                result.append(clean_pref)

    for stage in stages:
        entries = [m for m in catalogue.values() if m.tier == stage]
        entries.sort(key=_model_sort_key)
        for e in entries:
            if not public_only and "contributor" in e.raw_id:
                continue
            if e.id not in result:
                result.append(e.id)
    return result


def default_catalogue() -> Dict[str, ModelEntry]:
    """Built-in default catalogue of OpenCode Go models."""
    cat: Dict[str, ModelEntry] = {}
    for raw in DEFAULT_ALLOWLIST:
        full_id = "opencode-go/%s" % raw
        cat[full_id] = ModelEntry(
            id=full_id,
            provider="opencode-go",
            raw_id=raw,
            api_shape="openai-compatible",
            is_free=raw.endswith("-free"),
            tier=classify_tier(raw),
        )
    return cat


def catalogue_file(home: Union[str, Path]) -> Path:
    return Path(home) / "state" / "usage" / "catalogue.json"


def allowance_file(home: Union[str, Path]) -> Path:
    return Path(home) / "state" / "usage" / "allowance.json"


def save_catalogue(cat: Dict[str, ModelEntry], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    data = {k: asdict(v) for k, v in cat.items()}
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_catalogue(home: Optional[Union[str, Path]] = None) -> Dict[str, ModelEntry]:
    """Load catalogue from disk, or return default if missing or unreadable."""
    if home is None:
        home = os.environ.get("LOOM_HOME", ".")
    path = catalogue_file(home)
    if not path.is_file():
        return default_catalogue()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        cat = {}
        for k, v in raw.items():
            if isinstance(v, dict) and "id" in v:
                cat[k] = ModelEntry(**v)
        return cat or default_catalogue()
    except (OSError, ValueError):
        return default_catalogue()


def main(argv: Optional[List[str]] = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print("usage: python3 -m bus.catalogue <command> ...")
        return 1

    cmd = args[0]
    home = os.environ.get("LOOM_HOME", ".")

    if cmd == "discover":
        url = os.environ.get("LOOM_OPENCODE_GO_MODELS_URL", OPENCODE_GO_MODELS_URL)
        discovered = discover_models(url=url)
        if discovered:
            save_catalogue(discovered, catalogue_file(home))
            print("discovered %d models" % len(discovered))
        return 0

    if cmd == "select-model":
        tier = args[1] if len(args) > 1 else "medium"
        pinned = args[2] if len(args) > 2 and args[2] else None
        preferred = args[3] if len(args) > 3 and args[3] else None
        cat = load_catalogue(home)
        tracker = AllowanceTracker.load(allowance_file(home))
        entry = select_model(tier, cat, tracker, pinned_model=pinned, preferred_model=preferred)
        if entry:
            print(entry.id)
            return 0
        return 1

    if cmd == "models-for-tier":
        tier = args[1] if len(args) > 1 else "medium"
        preferred = args[2] if len(args) > 2 and args[2] else None
        cat = load_catalogue(home)
        candidates = tier_candidates(tier, cat, preferred_model=preferred)
        print(" ".join(candidates))
        return 0

    if cmd == "record-limit":
        if len(args) < 4:
            sys.exit("usage: python3 -m bus.catalogue record-limit <model> <window> <retry_after>")
        model, window, retry_after = args[1], args[2], args[3]
        tracker = AllowanceTracker.load(allowance_file(home))
        tracker.record_limit("opencode-go", model, window, retry_after)
        tracker.save(allowance_file(home))
        return 0

    if cmd == "record-call":
        if len(args) < 2:
            sys.exit("usage: python3 -m bus.catalogue record-call <model>")
        model = args[1]
        tracker = AllowanceTracker.load(allowance_file(home))
        tracker.record_call("opencode-go", model)
        tracker.save(allowance_file(home))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
