"""The proof record a ticket needs before it is verified and closed as completed.

The brain writes the record as JSON and passes it to `move --to verified`. The
record is checked, then posted on the ticket as a `loom:proof` comment that
carries the record and a hash of the ticket's acceptance criteria, so a
completed closure can read it back and refuse when the criteria changed.
The record also says who made it: verified_by is "brain" or, for a proof the
station applied from a lane's verify run, "<lane>/<model>" (bus/audit.py).
A station-applied proof also carries the facts the station read from GitHub
itself (station_facts), so the close evidence does not rest on the lane's prose.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

PROOF_MARKER = "<!-- loom:proof v1 -->"
KINDS = ("runtime", "docs")
# Runtime work is proven on the deployed system; documentation-only work by
# the revision of the default branch that carries it.
REQUIRED = {
    "runtime": ("kind", "revision", "environment", "artifact", "observed", "sources",
                "criteria", "measure"),
    "docs": ("kind", "revision", "observed", "sources", "criteria"),
}
MEASURE_FIELDS = ("name", "baseline", "target", "after")
_CRITERIA_HEADING = "## Acceptance criteria"
_BOX = re.compile(r"^\s*[-*] \[([ xX])\]\s+(.*?)\s*$")
_SHA = re.compile(r"[0-9a-f]{7,40}")
_RECORD = re.compile(r"^```json\n(.*?)\n```$", re.S | re.M)
FACTS_KEY = "station_facts"
# Evidence prose that says every check passed: "all checks passed", "every
# check passed", "all CI succeeded", "checks green".
_ALL_PASSED = re.compile(
    r"\b(?:all|every)\s+(?:the\s+)?(?:checks?|CI)\s+(?:(?:are|were|is|was|have)\s+)?"
    r"(?:passed|green|succeeded)\b|\bchecks?\s+(?:are\s+|were\s+)?green\b", re.I)


def criteria(body: str) -> List[Tuple[bool, str]]:
    """(checked, text) for each checkbox under the ticket's acceptance criteria."""
    found: List[Tuple[bool, str]] = []
    inside = False
    for line in (body or "").replace("\r", "").split("\n"):
        if line.startswith("## "):
            inside = line.strip() == _CRITERIA_HEADING
            continue
        match = _BOX.match(line) if inside else None
        if match:
            found.append((match.group(1) != " ", match.group(2)))
    return found


def tick(body: str) -> str:
    """The body with every checkbox under the acceptance criteria checked."""
    out: List[str] = []
    inside = False
    for line in (body or "").split("\n"):
        if line.startswith("## "):
            inside = line.strip() == _CRITERIA_HEADING
        match = _BOX.match(line.rstrip("\r")) if inside else None
        out.append(re.sub(r"\[ \]", "[x]", line, count=1) if match and match.group(1) == " " else line)
    return "\n".join(out)


def contract(body: str) -> str:
    """Hash of the criteria texts, not their check marks: ticking a box keeps
    the contract, rewording, adding or removing a criterion changes it."""
    texts = "\n".join(text for _, text in criteria(body))
    return hashlib.sha256(texts.encode("utf-8")).hexdigest()


def check_criteria(body: str) -> None:
    """Refuse a ticket whose acceptance criteria are missing or not all checked."""
    found = criteria(body)
    if not found:
        raise ValueError("the ticket has no acceptance criteria checkboxes")
    open_ = [text for checked, text in found if not checked]
    if open_:
        raise ValueError("unchecked acceptance criterion: %s" % open_[0])


