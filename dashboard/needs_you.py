"""The Needs-you page (loom#810): every open item that waits on a person.

Two sources feed it. A `needs-operator` issue waits on whoever runs this
Loom (the operator). A `needs:<login>` issue or pull request waits on that
login, whoever it is; TicketStore reads those on the Operators pass
(operator_claims["needs"]), so this page costs no request of its own. Hunt's
backlog document lists needs-operator items too, keyed by the bare repo name
where the store keys by OWNER/NAME; each item shows once whichever way it
was named.

The ask on each card is the issue body's "## Ask" section when it has one,
else the comment the store picked (operators.pick_ask), linked. A loom:human
issue waits on the operator as a needs-operator one does; its ask is its
loom:human comment.

Each card takes an answer in one tap (loom#1777): the buttons the ask's own
words offer (bus.answer.options) and an optional note, posted to POST
/api/needs-you/answer (dashboard/answers.py). An item answered there and not
yet read by the station (loom:answer-pending) leaves the lists and waits under
"Answered, not yet applied".
"""
from __future__ import annotations

import html
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote

from bus import answer as answers_lib

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


def _comment_field(waiting: bool) -> str:
    return "ask" if waiting else "comment_ask"


def _newer(one: Optional[Dict[str, Any]], other: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The newer of two answers (by created_at), either may be None."""
    if not one or not other:
        return one or other
    return other if str(other.get("created_at") or "") > str(one.get("created_at") or "") else one


def _merge(entry: Dict[str, Any], item: Dict[str, Any], waiting: bool, names: Dict[str, str]) -> None:
    for field in _SHOWN:
        if entry.get(field) in (None, "", 0) and item.get(field) not in (None, ""):
            entry[field] = item[field]
    if "/" in _repo_name(item.get("repo")) or not entry.get("repo"):
        entry["repo"] = _full(item.get("repo"), names)
    entry["unblocks"] = max(entry["unblocks"], _number(item.get("unblocks")))
    body, comment, url = _asks_of(item, waiting)
    entry["body_ask"] = entry["body_ask"] or body
    field = _comment_field(waiting)
    if comment and not entry["comment_ask"]:
        entry["comment_ask"], entry["comment_url"] = comment, url
        entry["comment_full"] = str(item.get(field + "_full") or "")
        entry["comment_at"] = str(item.get(field + "_at") or "")
    elif comment == "" and entry["comment_ask"] is None:
        entry["comment_ask"] = ""
    entry["labels"].extend(name for name in item.get("labels") or [] if name not in entry["labels"])
    entry["answer"] = _newer(entry.get("answer"), item.get("answer") if isinstance(item.get("answer"), dict) else None)


def _listable(item: Dict[str, Any], who: List[str]) -> bool:
    return item.get("number") is not None and bool(_repo_name(item.get("repo"))) and bool(who)


def _add(found: Dict[str, Dict[str, Any]], item: Dict[str, Any], is_needs: bool, who: List[str],
         names: Dict[str, str]) -> None:
    key = ref_key(item.get("repo"), item.get("number"), names)
    entry = found.setdefault(key, {"key": key, "repo": "", "who": [], "unblocks": 0, "body_ask": "",
                                   "comment_ask": None, "comment_url": "", "comment_full": "",
                                   "comment_at": "", "labels": [], "answer": None})
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
        # The page reads it on demand (GET /api/needs-you/ask, loom.js);
        # without script the next refresh does.
        return ('<p class="ask-missing" data-ask-read="%s">The ask is in a comment not read yet; '
                'reading it now.</p>') % esc(ask_read_url(entry), quote=True)
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


def full_ask(entry: Dict[str, Any]) -> str:
    """The whole ask the answer options are read from: the body section,
    else the whole ask comment, else its one line."""
    return str(entry.get("body_ask") or entry.get("comment_full") or entry.get("comment_ask") or "").strip()


def answer_options(entry: Dict[str, Any]) -> answers_lib.Options:
    """The answers the card offers (bus.answer.options over its ask)."""
    return answers_lib.options(full_ask(entry))


def ask_read_url(entry: Dict[str, Any]) -> str:
    return "/api/needs-you/ask?repo=%s&number=%s" % (quote(str(entry.get("repo") or ""), safe="/"),
                                                     quote(str(entry.get("number") or ""), safe=""))


def current_answer(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The entry's answer when it answers the ask shown: given after the ask
    comment (an answer older than a newer ask answered an earlier one)."""
    given = entry.get("answer")
    if not isinstance(given, dict):
        return None
    asked = str(entry.get("comment_at") or "")
    if asked and str(given.get("created_at") or "") and str(given.get("created_at")) < asked:
        return None
    return given


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


def _answer_state(entry: Dict[str, Any]) -> str:
    """The card's answered line: the answer, who gave it and when, and
    whether the station has applied it yet."""
    given = current_answer(entry)
    if given is None:
        return ""
    esc = html.escape
    age = views.format_age(str(given.get("created_at") or "")) or "just now"
    link = (' (<a href="%s">comment</a>)' % esc(str(given["url"]), quote=True)) if given.get("url") else ""
    note = (" - %s" % esc(str(given["note"]))) if given.get("note") else ""
    tail = "the station has not applied it yet." if entry.get("pending") else "the station read it."
    return ('<p class="answer-state" data-state="%s">Answered <b>%s</b>%s by %s, %s%s: %s</p>' % (
        "pending" if entry.get("pending") else "applied", esc(str(given.get("choice") or "")), note,
        esc(str(given.get("login") or "an operator")), esc(age), link, tail))


def answer_form(entry: Dict[str, Any], mode: str = "token") -> str:
    """The card's one-tap answer: a button per option the ask offers (or a
    number or text field) and an optional note. Mode "off" (no GitHub App to
    post with) shows why instead."""
    esc = html.escape
    if mode == "off":
        return ('<p class="answer-off machine-fact">Answering here needs the Loom GitHub App '
                '(LOOM_GITHUB_APP_*): answer on the issue.</p>')
    opts = answer_options(entry)
    if opts.kind == answers_lib.CHOICE:
        controls = "".join('<button type="submit" name="choice" value="%s">%s</button>' % (
            esc(choice, quote=True), esc(choice)) for choice in opts.choices)
    elif opts.kind == answers_lib.NUMBER:
        controls = ('<input name="choice" type="number" inputmode="numeric" required aria-label="Number">'
                    '<button type="submit">Answer</button>')
    else:
        controls = ('<input name="choice" type="text" required maxlength="%d" aria-label="Answer" '
                    'placeholder="Answer"><button type="submit">Answer</button>' % answers_lib.CHOICE_MAX)
    return ('<form class="answer-form" data-answer data-kind="%s" data-repo="%s" data-number="%s">'
            '<div class="answer-choices">%s</div><input class="answer-note" name="note" type="text" '
            'maxlength="%d" placeholder="Note (optional)" aria-label="Note (optional)">'
            '<p class="answer-status" role="status" aria-live="polite"></p></form>') % (
        esc(opts.kind, quote=True), esc(str(entry.get("repo") or ""), quote=True),
        esc(str(entry.get("number") or ""), quote=True), controls, answers_lib.NOTE_MAX)


def card(entry: Dict[str, Any], mode: str = "token") -> str:
    """One item as one line - age, who it waits on, title and the ask's first
    sentence - that opens to the whole ask and its facts, with its answer
    buttons below (none while an answer waits for the station)."""
    esc = html.escape
    url = str(entry.get("html_url") or entry.get("url") or "/tickets")
    waiting = "idle" if entry.get("waiting_since_source") == "updated" else "waiting"
    age = views.format_age(_since(entry)) if _since(entry) else None
    kind = "pull request" if entry.get("pull_request") else "issue"
    lead = first_sentence(ask_text(entry)) or "No exact ask written."
    form = "" if entry.get("pending") else answer_form(entry, mode)
    return ('<article class="card ask-card" data-who="%s" data-key="%s"><details class="ask-row"><summary>'
            '<span class="ask-age">%s</span><span class="ask-ref">%s - %s</span>'
            '<span class="ask-title">%s</span><span class="ask-lead">%s</span></summary>'
            '<div class="ask-body"><div class="ask-head"><span class="ask-human">needs a human</span></div>'
            '<h3><a href="%s">%s</a></h3>%s<p class="machine-fact">%s | unblocks %s | <a href="%s">open the %s</a>'
            '</p></div></details>%s%s</article>') % (
        esc(" ".join(OPERATOR_PARAM if login == OPERATOR else login for login in entry["who"]), quote=True),
        esc(str(entry.get("key") or ""), quote=True),
        esc(age or "age unknown"), esc(ref(entry)), esc(_who_text(entry["who"]) if entry["who"] else "answered"),
        esc(str(entry.get("title") or ref(entry))), esc(lead), esc(url, quote=True),
        esc(str(entry.get("title") or ref(entry))), _ask_html(entry), waiting, esc(str(entry["unblocks"])),
        esc(url, quote=True), kind, _answer_state(entry), form)


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


def groups(shown: List[Dict[str, Any]], mode: str = "token") -> str:
    """The items grouped by the kind of action they need, oldest first."""
    esc = html.escape
    out = []
    for kind, label, hint in KINDS:
        members = [entry for entry in _oldest_first(shown) if action_kind(entry) == kind]
        if not members:
            continue
        out.append('<section class="needs-group" data-kind="%s"><h2>%s (%d)</h2>%s%s</section>' % (
            kind, esc(label), len(members), '<p class="muted">%s</p>' % esc(hint) if hint else "",
            "".join(card(entry, mode) for entry in members)))
    return "".join(out)


def pending_section(pending: List[Dict[str, Any]]) -> str:
    """The items answered here that the station has not applied yet."""
    if not pending:
        return ""
    return ('<section class="needs-group needs-answered" data-kind="answered"><h2>Answered, not yet '
            'applied (%d)</h2><p class="muted">the station takes each one in on its next pass</p>%s</section>'
            % (len(pending), "".join(card(entry) for entry in _oldest_first(pending))))


def _pending_entries(answered: Iterable[Any], names: Dict[str, str]) -> Dict[str, Dict[str, Any]]:
    """key -> entry for each item labelled loom:answer-pending."""
    found: Dict[str, Dict[str, Any]] = {}
    for item in answered or []:
        if isinstance(item, dict) and item.get("number") is not None and _repo_name(item.get("repo")):
            _add(found, item, True, [], names)
    return found


def mark_answers(found: List[Dict[str, Any]], claims: Dict[str, Any], posted: Optional[Dict[str, Any]],
                 repos: Iterable[Any] = ()) -> List[Dict[str, Any]]:
    """Mark each entry answered-and-pending, and return the pending entries
    (those waiting plus those only labelled loom:answer-pending). An answer
    this dashboard posted since the last Operators read counts as pending
    until that read sees the label."""
    names = full_names(list(repos or []) + [entry.get("repo") for entry in found])
    pending = _pending_entries(claims.get("answered") or [], names)
    posted = posted or {}
    read_at = _number(claims.get("read_at"))
    by_key = {entry["key"]: entry for entry in found}
    for key, given in posted.items():
        if _number(given.get("at")) < read_at and key not in pending:
            continue
        entry = by_key.get(key) or pending.get(key)
        if entry is None:
            continue
        entry["answer"] = _newer(entry.get("answer"), given)
        pending.setdefault(key, entry)
    for key, entry in list(pending.items()):
        target = by_key.get(key, entry)
        target["pending"] = True
        if target is not entry:
            target["answer"] = _newer(target.get("answer"), entry.get("answer"))
            pending[key] = target
    return list(pending.values())


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
           repos: Iterable[Any] = (), posted: Optional[Dict[str, Any]] = None,
           mode: str = "token") -> Tuple[List[Dict[str, Any]], str]:
    """(every entry, the chips and the cards for the ?who= filter). An
    answered item waiting for the station is listed apart, below."""
    claims = claims if isinstance(claims, dict) else {}
    every = entries(operator_asks, claims.get("needs") or [], repos)
    pending = mark_answers(every, claims, posted, repos)
    found = [entry for entry in every if not entry.get("pending")]
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
        out.append(groups(shown, mode))
    out.append(pending_section(pending))
    out.append('<p class="machine-fact needs-read">%s</p>' % _read_note(claims))
    return found, "".join(out)


