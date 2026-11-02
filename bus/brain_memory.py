"""Brain memory, the station's and the brains' side (docs/decisions/0002,
item 3; the dashboard keeps the tables, dashboard/brain_memory.py).

After each brain tick on this machine the station posts one brain_ticks row,
built from state/brain.json (holder, lane, model, start, end, outcome), the
tick's step record (the notes on the lock commit) and the journals the tick
posted on the goal homes. Its commitments are the defer markers the tick
wrote, each open until a done marker for the same work, and the done markers
any brain wrote since the last row, which close them. It also loads the
rules digest: the Rules section of docs/MUSE.md keyed by the commit that last
changed it, and each goal home's rules keyed by their comment (or by a digest
of the body's Rules section).

Every brain reads the handoff packet the dashboard serves back, rendered by
`python3 -m bus.brain_memory handoff`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit

from . import handoff
from .github import _OPENER

# The lock step comes before the agent starts (brain.sh refreshes its
# checkout in between), so a tick's step record may start this long before
# the start state/brain.json records. Its journals cannot: only the agent
# posts them.
WINDOW = 15 * 60
# Clock slack between this machine and GitHub's comment times: a journal
# this close to the agent's start or end belongs to its tick.
GRACE = 60
# The dashboard's caps on one body (dashboard/brain_memory.py).
LINE_MAX = 500
LIST_MAX = 50
COMMITMENTS_MAX = 200
RULE_TEXT_MAX = 20000
# brain.sh's default holder, for a brain.json written before it named one.
DEFAULT_HOLDER = "lane-machine"
MODEL_MISSING = "not recorded"
HANDOFF_PATH = "/api/brain/handoff"
TICKS_PATH = "/api/brain/ticks"
RULES_PATH = "/api/brain/rules"
COMMITMENTS_PATH = "/api/brain/commitments"
FETCH_TIMEOUT = 30
FETCH_MAX = 4 * 1024 * 1024
# A read that failed on the wire is tried again after these pauses (seconds).
FETCH_RETRY_DELAYS = (2.0, 6.0)
# Statuses that warrant a retry (matching GitHub client).
FETCH_RETRY_STATUSES = (502, 503, 504)
# The rendered packet goes into a brain's prompt.
PACKET_MAX = 60000
_FIELD = re.compile(r"^- (Shipped|Briefed|Waiting): *(.*?)\s*$", re.M)
_RULES = re.compile(r"^##[ \t]+Rules\b")
_RULES_COMMENT = re.compile(r"^#{1,6}[ \t]*(?:Loom[ \t]+)?rules\b", re.I)
_HOLDER = re.compile(r"[A-Za-z0-9._-]{1,64}")


def _utc(at: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))


def _epoch(stamp: Any) -> Optional[float]:
    parsed = handoff._parse(str(stamp or ""))
    return parsed.timestamp() if parsed else None


def _line(text: Any) -> str:
    line = " ".join(str(text).split())
    return line if len(line) <= LINE_MAX else line[:LINE_MAX - 3] + "..."


def _name(value: Any, default: str) -> str:
    return value if isinstance(value, str) and value.strip() and len(value) <= 200 \
        and "\n" not in value else default


def holder_of(last: Dict[str, Any]) -> str:
    """The holder state/brain.json names, else brain.sh's default."""
    holder = last.get("holder")
    return holder if isinstance(holder, str) and _HOLDER.fullmatch(holder) else DEFAULT_HOLDER


def _notes(record: Optional[Dict[str, Any]], holder: str, started: float,
           ended: float) -> List[str]:
    """The step notes of this tick: the record's, when its holder ran it and
    it started within WINDOW before the agent did."""
    if not isinstance(record, dict) or record.get("holder") != holder:
        return []
    began = _epoch(record.get("started_at"))
    if began is None or not started - WINDOW <= began <= ended:
        return []
    return [_line("%s: %s" % (n.get("step"), n.get("text")))
            for n in record.get("notes") or [] if isinstance(n, dict) and n.get("text")]


