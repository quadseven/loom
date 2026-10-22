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
import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

from . import proof, status

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


def _utc_timestamp(value: Any) -> float:
    parsed = (value if isinstance(value, datetime)
              else datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def merged_before_regression_requeue(issue: Dict[str, Any], merged_at: Any,
                                     events: Iterable[Dict[str, Any]]) -> bool:
    """Whether a merged PR predates this regression ticket's latest re-queue."""
    if not merged_at or "loom:regression" not in {
            label.get("name") for label in issue.get("labels", [])}:
        return False
    return merged_before_requeue(merged_at, events)


def merged_before_requeue(merged_at: Any, events: Iterable[Dict[str, Any]]) -> bool:
    """Whether a merged PR predates the ticket's latest loom:queued or
    loom:briefed label: the ticket was sent back for work after that merge,
    so the merge cannot deliver it."""
    if not merged_at:
        return False
    requeues = [_utc_timestamp(event["created_at"]) for event in events
                if isinstance(event, dict) and event.get("event") == "labeled"
                and (event.get("label") or {}).get("name") in ("loom:queued", "loom:briefed")
                and event.get("created_at")]
    return bool(requeues) and max(requeues) > _utc_timestamp(merged_at)


# Status events the station writes about a merged pull request it has already
# judged for a ticket, before any build lane runs (loom#1385). The texts are
# the record: the next pass reads them back, so a pull request is judged once.
UNBUILT_NOTE = ("Pull request #%d merged and left ticket criteria unbuilt, so the ticket "
                "is back at loom:queued for the rest: %s")
MERGED_FIRST_NOTE = ("Pull request #%d (merged %s, merge commit %s) may already carry this "
                     "ticket's work, so the ticket goes to a verify run before any build.")
REFUTED_NOTE = ("A verify run found that pull request #%d does not meet this ticket's "
                "criteria: %s. The ticket is back at loom:queued for a build, and that "
                "pull request is not checked again.")
_UNBUILT_RE = re.compile(r"^Pull request #(\d+) merged and left ticket criteria unbuilt")
_MERGED_FIRST_RE = re.compile(r"^Pull request #(\d+) \(merged [^)]*\) may already carry this "
                              r"ticket's work")
_REFUTED_RE = re.compile(r"^A verify run found that pull request #(\d+) does not meet")


def judged_pulls(texts: Iterable[str]) -> List[int]:
    """The pull requests the station has already judged for a ticket, from the
    texts of its status events: one that left criteria unbuilt (the ticket was
    sent back for the rest) or one sent to a verify run before a build. A
    judged pull request is not sent to a verify run again."""
    return sorted({int(m.group(1)) for text in texts for rx in (_UNBUILT_RE, _MERGED_FIRST_RE)
                   for m in [rx.match(str(text))] if m})


def pending_verifies(texts: Iterable[str]) -> List[int]:
    """The pull requests sent to a verify run before a build whose verify run
    has not yet found them wanting."""
    texts = [str(t) for t in texts]
    sent = {int(m.group(1)) for t in texts for m in [_MERGED_FIRST_RE.match(t)] if m}
    refuted = {int(m.group(1)) for t in texts for m in [_REFUTED_RE.match(t)] if m}
    return sorted(sent - refuted)


REWRITTEN_FROM_RE = re.compile(r"^Rewritten from issue (\d+)\.[ \t]*$", re.M)


def rewritten_source(body: Any) -> Optional[int]:
    """The issue a ticket names with its "Rewritten from issue M." line, or
    None. Only that line: a bare Refs line may name a goal or an unrelated issue."""
    found = REWRITTEN_FROM_RE.search(without_original(str(body or "")))
    return int(found.group(1)) if found else None


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
# An issue rewritten in place (brain.brief --refs, loom#1229) keeps its
# original title and text under this last section, quoted line by line, so
# no reader takes a header, section, checkbox or Refs line from it and the
# lane pull request body (which copies named sections) never carries it.
ORIGINAL_HEADING = "## Original issue"


def with_original(body: str, issue: Dict[str, Any]) -> str:
    """The ticket body followed by the issue's original title and text,
    quoted in a collapsed section. The issue's status section (loom#1341)
    is not quoted: it stays at the end as a live section."""
    kept = status.body_events(issue.get("body"))
    text = status.strip_body(issue.get("body")).replace("\r\n", "\n").replace("\r", "\n")
    lines = ["Original title: %s" % " ".join(str(issue.get("title") or "").split()), ""]
    lines += text.rstrip("\n").split("\n")
    quoted = "\n".join(("> " + line) if line.strip() else ">" for line in lines)
    out = "%s\n\n%s\n\n<details><summary>Original issue</summary>\n\n%s\n\n</details>\n" % (
        body.rstrip("\n"), ORIGINAL_HEADING, quoted)
    try:
        return status.with_body_events(out, kept) if kept else out
    except ValueError:
        return out


def without_original(body: str) -> str:
    """The body without the original text an in-place rewrite kept."""
    found = re.search(r"^%s[ \t]*$" % re.escape(ORIGINAL_HEADING), body, re.M)
    return body[:found.start()] if found else body


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
    # The status section the station keeps at the end of the body (loom#1341)
    # is not the ticket's text.
    lines = status.strip_body(body).replace("\r\n", "\n").replace("\r", "\n").split("\n")
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


# Where a run that timed out or hit a limit after editing left its edits (lanes/run_lane.sh
# preserve_timedout), as the failure reason names it.
TIMEDOUT_RE = re.compile(r"the edits are on branch ([A-Za-z0-9._/-]+-timedout-[0-9]+)")


def timedout_branch(texts: List[str]) -> Optional[str]:
    """The newest timed-out branch any of `texts` (oldest first) names."""
    found = None
    for text in texts:
        match = TIMEDOUT_RE.search(text)
        if match:
            found = match.group(1)
    return found


# What a brief carries besides the ticket body (Station._claim): the newest
# NOTES_MAX operator comments, each clipped to NOTE_CHARS, and up to
# LINKED_MAX pull requests that reference the ticket. On one ticket the
# operator wrote that the merged pull request had changed the wrong target and
# the fix was in two other repos; neither the comment nor those merges reached
# the next brief, and a lane redid the wrong target (2026-10-22).
NOTES_MAX = 5
NOTE_CHARS = 1500
LINKED_MAX = 10
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def operator_notes(comments: Iterable[Any], logins: Iterable[str]) -> List[Tuple[str, str, str]]:
    """(login, created_at, body) of the newest NOTES_MAX comments a person
    wrote under one of `logins` (the operator's logins and trusted briefers),
    oldest first. A [bot] login, a factory marker comment and a claim line
    are the factory's own, not notes."""
    allowed = {login.lower() for login in logins}
    found = []
    for c in comments:
        if not isinstance(c, dict):
            continue
        login, body = _login(c.get("user")), str(c.get("body") or "").strip()
        if (login in allowed and not login.endswith("[bot]") and body
                and not body.startswith(("<!--", "Claimed for lane "))):
            found.append((login, str(c.get("created_at") or ""), body))
    return found[-NOTES_MAX:]


def linked_pulls(events: Any) -> List[str]:
    """One line per pull request, in any repo, that a cross-reference event on
    the ticket's timeline names: "owner/name#N, merged YYYY-MM-DD: title" or
    "owner/name#N, open: title"; newest LINKED_MAX. A pull request closed
    without merging is left out."""
    found: Dict[str, str] = {}
    for event in events if isinstance(events, list) else []:
        if not isinstance(event, dict) or event.get("event") != "cross-referenced":
            continue
        ref = (event.get("source") or {}).get("issue") or {}
        pull, number = ref.get("pull_request"), ref.get("number")
        where = str((ref.get("repository") or {}).get("full_name") or "")
        if not isinstance(pull, dict) or not isinstance(number, int) or not REPO_RE.fullmatch(where):
            continue
        merged = str(pull.get("merged_at") or "")
        if merged:
            state = "merged %s" % merged[:10]
        elif ref.get("state") == "open":
            state = "open"
        else:
            continue
        title = " ".join(_CONTROL_RE.sub(" ", str(ref.get("title") or "")).split())[:120]
        found["%s#%d" % (where, number)] = "%s#%d, %s: %s" % (where, number, state, title)
    return list(found.values())[-LINKED_MAX:]


def _context_text(notes: Sequence[Tuple[str, str, str]], linked: Sequence[str]) -> str:
    """The brief's Operator notes and Linked pull requests sections. Each
    note line is quoted, so none reads as a brief header (the runner's field
    lookup matches a line start)."""
    text = ""
    if notes:
        text += ("\n## Operator notes\n\nThe newest comments on this ticket by its operator, "
                 "oldest first. Where a note disagrees with the ticket above, the note wins: a "
                 "note that says the work is done, or that it belongs to another target or repo, "
                 "decides what you change.\n")
        for login, at, body in notes:
            clipped = body if len(body) <= NOTE_CHARS else body[:NOTE_CHARS] + " [clipped]"
            lines = _CONTROL_RE.sub(" ", clipped.replace("\r\n", "\n").replace("\r", "\n")).split("\n")
            text += "\n%s, %s:\n%s\n" % (login, at, "\n".join("> " + line for line in lines))
    if linked:
        text += ("\n## Linked pull requests\n\nPull requests in any repo that reference this "
                 "ticket. A merged one may already do this ticket's work: read it before you "
                 "change anything.\n\n" + "".join("- %s\n" % line for line in linked))
    return text


def render_brief(repo: str, issue: Dict[str, Any], lane: str, attempt: int = 1,
                  fix: Optional[Fix] = None,
                  ci_checks: Optional[List[Tuple[str, str]]] = None,
                  has_contract: bool = True, resume: Optional[str] = None,
                  notes: Sequence[Tuple[str, str, str]] = (),
                  linked: Sequence[str] = ()) -> str:
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
    text += _context_text(notes, linked)
    if resume:
        text += ("\n## Earlier attempt\n\nAn earlier attempt ended (a timeout or a limit) after "
                 "editing, and the runner kept its edits on branch %s. The runner merges them into this tree "
                 "before you start (the sandbox keeps .git read-only, so never fetch): check "
                 "what is there and finish the ticket instead of starting over.\n" % resume)
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
   baseline, target, after}. The revision must be a merge commit the GitHub
   data section shows, or a later commit on the default branch, and
   observed.start must not be before that pull request merged: the station
   refuses a proof of a stale build or of evidence seen before the merge.
   For a criterion that starts with "Live", the evidence quotes what was seen
   (a log line, a response, a page) and names the UTC time it was seen, like
   2030-01-01T00:00:00Z; never what "would" or "should" happen. The station
   refuses live evidence without that time.
