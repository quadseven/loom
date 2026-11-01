"""The one place Loom closes or reopens an issue or pull request.

A close with no comment reads as an unexplained event: the reason sat in a
body edit, a status event or nowhere, and the thread showed a bare close
(loom#2444). A state change now goes through this module, which posts the
reason in the thread and then changes the state, so a thread always says why.

It refuses, with no write sent, when the reason has no visible text, and it
sends no state change when the comment could not be posted. It asks first
everything that could stop the state change (shadow mode, unticked acceptance
criteria), so a refused close leaves no comment saying it closed.

Machine data in a reason goes in an HTML comment: ticket comments are read as
email. tests/test_closing_gateway.py fails on any direct state write outside
this module.

`said=True` is for a caller that already put this reason in the thread: a note
edited in place, or one a failed close left behind. The reason is still
required, and the guard test lists every caller that passes it.
"""

from __future__ import annotations

import re
from typing import Any, Dict

from . import notify, shadow
from .github import GitHubError, ShadowHeld, refuse_incomplete_close

_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def _check_reason(reason: Any) -> None:
    if not _HTML_COMMENT.sub("", str(reason or "")).strip():
        raise ValueError("a close or reopen needs a plain-English reason for the thread")


def _check_not_held(gh: Any, path: str, change: Dict[str, Any]) -> None:
    """Raise ShadowHeld, after recording it, when shadow mode holds this change."""
    found = shadow.classify("PATCH", path, change)
    if (found is None or not shadow.active(found.repo or None)
            or shadow.evidence_close_passes(found)):
        return
    item = None
    if found.check:
        try:
            item = gh.rest("GET", path).data
        except GitHubError:
            item = None   # an unread item is held as the worst it could be
    entry = shadow.guard("PATCH", path, change, item, source=shadow.caller())
    if entry is not None:
        raise ShadowHeld("shadow mode held PATCH %s; recorded in the shadow ledger and left "
                         "as it is" % path, entry)


def _change(gh: Any, repo: str, number: int, path: str, reason: Any,
            change: Dict[str, Any], kind: str, said: bool) -> None:
    _check_reason(reason)
    _check_not_held(gh, path, change)
    refuse_incomplete_close(gh, "PATCH", path, change)
    if not said:
        notify.post(gh, repo, number, str(reason), kind)
    gh.rest("PATCH", path, change)


def close(gh: Any, repo: str, number: int, reason: Any, state_reason: str,
          said: bool = False) -> None:
    """Post `reason` on the issue, then close it as `state_reason`."""
    _change(gh, repo, number, "/repos/%s/issues/%d" % (repo, number), reason,
            {"state": "closed", "state_reason": state_reason}, notify.CLOSED, said)


def close_pull(gh: Any, repo: str, number: int, reason: Any) -> None:
    """Post `reason` on the pull request, then close it."""
    _change(gh, repo, number, "/repos/%s/pulls/%d" % (repo, number), reason,
            {"state": "closed"}, notify.CLOSED, False)


def reopen(gh: Any, repo: str, number: int, reason: Any, said: bool = False) -> None:
    """Post `reason` on the issue, then reopen it."""
    _change(gh, repo, number, "/repos/%s/issues/%d" % (repo, number), reason,
            {"state": "open"}, notify.REOPENED, said)
