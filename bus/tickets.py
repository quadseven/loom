"""Ticket issues as local briefs: names, header lines, rendering and trust.

The patterns here mirror the checks in lanes/run_lane.sh, so a brief that
passes them is one the unchanged runner accepts. docs/BRAIN.md ("Ticket
issues", "Trust") is the contract.

Two header lines say when a ticket may run: `Not before:` names a UTC time,
and `After:` names the tickets it builds on. Neither reaches the brief the
lane reads; both are settled before the station claims a ticket.
"""
from __future__ import annotations

import calendar
import re
import time
from typing import Any, Callable, Dict, Iterable, List, NamedTuple, Optional, Tuple

from . import proof

# The runner's own patterns (lanes/run_lane.sh).
MODEL_RE = re.compile(r"[A-Za-z0-9._:][A-Za-z0-9._:/@-]*")
BASE_RE = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._/-]*")
REPO_RE = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")
DIFFICULTIES = ("easy", "medium", "hard")
# Ticket priority, most urgent first; a ticket without the header is P2.
PRIORITIES = ("P0", "P1", "P2", "P3")
DEFAULT_PRIORITY = "P2"
# Labels whose tickets go ahead of every priority: a fault to fix now.
FIRST_LABELS = ("loom:urgent", "loom:regression", "loom:revert")

HEADER_RE = re.compile(r"(Difficulty|Priority|Model|Base|Not before|After|Deliverable):[ \t]*(.*)")
# "Not before: 2026-10-13T17:00:00Z": the station claims the ticket no
# earlier than that UTC time, and the brain does not brief it before then.
NOT_BEFORE_RE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
# "After: #5, owner/name#7": the ticket builds on those tickets, and is not
# claimed until each of them is merged.
_AFTER_ITEM = r"(?:[A-Za-z0-9._-]+/[A-Za-z0-9._-]+)?#[0-9]+"
AFTER_RE = re.compile(r"%s(?:[ \t]*,[ \t]*%s)*" % (_AFTER_ITEM, _AFTER_ITEM))
_AFTER_REF_RE = re.compile(r"([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)?#([0-9]+)")
# The runner reads the first line starting with one of these, so a body line
# that looks like a header must not follow the real header lines.
FIELD_LINE_RE = re.compile(r"(Repo|Branch|Lane|Issue|Model|Base|Fix-PR|Deliverable):")
SLUG_MAX = 40


def slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(title).lower()).strip("-")
    return s[:SLUG_MAX].strip("-")


def branch_for(number: int, title: str, attempt: int = 1) -> str:
    name = "loom/%d" % int(number)
    s = slug(title)
    if s:
        name += "-" + s
    if attempt > 1:
        name += "-r%d" % attempt
    return name


def ticket_name(repo: str, number: int, attempt: int = 1) -> str:
    if not REPO_RE.fullmatch(repo):
        raise ValueError("expected OWNER/NAME, got %r" % repo)
    name = "%s-%d" % (repo.replace("/", "."), int(number))
    if attempt > 1:
        name += "-r%d" % attempt
    return name


# Each header's rule: (is the value usable, the message when it is not).
_HEADER_RULES = {
    "Difficulty": (lambda v: v in DIFFICULTIES,
                   lambda v: "Difficulty: must be one of %s" % ", ".join(DIFFICULTIES)),
    "Priority": (lambda v: v in PRIORITIES,
                 lambda v: "Priority: must be one of %s" % ", ".join(PRIORITIES)),
    "Model": (lambda v: bool(MODEL_RE.fullmatch(v)),
              lambda v: "Model: %r is not a model id" % v),
    "Base": (lambda v: bool(BASE_RE.fullmatch(v)),
             lambda v: "Base: %r is not a branch name" % v),
    "Not before": (lambda v: bool(NOT_BEFORE_RE.fullmatch(v)),
                   lambda v: "Not before: %r is not a UTC time like 2026-10-13T17:00:00Z" % v),
    "After": (lambda v: bool(AFTER_RE.fullmatch(v)),
              lambda v: "After: %r is not a list of tickets like '#5' or "
                        "'owner/name#7, #8'" % v),
    "Deliverable": (lambda v: v == "answer",
                    lambda v: "Deliverable: must be answer"),
}


def _check_header(key: str, value: str) -> None:
    ok, message = _HEADER_RULES[key]
    if not ok(value):
        raise ValueError(message(value))


