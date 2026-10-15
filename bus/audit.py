"""Audits of lane-made proofs (docs/BRAIN.md, Proof audits).

The station marks each proof it applies from a lane's verify run with
verified_by "<lane>/<model>"; a brain proof says "brain". Each brain tick
re-reads the evidence of one lane proof from the last day, oldest first, and
records what it found as an audit comment on the ticket. A failed audit moves
the ticket back to shipped and holds that lane's verify runs for a day: the
station reads the failed audits of the last day before it hands one out.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import proof
from .github import GitHub

BRAIN = "brain"
# Lane proofs this recent are audited; a failed audit holds the lane this long.
WINDOW = 24 * 3600
HOLD = 24 * 3600
# On a ticket whose lane proof failed its audit, so the station finds the
# failed audits without reading every ticket's comments.
LABEL = "loom:audit-failed"
RESULTS = ("pass", "fail")
_MARKER = re.compile(r"^<!-- loom:audit v1 result=(pass|fail) lane=([a-z0-9-]+) -->")
_LANE = re.compile(r"[a-z0-9-]+")
Found = Tuple[Dict[str, Any], Dict[str, Any]]


def marker(result: str, lane: str) -> str:
    return "<!-- loom:audit v1 result=%s lane=%s -->" % (result, lane)


def read(body: str) -> Optional[Tuple[str, str]]:
    """(result, lane) of an audit comment, or None."""
    m = _MARKER.match(body or "")
    return (m.group(1), m.group(2)) if m else None


def lane_of(record: Dict[str, Any]) -> str:
    """The lane that made a proof, from verified_by "<lane>/<model>"; "" for
    a brain proof or one that does not say."""
    lane, sep, _ = str(record.get("verified_by") or "").partition("/")
    return lane if sep and _LANE.fullmatch(lane) else ""


def _stamp(value: Any) -> float:
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0


def _iso(at: float) -> str:
    return datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _trusted(comments: Iterable[Any], authors: Sequence[str]) -> List[Dict[str, Any]]:
    allowed = {a.lower() for a in authors}
    return [c for c in comments if isinstance(c, dict)
            and str((c.get("user") or {}).get("login") or "").lower() in allowed]


def _standing(comments: Iterable[Any], authors: Sequence[str]) -> Tuple[Optional[Found], str]:
    """(the newest trusted proof comment and its record, or None; the result
    of the newest trusted audit after it, or "")."""
    found: Optional[Found] = None
    result = ""
    for comment in _trusted(comments, authors):
        body = str(comment.get("body") or "")
        record, audited = proof.read(body), read(body)
        if record is not None:
            found, result = (comment, record), ""
        elif found is not None and audited is not None:
            result = audited[0]
    return found, result


def unaudited(comments: Iterable[Any], authors: Sequence[str]) -> Optional[Found]:
    """The ticket's newest trusted proof when a lane made it and no trusted
    audit has judged it yet."""
    found, result = _standing(comments, authors)
    return found if found is not None and not result and lane_of(found[1]) else None


def proven(comments: Iterable[Any], authors: Sequence[str]) -> bool:
    """Whether the ticket's newest trusted proof stands: one exists and no
    failed audit came after it."""
    found, result = _standing(comments, authors)
    return found is not None and result != "fail"


def _labelled(gh: GitHub, repo: str, label: str, since: float) -> List[Dict[str, Any]]:
    """The repo's tickets with the label, open or closed, updated since."""
    found = gh.paginate("/repos/%s/issues?labels=%s&state=all&since=%s" % (repo, label, _iso(since)))
    return [i for i in found if isinstance(i, dict) and "pull_request" not in i
            and "loom:ticket" in {str((x or {}).get("name")) for x in i.get("labels") or []}]


def due(gh: GitHub, repos: Sequence[str], authors: Sequence[str], now: float
        ) -> List[Tuple[float, str, Dict[str, Any], Dict[str, Any]]]:
    """(posted, repo, issue, record) for each verified ticket whose newest
    proof a lane made in the last day and no audit has judged, oldest first."""
    out = []
    for repo in repos:
        for issue in _labelled(gh, repo, "loom:verified", now - WINDOW):
            found = unaudited(gh.paginate("/repos/%s/issues/%d/comments" % (
                repo, int(issue["number"]))), authors)
            posted = _stamp(found[0].get("created_at")) if found else 0.0
            if found and now - posted <= WINDOW:
                out.append((posted, repo, issue, found[1]))
    return sorted(out, key=lambda x: (x[0], x[1], int(x[2]["number"])))


def held(gh: GitHub, repos: Sequence[str], authors: Sequence[str], now: float) -> Dict[str, float]:
    """Each lane a trusted failed audit of the last day holds from verify
    runs, with the time its hold ends."""
    out: Dict[str, float] = {}
    for repo in repos:
        for issue in _labelled(gh, repo, LABEL, now - HOLD):
            comments = gh.paginate("/repos/%s/issues/%d/comments" % (repo, int(issue["number"])))
            for comment in _trusted(comments, authors):
                found = read(str(comment.get("body") or ""))
                until = _stamp(comment.get("created_at")) + HOLD
                if found and found[0] == "fail" and until > now:
                    out[found[1]] = max(out.get(found[1], 0.0), until)
    return out
