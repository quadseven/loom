"""Where the station keeps its durable JSON state (loom#3050).

The station reads and writes about two hundred ledgers through two helpers
in bus/station.py. They call the active StateStore, so a ledger can move from
a file under the loom home to a shared database without its callers changing.
The file backend is the only one for now and is the default: it reads and
writes exactly as the helpers did. LOOM_STATE_BACKEND names the backend;
a value this build does not know is refused, never read as "file".

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


BACKEND_SETTING = "LOOM_STATE_BACKEND"


def select(env: Mapping[str, str]) -> StateStore:
    """The store `env` names; files when it names none."""
    name = (env.get(BACKEND_SETTING) or "file").strip().lower()
    if name == "file":
        return FileStore()
    raise ValueError("%s=%r is not a state backend this build has (file)" % (BACKEND_SETTING, name))


_active: StateStore = FileStore()


def active() -> StateStore:
    return _active


def use(store: StateStore) -> StateStore:
    """Make `store` the active one; returns the one it replaced."""
    global _active
    previous, _active = _active, store
    return previous
