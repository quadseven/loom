"""The evidence close (owner's rulings of 2026-10-31, loom#2325, and 2026-11-19).

The 2026-10-22 hardening (#1524, #1532) keeps a ticket open while a live
criterion has no evidence, and shadow mode holds every close. The ruling of
2026-10-31 let this one close type revise that rule for a shipped ticket
whose one Live box was its only unticked box.

The ruling of 2026-11-19 widens it. Loom keeps holding finished tickets open
for live proof it cannot collect, and the backlog grows though the work is
merged. From now on a shipped ticket whose merged change still stands closes
without waiting for live evidence. Proof belongs in tests that run in CI or a
throwaway environment at build time, not in a live observation after the
fact. A step only a person can take still keeps a ticket open. Every other
close keeps the 2026-10-22 rule.

Claimed issues with live boxes use the same merge checks, Proof due issue,
budget and reopen count as shipped tickets. The claimed sweep passes the
pull requests whose Refs lines name the issue.

A ticket closes here only when all of these hold (`boxes`, `label_hold`,
`pulls_verdict`, `tier_refusal`, `symbol_refusal`; the station reads the rest,
bus/station.py _evidence_close):

- every unticked box is a live or a code box (bus/criteria_gate.py `kind`),
  any number of them and at least one live; an unticked operator or decision
  box keeps the ticket open;
- it has no regression, urgent, runaway, needs-brain or audit-failed label, no
  ask open and no wait: label other than wait:live-event, which marks the wait
  this ruling ends, and it never closed this way before;
- every linked pull request is merged, with green CI on its merge commit, and
  the default branch carries that commit;
- every pull request's tier is below deploy-config;
- the symbols each one added are still defined on the default branch, and
  the ones it removed are still gone;
- Loom has no check of its own for any unticked live box (a proof, a runnable
  check, a time window): a box with one keeps that path first, and its ticket
  with it. The ticket's Live line, when it has one, is known to be meetable
  (bus/unmeetable.py reads that one line; a live box beside it is not looked
  up).

A doubt refuses: a patch GitHub left out, a file that cannot be read, or a
lookup that failed. The close carries `closed-unproven`, and every unticked
live box goes into one rolling Proof due issue per repo, written before the
close so a box is never closed unlisted. An unticked code box is not listed:
the merged change is its evidence. It is the one close that passes through
shadow (bus/shadow.py evidence_close). Merges stay held. It makes no AI call.

The guardrail: reopens stay at or below MAX_REOPEN_RATE of seven days of
closes, including at least the newest 100 since resume when available.
A reopened ticket keeps the label, so one listing per repo finds it,
and it stays counted after it closes again. Above the limit this close type
stops until the operator resumes it. The first reopen of a young run stops it
too, on purpose: fewer than 20 closes cannot hold one at 5%. A stop raises one
needs:human ask on a Proof due issue; an operator's own pause raises none.

    python3 -m bus.evidence_close status
    python3 -m bus.evidence_close pause
    python3 -m bus.evidence_close resume    # closes before now stop counting

The station owns STATE_FILE; these commands write only CONTROL_FILE.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from typing import Any, Dict, Iterable, List, Mapping, NamedTuple, Optional, Sequence, Tuple

from . import claimed, criteria_gate, labels, merge_effect, merge_tier, proof, status, verify_data
from .github import GitHub, rest_login

LABEL = "closed-unproven"
PROOF_LABEL = "loom:proof-due"
PROOF_TITLE = "Proof due: Live boxes of tickets closed unproven"
STATE_FILE = "evidence-close.json"
CONTROL_FILE = "evidence-close-control.json"
# The one wait: label that does not hold a ticket back: it marks the wait for
# live evidence the 2026-11-19 ruling ends (bus/brain.py WAIT_LIVE_EVENT).
LIVE_EVENT_WAIT = "wait:live-event"

# Closes a pass may make, and tickets it may read in full (about five reads
# each, more for a pull request with many source files). The 2026-11-19
# ruling raised the three bounds from 3, 6 and 10.
CLOSES_PER_PASS = 10
READS_PER_PASS = 20
# At most this many a day, so a reopen has time to count before the next
# batch.
CLOSES_PER_DAY = 150
DAY = 24 * 3600
MAX_REOPEN_RATE = 0.05
MIN_CLOSES = 100
REOPEN_SPAN = 7 * DAY
# A refused ticket is read again after this long.
RECHECK = 6 * 3600
CLOSES_KEPT = 1000
PROOF_SHOWN = 150
BOX_WIDTH = 240
# Files a pull request may make the close read, and the most its file list
# may show: GitHub's first page, and a full page may hide the rest.
MAX_SOURCE_READS = 10
MAX_FILES = 100

HOLD_LABELS = ("loom:regression", "loom:urgent", "loom:runaway", "needs-brain",
               "loom:audit-failed", "loom:verified", LABEL)
TIERS = (merge_tier.DOCS_TESTS, merge_tier.APP_CODE)

# What the station does with a ticket after reading it.
CLOSED, WAIT, OTHER = "closed", "wait", "other"

TICKET_LINKS = """
query TicketPulls($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      timelineItems(first: 100, itemTypes: [CROSS_REFERENCED_EVENT, CONNECTED_EVENT, ISSUE_COMMENT]) {
        pageInfo { hasNextPage }
        nodes {
          __typename
          ... on CrossReferencedEvent { source { ...pull } }
          ... on ConnectedEvent { source { ...pull } subject { ...pull } }
          ... on IssueComment { author { login __typename } body }
        }
      }
    }
  }
}
fragment pull on PullRequest { __typename number repository { nameWithOwner } }
"""


def ticket_pull_numbers(gh: GitHub, repo: str, issue: Mapping[str, Any],
                        authors: Iterable[str]) -> List[int]:
    """Same-repo PR candidates from one timeline read and the ticket's status."""
    owner, name = repo.split("/")
    data = gh.graphql(TICKET_LINKS, {"owner": owner, "name": name, "number": issue["number"]})
    try:
        page = data["repository"]["issue"]["timelineItems"]
        nodes = page["nodes"]
        if page["pageInfo"]["hasNextPage"] is not False or not isinstance(nodes, list):
            raise ValueError("incomplete ticket timeline")
    except (KeyError, TypeError) as err:
        raise ValueError("missing ticket timeline") from err

    allowed = {a.lower() for a in authors}
    events = status.body_events(issue.get("body")) if status._ours(issue, allowed) else []
    numbers = set()
    for node in nodes:
        if not isinstance(node, dict):
            raise ValueError("unreadable timeline event")
        for ref in (node.get("source") or {}, node.get("subject") or {}):
            if (ref.get("__typename") == "PullRequest"
                    and ref["repository"]["nameWithOwner"].lower() == repo.lower()):
                numbers.add(ref["number"])
        if (node.get("__typename") == "IssueComment" and status.is_status(node.get("body"))
                and rest_login(node.get("author")).lower() in allowed):
            events.extend(status.parse(node["body"]))
    opened = re.compile(r"Lane \S+ opened a pull request: https://github\.com/%s/pull/(\d+)"
                        % re.escape(repo), re.I)
    for _, event in events:
        match = opened.fullmatch(event)
        if match:
            numbers.add(int(match.group(1)))
    if len(numbers) > verify_data.MAX_PULLS:
        raise ValueError("too many linked pull requests")
    return sorted(numbers)


