"""The narrow evidence close (operator ruling of 2026-10-31, loom#2325).

The 2026-10-22 hardening (#1524, #1532) keeps a ticket open while a live
criterion has no evidence, and shadow mode holds every close. Shipped tickets
whose only open box is a Live box then sat open with nothing left to build.
This one close type revises that rule, for these tickets only. Every other
close keeps it.

A ticket closes here only when all of these hold (`boxes`, `label_hold`,
`pulls_verdict`, `tier_refusal`, `symbol_refusal`; the station reads the rest,
bus/station.py _evidence_close):

- its one Live box is the only unticked box;
- it has no regression, urgent, runaway, needs-brain or audit-failed label, no
  ask open and no wait: label, and it never closed this way before;
- every linked pull request is merged, with green CI on its merge commit, and
  the default branch carries that commit;
- every pull request's tier is below deploy-config;
- the symbols each one added are still defined on the default branch, and
  the ones it removed are still gone;
- Loom has no check of its own for the box (a proof, a runnable check, a time
  window), and the box is known to be meetable (bus/unmeetable.py).

A doubt refuses: a patch GitHub left out, a file that cannot be read, or a
lookup that failed. The close carries `closed-unproven`, and the box goes into
one rolling Proof due issue per repo, written before the close so a box is
never closed unlisted. It is the one close that passes through shadow
(bus/shadow.py evidence_close). Merges stay held. It makes no AI call.

The guardrail: reopens stay at or below MAX_REOPEN_RATE of the last WINDOW
closes. A reopened ticket keeps the label, so one listing per repo finds it,
and it stays counted after it closes again. Above the limit this close type
stops until the operator resumes it, and the stop raises one needs:human ask
on the repo's Proof due issue (the station holds the flag; an operator's own
pause raises none). The first reopen of a young run stops it too, on purpose:
fewer than 20 closes cannot hold one at 5%.

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

from . import claimed, labels, merge_effect, merge_tier, proof, tickets, verify_data

LABEL = "closed-unproven"
OPERATOR_PAUSE = "the operator paused it"
RESUME_COMMAND = "python3 -m bus.evidence_close resume"
PROOF_LABEL = "loom:proof-due"
PROOF_TITLE = "Proof due: Live boxes of tickets closed unproven"
STATE_FILE = "evidence-close.json"
CONTROL_FILE = "evidence-close-control.json"

# Closes a pass may make, and tickets it may read in full (about five reads
# each, more for a pull request with many source files).
CLOSES_PER_PASS = 3
READS_PER_PASS = 6
# The stock is not this close's job: at most this many a day, so a reopen has
# time to count before the next batch.
CLOSES_PER_DAY = 10
DAY = 24 * 3600
MAX_REOPEN_RATE = 0.05
WINDOW = 100
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


class Pass(NamedTuple):
    """One pass's budget; a list so the station can spend it."""
    left: List[int]        # [closes, reads]
    counts: Dict[str, int]
    paused: str            # why this close type is stopped, or ""


def new_pass(state: Mapping[str, Any], control: Mapping[str, Any], now: float) -> Pass:
    today = len([c for c in closes(state) if 0 <= now - float(c.get("at") or 0) < DAY])
    return Pass([min(CLOSES_PER_PASS, max(0, CLOSES_PER_DAY - today)), READS_PER_PASS],
                {"closed": 0, "read": 0, "waiting": 0}, paused_why(state, control))


def bound_line(run: Pass) -> str:
    """The pass's one log line."""
    return ("evidence close: %d closed, %d read, %d waiting; at most %d closes and %d reads "
            "a pass, %d closes a day" % (run.counts["closed"], run.counts["read"],
                                         run.counts["waiting"], CLOSES_PER_PASS,
                                         READS_PER_PASS, CLOSES_PER_DAY))


def boxes(body: str) -> List[str]:
    """The ticket's one unticked Live box, or nothing when it is not a
    candidate: an unticked box of any other kind, no Live box, or more than
    one (bus/unmeetable.py reads one Live line)."""
    found = proof.criteria(body or "")
    live = [text for _, text in found if tickets.LIVE_CRITERION_RE.match(text)]
    unticked = [text for checked, text in found if not checked]
    return unticked if len(live) == 1 and unticked == live else []


def label_hold(names: Iterable[str]) -> bool:
    """Whether a ticket's labels keep it out of this close."""
    have = set(names)
    return ("loom:shipped" not in have or bool(have & set(HOLD_LABELS))
            or labels.needs_human(have) or any(n.startswith("wait:") for n in have))


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


def record(state: Dict[str, Any], repo: str, number: int, box: str, now: float) -> None:
    """Keep one close that landed."""
    kept = closes(state)
    kept.append({"repo": repo, "number": number, "box": box, "at": now, "reopened": False})
    state["closes"] = kept[-CLOSES_KEPT:]


def mark_reopened(state: Dict[str, Any], repo: str, numbers: Iterable[int], now: float) -> int:
    """Mark each of `numbers` reopened; a ticket that carries the label and
    has no record of its close counts as a close too. The number newly seen."""
    kept = closes(state)
    fresh = 0
    for number in numbers:
        mine = [c for c in kept if c.get("repo") == repo and c.get("number") == number]
        if not mine:
            mine = [{"repo": repo, "number": number, "box": "", "at": now, "reopened": False}]
            kept.extend(mine)
        if not mine[-1].get("reopened"):
            mine[-1]["reopened"] = True
            fresh += 1
    state["closes"] = kept[-CLOSES_KEPT:]
    return fresh


def rate(state: Mapping[str, Any], control: Mapping[str, Any]) -> Tuple[int, int]:
    """(reopened, closes) among the last WINDOW closes since the last resume."""
    since = float(control.get("at") or 0)
    recent = [c for c in closes(state) if float(c.get("at") or 0) >= since][-WINDOW:]
    return len([c for c in recent if c.get("reopened")]), len(recent)


def ask(why: str) -> str:
    """The one needs:human ask a guardrail stop raises on a repo's Proof due
    issue: the reason it stopped and the command that restarts it."""
    return ("Kind: decision\nThe evidence close is stopped: %s. Read the reopened "
            "closes listed in this issue, then run `%s` to restart this close, "
            "or leave it stopped." % (why, RESUME_COMMAND))


def paused_why(state: Mapping[str, Any], control: Mapping[str, Any]) -> str:
    """Why this close type is stopped, or ""."""
    if control.get("paused"):
        return OPERATOR_PAUSE
    reopened, total = rate(state, control)
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


def proof_body(repo: str, state: Mapping[str, Any], extra: Sequence[Tuple[int, str]],
               clean: Any, paused: str = "") -> str:
    """The Proof due issue's body: the Live box of each ticket closed unproven
    and not reopened, newest first. `extra` is a close about to be made, and
    `clean` makes a box safe to post."""
    rows = [(int(c.get("number") or 0), str(c.get("box") or "")) for c in closes(state)
            if c.get("repo") == repo and not c.get("reopened") and c.get("box")]
    rows = list(reversed(rows + list(extra)))
    lines = ["Each ticket below closed on merged code with its Live box unproven. Prove the "
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
    reopened, total = rate(state, control)
    why = paused_why(state, control)
    print("%d of %d closes reopened; limit %d%%; %s"
          % (reopened, total, round(MAX_REOPEN_RATE * 100),
             "paused: " + why if why else "running"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
