"""Run a command so that nothing it starts outlives it (loom#2438).

subprocess.run's timeout kills only the process it started. lanes/run_lane.sh
puts its agent and the agent's watchdog in process groups of their own
(job control), so a timed-out test left the watchdog, its tail -f and its grep
running for hours, each holding the test's cwd. run() starts the command in a
session of its own and, when it ends or times out, kills every process still
in that session: a new process group stays in its session, and only setsid
leaves it, which nothing under test does.
"""

import os
import signal
import subprocess
import sys
import tempfile
import time


# The highest pid scanned when ps cannot run: macOS pids stay below 100000,
# and Linux's default pid_max is 32768 (4194304 when raised; /proc covers it).
PID_SCAN = 100000


def _in_session(pid, sid):
    try:
        return pid != os.getpid() and os.getsid(pid) == sid
    except OSError:
        return False


def session_pids(sid):
    """Pids of the live processes in session sid, from the process table, or
    without one (the lane check sandbox cannot run ps) from /proc or a scan of
    every pid with getsid. None when no way could tell."""
    try:
        out = subprocess.run(["ps", "-axo", "pid="], capture_output=True, text=True,
                             timeout=30)
        if out.returncode == 0 and out.stdout.split():
            return [int(w) for w in out.stdout.split() if w.isdigit() and _in_session(int(w), sid)]
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        pids = [int(n) for n in os.listdir("/proc") if n.isdigit()]
    except OSError:
        pids = range(1, PID_SCAN)
    try:
        os.getsid(os.getpid())
    except OSError:
        return None
    return [pid for pid in pids if _in_session(pid, sid)]


def kill_session(sid):
    """Kill every process in session sid. Returns the pids still left, or
    None when the session's processes could not be listed."""
    try:
        os.killpg(sid, signal.SIGKILL)
    except OSError:
        pass
    left = session_pids(sid)
    if left is None:
        return None
    for _ in range(3):
        if not left:
            break
        groups = set()
        for pid in left:
            try:
                groups.add(os.getpgid(pid))
            except OSError:
                continue
        for group in groups:
            try:
                os.killpg(group, signal.SIGKILL)
            except OSError:
                pass
        for pid in left:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        time.sleep(0.1)
        left = session_pids(sid) or []
    return left


def _check_emptied(sid):
    """Kill the session; say on stderr when it could not be emptied, so a
    leak is never silent."""
    left = kill_session(sid)
    if left is None:
        print("session_procs: cannot list session %d's processes; any it left run on" % sid,
              file=sys.stderr)
    elif left:
        print("session_procs: session %d still has processes %s" % (sid, left), file=sys.stderr)


def run(argv, timeout, **kw):
    """subprocess.run(argv, capture_output=True, text=True, timeout=timeout),
    with argv in a session of its own that is emptied when it ends. Output
    goes through files, not pipes, so a process that outlives argv and holds
    its stdout cannot hold the run open until the timeout."""
    with tempfile.TemporaryFile("w+") as out, tempfile.TemporaryFile("w+") as err:
        proc = subprocess.Popen(argv, stdout=out, stderr=err, text=True,
                                start_new_session=True, **kw)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired as expired:
            _check_emptied(proc.pid)
            proc.wait()
            out.seek(0)
            err.seek(0)
            raise subprocess.TimeoutExpired(proc.args, timeout, output=out.read(),
                                            stderr=err.read()) from expired
        finally:
            _check_emptied(proc.pid)
        out.seek(0)
        err.seek(0)
        return subprocess.CompletedProcess(proc.args, proc.returncode, out.read(), err.read())
