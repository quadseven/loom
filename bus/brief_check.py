"""A stale or already-shipped ticket never reaches a build lane, and a health
ticket names its cause.

An audit of 2026-10-22 found lanes re-doing work that had already shipped
and rewriting working code to match a stale ticket: a rewrite of an issue
whose leak-scan caller and deny-list layer had shipped weeks before read
"out of scope" as "must be absent" and deleted the working deny-list layer,
twice, in two repos. Health tickets were met by changing the measurement: a
health read began counting money the fund's holder carried as fund balance,
so the "fund under its floor" finding vanished while every affected member
stayed affected, and its sibling held money back from refunds so the
balance never read under the floor.

Before the station hands a ticket to a lane (`check`), it reads:

- stale: the operator's newest comment says the work is done or superseded;
  the issue the ticket was rewritten from is already closed as completed; a
  pull request in another repo of the owner that names this ticket in its
  body has merged, or a pull request a person linked in the ticket or its
  comments merged after the ticket was filed; or every file the criteria
  ask to add already exists on the default branch. A merged pull request of
  the ticket's own repo is bus/station.py's verify-before-build, which sends
  the ticket to a verify run. A stale ticket goes back to the queue with
  `needs:human` and one ask naming the evidence, once per piece of evidence:
  when the operator gives it back to the factory, the same evidence does not
  hold it again. A recorded decision by a configured person beside blocked
  or awaiting-live-check, or a non-person Blocked on section beside those
  labels, keeps waiting work held before stale-evidence reads. A stale event
  wait without the label gets blocked once. This neither approves a build
  nor clears an existing hold. A Decision alone can be a decision to build.
- health: a ticket health intake filed (its health marker), or one whose Why
  names a health finding or the health endpoint, gets no lane until its body
  names the cause, on a `Cause:` line or under `## Cause`. A cause, or a
  What, that names only the measurement (the health read, what it counts,
  a threshold or floor) is refused. Naming a cause is diagnosis, the
  brain's work: the ticket goes back to the queue with `needs-brain`, and
  the factory gives it no lane until the cause is named
  (`health_hold`).
- area: in a repo that files its tickets by area of play (it defines at
  least one `area:` label), a ticket carrying no `area:` label gets no lane
  (`area_verdict`). Every per-area view misses it, and picking the area is
  the brain's intake work, so it goes back the same way a health ticket
  does. A repo with no `area:` labels is not held to the rule.
- criteria: a build ticket whose body names no acceptance criteria gets
  no lane (`criteria_verdict`): its brief could restate no criterion as
  the check that proves it, and the runner would park the brief as a work
  failure. Writing criteria is the brain's rewrite work, so the ticket
  goes back the same way a health ticket does. An answer ticket opens no
  pull request and is exempt, as at the runner.
- split: a brief whose What or acceptance criteria name more than one
  platform gets no lane (`split_verdict`): it is more than one ticket,
  and lanes spent a whole model ladder on one such brief before a person
  split it by hand. Splitting it into one slice per platform, with
  After: lines between the slices, is the brain's intake work, so the
  ticket goes back the same way a health ticket does.

A read that fails holds the ticket for the next pass: a gate that cannot
read never lets the ticket through.

At merge, bus/operator_hold.py holds a lane change on a health ticket that
edits the health read itself or a threshold (`measurement_files`).
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Iterable, List, Mapping, NamedTuple, Optional, Sequence, Tuple
from urllib.parse import quote

from . import answer, decision_claim, proof, status, tickets
from .github import GitHubError

STALE = "stale"
HEALTH = "health"
CRITERIA = "criteria"
AREA = "area"
SPLIT = "split"
AREA_PREFIX = "area:"

ASK_MARKER = "<!-- loom:stale-brief %s -->"
ASK_MARKER_RE = re.compile(r"<!-- loom:stale-brief (\S+) -->")
# An evidence key the marker carries as it is: a word and a number or hex
# stamp, which holds no repository reference. Any other key (a pull
# request key names its repo) is written as a hash of itself, because the
# public-text scrub rewrites a private repo reference inside the marker
# and the read-back then never matched (loom#2835).
PLAIN_KEY_RE = re.compile(r"[a-z]+-[0-9a-f]+")
HEALTH_NOTE = "Held for the brain: %s. %s"

# The marker bus/health.py writes into every ticket it files.
HEALTH_MARKER_RE = re.compile(r"<!-- loom:health key=[0-9a-f]{40} -->")
HEALTH_WORDS_RE = re.compile(r"\bhealth (?:finding|endpoint|read|check)s?\b", re.I)
CAUSE_LINE_RE = re.compile(r"^Cause:[ \t]*(\S.*)$", re.M | re.I)
CAUSE_MIN = 12
# A cause whose subject is the measurement, not why the affected are
# affected: it opens with the read, check or bound, says the read is wrong,
# or miscounts.
MEASURE_CAUSE_RE = re.compile(
    r"^\W*(?:the\s+)?(?:health\s+)?(?:read|check|endpoint|query|thresholds?|floors?|ceilings?"
    r"|cut-?offs?|measurement|metric|finding)s?\b"
    r"|\b(?:health (?:read|check|endpoint|query)|thresholds?|floors?|measurement)\b[^.\n]{0,30}"
    r"\b(?:is|was|are|were) (?:wrong|too \w+|incorrect|stale|off)\b"
    r"|\b(?:mis|under-?|over-?)counts?\b", re.I)
# A step that changes the measurement.
MEASURE_STEP_RE = re.compile(
    r"\b(?:change|raise|lower|adjust|loosen|relax|recount|count|exclude|include|hold)\b"
    r"[^.\n]{0,80}\b(?:thresholds?|floors?|ceilings?|cut-?offs?"
    r"|health (?:read|check|endpoint|query)|measurement|what \w+ (?:counts|measures))\b", re.I)
# A changed path that is the health read, or a changed line naming a bound.
HEALTH_PATH_RE = re.compile(r"(?:^|/)[^/]*(?:health|threshold)[^/]*$", re.I)
BOUND_LINE_RE = re.compile(r"threshold|floor|ceiling|cut-?off", re.I)

# The operator says the work is done or no longer wanted.
DONE_RE = re.compile(
    r"\balready (?:done|shipped|merged|fixed|in place|exists?|works)\b"
    r"|\b(?:is|was|has been|been) (?:done|shipped|fixed|superseded|obsolete)\b"
    r"|\bsuperseded\b|\bduplicate of\b|\bno longer (?:needed|applies|relevant|wanted)\b"
    r"|\bnot needed any ?more\b|\b(?:done|shipped|fixed) (?:in|by)\b", re.I)
NEGATED_RE = re.compile(
    r"\b(?:not|never|isn't|wasn't|hasn't|is not|was not|has not)\s+(?:\w+\s+){0,2}"
    r"(?:done|shipped|fixed|superseded|obsolete|in place)\b", re.I)
# A criterion asking for a file to be added.
CREATE_RE = re.compile(
    r"\b(?:adds?|added|creates?|created|new|introduces?|introduced|ships?)\b[^`\n]{0,60}"
    r"`([A-Za-z0-9_.][A-Za-z0-9_./-]*\.[A-Za-z0-9]+)`", re.I)
REF_RE = re.compile(r"(?<![\w.-])([A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+)#(\d+)\b")
PULL_URL_RE = re.compile(r"https://github\.com/([A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+)"
                         r"/pull/(\d+)\b")
LINKS_READ = 6
PATHS_READ = 5
SHOWN = 160


class Verdict(NamedTuple):
    """Why a ticket gets no lane: kind (STALE, HEALTH or CRITERIA), a key
    naming the evidence, and the reason in words."""
    kind: str
    key: str
    reason: str


class Unread(Exception):
    """A read the check needs failed: the ticket waits for the next pass."""


def _ts(value: Any) -> float:
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0


def _section(body: str, title: str) -> str:
    out: List[str] = []
    inside = False
    for line in (body or "").replace("\r", "").split("\n"):
        if line.startswith("## "):
            if inside:
                break
            inside = line[3:].strip().lower() == title.lower()
            continue
        if inside:
            out.append(line)
    return "\n".join(out).strip()


def _short(text: str) -> str:
    return " ".join(str(text).split())[:SHOWN]


# -- health -------------------------------------------------------------------
def is_health(body: str) -> bool:
    """A ticket health intake filed, or one whose Why names a health finding."""
    body = body or ""
    return bool(HEALTH_MARKER_RE.search(body)
                or HEALTH_WORDS_RE.search(_section(body, "Why")))


def cause(body: str) -> str:
    """The cause the ticket names (a `Cause:` line or a `## Cause` section),
    or "" when it names none."""
    found = CAUSE_LINE_RE.search(tickets.without_original(body or ""))
    text = found.group(1) if found else _section(body, "Cause")
    text = " ".join(text.split())
    return text if len(text) >= CAUSE_MIN else ""


def health_verdict(body: str) -> Optional[Verdict]:
    """Why a health ticket gets no lane, or None."""
    if not is_health(body):
        return None
    named = cause(body)
    stamp = hashlib.sha256((body or "").encode("utf-8")).hexdigest()[:12]
    if not named:
        return Verdict(HEALTH, "cause-" + stamp,
                       "it is a health ticket and its body names no cause (a `Cause:` line "
                       "or a `## Cause` section)")
    if MEASURE_CAUSE_RE.search(named):
        return Verdict(HEALTH, "measure-" + stamp,
                       "its cause names the measurement, not why the affected are affected: "
                       "%s" % _short(named))
    what = [line.lstrip("-* ").strip() for line in _section(body, "What").splitlines()
            if line.strip().startswith(("-", "*"))]
    if what and all(MEASURE_STEP_RE.search(line) for line in what):
        return Verdict(HEALTH, "measure-" + stamp,
                       "every step of its What changes the measurement, not the cause")
    return None


def health_hold(issue: Mapping[str, Any]) -> bool:
    """Whether the factory gives the queued ticket no lane: a health ticket
    whose cause is missing or names only the measurement. Reads nothing."""
    return health_verdict(str(issue.get("body") or "")) is not None


# -- criteria -------------------------------------------------------------------
def _criteria_named(body: str) -> bool:
    """Whether the body names at least one acceptance criterion, read the
    way bus/tickets.py restates them as proof checks: the two readings
    agree, so a dispatchable ticket's brief always carries the section."""
    return bool(tickets.proof_checks(body))


