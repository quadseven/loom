"""The Tree page (loom#1257): every open issue as goal, epic, child.

Built from the rows and goal data the other pages already read; it makes no
request of its own. A child row says its Loom state, lane and model, its open
pull request and how long it has been in its state. Open issues under no epic
are listed in one group per repo, so nothing open is missing. The filter row
runs in the page: each row carries the kinds it belongs to.
"""
from __future__ import annotations

import html
from collections import OrderedDict
from typing import Any, Dict, List, Sequence

from bus.naming import strip_kind

from . import goals as goals_view
from . import tickets_table

WORKED = ("running", "pr")
NO_GOAL = "no goal"
NEEDS_PREFIX = "needs:"
FILTERS = (("all", "All"), ("worked", "Being worked"), ("operator", "Waiting on the operator"),
           ("unlabeled", "Not in the factory"))
# Hides every row outside the chosen filter, then every epic, goal and group
# left with no visible row. Without script every row stays shown.
SCRIPT = """(function () {
  var bar = document.querySelector('[data-tree-bar]');
  if (!bar) { return; }
  function apply(kind) {
    var rows = document.querySelectorAll('.tree-row');
    for (var i = 0; i < rows.length; i++) {
      rows[i].hidden = kind !== 'all' && rows[i].getAttribute('data-kinds').split(' ').indexOf(kind) < 0;
    }
    var boxes = document.querySelectorAll('.tree-box');
    for (var j = boxes.length - 1; j >= 0; j--) {
      var any = boxes[j].querySelector('.tree-row:not([hidden])');
      boxes[j].hidden = kind !== 'all' && !any;
      if (kind !== 'all' && any) { boxes[j].open = true; }
    }
    var buttons = bar.querySelectorAll('[data-tree-filter]');
    for (var k = 0; k < buttons.length; k++) {
      buttons[k].setAttribute('aria-pressed', buttons[k].getAttribute('data-tree-filter') === kind ? 'true' : 'false');
    }
    var empty = document.querySelector('[data-tree-empty]');
    if (empty) { empty.hidden = !!document.querySelector('.tree-row:not([hidden])'); }
  }
  bar.addEventListener('click', function (event) {
    var button = event.target.closest('[data-tree-filter]');
    if (button) { apply(button.getAttribute('data-tree-filter')); }
  });
})();"""


def kinds_of(row: Dict[str, Any]) -> List[str]:
    """The filters a row answers to: worked (running or pr), operator (human
    or a needs:<login> label) and unlabeled (no loom label at all)."""
    labels = [str(name) for name in row.get("labels") or []]
    out = []
    if row.get("state") in WORKED:
        out.append("worked")
    if row.get("state") == "human" or any(name.lower().startswith(NEEDS_PREFIX) for name in labels):
        out.append("operator")
    if not any(name.lower().startswith("loom:") for name in labels):
        out.append("unlabeled")
    return out


def _row_html(row, entered, now, ticket_href) -> str:
    esc = html.escape
    state = str(row.get("state") or "none")
    lane, model = row.get("lane"), row.get("run_model")
    who = " / ".join(esc(str(part)) for part in (lane, model) if part)
    pr = row.get("pr") or {}
    pr_html = (' <a class="machine-fact" href="%s">PR #%s</a>' % (esc(str(pr["url"]), quote=True), esc(str(pr["number"])))
               if pr.get("url") and pr.get("number") else "")
    age, age_title = tickets_table.age_in_state(row, entered, now)
    title = str(row.get("title") or "Untitled ticket")
    return ('<li class="tree-row" data-kinds="%s"><a class="machine-fact" href="%s">%s#%s</a>'
            '<a class="tree-title" href="%s">%s</a>'
            '<span class="tree-meta"><span class="machine-fact state-%s">%s</span>%s%s'
            '<span class="machine-fact" title="%s">%s</span></span></li>') % (
        esc(" ".join(kinds_of(row)), quote=True), esc(str(row.get("html_url") or ticket_href(row)), quote=True), esc(str(row.get("repo"))),
        esc(str(row.get("number"))), esc(ticket_href(row), quote=True), esc(title), esc(state, quote=True), esc(state),
        ' <span class="machine-fact">%s</span>' % who if who else "", pr_html, esc(age_title, quote=True), esc(age))


