"""Clear Loom's own data before the free-space floor stops claims (loom#3187).

The station claims no new work while the loom home's disk has less than
LOOM_MIN_FREE_GB free (loom#1808). On 2026-11-17 the lane machine fell under
that floor and every claim paused for hours: nothing acted until a person
looked. This watchdog acts first. Once the disk has less than
LOOM_DISK_WATCHDOG_GB free (default 25; 0 turns it off), at most every
WATCH_EVERY seconds, it clears Loom-owned data in this order and stops as
soon as the disk is back above the trigger:

1. Lane logs (logs/<slot>/) unchanged for LOG_DAYS days, and rotated service
   logs: archived to LOOM_DISK_ARCHIVE_DIR when that folder is mounted,
   else compressed in place.
2. Finished run scratch of idle lanes (run/<slot>-check, -check-default,
   -tmp, -intent.*, -gh.*) unchanged for LOOM_DISK_RUN_DAYS days (default 2).
3. Lane trees unchanged for TREE_DAYS days: archived to the archive folder,
   or to <home>/archive/trees when none is mounted, and removed only once the
   archive reads back whole. Local tree archives go after LOCAL_ARCHIVE_DAYS.
4. Regenerable tool caches in idle lanes' homes (HOME_CACHES).

Nothing a waiting or running brief names is touched, nor a busy lane's run
folder. Every path it acts on must sit, with no link anywhere along it,
inside one of the Loom-owned roots (Roots.owned); a link is never followed.
When even that leaves the disk under the floor, it logs the largest folders
beside the loom home once an hour and keeps one line for the lane status
(the station's operator notice) until the disk is above the floor again.

The station runs it after each pass (Station.tend_watchdog). The station runs
only on the Mac lane host; this module is plain Python and works the same on
any host whose station calls it.
"""

from __future__ import annotations

import gzip
import hashlib
import math
import os
import re
import shutil
import stat
import sys
import tarfile
import time
from typing import Callable, Dict, FrozenSet, Iterator, List, NamedTuple, Optional, Tuple

GB = 1024 ** 3
DAY = 86400
TRIGGER_GB = 25.0
LOG_DAYS = 7
RUN_DAYS = 2
TREE_DAYS = 2
LOCAL_ARCHIVE_DAYS = 14
WATCH_EVERY = 300
REPORT_EVERY = 3600
SCAN_SECONDS = 120
RUN_SCRATCH = ("-check", "-check-default", "-tmp")
RUN_SCRATCH_PREFIXES = ("-intent.", "-gh.")
HOME_CACHES = (".cache", ".cargo", ".npm", "tmp", "Library/Caches", ".bun/install/cache",
               ".gradle/caches")
# owner.repo-N, with any -rN, -verify-N or .log tail (tickets.ticket_name).
TICKET_RE = re.compile(r"^([A-Za-z0-9_-]+\.[A-Za-z0-9_.-]+?-[0-9]+)(?=$|[-.])")
_ROTATED_RE = re.compile(r"^.+\.log\.[0-9]{8}T[0-9]{6}Z\.gz$")
COMPRESSED = (".gz", ".tgz", ".zst", ".xz", ".bz2")

Say = Callable[[str], None]


class Config(NamedTuple):
    trigger_gb: float = TRIGGER_GB
    archive_dir: str = ""
    log_days: int = LOG_DAYS
    run_days: int = RUN_DAYS
    tree_days: int = TREE_DAYS

    @classmethod
    def from_env(cls, env) -> "Config":
        return cls(trigger_gb=_number(env.get("LOOM_DISK_WATCHDOG_GB", ""), TRIGGER_GB),
                   archive_dir=str(env.get("LOOM_DISK_ARCHIVE_DIR", "") or "").strip(),
                   run_days=int(_number(env.get("LOOM_DISK_RUN_DAYS", ""), RUN_DAYS)))