def criteria_verdict(body: str) -> Optional[Verdict]:
    """Why a build ticket with no acceptance criteria gets no lane, or
    None: a brief is not dispatchable until each criterion is restated as
    the check that proves it (bus/tickets.py's proof checks, loom#2162),
    and a body with no criteria can carry none. The runner already parks
    such a brief as a work failure; held here, the ticket costs no claim
    and no lane run. An answer ticket opens no pull request and needs no
    criteria, as at the runner. Reads nothing."""
    body = body or ""
    try:
        headers, _ = tickets.parse(body)
    except ValueError:
        headers = {}
    if headers.get("Deliverable"):
        return None
    if _criteria_named(body):
        return None
    stamp = hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
    return Verdict(CRITERIA, "criteria-" + stamp,
                   "its body names no acceptance criteria, so no criterion can be "
                   "restated as the check that proves it")


# -- split --------------------------------------------------------------------
# The platform families a brief can span. A brief whose What or acceptance
# criteria name two or more of them is more than one ticket: lanes spent a
# whole model ladder on one such brief before a person split it by hand in
# minutes (loom#1651). "App code" and the "Go core" count as two families,
# as the ticket's own example names them.
PLATFORM_PATTERNS = (
    ("iOS", re.compile(r"\bios\b|\biphone\b|\bipad\b", re.I)),
    ("Android", re.compile(r"\bandroid\b", re.I)),
    ("macOS", re.compile(r"\bmacos\b|\bmac os\b", re.I)),
    ("Windows", re.compile(r"\bwindows\b", re.I)),
    ("Linux", re.compile(r"\blinux\b", re.I)),
    # The UI counts only as a scope phrase (a change to it), never as a bare
    # mention: a single-platform brief whose criterion says "the UI test
    # passes" names one platform, not two.
    ("the UI", re.compile(r"\bchange (?:the )?ui\b|\bui (?:change|changes|list|screen|screens|redesign)\b"
                          r"|\bnew ui\b|\buser interface (?:change|changes|redesign)\b", re.I)),
    ("app code", re.compile(r"\bapp code\b", re.I)),
    ("the Go core", re.compile(r"\bgo (?:core|backend|server|service|credential\w*)\b"
                               r"|\bgolang\b", re.I)),
)


