"""The one place Loom closes or reopens an issue or pull request.

A close with no comment reads as an unexplained event: the reason sat in a
body edit, a status event or nowhere, and the thread showed a bare close
(loom#2444). A state change now goes through this module, which posts the
reason in the thread and then changes the state, so a thread always says why.

It refuses, with no write sent, when the reason has no visible text, and it
sends no state change when the comment could not be posted. It asks first
everything that could stop the state change (shadow mode, unticked acceptance
criteria), so a refused close leaves no comment saying it closed. When GitHub
refuses the state change anyway it deletes the comment, so a retry does not say
it twice; an answer that may hide a landed change (a timeout, a 5xx) keeps it,
since a repeated reason is better than a close with none.

Machine data in a reason goes in an HTML comment: ticket comments are read as
email. tests/test_closing_gateway.py fails on any direct state write outside
this module.

`said=True` is for a caller that already put this reason in the thread: a note
edited in place, or one a failed close left behind. The reason is still
required, and the guard test lists every caller that passes it.
"""

from __future__ import annotations

from typing import Any, Dict

from . import notify, shadow, text
from .github import GitHubError, ShadowHeld, refuse_incomplete_close


def _check_reason(reason: Any) -> None:
    if not text.has_reason(reason):
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
    posted = None if said else notify.post(gh, repo, number, str(reason), kind)
    try:
        gh.rest("PATCH", path, change)
    except (GitHubError, ValueError) as err:
        if isinstance(err, ShadowHeld) or not isinstance(err, GitHubError) \
                or 400 <= err.status < 500:
            _take_back(gh, repo, posted)
        raise


def _take_back(gh: Any, repo: str, posted: Any) -> None:
    """Delete the comment of a state change GitHub refused. Best effort: the
    refusal is what the caller needs to hear, and at worst a retry repeats it."""
    ident = posted.data.get("id") if isinstance(getattr(posted, "data", None), dict) else None
    if ident is None:
        return
    try:
        gh.rest("DELETE", "/repos/%s/issues/comments/%s" % (repo, ident))
    except GitHubError as err:
        print("closing: could not take back comment %s on %s: %s" % (ident, repo, err),
              flush=True)


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
