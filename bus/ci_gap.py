"""Validation gaps: CI jobs a lane pull request fails whose commands the
repo's validation contract (.loom/validation.conf) never runs.

A lane runs the contract's check before it pushes, and CI runs every job the
pull request's files trigger. A job the contract leaves out passes the lane's
validation and fails in CI on the first push, and each such failure costs
the ticket fix rounds. A lane pull request in the deploy repo failed two
per-directory unit suites (a generated rbac.yaml not regenerated after its
source changed, and a pinned test in another directory) that its contract
never ran (loom#1773).

When a lane pull request goes back for a fix round, the station maps each
failing GitHub Actions check run to its workflow file, job and failing step,
reads the step's run: command, and compares it with the contract's setup,
check and affected text. A command the contract does not run is a gap: the
station files one issue per repo per gap in that repo (deduplicated by a
marker key and the state file), naming the job, its command and the line to
add to the contract, and the fix round is told to run the command itself.

The station also counts, per repo, how many lane pull requests were red in
CI on their first push (before any fix round), and bus.brain status shows the
rate with the gaps filed (status_lines).

The workflow reader is line based (no YAML library): it reads the job ids
and names under `jobs:`, each job's steps with their names, run: commands
and working-directory, and the `on.pull_request` paths filter.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple
from urllib.parse import quote

from . import contract, ship_paths
from .github import GitHubError, RateLimited, ShadowHeld

STATE_FILE = "validation_gaps.json"
LABEL = "loom:validation-gap"
MARKER = "<!-- loom:validation-gap v1 key=%s -->"
MARKER_RE = re.compile(r"<!-- loom:validation-gap v1 key=([0-9a-f]{16}) -->")
CONTRACT_FILE = ".loom/validation.conf"
# Pull requests kept per repo for the first-push rate.
FIRST_PUSH_KEEP = 200
TITLE_MAX = 90
COMMAND_SHOWN = 400
# A command part that prepares the shell rather than checking anything.
_TRIVIAL = re.compile(r"^(?:cd|echo|set|export|source|\.|true|:|mkdir|printf|shopt|ulimit)(?:\s|$)")
_SPLIT = re.compile(r"\s*(?:&&|\|\||;)\s*")
_EXPR = re.compile(r"\$\{\{.*?\}\}")


class Step(NamedTuple):
    name: str
    command: str
    workdir: str


class Job(NamedTuple):
    ident: str
    name: str
    steps: List[Step]


class Gap(NamedTuple):
    repo: str
    pr: int
    workflow: str
    job: str
    step: str
    command: str
    paths: List[str]
    matched: List[str]
    line: str
    key: str


# ---- the workflow reader -------------------------------------------------

def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _content(lines: List[str], start: int, end: int):
    for index in range(start, end):
        line = lines[index]
        if line.strip() and not line.lstrip().startswith("#"):
            yield index, _indent(line), line


def _block_end(lines: List[str], start: int, indent: int) -> int:
    """The index after the block of lines deeper than indent from start."""
    for index in range(start, len(lines)):
        line = lines[index]
        if line.strip() and not line.lstrip().startswith("#") and _indent(line) <= indent:
            return index
    return len(lines)


def _scalar(lines: List[str], index: int, indent: int, rest: str) -> str:
    """A key's value: the rest of its line, or a | or > block's lines."""
    rest = rest.strip()
    if re.fullmatch(r"[|>][+-]?", rest):
        end = _block_end(lines, index + 1, indent)
        block = [lines[i] for i in range(index + 1, end)]
        depth = min((_indent(l) for l in block if l.strip()), default=0)
        return "\n".join(l[depth:].rstrip() for l in block).strip("\n")
    return ship_paths._strip(rest)


def _steps(lines: List[str], start: int, end: int, job_workdir: str) -> List[Step]:
    """The run steps of a `steps:` list: each item's name, run: command (a
    block's lines kept) and working-directory (the job's when it has none)."""
    found: List[Step] = []
    current: Optional[Dict[str, str]] = None
    item_indent = key_indent = -1

    def close() -> None:
        if current and current.get("run"):
            found.append(Step(current.get("name", ""), current["run"],
                              current.get("working-directory", job_workdir)))

    for index, indent, line in _content(lines, start, end):
        item = re.match(r"^(\s*-\s+)(\S.*)$", line)
        if item and (item_indent < 0 or indent == item_indent):
            close()
            item_indent, key_indent, current = indent, len(item.group(1)), {}
            text = item.group(2)
        elif current is not None and indent == key_indent:
            text = line.strip()
        else:
            continue
        name, rest = ship_paths._name(text)
        if name == "run":
            current["run"] = _scalar(lines, index, key_indent, rest)
        elif name in ("name", "working-directory"):
            current[name] = ship_paths._strip(rest)
    close()
    return found


def _job(lines: List[str], index: int, indent: int, ident: str) -> Job:
    """One job: its name, its run steps and the working-directory its
    defaults name before the steps."""
    job_end = _block_end(lines, index + 1, indent)
    name, workdir, steps_at, child = ident, "", None, None
    for c_index, c_indent, c_line in _content(lines, index + 1, job_end):
        child = c_indent if child is None else child
        key, rest = ship_paths._name(c_line)
        if c_indent == child and key == "name":
            name = ship_paths._strip(rest) or ident
        elif c_indent == child and key == "steps":
            steps_at = (c_index, c_indent)
        elif key == "working-directory" and c_indent > child and steps_at is None:
            workdir = ship_paths._strip(rest)
    if steps_at is None:
        return Job(ident, name, [])
    start = steps_at[0] + 1
    return Job(ident, name, _steps(lines, start, _block_end(lines, start, steps_at[1]), workdir))


def workflow_jobs(text: str) -> List[Job]:
    """Each job under the workflow's top-level `jobs:`: its id, its name (the
    id when it has none) and its run steps."""
    lines = text.splitlines()
    jobs_at = next((i for i, indent, line in _content(lines, 0, len(lines))
                    if not indent and ship_paths._name(line)[0] == "jobs"), None)
    if jobs_at is None:
        return []
    entries = list(_content(lines, jobs_at + 1, _block_end(lines, jobs_at + 1, 0)))
    job_indent = entries[0][1] if entries else 0
    found: List[Job] = []
    for index, indent, line in entries:
        ident = ship_paths._name(line)[0] if indent == job_indent else None
        if ident:
            found.append(_job(lines, index, indent, ident))
    return found


def pull_request_paths(text: str) -> List[str]:
    """The workflow's on.pull_request paths filter ([] when it has none)."""
    return ship_paths.trigger_paths(text, ("pull_request", "pull_request_target"))


