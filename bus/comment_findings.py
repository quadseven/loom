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
  #issuecomment-<id> (its own URL works too) and states a resolution: a line
  opening with "Fixed in <commit>", or one opening with "Declined" and giving
  its reason with evidence (file:line, test name, or link). It is the only
  thing that clears a finding. A link alone answers nothing, so an
  acknowledgement or a "still broken, investigating" reply leaves it open, as
  do a comment without the link, an older one, and one from an untrusted login.
- A finding stays open across pushes. No commit clears it, empty or unrelated,
  and a newer head does not supersede it. A later trusted finding that links an
  older one is a repeated objection: both stay open.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Sequence

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
                    r"(?=[0-9a-f]*[0-9])(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b", re.I | re.M)
_DECLINED = re.compile(r"^[ \t>*_#-]*declined\b(.*)$", re.I | re.M)
_URL = re.compile(r"https?://\S+")
# Evidence patterns for Declined: file:line, test_name, or URL
_FILE_LINE = re.compile(r"\b[a-zA-Z0-9_.-]+:\d+\b")
_TEST_NAME = re.compile(r"\btest_[a-zA-Z0-9_]+\b")
DECLINE_WORDS_MIN = 3


def opens_finding(body: str) -> bool:
    """Whether a comment body is a blocking finding, in either form."""
    if FINDING_MARKER in body:
        return True
    if _MACHINE_MARKER in body:
        return False
    return bool(_BLOCKING_LINE.search(body) and _COMMIT.search(body))


def _links(body: str, ident: str) -> bool:
    """Whether a body links the comment with id `ident`, and not a longer id."""
    return bool(ident) and re.search(r"#issuecomment-%s(?!\d)" % re.escape(ident),
                                     body) is not None


def _fixed_commit(body: str) -> Optional[str]:
    """Extract the commit SHA from a 'Fixed in' line, or None."""
    m = _FIXED.search(body)
    if m:
        return m.group(0).split()[-1].strip("`")
    return None


def _has_evidence(reason: str, finding_url: Optional[str] = None) -> bool:
    """Whether a decline reason contains evidence: file:line, test_name, or external URL.
    The finding link itself is not evidence."""
    if finding_url:
        reason = reason.replace(finding_url, " ")
    if _FILE_LINE.search(reason):
        return True
    if _TEST_NAME.search(reason):
        return True
    if _URL.search(reason):
        return True
    return False


def resolves(body: str, head_sha: Optional[str] = None, finding_url: Optional[str] = None) -> bool:
    """Whether a body states a resolution: a line opening with "Fixed in" (or
    at, by) that names a commit that exists on the PR head history, or one
    opening with "Declined" that gives evidence (file:line, test name, or link).
    A finding never resolves another, so a repeated objection stays open."""
    if opens_finding(body):
        return False
    fixed_sha = _fixed_commit(body)
    if fixed_sha:
        if head_sha and fixed_sha.lower() != head_sha.lower()[:len(fixed_sha)]:
            return False
        return True
    for reason in _DECLINED.findall(body):
        if _has_evidence(reason, finding_url):
            return True
    return False


def _answered(finding: Mapping[str, Any], trusted: Sequence[Mapping[str, Any]],
              head_sha: Optional[str] = None) -> bool:
    """A trusted comment made after the finding that links it and resolves
    it. A link alone, an acknowledgement or a repeated objection is no
    answer. A finding with no id or no time cannot be answered, so it stays
    open."""
    ident = str(finding.get("id") or "")
    created = str(finding.get("created_at") or "")
    finding_url = finding.get("html_url")
    if not ident or not created:
        return False
    for c in trusted:
        body = str(c.get("body") or "")
        if (str(c.get("created_at") or "") > created and _links(body, ident)
                and resolves(body, head_sha, finding_url)):
            return True
    return False


def open_findings(comments: Sequence[Any], authors: Sequence[str],
                  head_sha: Optional[str] = None) -> List[Dict[str, Any]]:
    """The trusted blocking comments on a pull request that no trusted
    comment answers, oldest first. `authors` are the trusted logins
    (LOOM_BRIEF_AUTHORS); an empty list trusts nobody. `head_sha` is the
    pull request's head commit SHA, used to verify 'Fixed in' commits."""
    trusted = history.trusted_comments(list(comments or ()), list(authors))
    found = [c for c in trusted if opens_finding(str(c.get("body") or ""))]
    open_now = [c for c in found if not _answered(c, trusted, head_sha)]
    return sorted(open_now, key=lambda c: str(c.get("created_at") or ""))


def blocker(findings: Sequence[Mapping[str, Any]]) -> str:
    """The refusal naming each open finding's author and link."""
    return "; ".join(
        "unanswered blocking comment from %s: %s"
        % (history.comment_login(c), c.get("html_url") or "comment %s" % c.get("id"))
        for c in findings)