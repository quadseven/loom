"""What the anthropic lanes spend, from the claude CLI's own JSON result.

A print-mode run with --output-format json ends with a result object carrying
total_cost_usd and token usage. The lane runner hands each anthropic run's
output to `record`, which prints the human-readable result for the lane log
and appends one line to state/usage/spend.jsonl. A run with no result is
recorded with cost null (unknown), never zero. The station and the runner's
floor check read the file back through `summarize` (docs/LANES.md, "Anthropic
spend").

    python3 -m bus.spend record --output FILE --slot S --ticket T --model M
    python3 -m bus.spend floor     exit 1 with the reason when under the floor
"""
from __future__ import annotations

import argparse
import calendar
import json
import math
import os
import sys
import time
from typing import Any, Dict, List, Mapping, Optional

TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens",
              "cache_read_input_tokens")
DAY = 86400
DEFAULT_FLOOR = 5.0


def spend_path(home: str) -> str:
    return os.path.join(home, "state", "usage", "spend.jsonl")


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and value >= 0 else None


def find_result(text: str) -> Optional[Dict[str, Any]]:
    """The CLI's result object: the last line that is a JSON object of type
    result (stderr shares the stream, so other lines may surround it)."""
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "result":
            return obj
    return None


def entry(result: Optional[Dict[str, Any]], slot: str, ticket: str, model: str,
          now: float) -> Dict[str, Any]:
    usage = result.get("usage") if result else None
    usage = usage if isinstance(usage, dict) else {}
    line: Dict[str, Any] = {
        "at": int(now), "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "slot": slot, "ticket": ticket, "model": model,
        "cost_usd": _number(result.get("total_cost_usd")) if result else None,
    }
    for key in TOKEN_KEYS:
        n = _number(usage.get(key))
        line[key] = int(n) if n is not None else None
    return line


def human_text(text: str, result: Optional[Dict[str, Any]]) -> str:
    """The output with the result object replaced by its result text."""
    if result is None:
        return text
    out = []
    replaced = False
    for line in reversed(text.splitlines()):
        if not replaced and line.strip().startswith("{") and find_result(line) is not None:
            msg = result.get("result")
            out.append(msg if isinstance(msg, str) else "(no result text)")
            replaced = True
        else:
            out.append(line)
    return "\n".join(reversed(out)) + "\n"


