"""Lane pods on other nodes, fed through the shared state store (loom#3621).

A lane slot takes work from the inbox on its runner's own disk, and the
station reads a slot's heartbeat, keepalive stamp and outcome from files on
its own disk. That pins every lane to the station's node. A slot named in
LOOM_POD_FEED_SLOTS runs instead in a lane pod anywhere that reaches the
station's state store (bus/state_store.py; the shared Postgres backend of
bus/state_postgres.py when the pod is on another node). The one station stays
the only claimer: it claims the ticket and writes the brief to the slot's
inbox as for any lane, and this module carries the brief out and the outcome
back as rows, keyed below state/lane-feed/<slot>/:

  offer.json   written by the station only: the brief it offers the slot
               ({id, ticket, brief, at, state}); state is offered, then done
               when the pod's report is applied, or withdrawn when the brief
               left the station's inbox some other way (the silent-lane move).
  claim.json   written by the pod only: the offer it took ({id, ticket, pod,
               at, reported}).
  beat.json    written by the pod only: its runner's heartbeat, the side
               files beside it and its keepalive stamp, as run_lane.sh wrote
               them on the pod.
  report.json  written by the pod only: the run's files (the brief's move to
               done/ or failed/, its logs, its answers), as a cluster runner's
               report carries them (runners.run_report_paths).

Each row has one writer, so no row is ever read, changed and written by two
processes. The station mirrors the pod's heartbeat and keepalive stamp into
its own state/usage/ and applies a report with the cluster runner's own
checks (runners.Runners.apply: only this run's files, nothing else), so the
factory and the rest of the station count the slot as free, busy, limited or
silent exactly as they count a local slot. A pod that stops pushing lets its
heartbeat and stamp age, and its running ticket moves off the silent lane as
a local one's does; the offer is then withdrawn and a late report is ignored.

The station's own lease and fencing (bus/station_lease.py) cover the offer
rows, which it writes through its active store. A pod authenticates with the
database role its LOOM_DB_* settings name. Pod clocks are trusted as cluster
runner clocks are: keep them synchronized.

Pod side: `python3 -m bus.lane_feed sync SLOT...`, which lanes/k8s/pod.sh runs
before each round when LOOM_POD_FEED=1. docs/LANES.md, "Lane pods fed through
the state store".
"""
from __future__ import annotations

import base64
import binascii
import fnmatch
import json
import os
import re
import socket
import sys
import uuid
import zlib
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from . import runners, state_store
from .labels import LANES, REMOTE_LANES

SLOTS_SETTING = "LOOM_POD_FEED_SLOTS"
# The most of one file a report carries: a larger log goes as its tail.
FILE_MAX = 4 * 1024 * 1024
# The most one report carries in all; files past it are left out.
REPORT_MAX = 24 * 1024 * 1024
POD_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")


def row_path(home: str, slot: str, name: str) -> str:
    return os.path.join(home, "state", "lane-feed", slot, name)


def feed_slots(env: Mapping[str, str]) -> List[str]:
    """The slots LOOM_POD_FEED_SLOTS names (space- or comma-separated, empty
    by default). A name that is no lane slot, a remote lane or a cluster
    runner slot is refused: each of those has its own path."""
    raw = env.get(SLOTS_SETTING) or ""
    slots: List[str] = []
    for slot in re.split(r"[\s,]+", raw.strip()):
        if slot and slot not in slots:
            slots.append(slot)
    runner = set(runners.runner_slots(dict(env)))
    for slot in slots:
        if slot not in LANES or slot in REMOTE_LANES:
            raise ValueError("%s names %r, which is not a lane slot" % (SLOTS_SETTING, slot))
        if slot in runner:
            raise ValueError("%s and LOOM_RUNNER_SLOTS both name %s" % (SLOTS_SETTING, slot))
    return slots


def is_fed(lane: str, env: Optional[Mapping[str, str]] = None) -> bool:
    """Whether a lane pod elsewhere runs this slot (a bad setting: none)."""
    try:
        return lane in feed_slots(os.environ if env is None else env)
    except ValueError:
        return False


def encode(data: bytes) -> str:
    return base64.b64encode(zlib.compress(data)).decode("ascii")


