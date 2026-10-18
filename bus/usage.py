"""Collect locally available provider usage limits for the Usage view."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Optional

from . import labels, spend


def _record(provider: str, lanes: List[str], window: str, used_pct: Optional[float],
            remaining: Optional[float], unit: Optional[str], resets_at: Optional[str],
            source: str, now: float, note: Optional[str] = None) -> Dict[str, Any]:
    return {"provider": provider, "lanes": lanes, "window": window,
            "used_pct": used_pct, "remaining": remaining, "unit": unit,
            "resets_at": resets_at, "source": source,
            "as_of": datetime.fromtimestamp(now, timezone.utc).isoformat(), "note": note}


def _utc_epoch(value: Any, now: float, relative: bool = True) -> Optional[str]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(now + value if relative else value, timezone.utc).isoformat()
    if isinstance(value, str):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc).isoformat()
        except ValueError:
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
            except ValueError:
                try:
                    return parsedate_to_datetime(value).astimezone(timezone.utc).isoformat()
                except (TypeError, ValueError, OverflowError):
                    return None
    return None


def _codex_limits(item: Any, now: float) -> Dict[str, Dict[str, Any]]:
    payload = item.get("payload", item) if isinstance(item, dict) else {}
    # The current codex CLI puts rate_limits beside payload.info (token
    # counts); older ones put it inside info. Look in both.
    limits = None
    for holder in (payload, payload.get("info") if isinstance(payload, dict) else None):
        if isinstance(holder, dict) and isinstance(holder.get("rate_limits"), dict):
            limits = holder["rate_limits"]
            break
    if not isinstance(limits, dict):
        return {}
    found = {}
    for key, label in (("primary", "5-hour"), ("secondary", "weekly")):
        window = limits.get(key)
        if not isinstance(window, dict):
            continue
        used = window.get("used_percent")
        if isinstance(used, (int, float)) and not isinstance(used, bool):
            reset = window.get("resets_at")
            relative = reset is None
            if reset is None:
                reset = window.get("resets_in_seconds")
            if reset is None and isinstance(window.get("window_minutes"), (int, float)):
                reset = window["window_minutes"] * 60
            found[label] = {"used": float(used), "reset": _utc_epoch(reset, now, relative),
                            "minutes": _minutes(window.get("window_minutes")),
                            "observed": _observed(item)}
    return found


def _minutes(value: Any) -> Optional[float]:
    """A window length in minutes, or None when codex did not give one."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _observed(item: Any) -> Optional[str]:
    """When codex wrote the session line, in UTC: the reading's real age. The
    record's as_of is when the station collected it, so a reading from a
    codex that last ran days ago looked minutes old on the Usage view."""
    stamp = item.get("timestamp") if isinstance(item, dict) else None
    if not isinstance(stamp, str):
        return None
    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.astimezone(timezone.utc).isoformat() if moment.tzinfo else None


# The newest reading is at the end of the newest session file. Reading every
# session on every station pass parsed 4.4 GB (770 files) on the lane machine
# on 2026-10-14, so only the newest files are opened, and only their tails.
SESSION_FILES = 8
SESSION_TAIL = 512 * 1024


def _newest_sessions(sessions: Path) -> List[Path]:
    dated = []
    for path in sessions.glob("**/*.jsonl"):
        try:
            dated.append((path.stat().st_mtime, str(path), path))
        except OSError:
            continue
    dated.sort(reverse=True)
    return [path for _, _, path in dated[:SESSION_FILES]]


def _tail_lines(path: Path) -> List[str]:
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(max(0, size - SESSION_TAIL))
        lines = stream.read().decode("utf-8", "replace").splitlines()
    # A tail that starts mid-file starts mid-line.
    return lines[1:] if size > SESSION_TAIL else lines


