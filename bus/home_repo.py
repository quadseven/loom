"""The deployment's own repo, read from its settings (loom#2584).

LOOM_SELF_REPO, else LOOM_STATUS_REPO, else this checkout's origin remote:
the same default lanes/mac/install.sh and the station already give the
status repo. A fresh clone names its own repo and the running deployment
names the one it runs from, so no code line carries an owner's repo as a
literal and an unset setting changes nothing. An empty setting reads as
unset (the installed plists carry both as empty strings).
"""
from __future__ import annotations

import functools
import os
import re
import subprocess
from typing import Mapping, Optional

# owner/name at the end of an origin URL, ssh or https, .git or not.
REMOTE_RE = re.compile(r"[:/]([A-Za-z0-9._-]+/[A-Za-z0-9._-]+?)(?:\.git)?/?$")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@functools.lru_cache(maxsize=1)
def origin_repo() -> str:
    """owner/name of this checkout's origin remote, or "" when unknown."""
    try:
        out = subprocess.run(["git", "-C", ROOT, "remote", "get-url", "origin"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    m = REMOTE_RE.search(out.stdout.strip()) if out.returncode == 0 else None
    return m.group(1) if m else ""


def home_repo(env: Optional[Mapping[str, str]] = None) -> str:
    """LOOM_SELF_REPO, else LOOM_STATUS_REPO, else this checkout's origin;
    "" only when none of them says."""
    values = os.environ if env is None else env
    for name in ("LOOM_SELF_REPO", "LOOM_STATUS_REPO"):
        value = (values.get(name) or "").strip()
        if value:
            return value
    return origin_repo()


def status_repo(env: Optional[Mapping[str, str]] = None) -> str:
    """LOOM_STATUS_REPO, else LOOM_SELF_REPO, else this checkout's origin:
    the order the brain lock and the tick record are read in."""
    values = os.environ if env is None else env
    for name in ("LOOM_STATUS_REPO", "LOOM_SELF_REPO"):
        value = (values.get(name) or "").strip()
        if value:
            return value
    return origin_repo()