def find(operator_asks: Iterable[Any], claims: Optional[Dict[str, Any]], repo: str, number: Any,
         repos: Iterable[Any] = (), posted: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """The entry for repo#number among the waiting and the answered items,
    marked as render marks it; None when it is not listed."""
    claims = claims if isinstance(claims, dict) else {}
    every = entries(operator_asks, claims.get("needs") or [], repos)
    pending = mark_answers(every, claims, posted, repos)
    names = full_names(list(repos or []) + [entry.get("repo") for entry in every + pending])
    key = ref_key(repo, number, names)
    return next((entry for entry in every + pending if entry["key"] == key), None)


# The station's goals stuck at their in-flight cap (bus/station.py
# post_stuck_goals, loom#1831): kept while the station refreshes them, and
# dropped once a report is this old (the station sends every ten minutes).
GOAL_CAPS_MAX_AGE = 3600
GOAL_CAPS_MAX = 200
_GOAL_RE = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+#[0-9]+")


def goal_caps(body: Any, now: float) -> Dict[str, Any]:
    """The stored form of a POST /api/goal-caps body: each goal as
    OWNER/NAME#N with its whole-number counts and since time, and when it was
    received; ValueError for a body that is not that shape."""
    goals = body.get("goals") if isinstance(body, dict) else None
    if not isinstance(goals, list) or len(goals) > GOAL_CAPS_MAX:
        raise ValueError("goals must be a list of at most %d entries" % GOAL_CAPS_MAX)
    kept = []
    for goal in goals:
        if not (isinstance(goal, dict) and _GOAL_RE.fullmatch(str(goal.get("goal") or ""))):
            raise ValueError("each goal needs goal as OWNER/NAME#N")
        counts = {field: goal.get(field) for field in ("in_flight", "cap", "no_lane", "since")}
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v < 1e12
                   for v in counts.values()):
            raise ValueError("in_flight, cap, no_lane and since must be numbers")
        kept.append({"goal": goal["goal"], "since": float(counts["since"]),
                     **{field: int(counts[field]) for field in ("in_flight", "cap", "no_lane")}})
    return {"goals": kept, "received_at": now}


