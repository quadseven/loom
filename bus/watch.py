"""Read component logs incrementally and file tickets for repeated errors."""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
from typing import Any, Callable

from . import text
from .labels import LANES

WINDOW = 30 * 60
HISTORY = 24 * 60 * 60
COMMENT_INTERVAL = 60 * 60
ERROR = re.compile(r"\b(?:error|failed|refused|traceback|http\s*[45][0-9]{2}|[45][0-9]{2})\b", re.I)
TOKEN = re.compile(r"\b(?:[a-f0-9]{8,}|[0-9]+)\b", re.I)
WORD = re.compile(r"(?<!<)\b[A-Za-z][A-Za-z_-]*\b(?!>)")
ISSUE = re.compile(r"\b[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[0-9]+\b")
REPO = re.compile(r"\b[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\b")
URL = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.I)
HOST = re.compile(r"\b(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}\b")
SECRET = re.compile(r"(?i)\b(?:token|password|secret|key)\s*[=:]\s*\S+")
SAFE_WORDS = frozenset("""error failed refused traceback http https status response request
for from to in on at of the a an and or with by was is has had returned returning
connection connect transport sandbox permission denied forbidden unauthorized
not found timeout timed out rate limit exceeded unavailable internal server
service gateway bad fatal exception api git github issue repo lane station brain
run retry authentication access could cannot open read write file process exit
code upstream network reset closed broken pipe invalid missing fetch post get
put patch delete failed refused""".split())


def signature(line: str) -> str:
    """Keep useful error vocabulary while discarding deployment-specific text."""
    line = line.encode("ascii", "replace").decode("ascii")
    line = SECRET.sub("<secret>", line)
    line = URL.sub("<url>", line)
    line = text.scrub_paths(line)
    line = ISSUE.sub("<issue>", line)
    line = REPO.sub("<repo>", line)
    line = HOST.sub("<host>", line)
    line = TOKEN.sub(lambda m: m.group() if m.group() in {"400", "401", "403", "404",
                       "408", "409", "429", "500", "502", "503", "504"} else "<number>", line)
    line = WORD.sub(lambda m: m.group().lower() if m.group().lower() in SAFE_WORDS
                    else "<text>", line)
    line = re.sub(r"\s+", " ", line).strip()[:160]
    text.check(line)
    return line


def components(home: str, config: str | None = None) -> list[tuple[str, str]]:
    """Configured name=path lines, or the station, brain and installed lanes."""
    if config:
        with open(config, encoding="utf-8") as fh:
            entries = [line.strip().split("=", 1) for line in fh
                       if line.strip() and not line.lstrip().startswith("#")]
        if any(len(item) != 2 for item in entries):
            raise ValueError("LOOM_WATCH needs name=path lines")
        return [(name if name in {"station", "brain", *LANES} else "component-%d" % i,
                 path if os.path.isabs(path) else os.path.join(home, path))
                for i, (name, path) in enumerate(entries, 1)]
    logs = os.path.join(home, "logs")
    entries = [("station", os.path.join(logs, "station.log")),
               ("brain", os.path.join(logs, "brain.log"))]
    entries += [(os.path.basename(path)[:-12], path)
                for path in sorted(glob.glob(os.path.join(logs, "*-service.log")))
                if os.path.basename(path)[:-12] in LANES]
    return entries


def _read(path: str, offset: dict[str, Any]) -> tuple[list[str], dict[str, int]]:
    try:
        with open(path, "rb") as fh:
            st = os.fstat(fh.fileno())
            start = int(offset.get("pos", 0)) if offset.get("inode") == st.st_ino else 0
            if start > st.st_size:
                start = 0
            fh.seek(start)
            lines = []
            while True:
                before = fh.tell()
                raw = fh.readline()
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    fh.seek(before)
                    break
                lines.append(raw.decode("utf-8", "replace").rstrip("\r\n"))
            return lines, {"inode": st.st_ino, "pos": fh.tell()}
    except FileNotFoundError:
        return [], {}


def watch(gh: Any, home: str, state_dir: str, repo: str, now: float,
          config: str | None = None, say: Callable[[str], None] = print) -> None:
    """One station pass. The station lock serializes reads and state writes."""
    if not repo:
        return
    path = os.path.join(state_dir, "watch.json")
    try:
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)
    except FileNotFoundError:
        state = {"offsets": {}, "signatures": {}}
    offsets, records = state["offsets"], state["signatures"]
    for name, log in components(home, config):
        lines, offset = _read(log, offsets.get(name, {}))
        matches = 0
        for line in lines:
            if not ERROR.search(line):
                continue
            sig = signature(line)
            if not sig:
                continue
            matches += 1
            key = hashlib.sha256((name + "\0" + sig).encode()).hexdigest()
            rec = records.get(key, {})
            previous = rec.get("last_seen", 0)
            hits = [at for at in rec.get("hits", []) if at >= now - WINDOW]
            if not hits and not rec.get("issue"):
                rec["eligible"] = previous < now - HISTORY
            hits.append(now)
            rec.update(signature=sig, component=name, hits=hits, last_seen=now,
                       total=rec.get("total", 0) + 1)
            records[key] = rec
        offsets[name] = offset
        say("watch scanned %s: %d new lines, %d errors" % (name, len(lines), matches))
    os.makedirs(state_dir, exist_ok=True)
    for rec in records.values():
        hits = [at for at in rec.get("hits", []) if at >= now - WINDOW]
        rec["hits"] = hits
        if not rec.get("issue") and (len(hits) < 3 or not rec.get("eligible")):
            continue
        count = len(hits)
        if rec.get("issue"):
            if (rec["total"] <= rec.get("comment_count", rec["total"])
                    or now - rec.get("comment_at", now) < COMMENT_INTERVAL):
                continue
            body = "%d total occurrences; %d in the last 30 minutes." % (rec["total"], count)
            text.check(body)
            gh.rest("POST", "/repos/%s/issues/%d/comments" % (repo, rec["issue"]), {"body": body})
            rec.update(comment_at=now, comment_count=rec["total"])
        else:
            sig = rec["signature"]
            title = "%s: %s" % (rec["component"], sig)
            existing = next((item for item in gh.paginate(
                "/repos/%s/issues?labels=loom%%3Atriage&state=open&per_page=100" % repo)
                if item.get("title") == title and "pull_request" not in item), None)
            if existing is not None:
                rec.update(issue=int(existing["number"]), comment_at=now,
                           comment_count=rec["total"])
                continue
            body = ("Repeated %d times in 30 minutes.\n\nFirst example: %s\n"
                    "Last example: %s" % (count, sig, sig))
            text.check(title)
            text.check(body)
            made = gh.rest("POST", "/repos/%s/issues" % repo, {
                "title": title, "body": body, "labels": ["loom:triage"]}).data
            rec.update(issue=int(made["number"]), comment_at=now,
                       comment_count=rec["total"])
        with open(path + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(path + ".tmp", path)
    # Keep only recent unfiled signatures; filed ones retain their issue link.
    state["signatures"] = {key: rec for key, rec in records.items()
                           if rec.get("issue") or rec["last_seen"] >= now - HISTORY}
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    os.replace(path + ".tmp", path)