def _literal(name: str) -> str:
    return " ".join(_EXPR.sub("", name).split())


def find_job(jobs: Sequence[Job], run_name: str) -> Optional[Job]:
    """The job a check run reports: its name or id, with a matrix suffix
    (" (a, b)") or a caller prefix ("caller / ") allowed. A name with an
    expression in it matches on its literal text."""
    wanted = [" ".join(run_name.split())]
    wanted.append(re.sub(r"\s*\([^()]*\)$", "", wanted[0]))
    wanted.append(wanted[-1].rsplit(" / ", 1)[-1])
    for want in wanted:
        for job in jobs:
            if want in (job.name, job.ident):
                return job
    for job in jobs:
        literal = _literal(job.name)
        if "${{" in job.name and literal and wanted[0].startswith(literal.split(" ")[0]):
            return job
    return None


def failing_steps(job: Job, api_steps: Sequence[Any]) -> List[Step]:
    """The job's run steps the Actions job API reports as failed; every run
    step when a step failed but none can be matched, and none when the API
    reports no failed step. An unnamed run step is reported as
    "Run <first line of its command>"."""
    failed = [str(s.get("name") or "") for s in api_steps
              if isinstance(s, dict) and s.get("conclusion") == "failure"]
    found: List[Step] = []
    for name in failed:
        for step in job.steps:
            first = step.command.strip().splitlines()[0] if step.command.strip() else ""
            if (step.name and name == step.name) or (
                    not step.name and name.startswith("Run ") and first.startswith(name[4:].strip())):
                found.append(step)
    # A failed step this reader cannot match (a renamed or composite step)
    # still names the job's commands; a job with no failed step at all (it
    # was cancelled or timed out) names none, so nothing it runs is a gap.
    return found or (list(job.steps) if failed else [])


# ---- coverage -------------------------------------------------------------

def _joined(command: str) -> str:
    """A run block as one line: continuation lines joined, the rest with &&."""
    command = re.sub(r"\\\n\s*", " ", command)
    lines = [l.strip() for l in command.splitlines() if l.strip() and not l.strip().startswith("#")]
    return " && ".join(lines)


def _parts(command: str) -> List[str]:
    return [p for p in _SPLIT.split(_joined(command)) if p and not _TRIVIAL.match(p)]


def _norm(text: str) -> str:
    return " ".join(text.split())