def goal_caps_section(stored: Any, now: float) -> str:
    """A Needs-you section for each goal stuck at its in-flight cap: how
    long, its count against the cap, and how many of its in-flight tickets
    wait with no lane that takes them. Nothing when none is, or the report is
    older than GOAL_CAPS_MAX_AGE."""
    if not isinstance(stored, dict) or not isinstance(stored.get("received_at"), (int, float)):
        return ""
    if now - stored["received_at"] > GOAL_CAPS_MAX_AGE or not stored.get("goals"):
        return ""
    esc = html.escape
    rows = "".join(
        '<li class="card"><strong>%s</strong> stuck at its cap for %.0f hours: %d of %d in flight, '
        '%d blocked on no lane.</li>' % (
            esc(goal["goal"]), max(0.0, now - goal["since"]) / 3600, goal["in_flight"], goal["cap"],
            goal["no_lane"])
        for goal in stored["goals"])
    return ('<section class="needs-group needs-goal-caps" data-kind="goal-caps"><h2>Goals stuck at '
            'their cap (%d)</h2><p class="muted">the station skips their triage rewrites; tickets no '
            'lane takes are rerouted after hours and go to the hand-off pool within a day</p>'
            '<ul>%s</ul></section>') % (len(stored["goals"]), rows)



# The station's needs-brain holds older than a day, a repo a row (bus/station.py
# lift_brain_holds, loom#1872): kept while the station refreshes them (a run
# every half hour), dropped once a report is this old.
BRAIN_HOLDS_MAX_AGE = 2 * 3600
BRAIN_HOLDS_MAX = 200
_REPO_RE = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")


