"""The Risk line and tier label of a lane pull request (loom#2427).

Review could not sort pull requests by risk from the pull request page:
bus/merge_tier.py computes a tier and keeps it inside the merge decision, and
nothing states whether a change can be walked back. The runner now writes one
line in `## Intent` and a `tier:<name>` label:

    Risk: <tier>, <one-way|two-way> door, blast radius <one word>

The tier is computed with merge_tier.classify. The lane supplies the door and
the blast radius in a `## Risk` section of .loom/summary.md (`Door: two-way`,
`Blast radius: repo`), and a door written by a model alone is only a claim, so
the runner forces one-way when the diff touches a migration, deletes a tracked
data file, or the pull request lists an operator step. A lane that says
nothing reads `door unstated`, never two-way. Tiers keep deciding what Loom may
merge; this module only reports.
"""
from __future__ import annotations

import argparse
import re
import sys
from typing import Any, Dict, List, NamedTuple, Sequence

from . import change_class, merge_tier, operator_hold, pr_repair

ONE_WAY, TWO_WAY = "one-way", "two-way"
UNSTATED = "unstated"
HEADING = "Risk"
LABEL_PREFIX = "tier:"

_MIGRATION_DIRS = {"migrations", "migration", "migrate", "alembic"}
_TEST_DIRS = {"test", "tests", "t", "spec", "__tests__", "fixtures"}
_DATA_EXTS = (".csv", ".tsv", ".json", ".jsonl", ".ndjson", ".parquet", ".sql", ".sqlite",
              ".sqlite3", ".db")
_DOOR = re.compile(r"^\s*door\s*:\s*(\S+)\s*$", re.I | re.M)
_BLAST = re.compile(r"^\s*blast radius\s*:\s*([A-Za-z][A-Za-z-]{0,23})\s*$", re.I | re.M)
GITLINK_MODE = "160000"


class Risk(NamedTuple):
    tier: str
    door: str     # ONE_WAY, TWO_WAY or UNSTATED
    blast: str


def entries(raw: str) -> List[Dict[str, Any]]:
    """`git diff --raw -M` as merge_tier's changed-file entries, with each
    file's status. A submodule gitlink carries a patch merge_tier reads as one."""
    found: List[Dict[str, Any]] = []
    for row in (raw or "").splitlines():
        head, _, paths = row.partition("\t")
        fields, names = head.split(), paths.split("\t")
        if len(fields) < 5 or not names[0]:
            continue
        entry: Dict[str, Any] = {"filename": names[-1], "status": fields[4][:1]}
        if len(names) > 1:
            entry["previous_filename"] = names[0]
        if GITLINK_MODE in (fields[0].lstrip(":"), fields[1]):
            entry["patch"] = "+Subproject commit %s" % ("0" * 40)
        found.append(entry)
    return found


def _is_migration(path: str) -> bool:
    parts = path.lower().split("/")
    named = "migration" in parts[-1] and parts[-1].endswith(".sql")
    return not (_TEST_DIRS & set(parts[:-1]) or change_class.is_docs(path)) and bool(
        _MIGRATION_DIRS & set(parts[:-1]) or named)


def _deletes_data(entry: Dict[str, Any]) -> bool:
    path = str(entry.get("filename") or "")
    return (entry.get("status") == "D" and path.lower().endswith(_DATA_EXTS)
            and not _TEST_DIRS & set(path.lower().split("/")[:-1]))


def _lists_step(summary: str, ticket: str) -> bool:
    """Whether the merge gate would read an operator step (bus/operator_hold.py)
    in the lane's words or the ticket's."""
    return bool(operator_hold.operator_steps(summary, ticket)
                or operator_hold.section_steps(ticket))


def _lane_door(section: str) -> str:
    said = _DOOR.search(section)
    word = said.group(1).lower() if said else ""
    return word if word in (ONE_WAY, TWO_WAY) else UNSTATED


def _ticket_words(brief: str) -> str:
    """The brief without the context the runner added to it: merge_tier reads
    a ticket's own words, and a linked pull request is not one."""
    return re.split(r"^## (?:Operator notes|Linked pull requests|Proof checks|Earlier attempt"
                    r"|Fix|Checks CI runs)[ \t]*$", brief or "", maxsplit=1, flags=re.M)[0]


def compute(repo: str, changed: Sequence[Dict[str, Any]], summary: str, brief: str,
            sensitive: bool) -> Risk:
    """The pull request's Risk. The tier is merge_tier's; the door is the
    lane's word unless the change itself forces one-way."""
    ticket = _ticket_words(brief)
    tier = merge_tier.classify(repo, changed, ticket, sensitive)[0]
    section = pr_repair._brief_section(summary or "", HEADING)
    forced = (any(_is_migration(str(item.get(name) or "")) for item in changed
                  for name in ("filename", "previous_filename"))
              or any(_deletes_data(item) for item in changed) or _lists_step(summary, ticket))
    blast = _BLAST.search(section)
    return Risk(tier, ONE_WAY if forced else _lane_door(section),
                blast.group(1).lower() if blast else UNSTATED)


def line(risk: Risk) -> str:
    door = "door unstated" if risk.door == UNSTATED else "%s door" % risk.door
    return "Risk: %s, %s, blast radius %s" % (risk.tier, door, risk.blast)


def label(risk: Risk) -> str:
    return LABEL_PREFIX + risk.tier


def with_risk(intent: str, risk_text: str) -> str:
    """The Intent section with the Risk line under its Class line."""
    if not risk_text:
        return intent
    rows = (intent or "").split("\n")
    at = next((i for i, row in enumerate(rows) if row.startswith("Class: ")), None)
    if at is None:
        return intent
    return "\n".join(rows[:at + 1] + [risk_text] + rows[at + 1:])


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m bus.risk_line")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--class", dest="kind", required=True, help="the change class")
    parser.add_argument("--intent", required=True, help="file holding the Intent section")
    parser.add_argument("--raw", required=True, help="file holding git diff --raw -M")
    parser.add_argument("--summary", default="", help="file holding the lane's whole summary")
    parser.add_argument("--brief", default="", help="file holding the ticket brief")
    args = parser.parse_args(argv)
    summary = _read(args.summary) if args.summary else ""
    brief = _read(args.brief) if args.brief else ""
    risk = compute(args.repo, entries(_read(args.raw)), summary, brief, args.kind == "sensitive")
    sys.stdout.write(with_risk(_read(args.intent).rstrip("\n"), line(risk)) + "\n")
    sys.stdout.write(label(risk) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
