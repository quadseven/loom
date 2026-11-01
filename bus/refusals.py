"""One refusal class on one ticket, round after round.

Climbing the model ladder helps a ticket a model could not do. It does not
help one that keeps meeting the same gate: loom#1785 ran seven rounds across
two pull requests, and each came back on the leak scan, the complexity rule or
a new review thread at the same large file, on free models and the strongest
alike. The fix-round comparison (bus/converge.py) reads the same finding at
the same line, so a new thread at the same file never counted.

`repeated` counts the rounds of each class since the ladder last started (a
rung 0 marker: the brain or the operator rewrote the plan). At LIMIT rounds
the factory stops climbing (bus/factory.py, _climb and _rungs_left): no more
rounds run, it stays loom:failed in the hand-off pool, and its pull request
stays open for whoever takes it.

Comments here are the expanded ones the factory reads: each fix round and each
status event is one comment.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any, Collection, Counter as CounterType, Dict, Optional, Sequence

from . import ci_facts
from .converge import THREAD_RE
from .tickets import FIX_MARKER, _login

LIMIT = 3
LEAK_SCAN = "the leak scan"
COMPLEXITY = "the complexity rule"
_REVIEW = "review findings at %s"
_LEAK_RE = re.compile(r"leak scan failed|^- Check `leak-scan\b", re.M)
_RESTART_RE = re.compile(r"<!-- loom:rung 0 -->")


def classes(body: str) -> set:
    """The refusal classes one comment records."""
    found = set()
    if _LEAK_RE.search(body):
        found.add(LEAK_SCAN)
    if ci_facts.tangled_functions(body):
        found.add(COMPLEXITY)
    if body.startswith(FIX_MARKER):
        found |= {_REVIEW % match.group(2) for match in THREAD_RE.finditer(body)}
    return found


def repeated(comments: Sequence[Dict[str, Any]], trusted: Collection[str]) -> Optional[str]:
    """The class that reached LIMIT rounds since the ladder last started, the
    one with most rounds first; None when none did. Only `trusted` logins
    (lower case) count: anyone can comment on a public repo's ticket."""
    seen: CounterType[str] = Counter()
    for comment in comments:
        if str(_login(comment.get("user"))).lower() not in trusted:
            continue
        body = str(comment.get("body") or "")
        if _RESTART_RE.search(body):
            seen.clear()
        seen.update(classes(body))
    top = seen.most_common(1)
    return top[0][0] if top and top[0][1] >= LIMIT else None