def _number(raw, default: float) -> float:
    raw = str(raw or "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        return default
    return value if value >= 0 and math.isfinite(value) else default


class Roots(NamedTuple):
    """What the station knows this pass: the loom home, the lane tree
    folders, the lane slots, the brief names waiting or running, and the
    slots with no runner and no brief."""
    home: str
    trees: Tuple[str, ...]
    lanes: Tuple[str, ...]
    live: FrozenSet[str]
    idle: FrozenSet[str]

    def owned(self) -> Tuple[str, ...]:
        home = self.home.rstrip(os.sep)
        return tuple(os.path.join(home, d) for d in ("logs", "run", "archive")) + \
            tuple(t.rstrip(os.sep) for t in self.trees)


class Info(NamedTuple):
    is_dir: bool
    is_link: bool
    newest: float
    size: int


class Item(NamedTuple):
    step: str
    path: str
    dest: Optional[str]  # None: remove; else archive to dest, verified


def _gb(n: float) -> str:
    return "%.1f GB" % (n / GB)


def _ticket(name: str) -> Optional[str]:
    match = TICKET_RE.match(name)
    return match.group(1) if match else None


class Watchdog:
    def __init__(self, cfg: Config, roots: Roots, disk, floor: float, now: float, say: Say):
        self.cfg, self.roots, self.disk = cfg, roots, disk
        self.floor, self.now, self.say = floor, now, say
        self.trigger = cfg.trigger_gb * GB
        self.live = {t for t in (_ticket(n) for n in roots.live) if t} | set(roots.live)
        self.archive = cfg.archive_dir if cfg.archive_dir and disk.usable(cfg.archive_dir) else ""

    # -- the guard ------------------------------------------------------------
    def owned(self, path: str) -> bool:
        """path is strictly inside a Loom-owned root, neither the root nor
        anything below it on the way is a link, and the path resolves inside
        where the root resolves (a link above the root, such as the temp
        folder's on macOS, moves both alike)."""
        for root in self.roots.owned():
            if not path.startswith(root + os.sep):
                continue
            if self.disk.islink(root):
                return False
            rest = path[len(root) + 1:].split(os.sep)
            here = root
            for part in rest:
                if part in ("", ".", ".."):
                    return False
                here = os.path.join(here, part)
                if self.disk.islink(here):
                    return False
            return self.disk.realpath(path).startswith(self.disk.realpath(root) + os.sep)
        return False

    def _kept(self, name: str) -> bool:
        return name in self.live or _ticket(name) in self.live

    def _entries(self, folder: str) -> Iterator[Tuple[str, Info]]:
        for name in self.disk.listdir(folder):
            path = os.path.join(folder, name)
            info = self.disk.info(path)
            if info is not None and not info.is_link:
                yield name, info

    # -- the steps ----------------------------------------------------------
    def logs(self) -> List[Item]:
        cutoff = self.now - self.cfg.log_days * DAY
        logs = os.path.join(self.roots.home, "logs")
        found = []
        for slot in self.roots.lanes:
            for name, info in self._entries(os.path.join(logs, slot)):
                if info.newest < cutoff and not self._kept(name):
                    found.append((info.newest, os.path.join(logs, slot, name), info))
        for name, info in self._entries(logs):
            if not info.is_dir and _ROTATED_RE.match(name) and info.newest < cutoff:
                found.append((info.newest, os.path.join(logs, name), info))
        items = []
        for _, path, info in sorted(found):
            dest = self._log_dest(path, info)
            if dest is not None:
                items.append(Item("logs", path, dest))
        return items

    def _log_dest(self, path: str, info: Info) -> Optional[str]:
        tail = ".tar.gz" if info.is_dir else ".gz"
        done = path.endswith(COMPRESSED)
        if self.archive:
            rel = os.path.relpath(path, self.roots.home)
            return os.path.join(self.archive, "loom", rel) + ("" if done else tail)
        return None if done else path + tail

    def run(self) -> List[Item]:
        cutoff = self.now - self.cfg.run_days * DAY
        run = os.path.join(self.roots.home, "run")
        found = []
        for name, info in self._entries(run):
            slot = self._scratch_slot(name)
            if slot in self.roots.idle and info.newest < cutoff:
                found.append((info.newest, os.path.join(run, name)))
        return [Item("run", p, None) for _, p in sorted(found)]

    def _scratch_slot(self, name: str) -> Optional[str]:
        for slot in self.roots.lanes:
            if name in [slot + s for s in RUN_SCRATCH] or \
                    any(name.startswith(slot + p) for p in RUN_SCRATCH_PREFIXES):
                return slot
        return None

    def trees(self) -> List[Item]:
        local = os.path.join(self.roots.home, "archive", "trees")
        items = [Item("trees", os.path.join(local, n), None) for n, info in self._entries(local)
                 if info.newest < self.now - LOCAL_ARCHIVE_DAYS * DAY]
        cutoff = self.now - self.cfg.tree_days * DAY
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(self.now))
        found = []
        for root in self.roots.trees:
            for name, info in self._entries(root):
                if _ticket(name) and not self._kept(name) and info.newest < cutoff:
                    found.append((info.newest, os.path.join(root, name), name))
        base = os.path.join(self.archive, "loom", "trees") if self.archive else local
        for _, path, name in sorted(found):
            items.append(Item("trees", path, os.path.join(base, "%s-%s.tar.gz" % (name, stamp))))
        return items

    def caches(self) -> List[Item]:
        run = os.path.join(self.roots.home, "run")
        items = []
        for slot in sorted(self.roots.idle):
            for cache in HOME_CACHES:
                path = os.path.join(run, slot + "-home", *cache.split("/"))
                info = self.disk.info(path)
                if info is not None and not info.is_link and info.size > 0:
                    items.append(Item("caches", path, None))
        return items

    # -- the run ------------------------------------------------------------
    def clear(self) -> Tuple[int, bool]:
        """Work through the steps until the disk is above the trigger. Returns
        (bytes freed, whether the trigger was reached)."""
        start = free = self.disk.free(self.roots.home)
        steps = (("logs older than %d days" % self.cfg.log_days, self.logs),
                 ("finished run folders older than %d days" % self.cfg.run_days, self.run),
                 ("lane trees older than %d days" % self.cfg.tree_days, self.trees),
                 ("tool caches of idle lanes", self.caches))
        for label, step in steps:
            if free >= self.trigger:
                break
            before, count = free, 0
            for item in step():
                if free >= self.trigger:
                    break
                if self._act(item):
                    count += 1
                free = self.disk.free(self.roots.home)
            if count:
                self.say("disk watchdog: %s: %d cleared, %s freed, %s free"
                         % (label, count, _gb(max(0, free - before)), _gb(free)))
        return max(0, free - start), free >= self.trigger

    def _act(self, item: Item) -> bool:
        if not self.owned(item.path):
            self.say("disk watchdog: refused %s: not inside a loom-owned folder" % item.path)
            return False
        try:
            if item.dest is None:
                self.disk.remove(item.path)
            else:
                self.disk.archive(item.path, item.dest)
        except OSError as err:
            self.say("disk watchdog: kept %s: %s" % (item.path, err))
            return False
        return True

    def report(self) -> str:
        """Log the largest folders beside the loom home; the notice line."""
        home = self.roots.home.rstrip(os.sep)
        parent = os.path.dirname(home)
        skip = {home} | ({self.archive} if self.archive else set())
        found = self.disk.largest(parent, skip, time.monotonic() + SCAN_SECONDS)
        parts = ", ".join("%s %s" % (os.path.relpath(p, parent), _gb(n)) for p, n in found[:8])
        free = self.disk.free(home)
        self.say("disk watchdog: loom's own data is cleared and only %s is free, under the "
                 "%s floor; largest folders outside loom: %s" % (_gb(free), _gb(self.floor),
                                                                 parts or "none measured"))
        return ("Disk watchdog: Loom's own data is cleared and the disk still has only %s "
                "free, under the %s floor; station.log names the largest other folders."
                % (_gb(free), _gb(self.floor)))


