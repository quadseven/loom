"""One read of each issue per status or resume pass (loom#2057, loom#2078),
and one per-pass view of GitHub state owning reads, deadlines, and the
request budget (loom#2458).

Status reads a lane pull request's ticket in several helpers (the merge
refusals, the docs override, the goal-home rules) and the ticket's epic and
goal home once per pull request. The answers do not change within one pass,
so this client answers a repeated `GET /repos/{owner}/{name}/issues/{n}` from
the first read and sends every other call on unchanged. A failed read is not
kept: the next helper reads again and fails or succeeds as it did before.

A pass also lists the repo's issues first, and GitHub's listing carries the
same issue object a single read answers. So an issue a listing already holds
is answered from it, and so is the single read a ticket's first comments page
makes for its body's status section (GitHub._with_body_events). An issue
listing, and an issue's comments, events or sub-issues, are read once per
pass too. On 2026-10-27 status for quadseven/loom made 270 reads, 95 of them
single-issue reads of issues it had already listed (loom#2078).

PassView extends this into the single per-pass view of GitHub state that
owns reads, deadlines, and the request budget (loom#2458).
"""
from __future__ import annotations

import faulthandler
import json
import os
import re
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .github import GitHub, GitHubError, Response

# A single issue, not its comments, events, sub-issues or a listing.
ISSUE_PATH = re.compile(r"^/repos/[^/]+/[^/]+/issues/\d+$")
# An issue listing: its items are whole issues (pull requests included).
LISTING = re.compile(r"^/repos/([^/]+/[^/]+)/issues\?")
# The paginated reads kept for the pass: a listing, or one issue's comments,
# events or sub-issues.
KEPT_LIST = re.compile(
    r"^/repos/[^/]+/[^/]+/issues(?:\?|/\d+/(?:comments|events|sub_issues)(?:\?|$))")

PASS_DEADLINE = 3600
DEADLINE_MARKER_FILE = "pass-deadline.json"
PASS_REQUEST_TARGET = 500
BUDGET_FLOOR = 0.15
CACHE_KEEP = 24 * 3600


def _with_per_page(path: str) -> str:
    """The path GitHub.paginate reads, so two spellings share one answer."""
    if "per_page=" in path:
        return path
    return path + ("&" if "?" in path else "?") + "per_page=100"


def _read_json(path: str, default: Any = None) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, OSError, ValueError):
        return default


def _write_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def _discard(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _utc(at: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(at))


def _seconds(raw: str, default: int) -> int:
    """A whole number of seconds of at least 60, else default."""
    raw = raw.strip()
    return int(raw) if raw.isdigit() and int(raw) >= 60 else default


def pass_deadline(raw: str) -> int:
    """LOOM_PASS_DEADLINE: 0 turns the deadline off; a whole number of
    seconds of at least 60 is used; anything else is PASS_DEADLINE."""
    raw = str(raw).strip()
    if raw == "0":
        return 0
    if raw:
        value = _seconds(raw, 0)
        if value:
            return value
        print("ignoring LOOM_PASS_DEADLINE %r: not a whole number of seconds, at least 60"
              % raw, file=sys.stderr)
    return PASS_DEADLINE


def deadline_stream() -> Any:
    """The stream faulthandler writes to: the station log (stderr, as launchd
    runs the station), or the process's own stderr when sys.stderr has no
    file descriptor (a test capturing it)."""
    try:
        sys.stderr.fileno()
        return sys.stderr
    except (AttributeError, OSError, ValueError):
        return sys.__stderr__


def _arm_path(state_dir: str) -> str:
    if not state_dir:
        return ""
    folder = state_dir if os.path.basename(state_dir) == "station" else os.path.join(state_dir, "station")
    return os.path.join(folder, DEADLINE_MARKER_FILE)


def arm_deadline(state_dir: str, seconds: int, now: float,
                 log: Optional[Callable[[str], None]] = None) -> Optional[str]:
    """Say whether the last pass overran, then arm this pass's deadline and
    write its marker. Returns the marker's path, or None when off."""
    say = log or (lambda msg: print(msg, file=sys.stderr))
    path = _arm_path(state_dir)
    if path:
        last = _read_json(path, None)
        if isinstance(last, dict):
            started = last.get("started")
            limit = last.get("deadline")
            if isinstance(started, (int, float)) and isinstance(limit, int):
                ran = int(now - started)
                if ran >= limit:
                    say("the last pass (pid %s, started %s) overran its %ds deadline: its stack is "
                        "in this log above" % (last.get("pid"), time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)), limit))
                else:
                    say("the last pass (pid %s) ended %ds into its %ds deadline without a clean "
                        "finish (killed or crashed)" % (last.get("pid"), ran, limit))
    if seconds <= 0:
        if path:
            _discard(path)
        return None
    if path:
        _write_json(path, {"pid": os.getpid(), "started": now, "deadline": seconds})
    faulthandler.dump_traceback_later(seconds, exit=True, file=deadline_stream())
    return path


