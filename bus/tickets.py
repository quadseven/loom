"""Ticket issues as local briefs: names, header lines, rendering and trust.

The patterns here mirror the checks in lanes/run_lane.sh, so a brief that
passes them is one the unchanged runner accepts. docs/BRAIN.md ("Ticket
issues", "Trust") is the contract.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

# The runner's own patterns (lanes/run_lane.sh).
MODEL_RE = re.compile(r"[A-Za-z0-9._:][A-Za-z0-9._:/@-]*")
BASE_RE = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._/-]*")
REPO_RE = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")
DIFFICULTIES = ("easy", "medium", "hard")

HEADER_RE = re.compile(r"(Difficulty|Model|Base):[ \t]*(.*)")
# The runner reads the first line starting with one of these, so a body line
# that looks like a header must not follow the real header lines.
FIELD_LINE_RE = re.compile(r"(Repo|Branch|Lane|Issue|Model|Base):")
SLUG_MAX = 40


def slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(title).lower()).strip("-")
    return s[:SLUG_MAX].strip("-")


def branch_for(number: int, title: str, attempt: int = 1) -> str:
    name = "loom/%d" % int(number)
    s = slug(title)
    if s:
        name += "-" + s
    if attempt > 1:
        name += "-r%d" % attempt
    return name


def ticket_name(repo: str, number: int, attempt: int = 1) -> str:
    if not REPO_RE.fullmatch(repo):
        raise ValueError("expected OWNER/NAME, got %r" % repo)
    name = "%s-%d" % (repo.replace("/", "."), int(number))
    if attempt > 1:
        name += "-r%d" % attempt
    return name


def parse(body: Optional[str]) -> Tuple[Dict[str, str], str]:
    """Split the leading Difficulty:, Model: and Base: lines from the rest.

    Returns (headers in the order they appeared, the remaining body). Raises
    ValueError for a value the runner would refuse, a repeated header, or a
    later body line the runner would mistake for a header.
    """
    lines = (body or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    headers: Dict[str, str] = {}
    i = 0
    while i < len(lines):
        m = HEADER_RE.fullmatch(lines[i].rstrip())
        if not m:
            break
        key, value = m.group(1), m.group(2).strip()
        if key in headers:
            raise ValueError("%s: given twice" % key)
        if key == "Difficulty" and value not in DIFFICULTIES:
            raise ValueError("Difficulty: must be one of %s" % ", ".join(DIFFICULTIES))
        if key == "Model" and not MODEL_RE.fullmatch(value):
            raise ValueError("Model: %r is not a model id" % value)
        if key == "Base" and not BASE_RE.fullmatch(value):
            raise ValueError("Base: %r is not a branch name" % value)
        headers[key] = value
        i += 1
    rest = lines[i:]
    while rest and not rest[0].strip():
        rest.pop(0)
    for line in rest:
        if FIELD_LINE_RE.match(line):
            raise ValueError("body line %r looks like a brief header"
                             % line.split(":")[0])
    return headers, "\n".join(rest).rstrip("\n")


def render_brief(repo: str, issue: Dict[str, Any], lane: str, attempt: int = 1) -> str:
    number = int(issue["number"])
    title = " ".join(str(issue.get("title") or "").split())
    headers, body = parse(issue.get("body"))
    out: List[str] = [
        "# Brief: %s" % ticket_name(repo, number, attempt),
        "",
        "Repo: %s" % repo,
        "Branch: %s" % branch_for(number, title, attempt),
        "Lane: %s" % lane,
        "Issue: %s#%d" % (repo, number),
    ]
    out += ["%s: %s" % (k, v) for k, v in headers.items()]
    out += ["", "# T%d: %s" % (number, title), ""]
    text = "\n".join(out) + "\n"
    if body:
        text += body + "\n"
    return text


def _login(value: Any) -> str:
    return str((value or {}).get("login") or "").lower() if isinstance(value, dict) else ""


def issue_owner(issue: Dict[str, Any]) -> str:
    """The repo owner an issue belongs to, from its API or web URL."""
    for key, pattern in (("repository_url", r"/repos/([^/]+)/[^/]+/?$"),
                         ("html_url", r"^https://[^/]+/([^/]+)/[^/]+/(?:issues|pull)/\d+$")):
        m = re.search(pattern, str(issue.get(key) or ""))
        if m:
            return m.group(1)
    return ""


def _latest_label_actor(events: Iterable[Dict[str, Any]], match) -> Optional[str]:
    actor: Optional[str] = None
    for ev in events:
        if not isinstance(ev, dict) or ev.get("event") != "labeled":
            continue
        name = str((ev.get("label") or {}).get("name") or "")
        if match(name):
            actor = _login(ev.get("actor"))
    return actor


def trusted(issue: Dict[str, Any], events: List[Dict[str, Any]],
            editor_login: Optional[str], authors: Iterable[str],
            owners: Iterable[str]) -> Tuple[bool, str]:
    """Every rule in docs/BRAIN.md "Trust"; the reason says which one failed."""
    allowed = {a.lower() for a in authors}
    allowed_owners = {o.lower() for o in owners}
    author = _login(issue.get("user"))
    if not author or author not in allowed:
        return False, "issue author %s is not in LOOM_BRIEF_AUTHORS" % (author or "unknown")
    briefed = _latest_label_actor(events, lambda n: n == "loom:briefed")
    if briefed is None:
        return False, "no loom:briefed labelled event found"
    if briefed not in allowed:
        return False, "loom:briefed was applied by %s, not in LOOM_BRIEF_AUTHORS" % (briefed or "unknown")
    lane = _latest_label_actor(events, lambda n: n.startswith("lane:"))
    if lane is None:
        return False, "no lane: labelled event found"
    if lane not in allowed:
        return False, "the lane label was applied by %s, not in LOOM_BRIEF_AUTHORS" % (lane or "unknown")
    if editor_login is not None and editor_login.lower() not in allowed:
        return False, "body last edited by %s, not in LOOM_BRIEF_AUTHORS" % editor_login
    owner = issue_owner(issue).lower()
    if not owner or owner not in allowed_owners:
        return False, "repo owner %s is not in LOOM_REPO_OWNERS" % (owner or "unknown")
    return True, "ok"