def record(home: str, output: str, slot: str, ticket: str, model: str,
           now: Optional[float] = None) -> Dict[str, Any]:
    try:
        with open(output, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except FileNotFoundError:
        text = ""
    result = find_result(text)
    sys.stdout.write(human_text(text, result))
    sys.stdout.flush()
    line = entry(result, slot, ticket, model, time.time() if now is None else now)
    path = spend_path(home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")
    return line


def load(home: str) -> List[Dict[str, Any]]:
    """Every readable anthropic line; a torn or foreign line is skipped."""
    rows = []
    try:
        with open(spend_path(home), encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                try:
                    row = json.loads(raw)
                except ValueError:
                    continue
                if (isinstance(row, dict) and _number(row.get("at")) is not None
                        and str(row.get("slot", "")).startswith("anthropic")):
                    rows.append(row)
    except FileNotFoundError:
        pass
    return rows


def parse_when(value: str) -> float:
    """YYYY-MM-DD (00:00 UTC that day) or YYYY-MM-DDTHH:MM[:SS]Z, as epoch."""
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%MZ"):
        try:
            return float(calendar.timegm(time.strptime(value, fmt)))
        except ValueError:
            continue
    raise ValueError("not a date (YYYY-MM-DD or YYYY-MM-DDTHH:MMZ): %r" % value)


class Budget:
    """LOOM_ANTHROPIC_BUDGET_USD, _BUDGET_SINCE, _BUDGET_EXPIRES, _FLOOR_USD."""

    def __init__(self, usd: Optional[float] = None, since: Optional[float] = None,
                 expires: Optional[float] = None, floor: float = DEFAULT_FLOOR) -> None:
        self.usd = usd
        self.since = since
        self.expires = expires
        self.floor = floor

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Budget":
        """Raises ValueError naming the setting that does not parse."""
        def money(name: str, default: Optional[float]) -> Optional[float]:
            raw = env.get(name, "").strip()
            if not raw:
                return default
            try:
                value = _number(float(raw))
            except ValueError:
                value = None
            if value is None:
                raise ValueError("%s is not a dollar amount: %r" % (name, raw))
            return value

        def when(name: str) -> Optional[float]:
            raw = env.get(name, "").strip()
            if not raw:
                return None
            try:
                return parse_when(raw)
            except ValueError as err:
                raise ValueError("%s: %s" % (name, err)) from None
        floor = money("LOOM_ANTHROPIC_FLOOR_USD", DEFAULT_FLOOR)
        return cls(money("LOOM_ANTHROPIC_BUDGET_USD", None),
                   when("LOOM_ANTHROPIC_BUDGET_SINCE"), when("LOOM_ANTHROPIC_BUDGET_EXPIRES"),
                   DEFAULT_FLOOR if floor is None else floor)


class Summary:
    def __init__(self, rows: List[Dict[str, Any]], budget: Budget, now: float) -> None:
        self.budget = budget
        day_start = now - now % DAY
        since = budget.since if budget.since is not None else float("-inf")
        counted = [r for r in rows if r["at"] >= since]
        self.runs = len(counted)
        self.unknown = sum(1 for r in counted if _number(r.get("cost_usd")) is None)
        self.spent = sum(_number(r.get("cost_usd")) or 0.0 for r in counted)
        self.today = sum(_number(r.get("cost_usd")) or 0.0 for r in rows
                         if day_start <= r["at"] <= now)
        self.burn = sum(_number(r.get("cost_usd")) or 0.0 for r in rows
                        if now - DAY <= r["at"] <= now)
        self.remaining = budget.usd - self.spent if budget.usd is not None else None
        self.projected = None
        if self.remaining is not None and budget.expires is not None and budget.expires > now:
            self.projected = self.remaining - self.burn * (budget.expires - now) / DAY

    def under_floor(self) -> bool:
        return self.remaining is not None and self.remaining < self.budget.floor

    def floor_reason(self) -> str:
        return ("anthropic credit remaining $%.2f is under the floor of $%.2f "
                "(LOOM_ANTHROPIC_FLOOR_USD)" % (self.remaining or 0.0, self.budget.floor))

    def line(self) -> str:
        b = self.budget
        since = (" since " + time.strftime("%Y-%m-%d", time.gmtime(b.since))
                 if b.since is not None else " in all")
        parts = ["today $%.2f" % self.today, "spent%s $%.2f" % (since, self.spent)]
        if self.remaining is not None:
            parts.append("remaining $%.2f of $%.2f" % (self.remaining, b.usd))
            parts.append("burn $%.2f/day (last 24h)" % self.burn)
            if self.projected is not None:
                parts.append("projected at expiry %s $%.2f" % (
                    time.strftime("%Y-%m-%d", time.gmtime(b.expires)), self.projected))
        if self.unknown:
            parts.append("%d run%s of unknown cost not counted"
                         % (self.unknown, "" if self.unknown == 1 else "s"))
        text = "Anthropic spend: " + "; ".join(parts)
        if self.under_floor():
            text += ". Under the $%.2f floor: the anthropic slots take no work." % b.floor
        return text


def summarize(home: str, budget: Budget, now: Optional[float] = None) -> Summary:
    return Summary(load(home), budget, time.time() if now is None else now)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m bus.spend")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("record", help="print a run's result and append its cost")
    s.add_argument("--output", required=True)
    s.add_argument("--slot", required=True)
    s.add_argument("--ticket", required=True)
    s.add_argument("--model", required=True)
    sub.add_parser("floor", help="exit 1 with the reason when credit is under the floor")
    args = p.parse_args(argv)
    home = os.environ.get("LOOM_HOME") or os.path.join(os.path.expanduser("~"), "loom")
    if args.command == "record":
        record(home, args.output, args.slot, args.ticket, args.model)
        return 0
    try:
        budget = Budget.from_env(os.environ)
    except ValueError as err:
        print(err)
        return 1
    summary = summarize(home, budget)
    if summary.under_floor():
        print(summary.floor_reason())
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
