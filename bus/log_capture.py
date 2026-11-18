"""Keep a local, timestamped copy of the log of one pod per source."""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import tempfile
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from itertools import chain
from pathlib import Path
from typing import TextIO

NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
STAMP_RE = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})"
                      r"(?:\.([0-9]{1,9}))?(Z|[+-][0-9]{2}:[0-9]{2})")
GAP = "loom-log-gap:"
DEFAULT_RETENTION_HOURS = 48


@dataclass(frozen=True)
class Source:
    name: str
    namespace: str
    selector: str


@dataclass(frozen=True)
class Read:
    lines: list[str]
    covered_from: float
    through: float
    dropped: int


def sources(raw: str) -> list[Source]:
    """Parse the station's optional source list, with unique file names."""
    entries = json.loads(raw or "[]")
    if not isinstance(entries, list):
        raise ValueError("expected a list of log sources")
    found = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("invalid log source")
        name, namespace, selector = (entry.get(key) for key in ("source", "namespace", "selector"))
        if not all(isinstance(value, str) and value.strip() for value in (name, namespace, selector)):
            raise ValueError("a log source needs source, namespace and selector strings")
        if not NAME_RE.fullmatch(name) or name in found:
            raise ValueError("invalid or duplicate log source name")
        found[name] = Source(name, namespace, selector)
    return list(found.values())


def capture_path(home: str, name: str) -> Path:
    if not NAME_RE.fullmatch(name):
        raise ValueError("invalid log source name")
    path = Path(home) / "logs" / "capture" / (name + ".log")
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("log capture is a symlink")
    return path


def _stamp_key(stamp: str) -> tuple[float, str]:
    parts = STAMP_RE.fullmatch(stamp)
    if not parts:
        raise ValueError("expected a pod log timestamp")
    second, fraction, zone = parts.groups()
    if zone == "Z":
        zone = "+00:00"
    return datetime.fromisoformat(second + zone).timestamp(), (fraction or "").ljust(9, "0")


def _time(stamp: str) -> float:
    second, fraction = _stamp_key(stamp)
    return second + int(fraction) / 1_000_000_000


def _pod(prefix: list[str], source: Source) -> tuple[str, str, list[str]]:
    result = subprocess.run(
        prefix + ["get", "pods", "-l", source.selector, "--field-selector=status.phase=Running",
                  "-o", "json"], check=True, capture_output=True, text=True, timeout=15)
    pods = json.loads(result.stdout)["items"]
    if not pods:
        raise ValueError("no running pod matches")
    pod = max(pods, key=lambda item: (item["metadata"].get("creationTimestamp", ""),
                                      item["metadata"]["name"]))
    meta = pod["metadata"]
    container = (meta.get("annotations") or {}).get("kubectl.kubernetes.io/default-container")
    container = container or pod["spec"]["containers"][0]["name"]
    restarts = next((item.get("restartCount", 0)
                     for item in pod.get("status", {}).get("containerStatuses", [])
                     if item["name"] == container), 0)
    identity = [source.namespace, source.selector, meta["uid"], container, str(restarts)]
    return meta["name"], container, identity


def _header(log: TextIO) -> tuple[list[str], float]:
    identity, through = json.loads(log.readline())
    if not isinstance(identity, list) or not isinstance(through, (int, float)) or not math.isfinite(through):
        raise ValueError("invalid log capture header")
    return identity, through


def read(path: Path, literal: str, since: float, limit: int | None = None) -> Read:
    """Search retained lines and report the continuous tail of the capture."""
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("log capture is a symlink")
    oldest, gap_end, dropped = math.inf, -math.inf, 0
    lines = []
    with path.open(encoding="utf-8") as log:
        _, through = _header(log)
        for line in log:
            stamp = line.split(maxsplit=1)[0]
            if stamp == GAP:
                _, before, after = line.split()
                end = max(_time(before), _time(after))
                if _stamp_key(before) == _stamp_key(after):
                    end = math.nextafter(end, math.inf)
                gap_end = max(gap_end, end)
                continue
            at = _time(stamp)
            oldest = min(oldest, at)
            if since <= at <= through and literal in line:
                if limit is None or len(lines) < limit:
                    lines.append(line.rstrip("\n"))
                else:
                    dropped += 1
    return Read(lines, max(oldest, gap_end), through, dropped)


