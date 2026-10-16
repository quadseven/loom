"""Daily measurements of Loom's nine factory outcomes."""
from __future__ import annotations

import os
import re
import statistics
import subprocess
import sys
import argparse
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List, Sequence

from . import audit, proof
from .github import GitHub, GitHubError, token_from_env
from .factory import lacks_test
from .labels import LANES

WAIT_STATES = ("queued", "briefed", "running", "pr", "failed", "merged", "shipped", "verified")
WAIT_WINDOW = 7 * 86400


def ticket_waits(events: Sequence[Dict[str, Any]], now: float) -> Dict[str, float]:
    """Return hours spent in each lifecycle state, including the current state."""
    waits: Dict[str, float] = {}
    current: str | None = None
    entered: float | None = None
    for event in sorted(events, key=lambda item: stamp(item.get("created_at"))):
        kind = event.get("event")
        label = (event.get("label") or {}).get("name")
        state = label[5:] if isinstance(label, str) and label.startswith("loom:") else None
        if state not in WAIT_STATES:
            continue
        at = stamp(event.get("created_at"))
        if kind == "labeled":
            if current is not None and entered is not None:
                waits[current] = waits.get(current, 0.0) + max(0.0, at - entered) / 3600
            current, entered = state, at
        elif kind == "unlabeled" and state == current:
            if entered is not None:
                waits[current] = waits.get(current, 0.0) + max(0.0, at - entered) / 3600
            current, entered = None, None
    if current is not None and entered is not None:
        waits[current] = waits.get(current, 0.0) + max(0.0, now - entered) / 3600
    return waits


def summarize_waits(tickets: Sequence[tuple[str, Dict[str, float]]]) -> tuple[List[Dict[str, Any]], str | None]:
    """Summarize per-ticket hour maps and name the eligible median bottleneck."""
    rows: List[Dict[str, Any]] = []
    for state in WAIT_STATES:
        samples = [(ticket, float(values[state])) for ticket, values in tickets if state in values]
        if not samples:
            rows.append({"state": state, "median_h": None, "worst_h": None,
                         "worst_ticket": None, "n": 0})
            continue
        worst_ticket, worst_h = max(samples, key=lambda pair: pair[1])
        rows.append({"state": state, "median_h": statistics.median(v for _, v in samples),
                     "worst_h": worst_h, "worst_ticket": worst_ticket, "n": len(samples)})
    candidates = [row for row in rows if row["n"] >= 3]
    bottleneck = max(candidates, key=lambda row: row["median_h"])["state"] if candidates else None
    return rows, bottleneck