def decode(text: Any, limit: int = FILE_MAX + 4096) -> bytes:
    """A file of a row, at most `limit` bytes unpacked; ReportError otherwise."""
    if not isinstance(text, str):
        raise runners.ReportError("a file in the row is not text")
    try:
        packed = base64.b64decode(text.encode("ascii"), validate=True)
        inflate = zlib.decompressobj()
        data = inflate.decompress(packed, limit + 1)
    except (binascii.Error, ValueError, zlib.error) as err:
        raise runners.ReportError("a file in the row cannot be read (%s)" % type(err).__name__) from None
    if len(data) > limit or inflate.unconsumed_tail:
        raise runners.ReportError("a file in the row unpacks past %d bytes" % limit)
    return data


def _write(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def _read_bytes(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


# -- the station side -------------------------------------------------------
class Feed:
    """One station pass over the fed slots: mirror each pod's heartbeat,
    apply a finished run's report, withdraw an offer whose brief left the
    inbox, and offer the slot's next brief."""

    def __init__(self, home: str, env: Mapping[str, str], store: state_store.StateStore,
                 say: Callable[[str], None], now: float) -> None:
        self.home = home
        self.env = env
        self.store = store
        self.say = say
        self.now = now

    def inbox(self, slot: str) -> str:
        return os.path.join(self.home, "briefs", slot)

    def _row(self, slot: str, name: str, strict: bool = False) -> Dict[str, Any]:
        return _dict(self.store.read_json(row_path(self.home, slot, name), None, strict))

    def tend(self) -> None:
        for slot in feed_slots(self.env):
            self._mirror_beat(slot)
            offer = self._row(slot, "offer.json", strict=True)
            if offer.get("state") == "offered":
                offer = self._settle(slot, offer)
            if offer.get("state") != "offered":
                self._offer(slot)

    def _mirror_beat(self, slot: str) -> None:
        """The pod's heartbeat, side files and keepalive stamp, written where
        its runner would write them on this host."""
        row = self._row(slot, "beat.json")
        beat = row.get("beat")
        usage = os.path.join(self.home, "state", "usage")
        if isinstance(beat, dict):
            beat = dict(beat, lane=slot)
            _write(os.path.join(usage, slot + ".json"), (json.dumps(beat) + "\n").encode("utf-8"))
        files = _dict(row.get("files"))
        for suffix in runners.usage_suffixes(slot):
            if suffix != ".json" and suffix in files:
                try:
                    _write(os.path.join(usage, slot + suffix), decode(files[suffix]))
                except runners.ReportError as err:
                    self.say("lane pod %s: heartbeat file %s not read: %s" % (slot, suffix, err))
        alive = row.get("alive")
        stamp = os.path.join(usage, slot + ".alive")
        if isinstance(alive, (int, float)) and not isinstance(alive, bool):
            _write(stamp, ("%d\n" % int(alive)).encode("ascii"))
        elif row and os.path.exists(stamp):
            os.remove(stamp)

    def _settle(self, slot: str, offer: Dict[str, Any]) -> Dict[str, Any]:
        ticket = str(offer.get("ticket") or "")
        report = self._row(slot, "report.json")
        if report and report.get("id") == offer.get("id") and report.get("ticket") == ticket:
            self._apply(slot, ticket, str(offer.get("id")), report)
            state = "done"
        elif not os.path.exists(os.path.join(self.inbox(slot), ticket + ".md")):
            self.say("lane pod %s: %s left the inbox before its report; offer withdrawn"
                     % (slot, ticket))
            state = "withdrawn"
        else:
            return offer
        offer = dict(offer, state=state, settled_at=self.now)
        self.store.write_json(row_path(self.home, slot, "offer.json"), offer)
        return offer

    def _apply(self, slot: str, ticket: str, run_id: str, report: Dict[str, Any]) -> None:
        local = runners.Runners(None, self.home, "", [slot], dict(self.env), lambda repo: True,
                                self.say, now=self.now)
        try:
            files: Dict[str, bytes] = {}
            total = 0
            for rel, text in sorted(_dict(report.get("files")).items()):
                files[str(rel)] = data = decode(text)
                total += len(data)
                if total > REPORT_MAX:
                    raise runners.ReportError("the report unpacks past %d bytes" % REPORT_MAX)
            beat = local.apply(slot, ticket, files, run_id)
        except runners.ReportError as err:
            local._park(slot, ticket, "could not read the lane pod's report: %s" % err, "transient")
            return
        if os.path.exists(os.path.join(self.inbox(slot), ticket + ".md")):
            why = str(_dict(beat).get("reason") or "") or "the run ended without finishing the brief"
            local._park(slot, ticket, "lane pod: %s" % why, "transient")
            return
        self.say("read the lane pod report for %s/%s" % (slot, ticket))

    def _offer(self, slot: str) -> None:
        try:
            names = sorted(n for n in os.listdir(self.inbox(slot)) if n.endswith(".md"))
        except OSError:
            return
        for name in names:
            ticket = name[:-3]
            if not runners.TICKET_RE.fullmatch(ticket):
                continue
            try:
                with open(os.path.join(self.inbox(slot), name), encoding="utf-8") as fh:
                    brief = fh.read()
            except (OSError, UnicodeDecodeError) as err:
                self.say("lane pod %s: cannot read the brief %s: %s" % (slot, name, type(err).__name__))
                continue
            offer = {"id": uuid.uuid4().hex, "ticket": ticket, "brief": brief,
                     "at": self.now, "state": "offered"}
            self.store.write_json(row_path(self.home, slot, "offer.json"), offer)
            self.say("offered %s/%s to its lane pod" % (slot, ticket))
            return


# -- the pod side -----------------------------------------------------------
def _usage_row(home: str, slot: str, pod: str, now: float) -> Optional[Dict[str, Any]]:
    usage = os.path.join(home, "state", "usage")
    row: Dict[str, Any] = {"pod": pod, "at": now}
    raw = _read_bytes(os.path.join(usage, slot + ".json"))
    try:
        beat = json.loads(raw.decode("utf-8")) if raw is not None else None
    except (UnicodeDecodeError, ValueError):
        beat = None   # caught mid-write: the next round sends it
    if isinstance(beat, dict):
        row["beat"] = beat
    files = {}
    for suffix in runners.usage_suffixes(slot):
        data = _read_bytes(os.path.join(usage, slot + suffix)) if suffix != ".json" else None
        if data is not None and len(data) <= FILE_MAX:
            files[suffix] = encode(data)
    row["files"] = files
    alive = _read_bytes(os.path.join(usage, slot + ".alive"))
    try:
        row["alive"] = float(alive.decode("ascii").strip()) if alive is not None else None
    except (UnicodeDecodeError, ValueError):
        row["alive"] = None
    if "beat" not in row and row["alive"] is None:
        return None
    return row


def _file_bytes(full: str, rel: str) -> bytes:
    """A report file; a log past FILE_MAX as its tail under a line naming its
    size, as the cluster runner's job packs one (lanes/k8s/lane_job.py)."""
    size = os.path.getsize(full)
    with open(full, "rb") as fh:
        if size <= FILE_MAX:
            return fh.read()
        fh.seek(size - FILE_MAX)
        head = ("[loom] this log was %d bytes; only the last %d bytes follow\n"
                % (size, FILE_MAX)).encode("ascii")
        return head + fh.read()


def report_files(home: str, slot: str, ticket: str) -> Dict[str, str]:
    """This run's files under the pod's home, packed: the brief's move first,
    then its logs and answers, while they fit in REPORT_MAX."""
    globs = runners.run_report_paths(slot, ticket)
    found: List[Tuple[str, str]] = []
    for top in (os.path.join(home, "briefs", slot), os.path.join(home, "logs", slot),
                os.path.join(home, "state", "usage")):
        for root, dirs, names in os.walk(top):
            dirs.sort()
            for name in sorted(names):
                full = os.path.join(root, name)
                if os.path.islink(full) or not os.path.isfile(full):
                    continue
                rel = os.path.relpath(full, home).replace(os.sep, "/")
                if rel.startswith("state/usage/"):
                    continue   # the heartbeat travels in beat.json
                if any(fnmatch.fnmatchcase(rel, g) or rel.startswith(g + "/") for g in globs):
                    found.append((rel, full))
    found.sort(key=lambda item: (not item[0].startswith("briefs/"), item[0]))
    files: Dict[str, str] = {}
    total = 0
    for rel, full in found:
        data = _file_bytes(full, rel)
        if total + len(data) > REPORT_MAX:
            continue
        total += len(data)
        files[rel] = encode(data)
    return files


def sync(home: str, slots: List[str], store: state_store.StateStore, store_home: str,
         pod: str, now: float, say: Callable[[str], None] = lambda line: None) -> None:
    """One round for the pod's slots: push each heartbeat, push a finished
    run's report, take the next offered brief into the pod's inbox. `home`
    is the pod's loom home; `store_home` the home the store's rows are keyed
    below (any home on the shared backend; the station's own on files)."""
    for slot in slots:
        beat = _usage_row(home, slot, pod, now)
        if beat is not None:
            store.write_json(row_path(store_home, slot, "beat.json"), beat)
        claim_path = row_path(store_home, slot, "claim.json")
        claim = _dict(store.read_json(claim_path, None, True))
        inbox = os.path.join(home, "briefs", slot)
        ticket = str(claim.get("ticket") or "")
        if claim.get("id") and not claim.get("reported") and runners.TICKET_RE.fullmatch(ticket):
            if any(os.path.exists(os.path.join(inbox, kind, ticket + ".md"))
                   for kind in ("done", "failed")):
                report = {"id": claim["id"], "ticket": ticket, "pod": pod, "at": now,
                          "files": report_files(home, slot, ticket)}
                store.write_json(row_path(store_home, slot, "report.json"), report)
                claim = dict(claim, reported=True)
                store.write_json(claim_path, claim)
                say("reported %s/%s to the station" % (slot, ticket))
        offer = _dict(store.read_json(row_path(store_home, slot, "offer.json"), None, True))
        if (offer.get("state") == "withdrawn" and offer.get("id") == claim.get("id")
                and not claim.get("reported")):
            brief = os.path.join(inbox, ticket + ".md")
            if os.path.exists(brief):
                os.makedirs(os.path.join(inbox, "returned"), exist_ok=True)
                os.replace(brief, os.path.join(inbox, "returned", ticket + ".md"))
            store.write_json(claim_path, dict(claim, reported=True, withdrawn=True))
            say("%s/%s withdrawn by the station" % (slot, ticket))
            continue
        if offer.get("state") != "offered" or offer.get("id") == claim.get("id"):
            continue
        if claim.get("id") and not claim.get("reported"):
            continue   # the last run has not reported yet
        offered = str(offer.get("ticket") or "")
        if not runners.TICKET_RE.fullmatch(offered) or not isinstance(offer.get("brief"), str):
            say("%s: the offered brief is malformed; not taken" % slot)
            continue
        try:
            if any(n.endswith(".md") for n in os.listdir(inbox)):
                continue   # a brief already waits here
        except FileNotFoundError:
            pass
        _write(os.path.join(inbox, offered + ".md"), offer["brief"].encode("utf-8"))
        store.write_json(claim_path, {"id": offer["id"], "ticket": offered, "pod": pod,
                                      "at": now, "reported": False})
        say("claimed %s/%s from the station" % (slot, offered))


def main(argv: List[str]) -> int:
    """`python3 -m bus.lane_feed sync SLOT...`: one pod round (sync)."""
    import time
    if len(argv) < 3 or argv[1] != "sync":
        print("usage: python3 -m bus.lane_feed sync SLOT...", file=sys.stderr)
        return 2
    env = dict(os.environ)
    home = env.get("LOOM_HOME", "")
    if not home:
        print("lane feed: set LOOM_HOME", file=sys.stderr)
        return 2
    slots = argv[2:]
    for slot in slots:
        if slot not in LANES:
            print("lane feed: %s is not a lane slot" % slot, file=sys.stderr)
            return 2
    pod = (env.get("LOOM_POD_NAME") or socket.gethostname()).strip()
    if not POD_RE.fullmatch(pod):
        pod = "pod"
    store_home = env.get("LOOM_POD_FEED_HOME") or home
    try:
        store = state_store.select(env, store_home)
        sync(home, slots, store, store_home, pod, time.time(),
             say=lambda line: print("lane feed: " + line, file=sys.stderr))
    except (ValueError, OSError, state_store.StateError) as err:
        print("lane feed: %s" % err, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