def _read_codex_sessions(sessions: Path, now: float) -> Dict[str, Dict[str, Any]]:
    """The rate-limit windows of the newest line that carries any, looking at
    the newest SESSION_FILES files, newest first, and only their tails."""
    for path in _newest_sessions(sessions):
        try:
            lines = _tail_lines(path)
        except OSError:
            continue
        for line in reversed(lines):
            try:
                found = _codex_limits(json.loads(line), now)
            except ValueError:
                continue
            if found:
                return found
    return {}


def codex_records(lanes: List[str], sessions: Path, now: float) -> List[Dict[str, Any]]:
    if not sessions.is_dir():
        # The collector reads the session logs of the machine it runs on; a
        # machine where codex never ran has none, and says so.
        return [_record("codex", lanes, "all", None, None, None, None, "codex session logs", now,
                        "no codex session logs on this machine")]
    found = _read_codex_sessions(sessions, now)
    records = []
    for window, data in found.items():
        record = _record("codex", lanes, window, data["used"], None, "percent", data["reset"],
                         "codex session logs", now)
        record["window_minutes"] = data["minutes"]
        record["observed_at"] = data["observed"]
        records.append(record)
    return records or [
        _record("codex", lanes, "all", None, None, None, None, "codex session logs", now,
                "rate-limit fields not found")]


# No Anthropic API reads the remaining prepaid credit: the Admin API's usage
# and cost reports give spend, not the balance, and need an admin key. The
# balance is the console's figure, set by the operator.
BUDGET_ASK = ("budget is not configured: no Anthropic API reads the prepaid credit balance, "
              "so set LOOM_ANTHROPIC_BUDGET_USD to the console's balance and "
              "LOOM_ANTHROPIC_BUDGET_SINCE to the day it was read, in the lane machine's "
              "environment (lanes/mac/install.sh)")
OUT_OF_CREDIT = "out of credit"


def anthropic_records(home: str, lanes: List[str], env: Dict[str, str], now: float) -> List[Dict[str, Any]]:
    budget = spend.Budget.from_env(env)
    summary = spend.summarize(home, budget, now)
    if budget.usd is None:
        spent = "spent USD %.2f recorded" % summary.spent
        if lanes and all(OUT_OF_CREDIT in (_limit_hold(home, lane, now) or {}).get("reason", "")
                         for lane in lanes):
            # The provider itself refused every slot for low credit: the
            # credit is used up, whatever the budget would have said.
            return [_record("anthropic", lanes, "credit", 100.0, 0.0, "USD", None,
                            "lane heartbeats", now,
                            "every anthropic slot reports out of credit; %s; %s" % (spent, BUDGET_ASK))]
        return [_record("anthropic", lanes, "credit", None, None, "USD", None,
                        "bus/spend.py", now, "%s; %s" % (BUDGET_ASK, spent))]
    pct = min(100.0, max(0.0, summary.spent / budget.usd * 100)) if budget.usd else 100.0
    return [_record("anthropic", lanes, "credit", pct, summary.remaining, "USD",
                    _utc_epoch(budget.expires, now) if budget.expires else None,
                    "bus/spend.py", now, "floor USD %.2f" % budget.floor)]


# -- limit holds from the lane heartbeats ---------------------------------
# The runner writes each lane's heartbeat (state/usage/<lane>.json): a lane
# held at a limit says "limit: <why>; ... until <time>" with retry_at, the
# reset its CLI printed (lanes/agent_cli.sh classify_failure). That is the
# only usage state the claude and agy CLIs leave: neither writes a usage
# figure on disk. The last hold seen is kept beside the heartbeat, so the
# Usage view still says when a lane was last limited after the hold lifts.
HOLD_UNTIL = re.compile(r";\s*(?:lane waits|unavailable) until \S+")


def _beat(home: str, lane: str) -> Optional[Dict[str, Any]]:
    try:
        beat = json.loads((Path(home) / "state" / "usage" / (lane + ".json")).read_text())
    except (OSError, ValueError):
        return None
    return beat if isinstance(beat, dict) else None