def try_arm_deadline(state_dir: str, seconds: int, now: float,
                     log: Optional[Callable[[str], None]] = None) -> Optional[str]:
    """arm_deadline, but a failure to arm is logged and the pass runs unarmed."""
    say = log or (lambda msg: print(msg, file=sys.stderr))
    try:
        return arm_deadline(state_dir, seconds, now, say)
    except Exception as err:
        say("could not arm the pass deadline (%s: %s); this pass runs without one"
            % (type(err).__name__, err))
        try:
            faulthandler.cancel_dump_traceback_later()
            path = _arm_path(state_dir)
            if path:
                _discard(path)
        except OSError as cleanup:
            say("could not clear the pass deadline marker: %s" % cleanup)
        return None


def disarm_deadline(path: Optional[str]) -> None:
    """Cancel this pass's deadline and remove its marker."""
    faulthandler.cancel_dump_traceback_later()
    if path:
        _discard(path)


class IssueReads:
    """A GitHub client for one read-only pass that reads each issue once.

    Status helpers may run on several threads; a second reader of the same
    issue or list waits for the first one's answer instead of sending its
    own."""

    def __init__(self, gh: Any) -> None:
        self._gh = gh
        self._lock = threading.Lock()
        self._path_locks: Dict[str, threading.Lock] = {}
        self._answers: Dict[str, Response] = {}
        self._lists: Dict[str, List[Any]] = {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._gh, name)

    def _path_lock(self, path: str) -> threading.Lock:
        with self._lock:
            return self._path_locks.setdefault(path, threading.Lock())

    def _issue(self, path: str) -> Response:
        """The issue at `path`, read at most once."""
        with self._path_lock(path):
            with self._lock:
                kept = self._answers.get(path)
            if kept is None:
                kept = self._gh.rest("GET", path)
                with self._lock:
                    self._answers[path] = kept
            return kept

    def _issue_or_failure(self, path: str, ok: Any) -> Response:
        """A read that accepts a failure status (ok=(404,)): answered from a
        kept answer, and its own answer kept when it is a success. A failure
        is never kept, so a later read that does not accept it still raises.
        Resume reads each handoff item this way (item_revision), and an item
        named by several goals' journals was read once per goal: 21 of 103
        single reads on 2026-11-02 (loom#2483)."""
        with self._path_lock(path):
            with self._lock:
                kept = self._answers.get(path)
            if kept is not None:
                return kept
            answer = self._gh.rest("GET", path, None, None, ok)
            if answer.status == 200:
                with self._lock:
                    self._answers[path] = answer
            return answer

    def rest(self, method: str, path: str, body: Any = None,
             etag: Any = None, ok: Any = ()) -> Response:
        plain = method == "GET" and body is None and etag is None and not ok
        if plain and ISSUE_PATH.match(path):
            return self._issue(path)
        if method == "GET" and body is None and etag is None and ISSUE_PATH.match(path):
            return self._issue_or_failure(path, ok)
        if plain and isinstance(self._gh, GitHub):
            # A comments page reads its issue's body from this pass's answers.
            return self._gh.rest(method, path, issue=self._issue)
        return self._gh.rest(method, path, body, etag, ok)

    def paginate(self, path: str) -> List[Any]:
        key = _with_per_page(path)
        if not KEPT_LIST.match(key):
            return self._gh.paginate(path)
        with self._path_lock(key):
            with self._lock:
                kept = self._lists.get(key)
            if kept is None:
                kept = self._read_list(path)
                self._keep_listed(key, kept)
                with self._lock:
                    self._lists[key] = kept
            return list(kept)

    def _read_list(self, path: str) -> List[Any]:
        if isinstance(self._gh, GitHub):
            # GitHub's own paginate, its first page read through this
            # client's rest, so a comments page reads its issue once.
            return GitHub.paginate(self, path)  # type: ignore[arg-type]
        return self._gh.paginate(path)

    def recall(self, path: str, validator: str) -> Optional[List[Any]]:
        """GitHub.recall, for GitHub itself; a stand-in keeps nothing."""
        if isinstance(self._gh, GitHub):
            return self._gh.recall(_with_per_page(path), validator)
        return None

    def remember(self, path: str, validator: str, items: List[Any]) -> None:
        if isinstance(self._gh, GitHub):
            self._gh.remember(_with_per_page(path), validator, items)

    def keep_listed(self, repo: str, items: List[Any]) -> None:
        """Answer each item's single read from a listing of the repo's
        issues read without this client (bus.needs_you reads the pages on
        its own threads, loom#2394)."""
        self._keep_listed("/repos/%s/issues?per_page=100" % repo, items)

    def _keep_listed(self, key: str, items: List[Any]) -> None:
        """Answer each listed issue's single read from the listing. Only for
        GitHub itself, whose listing and single read return the same issue
        object; a stand-in client promises no such thing."""
        found = LISTING.match(key)
        if not found or not isinstance(self._gh, GitHub):
            return
        for item in items:
            number = item.get("number") if isinstance(item, dict) else None
            if not isinstance(number, int) or isinstance(number, bool):
                continue
            path = "/repos/%s/issues/%d" % (found.group(1), number)
            with self._lock:
                self._answers.setdefault(path, Response(200, item))


