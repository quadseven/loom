"""Where the station keeps its durable JSON state (loom#3050).

The station reads and writes about two hundred ledgers through two helpers
in bus/station.py. They call the active StateStore, so a ledger can move from
a file under the loom home to a shared database without its callers changing.
The file backend is the only one for now and is the default: it reads and
writes exactly as the helpers did. LOOM_STATE_BACKEND names the backend
(`file`, or `postgres` for the shared one in bus/state_postgres.py); a value
this build does not know, or `postgres` without its settings, is refused,
never read as "file".

Host-local files (the pass lock and its pid, runner locks, keepalive stamps,
the ETag cache, the event log) are not routed through a store.
"""
from __future__ import annotations

import json
import os
from typing import Any, Mapping


class StateError(Exception):
    """A state file that exists but cannot be read."""


class StateStore:
    """The interface every backend implements. A path is the ledger's name:
    the location it has under the loom home's state directory."""

    def read_json(self, path: str, default: Any, strict: bool = True) -> Any:
        """The stored JSON, or `default` when nothing is stored. Stored data
        that exists but cannot be read raises StateError when strict: the
        ledgers of refusals and reports would otherwise read as empty and
        every ticket would be told again. Heartbeats and caches are not
        strict (one can be caught mid-write; a lost cache costs a read)."""
        raise NotImplementedError

    def write_json(self, path: str, data: Any) -> None:
        raise NotImplementedError


class FileStore(StateStore):
    """One JSON file per ledger, replaced atomically."""

    def read_json(self, path: str, default: Any, strict: bool = True) -> Any:
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return default
        except (OSError, ValueError) as err:
            if strict:
                raise StateError("unreadable state file %s: %s" % (path, err)) from None
            return default

    def write_json(self, path: str, data: Any) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)


# Ledgers that describe THIS host and stay files whichever backend is chosen,
# named by their path below the state directory: the lane runners' heartbeats
# (lanes/run_lane.sh writes them there as files), this host's disk and deploy
# records, the pass deadline marker, the GitHub token and ETag caches, and the
# brain tick's local status. Everything else the station keeps is shared.
HOST_LOCAL_PREFIXES = ("usage/", "station/etags/")
HOST_LOCAL_FILES = frozenset({
    "brain.json", "station/disk.json", "station/disk-guard.json", "station/deployed.json",
    "station/deployed_seen.json", "station/pass-deadline.json", "station/watch.json",
    "station/app-token.json"})


def host_local(key: str) -> bool:
    return key in HOST_LOCAL_FILES or key.startswith(HOST_LOCAL_PREFIXES)         or key == ".." or key.startswith("../")


class RoutedStore(StateStore):
    """A shared store for the ledgers every station must see, and the file
    store for the ones that describe this host (host_local)."""

    def __init__(self, home: str, shared: StateStore, local: StateStore) -> None:
        self.state_dir = os.path.join(home, "state")
        self.shared = shared
        self.local = local

    def _store(self, path: str) -> StateStore:
        key = os.path.relpath(path, self.state_dir).replace(os.sep, "/")
        return self.local if host_local(key) else self.shared

    def read_json(self, path: str, default: Any, strict: bool = True) -> Any:
        return self._store(path).read_json(path, default, strict)

    def write_json(self, path: str, data: Any) -> None:
        self._store(path).write_json(path, data)

    def fence(self, epoch: int) -> None:
        fence = getattr(self.shared, "fence", None)
        if fence is not None:
            fence(epoch)


BACKEND_SETTING = "LOOM_STATE_BACKEND"


def backend_name(env: Mapping[str, str]) -> str:
    return (env.get(BACKEND_SETTING) or "file").strip().lower()


def select(env: Mapping[str, str], home: str = "") -> StateStore:
    """The store `env` names; files when it names none. `home` is the loom
    home, the root a shared backend keys its ledgers below."""
    name = backend_name(env)
    if name == "file":
        return FileStore()
    if name == "postgres":
        from . import state_postgres
        shared = state_postgres.PostgresStore(home, state_postgres.check_settings(env))
        return RoutedStore(home, shared, FileStore())
    raise ValueError("%s=%r is not a state backend this build has (file, postgres)"
                     % (BACKEND_SETTING, name))


_active: StateStore = FileStore()


def active() -> StateStore:
    return _active


def use(store: StateStore) -> StateStore:
    """Make `store` the active one; returns the one it replaced."""
    global _active
    previous, _active = _active, store
    return previous