def _text(record: Dict[str, Any], key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("proof %s must be non-empty text" % key)
    return value.strip()


def _time(value: Any, what: str) -> datetime:
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise ValueError("proof %s must be a UTC time like 2030-01-01T00:00:00Z" % what) from None


def _check_observed(record: Dict[str, Any]) -> None:
    observed = record.get("observed")
    if not isinstance(observed, dict):
        raise ValueError("proof observed must have start and end")
    if _time(observed.get("start"), "observed start") > _time(observed.get("end"), "observed end"):
        raise ValueError("proof observed window ends before it starts")


def _check_record_criteria(record: Dict[str, Any], body: str) -> None:
    results = record.get("criteria")
    if not isinstance(results, list) or not all(isinstance(x, dict) for x in results):
        raise ValueError("proof criteria must be a list of results")
    wanted = [text for _, text in criteria(body)]
    given = [str(x.get("criterion", "")).strip() for x in results]
    if given != wanted:
        raise ValueError("proof criteria must be the ticket's acceptance criteria, in order")
    for item in results:
        if item.get("result") != "pass":
            raise ValueError("proof criterion did not pass: %s" % item.get("criterion"))
        if not str(item.get("evidence", "")).strip():
            raise ValueError("proof criterion has no evidence: %s" % item.get("criterion"))


def check(record: Any, body: str) -> Dict[str, Any]:
    """Return the record if it proves the ticket with this body, else raise."""
    if not isinstance(record, dict):
        raise ValueError("proof must be a JSON object")
    kind = record.get("kind")
    if kind not in KINDS:
        raise ValueError("proof kind is runtime or docs")
    missing = [key for key in REQUIRED[kind] if key not in record]
    if missing:
        raise ValueError("%s proof is missing %s" % (kind, ", ".join(missing)))
    if not _SHA.fullmatch(_text(record, "revision")):
        raise ValueError("proof revision must be a commit SHA")
    if kind == "runtime":
        _text(record, "environment")
        _text(record, "artifact")
        measure = record.get("measure")
        if not isinstance(measure, dict):
            raise ValueError("proof measure must have %s" % ", ".join(MEASURE_FIELDS))
        for key in MEASURE_FIELDS:
            _text(measure, key)
    _check_observed(record)
    sources = record.get("sources")
    if not isinstance(sources, list) or not sources or not all(
            isinstance(x, str) and x.strip() for x in sources):
        raise ValueError("proof sources must list at least one reference")
    if "verified_by" in record:
        _text(record, "verified_by")
    check_criteria(body)
    _check_record_criteria(record, body)
    return record


def _line(value: Any) -> str:
    return " ".join(str(value).split())


def render(record: Dict[str, Any], body: str, facts: Sequence[str] = ()) -> str:
    """The proof comment: a readable summary, the facts the station checked
    itself when it gives any, then the record with the acceptance contract it
    was checked against. Only the station's facts are stored as station_facts:
    a record that carries its own is not believed."""
    lines = [PROOF_MARKER, "Proof (%s) at revision %s" % (record["kind"], record["revision"])]
    if "verified_by" in record:
        # In code: a model name such as provider/@org/model is no mention.
        lines.append("Verified by: `%s`" % _line(record["verified_by"]).replace("`", "'"))
    if record["kind"] == "runtime":
        lines.append("Environment: %s, artifact: %s" % (
            _line(record["environment"]), _line(record["artifact"])))
        m = record["measure"]
        lines.append("Measure %s: baseline %s, target %s, after %s" % tuple(
            _line(m[key]) for key in MEASURE_FIELDS))
    lines.append("Observed %s to %s" % (record["observed"]["start"], record["observed"]["end"]))
    facts = [_line(x) for x in facts]
    if facts:
        lines += ["", "Checked by the station:"] + ["- %s" % x for x in facts]
    lines += ["", "Criteria:"]
    lines += ["- pass: %s (%s)" % (_line(x["criterion"]), _line(x["evidence"]))
              for x in record["criteria"]]
    lines += ["", "Sources:"] + ["- %s" % _line(x) for x in record["sources"]]
    stored = dict(record, contract=contract(body))
    stored.pop(FACTS_KEY, None)
    if facts:
        stored[FACTS_KEY] = facts
    lines += ["", "```json", json.dumps(stored, indent=2, sort_keys=True), "```"]
    return "\n".join(lines) + "\n"


def contradicts(record: Dict[str, Any], failing_checks: Mapping[str, str]) -> str:
    """Why the record's evidence contradicts the checks the station read, or
    "". failing_checks maps each check run whose conclusion is not success or
    skipped to that conclusion; evidence that says every check passed while
    one of them exists is not believed."""
    if not failing_checks:
        return ""
    results = record.get("criteria")
    claims = any(isinstance(item, dict) and _ALL_PASSED.search(str(item.get("evidence", "")))
                 for item in results) if isinstance(results, list) else False
    if not claims:
        return ""
    name = sorted(failing_checks)[0]
    return "evidence says every check passed but %s concluded %s" % (
        _line(name), _line(failing_checks[name]))


def read(comment: str) -> Optional[Dict[str, Any]]:
    """The record a proof comment carries, or None."""
    if not comment.startswith(PROOF_MARKER):
        return None
    # The record is the last block: text above it cannot stand in for it.
    blocks = _RECORD.findall(comment)
    try:
        data = json.loads(blocks[-1]) if blocks else None
    except ValueError:
        return None
    return data if isinstance(data, dict) else None