def _values(found: Sequence[Dict[str, Any]], name: str) -> List[str]:
    """The items of every `- <name>: a, b` line in the journals, "none" left out."""
    out = []
    for comment in found:
        for m in _FIELD.finditer(str(comment.get("body") or "")):
            if m.group(1) == name and m.group(2).strip().lower() != "none":
                out += [" ".join(v.split()) for v in m.group(2).split(",") if v.strip()]
    return out


def _what(mark: handoff.Mark) -> str:
    return _line(mark.stage + (": " + mark.note if mark.note else ""))


def _commitment(mark: handoff.Mark, state: str, due: Optional[str] = None) -> Dict[str, Any]:
    return {"item": handoff.item_ref(mark.item), "stage": mark.stage, "key": mark.key,
            "what": _what(mark), "due": due, "state": state}


def _in_order(found: Sequence[Dict[str, Any]]) -> List[handoff.Mark]:
    return handoff.marks(sorted(found, key=lambda c: str(c.get("at"))))


def commitments(found: Sequence[Dict[str, Any]], holder: str, opened_since: float,
                closed_since: float, until: float) -> List[Dict[str, Any]]:
    """In the order written: each defer marker the holder wrote from
    opened_since, open and due QUIET after it was written (when another
    brain may take it), and each done marker any brain wrote from
    closed_since, which closes the commitment for the same stage, item and
    brief key. Over COMMITMENTS_MAX, the oldest done markers are left out
    first: the close pass (closings) sends done markers between ticks."""
    out = []
    for mark in _in_order(found):
        at = _epoch(mark.at)
        if at is None or at > until:
            continue
        if mark.kind == "defer" and mark.holder == holder and at >= opened_since:
            out.append(_commitment(mark, "open", _utc(at + handoff.QUIET.total_seconds())))
        elif mark.kind == "done" and at >= closed_since:
            out.append(_commitment(mark, "done"))
    extra = len(out) - COMMITMENTS_MAX
    if extra > 0:
        dropped = set([i for i, c in enumerate(out) if c["state"] == "done"][:extra])
        out = [c for i, c in enumerate(out) if i not in dropped]
    return out[:COMMITMENTS_MAX]


def closings(found: Sequence[Dict[str, Any]], since: float) -> List[Tuple[float, Dict[str, Any]]]:
    """(when written, commitment) for each done marker any brain wrote from
    since, oldest first, at most COMMITMENTS_MAX: the close pass sends them
    between ticks, so work another brain finished closes without waiting for
    this machine's next tick."""
    out = []
    for mark in _in_order(found):
        at = _epoch(mark.at)
        if mark.kind == "done" and at is not None and at >= since:
            out.append((at, _commitment(mark, "done")))
    return out[:COMMITMENTS_MAX]


def tick_body(last: Dict[str, Any], record: Optional[Dict[str, Any]],
              found: Sequence[Dict[str, Any]], holder: str,
              since: Optional[float] = None) -> Dict[str, Any]:
    """The brain_ticks row for the tick state/brain.json records (`last`,
    with numeric started and at). `record` is the step record on the lock
    commit, `found` the trusted journals on every goal home
    (handoff.journals), and `since` where the done markers already sent end:
    those from then on close commitments, and every one from the tick's own
    start, which a close pass during the tick sent before its defers were
    stored."""
    started, ended = float(last["started"]), float(last["at"])
    begin, until = started - GRACE, ended + GRACE
    mine = _tick_journals(found, holder, begin, until)
    return {"holder": holder, "lane": _name(last.get("lane"), "unknown"),
            "model": _name(last.get("model"), MODEL_MISSING),
            "started": _utc(started), "ended": _utc(ended), "outcome": last.get("outcome"),
            "decided": (_notes(record, holder, started, ended)
                        + [_line("shipped " + v) for v in _values(mine, "Shipped")])[:LIST_MAX],
            "left_open": _left_open(mine, holder)[:LIST_MAX],
            "handed_off": [_line(v) for v in _values(mine, "Briefed")][:LIST_MAX],
            "commitments": commitments(found, holder, begin,
                                       begin if since is None else min(since, begin), until)}