def brain_holds(body: Any, now: float) -> Dict[str, Any]:
    """The stored form of a POST /api/brain-holds body: each repo as
    OWNER/NAME with its whole-number counts, and when it was received;
    ValueError for a body that is not that shape."""
    repos = body.get("repos") if isinstance(body, dict) else None
    if not isinstance(repos, list) or len(repos) > BRAIN_HOLDS_MAX:
        raise ValueError("repos must be a list of at most %d entries" % BRAIN_HOLDS_MAX)
    kept = []
    for row in repos:
        if not (isinstance(row, dict) and _REPO_RE.fullmatch(str(row.get("repo") or ""))):
            raise ValueError("each row needs repo as OWNER/NAME")
        counts = {field: row.get(field) for field in ("old", "person", "unknown")}
        if not all(isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 1e6
                   for v in counts.values()):
            raise ValueError("old, person and unknown must be whole numbers")
        kept.append({"repo": row["repo"], **counts})
    return {"repos": kept, "received_at": now}


def brain_holds_section(stored: Any, now: float) -> str:
    """A Needs-you section with each repo's count of needs-brain holds
    older than a day, and how many of them a person set or nobody can tell
    who set (the factory never lifts those). Nothing when no repo has one,
    or the report is older than BRAIN_HOLDS_MAX_AGE."""
    if not isinstance(stored, dict) or not isinstance(stored.get("received_at"), (int, float)):
        return ""
    rows = [r for r in stored.get("repos") or [] if r.get("old")]
    if now - stored["received_at"] > BRAIN_HOLDS_MAX_AGE or not rows:
        return ""
    esc = html.escape
    items = "".join(
        '<li class="card"><strong>%s</strong>: %d held over a day (%d set by a person, %d by '
        'an unknown setter).</li>' % (esc(row["repo"]), row["old"], row["person"], row["unknown"])
        for row in rows)
    return ('<section class="needs-group needs-brain-holds" data-kind="brain-holds"><h2>needs-brain '
            'holds older than a day (%d)</h2><p class="muted">the factory resolves the ones Loom set, a '
            'few each half hour; a hold a person set, or one nobody can tell who set, waits for a '
            'person</p><ul>%s</ul></section>') % (sum(r["old"] for r in rows), items)
