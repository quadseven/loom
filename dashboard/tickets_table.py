"""The Tickets page: one dense table per goal home, in-flight tickets first.

Each ticket row says which model its last run used. Three records name a
run's model, and the newest of them wins: the transcript the station uploads
for every finished run, the footer a lane writes when it opens a pull request
("Loom ticket <id>, lane <lane>, model <model>."), and the station's "Claimed
for lane" comment, whose model follows the ladder rules in bus/factory.py
(claim_models). A ticket with none of them and no sign of a run says "not run
yet"; a run whose record names no model says so, never a guess.
"""
from __future__ import annotations

import html
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlencode

from bus import brain, factory
from bus.metrics import stamp

from . import operators

STATES = ("queued", "briefed", "running", "pr", "merged", "shipped", "verified", "failed")
IN_FLIGHT = ("queued", "briefed", "running", "pr", "failed")
DONE = ("merged", "shipped", "verified")
# The order rows take inside a goal: what needs acting on first.
ROW_ORDER = ("failed", "pr", "running", "briefed", "queued", "merged", "shipped", "verified")
# States a ticket only reaches after a lane ran on it.
RAN_STATES = ("running", "pr", "merged", "shipped", "verified", "failed")
NOT_RUN = "not run yet"
NOT_RECORDED = "model not recorded"
NO_GOAL = "no goal"
_ISSUE_NUMBER = re.compile(r"/issues/([0-9]+)$")


def _key(row: Dict[str, Any]) -> str:
    return "%s#%s" % (row.get("repo"), row.get("number"))


def _model(value: Any) -> Optional[str]:
    """A recorded model name, or None for an empty or "not recorded" one."""
    text = str(value or "").strip()
    return text if text and text != "not recorded" else None