2. {"missing": "<the evidence that does not exist yet>"} when you cannot
   prove every criterion. Name the evidence precisely; do not guess.

Live criterion: %s

Acceptance criteria:
%s

A runtime proof template with every required key and these criteria filled in
(replace every <...> value; keep the keys and the criterion texts as they are):

```json
%s
```
"""


# A criterion a live observation proves: the station refuses its evidence
# unless it names the time it was seen (station.unseen_live_evidence).
LIVE_CRITERION_RE = re.compile(r"^\s*live\b", re.I)


def verify_template(criteria: List[str]) -> str:
    """The runtime proof skeleton a verify brief shows (loom#714): lanes that
    built the record from prose alone left out observed or environment, and
    bus/proof.py refused the proof for its shape, not its evidence."""
    return json.dumps({
        "kind": "runtime",
        "revision": "<the commit SHA on the default branch that carries the change>",
        "environment": "<where the change ran when you observed it>",
        "artifact": "<the log, record or output you read>",
        "observed": {"start": "<UTC time like 2030-01-01T00:00:00Z>",
                     "end": "<UTC time like 2030-01-01T01:00:00Z>"},
        "sources": ["<a reference to each piece of evidence>"],
        "criteria": [{"criterion": text, "result": "pass",
                      "evidence": ("<what you saw, quoted, and the UTC time you saw it>"
                                   if LIVE_CRITERION_RE.match(text) else "<what shows it>")}
                     for text in criteria],
        "measure": {"name": "<what you measured>", "baseline": "<before>",
                    "target": "<the goal>", "after": "<after>"},
    }, indent=2)


def verify_name(repo: str, number: int, stamp: int) -> str:
    """The name of a verify brief; the stamp keeps each run's name its own."""
    return "%s-verify-%d" % (ticket_name(repo, number), stamp)


def merged_first_section(pulls: Iterable[int]) -> str:
    """The verify brief's section for a ticket sent to verification before any
    build: it tells the lane the one extra answer it may give."""
    names = ", ".join("#%d" % n for n in pulls)
    return ("\n## Sent to verification before any build\n\n"
            "The station found merged pull request %s linked to this ticket before any "
            "build lane ran, so it asks whether that change already does the ticket's "
            "work. Judge each acceptance criterion against the merged pull request's "
            "facts as usual. When the merged change does not meet one or more of them, "
            "write verify.json as {\"unmet\": \"<which criteria it does not meet and "
            "why>\"} instead: the station then queues the ticket for a build and does not "
            "send that pull request to a verify run again.\n" % names)


def render_verify_brief(repo: str, issue: Dict[str, Any], lane: str, stamp: int,
                        live: str) -> str:
    """The brief that hands a lane slot one shipped ticket to verify: no
    branch is pushed (the Branch: line only names the work directory) and the
    result is verify.json, not a pull request."""
    number = int(issue["number"])
    title = " ".join(str(issue.get("title") or "").split())
    texts = [text for _, text in proof.criteria(str(issue.get("body") or ""))]
    wanted = "\n".join("- %s" % " ".join(text.split()) for text in texts)
    head = ["# Brief: %s" % verify_name(repo, number, stamp), "", "Repo: %s" % repo,
            "Branch: loom/verify-%d-%d" % (number, stamp), "Lane: %s" % lane,
            "Issue: %s#%d" % (repo, number), "Deliverable: verify", "",
            "# T%d: Verify %s" % (number, title), ""]
    return "\n".join(head) + "\n" + VERIFY_BRIEF % (live, wanted, verify_template(texts))


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


# The settings that name a briefing login, as a refusal names them.
_BRIEFING = "LOOM_BRIEF_AUTHORS or LOOM_TRUSTED_BRIEFERS"


def _labeller_problem(events: List[Dict[str, Any]], allowed: set) -> Optional[str]:
    """Why the latest loom:briefed or lane: labeller is not trusted, if so."""
    for label, what, match in (
            ("loom:briefed", "loom:briefed", lambda n: n == "loom:briefed"),
            ("lane:", "the lane label", lambda n: n.startswith("lane:"))):
        actor = _latest_label_actor(events, match)
        if actor is None:
            return "no %s labelled event found" % label
        if actor not in allowed:
            return "%s was applied by %s, not in %s" % (what, actor or "unknown", _BRIEFING)
    return None


_LABELLER_REFUSAL_RE = re.compile(r"^(?:loom:briefed|the lane label) was applied by (\S+), not in ")


def refused_labeller(reason: str) -> Optional[str]:
    """The login a trust refusal says applied a label it does not trust, or
    None when the refusal is for another cause."""
    m = _LABELLER_REFUSAL_RE.match(reason)
    return m.group(1) if m and m.group(1) != "unknown" else None


def briefing_logins(authors: Iterable[str], briefers: Iterable[str] = ()) -> List[str]:
    """The lower-case logins whose briefs this Loom builds: its own
    LOOM_BRIEF_AUTHORS, then LOOM_TRUSTED_BRIEFERS (docs/LANES.md, "Trusted
    briefers"), each once. Briefing trust only: ownership, journals, proofs,
    adoption and merges read LOOM_BRIEF_AUTHORS alone."""
    found: List[str] = []
    for login in list(authors) + list(briefers):
        login = login.lower()
        if login and login not in found:
            found.append(login)
    return found


def _is_ticket(issue: Dict[str, Any]) -> bool:
    return "pull_request" not in issue and "loom:ticket" in _names(issue)


def brief_author_trusted(issue: Dict[str, Any], authors: Iterable[str],
                         briefers: Iterable[str] = (), as_ticket: bool = False) -> bool:
    """Whether the issue's author may brief this Loom: a LOOM_BRIEF_AUTHORS
    login, or a LOOM_TRUSTED_BRIEFERS login on a loom:ticket (a brief is the
    hand-off). as_ticket asks about the issue as it will be once a brief has
    made it the ticket. Every briefing trust check asks this, so they cannot
    drift."""
    author = _login(issue.get("user"))
    if not author:
        return False
    if author in {a.lower() for a in authors}:
        return True
    return author in {b.lower() for b in briefers} and (as_ticket or _is_ticket(issue))


def trusted(issue: Dict[str, Any], events: List[Dict[str, Any]],
            editor_login: Optional[str], authors: Iterable[str],
            owners: Iterable[str], briefers: Iterable[str] = ()) -> Tuple[bool, str]:
    """Every rule in docs/BRAIN.md "Trust"; the reason says which one failed.
    A briefing login is a LOOM_BRIEF_AUTHORS or LOOM_TRUSTED_BRIEFERS one."""
    authors, briefers = list(authors), list(briefers)
    allowed = set(briefing_logins(authors, briefers))
    if not brief_author_trusted(issue, authors, briefers):
        author = _login(issue.get("user"))
        return False, "issue author %s is not in %s" % (author or "unknown", _BRIEFING)
    problem = _labeller_problem(events, allowed)
    if problem:
        return False, problem
    if editor_login is not None and editor_login.lower() not in allowed:
        return False, "body last edited by %s, not in %s" % (editor_login, _BRIEFING)
    owner = issue_owner(issue).lower()
    if not owner or owner not in {o.lower() for o in owners}:
        return False, "repo owner %s is not in LOOM_REPO_OWNERS" % (owner or "unknown")
    return True, "ok"


def assignees(issue: Dict[str, Any]) -> List[str]:
    """The lower-case logins an issue or pull request is assigned to."""
    found = [_login(a) for a in issue.get("assignees") or [] if isinstance(a, dict)]
    if not found and isinstance(issue.get("assignee"), dict):
        found = [_login(issue["assignee"])]
    return [login for login in found if login]


CLAIM_LABEL_PREFIX = "claim:"


def claim_label(login: str) -> str:
    """The label that marks an issue claimed by an operator login."""
    return CLAIM_LABEL_PREFIX + login.lower()


def claimed_by(issue: Dict[str, Any]) -> List[str]:
    """The lower-case logins named by the issue's claim:<login> labels."""
    found = []
    for label in issue.get("labels") or []:
        name = str(label.get("name") if isinstance(label, dict) else label or "")
        if name.lower().startswith(CLAIM_LABEL_PREFIX) and len(name) > len(CLAIM_LABEL_PREFIX):
            found.append(name[len(CLAIM_LABEL_PREFIX):].strip().lower())
    return [login for login in found if login]


def owned_elsewhere(issue: Dict[str, Any], authors: Iterable[str],
                    briefers: Iterable[str] = ()) -> Optional[str]:
    """The login that owns an issue when it is another operator's, else None.

    An issue assigned to a login, or carrying a claim:<login> label, belongs
    to that login (docs/LANES.md, "Two Looms on shared repos"). A Loom acts
    only on issues with no owner and on issues with at least one owner among
    its own operator logins (LOOM_BRIEF_AUTHORS); anything else is left
    alone. A trusted briefer (LOOM_TRUSTED_BRIEFERS) owns nothing for this
    Loom, except that a loom:ticket they authored is handed to it even when
    they assigned or claim-labelled it to themselves."""
    logins = assignees(issue) + claimed_by(issue)
    if not logins:
        return None
    allowed = {a.lower() for a in authors}
    author = _login(issue.get("user"))
    if (author and author not in allowed
            and brief_author_trusted(issue, (), briefers)):
        allowed.add(author)
    if any(login in allowed for login in logins):
        return None
    return logins[0]


# A goal home may name the operator whose Loom works it, near the top of its
# body (before its first heading), as it may name a Trunk: branch.
_OPERATOR_RE = re.compile(r"^Operator:[ \t]*([A-Za-z0-9][A-Za-z0-9-]*(?:\[bot\])?)[ \t]*$")


def goal_operator(issue: Dict[str, Any]) -> Optional[str]:
    """The lower-case login an Operator: line in a goal home's header names,
    or None when the header has none."""
    for line in str(issue.get("body") or "").splitlines():
        if line.startswith("#"):
            break
        m = _OPERATOR_RE.match(line.strip())
        if m:
            return m.group(1).lower()
    return None


def goal_ours(issue: Dict[str, Any], authors: Iterable[str]) -> bool:
    """Whether this Loom works the goal home: it names no operator (it then
    belongs to every LOOM_BRIEF_AUTHORS login), or it names one of them."""
    operator = goal_operator(issue)
    return operator is None or operator in {a.lower() for a in authors}


def operator_login(authors: Iterable[str]) -> Optional[str]:
    """The login a claim assigns its issue to: the first LOOM_BRIEF_AUTHORS
    login that is not an app's ("[bot]"), since GitHub cannot assign an app."""
    return next((a for a in authors if a and not a.lower().endswith("[bot]")), None)


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
