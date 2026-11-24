"""Checks of a public repo's work commit on GitHub's hosted runners.

A lane on a public repo (LOOM_REMOTE_CHECKS=1) pushes its work commit and asks
for its check to run in .github/workflows/remote-check.yml, so no host check
slot is taken. The lanes' GitHub App cannot dispatch a workflow (only the
station's has actions write, bus/app.py), so the lane and the station talk
through files under the loom home, the way the runner slots do:

- the lane writes state/remote-checks/req/<id>.json (the repo, the commit and
  the check spec the lane resolved for that repo) and polls
  state/remote-checks/res/<id>.json;
- the station's pass (RemoteChecks.tend, called from the station) dispatches
  the workflow on main with its own App, follows only the run id the dispatch
  returned, believes a run only when it ran this workflow file on main as a
  workflow_dispatch by the station's own App (the same provenance
  bus/runners.py demands of a lane run), reads the small result artifact and
  writes the result file.

A result is {"state": "done", "exit": N, "failing": [names]}, or
{"state": "error", "why": ...} for anything the lane must answer by running
the check on the host: a refused request, a failed dispatch, an untrusted or
unreadable run. The lane also gives up on its own bound (docs/LANES.md,
"Remote checks"), so a station that is down costs it the start wait, no more.
"""
from __future__ import annotations

import fcntl
import io
import json
import os
import re
import sys
import threading
import time
import uuid
import zipfile
import zlib
from typing import Any, Callable, Dict, List, Optional

from . import runners
from .github import GitHubError, RateLimited

WORKFLOW = "remote-check.yml"
WORKFLOW_PATH = ".github/workflows/" + WORKFLOW
REF = runners.REF
ARTIFACT_PREFIX = "remote-check-result-"
RESULT_FILE = "result.json"

ID_RE = re.compile(r"[0-9a-f]{32}")
REPO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9._-]{1,100}")
SHA_RE = re.compile(r"[0-9a-f]{40}")
INTERP_RE = re.compile(r"[A-Za-z0-9._+-]{1,64}")
# A failing test's name as a check prints it (tests.mod.Class.test, a pytest
# node id, a Go test): no spaces, no markup a log or a pull request could
# misread.
NAME_RE = re.compile(r"[A-Za-z0-9_.:/\[\]()+,=-]{1,200}")
MAX_NAMES = 20
COMMAND_MAX = 1000
RESULT_MAX = 64 * 1024
# Requests in flight at once, and the age past which a run is given up on.
MAX_IN_FLIGHT = 4
RUN_MAX_SECONDS = 2 * 3600
# A finished request's files are kept this long, then pruned.
KEEP_SECONDS = 24 * 3600


class RemoteCheckError(Exception):
    """A request or a result that cannot be used."""


def root(home: str) -> str:
    return os.path.join(home, "state", "remote-checks")


def request_path(home: str, rid: str) -> str:
    return os.path.join(root(home), "req", rid + ".json")


def result_path(home: str, rid: str) -> str:
    return os.path.join(root(home), "res", rid + ".json")


def _printable(text: Any) -> bool:
    return (isinstance(text, str) and len(text) <= COMMAND_MAX
            and all(" " <= ch <= "~" for ch in text))


def check_request(req: Any) -> Dict[str, str]:
    """The request's fields, or RemoteCheckError. Everything the workflow
    receives is checked here; the workflow runs it with no secrets."""
    if not isinstance(req, dict):
        raise RemoteCheckError("the request is not an object")
    repo, sha = req.get("repo"), req.get("sha")
    if not isinstance(repo, str) or not REPO_RE.fullmatch(repo):
        raise RemoteCheckError("bad repo")
    if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
        raise RemoteCheckError("bad commit")
    interp = req.get("interpreter") or ""
    if interp and not (isinstance(interp, str) and INTERP_RE.fullmatch(interp)):
        raise RemoteCheckError("bad interpreter")
    subs = req.get("submodules") or "no"
    if subs not in ("yes", "no"):
        raise RemoteCheckError("bad submodules")
    setup, check = req.get("setup") or "", req.get("check")
    if not _printable(setup) or not _printable(check) or not check:
        raise RemoteCheckError("bad check spec")
    return {"repo": repo, "sha": sha, "interpreter": interp, "submodules": subs,
            "setup": setup, "check": check}


def _atomic(path: str, data: Dict[str, Any]) -> None:
    runners._write(path, json.dumps(data, sort_keys=True).encode("utf-8"), 0o600)


