"""Data builders and HTML for the /chain view: the command chain that drives
Loom, top to bottom (operator, mastermind sessions, brain, station, lanes and
factory, review bot), and the audit of what Loom did on its own. The station
and audit layers come from the station's POST /api/chain snapshot: the
dashboard has neither the station's shadow switch nor its state folder."""
from __future__ import annotations

import html
import os
import re
from typing import Any, Dict

from bus import shadow
from bus.factory import DEFAULT_REVIEW_BOT
from . import brain_view, operators
from .views import lane_state

# The operator Claude sessions that steer and audit Loom. Fixed by the
# operator, not read from GitHub; a login is the one their claims carry.
MASTERMIND = (("The operator's Loom session", "quadseven",
               "Steers Loom, coaches and audits the brain, lands operator pull requests."),
              ("The second operator's session (claim:ccqw)", "ccqw",
               "Takes Loom work after labelling it claim:ccqw."),
              ("The infra session", None,
               "Tends the cluster, hosts and secrets Loom runs on."))
# Ticket states the lanes layer counts, from the ticket snapshot.
LANE_COUNTS = ("running", "queued")
# What POST /api/chain accepts: this many repos and rows, these shapes.
CHAIN_REPOS_MAX = 200
CHAIN_ROWS_MAX = 50
TEXT_MAX = 200
_REPO_RE = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")
NO_STATION = "no data from the station yet"


def _int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _row(row) -> Dict[str, Any]:
    if not (isinstance(row, dict) and _REPO_RE.fullmatch(str(row.get("repo") or ""))
            and _int(row.get("number")) and isinstance(row.get("action"), str)):
        raise ValueError("each held or acted row needs action, repo as OWNER/NAME and a whole number")
    return {"action": row["action"][:TEXT_MAX], "repo": row["repo"], "number": row["number"],
            "at": str(row.get("at") or "")[:TEXT_MAX], "source": str(row.get("source") or "")[:TEXT_MAX]}


def chain_snapshot(body: Any, now: float) -> Dict[str, Any]:
    """The stored form of a POST /api/chain body, with when it was received;
    ValueError for a body that is not that shape."""
    if not isinstance(body, dict) or not isinstance(body.get("shadow_on"), bool):
        raise ValueError("shadow_on must be true or false")
    repos = body.get("repos")
    if not isinstance(repos, list) or len(repos) > CHAIN_REPOS_MAX:
        raise ValueError("repos must be a list of at most %d entries" % CHAIN_REPOS_MAX)
    kept = []
    for row in repos:
        if not (isinstance(row, dict) and _REPO_RE.fullmatch(str(row.get("repo") or ""))
                and isinstance(row.get("shadow"), bool)):
            raise ValueError("each repo needs repo as OWNER/NAME and shadow as true or false")
        kept.append({"repo": row["repo"], "shadow": row["shadow"]})
    rows = {}
    for key in ("held", "acted"):
        found = body.get(key)
        if not isinstance(found, list) or len(found) > CHAIN_ROWS_MAX:
            raise ValueError("%s must be a list of at most %d entries" % (key, CHAIN_ROWS_MAX))
        rows[key] = [_row(row) for row in found]
    at = body.get("at")
    return {"at": at if isinstance(at, (int, float)) and not isinstance(at, bool) else None,
            "shadow_on": body["shadow_on"], "repos": kept, "received_at": now, **rows}


def _mastermind(claims):
    """The fixed session list, each with what /operators lists for its login."""
    claims = claims or {}
    read = bool(claims.get("read_at"))
    groups = operators.person_groups(claims.get("items") or [], claims.get("needs") or []) if read else {}
    out = []
    for name, login, role in MASTERMIND:
        group = groups.get(login) if login else None
        out.append({"name": name, "login": login, "role": role, "read": read,
                    "claimed": len(group["claimed"]) if group else 0,
                    "waiting": len(group["waiting"]) if group else 0})
    return out


def _brain(payload):
    data = brain_view.build_brain(payload.get("brain_step"), payload.get("brain_step_error"),
                                  (payload.get("improving") or {}).get("journals", []))
    tick = data["ticks"][0] if data["ticks"] else None
    return {"tick": tick, "next": brain_view.split_next(data["next"])[0] if data["next"] else None,
            "reason": data["journal_reason"] or "the newest journal has no Next line",
            "lock": data["holder"] if data["progress"] is not None else None}


def _lanes(payload):
    rows = [row for found in (payload.get("tickets") or {}).values() if found for row in found]
    counts = {name: (sum(1 for row in rows if row.get("state") == name and row.get("open", True))
                     if rows else None) for name in LANE_COUNTS}
    lanes = payload.get("lanes") or []
    counts["held"] = sum(1 for lane in lanes if lane_state(lane) == "held") if lanes else None
    return counts


def _newest(rows):
    return sorted(rows or [], key=lambda row: str(row.get("at") or ""), reverse=True)


def build_chain(payload, chain, now, env=None):
    """Everything /chain shows. `chain` is the station's stored snapshot
    (chain_snapshot), or None when none has arrived."""
    env = os.environ if env is None else env
    bot = str(env.get("LOOM_REVIEW_BOT") or "").strip() or DEFAULT_REVIEW_BOT
    station = None
    if chain:
        station = {"on": chain["shadow_on"], "repos": chain["repos"],
                   "age": max(0.0, now - chain["received_at"]),
                   "acted": _newest(chain["acted"]), "held": _newest(chain["held"])}
    return {"mastermind": _mastermind(payload.get("operator_claims")), "brain": _brain(payload),
            "station": station, "lanes": _lanes(payload), "bot": bot + "[bot]"}