def _epoch(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _limit_hold(home: str, lane: str, now: float) -> Optional[Dict[str, Any]]:
    """The lane's current limit hold, {"reason", "until", "at"}, or None."""
    beat = _beat(home, lane)
    if beat is None or beat.get("available") is not False:
        return None
    reason = beat.get("reason")
    if not isinstance(reason, str) or not reason.startswith("limit:"):
        return None
    until = _epoch(beat.get("retry_at"))
    if until is not None and until <= now:
        return None
    why = HOLD_UNTIL.sub("", reason[len("limit:"):]).strip() or "a provider limit"
    return {"reason": why[:300], "until": until, "at": _epoch(beat.get("at")) or now}


def _remember(home: str, lane: str, hold: Dict[str, Any]) -> Dict[str, Any]:
    """Keep the hold as the lane's last limit; the same hold keeps the time it
    was first seen."""
    path = Path(home) / "state" / "usage" / (lane + ".last-limit.json")
    last = _last_limit(home, lane)
    if last and last.get("until") == hold["until"] and last.get("reason") == hold["reason"]:
        return last
    row = {"lane": lane, "reason": hold["reason"], "until": hold["until"], "seen": hold["at"]}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(row) + "\n")
        os.replace(tmp, path)
    except OSError:
        pass
    return row


def _last_limit(home: str, lane: str) -> Optional[Dict[str, Any]]:
    try:
        row = json.loads((Path(home) / "state" / "usage" / (lane + ".last-limit.json")).read_text())
    except (OSError, ValueError):
        return None
    return row if isinstance(row, dict) and isinstance(row.get("reason"), str) else None


def _iso(at: Optional[float]) -> Optional[str]:
    return datetime.fromtimestamp(at, timezone.utc).isoformat() if at is not None else None


def limit_records(provider: str) -> Callable[[str, List[str], Dict[str, str], float], List[Dict[str, Any]]]:
    """A "limit" reading per lane: limited until the reset while the lane is
    held, else the last limit seen and when it lifted, else none seen."""
    def adapter(home: str, slots: List[str], _env: Dict[str, str], now: float) -> List[Dict[str, Any]]:
        records = []
        for slot in slots:
            hold = _limit_hold(home, slot, now)
            last = _remember(home, slot, hold) if hold else _last_limit(home, slot)
            if hold:
                note = "limited: %s" % hold["reason"]
            elif last:
                note = "last limit: %s" % last["reason"]
            else:
                note = "no limit seen"
            row = _record(provider, [slot], "limit", None, None, None,
                          _iso(_epoch(last.get("until"))) if last else None,
                          "lane heartbeat", now, note)
            row["observed_at"] = _iso(_epoch(last.get("seen"))) if last else None
            records.append(row)
        return records
    return adapter


def _remote_records(home: str, slots: List[str], _env: Dict[str, str], now: float) -> List[Dict[str, Any]]:
    """A remote lane (muse) reports only a heartbeat, which the station keeps
    in state/station/remote-status.json: no usage figure or limit reaches
    Loom, so the reading says so and how old the heartbeat is."""
    try:
        saved = json.loads((Path(home) / "state" / "station" / "remote-status.json").read_text())
    except (OSError, ValueError):
        saved = {}
    records = []
    for slot in slots:
        cached = saved.get(slot) if isinstance(saved, dict) else None
        at = _epoch(cached.get("heartbeat")) if isinstance(cached, dict) else None
        note = ("the remote lane reports no usage or limit to Loom; last heartbeat %s"
                % (_iso(at) if at is not None else "not seen"))
        row = _record(slot, [slot], "limit", None, None, None, None, "remote heartbeat", now, note)
        row["observed_at"] = _iso(at)
        records.append(row)
    return records