def tend(cfg: Config, roots: Roots, disk, floor: float, now: float, say: Say,
         saved: Dict) -> Dict:
    """One watchdog turn. saved is the last turn's record; the new one is
    returned: at (last run), reported (last floor report), notice."""
    if cfg.trigger_gb <= 0:
        return {}
    free = disk.free(roots.home)
    last = saved.get("at")
    if free >= cfg.trigger_gb * GB or (isinstance(last, (int, float)) and
                                       0 <= now - last < WATCH_EVERY):
        if free >= floor and saved.get("notice"):
            return dict(saved, notice=None)
        return saved
    dog = Watchdog(cfg, roots, disk, floor, now, say)
    freed, _ = dog.clear()
    out = dict(saved, at=now, freed=freed)
    if disk.free(roots.home) >= floor:
        out["notice"] = None
        return out
    reported = saved.get("reported")
    if not isinstance(reported, (int, float)) or now - reported >= REPORT_EVERY:
        out["notice"] = dog.report()
        out["reported"] = now
    return out


# -- the real filesystem ------------------------------------------------------
def _walk(path: str) -> Tuple[float, int, int]:
    """(newest mtime, bytes on disk, regular files) of a path and, for a
    real folder, all under it; links are counted as themselves."""
    st = os.lstat(path)
    newest, size, files = st.st_mtime, _bytes(st), int(stat.S_ISREG(st.st_mode))
    if stat.S_ISDIR(st.st_mode):
        for top, dirs, names in os.walk(path, followlinks=False):
            for name in dirs + names:
                try:
                    sub = os.lstat(os.path.join(top, name))
                except OSError:
                    continue
                newest = max(newest, sub.st_mtime)
                size += _bytes(sub)
                files += int(stat.S_ISREG(sub.st_mode))
    return newest, size, files


def _bytes(st: os.stat_result) -> int:
    blocks = getattr(st, "st_blocks", None)
    return blocks * 512 if blocks is not None else st.st_size


