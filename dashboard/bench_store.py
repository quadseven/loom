"""The dashboard's bench list (loom#969).

One BenchStore lives on the ticket store. Each background pass, after the
Tiers sources are read, it runs bus.bench.evaluate over the gateway's 24 hour
stats and keeps the result: the pairs benched, on trial, and the recent
events. The Tiers page shows it and GET /api/bench publishes it, so Loom's
station, Grug and the gateway read one list.

The state is kept in memory and, when LOOM_BENCH_STATE_FILE is set, in that
file (written whole, then renamed), so a restart keeps the doubled cooldowns.
Without the file a restart starts empty and the next pass benches again from
the window's failures.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict, Mapping, Optional

from bus import bench

LOG = logging.getLogger(__name__)
GATEWAY_WINDOW = "gateway_24h"


class BenchStore:
    def __init__(self, path: str = "", config: Optional[bench.Config] = None) -> None:
        self.path = path
        self.config = config or bench.Config()
        self._lock = threading.Lock()
        self.state: Dict[str, Any] = bench.empty_state()
        self._load()

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "BenchStore":
        env = os.environ if env is None else env
        return cls(env.get("LOOM_BENCH_STATE_FILE", "").strip(), bench.Config.from_env(env))

    def _load(self) -> None:
        if not self.path:
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                value = json.load(fh)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            LOG.warning("bench state %s not read: %s", self.path, type(exc).__name__)
            return
        if isinstance(value, dict):
            self.state = value

    def _save(self) -> None:
        if not self.path:
            return
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.state, fh)
            os.replace(tmp, self.path)
        except OSError as exc:
            LOG.warning("bench state %s not written: %s", self.path, type(exc).__name__)

    def update(self, sources: Mapping[str, Any], now: Optional[float] = None) -> list:
        """Run one pass over the gateway's 24 hour stats. A gateway that is
        not configured, has not answered or is stale gives no rows: then only
        the clock moves a benched pair to its trial. Returns the events."""
        now = time.time() if now is None else now
        source = sources.get(GATEWAY_WINDOW)
        rows = None
        data = getattr(source, "data", None)
        if source is not None and not getattr(source, "stale", True) and isinstance(data, dict) \
                and isinstance(data.get("rows"), list):
            rows = data["rows"]
        with self._lock:
            before = self.state
            try:
                self.state, events = bench.evaluate(rows, self.state, now, self.config)
            # Broad on purpose: this runs on the refresh thread, and a state
            # file with the wrong shape must not end the pass.
            except Exception as exc:  # noqa: BLE001
                LOG.warning("bench pass failed: %s; starting the bench list again", type(exc).__name__)
                self.state, events = bench.empty_state(), []
            if self.state != before:
                self._save()
        for event in events:
            LOG.info("bench %s: %s %s (%s)", event["kind"], event["provider"], event["model"], event["reason"])
        return events

    def snapshot(self, now: Optional[float] = None) -> Dict[str, Any]:
        with self._lock:
            return bench.snapshot(self.state, time.time() if now is None else now)
