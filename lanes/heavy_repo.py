"""Coordinate a local lane agent and other builders of the same repo."""

import fcntl
import os
from pathlib import Path
import sys
from typing import Optional


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def update(action: str, lock: Path, runner: Optional[int] = None) -> bool:
    # Serialize the PID check and directory update. Two stale-lock reapers
    # must never remove the lock that one of them has just taken.
    lock.parent.mkdir(parents=True, exist_ok=True)
    with (lock.parent / ".guard").open("a") as guard:
        flags = fcntl.LOCK_EX
        if action == "take":
            flags |= fcntl.LOCK_NB
        try:
            fcntl.flock(guard, flags)
        except BlockingIOError:
            return False
        pid_file = lock / "pid"
        try:
            holder = int(pid_file.read_text().strip())
        except FileNotFoundError:
            holder = None
        caller = os.getppid()
        if action == "adopt":
            if runner is None or holder != runner:
                return False
        elif action == "release":
            if holder == caller or (holder is not None and holder > 0 and not alive(holder)):
                pid_file.unlink()
                lock.rmdir()
            return True
        elif action != "take":
            raise ValueError("unknown lock action")
        elif holder is not None and (holder <= 0 or alive(holder)):
            return False
        lock.mkdir(exist_ok=True)
        fresh = lock / "pid.new"
        fresh.write_text(str(caller) + "\n")
        fresh.replace(pid_file)
        return True


if __name__ == "__main__":
    runner = int(sys.argv[3]) if len(sys.argv) > 3 else None
    sys.exit(0 if update(sys.argv[1], Path(sys.argv[2]), runner) else 1)