def submit(home: str, repo: str, sha: str, interpreter: str, submodules: str,
           setup: str, check: str) -> str:
    """The lane's side: file a request and return its id."""
    req = check_request({"repo": repo, "sha": sha, "interpreter": interpreter,
                         "submodules": submodules, "setup": setup, "check": check})
    rid = uuid.uuid4().hex
    _atomic(request_path(home, rid), req)
    return rid


def read_result(home: str, rid: str) -> Optional[Dict[str, Any]]:
    try:
        with open(result_path(home, rid), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def parse_result(raw: bytes) -> Dict[str, Any]:
    """The workflow's result file, held to its small schema: an exit code and
    up to MAX_NAMES failing test names. The check's own code runs on the
    runner, so nothing else it could write is kept."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise RemoteCheckError("the result is not JSON") from None
    code = data.get("exit") if isinstance(data, dict) else None
    if isinstance(code, bool) or not isinstance(code, int) or not 0 <= code <= 255:
        raise RemoteCheckError("the result has no exit code")
    names = data.get("failing")
    if not isinstance(names, list):
        names = []
    kept: List[str] = []
    for name in names:
        if isinstance(name, str) and NAME_RE.fullmatch(name) and name not in kept:
            kept.append(name)
        if len(kept) >= MAX_NAMES:
            break
    return {"exit": code, "failing": kept}


def open_artifact(archive: bytes) -> bytes:
    """The result file inside the artifact zip, bounded."""
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            info = zf.getinfo(RESULT_FILE)
            if info.file_size > RESULT_MAX:
                raise RemoteCheckError("the result is too large")
            with zf.open(info) as fh:
                data = fh.read(RESULT_MAX + 1)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, KeyError, OSError, RuntimeError,
            EOFError, zlib.error, NotImplementedError, ValueError):
        raise RemoteCheckError("the result artifact could not be opened") from None
    if len(data) > RESULT_MAX:
        raise RemoteCheckError("the result is too large")
    return data


class RemoteChecks:
    """One station pass: dispatch the requests the lanes filed, follow the
    runs, write the results."""

    def __init__(self, gh: Any, home: str, repo: str, public: Callable[[str], bool],
                 say: Callable[[str], None], actor: Optional[str] = None,
                 now: Optional[float] = None,
                 fetch: Callable[[str], bytes] = runners._https_fetch) -> None:
        self.gh = gh
        self.home = home
        self.repo = repo          # the repo that holds remote-check.yml (this one)
        self.public = public
        self.say = say
        self.actor = actor        # the station App's login: only its runs are believed
        self.now = time.time() if now is None else now
        self.fetch = fetch
        self.ledger_path = os.path.join(root(home), "runs.json")

    # -- ledger -------------------------------------------------------------
    def load(self) -> Dict[str, Any]:
        try:
            with open(self.ledger_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return {"runs": {}}
        except (OSError, ValueError) as err:
            raise runners.ReportError("cannot read %s: %s" % (self.ledger_path, err)) from None
        if not isinstance(data, dict) or not isinstance(data.get("runs"), dict):
            raise runners.ReportError("%s is not a remote check ledger" % self.ledger_path)
        return data

    def save(self, ledger: Dict[str, Any]) -> None:
        _atomic(self.ledger_path, ledger)

    def _finish(self, ledger: Dict[str, Any], rid: str, result: Dict[str, Any],
                state: str) -> None:
        entry = ledger["runs"][rid]
        entry["state"] = state
        entry["done_at"] = self.now
        _atomic(result_path(self.home, rid), result)
        self.save(ledger)

    def _fail(self, ledger: Dict[str, Any], rid: str, why: str) -> None:
        self.say("remote check %s: %s; the lane runs its check on the host" % (rid[:8], why))
        self._finish(ledger, rid, {"state": "error", "why": why}, "error")

    # -- the pass -----------------------------------------------------------
    def tend(self) -> None:
        """One tending, under a lock on the ledger: the pass's own call and the
        Ticker's never run at once, and one that finds the other running
        leaves the work to it."""
        os.makedirs(root(self.home), exist_ok=True)
        with open(os.path.join(root(self.home), ".tend.lock"), "a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return
            self._tend()

    def _tend(self) -> None:
        directory = os.path.join(root(self.home), "req")
        try:
            names = sorted(n for n in os.listdir(directory) if n.endswith(".json"))
        except OSError:
            names = []
        ledger = self.load()
        if not names and not ledger["runs"]:
            return
        if not self.actor:
            if names:
                self.say("remote checks need the station's GitHub App, whose runs alone are "
                         "believed; nothing dispatched")
            return
        for rid in sorted(ledger["runs"]):
            if ledger["runs"][rid].get("state") == "dispatched":
                self._follow(ledger, rid)
        for name in names:
            rid = name[:-5]
            if not ID_RE.fullmatch(rid) or rid in ledger["runs"]:
                continue
            if sum(1 for e in ledger["runs"].values() if e.get("state") == "dispatched") >= MAX_IN_FLIGHT:
                break    # the request waits for the next pass
            self._dispatch(ledger, rid, os.path.join(directory, name))
        self._prune(ledger)
        self.save(ledger)

    # -- dispatch -----------------------------------------------------------
    def _dispatch(self, ledger: Dict[str, Any], rid: str, path: str) -> None:
        entry: Dict[str, Any] = {"at": self.now}
        ledger["runs"][rid] = entry
        try:
            with open(path, encoding="utf-8") as fh:
                req = check_request(json.load(fh))
        except (OSError, ValueError, RemoteCheckError) as err:
            self._fail(ledger, rid, "unusable request (%s)" % type(err).__name__)
            return
        if not self.public(req["repo"]):
            self._fail(ledger, rid, "%s is not a public repo" % req["repo"])
            return
        entry.update({"repo": req["repo"], "sha": req["sha"], "state": "dispatched"})
        # Recorded before the request, as bus/runners.py does: a request that
        # lands with its answer lost must never be sent twice.
        self.save(ledger)
        inputs = {"repo": req["repo"], "sha": req["sha"], "run_id": rid,
                  "interpreter": req["interpreter"], "submodules": req["submodules"],
                  "setup": req["setup"], "check": req["check"]}
        try:
            resp = self.gh.rest("POST", "/repos/%s/actions/workflows/%s/dispatches"
                                % (self.repo, WORKFLOW),
                                {"ref": REF, "inputs": inputs, "return_run_details": True})
        except RateLimited:
            del ledger["runs"][rid]
            self.save(ledger)
            raise
        except GitHubError as err:
            self._fail(ledger, rid, "the dispatch failed (HTTP %s)" % (getattr(err, "status", 0) or "no answer"))
            return
        data = resp.data if isinstance(resp.data, dict) else {}
        run = data.get("workflow_run_id")
        if not isinstance(run, int) or isinstance(run, bool):
            self._fail(ledger, rid, "the dispatch returned no run id")
            return
        entry["run"] = run
        self.save(ledger)
        self.say("dispatched remote check %s (run id %s)" % (rid[:8], run))

    # -- follow -------------------------------------------------------------
    def provenance(self, run: Dict[str, Any]) -> str:
        """Why a run is not one the station dispatched; empty when it is."""
        if run.get("head_branch") != REF:
            return "it ran on %r, not %s" % (str(run.get("head_branch"))[:60], REF)
        if str(run.get("path") or "").split("@", 1)[0] != WORKFLOW_PATH:
            return "it ran another workflow file"
        if run.get("event") != "workflow_dispatch":
            return "it was not a workflow_dispatch"
        actor = run.get("triggering_actor")
        login = actor.get("login") if isinstance(actor, dict) else None
        if not self.actor or login != self.actor:
            return "it was triggered by %r, not the station's App" % str(login)[:60]
        return ""

    def _follow(self, ledger: Dict[str, Any], rid: str) -> None:
        entry = ledger["runs"][rid]
        run_id = entry.get("run")
        if not isinstance(run_id, int):
            self._fail(ledger, rid, "no run id to follow")
            return
        run = self.gh.rest("GET", "/repos/%s/actions/runs/%d" % (self.repo, run_id)).data
        if not isinstance(run, dict):
            return
        why = self.provenance(run)
        if why:
            self._fail(ledger, rid, "run %s is not this station's: %s" % (run_id, why))
            return
        if run.get("status") != "completed":
            if self.now - float(entry.get("at") or self.now) > RUN_MAX_SECONDS:
                self._fail(ledger, rid, "run %s did not finish in time" % run_id)
            return
        conclusion = run.get("conclusion")
        if conclusion not in ("success", "failure"):
            self._fail(ledger, rid, "run %s ended %s" % (run_id, str(conclusion)[:30]))
            return
        try:
            result = parse_result(open_artifact(self._artifact(rid, run_id)))
        except (RemoteCheckError, runners.ReportError) as err:
            self._fail(ledger, rid, "run %s: %s" % (run_id, err))
            return
        # The run's conclusion and the artifact must agree: a green run with a
        # failing exit, or a red one that reports success, answers nothing.
        if (conclusion == "success") != (result["exit"] == 0):
            self._fail(ledger, rid, "run %s: its result does not match its conclusion" % run_id)
            return
        result["state"] = "done"
        result["sha"] = entry.get("sha", "")
        self._finish(ledger, rid, result, "done")

    def _artifact(self, rid: str, run_id: int) -> bytes:
        data = self.gh.rest("GET", "/repos/%s/actions/runs/%d/artifacts" % (self.repo, run_id)).data
        artifacts = data.get("artifacts") if isinstance(data, dict) else None
        name = ARTIFACT_PREFIX + rid
        found = next((a for a in artifacts or [] if isinstance(a, dict)
                      and a.get("name") == name and not a.get("expired")), None)
        if found is None or isinstance(found.get("id"), bool) or not isinstance(found.get("id"), int):
            raise RemoteCheckError("no result artifact")
        resp = self.gh.rest("GET", "/repos/%s/actions/artifacts/%d/zip" % (self.repo, found["id"]),
                            ok=(302,))
        return self.fetch(str((resp.headers or {}).get("location") or ""))

    # -- housekeeping -------------------------------------------------------
    def _prune(self, ledger: Dict[str, Any]) -> None:
        for rid, entry in list(ledger["runs"].items()):
            done = entry.get("done_at")
            if entry.get("state") != "dispatched" and isinstance(done, (int, float)) \
                    and self.now - done > KEEP_SECONDS:
                for path in (request_path(self.home, rid), result_path(self.home, rid)):
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
                del ledger["runs"][rid]


TICK_SECONDS = 60


class Ticker(threading.Thread):
    """Tends remote checks on their own cadence, whatever step the station's
    pass is in, so a finished run reaches its lane within a tick and not a
    pass later. `make` returns a fresh RemoteChecks (its own GitHub client:
    the pass's is not shared across threads). Every rule stays in
    RemoteChecks.tend; a failed tick is logged and the next one retries."""

    def __init__(self, make: Callable[[], "RemoteChecks"], say: Callable[[str], None],
                 interval: float = TICK_SECONDS) -> None:
        super().__init__(name="remote-checks", daemon=True)
        self.make = make
        self.say = say
        self.interval = interval
        self.stopped = threading.Event()

    def tick(self) -> None:
        try:
            self.make().tend()
        except RateLimited:
            # Visible, so a rate-limited station does not look idle; the next tick retries.
            self.say("remote check tick skipped: GitHub rate limit; the next tick retries")
        except (GitHubError, OSError, runners.ReportError) as err:
            self.say("remote check tick failed: %s" % err)

    def run(self) -> None:
        while not self.stopped.is_set():
            self.tick()
            self.stopped.wait(self.interval)

    def stop(self) -> None:
        self.stopped.set()


def main(argv: List[str], home: str, env: Optional[Dict[str, str]] = None) -> int:
    """The lane's command line. `wait REPO SHA CHECK_FILE START WAIT POLL`
    files a request (CHECK_FILE: interpreter, submodules, setup and check, one
    per line) and waits. Prints one line: "green", "red NAME..." or "none
    WHY"; exit 0, 1 or 2 (no answer: run the check on the host)."""
    if len(argv) != 7 or argv[0] != "wait":
        print("none usage", flush=True)
        return 2
    _, repo, sha, spec_file, start, wait, poll = argv
    try:
        with open(spec_file, encoding="utf-8") as fh:
            lines = fh.read().split("\n")
        rid = submit(home, repo, sha, lines[0], lines[1], lines[2], lines[3])
        start_s, wait_s, poll_s = float(start), float(wait), float(poll)
    except (OSError, IndexError, ValueError, RemoteCheckError) as err:
        print("none %s" % type(err).__name__, flush=True)
        return 2
    began = time.monotonic()
    seen = False
    while True:
        res = read_result(home, rid)
        if res is not None:
            if res.get("state") == "done" and isinstance(res.get("exit"), int):
                if res["exit"] == 0:
                    print("green", flush=True)
                    return 0
                print(" ".join(["red"] + [n for n in res.get("failing") or []
                                           if isinstance(n, str) and NAME_RE.fullmatch(n)]), flush=True)
                return 1
            print("none %s" % str(res.get("why") or "no answer")[:80].replace("\n", " "), flush=True)
            return 2
        seen = seen or os.path.exists(os.path.join(root(home), "runs.json")) and \
            _in_ledger(home, rid)
        elapsed = time.monotonic() - began
        if not seen and elapsed >= start_s:
            print("none the station did not pick up the request", flush=True)
            return 2
        if elapsed >= wait_s:
            print("none no result in time", flush=True)
            return 2
        time.sleep(poll_s)


def _in_ledger(home: str, rid: str) -> bool:
    try:
        with open(os.path.join(root(home), "runs.json"), encoding="utf-8") as fh:
            return rid in (json.load(fh).get("runs") or {})
    except (OSError, ValueError, AttributeError):
        return False


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:], os.environ.get("LOOM_HOME") or os.path.expanduser("~/loom")))