def covered(command: str, contract_text: str, workdir: str = "") -> bool:
    """Whether the contract runs the command: every part of it that checks
    something (not cd, echo, set, export) appears in the contract's setup,
    check or affected text, and so does the step's working directory when it
    has one (the same command run from the root runs another suite). A part
    with a ${{ }} expression counts by its text before the expression."""
    have = _norm(contract_text)
    if workdir and _norm(workdir.split("${{", 1)[0]).rstrip("/") not in have:
        return False
    for part in _parts(command):
        part = _norm(part.split("${{", 1)[0])
        if part and part not in have:
            return False
    return True


def _with_workdir(step: Step) -> str:
    joined = _joined(step.command)
    return "cd %s && %s" % (step.workdir, joined) if step.workdir else joined


def suggestion(command: str, paths: Sequence[str], fields: Dict[str, str]) -> str:
    """The contract line that would run the command. A job CI runs only for
    some paths belongs in the affected command, which gets the changed
    files; any other job in the check."""
    if paths:
        if fields.get("affected"):
            return "affected: %s && %s" % (fields["affected"], command)
        return "affected: %s" % command
    check = fields.get("check", "")
    return "check: %s && %s" % (check, command) if check and check != "true" else "check: %s" % command


def gap_key(repo: str, workflow: str, job: str, command: str) -> str:
    raw = json.dumps([repo.lower(), workflow, job, _norm(command)])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# ---- reading GitHub -------------------------------------------------------

def _text(gh: Any, repo: str, path: str, ref: str) -> Optional[str]:
    data = gh.rest("GET", "/repos/%s/contents/%s?ref=%s" % (
        repo, quote(path, safe="/"), quote(ref, safe="")), ok=(404,)).data
    if not isinstance(data, dict) or not data.get("content"):
        return None
    try:
        return base64.b64decode(data["content"]).decode("utf-8", "replace")
    except (TypeError, ValueError):
        return None


def contract_fields(gh: Any, repo: str, ref: str) -> Optional[Dict[str, str]]:
    """The contract on ref as key: value fields; None when the repo has none
    or it cannot be read (no gap is judged against a contract unread)."""
    text = _text(gh, repo, CONTRACT_FILE, ref)
    if text is None:
        return None
    try:
        return contract.fields(text)
    except ValueError:
        return None


