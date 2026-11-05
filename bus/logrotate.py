"""Rotate the loom's service logs (loom#2268).

The station, the brain and each lane write logs/<name>.log through a file
launchd or the shell opens afresh on every start. A log past its size limit
is renamed: a process still running keeps its open file and loses nothing,
and the next start makes a new file at the same path, which the log watcher
reads from its start as a new inode. A rotated file is compressed only once
no process holds it open, and is left for a later pass if it grew meanwhile.
Copying then truncating would lose what a writer adds between the two.

Run logs under logs/<slot>/ are not rotated: each belongs to one run, the
station reads them by name, and prune_logs and the size limit bound them.
"""

from __future__ import annotations

import gzip
import os
import re
import shutil
import stat
import subprocess
import time
from typing import Callable, Dict, List

Say = Callable[[str], None]

LSOF = shutil.which("lsof") or "/usr/sbin/lsof"
LSOF_TIMEOUT = 30
# KeepAlive services (lanes/mac/install.sh): launchd opens these paths once,
# so a renamed one would never get a successor.
NEVER_ROTATED = frozenset({"viewer.log", "dashboard.log"})
_ROTATED_RE = re.compile(r"^(.+\.log)\.(\d{8}T\d{6}Z)(\.gz)?$")


def has_writer(path: str) -> bool:
    """Whether a process holds path open. A check that gives no clear answer
    counts as yes: the file just waits for a later pass."""
    try:
        done = subprocess.run([LSOF, "-t", "--", path], stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=LSOF_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return True
    return done.returncode != 1 or bool(done.stdout.strip())


def _gzip(path: str, target: str) -> None:
    with open(path, "rb") as src, gzip.open(target, "wb", compresslevel=6) as dst:
        shutil.copyfileobj(src, dst, 1 << 20)


def _pack(path: str) -> bool:
    """gzip a rotated log to path.gz; False when it changed meanwhile."""
    before = os.lstat(path)
    target = path + ".gz"
    tmp = target + ".tmp"
    try:
        _gzip(path, tmp)
        after = os.lstat(path)
        if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            os.unlink(tmp)
            return False
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.unlink(path)
    return True


def rotate(folder: str, max_bytes: int, keep: int, now: float, say: Say,
           writers: Callable[[str], bool] = has_writer) -> None:
    """Rename each *.log directly in folder past max_bytes, compress each
    rotated file nothing holds open, and keep the newest keep compressed
    copies of each log. Only regular files count: no link is followed."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    try:
        names = os.listdir(folder)
    except OSError as err:
        say("cannot list %s to rotate logs: %s" % (folder, err))
        return
    for name in names:
        path = os.path.join(folder, name)
        target = "%s.%s" % (path, stamp)
        try:
            st = os.lstat(path)
            if (stat.S_ISREG(st.st_mode) and name.endswith(".log") and st.st_size > max_bytes
                    and name not in NEVER_ROTATED and not os.path.lexists(target)):
                os.rename(path, target)
                say("rotated %s at %.1f MB" % (name, st.st_size / 1024 ** 2))
        except OSError as err:
            say("cannot rotate %s: %s" % (name, err))
    try:
        names = os.listdir(folder)
    except OSError as err:
        say("cannot list %s to compress rotated logs: %s" % (folder, err))
        return
    for name in names:
        path = os.path.join(folder, name)
        match = _ROTATED_RE.match(name)
        if not match or match.group(3):
            continue
        try:
            if stat.S_ISREG(os.lstat(path).st_mode) and not writers(path):
                _pack(path)
        except OSError as err:
            say("cannot compress rotated log %s: %s" % (name, err))
    _trim(folder, keep, say)


def _trim(folder: str, keep: int, say: Say) -> None:
    """Remove all but the newest keep compressed copies of each log. The
    stamp in a name sorts by time."""
    copies: Dict[str, List[str]] = {}
    try:
        names = os.listdir(folder)
    except OSError as err:
        say("cannot list %s to trim rotated logs: %s" % (folder, err))
        return
    for name in sorted(names):
        match = _ROTATED_RE.match(name)
        if match and match.group(3):
            copies.setdefault(match.group(1), []).append(name)
    for found in copies.values():
        for name in found[:max(len(found) - keep, 0)]:
            try:
                os.unlink(os.path.join(folder, name))
            except OSError as err:
                say("cannot remove rotated log %s: %s" % (name, err))
