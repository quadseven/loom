"""The landing lock: one green lane pull request lands at a time in a repo.

On 2026-10-27 all 29 lane pull requests in the two busiest repos were behind
main, 19 of them green, and none landed: the factory brought one up to date a
pass, its checks and review took about 25 minutes on the new head, and main
moved again in that time (loom#2066). The factory now marks the one it brings
up to date with LABEL. While a pull request in a repo carries it, and was
marked less than HOLD ago, Loom updates and lands no other pull request in
that repo: the factory's _land and `bus.brain merge` both skip them. The
lock ends when that pull request merges or closes, or HOLD after the mark,
when the label comes off. The mark time is the label's own event on GitHub,
so a restart keeps it.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional
from urllib.parse import quote

LABEL = "loom:landing"
HOLD = 40 * 60


def _ts(value: Any) -> float:
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0


def _carries(pr: Dict[str, Any]) -> bool:
    return any((x.get("name") if isinstance(x, dict) else x) == LABEL
               for x in pr.get("labels") or [])


def marked_at(gh: Any, repo: str, number: int) -> float:
    """When LABEL was last put on the pull request, from its events; 0.0
    when no event says so, which reads as a lock long expired."""
    at = 0.0
    for event in gh.paginate("/repos/%s/issues/%d/events" % (repo, number)):
        if (isinstance(event, dict) and event.get("event") == "labeled"
                and (event.get("label") or {}).get("name") == LABEL):
            at = max(at, _ts(event.get("created_at")))
    return at


def holder(gh: Any, repo: str, now: float,
           pulls: Optional[Iterable[Dict[str, Any]]] = None) -> Optional[int]:
    """The open pull request that holds the repo's landing lock, or None.
    A mark HOLD old or older is released here: the label comes off. With
    more than one fresh mark, the earliest holds."""
    if pulls is None:
        pulls = gh.paginate("/repos/%s/pulls?state=open" % repo)
    held = None
    for pr in pulls:
        if not isinstance(pr, dict) or pr.get("state", "open") != "open" or not _carries(pr):
            continue
        number = int(pr["number"])
        at = marked_at(gh, repo, number)
        if now - at < HOLD:
            if held is None or at < held[0]:
                held = (at, number)
            continue
        release(gh, repo, number)
        pr["labels"] = [x for x in pr.get("labels") or []
                        if (x.get("name") if isinstance(x, dict) else x) != LABEL]
    return held[1] if held else None


def mark(gh: Any, repo: str, number: int) -> None:
    gh.rest("POST", "/repos/%s/issues/%d/labels" % (repo, number), {"labels": [LABEL]})


def release(gh: Any, repo: str, number: int) -> None:
    gh.rest("DELETE", "/repos/%s/issues/%d/labels/%s" % (repo, number, quote(LABEL, safe="")),
            ok=(404,))