def _tick_journals(found: Sequence[Dict[str, Any]], holder: str, begin: float,
                   until: float) -> List[Dict[str, Any]]:
    """The journals the holder posted between begin and until."""
    return [j for j in found if handoff.journal_holder(j) == holder
            and begin <= (_epoch(j.get("at")) or 0) <= until]


def _left_open(mine: Sequence[Dict[str, Any]], holder: str) -> List[str]:
    """Each defer marker the holder wrote, then each item it was waiting on."""
    defers = [m for m in handoff.marks(mine) if m.kind == "defer" and m.holder == holder]
    return ([_line("%s %s" % (m.stage, handoff.item_ref(m.item)) + (": " + m.note if m.note else ""))
             for m in defers] + [_line("waiting on " + v) for v in _values(mine, "Waiting")])


def rules_section(doc: str) -> str:
    """The text under a `## Rules` heading, to the next heading of level one
    or two; "" when there is none."""
    lines = doc.splitlines()
    start = next((i for i, line in enumerate(lines) if _RULES.match(line)), None)
    if start is None:
        return ""
    out = []
    for line in lines[start + 1:]:
        if line.startswith(("# ", "## ")):
            break
        out.append(line.rstrip())
    return "\n".join(out).strip()


def _login(item: Dict[str, Any]) -> str:
    return str((item.get("user") or {}).get("login") or "").lower()


def goal_rules(repo: str, issue: Dict[str, Any], comments: Sequence[Dict[str, Any]],
               authors: Sequence[str]) -> Optional[Dict[str, str]]:
    """One goal home's rules: its newest rules comment by an allowed login
    (one that opens with a "Rules" or "Loom rules" heading), keyed by the
    comment, else the Rules section of a body an allowed login wrote, keyed
    by its digest. None when it has neither."""
    allowed = {a.lower() for a in authors}
    source = "%s#%d" % (repo, int(issue["number"]))
    found = [c for c in comments if isinstance(c, dict) and _login(c) in allowed
             and _RULES_COMMENT.match(str(c.get("body") or "").lstrip())]
    if found:
        newest = max(found, key=lambda c: (str(c.get("created_at") or ""), int(c.get("id") or 0)))
        return {"source": source, "source_ref": "comment %d" % int(newest["id"]),
                "text": str(newest["body"]).strip()[:RULE_TEXT_MAX]}
    text = rules_section(str(issue.get("body") or "")) if _login(issue) in allowed else ""
    if not text:
        return None
    return {"source": source,
            "source_ref": "body sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:12],
            "text": text[:RULE_TEXT_MAX]}


def digest(rule: Dict[str, str]) -> str:
    """What the station keeps of a loaded rule, to post it again only when it
    changed."""
    return hashlib.sha256(json.dumps([rule["source_ref"], rule["text"]]).encode("utf-8")).hexdigest()


def dashboard_url(transcript_url: str, path: str) -> str:
    """LOOM_TRANSCRIPT_URL with its /api/transcripts path replaced by path;
    the query string is kept."""
    parts = urlsplit(transcript_url)
    return urlunsplit(parts._replace(path=parts.path.removesuffix("/api/transcripts") + path))


def fetch_json(url: str, token: str, what: str = "handoff") -> Dict[str, Any]:
    """GET one of the dashboard's bearer-token reads (the handoff packet, the
    usage snapshot) as a JSON object. Errors never carry the URL or the token.
    Retries on connection failures and 5xx server errors."""
    if not url.startswith("https://"):
        raise OSError("the %s URL is not https" % what)
    req = urllib.request.Request(  # noqa: S310 - https only, checked above
        url, method="GET", headers={"User-Agent": "loom-brain", "Authorization": "Bearer " + token})
    for attempt in range(len(FETCH_RETRY_DELAYS) + 1):
        try:
            with _OPENER.open(req, timeout=FETCH_TIMEOUT) as resp:  # noqa: S310
                status, raw = resp.status, resp.read(FETCH_MAX)
        except urllib.error.HTTPError as err:
            status, raw = err.code, b""
            err.close()
        except (urllib.error.URLError, OSError):
            if attempt == len(FETCH_RETRY_DELAYS):
                raise OSError("the %s read failed to connect" % what) from None
            time.sleep(FETCH_RETRY_DELAYS[attempt])
            continue
        if status in FETCH_RETRY_STATUSES and attempt < len(FETCH_RETRY_DELAYS):
            time.sleep(FETCH_RETRY_DELAYS[attempt])
            continue
        break
    if status != 200:
        raise OSError("the %s read answered %d" % (what, status))
    try:
        packet = json.loads(raw)
    except ValueError:
        raise OSError("the %s packet is not JSON" % what) from None
    if not isinstance(packet, dict):
        raise OSError("the %s packet is not an object" % what)
    return packet