def _not_exposed(provider: str, lanes: List[str], now: float) -> List[Dict[str, Any]]:
    return [_record(provider, lanes, "all", None, None, None, None,
                    "not exposed", now, "not exposed yet")]


def _provider_groups(lane_names: List[str]) -> Dict[str, List[str]]:
    grouped: Dict[str, List[str]] = {}
    for lane in lane_names:
        if lane in labels.REMOTE_LANES:
            provider = lane
        elif lane.startswith("anthropic"):
            provider = "anthropic"
        elif lane.startswith("codex"):
            provider = "codex"
        elif lane.startswith("opencode-go"):
            provider = "opencode-go"
        elif lane.startswith("spark"):
            provider = "spark"
        elif lane.startswith("claude"):
            provider = "claude"
        else:
            provider = lane
        grouped.setdefault(provider, []).append(lane)
    return grouped


def _collect_provider(provider: str, slots: List[str], registry: Dict[str, Any],
                      home: str, env: Dict[str, str], now: float) -> List[Dict[str, Any]]:
    adapter = registry.get(provider)
    if adapter:
        return adapter(home, slots, env, now)
    return _not_exposed(provider, slots, now)


def this_host() -> str:
    """The machine collecting, as its short name: a reading says where it was
    read, since codex's figures come from that machine's session logs."""
    return platform.node().split(".")[0][:100] or "unknown"


def collect(home: str, env: Optional[Dict[str, str]] = None, now: Optional[float] = None,
            lane_names: Optional[List[str]] = None,
            sessions: Optional[Path] = None,
            adapters: Optional[Dict[str, Callable[[str, List[str], Dict[str, str], float], List[Dict[str, Any]]]]] = None,
            host: Optional[str] = None) -> List[Dict[str, Any]]:
    env = dict(os.environ if env is None else env)
    now = time.time() if now is None else now
    host = this_host() if host is None else host
    lane_names = labels.LANES if lane_names is None else lane_names
    grouped = _provider_groups(lane_names)
    records = []
    registry = {
        "anthropic": lambda _home, slots, settings, stamp: anthropic_records(_home, slots, settings, stamp),
        "codex": lambda _home, slots, settings, stamp: codex_records(
            slots, sessions or Path(settings.get("LOOM_CODEX_SESSIONS")
                                    or Path.home() / ".codex" / "sessions"), stamp),
        "opencode-go": _opencode_records,
        "spark": _spark_records,
        "claude": limit_records("claude"),
        "agy": limit_records("agy"),
    }
    registry.update({lane: _remote_records for lane in labels.REMOTE_LANES})
    registry.update(adapters or {})
    for provider, slots in grouped.items():
        try:
            records.extend(_collect_provider(provider, slots, registry, home, env, now))
        except Exception as err:
            records.append(_record(provider, slots, "all", None, None, None, None,
                                   "adapter", now, "adapter error: %s" % type(err).__name__))
    for record in records:
        record["holds"] = lane_holds(home, record.get("lanes") or [])
        record["host"] = host
    return records


def lane_holds(home: str, lanes: List[str]) -> Dict[str, str]:
    """Why each unavailable lane is held, from its own heartbeat file
    (state/usage/<lane>.json, written by lanes/run_lane.sh), for the
    dashboard's lane cards. Lanes that are available, or unreadable, are left
    out."""
    holds: Dict[str, str] = {}
    for lane in lanes:
        try:
            beat = json.loads((Path(home) / "state" / "usage" / (lane + ".json")).read_text())
        except (OSError, ValueError):
            continue
        reason = beat.get("reason") if isinstance(beat, dict) else None
        if isinstance(beat, dict) and beat.get("available") is False and isinstance(reason, str) and reason:
            holds[lane] = reason[:300]
    return holds