def platforms_named(body: str) -> List[str]:
    """The distinct platform families the brief's What and acceptance
    criteria name, in the order above. The Why is background, not scope:
    platforms named only there do not split a brief. Reads nothing."""
    text = brief_text(body or "")
    scope = "\n".join(part for part in (_section(text, "What"),
                                        _section(text, "Acceptance criteria"),
                                        _section(text, "Acceptance")) if part)
    return [name for name, pattern in PLATFORM_PATTERNS if pattern.search(scope)]


def split_verdict(body: str) -> Optional[Verdict]:
    """Why a brief naming more than one platform gets no lane, or None: it
    is refused at queue time with a split request, and the brain files one
    slice per platform with After: lines between them. Splitting is the
    brain's intake work, so the ticket goes back the same way a health
    ticket does. Reads nothing."""
    found = platforms_named(body)
    if len(found) < 2:
        return None
    stamp = hashlib.sha256((body or "").encode("utf-8")).hexdigest()[:12]
    return Verdict(SPLIT, "split-" + stamp,
                   "its What or acceptance criteria name more than one platform "
                   "(%s): split it into one slice per platform, with After: lines "
                   "between the slices, before it can be queued" % ", ".join(found))


def split_hold(issue: Mapping[str, Any]) -> bool:
    """Whether the factory gives the queued ticket no lane: a brief that
    spans more than one platform. Reads nothing."""
    return split_verdict(str(issue.get("body") or "")) is not None


