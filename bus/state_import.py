"""One-shot import of a station's file ledgers into the shared backend (loom#3050).

A station that moves from files to LOOM_STATE_BACKEND=postgres would start with
empty ledgers: it would report, ask and notify again, re-run failed CI once and
forget its watches and holds. This copies the file ledgers it leaves behind
into the shared store once, so the new station carries on where the old one
stopped.

    python3 -m bus state-import --dry-run     # counts per ledger, writes nothing
    python3 -m bus state-import               # copy what the store lacks
    python3 -m bus state-import --overwrite   # replace ledgers the store already has

What is copied: every `*.json` below `$LOOM_HOME/state` except the host-local
ledgers (state_store.host_local: the ETag cache, the runner heartbeats, this
host's disk and deploy records and the like), which stay files by design.

Idempotent: a ledger the shared store already holds is left alone unless
--overwrite is given, so a second run, or a run after the new station has
started writing, never clobbers newer state. Each copy is read back and
compared before it counts as done.

Run it while the old station is stopped. It refuses to run while a pass on this
host holds the station lock; it cannot see a station on another host, so the
caller stops that one first.
"""
from __future__ import annotations

import contextlib
import json
import os
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from . import state_store

_MISSING = object()


def ledgers(home: str, skip: Tuple[str, ...] = ()) -> List[Tuple[str, str]]:
    """(key, path) of every importable file ledger, sorted by key."""
    state = os.path.join(home, "state")
    found: List[Tuple[str, str]] = []
    for root, _dirs, files in os.walk(state):
        for name in files:
            if not name.endswith(".json"):
                continue
            path = os.path.join(root, name)
            key = os.path.relpath(path, state).replace(os.sep, "/")
            if state_store.host_local(key) or key in skip:
                continue
            found.append((key, path))
    return sorted(found)


def _entries(value: Any) -> int:
    return len(value) if isinstance(value, (list, dict)) else 1


def station_busy(home: str) -> bool:
    """True while a pass on this host holds the station lock."""
    import fcntl
    path = os.path.join(home, "state", "station.lock")
    if not os.path.exists(path):
        return False
    fd = os.open(path, os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


@contextlib.contextmanager
def hold_station_lock(home: str):
    """Hold the station lock for the whole block; yield False if a pass holds it.

    station_busy only probes and lets go, so a pass could start between the
    probe and the copy. A real import holds the lock until it is done.
    """
    import fcntl
    path = os.path.join(home, "state", "station.lock")
    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def run(home: str, target: Optional[state_store.StateStore], dry_run: bool = False,
        overwrite: bool = False, skip: Tuple[str, ...] = (),
        say: Callable[[str], None] = print) -> int:
    """Copy the ledgers; returns 0 when every one was copied, skipped as
    already present, or (dry run) would be, and 1 when any could not be."""
    counts: Dict[str, int] = {}
    failed = 0
    for key, path in ledgers(home, skip):
        try:
            with open(path, encoding="utf-8") as fh:
                value = json.load(fh)
        except (OSError, ValueError) as err:
            say("%-44s UNREADABLE (%s)" % (key, type(err).__name__))
            failed += 1
            continue
        size = os.path.getsize(path)
        if target is None:
            status = "would copy (target not checked)"
        else:
            try:
                present = target.read_json(os.path.join(home, "state", key), _MISSING) is not _MISSING
                if present and not overwrite:
                    status = "kept (already in the store)"
                elif dry_run:
                    status = "would overwrite" if present else "would copy"
                else:
                    target.write_json(os.path.join(home, "state", key), value)
                    back = target.read_json(os.path.join(home, "state", key), _MISSING)
                    if back != value:
                        raise state_store.StateError("read-back differs")
                    status = "overwritten" if present else "copied"
            except state_store.StateError as err:
                say("%-44s FAILED (%s)" % (key, err))
                failed += 1
                continue
        word = status.split()[0] if not status.startswith("would") else " ".join(status.split()[:2])
        counts[word] = counts.get(word, 0) + 1
        say("%-44s %-32s %7d entries %10d bytes" % (key, status, _entries(value), size))
    say("summary: " + (", ".join("%s %d" % (k, v) for k, v in sorted(counts.items())) or "nothing to import")
        + ("; %d failed" % failed if failed else "") + ("; dry run, nothing written" if dry_run else ""))
    return 1 if failed else 0


def main(env: Mapping[str, str], dry_run: bool, overwrite: bool, skip: Tuple[str, ...],
         say: Callable[[str], None] = print) -> int:
    home = env.get("LOOM_HOME") or os.path.join(os.path.expanduser("~"), "loom")
    if not os.path.isdir(os.path.join(home, "state")):
        say("error: no state directory under the loom home")
        return 2
    target: Optional[state_store.StateStore] = None
    if state_store.backend_name(env) == "postgres":
        try:
            routed = state_store.select(env, home)
        except ValueError as err:
            if not dry_run:
                say("error: %s" % err)
                return 2
            say("note: %s; counting the files only" % err)
        else:
            target = getattr(routed, "shared", routed)
    elif not dry_run:
        say("error: LOOM_STATE_BACKEND must be postgres, the store to import into")
        return 2
    if dry_run:
        return run(home, target, dry_run, overwrite, skip, say)
    with hold_station_lock(home) as acquired:
        if not acquired:
            say("error: a station pass on this host holds the station lock; stop the station first")
            return 2
        return run(home, target, dry_run, overwrite, skip, say)