def _opencode_records(home: str, slots: List[str], _env: Dict[str, str], now: float) -> List[Dict[str, Any]]:
    p = Path(home) / "state" / "usage"
    samples = []
    for slot in slots:
        try:
            row = json.loads((p / (slot + ".provider-limit.json")).read_text())
            samples.append(row)
        except (OSError, ValueError):
            pass
    if not samples:
        return [_record("opencode-go", slots, "all", None, None, None, None,
                        "lane probe", now, "not exposed")]
    row = max(samples, key=lambda x: x.get("at", 0))
    return [_record("opencode-go", slots, "last 429", None, None, None,
                    _utc_epoch(row.get("retry_after"), now), "lane probe", now,
                    "limitName: %s" % row.get("limit_name", "unknown"))]


def _spark_records(home: str, slots: List[str], _env: Dict[str, str], now: float) -> List[Dict[str, Any]]:
    usage_dir = Path(home) / "state" / "usage"
    records = []
    for slot in slots:
        source = "state/usage/%s.limit" % slot
        try:
            fields = (usage_dir / (slot + ".limit")).read_text().split()
            until = float(fields[0])
            if until <= now:
                raise ValueError("hold has expired")
            reason = " ".join(fields[1:]) or None
            records.append(_record("spark", [slot], "thermal hold", None, None, "seconds",
                                   _utc_epoch(until, now, relative=False), source, now, reason))
        except (OSError, ValueError, IndexError):
            records.append(_record("spark", [slot], "thermal hold", None, None, None, None,
                                   source, now, "no active hold"))
    return records