# -- area ---------------------------------------------------------------------
def _area_names(labels: Iterable[Any]) -> List[str]:
    out = []
    for label in labels or ():
        name = str((label.get("name") if isinstance(label, Mapping) else label) or "")
        if name.startswith(AREA_PREFIX) and name[len(AREA_PREFIX):].strip():
            out.append(name)
    return out


def area_labels(gh: Any, repo: str) -> List[str]:
    """The repo's `area:` label names. Raises GitHubError when the read
    fails: the caller holds the ticket rather than guess the repo has none."""
    return sorted(_area_names(gh.paginate("/repos/%s/labels?per_page=100" % repo)))


def area_verdict(issue: Mapping[str, Any], areas: Sequence[str]) -> Optional[Verdict]:
    """Why a ticket with no `area:` label gets no lane, or None. `areas` is
    the repo's own `area:` labels (area_labels): a repo that defines none
    files no ticket by area, and nothing is held. Reads nothing."""
    if not areas or _area_names(issue.get("labels") or ()):
        return None
    return Verdict(AREA, "area-%s" % issue.get("number"),
                   "its repo files every ticket under an area of play and it carries "
                   "no `area:` label")


# A brief that says another issue does its work (loom#2033): "Superseded by
# #21", "superseded by issue #21", "issue #21 supersedes this work".
SUPERSEDED_RE = re.compile(
    r"(?<!not )(?<!never )\bsuperseded by (?:issue |ticket |pull request )?#(\d+)\b"
    r"|#(\d+) (?:now )?supersedes (?:this|it)\b", re.I)
# A "Blocked on" or "Blocked by" section: a heading, a bold line or a
# "Blocked on:" line, with the lines under it up to the next heading.
BLOCKED_HEAD_RE = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]*|\*\*)?Blocked (?:on|by)\b(.*)$", re.I)
NEXT_HEAD_RE = re.compile(r"^[ \t]*(?:#{1,6}[ \t]|\*\*[^*\n]+\*\*[ \t]*$)")
SAME_REPO_REF_RE = re.compile(r"(?<![\w/#.-])#(\d+)\b")
DETAILS_RE = re.compile(r"<details>.*?</details>", re.S | re.I)


