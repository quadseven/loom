"""Friction intake: a slowdown the tick journals keep naming gets a ticket.

The brains write what slowed the factory in each tick journal on the goal
home ("- Friction:", bus/friction.py). A slowdown noted tick after tick sat
as journal text for days until a brain spent its report budget briefing it
(loom#792). Every EVERY seconds, when LOOM_FRICTION_INTAKE is on, the
station reads the journals the LOOM_BRIEF_AUTHORS logins posted on the
invariant goal home in the last WINDOW seconds and:

- groups their friction lines: a line joins the group whose first line
  shares at least SIMILAR of its topic words (Jaccard), else starts one;
- counts a group as recurring when RECUR_JOURNALS journals or more name it
  over at least RECUR_SPAN seconds;
- files one ticket for a recurring group that no ticket is linked to yet (a
  "Noticed in" line naming one of its journals, or its key marker), under
  the goal's intake epic, at most PER_PASS per pass;
- adds the journals a linked open ticket does not cite yet as one evidence
  comment, at most every EVIDENCE_EVERY per ticket.

A ticket closed as not planned is never filed again for its group; one
closed as done is filed again only when journals after the close still name
the slowdown often enough. The ledger (state/station/friction.json) is
written after each write succeeds. LOOM_FRICTION_INTAKE=dry logs what would
be filed or commented and writes nothing.
"""
from __future__ import annotations

import calendar
import os
import re
import time
from typing import Any, Callable, Dict, FrozenSet, List, NamedTuple, Optional, Tuple

from . import brain, naming
from .friction import fingerprint, friction_lines, jaccard, words
from .github import GitHub

EVERY = 600
WINDOW = 24 * 3600
SIMILAR = 0.6
RECUR_JOURNALS = 3
RECUR_SPAN = 2 * 3600
PER_PASS = 1
EVIDENCE_EVERY = 6 * 3600
# A line with fewer topic words than this matches too much to group on.
WORDS_MIN = 3
# A ledger entry no group has matched for this long is forgotten, unless its
# ticket was declined.
FORGET_AFTER = 14 * 24 * 3600
TEXT_MAX = 240
JOURNAL_MARKER = "<!-- loom:journal v1 -->"
MARKER = "<!-- loom:friction key=%s -->"
MARKER_RE = re.compile(r"<!-- loom:friction key=([0-9a-f]{40}) -->")
EVIDENCE_MARKER = "<!-- loom:friction-evidence -->"
MODES = ("on", "dry")
# The friction's own words land in a public ticket a lane agent reads: they
# keep only plain words and punctuation, so they cannot forge a heading, a
# link, a marker, an issue reference or a mention.
_PLAIN_OUT = re.compile(r"[^A-Za-z0-9 ,.;:'()%+?!/=_-]")
# A ticket names the journal it was noticed in (docs/BRAIN.md, Ticket
# issues): "Noticed in <journal comment link>: "<the Friction line>"".
_NOTICED = re.compile(r"^[ \t>*-]*Noticed in\b[^\n]*$", re.I | re.M)


def ledger_path(home: str) -> str:
    return os.path.join(home, "state", "station", "friction.json")


def mode(raw: str) -> str:
    """LOOM_FRICTION_INTAKE as "on", "dry" or "" (off)."""
    value = raw.strip().lower()
    if value in ("1", "on", "true", "yes"):
        return "on"
    return "dry" if value == "dry" else ""


class Journal(NamedTuple):
    id: str
    url: str
    at: float
    body: str


class Group(NamedTuple):
    key: str
    words: FrozenSet[str]
    text: str
    # (journal, its friction line), oldest journal first, one per journal.
    members: List[Tuple[Journal, str]]

    @property
    def span(self) -> float:
        return self.members[-1][0].at - self.members[0][0].at

    @property
    def ids(self) -> List[str]:
        return [j.id for j, _ in self.members]


