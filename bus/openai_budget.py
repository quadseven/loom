"""What the OpenAI API lane and decision helper spend, and the monthly cap.

The operator's OpenAI API credit is 100 USD a calendar month (loom#3188).
The openai-mini lane and the decision helper (bus/openai_helper.py) both
spend it, so both price every call here: the cost comes from the response's
own token usage and the pinned price table below, never from a provider
meter, and each call is one line of state/usage/openai.jsonl. Before a call
both ask `refusal`, which refuses once the calendar month (UTC) has reached
the cap: CAP_USD, 90 USD, or a lower LOOM_OPENAI_CAP_USD. The last 10 USD is
headroom for what one lane run can spend after its start was allowed. The
OpenAI project's own monthly budget, set by the operator, is the backstop.

A call whose usage cannot be read is charged UNKNOWN_CALL_USD, never zero,
so a change in the usage shape cannot let calls run uncounted.

    python3 -m bus.openai_budget check [--model M]   exit 1 with the reason when refused
    python3 -m bus.openai_budget record-events --events FILE --model M --slot S --ticket T
    python3 -m bus.openai_budget month               the month's spend line
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from typing import Any, Dict, List, Mapping, NamedTuple, Optional

# Standard tier, USD per million tokens, from the official pricing page on
# the day named. A model missing here is refused: an unpriced call cannot be
# held under the cap.
PRICES_SOURCE = "https://developers.openai.com/api/docs/pricing"
PRICES_READ = "2026-11-17"
# A prompt over this many input tokens is billed at the long-context rates.
LONG_CONTEXT_OVER = 272_000


class Price(NamedTuple):
    input: float
    cached: float
    output: float
    long_input: float
    long_cached: float
    long_output: float


PRICES: Dict[str, Price] = {
    # The smallest current model: "most efficient model for focused,
    # high-volume tasks". The lane and the helper both run it.
    "gpt-6-luna": Price(input=0.10, cached=0.01, output=0.50,
                        long_input=0.20, long_cached=0.02, long_output=0.75),
}
LANE_MODEL = "openai/gpt-6-luna"
HELPER_MODEL = "gpt-6-luna"
# The hard ceiling: LOOM_OPENAI_CAP_USD may lower it, never raise it.
CAP_USD = 90.0
CREDIT_USD = 100.0
UNKNOWN_CALL_USD = 0.25
LEDGER = "openai.jsonl"


class Unpriced(ValueError):
    """The model has no pinned price."""


class UsageUnknown(ValueError):
    """The response carried no readable token usage."""


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UsageUnknown("token count is not a number: %r" % (value,))
    if not math.isfinite(value) or value < 0:
        raise UsageUnknown("token count out of range: %r" % (value,))
    return int(value)


def _optional(value: Any) -> int:
    return 0 if value is None else _count(value)


class Usage(NamedTuple):
    """Tokens of one call: uncached input, cached input, output (reasoning
    included)."""
    input: int
    cached: int
    output: int

    @classmethod
    def from_openai(cls, usage: Any) -> "Usage":
        """The usage object of a Chat Completions or Responses answer."""
        if not isinstance(usage, dict):
            raise UsageUnknown("no usage object")
        if "prompt_tokens" in usage or "completion_tokens" in usage:
            prompt, out = usage.get("prompt_tokens"), usage.get("completion_tokens")
            details = usage.get("prompt_tokens_details")
        elif "input_tokens" in usage or "output_tokens" in usage:
            prompt, out = usage.get("input_tokens"), usage.get("output_tokens")
            details = usage.get("input_tokens_details")
        else:
            raise UsageUnknown("usage has no token counts")
        prompt, out = _count(prompt), _count(out)
        cached = _optional(details.get("cached_tokens")) if isinstance(details, dict) else 0
        cached = min(cached, prompt)
        return cls(prompt - cached, cached, out)

    @classmethod
    def from_opencode(cls, tokens: Any) -> "Usage":
        """An opencode step_finish event's part.tokens. A cache write is
        charged as input and reasoning as output, so the sum is never under
        the bill."""
        if not isinstance(tokens, dict) or not any(k in tokens for k in ("input", "output")):
            raise UsageUnknown("step has no token counts")
        cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
        return cls(_optional(tokens.get("input")) + _optional(cache.get("write")),
                   _optional(cache.get("read")),
                   _optional(tokens.get("output")) + _optional(tokens.get("reasoning")))


def model_id(model: str) -> str:
    """The price table's name for a model: the lane's openai/ prefix dropped."""
    return model[len("openai/"):] if model.startswith("openai/") else model


def price_of(model: str, prices: Optional[Mapping[str, Price]] = None) -> Price:
    table = PRICES if prices is None else prices
    found = table.get(model_id(model))
    if found is None:
        raise Unpriced("no pinned price for %s (bus/openai_budget.py PRICES)" % model)
    return found


def cost(model: str, usage: Usage, prices: Optional[Mapping[str, Price]] = None) -> float:
    p = price_of(model, prices)
    if usage.input + usage.cached > LONG_CONTEXT_OVER:
        rates = (p.long_input, p.long_cached, p.long_output)
    else:
        rates = (p.input, p.cached, p.output)
    return (usage.input * rates[0] + usage.cached * rates[1] + usage.output * rates[2]) / 1e6


def month_of(now: float) -> str:
    return time.strftime("%Y-%m", time.gmtime(now))


def ledger_path(home: str) -> str:
    return os.path.join(home, "state", "usage", LEDGER)


def _append(home: str, line: Dict[str, Any]) -> None:
    path = ledger_path(home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = (json.dumps(line, sort_keys=True) + "\n").encode("utf-8")
    # One O_APPEND write per line: concurrent writers do not interleave.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def _line(source: str, model: str, now: float, slot: str, ticket: str) -> Dict[str, Any]:
    return {"at": int(now), "month": month_of(now), "source": source, "model": model,
            "slot": slot, "ticket": ticket}


def record(home: str, source: str, model: str, usage: Usage, now: float, slot: str = "",
           ticket: str = "", prices: Optional[Mapping[str, Price]] = None) -> Dict[str, Any]:
    """Price one call and append it to the ledger; returns the line."""
    line = _line(source, model, now, slot, ticket)
    line.update(input_tokens=usage.input, cached_input_tokens=usage.cached,
                output_tokens=usage.output, cost_usd=round(cost(model, usage, prices), 6),
                estimated=False)
    _append(home, line)
    return line


def record_unknown(home: str, source: str, model: str, now: float, slot: str = "",
                   ticket: str = "") -> Dict[str, Any]:
    """A call whose usage could not be read, charged UNKNOWN_CALL_USD."""
    line = _line(source, model, now, slot, ticket)
    line.update(input_tokens=None, cached_input_tokens=None, output_tokens=None,
                cost_usd=UNKNOWN_CALL_USD, estimated=True)
    _append(home, line)
    return line


def record_events(home: str, events: str, model: str, slot: str, ticket: str,
                  now: float) -> List[Dict[str, Any]]:
    """One ledger line per step_finish event of an opencode JSON event file."""
    lines: List[Dict[str, Any]] = []
    try:
        handle = open(events, encoding="utf-8", errors="replace")
    except OSError:
        return lines
    with handle:
        for raw in handle:
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(event, dict) or event.get("type") != "step_finish":
                continue
            part = event.get("part") if isinstance(event.get("part"), dict) else {}
            try:
                usage = Usage.from_opencode(part.get("tokens"))
            except UsageUnknown:
                lines.append(record_unknown(home, "lane", model, now, slot, ticket))
                continue
            lines.append(record(home, "lane", model, usage, now, slot, ticket))
    return lines


class MonthSpend(NamedTuple):
    month: str
    usd: float
    calls: int

    def line(self, cap: float) -> str:
        return "OpenAI spend %s: $%.2f of the $%.2f cap, %d call%s" % (
            self.month, self.usd, cap, self.calls, "" if self.calls == 1 else "s")


def month_spend(home: str, now: float) -> MonthSpend:
    """The ledger's total for the calendar month (UTC) holding `now`. A line
    that does not parse, or has no numeric cost, is skipped."""
    month, usd, calls = month_of(now), 0.0, 0
    try:
        handle = open(ledger_path(home), encoding="utf-8", errors="replace")
    except OSError:
        return MonthSpend(month, 0.0, 0)
    with handle:
        for raw in handle:
            try:
                line = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(line, dict) or line.get("month") != month:
                continue
            value = line.get("cost_usd")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                continue
            usd += max(0.0, float(value))
            calls += 1
    return MonthSpend(month, round(usd, 6), calls)


def cap_usd(env: Mapping[str, str]) -> float:
    """CAP_USD, or a lower LOOM_OPENAI_CAP_USD. ValueError when it does not parse."""
    raw = str(env.get("LOOM_OPENAI_CAP_USD", "")).strip()
    if not raw:
        return CAP_USD
    try:
        value = float(raw)
    except ValueError:
        value = float("nan")
    if not math.isfinite(value) or value < 0:
        raise ValueError("LOOM_OPENAI_CAP_USD is not a dollar amount: %r" % raw)
    return min(value, CAP_USD)


def refusal(home: str, env: Mapping[str, str], now: float, model: str,
            prices: Optional[Mapping[str, Price]] = None,
            worst_usd: float = 0.0) -> Optional[str]:
    """Why a call must not go out now, or None when it may. `worst_usd` is the
    most the call itself can cost, when the caller can bound it."""
    try:
        price_of(model, prices)
    except Unpriced as err:
        return str(err)
    try:
        cap = cap_usd(env)
    except ValueError as err:
        return "%s; no OpenAI call goes out" % err
    spent = month_spend(home, now)
    if round(spent.usd + worst_usd, 6) >= cap:
        return ("OpenAI spend cap reached: $%.2f spent in %s%s, cap $%.2f; "
                "calls resume next month" % (
                    spent.usd, spent.month,
                    " plus up to $%.2f for this call" % worst_usd if worst_usd else "", cap))
    return None


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m bus.openai_budget")
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("check", help="exit 1 with the reason when a call is refused")
    c.add_argument("--model", default=LANE_MODEL)
    r = sub.add_parser("record-events", help="record each step of an opencode event file")
    r.add_argument("--events", required=True)
    r.add_argument("--model", required=True)
    r.add_argument("--slot", required=True)
    r.add_argument("--ticket", required=True)
    sub.add_parser("month", help="print the month's spend line")
    args = p.parse_args(argv)
    home = os.environ.get("LOOM_HOME") or os.path.join(os.path.expanduser("~"), "loom")
    now = time.time()
    if args.command == "check":
        why = refusal(home, os.environ, now, args.model)
        if why:
            print(why)
            return 1
        return 0
    if args.command == "record-events":
        try:
            lines = record_events(home, args.events, args.model, args.slot, args.ticket, now)
        except Unpriced as err:
            print(err)
            return 1
        print("recorded %d OpenAI call%s, $%.4f" % (
            len(lines), "" if len(lines) == 1 else "s", sum(x["cost_usd"] for x in lines)))
        return 0
    try:
        cap = cap_usd(os.environ)
    except ValueError as err:
        print(err)
        return 1
    print(month_spend(home, now).line(cap))
    return 0


if __name__ == "__main__":
    sys.exit(main())
