"""The Needs-you page (loom#810): every open item that waits on a person.

Two sources feed it. A `needs-operator` issue waits on whoever runs this
Loom (the operator). A `needs:<login>` issue or pull request waits on that
login, whoever it is; TicketStore reads those on the Operators pass
(operator_claims["needs"]), so this page costs no request of its own. Hunt's
backlog document lists needs-operator items too, keyed by the bare repo name
where the store keys by OWNER/NAME; each item shows once whichever way it
was named.

The ask on each card is the issue body's "## Ask" section when it has one,
else the comment the store picked (operators.pick_ask), linked.
"""
from __future__ import annotations

import html
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote

from . import operators, views

# The ?who= value and the who-list entry for a needs-operator item. A
# GitHub login cannot hold a colon, so it never names a person.
OPERATOR = ":operator"
OPERATOR_PARAM = "operator"
_LOGIN_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,38})")
_SHOWN = ("number", "title", "html_url", "url", "created_at", "updated_at", "waiting_since",
          "waiting_since_source", "pull_request", "comments")


def _repo_name(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("full_name") or value.get("name") or ""
    return str(value or "").strip()


def full_names(repos: Iterable[Any]) -> Dict[str, str]:
    """bare repo name (lower case) -> OWNER/NAME, for each bare name exactly
    one known repo carries; an ambiguous bare name maps to nothing."""
    seen: Dict[str, set] = {}
    for repo in repos:
        name = _repo_name(repo)
        if "/" in name:
            seen.setdefault(name.rsplit("/", 1)[-1].lower(), set()).add(name)
    return {bare: next(iter(found)) for bare, found in seen.items() if len(found) == 1}


def _full(repo: Any, names: Dict[str, str]) -> str:
    name = _repo_name(repo)
    return name if "/" in name else names.get(name.lower(), name)


def ref_key(repo: Any, number: Any, names: Dict[str, str]) -> str:
    """One key per item, however its repo was named: "loom#687" and
    "quadseven/loom#687" are the same item when only one known repo is
    called loom."""
    return "%s#%s" % (_full(repo, names).lower(), number)


def exact_ask(item: Dict[str, Any]) -> Optional[str]:
    """A needs-operator item's "## Ask" section, else a one-line ask as
    written, else None: a missing ask is shown as missing, never stood in by
    the title."""
    body = str(item.get("ask") or item.get("body") or "")
    found = operators.ASK_SECTION_RE.search(body)
    text = found.group(1).strip() if found else ("" if "\n" in body.strip() else body.strip())
    return text if text and text != str(item.get("title") or "").strip() else None


def _asks_of(item: Dict[str, Any], waiting: bool) -> Tuple[str, Optional[str], str]:
    """(body ask, comment ask, comment url) of one source item. A comment ask
    of None means its comments were not read; "" means none names an ask."""
    if waiting:
        return str(item.get("body_ask") or ""), item.get("ask"), str(item.get("ask_url") or "")
    return exact_ask(item) or "", item.get("comment_ask"), str(item.get("comment_ask_url") or "")


def _merge(entry: Dict[str, Any], item: Dict[str, Any], waiting: bool, names: Dict[str, str]) -> None:
    for field in _SHOWN:
        if entry.get(field) in (None, "", 0) and item.get(field) not in (None, ""):
            entry[field] = item[field]
    if "/" in _repo_name(item.get("repo")) or not entry.get("repo"):
        entry["repo"] = _full(item.get("repo"), names)
    entry["unblocks"] = max(entry["unblocks"], _number(item.get("unblocks")))
    body, comment, url = _asks_of(item, waiting)
    entry["body_ask"] = entry["body_ask"] or body
    if comment and not entry["comment_ask"]:
        entry["comment_ask"], entry["comment_url"] = comment, url
    elif comment == "" and entry["comment_ask"] is None:
        entry["comment_ask"] = ""


def _listable(item: Dict[str, Any], who: List[str]) -> bool:
    return item.get("number") is not None and bool(_repo_name(item.get("repo"))) and bool(who)


def _add(found: Dict[str, Dict[str, Any]], item: Dict[str, Any], is_needs: bool, who: List[str],
         names: Dict[str, str]) -> None:
    key = ref_key(item.get("repo"), item.get("number"), names)
    entry = found.setdefault(key, {"key": key, "repo": "", "who": [], "unblocks": 0, "body_ask": "",
                                   "comment_ask": None, "comment_url": ""})
    _merge(entry, item, is_needs, names)
    entry["who"].extend(login for login in who if login not in entry["who"])


def entries(operator_asks: Iterable[Any], needs: Iterable[Any], repos: Iterable[Any] = ()) -> List[Dict[str, Any]]:
    """Every item waiting on a person, once each, with who it waits on
    (logins, and OPERATOR for needs-operator), its asks and its fields."""
    asks = [item for item in operator_asks or [] if isinstance(item, dict)]
    waiting = [item for item in needs or [] if isinstance(item, dict)]
    names = full_names(list(repos or []) + [item.get("repo") for item in asks + waiting])
    found: Dict[str, Dict[str, Any]] = {}
    sources = [(item, False, [OPERATOR]) for item in asks] + [
        (item, True, list(item.get("waiting_on") or [])) for item in waiting]
    for item, is_needs, who in sources:
        if _listable(item, who):
            _add(found, item, is_needs, who, names)
    return sorted(found.values(), key=lambda entry: (
        -entry["unblocks"], str(entry.get("waiting_since") or entry.get("updated_at") or "9999")))


def who_param(value: Any) -> str:
    """The filter a ?who= value names: OPERATOR, a login, or "" for all."""
    value = str(value or "").strip().lower()
    if value == OPERATOR_PARAM:
        return OPERATOR
    return value if _LOGIN_RE.fullmatch(value) else ""


def _number(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _who_text(who: Iterable[str]) -> str:
    return "waits on " + ", ".join("the operator" if login == OPERATOR else login for login in who)


def _ask_html(entry: Dict[str, Any]) -> str:
    esc = html.escape
    if entry["body_ask"]:
        return '<blockquote>%s</blockquote>' % esc(entry["body_ask"])
    if entry["comment_ask"]:
        source = ('<a href="%s">from a comment</a>' % esc(entry["comment_url"], quote=True)
                  if entry["comment_url"] else "from a comment")
        return '<blockquote>%s</blockquote><p class="machine-fact ask-source">%s</p>' % (
            esc(entry["comment_ask"]), source)
    if entry["comment_ask"] is None and entry.get("comments"):
        return '<p class="ask-missing">The ask is in a comment not read yet; a later refresh reads it.</p>'
    return ('<p class="ask-missing">No exact ask is written on the issue: no "## Ask" section, and no '
            'comment names it.</p>')


# The kind of action an ask needs, read from its words, in the order the page
# shows them: a decision is settled by a reply, so it comes first. Patterns are
# tried access, device, decide, so an ask to buy a phone service reads as access.
KINDS = (
    ("decide", "Decisions", "a reply settles each one"),
    ("access", "Access and credentials", "a sign-in, a key, a grant or a purchase"),
    ("device", "On a device or in person", "hands on a machine, a phone or a cable"),
    ("other", "Other asks", ""),
    ("missing", "No exact ask written", "an agent should write one; nothing here says what to do"),
)
_ACCESS_RE = re.compile(r"\b(sign in|log in|oauth|api key|token|credentials?|password|secret|rotate|"
                        r"grant|access|role|mint|reconnect|buy|buying|purchase|usd)\b", re.I)
_DEVICE_RE = re.compile(r"\b(handsets?|phones?|router|monitor|power(ed)?[ -](it )?(on|off|cycle)|plug|"
                        r"cable|cold boot|on the next trip|a real drive|physical|console|keyboard|"
                        r"bring the [\w -]{1,40} up)\b", re.I)
_DECIDE_RE = re.compile(r"\b(yes or no|reply|choose|pick|decide|decision|approve|merge|hold|keep|close|"
                        r"option|recommend(ed)?)\b", re.I)
_SENTENCE_RE = re.compile(r"^(.+?[.!?])(\s|$)", re.S)
# The lead-ins asks open with ("Needs the operator (one ask).", "Exact ask
# (owner):", "Ask (needs:login):", "Waiting on the repo owner (needs:login)."),
# which say who it waits on, never what to do.
_PREAMBLE_RE = re.compile(r"^(?:(?:needs the operator \(one ask\)\.|exact ask[^:]{0,60}:|ask(?: for [^:]{1,40})?"
                          r"(?: \([^)]{1,40}\))?:|waiting on the repo owner \([^)]{1,40}\)\.)\s*)+", re.I)


def ask_text(entry: Dict[str, Any]) -> str:
    """The ask as written: the body section, else the comment that names it."""
    return str(entry.get("body_ask") or entry.get("comment_ask") or "").strip()


def action_kind(entry: Dict[str, Any]) -> str:
    """decide, access, device, other, or missing (no ask written)."""
    text = ask_text(entry)
    if not text:
        return "missing"
    for kind, pattern in (("access", _ACCESS_RE), ("device", _DEVICE_RE), ("decide", _DECIDE_RE)):
        if pattern.search(text):
            return kind
    return "other"


def first_sentence(text: str, limit: int = 180) -> str:
    """The ask's first sentence, cut at a word boundary past the limit."""
    text = _PREAMBLE_RE.sub("", " ".join(text.replace("**", "").split()))
    found = _SENTENCE_RE.match(text)
    lead = found.group(1) if found else text
    return lead if len(lead) <= limit else lead[:limit].rsplit(" ", 1)[0] + "..."


def _since(entry: Dict[str, Any]) -> str:
    since = entry.get("created_at") or entry.get("waiting_since")
    return str(since) if since and since != "9999" else ""


def _oldest_first(found: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(found, key=lambda entry: _since(entry) or "9999")


def ref(entry: Dict[str, Any]) -> str:
    return "%s#%s" % (entry.get("repo"), entry.get("number"))


def card(entry: Dict[str, Any]) -> str:
    """One item as one line - age, who it waits on, title and the ask's first
    sentence - that opens to the whole ask and its facts."""
    esc = html.escape
    url = str(entry.get("html_url") or entry.get("url") or "/tickets")
    waiting = "idle" if entry.get("waiting_since_source") == "updated" else "waiting"
    age = views.format_age(_since(entry)) if _since(entry) else None
    kind = "pull request" if entry.get("pull_request") else "issue"
    lead = first_sentence(ask_text(entry)) or "No exact ask written."
    return ('<article class="card ask-card" data-who="%s"><details class="ask-row"><summary>'
            '<span class="ask-age">%s</span><span class="ask-ref">%s - %s</span>'
            '<span class="ask-title">%s</span><span class="ask-lead">%s</span></summary>'
            '<div class="ask-body"><div class="ask-head"><span class="ask-human">needs a human</span></div>'
            '<h3><a href="%s">%s</a></h3>%s<p class="machine-fact">%s | unblocks %s | <a href="%s">open the %s</a>'
            '</p></div></details></article>') % (
        esc(" ".join(OPERATOR_PARAM if login == OPERATOR else login for login in entry["who"]), quote=True),
        esc(age or "age unknown"), esc(ref(entry)), esc(_who_text(entry["who"])),
        esc(str(entry.get("title") or ref(entry))), esc(lead), esc(url, quote=True),
        esc(str(entry.get("title") or ref(entry))), _ask_html(entry), waiting, esc(str(entry["unblocks"])),
        esc(url, quote=True), kind)


def counts(found: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    """who -> how many items wait on them (OPERATOR included)."""
    out: Dict[str, int] = {}
    for entry in found:
        for login in entry["who"]:
            out[login] = out.get(login, 0) + 1
    return out


def chips(found: List[Dict[str, Any]], who: str) -> str:
    """The filter: All, one chip per login with its count, and Operator.
    Plain links (?who=<login>), so it works without script."""
    tally = counts(found)
    logins = sorted((login for login in tally if login != OPERATOR), key=lambda login: (-tally[login], login))
    if who and who != OPERATOR and who not in logins:
        logins.append(who)
    rows = [("", "All", len(found))] + [(login, login, tally.get(login, 0)) for login in logins] + [
        (OPERATOR, "Operator", tally.get(OPERATOR, 0))]
    esc = html.escape
    return '<nav class="chips needs-filter" aria-label="Who it waits on">%s</nav>' % "".join(
        '<a class="chip" href="%s"%s>%s <b>%d</b></a>' % (
            esc("/needs-you" if not key else "/needs-you?who=%s" % quote(
                OPERATOR_PARAM if key == OPERATOR else key, safe=""), quote=True),
            ' aria-current="true"' if key == who else "", esc(label), count)
        for key, label, count in rows)


def _who_name(login: str) -> str:
    return "the operator" if login == OPERATOR else login


def summary(shown: List[Dict[str, Any]]) -> str:
    """One headline: how many items wait, on whom, how many a reply settles,
    and the oldest."""
    esc = html.escape
    tally = counts(shown)
    whom = ", ".join("%d on %s" % (count, _who_name(login)) for login, count in
                     sorted(tally.items(), key=lambda pair: (-pair[1], pair[0])))
    decide = sum(1 for entry in shown if action_kind(entry) == "decide")
    oldest = next(iter(_oldest_first(shown)), None)
    parts = ['<p class="needs-headline"><strong>%d item%s wait on people</strong>: %s.</p>' % (
        len(shown), "" if len(shown) == 1 else "s", esc(whom))]
    facts = ["%d settled by a reply" % decide]
    if oldest is not None and _since(oldest):
        facts.append("Oldest: %s, %s" % (ref(oldest), views.format_age(_since(oldest)) or "age unknown"))
    parts.append('<p class="machine-fact">%s.</p>' % esc("; ".join(facts)))
    return '<section class="needs-summary card">%s%s</section>' % ("".join(parts), _start(shown))


def _start(shown: List[Dict[str, Any]]) -> str:
    """Up to three items to do first: the most unblocking, then the longest
    waiting."""
    first = sorted(_oldest_first(shown), key=lambda entry: -entry["unblocks"])[:3]
    if not first:
        return ""
    esc = html.escape
    rows = []
    for entry in first:
        url = str(entry.get("html_url") or entry.get("url") or "/tickets")
        lead = first_sentence(ask_text(entry)) or "no exact ask written"
        rows.append('<li><a href="%s">%s</a> - %s</li>' % (esc(url, quote=True), esc(ref(entry)), esc(lead)))
    return '<h2 class="needs-start-head">Start here</h2><ol class="needs-start">%s</ol>' % "".join(rows)


def groups(shown: List[Dict[str, Any]]) -> str:
    """The items grouped by the kind of action they need, oldest first."""
    esc = html.escape
    out = []
    for kind, label, hint in KINDS:
        members = [entry for entry in _oldest_first(shown) if action_kind(entry) == kind]
        if not members:
            continue
        out.append('<section class="needs-group" data-kind="%s"><h2>%s (%d)</h2>%s%s</section>' % (
            kind, esc(label), len(members), '<p class="muted">%s</p>' % esc(hint) if hint else "",
            "".join(card(entry) for entry in members)))
    return "".join(out)


def _read_note(claims: Dict[str, Any]) -> str:
    read_at = _number(claims.get("read_at"))
    if not read_at:
        return "needs:&lt;login&gt; items are not read yet: they are read on the Operators pass."
    note = "needs:&lt;login&gt; items read %s, every %d minutes" % (
        html.escape(views.format_age(read_at) or "-"), _number(claims.get("every")) // 60)
    if claims.get("error"):
        note += "; " + html.escape(str(claims["error"]))
    return note + "."


def render(operator_asks: Iterable[Any], claims: Optional[Dict[str, Any]], who: str = "",
           repos: Iterable[Any] = ()) -> Tuple[List[Dict[str, Any]], str]:
    """(every entry, the chips and the cards for the ?who= filter)."""
    claims = claims if isinstance(claims, dict) else {}
    found = entries(operator_asks, claims.get("needs") or [], repos)
    shown = [entry for entry in found if not who or who in entry["who"]]
    out = [chips(found, who)]
    if not shown:
        if who:
            out.append('<p class="empty-state">Nothing waits on %s right now.</p>' % html.escape(
                "the operator" if who == OPERATOR else who))
        else:
            out.append('<p class="empty-state">Nothing needs you right now: no open needs-operator or '
                       'needs:&lt;login&gt; issue.</p>')
    if shown:
        out.append(summary(shown))
        out.append(groups(shown))
    out.append('<p class="machine-fact needs-read">%s</p>' % _read_note(claims))
    return found, "".join(out)