def _box(css: str, label: str, title: str, facts: str, children: str, open_: bool = True) -> str:
    esc = html.escape
    return ('<details class="tree-box %s"%s><summary><span class="machine-fact">%s</span>'
            '<span class="tree-box-title">%s</span><span class="machine-fact tree-count">%s</span></summary>%s</details>') % (
        css, " open" if open_ else "", esc(label), esc(title), esc(facts), children)


def _list(rows, entered, now, ticket_href) -> str:
    ordered = sorted(rows, key=lambda row: tickets_table._sort_key(row, entered))
    return '<ul class="tree-rows">%s</ul>' % "".join(_row_html(row, entered, now, ticket_href) for row in ordered)


def _epic_html(ref, info, members, entered, now, ticket_href, short_ref) -> str:
    live = [row for row in members if row.get("open", True)]
    done = sum(1 for row in members if tickets_table._is_done(row))
    flying = sum(1 for row in live if row.get("state") in goals_view.IN_FLIGHT_STATES)
    title = strip_kind(info.get("title")) or short_ref(ref)
    facts = "%d open / %d done / %d in flight" % (len(live), done, flying)
    return _box("tree-epic", short_ref(ref), title, facts, _list(live, entered, now, ticket_href))


def render(rows: Sequence[Dict[str, Any]], goal_data: Dict[str, Any], *, entered: Dict[str, Sequence[Any]],
           now: float, ticket_href, short_ref) -> str:
    """The page body: a filter row, then one box per goal holding one box per
    epic, then the open issues under no epic, one box per repo."""
    esc = html.escape
    goal_data = goal_data if isinstance(goal_data, dict) else {}
    by_epic: Dict[str, List[Dict[str, Any]]] = {}
    orphans: Dict[str, List[Dict[str, Any]]] = OrderedDict()
    for row in rows:
        if not row.get("open", True):
            continue
        if row.get("epic"):
            by_epic.setdefault(row["epic"], []).append(row)
        else:
            orphans.setdefault(str(row.get("repo")), []).append(row)
    epic_info: Dict[str, Dict[str, Any]] = {}
    goal_epics: Dict[str, List[str]] = {}
    for goal, info in goal_data.items():
        for epic in (info or {}).get("epics") or []:
            if isinstance(epic, dict) and epic.get("ref"):
                epic_info[epic["ref"]] = epic
                goal_epics.setdefault(goal, []).append(epic["ref"])
    # An epic the goal data does not list still shows, under no goal.
    for ref in by_epic:
        if ref not in epic_info:
            epic_info[ref] = {}
            goal_epics.setdefault(NO_GOAL, []).append(ref)
    bar = ('<div class="tree-bar" data-tree-bar role="group" aria-label="Filter the tree">%s</div>' % "".join(
        '<button type="button" class="chip" data-tree-filter="%s" aria-pressed="%s">%s</button>' % (
            key, "true" if key == "all" else "false", esc(label)) for key, label in FILTERS))
    parts = []
    for goal in sorted(goal_epics, key=lambda g: (g == NO_GOAL, g)):
        refs = [ref for ref in goal_epics[goal] if by_epic.get(ref) or epic_info[ref].get("open", True)]
        if not refs:
            continue
        epics = "".join(_epic_html(ref, epic_info[ref], by_epic.get(ref, []), entered, now, ticket_href, short_ref)
                        for ref in refs)
        total = sum(len(by_epic.get(ref, [])) for ref in refs)
        info = goal_data.get(goal) or {}
        title = strip_kind(info.get("title")) or (short_ref(goal) if goal != NO_GOAL else "Epics with no goal")
        parts.append(_box("tree-goal", short_ref(goal) if goal != NO_GOAL else NO_GOAL, title,
                          "%d open" % total, epics))
    for repo, members in orphans.items():
        parts.append(_box("tree-orphans", repo, "Open issues under no epic", "%d open" % len(members),
                          _list(members, entered, now, ticket_href)))
    shown = "".join(parts) or '<p class="empty-state">No open issues in the snapshot.</p>'
    empty = '<p class="empty-state" data-tree-empty hidden>No issue matches this filter.</p>'
    return '<section class="tree-view">%s%s%s<script>%s</script></section>' % (bar, shown, empty, SCRIPT)