class Pass(NamedTuple):
    """One pass's budget; a list so the station can spend it."""
    left: List[int]        # [closes, reads]
    counts: Dict[str, int]
    paused: str            # why this close type is stopped, or ""


def new_pass(state: Dict[str, Any], control: Mapping[str, Any], now: float) -> Pass:
    today = len([c for c in closes(state) if 0 <= now - float(c.get("at") or 0) < DAY])
    why = paused_why(state, control, now)
    # A stop holds until resume even after its triggering closes age out.
    if why and why != OPERATOR_PAUSED:
        state["rate_stop"] = {"since": control.get("at", 0), "why": why}
    return Pass([min(CLOSES_PER_PASS, max(0, CLOSES_PER_DAY - today)), READS_PER_PASS],
                {"closed": 0, "read": 0, "waiting": 0}, why)


def bound_line(run: Pass) -> str:
    """The pass's one log line."""
    return ("evidence close: %d closed, %d read, %d waiting; at most %d closes and %d reads "
            "a pass, %d closes a day" % (run.counts["closed"], run.counts["read"],
                                         run.counts["waiting"], CLOSES_PER_PASS,
                                         READS_PER_PASS, CLOSES_PER_DAY))


def boxes(body: str) -> List[str]:
    """The ticket's unticked live boxes, or nothing when it is not a
    candidate: an unticked operator or decision box, which only a person
    meets, or no unticked live box. An unticked code box does not hold it."""
    kinds = [(criteria_gate.kind(text), text)
             for checked, text in proof.criteria(body or "") if not checked]
    if any(kind == criteria_gate.OPERATOR for kind, _ in kinds):
        return []
    return [text for kind, text in kinds if kind == criteria_gate.LIVE]


