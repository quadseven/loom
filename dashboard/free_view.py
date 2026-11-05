"""The /free page: open issues free for an operator to take (loom#685).

It shows what `python3 -m bus.free` prints, from the same function
(bus.free.free_issues, which asks bus.tickets.owned_elsewhere, the rule
triage and the station use). The open issues are read at most once an hour
(dashboard/github.py, FREE_ISSUES_SECONDS); the Loom tickets that cover them
are the ticket records every pass already holds. The page reads no comments
of its own: it settles a "Briefed as #N" or "Rewritten as
ticket #N" comment from the two weeks of comments every pass already holds,
where the command reads each candidate's comments in full.
"""
from __future__ import annotations

import html
import re
from typing import Any, Dict, List, Optional

from bus import free

# A GitHub login, as dashboard/operators.py reads one.
_LOGIN_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,38})(?:\[bot\])?")
_ISSUE_URL_RE = re.compile(r"/repos/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/(\d+)$")


def open_issue_record(issue: Dict[str, Any]) -> Dict[str, Any]:
    """The fields of an open issue the free view reads, in GitHub's shape."""
    summary = issue.get("sub_issues_summary") if isinstance(issue.get("sub_issues_summary"), dict) else {}
    return {"number": issue.get("number"), "title": issue.get("title"),
            "html_url": issue.get("html_url"), "state": issue.get("state", "open"),
            "labels": [{"name": label.get("name") if isinstance(label, dict) else label}
                       for label in issue.get("labels") or []],
            "assignees": [{"login": a.get("login")} for a in issue.get("assignees") or []
                          if isinstance(a, dict) and a.get("login")],
            "user": {"login": (issue.get("user") or {}).get("login")} if isinstance(issue.get("user"), dict) else None,
            "sub_issues_summary": {"total": summary.get("total") or 0}}


def _ticket_shape(row: Dict[str, Any]) -> Dict[str, Any]:
    """A dashboard ticket record in the shape bus.free reads a ticket in."""
    return {"number": row.get("number"), "body": row.get("body") or "",
            "state": "open" if row.get("open", True) else "closed",
            "labels": [{"name": name} for name in row.get("labels") or []]}


def operator_login(value: Optional[str]) -> str:
    """The operator query value as a login, or "" when it is not one."""
    login = str(value or "").strip().lower()
    return login if _LOGIN_RE.fullmatch(login) else ""


def _comments_by_issue(comments: List[Dict[str, Any]]) -> Dict[tuple, List[Dict[str, Any]]]:
    """{(repo, issue number): its comments} from the comment feed's records."""
    out: Dict[tuple, List[Dict[str, Any]]] = {}
    for comment in comments or []:
        m = _ISSUE_URL_RE.search(str(comment.get("issue_url") or ""))
        if m:
            out.setdefault((m.group(1), int(m.group(2))), []).append(comment)
    return out


def build(open_issues: Dict[str, Optional[List[Dict[str, Any]]]],
          ticket_rows: Dict[str, Optional[List[Dict[str, Any]]]],
          operator: str, comments: Optional[List[Dict[str, Any]]] = None) -> Dict[str, List[Dict[str, Any]]]:
    """{repo: bus.free entries} for each repo whose open issues were read,
    settled by the comment feed the dashboard already holds (two weeks of
    this Loom's comments) the way the command settles them by reading."""
    shaped = {repo: [_ticket_shape(r) for r in rows or []] for repo, rows in ticket_rows.items()}
    covers = free.coverage(shaped)
    index = {(repo, int(t["number"])): t for repo, rows in shaped.items() for t in rows
             if t.get("number") is not None}
    by_issue = _comments_by_issue(comments or [])
    out = {}
    for repo, issues in open_issues.items():
        if issues is None:
            continue
        entries = free.free_issues(repo, issues, operator, covers)
        numbered = {int(i["number"]): i for i in issues if i.get("number") is not None}
        free.resolve_comments(repo, entries, numbered, operator,
                              lambda number, repo=repo: by_issue.get((repo, number), []),
                              lambda where, number: index.get((where, number)))
        out[repo] = entries
    return out


def _entry(entry: Dict[str, Any]) -> str:
    url = str(entry.get("html_url") or "")
    ref = "#%d" % entry["number"]
    link = ('<a href="%s">%s</a>' % (html.escape(url, quote=True), ref)
            if url.startswith("https://") else ref)
    return ('<li>%s %s <span class="machine-fact">%s | loom: %s</span></li>' % (
        link, html.escape(entry["title"]), html.escape(entry["reason"]), html.escape(entry["loom"])))


def render(found: Dict[str, List[Dict[str, Any]]], operator: str,
           read_at: Optional[str] = None, error: str = "") -> str:
    who = operator or "anyone"
    out = ['<p class="page-lede">Open issues free for %s to take: no other operator assigned or '
           'claimed them, no open Loom ticket covers them, and they are not epics. Ownership is the '
           "rule triage and the station use. Assign yourself and add claim:&lt;your login&gt; "
           "before you start. The same list: <code>python3 -m bus.free OWNER/NAME --operator "
           "LOGIN</code>.</p>" % html.escape(who),
           '<form method="get" class="card"><label>Operator <input name="operator" value="%s" '
           'placeholder="your GitHub login"></label> <button type="submit">Show</button></form>'
           % html.escape(operator, quote=True)]
    if error:
        out.append('<p class="empty-state">Open issues could not be read: %s</p>' % html.escape(error))
    if not found:
        out.append('<p class="empty-state">No open issues read yet.</p>')
    for repo, entries in found.items():
        free_rows = [e for e in entries if e["bucket"] == free.FREE]
        check = [e for e in entries if e["bucket"] == free.CHECK]
        taken = len(entries) - len(free_rows) - len(check)
        out.append('<section class="section"><div class="section-head"><span class="label">%s</span>'
                   '<span class="machine-fact">%d free, %d to check, %d taken</span></div>' % (
                       html.escape(repo), len(free_rows), len(check), taken))
        out.append('<ul class="free-issues">%s</ul>' % "".join(map(_entry, free_rows))
                   if free_rows else '<p class="empty-state">Nothing free for %s here.</p>' % html.escape(who))
        if check:
            out.append('<details><summary>To check (%d): the Loom ticket is closed or merged, or '
                       'was not found</summary><ul class="free-issues">%s</ul></details>' % (
                           len(check), "".join(map(_entry, check))))
        out.append("</section>")
    if read_at:
        out.append('<footer>Open issues read %s; read again at most once an hour.</footer>'
                   % html.escape(read_at))
    return "".join(out)