def brief_text(body: Any) -> str:
    """The brief a person or the rewriter wrote: without the status section,
    the original issue an in-place rewrite kept, collapsed sections and
    quoted lines, which are the source's words, not the ticket's."""
    text = tickets.without_original(status.strip_body(body))
    text = DETAILS_RE.sub("", text)
    return "\n".join(line for line in text.split("\n") if not line.lstrip().startswith(">"))


def superseded_by(body: Any) -> Optional[int]:
    """The issue the brief says supersedes it, or None. Reads nothing."""
    found = SUPERSEDED_RE.search(brief_text(body))
    if not found:
        return None
    return int(found.group(1) or found.group(2))


def blocked_on(body: Any) -> List[int]:
    """The same-repo issues the brief's Blocked on section names, in order,
    once each. Reads nothing: whether one is still open is the caller's."""
    found: List[int] = []
    inside = False
    for line in brief_text(body).split("\n"):
        head = BLOCKED_HEAD_RE.match(line)
        if head:
            inside = True
            line = head.group(1)
        elif inside and NEXT_HEAD_RE.match(line):
            inside = False
        if inside:
            for ref in SAME_REPO_REF_RE.findall(line):
                if int(ref) not in found:
                    found.append(int(ref))
    return found


def _patch_complete(entry: Mapping[str, Any]) -> bool:
    """Whether GitHub sent the entry's whole patch: it has one, and when it
    gives the line counts, the patch holds every added and removed line
    (GitHub drops or cuts the patch of a binary or very large file)."""
    if not isinstance(entry.get("patch"), str) or not entry.get("patch"):
        return False
    lines = [line for line in str(entry["patch"]).splitlines()
             if line[:1] in "+-" and not line.startswith(("+++", "---"))]
    counted = [entry.get(k) for k in ("additions", "deletions")]
    if all(isinstance(n, int) for n in counted):
        return len(lines) >= sum(counted)
    return True


def measurement_files(entries: Sequence[Mapping[str, Any]]) -> List[str]:
    """The changed files that edit the health read itself (by the new path
    or, for a rename or delete, the old one), or a bound (a threshold,
    floor, ceiling or cutoff) in any file. An entry whose patch is missing or
    cut short cannot show it moves no bound, so it counts: the check fails
    closed."""
    found = []
    for entry in entries:
        path = str(entry.get("filename") or "")
        paths = [path, str(entry.get("previous_filename") or "")]
        patch = str(entry.get("patch") or "")
        changed = [line[1:] for line in patch.splitlines()
                   if line[:1] in "+-" and not line.startswith(("+++", "---"))]
        if (any(p and HEALTH_PATH_RE.search(p) for p in paths) or not _patch_complete(entry)
                or any(BOUND_LINE_RE.search(x) for x in changed)):
            found.append(path)
    return found


# -- stale --------------------------------------------------------------------
def _login(item: Mapping[str, Any]) -> str:
    return str((item.get("user") or {}).get("login") or "")


def marker_key(key: str) -> str:
    """The form of an evidence key the ask marker carries: the key itself
    when it is plain, else `sha-` and a short sha256 of it, which no scrub
    rewrites and names no repository."""
    if PLAIN_KEY_RE.fullmatch(key):
        return key
    return "sha-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def was_asked(key: str, done: Iterable[str]) -> bool:
    """Whether an earlier marker names the key, raw (a marker written before
    loom#2835, or on a private repo, where nothing is scrubbed) or hashed."""
    keys = set(done)
    return key in keys or marker_key(key) in keys


def asked(comments: Iterable[Mapping[str, Any]], trusted: Iterable[str]) -> set:
    """The evidence keys a trusted login already asked the operator about."""
    allowed = {t.lower() for t in trusted}
    return {key for c in comments if _login(c).lower() in allowed
            for key in ASK_MARKER_RE.findall(str(c.get("body") or ""))}