def label_hold(names: Iterable[str]) -> bool:
    """Whether a ticket's labels keep it out of this close."""
    have = set(names)
    return (bool(have & set(HOLD_LABELS))
            or labels.needs_human(have)
            or any(n.startswith("wait:") and n != LIVE_EVENT_WAIT for n in have))


def pulls_verdict(pulls: Sequence[Mapping[str, Any]], error: str) -> Tuple[str, str]:
    """("", "") when every linked pull request is merged, read, on the default
    branch and green on its merge commit; (WAIT, why) while a check has not
    finished; (OTHER, why) otherwise."""
    if error:
        return OTHER, "its pull requests could not be read (%s)" % error
    if not pulls:
        return OTHER, "no merged pull request names it"
    for pr in pulls:
        if "check_runs" not in pr or "files" not in pr:
            return OTHER, "it has more merged pull requests than the close reads"
        number = int(pr.get("number") or 0)
        if pr.get("on_branch") is not True:
            return OTHER, "the default branch does not carry #%d" % number
        runs = verify_data.newest_runs(pr.get("check_runs") or [])
        results = [str(r.get("conclusion") or "") for r in runs]
        if not runs:
            return OTHER, "#%d has no checks on its merge commit" % number
        if "" in results:
            return WAIT, "the checks of #%d have not finished" % number
        if not set(results) <= set(claimed.PASSING) or "success" not in results:
            return OTHER, "#%d is not green on its merge commit" % number
    return "", ""


def tier_refusal(repo: str, pulls: Sequence[Mapping[str, Any]], body: str) -> str:
    """Why a pull request's tier keeps the ticket open, or ""."""
    for pr in pulls:
        names = [str(x.get("name") or "") for x in pr.get("labels") or [] if isinstance(x, dict)]
        tier, why = merge_tier.classify(repo, pr.get("files") or [], body,
                                        "class:sensitive" in names)
        if tier not in TIERS:
            return "#%d is tier %s: %s" % (int(pr.get("number") or 0), tier, why)
    return ""


def symbol_files(pulls: Sequence[Mapping[str, Any]]
                 ) -> Tuple[Dict[str, Tuple[set, set]], str]:
    """path -> (symbols added, symbols removed) across the pull requests,
    oldest first so a later one's change stands, and why they cannot be read,
    or ""."""
    wanted: Dict[str, Tuple[set, set]] = {}
    for pr in sorted(pulls, key=lambda p: str(p.get("merged_at") or "")):
        files = pr.get("files") or []
        if len(files) >= MAX_FILES:
            return {}, "#%d changes too many files to read" % int(pr.get("number") or 0)
        for item in files:
            path = str(item.get("filename") or "")
            if not merge_effect.is_code(path) or verify_data.test_file(path):
                continue
            if not item.get("patch"):
                return {}, "GitHub shows no patch for %s" % path
            added, removed = merge_effect.changed_symbols(str(item["patch"]))
            have = wanted.setdefault(path, (set(), set()))
            have[0].difference_update(removed)
            have[1].difference_update(added)
            have[0].update(added)
            have[1].update(removed)
    wanted = {path: pair for path, pair in wanted.items() if pair[0] or pair[1]}
    if len(wanted) > MAX_SOURCE_READS:
        return {}, "it changes symbols in too many files to read"
    return wanted, ""