def _retained(old: TextIO, output: TextIO, cutoff: float) -> tuple[str, int, str | None]:
    last, count, first = "", 0, None
    oldest = math.inf
    for line in old:
        stamp = line.split(maxsplit=1)[0]
        if stamp == GAP:
            _, before, after = line.split()
            if max(_time(before), _time(after)) >= cutoff:
                output.write(line)
            last, count = "", 0
            continue
        count = count + 1 if stamp == last.partition(" ")[0] else 1
        last = line
        at = _time(stamp)
        if at >= cutoff:
            output.write(line)
            if at < oldest:
                first, oldest = stamp, at
    return last, count, first


def _overlap(log: TextIO, last: str, count: int) -> bool:
    """Skip the inclusive cursor, including multiple records at its timestamp."""
    cursor = _stamp_key(last.split(maxsplit=1)[0])
    first = None
    while True:
        position = log.tell()
        line = log.readline()
        if not line:
            break
        stamp = _stamp_key(line.split(maxsplit=1)[0])
        if stamp < cursor:
            continue
        if first is None:
            first = position
        if stamp == cursor:
            count -= 1
            if not count:
                if line.rstrip("\n") == last.rstrip("\n"):
                    return True
                break
        else:
            break
    if first is not None:
        log.seek(first)
    return False


def update(path: Path, source: Source, now: float, retention: float) -> str | None:
    """Atomically replace one capture with its retained history and new lines."""
    prefix = ["kubectl", "-n", source.namespace]
    pod, container, identity = _pod(prefix, source)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    cutoff = now - retention * 3600
    with tempfile.TemporaryDirectory(dir=path.parent) as scratch:
        staged = Path(scratch) / "capture"
        old = path.open(encoding="utf-8") if path.exists() else nullcontext(None)
        with old as previous, open(staged, "w", encoding="utf-8", opener=_private_open) as output:
            old_identity, _ = _header(previous) if previous is not None else ([], 0)
            output.write(json.dumps([identity, now]) + "\n")
            last, count, first = _retained(previous, output, cutoff) if previous is not None else ("", 0, None)
            args = prefix + ["logs", "pod/" + pod, "-c", container, "--timestamps"]
            same_pod = identity == old_identity
            last_stamp = last.split(maxsplit=1)[0] if last else ""
            if last and same_pod:
                args.append("--since-time=" + last_stamp)
            oldest = _time(first) if first else math.inf
            with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace",
                                        dir=scratch) as fetched:
                subprocess.run(args, check=True, stdout=fetched, stderr=subprocess.DEVNULL,
                               timeout=30)
                fetched.seek(0)
                continuous = last and same_pod and _overlap(fetched, last, count)
                head = fetched.readline()
                if last and not continuous:
                    if not head:
                        raise ValueError("log cursor disappeared before a new record arrived")
                    new_stamp = head.split(maxsplit=1)[0]
                    if max(_time(last_stamp), _time(new_stamp)) >= cutoff:
                        output.write("%s %s %s\n" % (GAP, last_stamp, new_stamp))
                for line in chain((head,) if head else (), fetched):
                    stamp = line.split(maxsplit=1)[0]
                    at = _time(stamp)
                    if at >= cutoff:
                        output.write(line.rstrip("\n") + "\n")
                        if at < oldest:
                            first, oldest = stamp, at
        os.replace(staged, path)
    return first


def _private_open(path: str, flags: int) -> int:
    return os.open(path, flags, 0o600)