def operator_done(comments: Sequence[Mapping[str, Any]], operator: str) -> Optional[Verdict]:
    """The operator's newest comment, when it says the work is done or
    superseded."""
    if not operator:
        return None
    mine = [c for c in comments if _login(c).lower() == operator.lower()
            and (c.get("user") or {}).get("type") != "Bot"]
    if not mine:
        return None
    newest = max(mine, key=lambda c: (_ts(c.get("created_at")), int(c.get("id") or 0)))
    words = str(newest.get("body") or "")
    if not DONE_RE.search(words) or NEGATED_RE.search(words):
        return None
    return Verdict(STALE, "comment-%s" % newest.get("id"),
                   "the operator's newest comment says the work is done or superseded: "
                   "\"%s\"" % _short(words))


def source_shipped(gh: Any, repo: str, body: str) -> Optional[Verdict]:
    """The issue the ticket was rewritten from, when it is already closed as
    completed: its work shipped under another ticket or by hand."""
    source = tickets.rewritten_source(body)
    if source is None:
        return None
    issue = gh.rest("GET", "/repos/%s/issues/%d" % (repo, source)).data or {}
    if issue.get("state") == "closed" and issue.get("state_reason") == "completed":
        return Verdict(STALE, "source-%d" % source,
                       "issue #%d, which it was rewritten from, is already closed as "
                       "completed" % source)
    return None


def _names_ticket(text: str, repo: str, number: int) -> bool:
    lowered = (text or "").lower()
    return ("%s#%d" % (repo.lower(), number) in re.sub(r"\s+", "", lowered)
            or "github.com/%s/issues/%d" % (repo.lower(), number) in lowered)


def _is_revert(pr: Mapping[str, Any]) -> bool:
    from . import revert
    return revert.looks_like_revert(dict(pr))


def _merged_naming(source: Mapping[str, Any], repo: str, number: int) -> bool:
    """A cross-referencing pull request of another repo of the owner that
    merged, is no revert, and names the ticket in full in its body."""
    where = str((source.get("repository") or {}).get("full_name") or "")
    merged = (source.get("pull_request") or {}).get("merged_at")
    return (bool(merged) and bool(where) and where.lower() != repo.lower()
            and where.partition("/")[0].lower() == repo.partition("/")[0].lower()
            and not _is_revert(source)
            and _names_ticket(str(source.get("body") or ""), repo, number))


def merged_elsewhere(gh: Any, repo: str, number: int) -> Optional[Verdict]:
    """A merged pull request in another repo of the owner whose body names
    this ticket in full (its cross-reference on the ticket's timeline)."""
    for event in gh.paginate("/repos/%s/issues/%d/timeline" % (repo, number)):
        if not isinstance(event, dict) or event.get("event") != "cross-referenced":
            continue
        source = ((event.get("source") or {}).get("issue") or {})
        if not _merged_naming(source, repo, number):
            continue
        where = (source.get("repository") or {}).get("full_name")
        return Verdict(STALE, "pr-%s#%s" % (where, source.get("number")),
                       "pull request %s#%s, which names this ticket, merged at %s"
                       % (where, source.get("number"),
                          (source.get("pull_request") or {}).get("merged_at")))
    return None


def _links(texts: Iterable[str]) -> List[Tuple[str, int]]:
    found: List[Tuple[str, int]] = []
    for text in texts:
        for who, num in PULL_URL_RE.findall(text or "") + REF_RE.findall(text or ""):
            if (who, int(num)) not in found:
                found.append((who, int(num)))
    return found


def _person_texts(issue: Mapping[str, Any], comments: Sequence[Mapping[str, Any]],
                  trusted: Iterable[str]) -> List[str]:
    """The ticket's body and the comments a trusted person wrote (a
    LOOM_BRIEF_AUTHORS login that is no App: the operator). Anyone can
    comment on a public ticket, so a stranger's link never decides that its
    work shipped; Loom's own Apps write links to merges they made."""
    people = {t.lower() for t in trusted if not t.lower().endswith("[bot]")}
    return [str(issue.get("body") or "")] + [
        str(c.get("body") or "") for c in comments
        if _login(c).lower() in people and (c.get("user") or {}).get("type") != "Bot"]