def symbol_refusal(path: str, text: str, added: Iterable[str], removed: Iterable[str]) -> str:
    """Why this file on the default branch undoes the change, or ""."""
    present = merge_effect.symbols_in(text)
    for name in sorted(added):
        if name not in present:
            return "%s is gone from %s" % (name, path)
    for name in sorted(removed):
        if name in present:
            return "%s is back in %s" % (name, path)
    return ""


def closes(state: Mapping[str, Any]) -> List[Dict[str, Any]]:
    found = state.get("closes")
    return [c for c in found if isinstance(c, dict)] if isinstance(found, list) else []


def _window(kept: List[Dict[str, Any]], now: float, minimum: int) -> List[Dict[str, Any]]:
    """Keep the full time span and at least the newest minimum records."""
    cutoff = now - REOPEN_SPAN
    return [c for c in kept[:-minimum] if float(c.get("at") or 0) >= cutoff] + kept[-minimum:]


def record(state: Dict[str, Any], repo: str, number: int, unproven: Sequence[str],
           now: float) -> None:
    """Keep one close that landed, with the Live boxes it left unproven."""
    kept = closes(state)
    kept.append({"repo": repo, "number": number, "boxes": list(unproven), "at": now,
                 "reopened": False})
    state["closes"] = _window(kept, now, CLOSES_KEPT)


def listed(close: Mapping[str, Any]) -> List[str]:
    """The Live boxes one kept close left unproven. A close kept before
    2026-11-19 holds its one box as `box`."""
    found = close.get("boxes")
    if isinstance(found, list):
        return [str(box) for box in found if box]
    return [str(close["box"])] if close.get("box") else []


def mark_reopened(state: Dict[str, Any], repo: str, numbers: Iterable[int], now: float) -> int:
    """Mark each of `numbers` reopened; a ticket that carries the label and
    has no record of its close counts as a close too. The number newly seen."""
    kept = closes(state)
    fresh = 0
    open_now = set(numbers)
    open_by_repo = state.setdefault("open_reopens", {})
    previously_open = set(open_by_repo.get(repo, []))
    for number in sorted(open_now):
        mine = [c for c in kept if c.get("repo") == repo and c.get("number") == number]
        if not mine:
            # Aging out its close record does not make a still-open ticket a new reopen.
            if number in previously_open:
                continue
            mine = [{"repo": repo, "number": number, "boxes": [], "at": now, "reopened": False}]
            kept.extend(mine)
        if not mine[-1].get("reopened"):
            mine[-1]["reopened"] = True
            fresh += 1
    open_by_repo[repo] = sorted(open_now)
    state["closes"] = _window(kept, now, CLOSES_KEPT)
    return fresh


def rate(state: Mapping[str, Any], control: Mapping[str, Any], now: float) -> Tuple[int, int]:
    """(reopened, closes) in the guardrail window since the last resume."""
    since = float(control.get("at") or 0)
    recent = _window([c for c in closes(state) if float(c.get("at") or 0) >= since],
                     now, MIN_CLOSES)
    return len([c for c in recent if c.get("reopened")]), len(recent)


OPERATOR_PAUSED = "the operator paused it"


def stop_ask(why: str) -> str:
    """The one ask a guardrail stop raises (brain.human)."""
    return ("Kind: decision\nThe evidence close stopped: %s. Read the reopened tickets "
            "with `python3 -m bus.evidence_close status`, then either run `python3 -m "
            "bus.evidence_close resume` or leave it stopped." % why)