def _claims_by_ticket(comments: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    for comment in sorted(comments or [], key=lambda c: str(c.get("created_at") or "")):
        number = _ISSUE_NUMBER.search(str(comment.get("issue_url") or ""))
        if number and comment.get("repo"):
            out.setdefault("%s#%s" % (comment["repo"], number.group(1)), []).append(comment)
    return out


def last_run(row: Dict[str, Any], *, transcript: Optional[Dict[str, Any]] = None,
             footer: Optional[Sequence[Any]] = None,
             comments: Sequence[Dict[str, Any]] = ()) -> Tuple[Optional[float], Optional[str]]:
    """(when, model) of the ticket's newest run, from its newest transcript,
    its lane pull request footer and its claim comments; (None, None) when no
    record of a run exists. The model is None when that run's record names
    none. A tie goes to the transcript, then the footer: both are written by
    the run itself, a claim's model is inferred."""
    found: List[Tuple[float, int, Optional[str]]] = []
    if transcript:
        found.append((stamp(transcript.get("started_at")), 2, _model(transcript.get("model"))))
    if footer:
        found.append((stamp(footer[0]), 1, _model(footer[1])))
    claims = [(comment, model) for comment, model in factory.claim_models(row, comments) if comment]
    if claims:
        comment, model = claims[-1]
        found.append((stamp(comment.get("created_at")), 0, _model(model)))
    if not found:
        return None, None
    when, _, model = max(found, key=lambda item: (item[0], item[1]))
    return when, model


def resolve_models(rows: Sequence[Dict[str, Any]], *, footer_runs: Dict[str, Sequence[Any]],
                   latest: Dict[str, Dict[str, Any]], comments: Iterable[Dict[str, Any]]) -> None:
    """Set run_model (the newest run's model, or None) and model_text (what
    the page shows) on each row."""
    claims = _claims_by_ticket(comments)
    for row in rows:
        key = _key(row)
        footer = footer_runs.get(key)
        when, model = last_run(row, transcript=latest.get(key), footer=footer, comments=claims.get(key, ()))
        if when is None and row.get("state") in RAN_STATES:
            # It ran before the records the dashboard keeps: the run used the
            # model its body names, else its lane's configured model, as a
            # claim does (bus/factory.py claim_models).
            when = 0.0
            model = _model(row.get("model")) or (brain._lane_model(row["lane"]) if row.get("lane") else None)
        row["run_model"] = model
        row["model_text"] = model or (NOT_RUN if when is None else NOT_RECORDED)
        if footer and len(footer) >= 4 and footer[2] and footer[3]:
            row["pr"] = {"number": footer[2], "url": footer[3]}


def _age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return "%dm" % max(1, seconds // 60)
    if seconds < 48 * 3600:
        return "%dh" % (seconds // 3600)
    return "%dd" % (seconds // 86400)


def age_in_state(row: Dict[str, Any], entered: Dict[str, Sequence[Any]], now: float) -> Tuple[str, str]:
    """(short text, full sentence) for how long the ticket has been in its
    state: from its label event when the hourly read saw this state, else its
    last update, which the text says."""
    found = entered.get(_key(row))
    if found and found[0] == row.get("state") and stamp(found[1]):
        text = _age(now - stamp(found[1]))
        return text, "in %s for %s" % (row.get("state"), text)
    updated = stamp(row.get("updated_at"))
    if updated:
        text = _age(now - updated)
        return "upd %s" % text, "state entry time not read yet; last updated %s ago" % text
    return "-", "no state entry or update time"


def _is_done(row: Dict[str, Any]) -> bool:
    return row.get("state") in DONE or not row.get("open", True)


def _href(**params: str) -> str:
    kept = [(key, params[key]) for key in ("state", "goal", "epic", "model", "done") if params.get(key)]
    return "/tickets" + ("?" + urlencode(kept) if kept else "")


def _row_html(row: Dict[str, Any], entered: Dict[str, Sequence[Any]], now: float,
              ticket_href, short_ref) -> str:
    esc = html.escape
    state = row.get("state") if row.get("state") in STATES else None
    state_text = (state or "unlabeled") + ("" if row.get("open", True) else " / closed")
    title = str(row.get("title") or "Untitled ticket")
    model = row.get("run_model")
    model_html = ('<a href="%s">%s</a>' % (esc(_href(model=model), quote=True), esc(model)) if model
                  else '<span class="tickets-none">%s</span>' % esc(row.get("model_text") or NOT_RUN))
    age, age_title = age_in_state(row, entered, now)
    pr = row.get("pr") or {}
    pr_html = ('<a href="%s">#%s</a>' % (esc(str(pr["url"]), quote=True), esc(str(pr["number"])))
               if pr.get("url") and pr.get("number") else '<span class="tickets-none">-</span>')
    return ('<tr><td class="tickets-ticket"><a href="%s" title="%s"><span class="machine-fact">%s</span>'
            '<span class="tickets-title">%s</span>%s</a></td>'
            '<td class="tickets-state machine-fact state-%s">%s</td>'
            '<td class="tickets-model machine-fact">%s / %s</td>'
            '<td class="tickets-age machine-fact" title="%s">%s</td>'
            '<td class="tickets-pr machine-fact">%s</td></tr>') % (
        esc(ticket_href(row), quote=True), esc(title, quote=True), esc(short_ref(_key(row))), esc(title),
        operators.chip_html(row), esc(state or "unlabeled"), esc(state_text), esc(str(row.get("lane") or "no lane")), model_html,
        esc(age_title, quote=True), esc(age), pr_html)


def _sort_key(row: Dict[str, Any], entered: Dict[str, Sequence[Any]]):
    state = row.get("state")
    found = entered.get(_key(row))
    since = stamp(found[1]) if found and found[0] == state else stamp(row.get("updated_at"))
    return (ROW_ORDER.index(state) if state in ROW_ORDER else len(ROW_ORDER), since or float("inf"),
            str(row.get("repo")), int(row.get("number") or 0))


def _counter(label: str, count: int, href: str, current: bool, state: str = "", extra: str = "") -> str:
    return '<a class="chip tickets-counter%s" href="%s"%s>%s <b>%d</b>%s</a>' % (
        " " + state if state else "", html.escape(href, quote=True),
        ' aria-current="true"' if current else "", html.escape(label), count, extra)


def _counters(live, finished, pick, done) -> str:
    """The state counters: in flight, each in-flight state, Done with its
    counts, and in the Done view each done state. Each one filters the table."""
    base = {"goal": pick["goal"], "epic": pick["epic"], "model": pick["model"]}
    out = [_counter("in flight", len(live), _href(**base), not done and not pick["state"])]
    out += [_counter(name, count, _href(state=name, **base), pick["state"] == name, "state-" + name)
            for name in IN_FLIGHT for count in [sum(row.get("state") == name for row in live)] if count]
    done_counts = [(name, sum(row.get("state") == name for row in finished)) for name in DONE]
    closed = sum(row.get("state") not in DONE for row in finished)
    detail = ", ".join("%s %d" % pair for pair in done_counts + [("closed", closed)] if pair[1])
    out.append(_counter("Done", len(finished), _href(done="1", **base), done and not pick["state"], "tickets-done",
                        "<small>%s</small>" % html.escape(detail) if detail else ""))
    if done:
        out += [_counter(name, count, _href(state=name, **base), pick["state"] == name, "state-" + name)
                for name, count in done_counts if count]
    return '<div class="tickets-counters" role="group" aria-label="Filter by state">%s</div>' % "".join(out)


def _filter_bar(rows, pick, short_ref) -> str:
    """The epic and model filters (#533) and, when any filter is on, what it
    shows."""
    esc = html.escape
    models: Dict[str, int] = {}
    for row in rows:
        if row.get("run_model"):
            models[row["run_model"]] = models.get(row["run_model"], 0) + 1
    chips = "".join('<a class="chip" href="%s"%s>%s %d</a>' % (
        esc(_href(model=model, done=pick["done"]), quote=True),
        ' aria-current="true"' if model == pick["model"] else "", esc(model), count)
        for model, count in sorted(models.items()))
    epics: Dict[str, int] = {}
    for row in rows:
        if row.get("epic"):
            epics[row["epic"]] = epics.get(row["epic"], 0) + 1
    epic_chips = "".join('<a class="chip" href="%s"%s>%s %d</a>' % (
        esc(_href(epic=epic, done=pick["done"]), quote=True),
        ' aria-current="true"' if epic == pick["epic"] else "", esc(short_ref(epic)), count)
        for epic, count in sorted(epics.items()))
    active = [text for key, text in (("state", "state %s" % pick["state"]),
                                     ("goal", "goal %s" % short_ref(pick["goal"])),
                                     ("epic", "epic %s" % short_ref(pick["epic"])),
                                     ("model", "model %s" % pick["model"])) if pick[key]]
    bar = []
    if epic_chips:
        bar.append('<details class="tickets-epics"%s><summary>Filter by epic</summary>'
                   '<div class="chips">%s</div></details>' % (" open" if pick["epic"] else "", epic_chips))
    if chips:
        bar.append('<details class="tickets-models"><summary>Filter by the model the last run used</summary>'
                   '<div class="chips">%s</div></details>' % chips)
    if active:
        bar.append('<p class="tickets-active machine-fact">Showing %s. <a href="/tickets">Clear filters</a></p>'
                   % esc(", ".join(active)))
    return '<div class="tickets-bar">%s</div>' % "".join(bar) if bar else ""


TABLE_HEAD = ('<colgroup><col><col class="c-state"><col class="c-model"><col class="c-age"><col class="c-pr">'
              '</colgroup><thead><tr><th scope="col">Ticket</th><th scope="col">State</th>'
              '<th scope="col">Lane / model</th><th scope="col">In state</th><th scope="col">PR</th></tr></thead>')


def _group(goal, members, *, goal_titles, entered, now, ticket_href, short_ref, linked) -> str:
    """One goal home's collapsible table."""
    esc = html.escape
    members = sorted(members, key=lambda row: _sort_key(row, entered))
    title = goal_titles.get(goal) if goal != NO_GOAL else "Tickets with no Part of line"
    link = ('<a class="tickets-goal-link" href="%s">only this goal</a>' % esc(_href(goal=goal), quote=True)
            if linked and goal != NO_GOAL else "")
    return ('<details class="tickets-group" open><summary><span class="machine-fact">%s</span>'
            '<span class="tickets-group-title">%s</span>%s<span class="machine-fact tickets-count">%d</span></summary>'
            '<table class="tickets-table">%s<tbody>%s</tbody></table></details>') % (
        esc(short_ref(goal) if goal != NO_GOAL else NO_GOAL), esc(str(title or "")), link, len(members),
        TABLE_HEAD, "".join(_row_html(row, entered, now, ticket_href, short_ref) for row in members))


def render(rows: Sequence[Dict[str, Any]], params: Dict[str, List[str]], *,
           goal_titles: Dict[str, Optional[str]], entered: Dict[str, Sequence[Any]], now: float,
           ticket_href, short_ref) -> str:
    """The page body. Filters are query parameters: state, goal, epic, model and
    done=1 (merged, shipped, verified and closed tickets instead of the ones
    in flight)."""
    pick = {key: (params.get(key) or [""])[0] for key in ("state", "goal", "epic", "model", "done")}
    done = pick["done"] == "1" or pick["state"] in DONE
    scoped = [row for row in rows if (not pick["goal"] or row.get("goal") == pick["goal"])
              and (not pick["epic"] or row.get("epic") == pick["epic"])
              and (not pick["model"] or row.get("run_model") == pick["model"])]
    live = [row for row in scoped if not _is_done(row)]
    finished = [row for row in scoped if _is_done(row)]
    shown = [row for row in (finished if done else live) if not pick["state"] or row.get("state") == pick["state"]]
    parts = [_counters(live, finished, pick, done), _filter_bar(rows, pick, short_ref)]
    if not shown:
        parts.append('<div class="empty-state"><p>No tickets match these filters.</p>'
                     '<a class="chip" href="/tickets">Clear filters</a></div>')
        return '<section class="tickets-view">%s</section>' % "".join(parts)
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for row in shown:
        groups.setdefault(row.get("goal") or NO_GOAL, []).append(row)
    for goal in sorted(groups, key=lambda goal: (goal == NO_GOAL, -len(groups[goal]), goal)):
        parts.append(_group(goal, groups[goal], goal_titles=goal_titles, entered=entered, now=now,
                            ticket_href=ticket_href, short_ref=short_ref, linked=not pick["goal"]))
    return '<section class="tickets-view">%s</section>' % "".join(parts)