def _merged_at(gh: Any, where: str, number: int) -> str:
    """When the pull request merged, "" when it did not, is a revert, or is
    no pull request."""
    try:
        pr = gh.rest("GET", "/repos/%s/pulls/%d" % (where, number)).data or {}
    except GitHubError as err:
        if err.status == 404:
            return ""  # an issue, not a pull request, or gone
        raise
    if not isinstance(pr, dict) or _is_revert(pr):
        return ""
    return str(pr.get("merged_at") or "")


def merged_linked(gh: Any, repo: str, issue: Mapping[str, Any],
                  comments: Sequence[Mapping[str, Any]], trusted: Iterable[str]) -> Optional[Verdict]:
    """A pull request in any repo of the owner, linked in the ticket or in a
    comment a person wrote (not one of Loom's own Apps), that merged after
    the ticket was filed. A link to work merged before it is context."""
    owner = repo.partition("/")[0].lower()
    filed = _ts(issue.get("created_at"))
    links = [(r, n) for r, n in _links(_person_texts(issue, comments, trusted))
             if r.partition("/")[0].lower() == owner and (r, n) != (repo, int(issue["number"]))]
    for where, num in links[:LINKS_READ]:
        merged = _merged_at(gh, where, num)
        if merged and _ts(merged) > filed:
            return Verdict(STALE, "pr-%s#%d" % (where, num),
                           "pull request %s#%d, linked on this ticket, merged at %s, after "
                           "the ticket was filed" % (where, num, merged))
    return None


def already_built(gh: Any, repo: str, body: str, branch: str) -> Optional[Verdict]:
    """Every file the criteria ask to add already exists on the default
    branch."""
    paths: List[str] = []
    for _, text in proof.criteria(body):
        for path in CREATE_RE.findall(text):
            if path not in paths:
                paths.append(path)
    if not paths:
        return None
    for path in paths[:PATHS_READ]:
        try:
            gh.rest("GET", "/repos/%s/contents/%s?ref=%s"
                    % (repo, quote(path[2:] if path.startswith("./") else path),
                       quote(branch, safe="")))
        except GitHubError as err:
            if err.status == 404:
                return None
            raise
    return Verdict(STALE, "built-" + hashlib.sha256(
        " ".join(paths).encode("utf-8")).hexdigest()[:12],
        "every file its criteria ask to add already exists on %s: %s"
        % (branch, ", ".join("`%s`" % p for p in paths[:PATHS_READ])))


def _visible_ask_body(body: str) -> str:
    """Current prose only: examples, quotations and preserved originals settle no ask."""
    body = re.sub(r"<!--.*?(?:-->|\Z)", "", brief_text(body), flags=re.S)
    lines: List[str] = []
    fence = ""
    for line in body.splitlines():
        boundary = re.match(r" {0,3}(`{3,}|~{3,})", line)
        if fence:
            if (boundary and boundary.group(1).startswith(fence)
                    and not line[boundary.end():].strip()):
                fence = ""
        elif boundary:
            fence = boundary.group(1)
        elif not line.startswith(("    ", "\t")) and not line.lstrip().startswith(">"):
            lines.append(line)
    return "\n".join(lines)


def _recorded_ask_decision(body: str, trusted: Sequence[str]) -> bool:
    """A nonempty Decision section naming a configured person, with an optional role."""
    body = re.sub(r"(^#{2,3}[ \t]*Decision[ \t]*\()[ \t]*operator[ \t]+",
                  r"\1", body, flags=re.M | re.I)
    people = decision_claim.person_logins(trusted, answer.operators(trusted=trusted))
    for heading in decision_claim.DECISION_HEADING.finditer(body):
        if not decision_claim.body_decision(heading.group(0), None, people):
            continue
        section = re.split(r"^#{1,3}[ \t]+", body[heading.end():], maxsplit=1, flags=re.M)[0]
        if section.strip():
            return True
    return False


