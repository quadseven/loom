"""The lane checks' shared uv directory (lanes/run_lane.sh, loom#3591).

Layout under ROOT ($LOOM_HOME/run/uv-shared):

  python/            managed Pythons; the runner installs them, checks read them
  repos/<key>/cache  one repo's uv cache; only that repo's checks write it
  locks/<key>.lock   that cache's lock; no sandbox can write here
  locks/python.lock  the lock the runner's Python installs take

Three commands:

  shared LOCK -- CMD...   take a shared lock on LOCK and exec CMD; a check
                          holds it while it runs
  exclusive LOCK -- CMD...
                          the same, exclusive: one Python install at a time
  bound ROOT KEY MAX_MB [UV]
                          with LOCK taken exclusively and without waiting,
                          bring repos/KEY/cache under MAX_MB: `UV cache
                          prune`, then `UV cache clean`; with no UV, remove
                          it. Refuses a cache any part of whose path is a
                          symlink. Busy or refused: does nothing, exit 0.

Standard library only. Messages go to stderr; the runner logs them.
"""

import errno
import fcntl
import os
import shutil
import subprocess
import sys

KEY_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789._-")


def _open_lock(path):
    return os.open(path, os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)


def locked(lock, mode, argv):
    """Take LOCK in mode (fcntl.LOCK_SH or LOCK_EX), waiting for it, and
    exec argv holding it: the lock goes when that command ends."""
    fd = _open_lock(lock)
    fcntl.flock(fd, mode)
    os.set_inheritable(fd, True)
    # S606 (a process started without a shell) is the intent: argv is the
    # runner's own command line, run as given; a shell would re-parse it.
    os.execvp(argv[0], argv)  # noqa: S606


def size_kb(path):
    """Bytes under path in KB, never following a symlink."""
    total = 0
    for top, dirs, files in os.walk(path, followlinks=False):
        for name in dirs + files:
            try:
                total += os.lstat(os.path.join(top, name)).st_blocks * 512
            except OSError:
                pass
    return total // 1024


def safe_cache(root, key):
    """The cache path when every part of it from ROOT down is a real
    directory and it resolves where it should; None otherwise."""
    real_root = os.path.realpath(root)
    if os.path.islink(root) or not os.path.isdir(real_root):
        return None
    path = root
    for part in ("repos", key, "cache"):
        path = os.path.join(path, part)
        if os.path.islink(path) or not os.path.isdir(path):
            return None
    if os.path.realpath(path) != os.path.join(real_root, "repos", key, "cache"):
        return None
    return path


def bound(root, key, max_mb, uv):
    if not key or set(key) - KEY_CHARS or key.startswith("."):
        print("uv cache bound: refusing key %r" % key, file=sys.stderr)
        return 0
    locks = os.path.join(root, "locks")
    if os.path.islink(locks) or not os.path.isdir(locks):
        print("uv cache bound: refusing %s: not a real directory" % locks, file=sys.stderr)
        return 0
    fd = _open_lock(os.path.join(locks, key + ".lock"))
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as err:
        if err.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            return 0  # a check is using the cache; the next one bounds it
        raise
    cache = safe_cache(root, key)
    if cache is None:
        print("uv cache bound: refusing the cache of %s: a symlink or not a directory" % key,
              file=sys.stderr)
        return 0
    limit = max_mb * 1024
    if size_kb(cache) <= limit:
        return 0
    env = dict(os.environ, UV_CACHE_DIR=cache)
    if not uv:
        print("uv cache bound: %s is over %d MB and the runner has no uv; removing it"
              % (cache, max_mb), file=sys.stderr)
        shutil.rmtree(cache, ignore_errors=True)
        os.makedirs(cache, exist_ok=True)
        return 0
    print("uv cache bound: %s is over %d MB; pruning it" % (cache, max_mb), file=sys.stderr)
    subprocess.run([uv, "cache", "prune"], env=env, stdin=subprocess.DEVNULL, check=False)
    if size_kb(cache) <= limit:
        return 0
    print("uv cache bound: %s is still over %d MB; cleaning it" % (cache, max_mb), file=sys.stderr)
    subprocess.run([uv, "cache", "clean"], env=env, stdin=subprocess.DEVNULL, check=False)
    os.makedirs(cache, exist_ok=True)
    return 0


def main(argv):
    if len(argv) >= 4 and argv[0] in ("shared", "exclusive") and argv[2] == "--":
        locked(argv[1], fcntl.LOCK_SH if argv[0] == "shared" else fcntl.LOCK_EX, argv[3:])
    if len(argv) in (4, 5) and argv[0] == "bound" and argv[3].isdigit():
        return bound(argv[1], argv[2], int(argv[3]), argv[4] if len(argv) == 5 else "")
    print("usage: uv_cache.py shared|exclusive LOCK -- CMD... | bound ROOT KEY MAX_MB [UV]",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