class PassView(IssueReads):
    """A per-pass view of GitHub state owning reads, deadlines, and the request budget."""

    def __init__(
        self,
        gh: Any,
        state_dir: str = "",
        now: Optional[float] = None,
        deadline: int = 0,
        target: Optional[int] = None,
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        super().__init__(gh)
        self.state_dir = state_dir
        self.now = time.time() if now is None else float(now)
        self.deadline = deadline
        self._target = target
        self._log = log or (lambda msg: print(msg, file=sys.stderr))
        self._issue_stamps: Dict[Tuple[str, int], str] = {}
        self._ticket_reads_path = (
            os.path.join(state_dir, "ticket_reads.json") if state_dir else ""
        )
        saved_reads = (
            _read_json(self._ticket_reads_path, {})
            if self._ticket_reads_path
            else {}
        )
        self._ticket_reads: Dict[str, Any] = (
            saved_reads if isinstance(saved_reads, dict) else {}
        )
        self._budget_path = (
            os.path.join(state_dir, "budget.json") if state_dir else ""
        )
        saved_budget = (
            _read_json(self._budget_path, {}) if self._budget_path else {}
        )
        self._budget: Dict[str, Any] = (
            saved_budget if isinstance(saved_budget, dict) else {}
        )
        self._told_low = False
        self._deadline_marker: Optional[str] = None
        if target is not None and hasattr(self._gh, "target") and getattr(self._gh, "target", None) is None:
            self._gh.target = target

    def __setattr__(self, name: str, value: Any) -> None:
        internal = (
            "state_dir", "now", "deadline", "target", "_gh", "_lock",
            "_path_locks", "_answers", "_lists", "_target", "_log",
            "_issue_stamps", "_ticket_reads_path", "_ticket_reads",
            "_budget_path", "_budget", "_told_low", "_deadline_marker",
        )
        if name.startswith("_") or name in internal:
            super().__setattr__(name, value)
        elif hasattr(self, "_gh") and hasattr(self._gh, name):
            setattr(self._gh, name, value)
        else:
            super().__setattr__(name, value)

    def remember_issues(self, repo: str, items: Any) -> None:
        """Record the updated_at timestamp for each issue, and keep in single read cache."""
        if not isinstance(items, list):
            return
        for issue in items:
            if not isinstance(issue, dict) or not isinstance(issue.get("updated_at"), str):
                continue
            try:
                number = int(issue["number"])
            except (KeyError, TypeError, ValueError):
                continue
            self._issue_stamps[(repo, number)] = issue["updated_at"]
        self.keep_listed(repo, items)

    def issue(self, repo: str, number: int) -> Dict[str, Any]:
        """Read an issue at most once, remembering its stamp and setting repository_url."""
        resp = self.rest("GET", "/repos/%s/issues/%d" % (repo, number))
        data = resp.data
        if not isinstance(data, dict):
            raise GitHubError("no issue %s#%d" % (repo, number))
        self.remember_issues(repo, [data])
        data.setdefault("repository_url", "%s/repos/%s" % (
            getattr(self._gh, "api", "https://api.github.com"), repo))
        return data

    def seed_issue(self, path: str, issue: Dict[str, Any]) -> None:
        """Pre-seed an issue response for testing."""
        if not path.startswith("/"):
            path = "/" + path
        with self._lock:
            self._answers[path] = Response(200, issue)
        m = re.match(r"^/repos/([^/]+/[^/]+)/issues/(\d+)$", path)
        if m and isinstance(issue, dict) and isinstance(issue.get("updated_at"), str):
            self._issue_stamps[(m.group(1), int(m.group(2)))] = issue["updated_at"]

    def seed_list(self, path: str, items: List[Any]) -> None:
        """Pre-seed a listing response for testing."""
        key = _with_per_page(path)
        with self._lock:
            self._lists[key] = list(items)
        self._keep_listed(key, items)
        found = LISTING.match(key)
        if found:
            self.remember_issues(found.group(1), items)

    def paginate(self, path: str) -> List[Any]:
        issue_list = re.match(r"^/repos/([^/]+/[^/]+)/issues\?", path)
        if issue_list:
            items = super().paginate(path)
            self.remember_issues(issue_list.group(1), items)
            return items
        detail = re.match(r"^/repos/([^/]+/[^/]+)/issues/(\d+)/(comments|events)(?:\?.*)?$", path)
        if detail is None or ("?" in path and "since=" in path):
            return super().paginate(path)
        repo, number_text, kind = detail.groups()
        number = int(number_text)
        stamp = self._issue_stamps.get((repo, number))
        issue_writes = getattr(self._gh, "issue_writes", ())
        comment_edits = getattr(self._gh, "comment_edits", False)
        if not stamp or not hasattr(self._gh, "issue_writes") or comment_edits or (repo, number) in issue_writes:
            return super().paginate(path)
        key = "%s#%d/%s" % (repo, number, kind)
        kept = self._ticket_reads.get(key)
        if isinstance(kept, dict) and kept.get("stamp") == stamp and isinstance(kept.get("data"), list):
            kept["seen"] = self.now
            with self._lock:
                self._lists[_with_per_page(path)] = kept["data"]
            return list(kept["data"])
        carried_before = dict(getattr(self._gh, "carried_over", {}))
        items = super().paginate(path)
        carried_after = getattr(self._gh, "carried_over", {})
        if carried_before == carried_after:
            self._ticket_reads[key] = {"stamp": stamp, "data": items, "seen": self.now}
        return items

    def arm_deadline(self, seconds: Optional[int] = None) -> Optional[str]:
        sec = self.deadline if seconds is None else seconds
        self._deadline_marker = arm_deadline(self.state_dir, sec, self.now, self._log)
        return self._deadline_marker

    def try_arm_deadline(self, seconds: Optional[int] = None) -> Optional[str]:
        sec = self.deadline if seconds is None else seconds
        self._deadline_marker = try_arm_deadline(self.state_dir, sec, self.now, self._log)
        return self._deadline_marker

    def disarm_deadline(self) -> None:
        disarm_deadline(self._deadline_marker)
        self._deadline_marker = None

    def deadline_passed(self) -> bool:
        return self.deadline > 0 and (time.time() - self.now) >= self.deadline

    def time_remaining(self) -> float:
        return max(0.0, self.deadline - (time.time() - self.now)) if self.deadline > 0 else float("inf")

    @property
    def target(self) -> Optional[int]:
        return getattr(self._gh, "target", self._target)

    @target.setter
    def target(self, val: Optional[int]) -> None:
        self._target = val
        if hasattr(self._gh, "target"):
            self._gh.target = val

    @property
    def counted(self) -> int:
        counted = getattr(self._gh, "counted", None)
        if counted is None:
            counted = getattr(self._gh, "requests", 0) - getattr(self._gh, "not_modified", 0)
        return int(counted)

    @property
    def budget_state(self) -> Dict[str, Any]:
        return self._budget

    def rates(self) -> Dict[str, Tuple[int, int, int]]:
        rates: Dict[str, Tuple[int, int, int]] = {}
        saved = self._budget.get("rates")
        for resource, value in (saved.items() if isinstance(saved, dict) else ()):
            if (isinstance(value, list) and len(value) == 3
                    and all(isinstance(v, int) and not isinstance(v, bool) for v in value)):
                rates[str(resource)] = (value[0], value[1], value[2])
        rates.update(getattr(self._gh, "rate", {}))
        return {r: v for r, v in rates.items() if v[1] > 0 and v[2] > self.now}

    def budget_low(self) -> Optional[Tuple[str, int, int, int]]:
        low = [(v[0] / v[1], r) + v for r, v in self.rates().items()
               if v[0] < BUDGET_FLOOR * v[1]]
        return min(low)[1:] if low else None

    def target_reached(self) -> bool:
        tgt = self.target
        if not isinstance(tgt, int) or isinstance(tgt, bool):
            return False
        return self.counted >= tgt

    def deferred(self) -> bool:
        if self.target_reached():
            return True
        low = self.budget_low()
        if low is None:
            return False
        resource, remaining, limit, reset = low
        if not self._told_low and self._budget.get("told") != [resource, reset]:
            self._log("GitHub %s budget low (%d of %d left, resets %s): claims and "
                      "reports only until then" % (resource, remaining, limit, _utc(reset)))
        self._budget["told"] = [resource, reset]
        self._told_low = True
        return True

    def budget_exhausted(self) -> bool:
        kept = self._budget.get("limited_until")
        until = getattr(self._gh, "limited_until", None) or (kept if isinstance(kept, int) else None)
        if until is None or until <= self.now:
            return False
        self._gh.limited_until = until
        self._log("GitHub budget exhausted until %s: this pass waits for the reset"
                  % _utc(until))
        return True

    def end_pass(self, log: bool = True) -> None:
        if log:
            self._log_summary()
        self._budget["rates"] = {r: list(v) for r, v in self.rates().items()}
        limited_until = getattr(self._gh, "limited_until", None)
        if limited_until is not None and limited_until > self.now:
            self._budget["limited_until"] = int(limited_until)
        else:
            self._budget.pop("limited_until", None)
        if self.budget_low() is None:
            self._budget.pop("told", None)
        self._save_state()

    def _log_summary(self) -> None:
        gh = self._gh
        requests = getattr(gh, "requests", 0)
        not_modified = getattr(gh, "not_modified", 0)
        kinds_dict = getattr(gh, "kinds", {})
        kinds = ", ".join("%s %d" % (k, n) for k, n in
                          sorted(kinds_dict.items(), key=lambda kv: (-kv[1], kv[0])))
        line = "requests: %d, %d counted (%s), %d answered 304 (not counted)" % (
            requests, requests - not_modified, kinds or "none", not_modified)
        counted = requests - not_modified
        tgt = self.target or PASS_REQUEST_TARGET
        line += "; target %d counted (%s)" % (
            tgt, "over" if counted > tgt else "met")
        carried_dict = getattr(gh, "carried_over", {})
        carried = ", ".join("%s %d" % (k, n) for k, n in
                            sorted(carried_dict.items(), key=lambda kv: (-kv[1], kv[0])))
        if carried:
            line += "; carried over %s" % carried
        rates = self.rates()
        if "core" in rates:
            remaining, limit, reset = rates["core"]
            auth = getattr(gh, "auth", "")
            line += "; %sbudget %d of %d left, resets %s" % (
                "GitHub App " if auth == "app" else "", remaining, limit, _utc(reset))
        self._log(line)

    def _save_state(self) -> None:
        if not self.state_dir:
            return
        gh = self._gh
        try:
            _write_json(os.path.join(self.state_dir, "budget.json"), self._budget)
            if getattr(gh, "comment_edits", False):
                self._ticket_reads.clear()
            for repo, number in getattr(gh, "issue_writes", ()):
                for kind in ("comments", "events"):
                    self._ticket_reads.pop("%s#%d/%s" % (repo, number, kind), None)
            pruned = {
                key: value for key, value in self._ticket_reads.items()
                if isinstance(value, dict) and isinstance(value.get("seen"), (int, float))
                and 0 <= self.now - value["seen"] < CACHE_KEEP
            }
            self._ticket_reads.clear()
            self._ticket_reads.update(pruned)
            _write_json(os.path.join(self.state_dir, "ticket_reads.json"), self._ticket_reads)
            if hasattr(gh, "prune_cache"):
                gh.prune_cache(CACHE_KEEP, self.now)
        except OSError as err:
            self._log("could not keep the GitHub budget: %s" % err)

    @classmethod
    def memory(
        cls,
        issues: Optional[Dict[str, Any]] = None,
        lists: Optional[Dict[str, List[Any]]] = None,
        rates: Optional[Dict[str, Tuple[int, int, int]]] = None,
        now: Optional[float] = None,
    ) -> PassView:
        from .github import GitHub
        gh = GitHub("token", transport=lambda m, u, h, b: (404, {}, b'{"message": "Not Found"}'))
        view = cls(gh, now=now)
        if rates:
            gh.rate = dict(rates)
        if issues:
            for key, val in issues.items():
                if "#" in key and not key.startswith("/"):
                    repo, num = key.split("#")
                    path = "/repos/%s/issues/%s" % (repo, num)
                else:
                    path = key
                view.seed_issue(path, val)
        if lists:
            for path, items in lists.items():
                view.seed_list(path, items)
        return view


def pass_view(
    gh: Any,
    state_dir: str = "",
    now: Optional[float] = None,
    deadline: int = 0,
    target: Optional[int] = None,
    log: Optional[Callable[[str], None]] = None,
) -> PassView:
    """Return a PassView wrapping `gh`, or `gh` as is if already a PassView."""
    if isinstance(gh, PassView):
        return gh
    return PassView(
        gh,
        state_dir=state_dir,
        now=now,
        deadline=deadline,
        target=target,
        log=log,
    )


def once(gh: Any) -> Any:
    """gh wrapped so each issue is read once; an already wrapped gh as is."""
    return gh if isinstance(gh, IssueReads) else IssueReads(gh)