def _ask_blocker_text(body: str) -> str:
    """All current blocker sections, using the same boundaries as blocked_on."""
    found: List[str] = []
    inside = False
    for line in body.splitlines():
        head = BLOCKED_HEAD_RE.match(line)
        if head:
            inside = True
            line = head.group(1).strip(" *:\t")
        elif inside and NEXT_HEAD_RE.match(line):
            inside = False
        if inside:
            found.append(line)
    return "\n".join(found).strip()


def _non_person_event_wait(blocked: str) -> bool:
    """Only a stated event wait, not no blocker or an issue dependency."""
    plain = blocked.strip().strip("-* \t.").lower()
    if (not re.search(r"\w", plain)
            or plain in {"n/a", "nothing", "tbd", "todo", "unknown", "to be determined"}
            or re.match(r"(?:none|no blockers|nothing blocks|not blocked)\b", plain)):
        return False
    if SAME_REPO_REF_RE.search(blocked) or REF_RE.search(blocked):
        return False
    return not re.search(r"https://github\.com/[\w.-]+/[\w.-]+/(?:issues|pull)/\d+", blocked)


def settled_brief_wait(issue: Mapping[str, Any], trusted: Sequence[str], *,
                       stale: bool = False) -> bool:
    """Recognize an explicit non-person wait without any remote evidence reads.

    Before evidence reads, both branches require a visible wait label. After
    a stale verdict, a non-person event wait may gain that label. A Decision
    alone may authorize a build and always requires the existing wait label.
    Person blockers and issue dependencies keep their existing evaluation.
    This clears no hold and authorizes no build.
    """
    body = _visible_ask_body(str(issue.get("body") or ""))
    blocked = _ask_blocker_text(body)
    if re.search(r"(?<![\w.-])@[A-Za-z0-9][\w-]*|\bneeds:[A-Za-z0-9][\w-]*", blocked):
        return False
    names = {str(n.get("name") if isinstance(n, dict) else n)
             for n in issue.get("labels") or []}
    labelled = bool(names & {"blocked", "awaiting-live-check"})
    return ((labelled or stale) and _non_person_event_wait(blocked)
            or labelled and _recorded_ask_decision(body, trusted))


def check(gh: Any, repo: str, issue: Mapping[str, Any], comments: Sequence[Mapping[str, Any]],
          trusted: Sequence[str], branch: str) -> Optional[Verdict]:
    """Why the ticket gets no lane, or None. A stale verdict the operator was
    already asked about is passed over: they gave the ticket back. Raises
    Unread when a read fails."""
    body = str(issue.get("body") or "")
    health = health_verdict(body)
    if health is not None:
        return health
    # The stale reads come before the criteria hold: a ticket whose work an
    # operator answered, a source shipped, or a twin already built surfaces
    # that verdict and its ask, and the criteria park still waits for every
    # ticket the stale reads pass over.
    criteria = criteria_verdict(body)
    split = split_verdict(body)
    done = asked(comments, trusted)
    operator = tickets.operator_login(trusted) or ""
    # An answer an operator gave on the dashboard (bus/answer.py) reads as
    # the operator's own comment.
    spoken = answer.as_operator_comments(comments, trusted, answer.operators(trusted=trusted), operator)
    reads = (lambda: operator_done(spoken, operator),
             lambda: source_shipped(gh, repo, body),
             lambda: merged_elsewhere(gh, repo, int(issue["number"])),
             lambda: merged_linked(gh, repo, issue, comments, trusted),
             lambda: already_built(gh, repo, body, branch))
    try:
        for read in reads:
            found = read()
            if found is not None and not was_asked(found.key, done):
                return found
    except (GitHubError, ValueError, KeyError, TypeError) as err:
        raise Unread(str(err)) from err
    return split if split is not None else criteria


def ask(verdict: Verdict) -> str:
    """The one ask for a stale ticket."""
    return ("Kind: decision\n%s\nThis ticket did not go to a build lane: %s. A lane that "
            "builds a stale ticket rewrites working code to match it. Close the ticket if "
            "its work is done, or rewrite it to what is still missing and remove "
            "`needs:human`; this evidence will not hold it again."
            % (ASK_MARKER % marker_key(verdict.key), verdict.reason))
