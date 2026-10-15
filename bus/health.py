"""Health findings: a project's live problems become ranked tickets.

A goal home can name an https endpoint that reports what is wrong right now
(docs/HEALTH.md is the contract). Every LOOM_HEALTH_EVERY seconds the
station reads each endpoint and, per finding:

- files one ticket on the goal home when no open ticket carries the
  finding's marker, with Priority from its impact (affected x hours);
- on the open ticket, moves the Priority line when the band changes and
  posts the new numbers at most every EVIDENCE_EVERY;
- once the finding is gone, posts one "finding absent since" comment, the
  evidence the brain verifies against. It never closes a ticket.

An endpoint that answers with an error, a redirect, too much data or a
document that breaks the contract is "not looked": nothing is filed,
updated or noted absent for it, and the reason is logged when it changes.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple
from urllib.parse import urlsplit

from . import brain, text, tickets
from .github import GitHub, GitHubError, _OPENER

EVERY = 900
PER_PASS = 3
EVIDENCE_EVERY = 6 * 3600
MAX_BYTES = 256 * 1024
TIMEOUT = 20
TITLE_MAX = 90
MARKER = "<!-- loom:health key=%s -->"
MARKER_RE = re.compile(r"<!-- loom:health key=([0-9a-f]{40}) -->")
EVIDENCE_MARKER = "<!-- loom:health-evidence -->"
ABSENT_MARKER = "<!-- loom:health-absent -->"
# Findings become public issues: refuse text that names a host or address.
_HOSTLIKE = re.compile(
    r"://|\b\d{1,3}(?:\.\d{1,3}){3}\b"
    r"|\b[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.(?:com|net|org|io|me|dev|app|lol|local|lan"
    r"|internal|arpa|home)\b", re.IGNORECASE)
_PRINTABLE = re.compile(r"[ -~]*")


def ledger_path(home: str) -> str:
    """Where the station keeps what it last read: state/station/health.json
    under LOOM_HOME. bus.brain status reads the same file."""
    return os.path.join(home, "state", "station", "health.json")


class Source(NamedTuple):
    repo: str
    goal: int
    url: str

    @property
    def key(self) -> str:
        return "%s#%d" % (self.repo, self.goal)


class Finding(NamedTuple):
    key: str
    title: str
    area: str
    affected: float
    hours: float
    impact: float
    evidence: str
    since: str

    @property
    def digest(self) -> str:
        return hashlib.sha1(self.key.encode("utf-8")).hexdigest()

    @property
    def priority(self) -> str:
        return tickets.priority_for_impact(self.impact)


class NotLooked(Exception):
    """The endpoint could not be read as a findings document."""


def parse_sources(raw: str) -> Tuple[List[Source], List[str]]:
    """LOOM_HEALTH_URLS ("owner/name#goal=https://... ...") as sources, and
    a problem line for each entry that is not one."""
    found: List[Source] = []
    problems: List[str] = []
    for entry in raw.split():
        home, sep, url = entry.partition("=")
        repo, _, goal = home.rpartition("#")
        if not (sep and tickets.REPO_RE.fullmatch(repo) and goal.isdigit()):
            problems.append("%r is not owner/name#goal=https://..." % entry.split("=")[0])
        elif urlsplit(url).scheme != "https" or not urlsplit(url).hostname:
            problems.append("%s#%s: the endpoint is not an https URL" % (repo, goal))
        else:
            found.append(Source(repo, int(goal), url))
    return found, problems


def fetch(url: str) -> Any:
    """The endpoint's JSON. NotLooked for anything but a 200 https answer
    under MAX_BYTES that parses."""
    if urlsplit(url).scheme != "https":
        raise NotLooked("the endpoint is not https")
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with _OPENER.open(req, timeout=TIMEOUT) as resp:  # noqa: S310
            raw = resp.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as err:
        raise NotLooked("the endpoint answered %d" % err.code) from None
    except (urllib.error.URLError, OSError, ValueError) as err:
        raise NotLooked("the endpoint could not be read (%s)" % type(err).__name__) from None
    if len(raw) > MAX_BYTES:
        raise NotLooked("the answer is over %d bytes" % MAX_BYTES)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise NotLooked("the answer is not JSON") from None


def _number(item: Dict[str, Any], name: str) -> float:
    value = item.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
            or value < 0:
        raise NotLooked("finding field %s is not a number of at least 0" % name)
    return float(value)


def _words(item: Dict[str, Any], name: str, limit: int, required: bool = True) -> str:
    value = item.get(name)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise NotLooked("finding field %s is not text" % name)
    if "\n" in value or len(value) > limit or not _PRINTABLE.fullmatch(value):
        raise NotLooked("finding field %s is not one line of ASCII up to %d characters"
                        % (name, limit))
    if _HOSTLIKE.search(value) or text.SECRET_RE.search(value):
        raise NotLooked("finding field %s names a host, address or secret" % name)
    try:
        text.check(value)
    except ValueError as err:
        raise NotLooked("finding field %s: %s" % (name, err)) from None
    return value.strip()


def _finding(item: Any) -> Finding:
    if not isinstance(item, dict):
        raise NotLooked("a finding is not an object")
    affected, hours = _number(item, "affected"), _number(item, "hours")
    impact = _number(item, "impact") if "impact" in item else affected * hours
    return Finding(_words(item, "key", 200), _words(item, "title", TITLE_MAX),
                   _words(item, "area", 40), affected, hours, impact,
                   _words(item, "evidence", 600), _words(item, "since", 40, required=False))


def findings(doc: Any) -> List[Finding]:
    """The document's findings, highest impact first. NotLooked for a
    document that breaks the contract anywhere: half a list would read the
    missing findings as cleared."""
    if not isinstance(doc, dict) or not isinstance(doc.get("findings"), list):
        raise NotLooked("the answer has no findings list")
    found = [_finding(item) for item in doc["findings"]]
    if len({f.digest for f in found}) != len(found):
        raise NotLooked("two findings share a key")
    return sorted(found, key=lambda f: (-f.impact, f.key))


def _count(value: float) -> str:
    return ("%d" % value) if value == int(value) else ("%.1f" % value)


def ticket_body(f: Finding) -> str:
    since = " (since %s)" % f.since if f.since else ""
    return "\n".join([
        "## Why", "",
        "The goal's health endpoint reports this problem. Area: %s. Affected now: %s. "
        "Lasting: %s hours%s. Impact (affected x hours): %s, so %s."
        % (f.area, _count(f.affected), _count(f.hours), since, _count(f.impact), f.priority),
        "", "Evidence: %s" % f.evidence, "",
        "## What", "",
        "Find why this finding is reported and fix the cause, so the problem stops "
        "for everything it affects rather than for one case.", "",
        "## Acceptance criteria", "",
        "- [ ] The pull request names the cause with the numbers that show it.",
        "- [ ] A test fails without the fix and passes with it.",
        "- [ ] Live: the finding is absent from the health endpoint for 24 hours.", "",
        "## Size", "", "M", "",
        "## Out of scope", "",
        "Other health findings: each has its own ticket.", "",
        "## Definition of done", "",
        "Merged, shipped, and the finding absent from the health endpoint for 24 hours "
        "(the ticket gets a comment when it goes absent).", "",
        MARKER % f.digest, ""])


def evidence_comment(f: Finding, checked_at: str) -> str:
    return ("%s\nStill reported%s: affected %s, lasting %s hours, impact %s (%s).\n\n"
            "Evidence: %s" % (EVIDENCE_MARKER, " at %s" % checked_at if checked_at else "",
                              _count(f.affected), _count(f.hours), _count(f.impact),
                              f.priority, f.evidence))


def absent_comment(when: str) -> str:
    return ("%s\nThe health endpoint no longer reports this finding: finding absent since "
            "%s. It verifies once it has stayed absent for 24 hours." % (ABSENT_MARKER, when))


class Intake:
    """One read of every source against the open tickets; `state` is the
    ledger the station keeps in state/health.json between passes."""

    def __init__(self, gh: GitHub, state: Dict[str, Any], now: float, stamp: str,
                 say: Callable[[str], None],
                 read: Callable[[str], Any] = fetch,
                 brief: Callable[..., str] = brain.brief) -> None:
        self.gh = gh
        self.state = state
        self.now = now
        self.stamp = stamp
        self.say = say
        self.read = read
        self.brief = brief
        self.filed = 0

    def run(self, sources: List[Source]) -> None:
        looked = self.state.setdefault("sources", {})
        keys = self.state.setdefault("keys", {})
        for stale in [k for k in list(looked) + list(keys) if k not in {s.key for s in sources}]:
            looked.pop(stale, None)
            keys.pop(stale, None)
        for source in sources:
            try:
                self._source(source, looked.setdefault(source.key, {}),
                             keys.setdefault(source.key, {}))
            except (GitHubError, OSError) as err:
                self.say("health %s failed: %s" % (source.key, err))
        self.state["last"] = self.now

    def _source(self, source: Source, seen: Dict[str, Any], ledger: Dict[str, Any]) -> None:
        try:
            doc = self.read(source.url)
            found = findings(doc)
        except NotLooked as err:
            if seen.get("error") != str(err):
                self.say("health %s not looked: %s" % (source.key, err))
            seen.update(error=str(err), looked_at=self.now)
            return
        checked = doc.get("checked_at") if isinstance(doc.get("checked_at"), str) else ""
        seen.clear()
        seen.update(looked_at=self.now, checked_at=checked, count=len(found),
                    findings=[{"title": f.title, "area": f.area, "impact": f.impact,
                               "priority": f.priority} for f in found])
        open_by_digest = self._open_tickets(source.repo)
        for f in found:
            self._finding(source, f, ledger.setdefault(f.digest, {}),
                          open_by_digest.get(f.digest), checked)
        current = {f.digest for f in found}
        for digest, issue in open_by_digest.items():
            rec = ledger.setdefault(digest, {"ticket": int(issue["number"])})
            if digest not in current and not rec.get("absent"):
                self._comment(source.repo, issue, absent_comment(checked or self.stamp))
                rec["absent"] = checked or self.stamp
        for digest in [d for d in ledger if d not in current and d not in open_by_digest]:
            del ledger[digest]

    def _open_tickets(self, repo: str) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for issue in self.gh.paginate("/repos/%s/issues?state=open&labels=loom:ticket" % repo):
            m = MARKER_RE.search(str(issue.get("body") or ""))
            if m and "pull_request" not in issue:
                out.setdefault(m.group(1), issue)
        return out

    def _finding(self, source: Source, f: Finding, rec: Dict[str, Any],
                 issue: Optional[Dict[str, Any]], checked: str) -> None:
        rec.pop("absent", None)
        if issue is None:
            self._file(source, f, rec)
            return
        rec["ticket"] = int(issue["number"])
        if rec.get("band") != f.priority:
            if rec.get("band") is not None or tickets.priority_of(issue) != f.priority:
                self._set_priority(source.repo, issue, f.priority)
            rec["band"] = f.priority
        posted = [f.affected, f.evidence]
        if rec.get("posted") != posted and self.now - float(rec.get("commented") or 0) \
                >= EVIDENCE_EVERY:
            self._comment(source.repo, issue, evidence_comment(f, checked))
            rec.update(posted=posted, commented=self.now)

    def _file(self, source: Source, f: Finding, rec: Dict[str, Any]) -> None:
        if self._declined(source.repo, rec):
            return
        if self.filed >= PER_PASS:
            return
        try:
            url = self.brief(self.gh, source.repo, source.goal, f.title, ticket_body(f),
                             difficulty="medium", priority=f.priority)
        except ValueError as err:
            if rec.get("refused") != str(err):
                self.say("health %s: could not brief %r: %s" % (source.key, f.title, err))
            rec["refused"] = str(err)
            return
        self.filed += 1
        number = url.rstrip("/").rpartition("/")[2]
        rec.clear()
        rec.update(band=f.priority, posted=[f.affected, f.evidence], commented=self.now)
        if number.isdigit():
            rec["ticket"] = int(number)
        self.say("health %s: briefed %s (%s)" % (source.key, url, f.priority))

    def _declined(self, repo: str, rec: Dict[str, Any]) -> bool:
        """True when this finding's last ticket was closed as not planned: the
        operator or the brain decided against it, so it is not filed again."""
        number = rec.get("ticket")
        if not isinstance(number, int):
            return False
        if rec.get("declined"):
            return True
        issue = self.gh.rest("GET", "/repos/%s/issues/%d" % (repo, number)).data or {}
        if issue.get("state") == "closed" and issue.get("state_reason") == "not_planned":
            rec["declined"] = True
            return True
        return False

    def _set_priority(self, repo: str, issue: Dict[str, Any], band: str) -> None:
        body = tickets.with_header(str(issue.get("body") or ""), "Priority", band)
        self.gh.rest("PATCH", "/repos/%s/issues/%d" % (repo, int(issue["number"])),
                     {"body": body})
        issue["body"] = body
        self.say("health %s#%s: priority now %s" % (repo, issue["number"], band))

    def _comment(self, repo: str, issue: Dict[str, Any], body: str) -> None:
        self.gh.rest("POST", "/repos/%s/issues/%d/comments" % (repo, int(issue["number"])),
                     {"body": body})


def status_lines(state: Optional[Dict[str, Any]], raw_urls: str) -> List[str]:
    """The "## Health findings" section for bus.brain status: each goal
    home's findings from the station's ledger, top 5 by impact. The goal
    homes come from LOOM_HEALTH_URLS, or from the ledger when this process
    was not given it."""
    sources, problems = parse_sources(raw_urls)
    looked = (state or {}).get("sources") or {}
    keys = [s.key for s in sources] or sorted(looked)
    lines = ["## Health findings"]
    lines += ["- ignored: %s" % p for p in problems]
    if not keys:
        return lines + ["- none: no health endpoints are set (LOOM_HEALTH_URLS)"]
    for key in keys:
        lines += _status_rows(key, looked.get(key) or {})
    return lines


def _status_rows(key: str, seen: Dict[str, Any]) -> List[str]:
    if not seen:
        return ["- %s: not looked yet" % key]
    if seen.get("error"):
        return ["- %s: not looked: %s" % (key, seen["error"])]
    rows = seen.get("findings") or []
    out = ["- %s: %d finding%s%s" % (key, len(rows), "" if len(rows) == 1 else "s",
                                     " (checked %s)" % seen["checked_at"]
                                     if seen.get("checked_at") else "")]
    out += ["  - %s %s (%s, impact %s)" % (r.get("priority"), r.get("title"), r.get("area"),
                                          _count(float(r.get("impact") or 0)))
            for r in rows[:5] if isinstance(r, dict)]
    return out
