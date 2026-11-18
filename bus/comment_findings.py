"""Blocking comments on a pull request, from trusted logins (loom#2988).

A review bot's findings live in review threads and review bodies, which
bus/merge_tier.py reads. A trusted builder can also post a blocking finding
as an ordinary pull request comment. That comment was the only place a
substantive current-head objection was written once, and a merge read green
checks and review and landed past it. This module reads those comments, so a
merge cannot pass over one silently. Trust comes from the API author login
(history.comment_login), never from a name written in the text.

The convention:

- A finding is a trusted comment in one of two forms. The marker form opens
  with FINDING_MARKER. The legacy form has a line that opens with "Blocking"
  (not "Blocking: none") and names a commit by 7 or more hex characters. A
  comment with any other loom machine marker is never a legacy finding, so a
  Luna verdict for an older head does not block a newer one.
- A finding is answered by a later trusted comment that links it as
  #issuecomment-<id> (its own URL works too) and states a resolution backed by
  evidence: a line opening with "Fixed in <commit>" whose commit is one of the
  pull request's own commits, or one opening with "Declined" whose reason has
  three or more words and cites evidence: a file line (path:line), one of the
  pull request's commits, or a link other than the finding's own. It is the
  only thing that clears a finding. A link alone answers nothing, so an
  acknowledgement, a "still broken, investigating" reply, a fix naming a
  commit the pull request does not carry, or a decline with no evidence leaves
  it open, as do a comment without the link, an older one, and one from an
  untrusted login.
- A finding stays open across pushes. No commit clears it, empty or unrelated,
  and a newer head does not supersede it. A later trusted finding that links an
  older one is a repeated objection: both stay open.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from . import history

FINDING_MARKER = "<!-- loom:finding v1 -->"
_MACHINE_MARKER = "<!-- loom:"
# A line that opens with "Blocking", after the markdown marks a comment may
# carry, unless the line says there is nothing blocking.
_BLOCKING_LINE = re.compile(
    r"^[ \t>*_#-]*blocking\b(?![ \t]*[:.-]?[ \t]*(?:none|no|nothing)\b)", re.I | re.M)
# A commit named by 7 to 40 hex characters with a letter and a digit, so a
# plain number or a date is not a commit.
_COMMIT = re.compile(r"\b(?=[0-9a-f]*[0-9])(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b", re.I)
# The two resolutions, each a line of its own after markdown marks: a fix
# that names its commit, or a decline with its reason.
_FIXED = re.compile(r"^[ \t>*_#-]*fixed[ \t]+(?:in|at|by)[ \t]+(?:commit[ \t]+)?[`]?"
                    r"((?=[0-9a-f]*[0-9])(?=[0-9a-f]*[a-f])[0-9a-f]{7,40})\b", re.I | re.M)
_DECLINED = re.compile(r"^[ \t>*_#-]*declined\b(.*)$", re.I | re.M)
_URL = re.compile(r"https?://\S+")
# A file line a decline cites as its evidence, such as bus/brain.py:120.
_FILE_LINE = re.compile(r"\b[\w./-]+\.\w+:\d+\b")
DECLINE_WORDS_MIN = 3
# Reads the pull request's commit SHAs. It is called only when an answer
# names a commit, at most once, and its error reaches the caller, which
# refuses the merge: a fix it cannot check is no fix.
CommitReader = Callable[[], Sequence[str]]


def opens_finding(body: str) -> bool:
    """Whether a comment body is a blocking finding, in either form."""
    # The marker form opens the comment; a marker quoted later is only text.
    if body.lstrip().startswith(FINDING_MARKER):
        return True
    if _MACHINE_MARKER in body:
        return False
    return bool(_BLOCKING_LINE.search(body) and _COMMIT.search(body))


def _links(body: str, ident: str) -> bool:
    """Whether a body links the comment with id `ident`, and not a longer id."""
    return bool(ident) and re.search(r"#issuecomment-%s(?!\d)" % re.escape(ident),
                                     body) is not None


def _on_pull_request(sha: str, commits: Callable[[], List[str]]) -> bool:
    """Whether the commit named by `sha` (7 to 40 hex characters) is one of
    the pull request's commits."""
    sha = sha.lower()
    return any(c.startswith(sha) for c in commits())


def _evidence(reason: str, ident: str, commits: Callable[[], List[str]]) -> bool:
    """Whether a decline's reason cites evidence: a file line, a link other
    than the finding's own, or one of the pull request's commits."""
    if _FILE_LINE.search(_URL.sub(" ", reason)):
        return True
    if any(not _links(link, ident) for link in _URL.findall(reason)):
        return True
    return any(_on_pull_request(sha, commits) for sha in _COMMIT.findall(reason))


def resolves(body: str, ident: str, commits: Callable[[], List[str]]) -> bool:
    """Whether a body states a resolution of finding `ident` backed by
    evidence: a line opening with "Fixed in" (or at, by) that names one of the
    pull request's commits, or one opening with "Declined" that gives a reason
    of DECLINE_WORDS_MIN or more words besides links and cites evidence
    (_evidence). A finding never resolves another, so a repeated objection
    stays open."""
    if opens_finding(body):
        return False
    if any(_on_pull_request(sha, commits) for sha in _FIXED.findall(body)):
        return True
    return any(len(re.findall(r"[A-Za-z0-9]+", _URL.sub(" ", reason))) >= DECLINE_WORDS_MIN
               and _evidence(reason, ident, commits)
               for reason in _DECLINED.findall(body))


def _answered(finding: Mapping[str, Any], trusted: Sequence[Mapping[str, Any]],
              commits: Callable[[], List[str]]) -> bool:
    """A trusted comment made after the finding that links it and resolves
    it. A link alone, an acknowledgement or a repeated objection is no
    answer. A finding with no id or no time cannot be answered, so it stays
    open."""
    ident = str(finding.get("id") or "")
    created = str(finding.get("created_at") or "")
    if not ident or not created:
        return False
    for c in trusted:
        body = str(c.get("body") or "")
        if (str(c.get("created_at") or "") > created and _links(body, ident)
                and resolves(body, ident, commits)):
            return True
    return False


def _once(read: Optional[CommitReader]) -> Callable[[], List[str]]:
    """`read`, called at most once and lowercased. With no reader, no commit
    is the pull request's, so no answer that names one resolves anything."""
    cache: List[List[str]] = []

    def commits() -> List[str]:
        if not cache:
            cache.append([str(s).lower() for s in (read() if read else ()) if s])
        return cache[0]
    return commits


def open_findings(comments: Sequence[Any], authors: Sequence[str],
                  commits: Optional[CommitReader] = None) -> List[Dict[str, Any]]:
    """The trusted blocking comments on a pull request that no trusted
    comment answers, oldest first. `authors` are the trusted logins
    (LOOM_BRIEF_AUTHORS); an empty list trusts nobody. `commits` reads the
    pull request's commit SHAs (CommitReader); its error propagates."""
    trusted = history.trusted_comments(list(comments or ()), list(authors))
    found = [c for c in trusted if opens_finding(str(c.get("body") or ""))]
    read = _once(commits)
    open_now = [c for c in found if not _answered(c, trusted, read)]
    return sorted(open_now, key=lambda c: str(c.get("created_at") or ""))


def blocker(findings: Sequence[Mapping[str, Any]]) -> str:
    """The refusal naming each open finding's author and link."""
    return "; ".join(
        "unanswered blocking comment from %s: %s"
        % (history.comment_login(c), c.get("html_url") or "comment %s" % c.get("id"))
        for c in findings)