def paused_why(state: Mapping[str, Any], control: Mapping[str, Any], now: float) -> str:
    """Why this close type is stopped, or ""."""
    if control.get("paused"):
        return OPERATOR_PAUSED
    stop = state.get("rate_stop") or {}
    if stop.get("since") == control.get("at", 0):
        return stop["why"]
    reopened, total = rate(state, control, now)
    if total and reopened / total > MAX_REOPEN_RATE:
        return "%d of %d closes reopened, above %d%%" % (
            reopened, total, round(MAX_REOPEN_RATE * 100))
    return ""


def refused_recently(state: Mapping[str, Any], ticket: str, now: float) -> bool:
    seen = (state.get("refused") or {}).get(ticket)
    at = seen.get("at") if isinstance(seen, dict) else None
    return isinstance(at, (int, float)) and 0 <= now - at < RECHECK


def note_refused(state: Dict[str, Any], ticket: str, why: str, now: float) -> None:
    """Keep a refusal for RECHECK; older ones go."""
    old = state.get("refused") if isinstance(state.get("refused"), dict) else {}
    kept = {k: v for k, v in old.items() if isinstance(v, dict)
            and 0 <= now - float(v.get("at") or 0) < RECHECK}
    kept[ticket] = {"at": now, "why": why}
    state["refused"] = kept


def proof_body(repo: str, state: Mapping[str, Any],
               extra: Sequence[Tuple[int, Sequence[str]]], clean: Any, paused: str = "") -> str:
    """The Proof due issue's body: every unproven Live box of each ticket
    closed unproven and not reopened, newest ticket first. `extra` is a close
    about to be made, and `clean` makes a box safe to post."""
    kept = [(int(c.get("number") or 0), listed(c)) for c in closes(state)
            if c.get("repo") == repo and not c.get("reopened")]
    rows = [(n, box) for n, found in reversed(kept + list(extra)) for box in found]
    lines = ["Each ticket below closed on merged code with a Live box unproven. Prove the "
             "box, or reopen the ticket if it does not hold. A reopen counts against this "
             "close type: it stops above %d%%." % round(MAX_REOPEN_RATE * 100), ""]
    if paused:
        lines += ["Paused: %s." % paused, ""]
    lines += ["- #%d: %s" % (n, clean(box)[:BOX_WIDTH]) for n, box in rows[:PROOF_SHOWN]]
    if len(rows) > PROOF_SHOWN:
        lines += ["", "%d older boxes are not shown." % (len(rows) - PROOF_SHOWN)]
    return "\n".join(lines) + "\n"


def state_dir(env: Optional[Mapping[str, str]] = None) -> str:
    values = os.environ if env is None else env
    home = str(values.get("LOOM_HOME") or "").strip() or os.path.join(
        os.path.expanduser("~"), "loom")
    return os.path.join(home, "state", "station")


def read_file(path: str) -> Dict[str, Any]:
    """The JSON object at `path`, empty when it is missing. ValueError when
    it exists and cannot be read: a guardrail's lost count is not a zero."""
    try:
        with open(path, encoding="utf-8") as fh:
            found = json.load(fh)
    except FileNotFoundError:
        return {}
    except OSError as err:
        raise ValueError("%s cannot be read: %s" % (path, type(err).__name__)) from err
    if not isinstance(found, dict):
        raise ValueError("%s is not an object" % path)
    return found


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m bus.evidence_close")
    parser.add_argument("command", choices=("status", "pause", "resume"))
    args = parser.parse_args(argv)
    folder = state_dir()
    control_path = os.path.join(folder, CONTROL_FILE)
    if args.command != "status":
        os.makedirs(folder, exist_ok=True)
        fresh = {"paused": args.command == "pause"}
        fresh["at"] = time.time() if args.command == "resume" \
            else read_file(control_path).get("at", 0)
        with open(control_path + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(fresh, fh)
        os.replace(control_path + ".tmp", control_path)
    state, control = read_file(os.path.join(folder, STATE_FILE)), read_file(control_path)
    now = time.time()
    reopened, total = rate(state, control, now)
    why = paused_why(state, control, now)
    print("%d of %d closes reopened; limit %d%%; %s"
          % (reopened, total, round(MAX_REOPEN_RATE * 100),
             "paused: " + why if why else "running"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
