"""Shadow mode off for a test module that exercises merges and closes.

Shadow mode is on unless LOOM_SHADOW reads 0 (bus/shadow.py), so a module
whose tests expect a merge or a close to be sent imports these two names:

    from shadow_env import setUpModule, tearDownModule  # noqa: F401

They turn shadow off for the module's tests, whatever the environment says
(a lane machine sets LOOM_SHADOW=1), and point the ledger at a temporary
file, so no test writes to a real one. tests/test_shadow.py covers shadow on.
"""
from __future__ import annotations

import os
import tempfile
from typing import List
from unittest import mock

_active: List[mock._patch] = []
_dirs: List[tempfile.TemporaryDirectory] = []


def setUpModule() -> None:  # noqa: N802 - the unittest hook name
    folder = tempfile.TemporaryDirectory()
    _dirs.append(folder)
    patch = mock.patch.dict(os.environ, {
        "LOOM_SHADOW": "0",
        "LOOM_SHADOW_LEDGER": os.path.join(folder.name, "ledger.jsonl")})
    patch.start()
    _active.append(patch)


def tearDownModule() -> None:  # noqa: N802
    while _active:
        _active.pop().stop()
    while _dirs:
        _dirs.pop().cleanup()