def _run_workflow(gh: Any, repo: str, run: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """(workflow path, Actions job) of a GitHub Actions check run; ("", {})
    when it is not one or either read fails."""
    if (run.get("app") or {}).get("slug") != "github-actions":
        return "", {}
    job_data = gh.rest("GET", "/repos/%s/actions/jobs/%d" % (repo, int(run["id"])),
                       ok=(404,)).data
    if not isinstance(job_data, dict) or not job_data.get("run_id"):
        return "", {}
    run_data = gh.rest("GET", "/repos/%s/actions/runs/%d" % (repo, int(job_data["run_id"])),
                       ok=(404,)).data
    path = str(run_data.get("path") or "") if isinstance(run_data, dict) else ""
    path = path.split("@", 1)[0]
    if not re.fullmatch(r"\.github/workflows/[\w.-]+\.ya?ml", path):
        return "", {}
    return path, job_data


class _Reads:
    """What find_gaps reads once per call: workflow texts by path and the
    pull request's files (only when a gap's workflow has a paths filter)."""

    def __init__(self, gh: Any, repo: str, sha: str, changed: Callable[[], List[str]]):
        self.gh, self.repo, self.sha, self.changed = gh, repo, sha, changed
        self.texts: Dict[str, Optional[str]] = {}
        self.files: Optional[List[str]] = None

    def text(self, path: str) -> Optional[str]:
        if path not in self.texts:
            self.texts[path] = _text(self.gh, self.repo, path, self.sha)
        return self.texts[path]

    def matched(self, paths: List[str]) -> List[str]:
        if not paths:
            return []
        if self.files is None:
            self.files = self.changed()
        return [f for f in self.files if ship_paths.matches(paths, f)][:5]


def find_gaps(gh: Any, repo: str, pr: int, sha: str, fields: Dict[str, str],
              failing: Sequence[Dict[str, Any]], changed: Callable[[], List[str]]) -> List[Gap]:
    """The gaps in the failing GitHub Actions check runs of a lane pull
    request's head: for each run, its job's failing steps whose commands
    the contract (its fields, contract_fields on the base branch) does not
    run. `changed` lists the pull request's files, read only when a gap's
    workflow has a paths filter."""
    have = "\n".join(fields.get(k, "") for k in ("setup", "check", "affected"))
    reads = _Reads(gh, repo, sha, changed)
    gaps: List[Gap] = []
    for run in failing:
        path, job_data = _run_workflow(gh, repo, run)
        text = reads.text(path) if path else None
        job = find_job(workflow_jobs(text), str(run.get("name") or job_data.get("name") or "")) \
            if text is not None else None
        if job is None:
            continue
        paths = pull_request_paths(text)
        for step in failing_steps(job, job_data.get("steps") or []):
            if covered(step.command, have, step.workdir):
                continue
            command = _with_workdir(step)
            gaps.append(Gap(repo, pr, path, job.name, step.name or "run step", command,
                            list(paths), reads.matched(paths), suggestion(command, paths, fields),
                            gap_key(repo, path, job.name, command)))
    return gaps


# ---- filing ---------------------------------------------------------------

def _shown(text: str) -> str:
    return "`%s`" % " ".join(str(text).replace("`", "'").split())[:COMMAND_SHOWN]


def _block(text: str) -> List[str]:
    return ["    " + "".join(c if " " <= c <= "~" else " " for c in l)[:COMMAND_SHOWN]
            for l in text.splitlines() if l.strip()] or ["    (empty)"]


def title(gap: Gap) -> str:
    text = "Validation gap: CI job %s is not in %s" % (" ".join(gap.job.split()), CONTRACT_FILE)
    return text if len(text) <= TITLE_MAX else text[:TITLE_MAX - 3].rstrip() + "..."


def body(gap: Gap) -> str:
    scope = ("CI runs this job only when a change matches the paths filter %s; pull request "
             "#%d changed %s." % (", ".join(_shown(p) for p in gap.paths[:8]), gap.pr,
                                   ", ".join(_shown(f) for f in gap.matched) or "matching files")
             if gap.paths else "CI runs this job on every pull request.")
    lines = [
        MARKER % gap.key, "",
        "## Why", "",
        ("Lane pull request #%d failed CI job %s (workflow %s, step %s). The lane's own "
         "validation passed because %s does not run this command, so the failure showed up "
         "only in CI and the ticket went back for a fix round. Every lane change this job "
         "covers will repeat that until the contract runs it." % (
             gap.pr, _shown(gap.job), _shown(gap.workflow), _shown(gap.step), CONTRACT_FILE)),
        "", "## What", "",
        "Make %s run the command CI runs. The line to add:" % CONTRACT_FILE, "",
    ] + _block(gap.line) + ["", "CI runs:", ""] + _block(gap.command) + [
        "", scope,
        ("An `affected:` command gets the pull request's changed files on stdin and in "
         "LOOM_CHANGED_FILES, so it can run this job's command only for the paths CI runs "
         "it for (docs/LANES.md)." if gap.paths else ""),
        "", "## Acceptance criteria", "",
        "- [ ] %s runs %s (in its check or affected command)." % (CONTRACT_FILE, _shown(gap.command)),
        "- [ ] The contract passes on the default branch.",
        "- [ ] The command runs without secrets, simulators or unset variables on a lane machine.",
        "", "Size: XS", "",
        "## Out of scope", "",
        "Changing the CI workflow, and other jobs the contract leaves out (each has its own issue).",
        "",
    ]
    return "\n".join(lines)


def _load(path: str) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        # Missing or torn: the marker search on the repo's issues still
        # keeps a gap from being filed twice.
        return {}
    return data if isinstance(data, dict) else {}


def _save(path: str, data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def state_path(state_dir: str) -> str:
    return os.path.join(state_dir, STATE_FILE)


def _existing(gh: Any, repo: str) -> Dict[str, int]:
    """Marker key to issue number for the repo's validation-gap issues, open
    or closed: a closed one was settled and is not filed again."""
    found: Dict[str, int] = {}
    issues = gh.rest("GET", "/repos/%s/issues?labels=%s&state=all&per_page=100"
                     % (repo, quote(LABEL, safe="")), ok=(404,)).data
    for issue in issues if isinstance(issues, list) else []:
        if not isinstance(issue, dict):
            continue
        match = MARKER_RE.search(str(issue.get("body") or ""))
        if match:
            found[match.group(1)] = int(issue.get("number") or 0)
    return found


def file_gaps(gh: Any, state_dir: str, gaps: Sequence[Gap], now: float,
              say: Callable[[str], None]) -> List[Tuple[Gap, int]]:
    """File one issue per new gap in the gap's repo and record it in the
    state file; a gap already recorded or already on an issue (by marker) is
    not filed again. Returns (gap, issue number) for each gap, filed now or
    before (0 when the filing failed)."""
    path = state_path(state_dir)
    data = _load(path)
    filed = data.setdefault("gaps", {})
    out: List[Tuple[Gap, int]] = []
    existing: Optional[Dict[str, int]] = None
    seen = set()
    for gap in gaps:
        if gap.key in seen:
            continue
        seen.add(gap.key)
        known = (filed.get(gap.repo) or {}).get(gap.key)
        if isinstance(known, dict) and int(known.get("issue") or 0):
            out.append((gap, int(known["issue"])))
            continue
        if existing is None:
            existing = _existing(gh, gap.repo)
        number = existing.get(gap.key, 0)
        if not number:
            number = _create(gh, gap, say)
        if number:
            filed.setdefault(gap.repo, {})[gap.key] = {
                "issue": number, "pr": gap.pr, "job": gap.job, "workflow": gap.workflow,
                "line": gap.line, "at": now}
            _save(path, data)
            say("validation gap: %s CI job %s is not in the contract; issue #%d"
                % (gap.repo, gap.job, number))
        out.append((gap, number))
    return out


def _create(gh: Any, gap: Gap, say: Callable[[str], None]) -> int:
    payload = {"title": title(gap), "body": body(gap), "labels": [LABEL]}
    try:
        data = gh.rest("POST", "/repos/%s/issues" % gap.repo, payload).data
    except RateLimited:
        raise
    except ShadowHeld as err:
        say("validation gap: %s: shadow mode held the filing: %s" % (gap.repo, err))
        return 0
    except GitHubError as err:
        say("validation gap: %s: could not file with the %s label (%s); filing without it"
            % (gap.repo, LABEL, err))
        try:
            data = gh.rest("POST", "/repos/%s/issues" % gap.repo,
                           {"title": payload["title"], "body": payload["body"]}).data
        except RateLimited:
            raise
        except GitHubError as err2:
            say("validation gap: %s: could not file the issue: %s" % (gap.repo, err2))
            return 0
    return int((data or {}).get("number") or 0) if isinstance(data, dict) else 0


def fix_notes(gaps: Sequence[Tuple[Gap, int]]) -> str:
    """Lines for the fix round: run each gap's command before pushing."""
    lines = []
    for gap, number in gaps:
        lines.append("CI job %s runs %s, which this repo's %s does not run, so the lane's own "
                     "validation never ran it%s. Run it in the tree before you push." % (
                         _shown(gap.job), _shown(gap.command), CONTRACT_FILE,
                         " (validation gap #%d)" % number if number else ""))
    return "\n".join(lines)


# ---- first-push red rate --------------------------------------------------

def note_first_push(state_dir: str, repo: str, pr: int, red: bool, now: float) -> bool:
    """Record a lane pull request's first-push CI result once. True when it
    was recorded now."""
    path = state_path(state_dir)
    data = _load(path)
    pushes = data.setdefault("first_push", {}).setdefault(repo, {})
    if str(pr) in pushes:
        return False
    pushes[str(pr)] = {"red": bool(red), "at": now}
    if len(pushes) > FIRST_PUSH_KEEP:
        for old in sorted(pushes, key=lambda k: pushes[k].get("at") or 0)[:len(pushes) - FIRST_PUSH_KEEP]:
            del pushes[old]
    _save(path, data)
    return True


def status_lines(state_dir: str) -> List[str]:
    """The "## Validation gaps" section for bus.brain status: per repo, the
    first-push CI red rate of lane pull requests and the gaps filed."""
    data = _load(state_path(state_dir))
    pushes = data.get("first_push") or {}
    gaps = data.get("gaps") or {}
    lines = ["## Validation gaps (first-push CI red rate per repo)"]
    repos = sorted(set(pushes) | set(gaps))
    if not repos:
        return lines + ["- none recorded yet"]
    for repo in repos:
        rows = [r for r in (pushes.get(repo) or {}).values() if isinstance(r, dict)]
        red = sum(1 for r in rows if r.get("red"))
        filed = gaps.get(repo) or {}
        rate = ("%d of %d lane pull requests red on first push (%d%%)" % (
            red, len(rows), round(100.0 * red / len(rows))) if rows else "no first push seen")
        lines.append("- %s: %s; %d gap%s filed" % (repo, rate, len(filed),
                                                   "" if len(filed) == 1 else "s"))
        for key in sorted(filed, key=lambda k: (filed[k] or {}).get("at") or 0):
            item = filed[key] or {}
            lines.append("  - #%s job %s: add %s" % (item.get("issue"), item.get("job"),
                                                     _shown(item.get("line") or "")))
    return lines
