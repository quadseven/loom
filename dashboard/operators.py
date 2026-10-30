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
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from bus import answer, brain, tickets

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


# The Operators page (loom#786): every open issue and pull request another
# operator holds, grouped by login. Built from the claim:<login> label lists
# TicketStore reads (one list per foreign claim label per repo) and the
# ticket and pull request records the refresh pass already holds.

LAND_OR_SUPERSEDE = "land or supersede"
_ADOPTED_RE = re.compile(r"^Land or supersede #([0-9]+)\b", re.I)
_REF_RE = re.compile(r"(?:\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+))?#([0-9]+)\b")
CHECK_PASSED = ("success", "skipped")
CHECK_NEUTRAL = ("neutral",)


def foreign_claim_labels(names: Iterable[str], env: Optional[Dict[str, str]] = None) -> List[str]:
    """The claim:<login> label names in a repo's label list that may mark
    another operator's work: those naming a login outside LOOM_BRIEF_AUTHORS,
    or every claim label when it is not set."""
    mine = {login.lower() for login in brain.trusted_authors(env)}
    found = []
    for name in names:
        login = tickets.claimed_by({"labels": [{"name": name}]})
        if login and login[0] not in mine:
            found.append(name)
    return found


def claim_record(repo: str, issue: Dict[str, Any]) -> Dict[str, Any]:
    """The fields the Operators page reads from an issue list item."""
    labels = [label.get("name") if isinstance(label, dict) else label for label in issue.get("labels") or []]
    return {"repo": repo, "number": issue.get("number"), "title": issue.get("title"),
            "html_url": issue.get("html_url"), "created_at": issue.get("created_at"),
            "updated_at": issue.get("updated_at"), "labels": [n for n in labels if isinstance(n, str)],
            "assignees": [_login(a) for a in issue.get("assignees") or [] if _login(a)],
            "author": _login(issue.get("user")) or None,
            "pull_request": "pull_request" in issue}


# needs:<login> (loom#810): an issue or pull request waiting on one person,
# with a comment that names the exact ask. Unlike claim:<login>, every login
# is listed, this Loom's own included: the person named is the one who has
# to act. needs:<login> never blocks a lane (bus.tickets.owned_elsewhere
# ignores it); the dashboard only shows it.
NEEDS_PREFIX = "needs:"
ASK_CHARS = 200
_MARKER_RE = re.compile(r"<!--.*?-->", re.S)
_LEAD_RE = re.compile(r"^(?:[#>]+\s*|[-*+]\s+)+")
# Claim/handoff comments (e.g. <!-- ccqw:handoff v1 -->) are not asks.
_CLAIM_COMMENT_RE = re.compile(r"<!--\s*ccqw:handoff\b", re.I)


def needs_labels(names: Iterable[str]) -> List[str]:
    """The needs:<login> label names in a repo's label list."""
    return [name for name in names if isinstance(name, str) and name.lower().startswith(NEEDS_PREFIX)
            and _login(name[len(NEEDS_PREFIX):])]


def needs_of(labels: Iterable[str]) -> List[str]:
    """The logins an item's needs:<login> labels name, once each, in order."""
    return list(dict.fromkeys(_login(name[len(NEEDS_PREFIX):]) for name in needs_labels(labels)))


def needs_record(repo: str, issue: Dict[str, Any]) -> Dict[str, Any]:
    """The fields the Operators page reads from a needs:<login> list item."""
    item = claim_record(repo, issue)
    comments = issue.get("comments")
    item["comments"] = comments if isinstance(comments, int) and comments > 0 else 0
    item["waiting_on"] = needs_of(item["labels"])
    item["body_ask"] = body_ask(issue.get("body"))
    return item


