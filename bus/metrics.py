"""Daily measurements of Loom's nine factory outcomes."""
from __future__ import annotations

import os
import re
import statistics
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Sequence

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
        data = self.gh.rest("GET", "/repos/%s/commits/%s/check-runs" % (repo, sha)).data
        checks = data.get("check_runs", []) if isinstance(data, dict) else []
        return any(c.get("name") == self.review_check for c in checks)

    def local_runs(self) -> tuple[Dict[str, int], Dict[str, Dict[str, int]], int]:
        counts: Dict[str, int] = {lane: 0 for lane in LANES}
        failures: Dict[str, Dict[str, int]] = {}
        unlinked = 0
        for lane in LANES:
            for kind in ("done", "failed"):
                folder = os.path.join(self.home, "briefs", lane, kind)
                try:
                    entries = os.scandir(folder)
                except FileNotFoundError:
                    continue
                with entries:
                    for entry in entries:
                        if not entry.name.endswith(".md") or not entry.is_file():
                            continue
                        info = entry.stat()
                        completed = max(info.st_mtime, info.st_ctime)
                        if not self.since <= completed <= self.now:
                            continue
                        counts[lane] += 1
                        if kind == "failed":
                            with open(entry.path, encoding="utf-8") as fh:
                                body = fh.read()
                            issue = re.search(r"^Issue: (\S+#\d+)$", body, re.M)
                            model = re.search(r"^Model: ([^\n]+)$", body, re.M)
                            if not issue:
                                unlinked += 1
                            if issue and model:
                                by_model = failures.setdefault(issue.group(1), {})
                                by_model[model.group(1)] = by_model.get(model.group(1), 0) + 1
        return counts, failures, unlinked

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

    def collect(self) -> List[Number]:
        pull_old = 0
        merge_times: List[float] = []
        past_rung = 0
        merged_by_repo: Dict[str, int] = {}
        merged_tickets = verified = 0
        asks = regressions = unchecked = fix_no_test = faults = 0
        report_ages: Dict[str, float] = {}
        goal_repos: set[str] = set()
        goals = 0
        for repo in self.repos:
            issues = self.list("/repos/%s/issues?state=all&per_page=100" % repo)
            pulls = self.list("/repos/%s/pulls?state=all&per_page=100" % repo)
            goal_issues = [i for i in issues if "loom:goal" in names(i)]
            goals += len(goal_issues)
            if goal_issues:
                goal_repos.add(repo)
            for goal in goal_issues:
                newest = 0.0
                for comment in self.issue_comments(repo, int(goal["number"])):
                    if self.trusted_comment(comment) and str(comment.get("body") or "").startswith(REPORT):
                        newest = max(newest, stamp(comment.get("created_at")))
                report_ages["%s#%d" % (repo, goal["number"])] = (
                    self.now - newest if newest else float("inf"))
            for issue in issues:
                if "pull_request" in issue:
                    continue
                labels = names(issue)
                created = stamp(issue.get("created_at"))
                if created >= self.since and ("needs-operator" in labels or
                        self.label_time(repo, issue, "needs-operator")):
                    asks += 1
                if "loom:urgent" in labels and issue.get("state") == "open":
                    notes = [(created, str(issue.get("body") or ""))]
                    notes += [(stamp(c.get("created_at")), str(c.get("body") or ""))
                              for c in self.issue_comments(repo, int(issue["number"]))]
                    if any(REGRESSION.search(text) and self.now - at > 7200
                           for at, text in notes if at):
                        regressions += 1
                if "loom:ticket" not in labels:
                    continue
                number = int(issue["number"])
                comments = [c for c in self.issue_comments(repo, number)
                            if self.trusted_comment(c)]
                last_reset = max((stamp(c.get("created_at")) for c in comments
                                  if "<!-- loom:rung 0 -->" in str(c.get("body") or "")), default=0)
                given_up = max((stamp(c.get("created_at")) for c in comments
                                if "tried every rung" in str(c.get("body") or "").lower()), default=0)
                if (given_up > last_reset and self.now - given_up > WINDOW
                        and issue.get("state") == "open" and "loom:failed" in labels):
                    past_rung += 1
                    if not any(match.group(1) != repo or int(match.group(2)) != number
                               for c in comments if stamp(c.get("created_at")) >= given_up
                               for match in REPAIR_REF.finditer(str(c.get("body") or ""))):
                        faults += 1
                merged = self.label_time(repo, issue, "loom:merged")
                if merged >= self.since:
                    merged_tickets += 1
                    verified += "loom:verified" in labels
                    briefed = min((stamp(e.get("created_at"))
                                   for e in self.issue_events(repo, number)
                                   if e.get("event") == "labeled"
                                   and (e.get("label") or {}).get("name") == "loom:briefed"),
                                  default=0.0)
                    if briefed and briefed <= merged:
                        merge_times.append((merged - briefed) / 3600)
            merged_by_repo[repo] = 0
            for pr in pulls:
                if not str((pr.get("head") or {}).get("ref") or "").startswith("loom/"):
                    continue
                if pr.get("state") == "open" and self.now - stamp(pr.get("created_at")) > 21600:
                    pull_old += 1
                merged = stamp(pr.get("merged_at"))
                if merged < self.since:
                    continue
                merged_by_repo[repo] += 1
                sha = str((pr.get("head") or {}).get("sha") or "")
                if not sha or not self.passes(repo, sha):
                    unchecked += 1
                if FIX.search(str(pr.get("body") or "")):
                    files = self.list("/repos/%s/pulls/%d/files" % (repo, int(pr["number"])))
                    if not any(TEST_FILE.search(str(f.get("filename") or ""))
                               and f.get("status") != "removed" for f in files):
                        fix_no_test += 1
        runs, failures, parked_without_ticket = self.local_runs()
        faults += parked_without_ticket
        total_runs = sum(runs.values())
        anthropic = sum(v for lane, v in runs.items() if lane.startswith("anthropic"))
        repeat = sum(any(n >= 2 for n in models.values()) for models in failures.values())
        idle, samples = self.slot_samples()
        generic = self.generic_files()
        median = statistics.median(merge_times) if merge_times else float("inf")
        flow_ok = pull_old == 0 and median < 3 and past_rung == 0
        throughput_ok = bool(goals) and all(merged_by_repo[r] for r in goal_repos) \
            and merged_tickets > 0 and verified * 2 >= merged_tickets
        spread_ok = total_runs > 0 and anthropic * 2 <= total_runs and samples > 0 and idle == 0
        max_age = max(report_ages.values(), default=float("inf"))
        return [
            Number("Flow", "%d PRs >6h; median %.1fh (%d tickets); %d past last rung >24h" %
                   (pull_old, median, len(merge_times), past_rung), "0; <3h; 0", flow_ok),
            Number("Throughput", "%s; verified %d/%d" %
                   (", ".join("%s %d" % pair for pair in merged_by_repo.items()), verified, merged_tickets),
                   "each goal repo >=1 merge; >=50% verified", throughput_ok),
            Number("Spread", "%s; Anthropic %d/%d; idle %d/%d passes" %
                   (", ".join("%s %d" % pair for pair in runs.items()), anthropic, total_runs, idle, samples),
                   "Anthropic <=50%; idle 0", spread_ok),
            Number("Escalation", "%d tickets failed twice on one model" % repeat, "0", repeat == 0),
            Number("Quality", "%d unfixed regressions >2h; %d unreviewed merges; %d fix PRs without tests" %
                   (regressions, unchecked, fix_no_test), "0; 0; 0",
                   regressions == unchecked == fix_no_test == 0),
            Number("Autonomy", "%d asks opened" % asks, "<=1/day", asks <= 1),
            Number("Visibility", "%s" % ", ".join("%s %.1fh" % (r, age / 3600)
                                                 for r, age in report_ages.items()),
                   "each goal report <=2h", bool(goals) and max_age <= 7200),
            Number("Self-repair", "%d un-ticketed factory faults" % faults, "0", faults == 0),
            Number("Generic", "%d files: %s" % (len(generic), ", ".join(generic)),
                   "0 files", not generic),
        ]


def render(numbers: Sequence[Number], today: str, streak: int) -> str:
    passed = sum(number.passed for number in numbers)
    projected = datetime.strptime(today, "%Y-%m-%d").date() + timedelta(days=max(0, 7 - streak))
    return "%s\n%s: %d/9 pass; streak %d day(s); projected 7-day date %s\n%s" % (
        MARKER, today, passed, streak, projected.isoformat(),
        "\n".join("%d. %s" % (index, number.line())
                  for index, number in enumerate(numbers, 1)))
