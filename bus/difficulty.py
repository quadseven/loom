"""A blind settled difficulty after merge, beside the perceived one.

The factory routes a ticket to a lane tier by the Difficulty: line on its
brief (the perceived difficulty, set when the ticket is filed) and never
checked that guess against how hard the work turned out to be, so routing
could not learn.

After a lane pull request merges, the scorecard (bus/scorecard.py) asks
`settle` for a settled difficulty. The scorer is blind by construction: it
takes only what the merged work shows (the diff's changed lines and files,
the fix rounds the factory sent back, and the review bot's findings) and
never the ticket's Difficulty: line, so the guess cannot steer the score.
The perceived difficulty is read beside it and is never changed by this
step: nothing here writes to the ticket.

Each signal gives 0, 1 or 2 points:

- changed lines (additions plus deletions): up to 60, up to 300, more;
- changed files: up to 3, up to 10, more;
- fix rounds sent back: none, one, two or more;
- review findings: none, one or two, three or more (or any bad-work one).

A total of 0 or 1 is easy, 2 to 4 medium, 5 or more hard.

`report` compares the two per lane, model and kind (the pull request's
change class, bus/change_class.py) over the last REPORT_DAYS days: how many
agree, how many turned out harder or easier, and the bias (the mean of
settled minus perceived, in tiers: +1.00 means every ticket was one tier
harder than its brief said). The table goes on the lane status comment
beside the scorecard.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Tuple

LEVELS = ("easy", "medium", "hard")
REPORT_DAYS = 14

LINES = (60, 300)
FILES = (3, 10)
ROUNDS = (0, 1)
FINDINGS = (0, 2)
EASY_UP_TO, MEDIUM_UP_TO = 1, 4

TABLE_HEAD = ("| difficulty lane | model | kind | scored | agree | harder | easier "
              "| bias |")


def _points(value: int, bounds: Tuple[int, int]) -> int:
    return 0 if value <= bounds[0] else 1 if value <= bounds[1] else 2


def settle(additions: int, deletions: int, files: int, rounds: int, findings: int,
           bad_work: bool = False) -> Tuple[str, str]:
    """(level, one-line reason) from the merged work alone. It takes no
    perceived difficulty: the scorer must not see the brief's guess."""
    lines = max(0, int(additions)) + max(0, int(deletions))
    files, rounds, findings = max(0, int(files)), max(0, int(rounds)), max(0, int(findings))
    score = (_points(lines, LINES) + _points(files, FILES) + _points(rounds, ROUNDS)
             + (2 if bad_work else _points(findings, FINDINGS)))
    level = ("easy" if score <= EASY_UP_TO else "medium" if score <= MEDIUM_UP_TO
             else "hard")
    why = "%d changed lines in %d files, %d fix rounds sent back, %d review findings%s" % (
        lines, files, rounds, findings, " (bad work)" if bad_work else "")
    return level, why


def perceived(body: Any) -> str:
    """The ticket's Difficulty: line, or "" when it has none readable."""
    from . import tickets
    try:
        found = str(tickets.parse(str(body or ""))[0].get("Difficulty", "")).strip().lower()
    except ValueError:
        return ""
    return found if found in LEVELS else ""


def report(records: Mapping[str, Mapping[str, Any]], since: float
           ) -> Dict[Tuple[str, str, str], Dict[str, Any]]:
    """(lane, model, kind) -> scored, agree, harder, easier and bias, over
    the merged records since `since` that carry both difficulties."""
    rows: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for record in records.values():
        guess, got = record.get("perceived"), record.get("settled")
        if (guess not in LEVELS or got not in LEVELS or not record.get("merged")
                or float(record.get("at") or 0) < since):
            continue
        key = (str(record.get("lane")), str(record.get("model")), str(record.get("kind") or "-"))
        row = rows.setdefault(key, {"scored": 0, "agree": 0, "harder": 0, "easier": 0,
                                    "delta": 0})
        delta = LEVELS.index(got) - LEVELS.index(guess)
        row["scored"] += 1
        row["delta"] += delta
        row["agree" if delta == 0 else "harder" if delta > 0 else "easier"] += 1
    for row in rows.values():
        row["bias"] = row["delta"] / row["scored"]
    return rows


def markdown(records: Mapping[str, Mapping[str, Any]], now: float,
             days: Optional[int] = None) -> List[str]:
    """The guessed-against-settled table for the lane status comment."""
    days = REPORT_DAYS if days is None else days
    rows = report(records, now - days * 86400)
    lines = ["", "Difficulty: the brief's guess against a blind settled score (from the "
             "merged diff, fix rounds and review findings), the last %d days. A positive "
             "bias means the work was harder than the brief said." % days, "",
             TABLE_HEAD, "|---|---|---|---|---|---|---|---|"]
    if not rows:
        lines.append("| - | - | - | 0 | 0 | 0 | 0 | 0.00 |")
    for (lane, model, kind), row in sorted(rows.items()):
        lines.append("| %s | %s | %s | %d | %d | %d | %d | %+.2f |" % (
            lane, model or "-", kind, row["scored"], row["agree"], row["harder"],
            row["easier"], row["bias"]))
    return lines