def _layer(heading, inner, *links):
    more = " ".join('<a href="%s">%s &rarr;</a>' % link for link in links)
    return '<section class="section card"><h2 class="label">%s</h2>%s%s</section>' % (
        html.escape(heading), inner, more)


def _no_data(why):
    return '<p class="empty-state">no data: %s.</p>' % html.escape(why)


def _age(seconds):
    minutes = int(seconds // 60)
    if minutes < 120:
        return "%d minute%s" % (minutes, "" if minutes == 1 else "s")
    return "%d hours" % (minutes // 60)


def _render_mastermind(rows):
    esc = html.escape
    items = []
    for row in rows:
        if not row["login"]:
            claims = "claims are not split out for this session"
        elif not row["read"]:
            claims = "no data: claims have not been read yet"
        elif row["claimed"] or row["waiting"]:
            claims = "%d claimed, %d waiting on %s" % (row["claimed"], row["waiting"], row["login"])
        else:
            claims = "nothing listed for %s on /operators" % row["login"]
        items.append('<li><p><strong>%s</strong>%s</p><p>%s</p><p class="machine-fact">%s</p></li>' % (
            esc(row["name"]), ' <span class="machine-fact">%s</span>' % esc(row["login"]) if row["login"] else "",
            esc(row["role"]), esc(claims)))
    return "<ul>%s</ul>" % "".join(items)


def _render_brain(data):
    esc = html.escape
    tick = data["tick"]
    if not tick and not data["next"]:
        return _no_data(data["reason"])
    parts = []
    if tick:
        parts.append('<p class="machine-fact">newest tick %s / %s / %s</p>' % (
            esc(brain_view._when(tick["at"])), esc(tick["holder"]), esc(tick["outcome"])))
    else:
        parts.append(_no_data("no tick in the goal journals"))
    if data["next"]:
        parts.append('<p class="next-move">%s</p>' % esc(data["next"]))
    else:
        parts.append(_no_data(data["reason"]))
    if data["lock"]:
        parts.append('<p class="machine-fact">%s holds the lock now</p>' % esc(data["lock"]))
    return "".join(parts)


def _render_station(data):
    esc = html.escape
    if data is None:
        return '<p class="empty-state">%s.</p>' % NO_STATION
    rows = "".join('<li><span class="machine-fact">%s</span> <strong>%s</strong></li>' % (
        esc(row["repo"]), "shadow" if row["shadow"] else "live") for row in data["repos"])
    switch = "on" if data["on"] else "off for every repo"
    return ('<p>Shadow mode is %s: a repo in shadow has its merges and closes recorded, not taken.</p>'
            '%s<p class="machine-fact">sent by the station %s ago, from bus.shadow</p>' % (
                switch, "<ul>%s</ul>" % rows if rows else _no_data("the station watches no repo"),
                esc(_age(data["age"]))))


def _render_lanes(counts):
    words = {"running": ("ticket", "running"), "queued": ("ticket", "queued"), "held": ("lane", "held")}
    return "<ul>%s</ul>" % "".join(
        "<li>%s</li>" % ("%d %s%s %s" % (counts[key], noun, "" if counts[key] == 1 else "s", state)
                         if counts[key] is not None else "%ss %s: no data" % (noun, state))
        for key, (noun, state) in words.items())


def _render_bot(bot):
    return ('<p><code>%s</code> reviews every pull request: Elder reviews the code, Chief checks '
            'the plan in its body.</p>' % html.escape(bot))


def _audit_rows(rows, verb):
    """One line per row, linked to its issue or pull request (GitHub
    redirects /issues/N to the pull request when N is one)."""
    esc = html.escape
    return "<ol>%s</ol>" % "".join(
        '<li><span class="machine-fact">%s</span> %s %s <a href="%s">%s#%d</a></li>' % (
            esc(brain_view._when(row["at"])), verb, esc(row["action"]),
            esc(shadow.link(row["repo"], row["number"], "issue"), quote=True),
            esc(row["repo"]), row["number"])
        for row in rows)


def _render_audit(data):
    if data is None:
        return '<p class="empty-state">%s.</p>' % NO_STATION
    acted = _audit_rows(data["acted"], "did") if data["acted"] else _no_data(
        "the station recorded no merge or close of its own")
    held = _audit_rows(data["held"], "would") if data["held"] else _no_data(
        "the shadow ledger holds no entries")
    return ('<h3 class="label">Loom acted on its own, newest first</h3>%s'
            '<h3 class="label">Held by shadow mode, newest first</h3>%s'
            '<p class="machine-fact">sent by the station %s ago</p>' % (acted, held, html.escape(_age(data["age"]))))


def render_chain(data):
    layers = [
        _layer("Operator", "<p><strong>The operator (repo owner)</strong>, the human. Decides only "
                           "irreversible data loss and owner-only actions.</p>"),
        _layer("Mastermind", _render_mastermind(data["mastermind"]), ("/operators", "who holds what")),
        _layer("Brain", _render_brain(data["brain"]), ("/brain", "see the reasoning")),
        _layer("Station", _render_station(data["station"])),
        _layer("Lanes and factory", _render_lanes(data["lanes"]), ("/lanes", "lanes and models"),
               ("/hot-spots", "hot spots")),
        _layer("Review bot", _render_bot(data["bot"]), ("/hot-spots", "what it keeps sending back")),
        _layer("Audit: what Loom did on its own in live repos", _render_audit(data["station"])),
    ]
    return '<div class="chain-view">%s</div>' % "".join(layers)