def _sha(path: str, opener=open) -> str:
    digest = hashlib.sha256()
    with opener(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rmtree(path: str) -> None:
    """shutil.rmtree, making a read-only folder writable on the way (a run
    can leave a read-only snapshot, lanes/run_lane.sh run_tmpdir). A link
    is removed as a link."""
    def writable(func, target, _exc):
        os.chmod(os.path.dirname(target), 0o700)
        if not os.path.islink(target) and os.path.isdir(target):
            os.chmod(target, 0o700)
        func(target)
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=writable)
    else:
        shutil.rmtree(path, onerror=writable)


def _measure(path: str, deadline: float) -> int:
    """Bytes on disk under path, counted until deadline."""
    size = 0
    for top, dirs, names in os.walk(path, followlinks=False):
        if time.monotonic() > deadline:
            break
        for name in dirs + names:
            try:
                size += _bytes(os.lstat(os.path.join(top, name)))
            except OSError:
                continue
    return size


class RealDisk:
    def free(self, path: str) -> int:
        return shutil.disk_usage(path).free

    def listdir(self, folder: str) -> List[str]:
        try:
            if not stat.S_ISDIR(os.lstat(folder).st_mode):
                return []
            return sorted(os.listdir(folder))
        except OSError:
            return []

    def info(self, path: str) -> Optional[Info]:
        try:
            st = os.lstat(path)
            if stat.S_ISLNK(st.st_mode):
                return Info(False, True, st.st_mtime, 0)
            newest, size, _ = _walk(path)
        except OSError:
            return None
        return Info(stat.S_ISDIR(st.st_mode), False, newest, size)

    def realpath(self, path: str) -> str:
        return os.path.realpath(path)

    def islink(self, path: str) -> bool:
        return os.path.islink(path)

    def usable(self, folder: str) -> bool:
        return os.path.isdir(folder) and os.access(folder, os.W_OK)

    def remove(self, path: str) -> None:
        if stat.S_ISDIR(os.lstat(path).st_mode):
            _rmtree(path)
            if os.path.lexists(path):
                raise OSError("could not remove all of %s" % path)
        else:
            os.unlink(path)

    def archive(self, src: str, dest: str) -> None:
        """Write src to dest (gzip for a file, tar.gz for a folder), read it
        back, and remove src only when the copy is whole. Links are stored
        as links."""
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        part = dest + ".part"
        st = os.lstat(src)
        try:
            if stat.S_ISDIR(st.st_mode):
                self._tar(src, part)
            elif src.endswith(COMPRESSED):
                self._copy(src, part)
            else:
                self._gzip(src, part)
            # The source's time, so log retention still counts from the run.
            os.utime(part, (st.st_atime, st.st_mtime))
            os.replace(part, dest)
        finally:
            if os.path.lexists(part):
                os.unlink(part)
        self.remove(src)

    @staticmethod
    def _gzip(src: str, part: str) -> None:
        with open(src, "rb") as fh, gzip.open(part, "wb", compresslevel=6) as out:
            shutil.copyfileobj(fh, out, 1 << 20)
        if _sha(part, gzip.open) != _sha(src):
            raise OSError("archive of %s did not verify" % src)

    @staticmethod
    def _copy(src: str, part: str) -> None:
        shutil.copyfile(src, part)
        if _sha(part) != _sha(src):
            raise OSError("copy of %s did not verify" % src)

    @staticmethod
    def _tar(src: str, part: str) -> None:
        _, _, files = _walk(src)
        want = sum(os.lstat(os.path.join(t, n)).st_size
                   for t, _, ns in os.walk(src, followlinks=False) for n in ns
                   if stat.S_ISREG(os.lstat(os.path.join(t, n)).st_mode))
        with tarfile.open(part, "w:gz", compresslevel=6) as tf:
            tf.add(src, arcname=os.path.basename(src), recursive=True)
        got_files = got_bytes = 0
        with tarfile.open(part, "r:gz") as tf:
            for member in tf:
                if member.isreg():
                    got_files += 1
                    got_bytes += member.size
                    reader = tf.extractfile(member)
                    while reader is not None and reader.read(1 << 20):
                        pass
        if (got_files, got_bytes) != (files, want):
            raise OSError("archive of %s did not verify: %d files, %d bytes; wanted %d, %d"
                          % (src, got_files, got_bytes, files, want))

    def largest(self, root: str, skip, deadline: float) -> List[Tuple[str, int]]:
        """The folders in root (and in its largest few) by size, measured
        until deadline, never inside skip."""
        sizes: List[Tuple[str, int]] = []
        for name in self.listdir(root):
            path = os.path.join(root, name)
            if time.monotonic() > deadline:
                break
            if path in skip or any(s.startswith(path + os.sep) for s in skip):
                if path not in skip:
                    sizes += self.largest(path, skip, deadline)
                continue
            if os.path.isdir(path) and not os.path.islink(path):
                sizes.append((path, _measure(path, deadline)))
        return sorted(sizes, key=lambda kv: -kv[1])