def collect_needs(found: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Each needs:<login> item once (an item with two needs labels is in two
    lists), oldest first."""
    by_key: Dict[str, Dict[str, Any]] = {}
    for item in found:
        if item.get("waiting_on"):
            by_key.setdefault(_key(item.get("repo"), item.get("number")), item)
    return sorted(by_key.values(), key=lambda item: (str(item.get("created_at") or ""), str(item["repo"]),
                                                      int(item["number"] or 0)))


def ask_line(body: Any) -> str:
    """The first line of a comment that carries words: the ask, with HTML
    comment markers and leading markdown marks dropped, at most ASK_CHARS."""
    for line in _MARKER_RE.sub("", str(body or "")).splitlines():
        line = _LEAD_RE.sub("", line.strip()).strip()
        if line:
            return line if len(line) <= ASK_CHARS else line[:ASK_CHARS - 3].rstrip() + "..."
    return ""


# The ask of an item waiting on a person: its body's "## Ask" section when it
# has one, else the newest comment that names the ask (a "## Ask" section, a
# line that starts with "Ask", or a mention of the item's needs label), else
# its latest comment. A comment read is one page of ASK_COMMENTS, the last.
ASK_COMMENTS = 100
ASK_SECTION_RE = re.compile(r"^##\s*Ask\s*\n(.*?)(?=^##\s|\Z)", re.M | re.S)
_ASK_START_RE = re.compile(r"^[*_]*(?:exact\s+)?ask\b", re.I | re.M)


def body_ask(body: Any) -> str:
    """An issue body's "## Ask" section, stripped, or ""."""
    found = ASK_SECTION_RE.search(str(body or ""))
    return found.group(1).strip() if found else ""


HUMAN_LABEL = "loom:human"
HUMAN_MARKER = brain.HUMAN_MARKER


def ask_names(labels: Iterable[str]) -> List[str]:
    """The lower-case needs labels an ask comment may mention, and
    loom:human when the item carries it."""
    labels = list(labels)
    names = [name.lower() for name in needs_labels(labels)]
    names += ["needs-operator"] if "needs-operator" in labels else []
    return names + [HUMAN_LABEL] if HUMAN_LABEL in labels else names


def _ask_lines(body: str) -> List[str]:
    return [_LEAD_RE.sub("", line.strip()).strip() for line in _MARKER_RE.sub("", body).splitlines()]


def _names_the_ask(body: str, names: Iterable[str]) -> bool:
    lower = body.lower()
    return (bool(ASK_SECTION_RE.search(body)) or any(name in lower for name in names)
            or any(_ASK_START_RE.match(line) for line in _ask_lines(body)))


def comment_ask(body: Any) -> str:
    """One short line from an ask comment: its "## Ask" section's first line,
    else its first line that starts with "Ask", else its first worded line."""
    text = str(body or "")
    section = body_ask(text)
    if section:
        return ask_line(section)
    starts = next((line for line in _ask_lines(text) if _ASK_START_RE.match(line)), "")
    return ask_line(starts or text)


def _is_claim_comment(body: str) -> bool:
    """True if the comment is only a claim/handoff with no ask content."""
    text = str(body or "")
    if not _CLAIM_COMMENT_RE.search(text):
        return False
    # Remove the claim marker and check if anything meaningful remains
    without_marker = _CLAIM_COMMENT_RE.sub("", text)
    # If it only says "Labelled needs:X" or similar, it's a pure claim comment
    without_marker = _MARKER_RE.sub("", without_marker).strip()
    # Check if it has any ask-like content (## Ask section, "Exact ask:", "Ask:" etc.)
    # If it does, it's NOT a pure claim comment - it's a handoff WITH an ask
    if ASK_SECTION_RE.search(without_marker) or _ASK_START_RE.search(without_marker):
        return False
    return not _names_the_ask(without_marker, [])


def ask_comment(comments: Iterable[Any], names: Iterable[str], *, strict: bool = False) -> Optional[Dict[str, Any]]:
    """The comment that holds an item's ask, from its comments oldest first:
    on a loom:human item the newest loom:human ask comment; else the newest
    comment that names the ask; else the latest non-claim comment (unless strict);
    None with none. An answer comment (bus/answer.py) and a pure claim/handoff
    comment are never the ask."""
    found = [comment for comment in comments if isinstance(comment, dict)
             and not str(comment.get("body") or "").lstrip().startswith(answer.ANSWER_MARKER)
             and not _is_claim_comment(str(comment.get("body") or ""))]
    names = list(names)
    if HUMAN_LABEL in names:
        human = next((comment for comment in reversed(found)
                      if str(comment.get("body") or "").startswith(HUMAN_MARKER)), None)
        if human is not None:
            return human
    named = next((comment for comment in reversed(found)
                  if _names_the_ask(str(comment.get("body") or ""), names)), None)
    if named:
        return named
    if strict:
        return None
    return found[-1] if found else None


def pick_ask(comments: Iterable[Any], names: Iterable[str], *, strict: bool = False) -> Tuple[str, str]:
    """(ask, comment url) from an item's comments, oldest first: the newest
    comment that names the ask, else the latest comment (unless strict); ("", "") with none."""
    chosen = ask_comment(comments, names, strict=strict)
    if chosen is None:
        return "", ""
    return comment_ask(chosen.get("body")), str(chosen.get("html_url") or "")


ASK_FULL_CHARS = 2000


def ask_full(body: Any) -> str:
    """An ask comment's whole text, HTML comment markers dropped, at most
    ASK_FULL_CHARS: what the answer options are read from."""
    text = _MARKER_RE.sub("", str(body or "")).strip()
    return text if len(text) <= ASK_FULL_CHARS else text[:ASK_FULL_CHARS]


def _loom_state(labels: Iterable[str]) -> Optional[str]:
    return next((name[5:] for name in labels if name.startswith("loom:") and name[5:] in
                 ("queued", "briefed", "running", "pr", "merged", "shipped", "verified", "failed")), None)


def _ref_index(prs: Iterable[Dict[str, Any]], repos: Iterable[str]) -> Dict[str, List[Dict[str, Any]]]:
    """repo#number -> the pull requests in that repo whose body names it."""
    wanted = set(repos)
    index: Dict[str, List[Dict[str, Any]]] = {}
    for pr in prs:
        repo = _repo_of(pr)
        if repo not in wanted:
            continue
        for named, number in set(_REF_RE.findall(str(pr.get("body") or ""))):
            if not named or named == repo:
                index.setdefault("%s#%s" % (repo, number), []).append(pr)
    return index


def _repo_of(pr: Dict[str, Any]) -> str:
    """A pull request record's repo: its base repo, as results.py reads it."""
    return str(((pr.get("base") or {}).get("repo") or {}).get("full_name") or pr.get("repo") or "")


def _pr_state(pr: Dict[str, Any]) -> str:
    if pr.get("merged_at"):
        return "merged"
    if pr.get("state") == "closed":
        return "closed"
    return "draft" if pr.get("draft") else "open"


def _pr_link(repo: str, pr: Dict[str, Any]) -> Dict[str, Any]:
    return {"repo": repo, "number": pr.get("number"), "html_url": pr.get("html_url"),
            "state": _pr_state(pr), "sha": (pr.get("head") or {}).get("sha")}


def _key(repo: Any, number: Any) -> str:
    return "%s#%s" % (repo, number)


def _ticket_item(row: Dict[str, Any]) -> Dict[str, Any]:
    """An open ticket record in the shape claim_record gives an issue."""
    keep = ("repo", "number", "title", "html_url", "created_at", "updated_at", "author")
    item = {key: row.get(key) for key in keep}
    item.update(labels=list(row.get("labels") or []), assignees=list(row.get("assignees") or []),
                pull_request=False)
    return item


def _candidates(claimed, ticket_rows, prs) -> Dict[str, Dict[str, Any]]:
    """Every open item that might be held, once each: the claim lists first,
    then open tickets, then open pull requests."""
    by_key: Dict[str, Dict[str, Any]] = {}
    for item in claimed:
        by_key.setdefault(_key(item.get("repo"), item.get("number")), dict(item))
    for row in ticket_rows:
        if row.get("open", True):
            by_key.setdefault(_key(row.get("repo"), row.get("number")), _ticket_item(row))
    for pr in prs:
        repo = _repo_of(pr)
        if pr.get("state") == "open" and repo:
            item = claim_record(repo, pr)
            item["pull_request"] = True
            by_key.setdefault(_key(repo, pr.get("number")), item)
    return by_key


def _held(item: Dict[str, Any], env: Optional[Dict[str, str]]) -> bool:
    """Set the item's owners, how it is held and its Loom state; whether
    another operator holds it."""
    found = owners(item, env, pull_request=bool(item.get("pull_request")))
    if not found:
        return False
    labels = {name.lower() for name in item["labels"]}
    item["owners"] = found
    item["via"] = sorted({"claim label" if "claim:" + owner["login"] in labels else "assigned"
                          for owner in found})
    item["loom_state"] = _loom_state(item["labels"])
    return True


def _linked(item, index, pr_by_key) -> List[Dict[str, Any]]:
    """The pull requests linked to an item: itself when it is one, else
    those whose body names it and the one an adoption ticket's title names,
    newest first, at most three."""
    repo = item["repo"]
    if item.get("pull_request"):
        pr = pr_by_key.get(_key(repo, item["number"]))
        return [_pr_link(repo, pr or {"number": item["number"], "html_url": item["html_url"]})]
    linked = list(index.get(_key(repo, item["number"]), []))
    adopted = _ADOPTED_RE.match(str(item.get("title") or ""))
    pr = pr_by_key.get(_key(repo, adopted.group(1))) if adopted else None
    if pr is not None and pr not in linked:
        linked.append(pr)
    linked.sort(key=lambda found: int(found.get("number") or 0), reverse=True)
    return [_pr_link(repo, found) for found in linked[:3]]


def collect(claimed: Iterable[Dict[str, Any]], ticket_rows: Iterable[Dict[str, Any]],
            prs: Iterable[Dict[str, Any]], env: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """Every open item another operator holds, once each, with its owners
    (operators.owners, the station's rule) and its linked pull requests,
    oldest first."""
    prs = list(prs)
    items = [item for item in _candidates(claimed, ticket_rows, prs).values() if _held(item, env)]
    index = _ref_index(prs, {item["repo"] for item in items if not item.get("pull_request")})
    pr_by_key = {_key(_repo_of(pr), pr.get("number")): pr for pr in prs}
    for item in items:
        item["linked"] = _linked(item, index, pr_by_key)
    items.sort(key=lambda item: (str(item.get("created_at") or ""), str(item["repo"]), int(item["number"] or 0)))
    return items


def adoption_tickets(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The loom:queued "Land or supersede" tickets another operator claimed:
    Loom adopted a pull request into them and then leaves them alone."""
    return [item for item in items if not item.get("pull_request") and item.get("loom_state") == "queued"
            and str(item.get("title") or "").strip().lower().startswith(LAND_OR_SUPERSEDE)]


def checks_summary(runs: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Counts of a head commit's check runs: passed, failed, neutral and
    running, and the names of the ones that did not pass."""
    out = {"passed": 0, "failed": 0, "neutral": 0, "running": 0, "not_passed": []}
    for run in runs:
        if not isinstance(run, dict):
            continue
        if run.get("status") != "completed":
            out["running"] += 1
            out["not_passed"].append("%s (running)" % run.get("name"))
        elif run.get("conclusion") in CHECK_PASSED:
            out["passed"] += 1
        elif run.get("conclusion") in CHECK_NEUTRAL:
            out["neutral"] += 1
            out["not_passed"].append("%s (neutral)" % run.get("name"))
        else:
            out["failed"] += 1
            out["not_passed"].append("%s (%s)" % (run.get("name"), run.get("conclusion")))
    return out


def checks_wanted(items: Iterable[Dict[str, Any]]) -> List[Tuple[str, str]]:
    """(repo, head sha) of each open linked pull request, once each."""
    found: List[Tuple[str, str]] = []
    for item in items:
        for pr in item.get("linked") or []:
            key = (pr.get("repo"), pr.get("sha"))
            if pr.get("state") in ("open", "draft") and key[1] and key not in found:
                found.append(key)
    return found


def _age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return "%dm" % max(1, seconds // 60)
    if seconds < 48 * 3600:
        return "%dh" % (seconds // 3600)
    return "%dd" % (seconds // 86400)


def _stamp(value: Any) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _checks_text(summary: Optional[Dict[str, Any]]) -> Tuple[str, str]:
    if not summary:
        return "checks not read", "the checks of this head are read for open pull requests only"
    if not any(summary.get(k) for k in ("passed", "failed", "neutral", "running")):
        return "no checks", "no check runs on this head"
    text = ", ".join("%d %s" % (summary[k], k) for k in ("failed", "neutral", "running", "passed") if summary.get(k))
    return text, "; ".join(summary.get("not_passed") or []) or "every check passed"


def _linked_html(item: Dict[str, Any], checks: Dict[str, Dict[str, Any]]) -> str:
    esc = html.escape
    if not item.get("linked"):
        return '<span class="tickets-none">no linked pull request</span>'
    out = []
    for pr in item["linked"]:
        text = "#%s %s" % (pr.get("number"), pr.get("state"))
        if pr.get("state") in ("open", "draft"):
            check, title = _checks_text(checks.get("%s@%s" % (pr.get("repo"), pr.get("sha"))))
            text += ", " + check
        else:
            title = "this pull request is %s" % pr.get("state")
        out.append('<a href="%s" title="%s">%s</a>' % (esc(str(pr.get("html_url") or ""), quote=True),
                                                       esc(title, quote=True), esc(text)))
    return "; ".join(out)


def _row(item: Dict[str, Any], checks: Dict[str, Dict[str, Any]], now: float) -> str:
    esc = html.escape
    ref = "%s#%s" % (str(item["repo"]).split("/")[-1], item["number"])
    state = " / ".join(item.get("via") or []) + (" / loom:%s" % item["loom_state"] if item.get("loom_state") else "")
    created = _stamp(item.get("created_at"))
    age = _age(now - created) if created else "-"
    updated = _stamp(item.get("updated_at"))
    age_title = "opened %s ago" % age + ("; updated %s ago" % _age(now - updated) if updated else "")
    return ('<tr><td class="tickets-ticket"><a href="%s" title="%s"><span class="machine-fact">%s</span>'
            '<span class="tickets-title">%s</span></a></td><td class="tickets-state machine-fact">%s</td>'
            '<td class="tickets-model machine-fact">%s</td><td class="tickets-age machine-fact" title="%s">%s</td>'
            '<td class="tickets-pr machine-fact">%s</td></tr>') % (
        esc(str(item.get("html_url") or ""), quote=True), esc(str(item.get("title") or ""), quote=True),
        esc(ref), esc(str(item.get("title") or "")), esc(state), _linked_html(item, checks),
        esc(age_title, quote=True), esc(age), "PR" if item.get("pull_request") else "issue")


TABLE_HEAD = ('<colgroup><col><col class="c-state"><col class="c-model"><col class="c-age"><col class="c-pr">'
              '</colgroup><thead><tr><th scope="col">Item</th><th scope="col">Held via</th>'
              '<th scope="col">Linked pull request</th><th scope="col">Age</th><th scope="col">Kind</th></tr></thead>')


def _table(rows: List[Dict[str, Any]], checks, now) -> str:
    return '<table class="tickets-table">%s<tbody>%s</tbody></table>' % (
        TABLE_HEAD, "".join(_row(item, checks, now) for item in rows))


def person_groups(items: Iterable[Dict[str, Any]],
                  needs: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    """login -> {"waiting": items labelled needs:<login>, "claimed": items
    that login holds}. An item claimed by one login and waiting on another
    is a row in each group, never merged into one."""
    groups: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    for item in needs:
        for login in item.get("waiting_on") or []:
            groups.setdefault(login, {"waiting": [], "claimed": []})["waiting"].append(item)
    for item in items:
        for owner in item.get("owners") or []:
            groups.setdefault(owner["login"], {"waiting": [], "claimed": []})["claimed"].append(item)
    return groups


def _group_size(group: Dict[str, List[Dict[str, Any]]]) -> int:
    return len(group["waiting"]) + len(group["claimed"])


def _ask_text(item: Dict[str, Any]) -> Tuple[str, str]:
    ask = item.get("ask")
    if ask is None:
        return "ask not read yet", "the latest comment is read on a later refresh"
    if not ask:
        return "no comment names the ask", "this item has no comment, or its latest comment is empty"
    return ask, "the body's ## Ask section, else the newest comment that names the ask"


def _waiting_row(item: Dict[str, Any], now: float) -> str:
    esc = html.escape
    ref = "%s#%s" % (str(item["repo"]).split("/")[-1], item["number"])
    ask, ask_title = _ask_text(item)
    created = _stamp(item.get("created_at"))
    age = _age(now - created) if created else "-"
    updated = _stamp(item.get("updated_at"))
    age_title = "opened %s ago" % age + ("; updated %s ago" % _age(now - updated) if updated else "")
    return ('<tr><td class="tickets-ticket"><a href="%s" title="%s"><span class="machine-fact">%s</span>'
            '<span class="tickets-title">%s</span></a></td><td class="operators-ask" title="%s">%s</td>'
            '<td class="tickets-age machine-fact" title="%s">%s</td><td class="tickets-pr machine-fact">%s</td>'
            '</tr>') % (
        esc(str(item.get("html_url") or ""), quote=True), esc(str(item.get("title") or ""), quote=True),
        esc(ref), esc(str(item.get("title") or "")), esc(ask_title, quote=True), esc(ask),
        esc(age_title, quote=True), esc(age), "PR" if item.get("pull_request") else "issue")


WAITING_HEAD = ('<colgroup><col><col><col class="c-age"><col class="c-pr"></colgroup><thead><tr>'
                '<th scope="col">Item</th><th scope="col">Ask</th><th scope="col">Age</th>'
                '<th scope="col">Kind</th></tr></thead>')


def _group_html(login: str, group: Dict[str, List[Dict[str, Any]]], checks, now: float) -> str:
    esc = html.escape
    body = []
    if group["waiting"]:
        body.append('<h3 class="label operators-sub">Waiting on %s</h3><table class="tickets-table">%s<tbody>%s'
                    '</tbody></table>' % (esc(login), WAITING_HEAD,
                                          "".join(_waiting_row(item, now) for item in group["waiting"])))
    if group["claimed"]:
        heading = ('<h3 class="label operators-sub">Claimed by %s</h3>' % esc(login)) if group["waiting"] else ""
        body.append(heading + _table(group["claimed"], checks, now))
    return ('<details class="tickets-group" id="person-%s" open><summary><span class="tickets-group-title">%s'
            '</span><span class="machine-fact tickets-count">%d</span></summary>%s</details>'
            % (esc(login, quote=True), esc(login), _group_size(group), "".join(body)))


def floor_html(source: Optional[Dict[str, Any]]) -> str:
    """The Floor's per-person count of open needs:<login> items, each linking
    to that person's group on the Operators page; "" when there are none."""
    counts: Dict[str, int] = {}
    for item in (source or {}).get("needs") or []:
        for login in item.get("waiting_on") or []:
            counts[login] = counts.get(login, 0) + 1
    if not counts:
        return ""
    esc = html.escape
    rows = "".join('<li><a href="/operators#person-%s">%s</a> <strong class="count">%d</strong></li>' % (
        esc(login, quote=True), esc(login), counts[login])
        for login in sorted(counts, key=lambda login: (-counts[login], login)))
    return ('<section class="card waiting-on-card"><span class="label">Waiting on a person</span>'
            '<ul class="waiting-on">%s</ul><a href="/operators">who holds what &rarr;</a></section>' % rows)


def render(source: Optional[Dict[str, Any]], now: float, env: Optional[Dict[str, str]] = None) -> str:
    """The Operators page body from TicketStore's operator_claims."""
    esc = html.escape
    source = source or {}
    items = list(source.get("items") or [])
    checks = dict(source.get("checks") or {})
    known = bool(brain.trusted_authors(env))
    parts = ['<p class="operators-note">Loom leaves these alone. An issue or pull request assigned to, '
             'or labelled <code>claim:&lt;login&gt;</code> for, a login outside this Loom&#39;s '
             '<code>LOOM_BRIEF_AUTHORS</code> belongs to that operator: the station does not claim, '
             'build, adopt or merge it.</p>']
    if not known:
        parts.append('<p class="empty-state">%s, so every <code>claim:&lt;login&gt;</code> label is '
                     'listed, this Loom&#39;s own included, and assignees are not judged.</p>'
                     % esc(NEUTRAL_TITLE))
    if source.get("error"):
        parts.append('<p class="empty-state">Some claims could not be read: %s</p>' % esc(source["error"]))
    if not source.get("read_at"):
        parts.append('<p class="empty-state">Claims have not been read yet; the dashboard reads them on '
                     'its next refresh.</p>')
        return '<section class="operators-view">%s</section>' % "".join(parts)
    adopted = adoption_tickets(items)
    if adopted:
        parts.append('<section class="operators-adopted"><h2 class="label">Land or supersede tickets another '
                     'operator claimed</h2><p class="machine-fact">loom:queued and claimed: Loom adopted '
                     'these pull requests and now waits for their operator to land or close them.</p>'
                     '%s</section>' % _table(adopted, checks, now))
    groups = person_groups(items, source.get("needs") or [])
    if not groups:
        parts.append('<div class="empty-state"><p>No open issue or pull request in LOOM_REPOS is held by '
                     'another operator or waiting on a person.</p></div>')
    for login in sorted(groups, key=lambda login: (-_group_size(groups[login]), login)):
        parts.append(_group_html(login, groups[login], checks, now))
    parts.append('<p class="machine-fact operators-read">Read %s ago, every %d minutes. An issue assigned to '
                 'another operator with no <code>claim:</code> label shows here only when it is a Loom ticket '
                 'or a pull request. An item labelled <code>needs:&lt;login&gt;</code> waits on that person, '
                 'whoever they are; the ask is the <code>## Ask</code> section of its body, else the newest '
                 'comment that names it.</p>' % (_age(now - _number(source.get("read_at"))),
                                                 int(source.get("every") or 0) // 60))
    return '<section class="operators-view">%s</section>' % "".join(parts)


def page_html(payload: Dict[str, Any], now: float) -> str:
    """The whole Operators page."""
    from .shell import page
    return page("Operators", "operators", render(payload.get("operator_claims"), now),
                status="stale" if payload.get("stale") else "live")
