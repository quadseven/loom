"""Daily measurements of Loom's nine factory outcomes."""
from __future__ import annotations

import os
import re
import statistics
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Iterator, List, Sequence

from .github import GitHub
from .labels import LANES

MARKER = "<!-- loom:metrics v1 -->"
REPORT = "<!-- loom:report v1 -->"
WINDOW = 86400
TEST_FILE = re.compile(r"(^|/)(test_[^/]+\.py|[^/]+_test\.[^/]+|[^/]+\.(test|spec)\.[^/]+)$")
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


@dataclass
class Totals:
    pull_old: int = 0
    past_rung: int = 0
    merged_tickets: int = 0
    verified: int = 0
    asks: int = 0
    regressions: int = 0
    unchecked: int = 0
    fix_no_test: int = 0
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
        data = self.gh.rest("GET", "/repos/%s/commits/%s/check-runs" % (repo, sha)).data
        checks = data.get("check_runs", []) if isinstance(data, dict) else []
        return any(c.get("name") == self.review_check for c in checks)

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

    def local_runs(self) -> tuple[Dict[str, int], Dict[str, Dict[str, int]], int]:
        counts: Dict[str, int] = {lane: 0 for lane in LANES}
        failures: Dict[str, Dict[str, int]] = {}
        unlinked = 0
        for lane in LANES:
            for kind in ("done", "failed"):
                for path in self._finished(lane, kind):
                    counts[lane] += 1
                    if kind == "failed":
                        unlinked += self._failed_run(path, failures)
        return counts, failures, unlinked

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
                try:
                    record = json.loads(line)
                except ValueError:
                    raise OSError("unreadable slot sample") from None
                if self.since <= stamp(record.get("at")) <= self.now:
                    samples.append(record)
        return sum(bool(s.get("idle")) for s in samples), len(samples)

    def generic_files(self) -> List[str]:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        result = subprocess.run(["git", "ls-files", "-z"], cwd=root,
                                capture_output=True, check=False)
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
            total.verified += "loom:verified" in labels
            self._merge_time(repo, number, merged, total)

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
        if not sha or not self.passes(repo, sha):
            total.unchecked += 1
        if FIX.search(str(pr.get("body") or "")):
            files = self.list("/repos/%s/pulls/%d/files" % (repo, int(pr["number"])))
            if not any(TEST_FILE.search(str(f.get("filename") or ""))
                       and f.get("status") != "removed" for f in files):
                total.fix_no_test += 1

    def _scan(self, total: Totals) -> None:
        for repo in self.repos:
            issues = self.list("/repos/%s/issues?state=all&per_page=100" % repo)
            pulls = self.list("/repos/%s/pulls?state=all&per_page=100" % repo)
            self._goal_reports(repo, issues, total)
            for issue in issues:
                self._issue(repo, issue, total)
            total.merged_by_repo[repo] = 0
            for pr in pulls:
                self._pull(repo, pr, total)

    def collect(self) -> List[Number]:
        total = Totals()
        self._scan(total)
        runs, failures, parked_without_ticket = self.local_runs()
        total.faults += parked_without_ticket
        return [
            flow_number(total),
            throughput_number(total),
            spread_number(runs, *self.slot_samples()),
            escalation_number(failures),
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
    return Number("Throughput", "%s; verified %d/%d" %
                  (", ".join("%s %d" % pair for pair in total.merged_by_repo.items()),
                   total.verified, total.merged_tickets),
                  "each goal repo >=1 merge; >=50% verified", ok)


def spread_number(runs: Dict[str, int], idle: int, samples: int) -> Number:
    total_runs = sum(runs.values())
    anthropic = sum(v for lane, v in runs.items() if lane.startswith("anthropic"))
    ok = total_runs > 0 and anthropic * 2 <= total_runs and samples > 0 and idle == 0
    return Number("Spread", "%s; Anthropic %d/%d; idle %d/%d passes" %
                  (", ".join("%s %d" % pair for pair in runs.items()),
                   anthropic, total_runs, idle, samples),
                  "Anthropic <=50%; idle 0", ok)


def escalation_number(failures: Dict[str, Dict[str, int]]) -> Number:
    repeat = sum(any(n >= 2 for n in models.values()) for models in failures.values())
    return Number("Escalation", "%d tickets failed twice on one model" % repeat,
                  "0", repeat == 0)


def quality_number(total: Totals) -> Number:
    return Number("Quality", "%d unfixed regressions >2h; %d unreviewed merges; %d fix PRs without tests" %
                  (total.regressions, total.unchecked, total.fix_no_test), "0; 0; 0",
                  total.regressions == total.unchecked == total.fix_no_test == 0)


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
