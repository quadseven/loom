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
            # model its body names, else its lane's ladder model, as a claim
            # does (bus/factory.py claim_models).
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
    kept = [(key, params[key]) for key in ("state", "goal", "model", "done") if params.get(key)]
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
            '<span class="tickets-title">%s</span></a></td>'
            '<td class="tickets-state machine-fact state-%s">%s</td>'
            '<td class="tickets-model machine-fact">%s / %s</td>'
            '<td class="tickets-age machine-fact" title="%s">%s</td>'
            '<td class="tickets-pr machine-fact">%s</td></tr>') % (
        esc(ticket_href(row), quote=True), esc(title, quote=True), esc(short_ref(_key(row))), esc(title),
        esc(state or "unlabeled"), esc(state_text), esc(str(row.get("lane") or "no lane")), model_html,
        esc(age_title, quote=True), esc(age), pr_html)


def _sort_key(row: Dict[str, Any], entered: Dict[str, Sequence[Any]]):
    state = row.get("state")
    found = entered.get(_key(row))
    since = stamp(found[1]) if found and found[0] == state else stamp(row.get("updated_at"))
    return (ROW_ORDER.index(state) if state in ROW_ORDER else len(ROW_ORDER), since or float("inf"),
            str(row.get("repo")), int(row.get("number") or 0))


def render(rows: Sequence[Dict[str, Any]], params: Dict[str, List[str]], *,
           goal_titles: Dict[str, Optional[str]], entered: Dict[str, Sequence[Any]], now: float,
           ticket_href, short_ref) -> str:
    """The page body. Filters are query parameters: state, goal, model and
    done=1 (merged, shipped, verified and closed tickets instead of the ones
    in flight)."""
    esc = html.escape
    pick = {key: (params.get(key) or [""])[0] for key in ("state", "goal", "model", "done")}
    done = pick["done"] == "1" or pick["state"] in DONE
    scoped = [row for row in rows if (not pick["goal"] or row.get("goal") == pick["goal"])
              and (not pick["model"] or row.get("run_model") == pick["model"])]
    live = [row for row in scoped if not _is_done(row)]
    finished = [row for row in scoped if _is_done(row)]
    base = {"goal": pick["goal"], "model": pick["model"]}

    counters = ['<a class="chip tickets-counter" href="%s"%s>in flight <b>%d</b></a>' % (
        esc(_href(**base), quote=True), ' aria-current="true"' if not done and not pick["state"] else "", len(live))]
    for name in IN_FLIGHT:
        count = sum(row.get("state") == name for row in live)
        if count:
            counters.append('<a class="chip tickets-counter state-%s" href="%s"%s>%s <b>%d</b></a>' % (
                name, esc(_href(state=name, **base), quote=True),
                ' aria-current="true"' if pick["state"] == name else "", name, count))
    done_counts = [(name, sum(row.get("state") == name for row in finished)) for name in DONE]
    closed = sum(row.get("state") not in DONE for row in finished)
    detail = ", ".join("%s %d" % pair for pair in done_counts + [("closed", closed)] if pair[1])
    counters.append('<a class="chip tickets-counter tickets-done" href="%s"%s>Done <b>%d</b>%s</a>' % (
        esc(_href(done="1", **base), quote=True), ' aria-current="true"' if done and not pick["state"] else "",
        len(finished), '<small>%s</small>' % esc(detail) if detail else ""))
    if done:
        for name, count in done_counts:
            if count:
                counters.append('<a class="chip tickets-counter state-%s" href="%s"%s>%s <b>%d</b></a>' % (
                    name, esc(_href(state=name, **base), quote=True),
                    ' aria-current="true"' if pick["state"] == name else "", name, count))

    shown = finished if done else live
    if pick["state"]:
        shown = [row for row in shown if row.get("state") == pick["state"]]

    models: Dict[str, int] = {}
    for row in rows:
        if row.get("run_model"):
            models[row["run_model"]] = models.get(row["run_model"], 0) + 1
    model_chips = "".join('<a class="chip" href="%s"%s>%s %d</a>' % (
        esc(_href(model=model, done=pick["done"]), quote=True),
        ' aria-current="true"' if model == pick["model"] else "", esc(model), count)
        for model, count in sorted(models.items()))
    active = [text for text in (
        "state %s" % pick["state"] if pick["state"] else "", "goal %s" % short_ref(pick["goal"]) if pick["goal"] else "",
        "model %s" % pick["model"] if pick["model"] else "") if text]
    parts = ['<div class="tickets-counters" role="group" aria-label="Filter by state">%s</div>' % "".join(counters)]
    bar = []
    if model_chips:
        bar.append('<details class="tickets-models"><summary>Filter by the model the last run used</summary>'
                   '<div class="chips">%s</div></details>' % model_chips)
    if active:
        bar.append('<p class="tickets-active machine-fact">Showing %s. <a href="/tickets">Clear filters</a></p>'
                   % esc(", ".join(active)))
    if bar:
        parts.append('<div class="tickets-bar">%s</div>' % "".join(bar))
    if not shown:
        parts.append('<div class="empty-state"><p>No tickets match these filters.</p>'
                     '<a class="chip" href="/tickets">Clear filters</a></div>')
        return '<section class="tickets-view">%s</section>' % "".join(parts)

    groups: Dict[str, List[Dict[str, Any]]] = {}
    for row in shown:
        groups.setdefault(row.get("goal") or NO_GOAL, []).append(row)
    order = sorted(groups, key=lambda goal: (goal == NO_GOAL, -len(groups[goal]), goal))
    head = ('<thead><tr><th scope="col">Ticket</th><th scope="col">State</th><th scope="col">Lane / model</th>'
            '<th scope="col">In state</th><th scope="col">PR</th></tr></thead>')
    for goal in order:
        members = sorted(groups[goal], key=lambda row: _sort_key(row, entered))
        title = goal_titles.get(goal) if goal != NO_GOAL else "Tickets with no Part of line"
        label = short_ref(goal) if goal != NO_GOAL else NO_GOAL
        link = ('<a class="tickets-goal-link" href="%s">only this goal</a>' % esc(_href(goal=goal), quote=True)
                if goal != NO_GOAL and not pick["goal"] else "")
        parts.append(
            '<details class="tickets-group" open><summary><span class="machine-fact">%s</span>'
            '<span class="tickets-group-title">%s</span>%s<span class="machine-fact tickets-count">%d</span></summary>'
            '<table class="tickets-table"><colgroup><col><col class="c-state"><col class="c-model">'
            '<col class="c-age"><col class="c-pr"></colgroup>%s<tbody>%s</tbody></table></details>' % (
                esc(label), esc(str(title or "")), link, len(members), head,
                "".join(_row_html(row, entered, now, ticket_href, short_ref) for row in members)))
    return '<section class="tickets-view">%s</section>' % "".join(parts)