def epoch(value: Any) -> Optional[float]:
    if not isinstance(value, str):
        return None
    try:
        return float(calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")))
    except ValueError:
        return None


def journals_from(comments: List[Any], authors: List[str], now: float) -> List[Journal]:
    """The tick journals in the comments that an author posted in the last
    WINDOW seconds."""
    trusted = {a.lower() for a in authors}
    found = []
    for c in comments:
        if not isinstance(c, dict):
            continue
        login = str((c.get("user") or {}).get("login") or "") if isinstance(c.get("user"), dict) \
            else ""
        body = str(c.get("body") or "").lstrip()
        at = epoch(c.get("created_at"))
        if login.lower() not in trusted or not body.startswith(JOURNAL_MARKER) or at is None:
            continue
        if 0 <= now - at <= WINDOW and c.get("id") is not None:
            found.append(Journal(str(c["id"]), str(c.get("html_url") or ""), at, body))
    return found


def groups(journals: List[Journal], now: float) -> List[Group]:
    """The friction lines of the journals in the window, grouped by their
    topic words. A line joins the group whose first line it is most like at
    SIMILAR or more; a journal counts once per group."""
    found: List[Group] = []
    window = sorted((j for j in journals if 0 <= now - j.at <= WINDOW),
                    key=lambda j: (j.at, j.id))
    for journal in window:
        for line in friction_lines(journal.body):
            words, key = fingerprint(line)
            if len(words) < WORDS_MIN:
                continue
            best, score = None, SIMILAR
            for group in found:
                similar = jaccard(group.words, words)
                if similar >= score:
                    best, score = group, similar
            if best is None:
                found.append(Group(key, words, line, [(journal, line)]))
            elif journal.id not in best.ids:
                best.members.append((journal, line))
    return found


def is_recurring(group: Group) -> bool:
    return len(group.members) >= RECUR_JOURNALS and group.span >= RECUR_SPAN


def recurring(journals: List[Journal], now: float) -> List[Group]:
    """The recurring groups, the most journals first, then the oldest."""
    found = [g for g in groups(journals, now) if is_recurring(g)]
    return sorted(found, key=lambda g: (-len(g.members), g.members[0][0].at))


def plain(text: str) -> str:
    cleaned = " ".join(_PLAIN_OUT.sub(" ", str(text or "")).split())
    return naming.clip(cleaned, TEXT_MAX) if len(cleaned) > TEXT_MAX else cleaned


def _quoted(journal: Journal, line: str) -> str:
    return '%s: "%s"' % (journal.url or "journal %s" % journal.id, plain(line))


def ticket_title(group: Group) -> str:
    return naming.generated("Friction", plain(group.text))


def ticket_body(group: Group) -> str:
    """The brief: loom writes every section; the journals' words appear
    only as quoted Friction lines."""
    first, line = group.members[0]
    hours = group.span / 3600
    return "\n".join([
        "## Why", "",
        "Loom's friction intake filed this ticket: %d tick journals on this goal home "
        "named the same slowdown over %.1f hours, so it recurs instead of passing."
        % (len(group.members), hours), "",
        "Noticed in " + _quoted(first, line), "",
        "The journals that name it:", "",
        *["- " + _quoted(j, text) for j, text in group.members], "",
        "## What", "",
        "Find why the factory keeps hitting this slowdown and remove its cause, so the "
        "tick journals stop naming it.", "",
        "## Acceptance criteria", "",
        "- [ ] The pull request names the cause, with the numbers from the journals or the "
        "station log that show it.",
        "- [ ] A test fails without the fix and passes with it.",
        "- [ ] Live: no tick journal names this slowdown for 24 hours after the fix ships.", "",
        "## Size", "", "M", "",
        "## Out of scope", "",
        "Other slowdowns in the same journals: each recurring one gets its own ticket. "
        "What the brains write in their journals.", "",
        "## Definition of done", "",
        "Merged, shipped, and no tick journal on this goal home names the slowdown for "
        "24 hours.", "",
        MARKER % group.key, ""])


def evidence_comment(members: List[Tuple[Journal, str]]) -> str:
    return "\n".join([EVIDENCE_MARKER,
                      "Still named in %d more tick journal%s:" % (len(members),
                                                                "" if len(members) == 1 else "s"),
                      ""] + ["- " + _quoted(j, text) for j, text in members])


def names_journal(text: str, journal: Journal) -> bool:
    """True when text links the journal: its URL, or issuecomment-ID, never
    a longer id that starts with it."""
    return bool((journal.url and re.search(re.escape(journal.url) + r"(?!\d)", text))
                or re.search(r"issuecomment-" + re.escape(journal.id) + r"(?!\d)", text))


_LINKS = re.compile(r"\[[^\]]*\]\([^)\s]*\)|<?https?://[^\s>)]+>?|\S*issuecomment-\d+")


def _noticed_in(issue: Dict[str, Any], group: Group) -> bool:
    """True when a Noticed in line of the issue names one of the group's
    journals. A line that quotes a friction must quote this one: a journal
    names several slowdowns, and a ticket for one of them is not a ticket
    for the others."""
    for line in _NOTICED.findall(str(issue.get("body") or "")):
        quote = words(_LINKS.sub(" ", line)) - {"noticed"}
        for journal, text in group.members:
            if names_journal(line, journal) and (
                    len(quote) < WORDS_MIN or jaccard(quote, words(text)) >= SIMILAR):
                return True
    return False


class Intake:
    """One read of the journals against the tickets; `state` is the ledger
    the station keeps between passes."""

    def __init__(self, gh: GitHub, state: Dict[str, Any], now: float,
                 say: Callable[[str], None], write: bool = True,
                 brief: Callable[..., str] = brain.brief_intake) -> None:
        self.gh = gh
        self.state = state
        self.now = now
        self.say = say
        self.write = write
        self.brief = brief
        self.filed = 0

    def run(self, repo: str, goal: int, journals: List[Journal],
            issues: List[Dict[str, Any]]) -> None:
        """issues: the repo's open issues, and those closed in the last
        WINDOW (a ticket closed this morning still owns this morning's
        journals)."""
        ledger = self.state.get("groups")
        if not isinstance(ledger, dict):
            ledger = self.state["groups"] = {}
        issues = [i for i in issues if isinstance(i, dict) and "pull_request" not in i]
        found = recurring(journals, self.now)
        for group in found:
            self._group(repo, goal, group, ledger, issues)
        # A refusal is logged once while its group recurs.
        errors = self.state.get("errors")
        if isinstance(errors, dict):
            self.state["errors"] = {k: v for k, v in errors.items()
                                    if k in {g.key for g in found}}
        for key in [k for k, rec in ledger.items() if not isinstance(rec, dict) or (
                not rec.get("declined") and self.now - float(rec.get("seen") or 0) > FORGET_AFTER)]:
            del ledger[key]
        self.state["last"] = self.now

    def _entries(self, group: Group, ledger: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
        """The ledger entries this group continues: its own key, then any
        whose words or cited journals it shares, the most recently seen
        first. The window slides, so a group's first line, and with it its
        key, can change from pass to pass."""
        found = []
        for key, rec in ledger.items():
            if not isinstance(rec, dict):
                continue
            if key == group.key or jaccard(rec.get("words") or [], group.words) >= SIMILAR \
                    or set(rec.get("cited") or []) & set(group.ids):
                found.append((key, rec))
        found.sort(key=lambda kr: (kr[0] != group.key, -float(kr[1].get("seen") or 0)))
        return found

    @staticmethod
    def _linked(group: Group, entries: List[Tuple[str, Dict[str, Any]]],
                issues: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """The ticket the group is linked to: a ledger entry's, one carrying
        a key marker of the group or its entries, one whose Noticed in line
        names one of its journals, or a friction ticket naming one of them
        anywhere. An open one wins, then the newest."""
        keys = {group.key} | {k for k, _ in entries}
        numbers = {rec.get("ticket") for _, rec in entries}
        found = []
        for issue in issues:
            body = str(issue.get("body") or "")
            markers = set(MARKER_RE.findall(body))
            if issue.get("number") in numbers or markers & keys or _noticed_in(issue, group) \
                    or (markers and any(names_journal(body, j) for j, _ in group.members)):
                found.append(issue)
        found.sort(key=lambda i: (i.get("state") != "open", -int(i.get("number") or 0)))
        return found[0] if found else None

    def _group(self, repo: str, goal: int, group: Group, ledger: Dict[str, Any],
               issues: List[Dict[str, Any]]) -> None:
        entries = self._entries(group, ledger)
        for _, rec in entries:
            rec["seen"] = self.now
        if any(rec.get("declined") for _, rec in entries):
            return
        issue = self._linked(group, entries, issues)
        if issue is None:
            # A ledger ticket closed before the window is read only when the
            # group would otherwise be filed.
            number = next((rec["ticket"] for _, rec in entries
                           if isinstance(rec.get("ticket"), int)), None)
            if number is not None:
                read = self.gh.rest("GET", "/repos/%s/issues/%d" % (repo, number)).data
                issue = read if isinstance(read, dict) and read.get("number") == number else None
        if issue is not None and issue.get("state") == "open":
            self._evidence(repo, group, issue, ledger)
            return
        if issue is not None:
            if issue.get("state_reason") == "not_planned":
                # The operator or the brain decided against it: never again.
                ledger.setdefault(self._key_for(int(issue["number"]), group, ledger),
                                  {"ticket": int(issue["number"]), "words": sorted(group.words),
                                   "seen": self.now})["declined"] = True
                return
            closed = epoch(issue.get("closed_at")) or self.now
            after = [m for m in group.members if m[0].at > closed]
            if not after:
                return
            group = Group(group.key, group.words, after[0][1], after)
            if not is_recurring(group):
                return
        self._file(repo, goal, group, ledger)

    @staticmethod
    def _key_for(number: int, group: Group, ledger: Dict[str, Any]) -> str:
        return next((k for k, rec in ledger.items()
                     if isinstance(rec, dict) and rec.get("ticket") == number), group.key)

    def _evidence(self, repo: str, group: Group, issue: Dict[str, Any],
                  ledger: Dict[str, Any]) -> None:
        number = int(issue["number"])
        key = self._key_for(number, group, ledger)
        rec = ledger.get(key)
        if not isinstance(rec, dict) or rec.get("ticket") != number:
            # A ticket the brain briefed: it cites the journals its body names.
            body = str(issue.get("body") or "")
            rec = {"ticket": number, "words": sorted(group.words), "seen": self.now,
                   "cited": [j.id for j, _ in group.members if names_journal(body, j)]}
            if self.write:
                ledger[key] = rec
        cited = set(rec.get("cited") or [])
        new = [(j, line) for j, line in group.members if j.id not in cited]
        if not new or self.now - float(rec.get("commented") or 0) < EVIDENCE_EVERY:
            return
        plural = "" if len(new) == 1 else "s"
        if not self.write:
            self.say("friction %s: would add %d journal%s to #%d" % (repo, len(new), plural, number))
            return
        self.gh.rest("POST", "/repos/%s/issues/%d/comments" % (repo, number),
                     {"body": evidence_comment(new)})
        rec.update(cited=sorted(cited | {j.id for j, _ in new}), commented=self.now)
        ledger[key] = rec
        self.say("friction %s: added %d journal%s to #%d" % (repo, len(new), plural, number))

    def _file(self, repo: str, goal: int, group: Group, ledger: Dict[str, Any]) -> None:
        if self.filed >= PER_PASS:
            return
        if not self.write:
            self.filed += 1
            self.say("friction %s: would file %r (%d journals over %.1f hours)" % (
                repo, ticket_title(group), len(group.members), group.span / 3600))
            return
        errors = self.state.setdefault("errors", {})
        try:
            url = self.brief(self.gh, repo, goal, ticket_title(group), ticket_body(group),
                             difficulty="medium")
        except ValueError as err:
            if errors.get(group.key) != str(err):
                self.say("friction %s: could not file %s: %s" % (repo, group.key[:12], err))
            errors[group.key] = str(err)
            return
        errors.pop(group.key, None)
        self.filed += 1
        number = url.rstrip("/").rpartition("/")[2]
        ledger[group.key] = {"words": sorted(group.words), "cited": group.ids,
                             "commented": self.now, "seen": self.now}
        if number.isdigit():
            ledger[group.key]["ticket"] = int(number)
        self.say("friction %s: filed #%s" % (repo, number))
