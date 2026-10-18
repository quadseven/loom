"""Other operators' work on the dashboard (docs/LANES.md, "Two Looms on
shared repos").

An issue or pull request assigned to a login, or labelled claim:<login>,
belongs to that login. The station leaves alone anything whose owners are
all outside its own LOOM_BRIEF_AUTHORS logins, by one rule:
bus.tickets.owned_elsewhere. The dashboard asks the same function, through
bus.brain.owned_elsewhere, so it can never show a different answer. With no
LOOM_BRIEF_AUTHORS set the dashboard cannot tell this Loom's logins from
another operator's, so it shows every claim:<login> label neutrally.
"""
from __future__ import annotations

import html
import re
from typing import Any, Dict, List, Optional

from bus import brain, tickets

NEUTRAL_TITLE = ("LOOM_BRIEF_AUTHORS is not set on the dashboard, so it cannot tell this Loom's "
                 "claims from another operator's")
ELSEWHERE_TITLE = ("Another operator owns this: it is assigned to, or labelled claim:%s for, a login "
                   "outside this Loom's LOOM_BRIEF_AUTHORS. This Loom leaves it alone.")


# A GitHub login: letters, digits and single hyphens, or an App's
# <slug>[bot]. Anything else in a record is dropped, never shown as an owner.
_LOGIN_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,38})(?:\[bot\])?")


def _login(value: Any) -> str:
    """The lower-case login of a user object or login string, or "" when it
    is not shaped like a GitHub login."""
    if isinstance(value, dict):
        value = value.get("login")
    login = str(value or "").strip().lower()
    return login if _LOGIN_RE.fullmatch(login) else ""


def issue_shape(record: Dict[str, Any], pull_request: bool = False) -> Dict[str, Any]:
    """A dashboard record (a ticket or a pull request) in the shape of the
    GitHub issue that bus.tickets reads: assignee and user objects, label
    objects, and a pull_request key on a pull request."""
    shaped: Dict[str, Any] = {
        "assignees": [{"login": login} for login in map(_login, record.get("assignees") or []) if login],
        "labels": [{"name": label.get("name") if isinstance(label, dict) else label}
                   for label in record.get("labels") or []],
    }
    author = _login(record.get("author")) or _login(record.get("user"))
    if author:
        shaped["user"] = {"login": author}
    if pull_request:
        shaped["pull_request"] = {}
    return shaped


def owners(record: Dict[str, Any], env: Optional[Dict[str, str]] = None,
           pull_request: bool = False) -> List[Dict[str, Any]]:
    """The other operators that own the record, each {"login", "known"}.
    known is True when LOOM_BRIEF_AUTHORS is set and bus.brain.owned_elsewhere
    names the login; with no LOOM_BRIEF_AUTHORS set, every claim:<login>
    label is listed with known False (whose Loom it is cannot be told)."""
    shaped = issue_shape(record, pull_request)
    if brain.trusted_authors(env):
        login = brain.owned_elsewhere(shaped, env)
        return [{"login": login, "known": True}] if login else []
    return [{"login": login, "known": False} for login in dict.fromkeys(tickets.claimed_by(shaped))]


def chip_html(record: Dict[str, Any], env: Optional[Dict[str, str]] = None,
              pull_request: bool = False) -> str:
    """The owner chips for a /tickets row or a ticket header: "held by
    <login>" for another operator's, "claim:<login>" neutrally when the
    dashboard has no LOOM_BRIEF_AUTHORS; "" when nothing to show."""
    out = []
    for owner in owners(record, env, pull_request):
        login = owner["login"]
        if owner["known"]:
            out.append('<span class="owner-chip" title="%s">held by %s</span>' % (
                html.escape(ELSEWHERE_TITLE % login, quote=True), html.escape(login)))
        else:
            out.append('<span class="owner-chip neutral" title="%s">claim:%s</span>' % (
                html.escape(NEUTRAL_TITLE, quote=True), html.escape(login)))
    return "".join(out)