def fetch_handoff(url: str, token: str) -> Dict[str, Any]:
    """GET the packet. Errors never carry the URL or the token."""
    return fetch_json(url, token, "handoff")


def _joined(values: Any) -> str:
    if not isinstance(values, list):
        return "none"
    return "; ".join(str(v) for v in values) or "none"


def _rows(packet: Dict[str, Any], key: str) -> List[Dict[str, Any]]:
    """The packet's list under key, objects only: a malformed packet drops
    the rows it cannot read instead of failing the tick."""
    rows = packet.get(key)
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


# Every field of a tick or commitment is a record, not instructions, so each
# renders as one bounded line: a field cannot open a heading of its own, such
# as a forged rules digest. Only the rules text keeps its lines.
def _tick_line(t: Dict[str, Any]) -> str:
    return _line("- %s %s on %s, model %s: %s. Decided: %s. Left open: %s. Handed off: %s." % (
        t.get("started"), t.get("holder"), t.get("lane"), t.get("model"), t.get("outcome"),
        _joined(t.get("decided")), _joined(t.get("left_open")), _joined(t.get("handed_off"))))


def _commitment_line(c: Dict[str, Any]) -> str:
    return _line("- %s %s from %s's tick %s, due %s: %s" % (
        c.get("stage"), c.get("item"), c.get("holder"), c.get("tick_id"),
        c.get("due") or "none", c.get("what")))


def render(packet: Dict[str, Any]) -> str:
    """The packet as text for a brain's prompt: the last ticks, the open
    commitments and the rules digest with the commit or comment of each."""
    commits = _rows(packet, "commitments")
    rules = _rows(packet, "rules")
    out = ["Handoff packet from the dashboard (docs/decisions/0002, item 3).", "",
           "## Last ticks"]
    out += [_tick_line(t) for t in _rows(packet, "ticks")] or ["- none"]
    out += ["", "## Open commitments (%d)" % len(commits)]
    out += [_commitment_line(c) for c in commits] or ["- none"]
    out += ["", "## Rules digest"]
    for rule in rules:
        out += ["", _line("### %s at %s" % (rule.get("source"), rule.get("source_ref"))),
                str(rule.get("text") or "")]
    if not rules:
        out.append("- none")
    text = "\n".join(out)
    return text if len(text) <= PACKET_MAX else text[:PACKET_MAX] + "\n[cut]"


def _handoff_command() -> int:
    url = os.environ.get("LOOM_TRANSCRIPT_URL", "").strip()
    ssm = os.environ.get("LOOM_TRANSCRIPT_TOKEN_SSM", "").strip()
    region = os.environ.get("LOOM_AWS_REGION", "").strip() or "us-east-1"
    if not (url and ssm):
        print("the handoff packet needs LOOM_TRANSCRIPT_URL and LOOM_TRANSCRIPT_TOKEN_SSM",
              file=sys.stderr)
        return 1
    from . import station  # station imports this module
    try:
        packet = fetch_handoff(dashboard_url(url, HANDOFF_PATH), station.read_ssm(ssm, region))
    # The fetch's errors never carry the URL or the token; a hung token read
    # is named by its type only, as the station logs it.
    except OSError as err:
        print("could not read the handoff packet: %s" % err, file=sys.stderr)
        return 1
    except subprocess.SubprocessError as err:
        print("could not read the handoff token (%s)" % type(err).__name__, file=sys.stderr)
        return 1
    print(render(packet))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m bus.brain_memory")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("handoff", help="print the handoff packet from the dashboard")
    p.parse_args(argv)
    return _handoff_command()


if __name__ == "__main__":
    sys.exit(main())