def write(home: str, records: List[Dict[str, Any]]) -> str:
    path = os.path.join(home, "state", "usage", "limits.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as stream:
        json.dump(records, stream, indent=2)
        stream.write("\n")
    os.replace(tmp, path)
    return path


# -- the command line -----------------------------------------------------
# A reading older than this, or from a window that has reset since, is not
# current (dashboard/usage.py STALE_AFTER).
STALE_AFTER = 2 * 3600
# A lane heartbeat this fresh means the lane runners write here: this is the
# lane machine (bus/station.py HEARTBEAT_MAX_AGE).
HEARTBEAT_FRESH = 600
LIVE_PATH = "/api/usage"


def _parse(value: Any) -> Optional[float]:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.timestamp() if moment.tzinfo else None


def stale_reason(row: Dict[str, Any], now: float) -> Optional[str]:
    """Why a reading's figure cannot be taken as current, or None."""
    if row.get("used_pct") is None:
        return None
    reset = _parse(row.get("resets_at"))
    if reset is not None and reset <= now and row.get("window") not in ("credit",):
        return "its window reset after this reading"
    observed = _parse(row.get("observed_at")) or _parse(row.get("as_of"))
    if observed is not None and now - observed > STALE_AFTER:
        return "read %dh ago" % ((now - observed) // 3600)
    return None


def newest_heartbeat(home: str) -> Optional[float]:
    """The newest lane heartbeat time under home, or None."""
    newest = None
    for path in (Path(home) / "state" / "usage").glob("*.json"):
        if path.name.endswith((".provider-limit.json", ".last-limit.json")) or path.name == "limits.json":
            continue
        try:
            beat = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        at = _epoch(beat.get("at")) if isinstance(beat, dict) else None
        if at is not None and (newest is None or at > newest):
            newest = at
    return newest


def table(records: List[Dict[str, Any]], now: float) -> List[str]:
    lines = ["provider\tlanes\twindow\tused\tremaining\tresets_at\tas_of\thost\tnote"]
    for row in records:
        stale = stale_reason(row, now)
        used = "unknown" if row.get("used_pct") is None else "%.1f%%" % row["used_pct"]
        lines.append("%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s" % (
            row.get("provider"), ",".join(row.get("lanes") or []), row.get("window"),
            used + (" (stale: %s)" % stale if stale else ""),
            "unknown" if row.get("remaining") is None else row["remaining"],
            row.get("resets_at") or "unknown", row.get("observed_at") or row.get("as_of") or "unknown",
            row.get("host") or "unknown", row.get("note") or ""))
    return lines


def live_url(env: Dict[str, str]) -> str:
    """The dashboard's usage read, from LOOM_TRANSCRIPT_URL, or empty."""
    from .brain_memory import dashboard_url
    url = env.get("LOOM_TRANSCRIPT_URL", "").strip()
    return dashboard_url(url, LIVE_PATH) if url else ""


def _live_token(env: Dict[str, str]) -> str:
    """The read-only LOOM_HANDOFF_TOKEN a remote brain holds, else the upload
    token at LOOM_TRANSCRIPT_TOKEN_SSM, as the lane machine reads it."""
    token = env.get("LOOM_HANDOFF_TOKEN", "").strip()
    if token:
        return token
    ssm = env.get("LOOM_TRANSCRIPT_TOKEN_SSM", "").strip()
    if not ssm:
        return ""
    from . import station  # station imports this module
    return station.read_ssm(ssm, env.get("LOOM_AWS_REGION", "").strip() or "us-east-1")


def fetch_live(url: str, token: str) -> Dict[str, Any]:
    from .brain_memory import fetch_json
    return fetch_json(url, token, "usage")


def _live(env: Dict[str, str], now: float) -> int:
    url = live_url(env)
    if not url:
        print("the live usage read needs LOOM_TRANSCRIPT_URL (the dashboard) and "
              "LOOM_HANDOFF_TOKEN or LOOM_TRANSCRIPT_TOKEN_SSM", file=sys.stderr)
        return 1
    try:
        token = _live_token(env)
        if not token:
            print("the live usage read needs LOOM_HANDOFF_TOKEN or LOOM_TRANSCRIPT_TOKEN_SSM",
                  file=sys.stderr)
            return 1
        packet = fetch_live(url, token)
    # The fetch's errors never carry the URL or the token.
    except OSError as err:
        print("could not read the live usage snapshot: %s" % err, file=sys.stderr)
        return 1
    except subprocess.SubprocessError as err:
        print("could not read the dashboard token (%s)" % type(err).__name__, file=sys.stderr)
        return 1
    rows = [row for row in packet.get("readings") or [] if isinstance(row, dict)]
    received = _parse(packet.get("received_at"))
    if received is None:
        print("The dashboard holds no usage snapshot yet: the station has not uploaded one "
              "since the dashboard started.")
        return 1
    age = now - received
    print("Live snapshot the station uploaded at %s (%dm ago)%s." % (
        packet["received_at"], age // 60,
        "; over %dh old, so the station has stopped uploading" % (STALE_AFTER // 3600)
        if age > STALE_AFTER else ""))
    print("\n".join(table(rows, now)))
    return 0


def main(argv: Optional[List[str]] = None, env: Optional[Dict[str, str]] = None,
         now: Optional[float] = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m bus.usage")
    parser.add_argument("--live", action="store_true",
                        help="print the station's live snapshot from the dashboard (GET /api/usage)")
    args = parser.parse_args(argv)
    env = dict(os.environ if env is None else env)
    now = time.time() if now is None else now
    if args.live:
        return _live(env, now)
    home = env.get("LOOM_HOME") or os.path.join(os.path.expanduser("~"), "loom")
    newest = newest_heartbeat(home)
    if newest is None or now - newest > HEARTBEAT_FRESH:
        # Off the lane machine every file here is a leftover nothing
        # refreshes: printing it as current misled a remote brain (loom#813).
        print("No lane on this machine wrote a heartbeat in the last %d minutes (newest: %s), so "
              "this is not the lane machine and its usage files are not current. Read the live "
              "snapshot with `python3 -m bus.usage --live` or the dashboard's /usage page."
              % (HEARTBEAT_FRESH // 60, _iso(newest) or "none"))
        return 1
    records = collect(home, env=env, now=now)
    write(home, records)
    print("\n".join(table(records, now)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
