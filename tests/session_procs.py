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
import time


def session_pids(sid):
    """Pids of the live processes in session sid, found through the process
    table. Without a readable process table (the lane check sandbox cannot
    run ps), only the session leader's group is known."""
    try:
        out = subprocess.run(["ps", "-axo", "pid="], capture_output=True, text=True,
                             timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    pids = []
    for word in out.split():
        try:
            pid = int(word)
            if pid != os.getpid() and os.getsid(pid) == sid:
                pids.append(pid)
        except (ValueError, OSError):
            continue
    return pids


def kill_session(sid):
    """Kill every process in session sid; return the pids that were left."""
    try:
        os.killpg(sid, signal.SIGKILL)
    except OSError:
        pass
    left = session_pids(sid)
    if not left:
        return []
    for _ in range(3):
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
        if not left:
            break
    return left


def run(argv, timeout, **kw):
    """subprocess.run(argv, capture_output=True, text=True, timeout=timeout),
    with argv in a session of its own that is emptied when it ends."""
    with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                          start_new_session=True, **kw) as proc:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as expired:
            kill_session(proc.pid)
            out, err = proc.communicate()
            raise subprocess.TimeoutExpired(proc.args, timeout, output=out, stderr=err) from expired
        finally:
            kill_session(proc.pid)
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)