def parse(body: Optional[str]) -> Tuple[Dict[str, str], str]:
    """Split the leading Difficulty:, Model: and Base: lines from the rest.

    Returns (headers in the order they appeared, the remaining body). Raises
    ValueError for a value the runner would refuse, a repeated header, or a
    later body line the runner would mistake for a header.
    """
    lines = (body or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    headers: Dict[str, str] = {}
    i = 0
    while i < len(lines):
        m = HEADER_RE.fullmatch(lines[i].rstrip())
        if not m:
            break
        key, value = m.group(1), m.group(2).strip()
        if key == "Difficulty":
            # A hand-edited "Difficulty: Medium" failed the check, so the
            # ticket read as having no tier and no lane ever claimed it
            # (2026-10-16).
            value = value.lower()
        if key in headers:
            if headers[key] == value:
                # The same line twice (a brief whose file repeated the header
                # the brief tool also writes) says one thing; keep it.
                i += 1
                continue
            raise ValueError("%s: given twice" % key)
        _check_header(key, value)
        headers[key] = value
        i += 1
    rest = lines[i:]
    while rest and not rest[0].strip():
        rest.pop(0)
    for line in rest:
        if FIELD_LINE_RE.match(line):
            raise ValueError("body line %r looks like a brief header"
                             % line.split(":")[0])
    return headers, "\n".join(rest).rstrip("\n")


def priority_of(issue: Dict[str, Any]) -> str:
    """The ticket's Priority: header, or DEFAULT_PRIORITY when it has none or
    its headers do not parse."""
    try:
        return parse(issue.get("body"))[0].get("Priority", DEFAULT_PRIORITY)
    except ValueError:
        return DEFAULT_PRIORITY


def _names(issue: Dict[str, Any]) -> set:
    return {str(x.get("name") if isinstance(x, dict) else x)
            for x in issue.get("labels") or []}


def work_order(repo: str, issue: Dict[str, Any]) -> Tuple[Any, ...]:
    """The sort key every queue uses: a FIRST_LABELS ticket first, then by
    priority (P0 first), then the oldest."""
    return (not (_names(issue) & set(FIRST_LABELS)), PRIORITIES.index(priority_of(issue)),
            str(issue.get("created_at") or ""), repo, int(issue.get("number") or 0))


def priority_for_impact(impact: float) -> str:
    """The priority band of a health finding's impact (affected x hours)."""
    for floor, band in ((200, "P0"), (50, "P1"), (10, "P2")):
        if impact >= floor:
            return band
    return "P3"


def with_header(body: str, key: str, value: str) -> str:
    """The body with its `key:` header line set to value, added after the
    other header lines when it has none."""
    lines = (body or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    header = 0
    while header < len(lines) and HEADER_RE.fullmatch(lines[header].rstrip()):
        header += 1
    line = "%s: %s" % (key, value)
    for i in range(header):
        if lines[i].startswith(key + ":"):
            lines[i] = line
            return "\n".join(lines)
    if header == 0:
        return "\n".join([line, ""] + lines) if (body or "").strip() else line + "\n"
    return "\n".join(lines[:header] + [line] + lines[header:])


class Fix:
    """A loom:fix comment: the lane's open pull request and what to fix on it."""

    def __init__(self, pr: int, branch: str, findings: str = "") -> None:
        self.pr = pr
        self.branch = branch
        self.findings = findings
        self._findings_list: Optional[List[Finding]] = None

    @property
    def findings_list(self) -> List[Finding]:
        if self._findings_list is None:
            self._findings_list = parse_findings(self.findings)
        return self._findings_list

    def has_all_dispositions(self) -> bool:
        """Return True if every finding carries a disposition line. A finding
        without one stays None (undispositioned); 'does_not_apply' counts only
        when it was stated."""
        return all(f.disposition for f in self.findings_list)


class Finding:
    """A single pre-roll finding with a stable ID and disposition."""

    DISPOSITIONS = ("fixed", "existing_ticket", "does_not_apply")
    
    def __init__(self, id: str, text: str, location: Optional[Tuple[str, int, int]] = None,
                 disposition: Optional[str] = None, ticket: Optional[str] = None) -> None:
        if not re.fullmatch(r"F[0-9]+", id):
            raise ValueError("Finding ID must be F<number>")
        if disposition is not None and disposition not in self.DISPOSITIONS:
            raise ValueError("Disposition must be one of %s" % ", ".join(self.DISPOSITIONS))
        self.id = id
        self.text = text
        self.location = location
        self.disposition = disposition
        self.ticket = ticket

    def to_dict(self) -> Dict[str, Any]:
        result = {"id": self.id, "text": self.text, "disposition": self.disposition}
        if self.location:
            result["location"] = [self.location[0], self.location[1], self.location[2]]
        if self.ticket:
            result["ticket"] = self.ticket
        return result

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "Finding":
        return Finding(
            id=data.get("id", ""),
            text=data.get("text", ""),
            location=tuple(data["location"]) if "location" in data else None,
            disposition=data.get("disposition"),
            ticket=data.get("ticket")
        )


def _thread_finding(thread: Dict[str, str], comments: List[str],
                    disposition: Optional[str], value: Optional[str]) -> Finding:
    finding = Finding(
        id=thread["id"],
        text="\n".join(comments).rstrip("\n"),
        location=(thread["path"], int(thread["line"]), int(thread["comment_id"])),
        disposition=disposition,
    )
    if disposition == "existing_ticket" and value:
        finding.ticket = value
    return finding


_THREAD_RE = re.compile(r"^### Thread(?: ([A-Za-z0-9]+))? \(comment ([0-9]+)\) at (.+):(\d+)$")


def _disposition_line(stripped: str) -> Optional[Tuple[str, Optional[str]]]:
    """The disposition a finding line states: "Fixing: <ticket>", "Fixed" or
    "Does not apply"; None for an ordinary comment line."""
    fixing_match = FIXING_DISPOSITION_RE.match(stripped)
    if fixing_match:
        return "existing_ticket", fixing_match.group(1)
    if stripped == "Fixed":
        return "fixed", None
    if stripped == "Does not apply":
        return "does_not_apply", None
    return None


def _thread_header(stripped: str) -> Optional[Dict[str, str]]:
    """The thread a "### Thread" header line opens, or None for any other
    line. Its "id" is empty when the header names none."""
    m = _THREAD_RE.match(stripped)
    if not m:
        return None
    return {"id": m.group(1) or "", "comment_id": m.group(2),
            "path": m.group(3), "line": m.group(4)}


def _append_thread(findings: List[Finding], thread: Optional[Dict[str, str]],
                   comments: List[str], disposition: Optional[Tuple[str, Optional[str]]]) -> None:
    if thread:
        kind, value = disposition or (None, None)
        findings.append(_thread_finding(thread, comments, kind, value))


def parse_findings(body: str) -> List[Finding]:
    """Parse findings from a loom:fix comment body.

    Each "### Thread [<id>] (comment <id>) at <path>:<line>" header starts a
    finding; a header without an ID gets the next F<number>. A finding
    without a disposition line remains undispositioned.
    """
    findings: List[Finding] = []
    current_thread: Optional[Dict[str, str]] = None
    current_comments: List[str] = []
    disposition: Optional[Tuple[str, Optional[str]]] = None
    next_id = 1

    for line in body.split("\n"):
        stripped = line.strip()
        header = _thread_header(stripped)
        if header:
            _append_thread(findings, current_thread, current_comments, disposition)
            if not header["id"]:
                header["id"] = "F%d" % next_id
                next_id += 1
            current_thread, current_comments, disposition = header, [], None
        elif current_thread:
            stated = _disposition_line(stripped)
            if stated:
                disposition = stated
            else:
                current_comments.append(line)
    _append_thread(findings, current_thread, current_comments, disposition)
    return findings


FIX_MARKER = "<!-- loom:fix v1 -->"
FIX_PR_RE = re.compile(r"Pull request: ([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)#([0-9]+), "
                       r"branch (loom/[A-Za-z0-9._/-]+)[ \t]*")
FIXING_DISPOSITION_RE = re.compile(r"^Fixing: (.+)$")
# Lines in the findings the runner could read as a brief header or a section
# of the ticket get indented, which makes them plain quoted text.
_FIX_UNSAFE_RE = re.compile(r"(Repo|Branch|Lane|Issue|Model|Base|Difficulty|Fix-PR):|#{1,2} ")


def parse_fix(repo: str, body: Optional[str]) -> Fix:
    """A loom:fix comment written by bus.brain fix. Raises ValueError when it
    names another repo or a branch the runner would refuse."""
    lines = (body or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if not lines or lines[0].strip() != FIX_MARKER:
        raise ValueError("not a loom:fix comment")
    m = FIX_PR_RE.fullmatch(lines[1]) if len(lines) > 1 else None
    if not m:
        raise ValueError("loom:fix comment names no pull request and branch")
    if m.group(1) != repo:
        raise ValueError("loom:fix comment names a pull request in another repo")
    rest = ["    " + l if _FIX_UNSAFE_RE.match(l) else l for l in lines[2:]]
    return Fix(int(m.group(2)), m.group(3), "\n".join(rest).strip("\n"))


def render_findings(findings: List[Finding]) -> str:
    """Render findings with their dispositions for a loom:fix comment.
    
    Format:
    ### Thread <id> (comment <id>) at <path>:<line>
    <comment text>
    <comment text>
    <disposition line if any>
    """
    lines = []
    for f in findings:
        lines.append("### Thread %s (comment %s) at %s:%s" % (
            f.id, f.location[2] if f.location else "?",
            f.location[0] if f.location else "?",
            f.location[1] if f.location else "?"))
        for line in f.text.split("\n"):
            lines.append(line)
        if f.disposition:
            if f.disposition == "existing_ticket" and not f.ticket:
                raise ValueError("finding %s has existing_ticket with no ticket" % f.id)
            line = {"existing_ticket": "Fixing: %s" % f.ticket, "fixed": "Fixed",
                    "does_not_apply": "Does not apply"}.get(f.disposition, "")
            # The line must read back as this disposition and ticket, or the
            # record would silently say something other than what was decided.
            ticket = f.ticket if f.disposition == "existing_ticket" else None
            if _disposition_line(line) != (f.disposition, ticket) or (f.ticket and not ticket):
                raise ValueError("finding %s has disposition %s with ticket %s, which "
                                 "its disposition line cannot carry"
                                 % (f.id, f.disposition, f.ticket))
            lines.append(line)
        lines.append("")
    return "\n".join(lines).strip("\n")


# How a fix brief's agent answers a finding it judges invalid. The runner
# moves the folder out of the tree (lanes/run_lane.sh keep_answers) and the
# station posts each answer on its thread (bus/station.py).
ANSWER_NOTE = (
    "A review thread whose finding is not valid against the current code can be "
    "answered instead of fixed: write the reason, in a few plain sentences that "
    "point at the code, to `.loom-answers/<thread id>.md` at the top of the tree "
    "(the id after `### Thread`, for example `.loom-answers/PRRT_abc123.md`), and "
    "change nothing for it. The runner posts each answer as the reply on its "
    "thread and never commits that folder. An answer settles a medium or low "
    "finding on a strong model's round; anything else still needs a fix.")


def render_brief(repo: str, issue: Dict[str, Any], lane: str, attempt: int = 1,
                  fix: Optional[Fix] = None,
                  ci_checks: Optional[List[Tuple[str, str]]] = None,
                  has_contract: bool = True) -> str:
    number = int(issue["number"])
    title = " ".join(str(issue.get("title") or "").split())
    headers, body = parse(issue.get("body"))
    out: List[str] = [
        "# Brief: %s" % ticket_name(repo, number, attempt),
        "",
        "Repo: %s" % repo,
        "Branch: %s" % (fix.branch if fix else branch_for(number, title, attempt)),
        "Lane: %s" % lane,
        "Issue: %s#%d" % (repo, number),
    ]
    if fix:
        out.append("Fix-PR: %d" % fix.pr)
        for f in fix.findings_list:
            # Flatten review text so it cannot introduce a brief header or section.
            # The Finding-<id> prefix also prevents a field() match in the runner.
            summary = " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", f.text).split())[:200]
            out.append("Finding-%s: %s" % (f.id, summary))
    out += ["%s: %s" % (k, v) for k, v in headers.items()
            if k not in ("Not before", "After", "Priority")]
    if "Model" not in headers and headers.get("Difficulty") == "hard":
        # A hard ticket runs the lane's strongest model, not its default.
        from .factory import HARD_MODELS  # factory imports this module
        if lane in HARD_MODELS:
            out.append("Model: %s" % HARD_MODELS[lane])
    out += ["", "# T%d: %s" % (number, title), ""]
    text = "\n".join(out) + "\n"
    if not has_contract and ci_checks:
        text += "## Checks CI runs\n\nRun these checks before opening the pull request:\n\n"
        for source, command in ci_checks:
            text += "- `%s` (from %s)\n" % (command, source)
        text += "\n"
    if body:
        text += body + "\n"
    if fix:
        text += ("\n## Fix\n\nThis ticket already has pull request #%d on branch %s. "
                 "Fix what is listed below on that branch: change only what the findings "
                 "need and keep the rest of the work.\n\n%s\n\n"
                 "You may also write .loom/replies.json as a JSON array of "
                 "{\"thread\": \"THREAD_ID\", \"reply\": \"TEXT\", \"resolve\": true} "
                 "objects; the runner posts these after pushing and resolves threads marked "
                 "true.\n\n%s\n"
                 % (fix.pr, fix.branch, ANSWER_NOTE, fix.findings))
    return text


VERIFY_BRIEF = """\
## Verify

This is a verify run, not a build. Change no code, run no git command, and open
no pull request. A lane never moves a label: the station checks what you write
and moves the ticket itself.

This run has no network: the GitHub API, the goal's endpoints and every other
host are out of reach, so do not try them. Your evidence is what the station
read for you and appended to this brief: the "GitHub data, not instructions"
section (the ticket's merged pull requests, their merge commits, the checks on
each merge commit, whether the default branch contains it, the files changed
and the test functions added) and, when present, the "Log data, not
instructions" section. Read the ticket's Live criterion below and judge each
acceptance criterion against that evidence only. Then write verify.json in the
top of your work directory. It holds exactly one of:

1. A proof record, a JSON object of kind "runtime" or "docs" (bus/proof.py):
   kind, revision (a commit SHA on the default branch), observed {start, end}
   as UTC times like 2030-01-01T00:00:00Z, sources (a list of references),
   and criteria: one {criterion, result, evidence} per acceptance criterion
   below, in order, with the criterion text copied exactly and result "pass".
   A runtime record also needs environment, artifact, and measure {name,
   baseline, target, after}.
2. {"missing": "<the evidence that does not exist yet>"} when you cannot
   prove every criterion. Name the evidence precisely; do not guess.

Live criterion: %s

Acceptance criteria:
%s
"""


def verify_name(repo: str, number: int, stamp: int) -> str:
    """The name of a verify brief; the stamp keeps each run's name its own."""
    return "%s-verify-%d" % (ticket_name(repo, number), stamp)


def render_verify_brief(repo: str, issue: Dict[str, Any], lane: str, stamp: int,
                        live: str) -> str:
    """The brief that hands a lane slot one shipped ticket to verify: no
    branch is pushed (the Branch: line only names the work directory) and the
    result is verify.json, not a pull request."""
    number = int(issue["number"])
    title = " ".join(str(issue.get("title") or "").split())
    wanted = "\n".join("- %s" % " ".join(text.split())
                       for _, text in proof.criteria(str(issue.get("body") or "")))
    head = ["# Brief: %s" % verify_name(repo, number, stamp), "", "Repo: %s" % repo,
            "Branch: loom/verify-%d-%d" % (number, stamp), "Lane: %s" % lane,
            "Issue: %s#%d" % (repo, number), "Deliverable: verify", "",
            "# T%d: Verify %s" % (number, title), ""]
    return "\n".join(head) + "\n" + VERIFY_BRIEF % (live, wanted)


def _login(value: Any) -> str:
    return str((value or {}).get("login") or "").lower() if isinstance(value, dict) else ""


def issue_owner(issue: Dict[str, Any]) -> str:
    """The repo owner an issue belongs to, from its API or web URL."""
    for key, pattern in (("repository_url", r"/repos/([^/]+)/[^/]+/?$"),
                         ("html_url", r"^https://[^/]+/([^/]+)/[^/]+/(?:issues|pull)/\d+$")):
        m = re.search(pattern, str(issue.get(key) or ""))
        if m:
            return m.group(1)
    return ""


def _latest_label_actor(events: Iterable[Dict[str, Any]], match) -> Optional[str]:
    actor: Optional[str] = None
    for ev in events:
        if not isinstance(ev, dict) or ev.get("event") != "labeled":
            continue
        name = str((ev.get("label") or {}).get("name") or "")
        if match(name):
            actor = _login(ev.get("actor"))
    return actor


def _labeller_problem(events: List[Dict[str, Any]], allowed: set) -> Optional[str]:
    """Why the latest loom:briefed or lane: labeller is not trusted, if so."""
    for label, what, match in (
            ("loom:briefed", "loom:briefed", lambda n: n == "loom:briefed"),
            ("lane:", "the lane label", lambda n: n.startswith("lane:"))):
        actor = _latest_label_actor(events, match)
        if actor is None:
            return "no %s labelled event found" % label
        if actor not in allowed:
            return "%s was applied by %s, not in LOOM_BRIEF_AUTHORS" % (what, actor or "unknown")
    return None


def trusted(issue: Dict[str, Any], events: List[Dict[str, Any]],
            editor_login: Optional[str], authors: Iterable[str],
            owners: Iterable[str]) -> Tuple[bool, str]:
    """Every rule in docs/BRAIN.md "Trust"; the reason says which one failed."""
    allowed = {a.lower() for a in authors}
    author = _login(issue.get("user"))
    if not author or author not in allowed:
        return False, "issue author %s is not in LOOM_BRIEF_AUTHORS" % (author or "unknown")
    problem = _labeller_problem(events, allowed)
    if problem:
        return False, problem
    if editor_login is not None and editor_login.lower() not in allowed:
        return False, "body last edited by %s, not in LOOM_BRIEF_AUTHORS" % editor_login
    owner = issue_owner(issue).lower()
    if not owner or owner not in {o.lower() for o in owners}:
        return False, "repo owner %s is not in LOOM_REPO_OWNERS" % (owner or "unknown")
    return True, "ok"


def not_before(headers: Dict[str, str]) -> Optional[float]:
    """The ticket's Not before: time as epoch seconds, or None."""
    value = headers.get("Not before")
    if not value:
        return None
    return calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ"))


# The state labels a ticket carries, in the order they are looked for.
TICKET_STATES = ("queued", "briefed", "running", "pr", "ready", "failed",
                 "merged", "closed-unmerged", "shipped", "verified")
# A ticket at or past merged: its pull request landed.
MERGED_STATES = ("merged", "shipped", "verified")


class After(NamedTuple):
    """One ticket named in an After: line, and what it says about the ticket
    waiting for it."""

    repo: str      # OWNER/NAME, the waiting ticket's own repo for a bare "#5"
    number: int
    short: str     # "#5" or "owner/name#7", as the line wrote it
    why: str       # dependency_state: why it still holds, or "not-planned"


def ticket_state(issue: Dict[str, Any]) -> str:
    """The ticket's own state label ("queued", "pr", "merged", ...), or "".
    A merged, shipped or verified label wins over an earlier state's label
    left beside it (a stale loom:ready on a shipped ticket): the pull request
    has landed, so the earlier stage is over. The furthest of them wins."""
    names = _names(issue)
    landed = [s for s in MERGED_STATES if "loom:" + s in names]
    if landed:
        return landed[-1]
    return next((s for s in TICKET_STATES if "loom:" + s in names), "")


def dependency_state(issue: Optional[Dict[str, Any]]) -> str:
    """Whether the ticket named in an After: line is merged yet.

    "merged" once its state label is merged, shipped or verified, "closed"
    for a closed ticket closed as completed, "not-planned" for one closed as
    not planned (which releases the hold), its own state label ("queued",
    "pr", ...) while it is still open, "none" when it carries no state
    label, and "unreadable" when it could not be read at all. A label is
    enough: the station reconciles pull requests to their terminal state
    before it claims anything.
    """
    if not isinstance(issue, dict):
        return "unreadable"
    if issue.get("state") == "closed":
        return "not-planned" if issue.get("state_reason") == "not_planned" else "closed"
    return ticket_state(issue) or "none"


def after_refs(headers: Dict[str, str]) -> List[Tuple[str, int]]:
    """The After: line as (repo, number) pairs; repo is "" for this repo.

    Raises ValueError for a value that is not a comma separated list of
    "#N" and "owner/name#N", the same rule parse applies."""
    if "After" not in headers:
        return []
    value = headers["After"]
    if not AFTER_RE.fullmatch(value):
        _check_header("After", value)
    return [(m.group(1) or "", int(m.group(2)))
            for m in _AFTER_REF_RE.finditer(value)]


def after_holds(repo: str, headers: Dict[str, str],
                lookup: Callable[[str, int], Optional[Dict[str, Any]]]
                ) -> Tuple[List[After], List[After]]:
    """The After: line resolved against the tickets it names.

    `lookup(repo, number)` reads one ticket and returns None when it cannot
    be read. Returns (still holding, no longer holding): the tickets that
    are not merged yet, and those closed as not planned, which release the
    hold on their own."""
    holds: List[After] = []
    released: List[After] = []
    for named, number in after_refs(headers):
        short = "%s#%d" % (named, number) if named else "#%d" % number
        where = named or repo
        state = dependency_state(lookup(where, number))
        if state in MERGED_STATES or state == "closed":
            continue
        entry = After(where, number, short, state)
        (released if state == "not-planned" else holds).append(entry)
    return holds, released
