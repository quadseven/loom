"""Workers AI request sizing for the station, lane and on-demand coding check."""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request
from typing import TextIO

from . import meter
from .state_store import FileStore, StateError

CODE_TOKENS = 65536
VERIFY_TOKENS = 8192
VERIFY_OUTPUT = 1024
VERIFY_INSTRUCTION = "Return only the JSON object requested for verify.json.\n\n"
STORE = FileStore()
RATE_REFUSAL = re.compile(r"too many requests|(?:http|status(?:code)?|code)\W{0,4}429\b", re.I)


def usage_file(home: str | Path, name: str) -> str:
    return str(Path(home) / "state" / "usage" / name)


def code_ready(home: str | Path, model: str | None) -> bool:
    check = STORE.read_json(usage_file(home, "workersai-code.json"), {}, strict=False)
    if not isinstance(check, dict) or check.get("model") != model:
        return False
    count = check.get("prompt_tokens")
    checked = check.get("at")
    try:
        refused = STORE.read_json(usage_file(home, "workersai.code-refused.json"), 0)
    except StateError:
        return False
    return (type(count) is int and count >= CODE_TOKENS
            and isinstance(checked, (float, int)) and isinstance(refused, (float, int))
            and checked > refused)


def daily_pct(home: str | Path, now: float) -> float | None:
    record = STORE.read_json(usage_file(home, "workersai.meter.json"), {}, strict=False)
    if not isinstance(record, dict) or record.get("lane") != "workersai" or record.get("error"):
        return None
    at = record.get("at")
    if not isinstance(at, (int, float)) or not 0 <= now - at < meter.METER_EVERY:
        return None
    if int(at // 86400) != int(now // 86400):
        return None
    windows = record.get("windows")
    if not isinstance(windows, list):
        return None
    for window in windows:
        if isinstance(window, dict) and (window.get("window"), window.get("unit")) == ("daily", "neurons"):
            pct = window.get("used_pct")
            if type(pct) in (float, int) and 0 <= pct <= 100:
                return pct
    return None


def short_verify(brief: str) -> bool:
    # One token per UTF-8 byte is a conservative input bound. Reserve space
    # for the chat template and the entire reply as well.
    return len((VERIFY_INSTRUCTION + brief).encode()) + 128 + VERIFY_OUTPUT <= VERIFY_TOKENS


def first_refusal(home: str, events: str, output: str) -> bool:
    pct = daily_pct(home, time.time())
    if pct is None or pct >= 50 or not RATE_REFUSAL.search(output):
        return False
    with open(events, encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("type") in (
                    "text", "reasoning", "tool", "tool_use", "step_finish"):
                return False
    return True


def completion(model: str, prompt: str, max_tokens: int, stdin: TextIO) -> tuple[str, dict]:
    key, account = stdin.readline().strip(), stdin.readline().strip()
    if not key or not re.fullmatch(r"[A-Za-z0-9]+", account):
        raise ValueError("missing provider settings")
    if not re.fullmatch(r"cloudflare-workers-ai/@cf/[A-Za-z0-9._/-]+", model):
        raise ValueError("invalid provider model")
    payload = {"model": model.split("/", 1)[1], "max_tokens": max_tokens,
               "messages": [{"role": "user", "content": prompt}]}
    url = "https://api.cloudflare.com/client/v4/accounts/%s/ai/v1/chat/completions" % account
    req = meter._request(url, key, json.dumps(payload).encode())
    with urllib.request.urlopen(req, timeout=60) as reply:
        result = json.load(reply)
    content = result["choices"][0]["message"]["content"]
    if not isinstance(content, str) or not content.strip():
        raise ValueError("empty answer")
    return content, result.get("usage") or {}


def main(args: list[str] | None = None, stdin: TextIO | None = None) -> int:
    args = sys.argv[1:] if args is None else args
    stream = sys.stdin if stdin is None else stdin
    try:
        if len(args) == 4 and args[0] == "refused":
            _, home, events, job = args
            if not first_refusal(home, events, stream.read()):
                return 1
            if job != "verify":
                STORE.write_json(usage_file(home, "workersai.code-refused.json"), time.time())
            return 0
        if len(args) == 3 and args[0] == "check":
            _, home, model = args
            started = time.time()
            path = usage_file(home, "workersai-code.json")
            Path(path).unlink(missing_ok=True)
            prompt = "Read this sample and reply OK.\n" + " pass" * CODE_TOKENS
            _, usage = completion(model, prompt, 16, stream)
            count = usage.get("prompt_tokens")
            if type(count) is not int or count < CODE_TOKENS:
                return 1
            STORE.write_json(path, {"model": model, "at": started, "prompt_tokens": count})
            return 0
        if len(args) == 5 and args[0] == "verify":
            _, brief, model, out, home = args
            pct = daily_pct(home, time.time())
            if pct is None:
                print("daily meter unavailable", file=sys.stderr)
                return 1
            if pct >= 100:
                print("daily quota reached", file=sys.stderr)
                return 1
            text = Path(brief).read_text(encoding="utf-8")
            if not short_verify(text):
                return 1
            content, _ = completion(model, VERIFY_INSTRUCTION + text, VERIFY_OUTPUT, stream)
            record = json.loads(content)
            if not isinstance(record, dict):
                return 1
            Path(out).write_text(json.dumps(record) + "\n", encoding="utf-8")
            return 0
    except urllib.error.HTTPError as err:
        print("provider answered HTTP %d" % err.code, file=sys.stderr)
        return 1
    except (OSError, ValueError, IndexError, KeyError, TypeError, AttributeError) as err:
        print("Workers AI request check failed (%s)" % type(err).__name__, file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
