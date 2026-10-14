"""What the process running the tests may do.

A lane's pre-PR check (.loom/validation.conf) runs this suite inside the lane
sandbox that lanes/run_lane.sh builds in check mode. That sandbox cannot start
a sandbox of its own, run ps, or connect to a localhost port, so the tests
that need one skip there. CI runs outside it and fails on any skip, so a
probe that is wrong on CI shows up as a failed CI run, never a hidden test.
"""

import shutil
import socket
import subprocess


def _runs(argv):
    try:
        return subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _connects_to_localhost():
    try:
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            with socket.create_connection(server.getsockname(), timeout=5):
                return True
    except OSError:
        return False


NESTED_SANDBOX = bool(shutil.which("sandbox-exec")) and _runs(
    ["sandbox-exec", "-p", "(version 1)(allow default)", "/usr/bin/true"])
PROCESS_TABLE = _runs(["ps", "-o", "pid=", "-p", "1"])
LOCALHOST = _connects_to_localhost()

NO_NESTED_SANDBOX = "needs a working sandbox-exec: none on this host, or inside the lane check sandbox"
NO_PROCESS_TABLE = "needs ps, which the lane check sandbox cannot run"
NO_LOCALHOST = "needs a localhost connection, which the lane check sandbox denies"