def waits_for_repos(gh: GitHub, repos: Sequence[str], now: float) -> tuple[List[Dict[str, Any]], str | None]:
    tickets: List[tuple[str, Dict[str, float]]] = []
    since = datetime.fromtimestamp(now - WAIT_WINDOW, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for repo in repos:
        issues = gh.paginate("/repos/%s/issues?state=all&labels=loom%%3Aticket&since=%s&per_page=100" % (repo, since))
        for issue in issues:
            if not isinstance(issue, dict) or "pull_request" in issue:
                continue
            if stamp(issue.get("updated_at")) < now - WAIT_WINDOW:
                continue
            number = int(issue["number"])
            events = gh.paginate("/repos/%s/issues/%d/events?per_page=100" % (repo, number))
            tickets.append(("%s#%d" % (repo, number), ticket_waits(events, now)))
    return summarize_waits(tickets)


def waits_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m bus.metrics")
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("waits", help="show time spent in each ticket state")
    command.add_argument("repos", nargs="+")
    args = parser.parse_args(argv)
    try:
        home = os.environ.get("LOOM_HOME") or os.path.join(os.path.expanduser("~"), "loom")
        gh = GitHub(token_from_env(), cache_dir=os.path.join(home, "state", "github-cache"))
        rows, bottleneck = waits_for_repos(gh, args.repos, time.time())
        print("state       median_h  worst_h  worst_ticket  n")
        for row in rows:
            print("%-11s %8s %7s  %-12s %d" % (
                row["state"], "null" if row["median_h"] is None else "%.1f" % row["median_h"],
                "null" if row["worst_h"] is None else "%.1f" % row["worst_h"],
                row["worst_ticket"] or "null", row["n"]))
        print("bottleneck: %s" % (bottleneck or "none"))
        return 0
    except (ValueError, OSError, GitHubError) as err:
        print("error: %s" % err, file=sys.stderr)
        return 2


MARKER = "<!-- loom:metrics v1 -->"
REPORT = "<!-- loom:report v1 -->"
WINDOW = 86400
FIX = re.compile(r"<!-- loom:fix|\bfix(?:es|ed)?\b", re.I)
REGRESSION = re.compile(r"\bregression\b", re.I)
GENERIC_TERMS = re.compile(r"\b(wow|warcraft|guild|raid|realm|dungeon|playerbot)\b", re.I)
REPAIR_REF = re.compile(r"\b(?:Refs|Fixes) ([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)#(\d+)")


def stamp(value: Any) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def names(item: Dict[str, Any]) -> set[str]:
    return {str(label.get("name")) for label in item.get("labels", [])
            if isinstance(label, dict)}


@dataclass(frozen=True)
class Number:
    name: str
    value: str
    target: str
    passed: bool

    def line(self) -> str:
        return "%s %s: %s (target %s)" % (
            "PASS" if self.passed else "FAIL", self.name, self.value, self.target)


class RefList(list):
    """A list of reference strings that also compares equal to its length as an integer."""

    def __eq__(self, other: object) -> bool:
        if isinstance(other, int):
            return len(self) == other
        return super().__eq__(other)

    def __int__(self) -> int:
        return len(self)

    def __add__(self, other: Any) -> Any:
        if isinstance(other, int):
            return len(self) + other
        return super().__add__(other)

    def __radd__(self, other: Any) -> Any:
        if isinstance(other, int):
            return other + len(self)
        return super().__radd__(other)


def _items(value: Any) -> tuple[int, List[str]]:
    if isinstance(value, int):
        return value, []
    if isinstance(value, (list, tuple, set)):
        return len(value), list(value)
    return 0, []


def _format_refs(refs: Sequence[str]) -> str:
    if not refs:
        return ""
    if len(refs) <= 5:
        return ": " + ", ".join(refs)
    return ": " + ", ".join(refs[:5]) + " and %d more" % (len(refs) - 5)


@dataclass
class Totals:
    pull_old: int = 0
    past_rung: int = 0
    merged_tickets: int = 0
    verified: int = 0
    # Of the verified: proofs a lane's verify run made, and the brain's.
    verified_lane: int = 0
    verified_brain: int = 0
    asks: int = 0
    regressions: Any = field(default_factory=RefList)
    unchecked: Any = field(default_factory=RefList)
    fix_no_test: Any = field(default_factory=RefList)
    faults: int = 0
    goals: int = 0
    merge_times: List[float] = field(default_factory=list)
    merged_by_repo: Dict[str, int] = field(default_factory=dict)
    report_ages: Dict[str, float] = field(default_factory=dict)
    goal_repos: set[str] = field(default_factory=set)


class Collector:
    def __init__(self, gh: GitHub, repos: Sequence[str], home: str,
                 now: float, review_check: str, trusted: Sequence[str] = ()) -> None:
        self.gh, self.repos, self.home = gh, list(repos), home
        self.now, self.since, self.review_check = now, now - WINDOW, review_check
        self.trusted = {login.lower() for login in trusted}
        self.events: Dict[tuple[str, int], List[Dict[str, Any]]] = {}
        self.comments: Dict[tuple[str, int], List[Dict[str, Any]]] = {}

    def list(self, path: str) -> List[Dict[str, Any]]:
        return [item for item in self.gh.paginate(path) if isinstance(item, dict)]

    def issue_events(self, repo: str, number: int) -> List[Dict[str, Any]]:
        key = (repo, number)
        if key not in self.events:
            self.events[key] = self.list("/repos/%s/issues/%d/events" % key)
        return self.events[key]

    def issue_comments(self, repo: str, number: int) -> List[Dict[str, Any]]:
        key = (repo, number)
        if key not in self.comments:
            self.comments[key] = self.list("/repos/%s/issues/%d/comments" % key)
        return self.comments[key]

    def trusted_comment(self, comment: Dict[str, Any]) -> bool:
        return not self.trusted or str((comment.get("user") or {}).get("login") or "").lower() in self.trusted

    def label_time(self, repo: str, issue: Dict[str, Any], label: str) -> float:
        found = [stamp(e.get("created_at")) for e in self.issue_events(repo, int(issue["number"]))
                 if e.get("event") == "labeled"
                 and (e.get("label") or {}).get("name") == label]
        return max(found, default=0.0)

    def passes(self, repo: str, sha: str) -> bool:
        # rest() returns a Response or raises GitHubError, never None; only
        # .data can be None (an empty or non-JSON body), hence the dict check.
        # A GitHubError is left to propagate on purpose: counting a failed
        # lookup as "no review check" would fail the Quality number on an API
        # blip. Station._post_metrics_comment catches it and retries the pass.
        # Every page: GitHub returns 30 check runs by default, and a commit in
        # a repo with a CI matrix has more, so the review check can sit past
        # the first page (two reviewed merges counted as unreviewed, 2026-10-13).
        for page in range(1, 11):
            data = self.gh.rest("GET", "/repos/%s/commits/%s/check-runs?per_page=100&page=%d"
                                % (repo, sha, page)).data
            checks = data.get("check_runs", []) if isinstance(data, dict) else []
            if any(c.get("name") == self.review_check for c in checks):
                return True
            if len(checks) < 100:
                return False
        return False

    def _finished(self, lane: str, kind: str) -> Iterator[str]:
        """Paths of the briefs a lane finished in the window under done/ or failed/."""
        try:
            entries = os.scandir(os.path.join(self.home, "briefs", lane, kind))
        except FileNotFoundError:
            return
        with entries:
            for entry in entries:
                if (entry.name.endswith(".md") and entry.is_file()
                        and self.since <= self._completed(entry) <= self.now):
                    yield entry.path

    @staticmethod
    def _completed(entry: os.DirEntry) -> float:
        info = entry.stat()
        return max(info.st_mtime, info.st_ctime)

    def local_runs(self) -> tuple[Dict[str, int], Dict[str, Dict[str, int]], int, int]:
        counts: Dict[str, int] = {lane: 0 for lane in LANES}
        failures: Dict[str, Dict[str, int]] = {}
        unlinked = 0
        left_out = 0
        for lane in LANES:
            for kind in ("done", "failed"):
                for path in self._finished(lane, kind):
                    counts[lane] += 1
                    if kind == "failed":
                        # Only a failure at the work counts toward Escalation; a
                        # provider limit or a transient push failure says nothing
                        # about the model.
                        if self._failure_kind(lane, path) == "work":
                            unlinked += self._failed_run(path, failures)
                        else:
                            left_out += 1
        return counts, failures, unlinked, left_out

    def verify_runs(self) -> int:
        """Lane runs in the window that were verify runs (Deliverable: verify),
        a kind of their own inside the Spread count."""
        found = 0
        for lane in LANES:
            for kind in ("done", "failed"):
                for path in self._finished(lane, kind):
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        found += bool(re.search(r"^Deliverable: verify[ \t]*$", fh.read(), re.M))
        return found

    def _failure_kind(self, lane: str, path: str) -> str:
        """The kind the runner tagged the ticket's last failure with; work if untagged."""
        log = os.path.join(self.home, "logs", lane, os.path.basename(path)[:-len(".md")] + ".log")
        try:
            with open(log, encoding="utf-8", errors="replace") as fh:
                kinds = re.findall(r"^.*\bfailed: \[(work|limit|transient)\]", fh.read(), re.M)
        except OSError:
            return "work"
        return kinds[-1] if kinds else "work"

    @staticmethod
    def _failed_run(path: str, failures: Dict[str, Dict[str, int]]) -> int:
        with open(path, encoding="utf-8") as fh:
            body = fh.read()
        issue = re.search(r"^Issue: (\S+#\d+)$", body, re.M)
        model = re.search(r"^Model: ([^\n]+)$", body, re.M)
        if issue and model:
            by_model = failures.setdefault(issue.group(1), {})
            by_model[model.group(1)] = by_model.get(model.group(1), 0) + 1
        return int(not issue)

    def slot_samples(self) -> tuple[int, int]:
        path = os.path.join(self.home, "state", "station", "slot-samples.jsonl")
        if not os.path.exists(path):
            return 0, 0
        import json
        samples = []
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                # The station appends one line per pass; a pass killed mid-write
                # leaves a torn line, which must not stop every later day's line.
                try:
                    record = json.loads(line)
                except ValueError:
                    print("metrics: skipping an unreadable slot sample", file=sys.stderr)
                    continue
                if not isinstance(record, dict):
                    continue
                if self.since <= stamp(record.get("at")) <= self.now:
                    samples.append(record)
        return sum(bool(s.get("idle")) for s in samples), len(samples)

    def generic_files(self) -> List[str]:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        try:
            result = subprocess.run(["git", "ls-files", "-z"], cwd=root,
                                    capture_output=True, check=False, timeout=60)
        except subprocess.TimeoutExpired:
            raise OSError("git ls-files timed out for the generic metric") from None
        if result.returncode:
            raise OSError("could not list tracked files for the generic metric")
        found = []
        for rel in result.stdout.decode().split("\0"):
            if not rel or rel == "bus/metrics.py" or rel.startswith(("tests/", "docs/", ".")):
                continue
            path = os.path.join(root, rel)
            if not os.path.isfile(path):
                continue
            try:
                with open(path, encoding="utf-8") as fh:
                    content = fh.read()
            except (UnicodeDecodeError, OSError):
                continue
            if GENERIC_TERMS.search(content):
                found.append(rel)
        return found

    def _goal_reports(self, repo: str, issues: List[Dict[str, Any]], total: Totals) -> None:
        goals = [issue for issue in issues if "loom:goal" in names(issue)]
        total.goals += len(goals)
        if goals:
            total.goal_repos.add(repo)
        for goal in goals:
            newest = max((stamp(c.get("created_at"))
                          for c in self.issue_comments(repo, int(goal["number"]))
                          if self.trusted_comment(c)
                          and str(c.get("body") or "").startswith(REPORT)), default=0.0)
            total.report_ages["%s#%d" % (repo, goal["number"])] = (
                self.now - newest if newest else float("inf"))

    def _urgent_regression(self, repo: str, issue: Dict[str, Any], created: float) -> bool:
        notes = [(created, str(issue.get("body") or ""))]
        notes += [(stamp(c.get("created_at")), str(c.get("body") or ""))
                  for c in self.issue_comments(repo, int(issue["number"]))]
        return any(REGRESSION.search(body) and self.now - at > 7200
                   for at, body in notes if at)

    def _given_up(self, repo: str, number: int, comments: List[Dict[str, Any]]) -> float:
        """When the ticket was last left past the last rung, or 0 if a later
        `loom:rung 0` reset it."""
        last_reset = max((stamp(c.get("created_at")) for c in comments
                          if "<!-- loom:rung 0 -->" in str(c.get("body") or "")), default=0)
        given_up = max((stamp(c.get("created_at")) for c in comments
                        if "tried every rung" in str(c.get("body") or "").lower()), default=0)
        return given_up if given_up > last_reset else 0.0

    @staticmethod
    def _linked(repo: str, number: int, comments: List[Dict[str, Any]], since: float) -> bool:
        """True when a comment after `since` refers to some other ticket."""
        return any(match.group(1) != repo or int(match.group(2)) != number
                   for c in comments if stamp(c.get("created_at")) >= since
                   for match in REPAIR_REF.finditer(str(c.get("body") or "")))

    def _ticket(self, repo: str, issue: Dict[str, Any], labels: set[str],
                total: Totals) -> None:
        number = int(issue["number"])
        comments = [c for c in self.issue_comments(repo, number) if self.trusted_comment(c)]
        given_up = self._given_up(repo, number, comments)
        if (given_up and self.now - given_up > WINDOW
                and issue.get("state") == "open" and "loom:failed" in labels):
            total.past_rung += 1
            total.faults += not self._linked(repo, number, comments, given_up)
        merged = self.label_time(repo, issue, "loom:merged")
        if merged >= self.since:
            total.merged_tickets += 1
            if "loom:verified" in labels:
                self._count_verified(comments, total)
            self._merge_time(repo, number, merged, total)

    @staticmethod
    def _count_verified(comments: List[Dict[str, Any]], total: Totals) -> None:
        """Count a verified ticket, and who made its newest proof: a proof
        from before verified_by existed counts as neither."""
        total.verified += 1
        records = [r for r in (proof.read(str(c.get("body") or "")) for c in comments) if r]
        newest = records[-1] if records else {}
        total.verified_brain += newest.get("verified_by") == audit.BRAIN
        total.verified_lane += bool(audit.lane_of(newest))

    def _merge_time(self, repo: str, number: int, merged: float, total: Totals) -> None:
        briefed = min((stamp(e.get("created_at"))
                       for e in self.issue_events(repo, number)
                       if e.get("event") == "labeled"
                       and (e.get("label") or {}).get("name") == "loom:briefed"),
                      default=0.0)
        if briefed and briefed <= merged:
            total.merge_times.append((merged - briefed) / 3600)

    def _issue(self, repo: str, issue: Dict[str, Any], total: Totals) -> None:
        if "pull_request" in issue:
            return
        labels = names(issue)
        created = stamp(issue.get("created_at"))
        if created >= self.since and ("needs-operator" in labels or
                self.label_time(repo, issue, "needs-operator")):
            total.asks += 1
        if ("loom:urgent" in labels and issue.get("state") == "open"
                and self._urgent_regression(repo, issue, created)):
            if isinstance(total.regressions, list):
                total.regressions.append("%s#%d" % (repo, int(issue["number"])))
            else:
                total.regressions += 1
        if "loom:ticket" in labels:
            self._ticket(repo, issue, labels, total)

    def _pull(self, repo: str, pr: Dict[str, Any], total: Totals) -> None:
        if not str((pr.get("head") or {}).get("ref") or "").startswith("loom/"):
            return
        if pr.get("state") == "open" and self.now - stamp(pr.get("created_at")) > 21600:
            total.pull_old += 1
        if stamp(pr.get("merged_at")) >= self.since:
            total.merged_by_repo[repo] += 1
            self._merged_pull(repo, pr, total)

    def _merged_pull(self, repo: str, pr: Dict[str, Any], total: Totals) -> None:
        sha = str((pr.get("head") or {}).get("sha") or "")
        ref = "%s#%d" % (repo, int(pr["number"]))
        if not sha or not self.passes(repo, sha):
            if isinstance(total.unchecked, list):
                total.unchecked.append(ref)
            else:
                total.unchecked += 1
        if FIX.search(str(pr.get("body") or "")):
            # The factory's own rule (factory.lacks_test): a fix that changes
            # code needs a test; a config, doc or digest-pin change needs none.
            # Counting every untested fix-worded body flagged three
            # config-only pull requests on 2026-10-15.
            files = self.list("/repos/%s/pulls/%d/files" % (repo, int(pr["number"])))
            if lacks_test([str(f.get("filename") or "") for f in files
                           if f.get("status") != "removed"]):
                if isinstance(total.fix_no_test, list):
                    total.fix_no_test.append(ref)
                else:
                    total.fix_no_test += 1

    def _scan(self, total: Totals) -> None:
        for repo in self.repos:
            issues = self.list("/repos/%s/issues?state=all&per_page=100" % repo)
            pulls = self.list("/repos/%s/pulls?state=all&per_page=100" % repo)
            self._goal_reports(repo, issues, total)
            for issue in issues:
                self._issue(repo, issue, total)
            # Set in the same iteration that may add repo to goal_repos, so
            # every goal repo has a count once _scan returns.
            total.merged_by_repo[repo] = 0
            for pr in pulls:
                self._pull(repo, pr, total)

    def collect(self) -> List[Number]:
        total = Totals()
        self._scan(total)
        runs, failures, parked_without_ticket, left_out = self.local_runs()
        total.faults += parked_without_ticket
        return [
            flow_number(total),
            throughput_number(total),
            spread_number(runs, *self.slot_samples(), verify=self.verify_runs()),
            escalation_number(failures, left_out),
            quality_number(total),
            autonomy_number(total),
            visibility_number(total),
            self_repair_number(total),
            generic_number(self.generic_files()),
        ]


def flow_number(total: Totals) -> Number:
    median = statistics.median(total.merge_times) if total.merge_times else float("inf")
    ok = total.pull_old == 0 and median < 3 and total.past_rung == 0
    return Number("Flow", "%d PRs >6h; median %.1fh (%d tickets); %d past last rung >24h" %
                  (total.pull_old, median, len(total.merge_times), total.past_rung),
                  "0; <3h; 0", ok)


def throughput_number(total: Totals) -> Number:
    ok = (bool(total.goals) and all(total.merged_by_repo[r] for r in total.goal_repos)
          and total.merged_tickets > 0 and total.verified * 2 >= total.merged_tickets)
    return Number("Throughput", "%s; verified %d/%d (lane %d, brain %d)" %
                  (", ".join("%s %d" % pair for pair in total.merged_by_repo.items()),
                   total.verified, total.merged_tickets, total.verified_lane,
                   total.verified_brain),
                  "each goal repo >=1 merge; >=50% verified", ok)


def spread_number(runs: Dict[str, int], idle: int, samples: int, verify: int = 0) -> Number:
    total_runs = sum(runs.values())
    anthropic = sum(v for lane, v in runs.items() if lane.startswith("anthropic"))
    ok = total_runs > 0 and anthropic * 2 <= total_runs and samples > 0 and idle == 0
    return Number("Spread", "%s; Anthropic %d/%d; verify %d; idle %d/%d passes" %
                  (", ".join("%s %d" % pair for pair in runs.items()),
                   anthropic, total_runs, verify, idle, samples),
                  "Anthropic <=50%; idle 0", ok)


def escalation_number(failures: Dict[str, Dict[str, int]], left_out: int = 0) -> Number:
    offenders = [
        "%s %s" % (ticket, model)
        for ticket, models in failures.items()
        for model, count in models.items()
        if count >= 2
    ]
    repeat = sum(any(n >= 2 for n in models.values()) for models in failures.values())
    refs = _format_refs(offenders)
    return Number("Escalation", "%d tickets failed twice on one model doing the work%s; "
                  "%d failures left out as limit or transient" % (repeat, refs, left_out),
                  "0", repeat == 0)


def quality_number(total: Totals) -> Number:
    n_reg, reg_refs = _items(total.regressions)
    n_unc, unc_refs = _items(total.unchecked)
    n_fix, fix_refs = _items(total.fix_no_test)
    parts = [
        "%d unfixed regressions >2h%s" % (n_reg, _format_refs(reg_refs)),
        "%d unreviewed merges%s" % (n_unc, _format_refs(unc_refs)),
        "%d fix PRs without tests%s" % (n_fix, _format_refs(fix_refs)),
    ]
    return Number("Quality", "; ".join(parts), "0; 0; 0",
                  n_reg == 0 and n_unc == 0 and n_fix == 0)


def autonomy_number(total: Totals) -> Number:
    return Number("Autonomy", "%d asks opened" % total.asks, "<=1/day", total.asks <= 1)


def visibility_number(total: Totals) -> Number:
    max_age = max(total.report_ages.values(), default=float("inf"))
    return Number("Visibility", "%s" % ", ".join("%s %.1fh" % (r, age / 3600)
                                                for r, age in total.report_ages.items()),
                  "each goal report <=2h", bool(total.goals) and max_age <= 7200)


def self_repair_number(total: Totals) -> Number:
    return Number("Self-repair", "%d un-ticketed factory faults" % total.faults,
                  "0", total.faults == 0)


def generic_number(generic: List[str]) -> Number:
    return Number("Generic", "%d files: %s" % (len(generic), ", ".join(generic)),
                  "0 files", not generic)


def render(numbers: Sequence[Number], today: str, streak: int) -> str:
    passed = sum(number.passed for number in numbers)
    projected = datetime.strptime(today, "%Y-%m-%d").date() + timedelta(days=max(0, 7 - streak))
    return "%s\n%s: %d/9 pass; streak %d day(s); projected 7-day date %s\n%s" % (
        MARKER, today, passed, streak, projected.isoformat(),
        "\n".join("%d. %s" % (index, number.line())
                  for index, number in enumerate(numbers, 1)))


if __name__ == "__main__":
    # At the end, so every helper above is defined when the CLI runs.
    raise SystemExit(waits_main())
