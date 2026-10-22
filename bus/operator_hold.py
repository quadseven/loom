"""When a lane pull request waits for the operator instead of merging.

On 2026-10-22 the brain merged a lane pull request on green checks and a
passing review. Its own body said "Leave VM resizing and the live data
migration to the operator", none of that had been done, and the merge's
deploy moved every workload of a service off the node that held its local
volumes: the service was down for about four hours. Nothing in the merge path
read the body, and the change class (logic) said nothing about deploys.

Two holds, read the same way by the factory before it lands a pull request
and by `bus.brain merge` before it merges one:

- Operator steps. The pull request's "## Operator steps before merge"
  section (the lane writes it, or the single word none) lists a step, or, as
  a backstop, the pull request's body or its ticket's body names one in words:
  "leave X to the operator", an operator task or prerequisite, a data
  migration, a cutover, a VM or disk resize.
- Placement. A file that merging deploys (a deploy workflow's on.push.paths
  filter matches it, the filters LOOM_SHIP_PATH_WORKFLOWS ships by) changes
  where a workload runs or what it mounts (ship_paths.placement_lines).

- Rewrite. The pull request deletes, or replaces most of, a doc the ticket
  does not name: a top-level README, AGENTS or CLAUDE file, or any Markdown
  file (2026-10-22: a lane replaced a deployment repo's root README with a
  write-up of its ticket, and the brain merged it).

- Measurement. On a health ticket (bus/brief_check.py), the pull request
  edits the health read itself or a bound (a threshold, floor, ceiling or
  cutoff): 2026-10-15, a health read began counting money the fund's holder
  carried so its finding vanished, and a sibling held refunds back to stay
  above the floor.

A held pull request is never merged by Loom. Its ticket goes to loom:human
with one exact ask, and the operator merges it by hand after the steps.

A ticket whose every acceptance criterion is a check on a live host or
network (host_only) is never given a lane: no pull request can meet it, and
a lane given one writes something else to have a diff.
"""
from __future__ import annotations

import base64
import re
from typing import Any, Callable, Dict, List, Optional, Sequence
from urllib.parse import quote

from . import brief_check, proof, ship_paths

SECTION = "Operator steps before merge"
_HEADING = re.compile(r"^#{2,3}\s*(.+?)\s*#*\s*$")
_RULE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
# The whole section must be one of these to say "no steps": "None, but
# first grow the VM" is a step.
_NONE = re.compile(r"(?:none|none stated|n/a|no operator steps?|nothing)\.?", re.I)
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?")
# Text that is not the lane's or the ticket's own words: HTML comments
# (markers) and the status section Loom keeps on a ticket.
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_STATUS = re.compile(r"<!-- loom:status-body v1 begin -->.*?<!-- loom:status-body v1 end -->",
                     re.S)
_FENCE = re.compile(r"^(```|~~~).*?^\1", re.S | re.M)
# The backstop: words that name a step a person takes before the change can
# be deployed. Each is read within one sentence.
BACKSTOP = (
    re.compile(r"\b(?:leave|leaves|left|leaving)\b[^.\n]{0,120}?\bto the operator\b", re.I),
    re.compile(r"\boperator[- ](?:task|step|prerequisite)s?\b", re.I),
    re.compile(r"\boperator prerequisites?\b|\bprerequisites? for the operator\b", re.I),
    re.compile(r"\b(?:live |manual )?data migration\b"
               r"|\bmigrat(?:e|es|ing)\b[^.\n]{0,40}\b(?:data|volumes?)\b", re.I),
    # A cutover of a live system, not a refactor's cut-over from old code.
    re.compile(r"\b(?:live|production|data|node|host|traffic|dns)\s+cut-?over\b"
               r"|\bcut-?over\b[^.\n]{0,60}\b(?:operator|by hand|manually|runbook)\b"
               r"|\b(?:operator|runbook)\b[^.\n]{0,60}\bcut-?over\b", re.I),
    re.compile(r"\b(?:resize|resizes|resizing|grow|grows|growing)\b[^.\n]{0,40}?"
               r"\b(?:VM|virtual machine|disk|volume)\b|\bVM resiz\w*", re.I),
)
STEP_MAX = 160
ASK_STEPS = 4
FILES_SHOWN = 5


def _prose(body: str) -> str:
    text = _STATUS.sub("", body or "")
    text = _FENCE.sub("", text)
    return _COMMENT.sub("", text)


def section_steps(body: str) -> Optional[List[str]]:
    """The steps the "## Operator steps before merge" section lists: None
    when the body has no such section, [] when it says none."""
    lines = _prose(body).replace("\r\n", "\n").split("\n")
    found: Optional[List[str]] = None
    for line in lines:
        if found is not None and _RULE.match(line):
            break  # the runner's footer follows the last section
        heading = _HEADING.match(line)
        if heading:
            if found is not None:
                break
            if heading.group(1).strip().lower() == SECTION.lower():
                found = []
            continue
        if found is not None and line.strip():
            found.append(_BULLET.sub("", line).strip())
    if found and _NONE.fullmatch(" ".join(found).strip()):
        return []
    return found


def _sentences(text: str) -> List[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def named_steps(body: str) -> List[str]:
    """Each sentence of the body that names an operator step in words."""
    out: List[str] = []
    # Headings name sections, never steps ("## Operator steps before merge").
    prose = "\n".join(line for line in _prose(body).split("\n")
                      if not _HEADING.match(line))
    for sentence in _sentences(prose):
        if any(p.search(sentence) for p in BACKSTOP):
            clean = _BULLET.sub("", sentence).strip()
            out.append(clean if len(clean) <= STEP_MAX else clean[:STEP_MAX - 3] + "...")
    return out


def operator_steps(pr_body: str, ticket_body: str = "") -> List[str]:
    """The operator steps the pull request or its ticket declares: the
    section's steps first, then every sentence the backstop matches, once."""
    steps = list(section_steps(pr_body) or [])
    steps += named_steps(pr_body) + named_steps(ticket_body)
    return list(dict.fromkeys(steps))


def pull_files(gh: Any, repo: str, number: int) -> List[Dict[str, Any]]:
    """Every file entry (name and patch) of a pull request, all pages."""
    files: List[Dict[str, Any]] = []
    for page in range(1, 31):
        data = gh.rest("GET", "/repos/%s/pulls/%d/files?per_page=100&page=%d"
                       % (repo, number, page)).data or []
        batch = [f for f in data if isinstance(f, dict)]
        files += batch
        if len(batch) < 100:
            break
    return files


def manifest_paths(paths: Sequence[str]) -> List[str]:
    return [p for p in paths if str(p).lower().endswith(ship_paths.MANIFEST_EXTS)]


def _manual_listed(gh: Any, repo: str, branch: str) -> List[str]:
    """The repo's .loom/manual-paths patterns. [] (nothing exempt, so every
    placement change stays held) when the repo has no such file, and when
    GitHub's answer holds no decodable content: an unread listing never
    exempts a file."""
    data = gh.rest("GET", "/repos/%s/contents/%s?ref=%s" % (
        repo, ship_paths.MANUAL_PATHS_FILE, quote(branch, safe="")), ok=(404,)).data
    content = data.get("content") if isinstance(data, dict) else None
    if not isinstance(content, str):
        return []
    try:
        raw = base64.b64decode(content)
    except ValueError:
        return []  # not base64: read as no listing, which holds more, never less
    return ship_paths.listed_manual(raw.decode("utf-8", "replace"))


# Paths that decide what a merge deploys: a pull request that changes them
# and a placement manifest could exempt the manifest from the hold, so the
# base branch's reading of them does not stand.
DEPLOY_RULES = (ship_paths.WORKFLOWS_DIR + "/", ship_paths.MANUAL_PATHS_FILE)


def _placement_manifests(entries: Sequence[Dict[str, Any]]) -> List[str]:
    """The manifests whose diff changes placement; one GitHub sends no patch
    for counts as changing it."""
    out = set()
    for entry in entries:
        name, patch = str(entry.get("filename") or ""), entry.get("patch")
        if manifest_paths([name]) and (not patch or ship_paths.placement_lines(str(patch))):
            out.add(name)
    return sorted(out)


def _touches_deploy_rules(entries: Sequence[Dict[str, Any]]) -> bool:
    names = [str(f.get("filename") or "") for f in entries]
    return any(n.startswith(DEPLOY_RULES[0]) or n == DEPLOY_RULES[1] for n in names)


def placement_files(gh: Any, repo: str, pr: Dict[str, Any],
                    files: Optional[List[Dict[str, Any]]] = None) -> List[str]:
    """The manifest files of the pull request that change placement and are
    not listed in the base branch's .loom/manual-paths. Fails closed: a
    manifest GitHub sends no patch for counts as changing placement, a file
    no deploy workflow covers still counts, and when the pull request also
    changes a workflow or .loom/manual-paths nothing is exempt."""
    entries = pull_files(gh, repo, int(pr.get("number") or 0)) if files is None else files
    moved = _placement_manifests(entries)
    if not moved or _touches_deploy_rules(entries):
        return moved
    branch = str((pr.get("base") or {}).get("ref") or "main")
    return ship_paths.unlisted(moved, _manual_listed(gh, repo, branch))


def _deployers(gh: Any, repo: str, pr: Dict[str, Any], moved: Sequence[str]) -> str:
    """Which push workflows on the base branch deploy the files, for the ask."""
    branch = str((pr.get("base") or {}).get("ref") or "main")
    texts = ship_paths.read_workflows(gh, repo, branch)
    if texts is None:
        return "the deploy workflows could not be read"
    found = ship_paths.covering(moved, ship_paths.push_filters(texts))
    return ("merging runs " + ", ".join(found)) if found else "no push workflow covers it"


# A criterion that only a live host or network can meet: a command run on a
# host, a reach test from a network, a port, a firewall or ACL rule, a check
# across the hosts. A criterion that names a test is code, whatever else it
# says.
HOST_CHECK_RE = re.compile(
    r"`[^`\n]+`\s+(?:on|from|against|at)\s+[\w.-]+"
    r"|\bfrom (?:an?|the|any)\b[^.\n]{0,60}\b(?:address|host|device|vlan|network|subnet"
    r"|machine)\b"
    r"|\bports?\s+\d+"
    r"|\b(?:acl|firewall|security group|iptables|nftables)\b[^.\n]{0,60}"
    r"\b(?:lets|allows|blocks|denies|permits|only)\b"
    r"|\b(?:on|across) (?:both|all|each|every) (?:hosts?|servers?|nodes?|machines?|devices?)\b",
    re.I)
_TEST_RE = re.compile(r"\btests?\b|\btest_\w+", re.I)
_LIVE_RE = re.compile(r"^\W*live\b", re.I)


def network_check(criterion: str) -> bool:
    """Whether a criterion is a HOST_CHECK_RE check on a host or network that
    names no test."""
    text = criterion.strip()
    return not _TEST_RE.search(text) and bool(HOST_CHECK_RE.search(text))


def host_check(criterion: str) -> bool:
    """Whether a criterion is met on a live system, not in a repo: a Live
    line, or a network_check."""
    text = criterion.strip()
    return network_check(text) or (not _TEST_RE.search(text) and bool(_LIVE_RE.match(text)))


def host_only(body: str) -> List[str]:
    """The ticket's criteria when every one is a host check; [] when any is
    something a pull request can meet, or it has none."""
    found = [text for _, text in proof.criteria(_prose(body))]
    return found if found and all(host_check(t) for t in found) else []


def host_only_ask(criteria: Sequence[str]) -> str:
    """The one exact ask for a ticket no lane can meet."""
    return ("Kind: decision\nEvery acceptance criterion of this ticket is a check on a live "
            "host or network (%s), so no pull request can meet it and no lane takes it. Do it "
            "by hand and close the ticket, or rewrite it with a criterion a repo change can "
            "meet." % "; ".join(c[:STEP_MAX] for c in criteria[:ASK_STEPS]))


# Docs a lane must not delete or mostly replace unless its ticket names them:
# a top-level README (any extension), AGENTS.md or CLAUDE.md at any size,
# any other Markdown file from REWRITE_MIN_LINES lines.
TOP_DOCS = ("agents.md", "claude.md")
REWRITE_SHARE = 0.5
REWRITE_MIN_LINES = 10
_GONE = ("removed", "deleted")


def _named(path: str, ticket_body: str) -> bool:
    body = ticket_body.lower()
    return path.lower() in body or path.rsplit("/", 1)[-1].lower() in body


def _top_doc(path: str) -> bool:
    name = path.lower()
    return "/" not in name and (name.startswith("readme") or name in TOP_DOCS)


def _guarded_doc(path: str) -> bool:
    return _top_doc(path) or path.lower().endswith(".md")


def _base_lines(gh: Any, repo: str, branch: str, path: str) -> Optional[int]:
    """The file's line count on the base branch; None when it cannot be read."""
    data = gh.rest("GET", "/repos/%s/contents/%s?ref=%s" % (
        repo, quote(path), quote(branch, safe="")), ok=(404,)).data
    content = data.get("content") if isinstance(data, dict) else None
    if not isinstance(content, str):
        return None
    try:
        return base64.b64decode(content).decode("utf-8", "replace").count("\n") + 1
    except ValueError:
        return None


def _renamed_away(entry: Dict[str, Any], ticket_body: str) -> Optional[str]:
    """For a renamed entry, the guarded doc it moved away ("" for none); None
    when the entry is no rename. A guarded doc renamed away is removed."""
    path, old = str(entry.get("filename") or ""), str(entry.get("previous_filename") or "")
    if str(entry.get("status") or "").lower() != "renamed" or not old or old == path:
        return None
    return old if _guarded_doc(old) and not _named(old, ticket_body) else ""


def _mostly_replaced(gh: Any, repo: str, branch: str, path: str, deleted: int) -> bool:
    lines = _base_lines(gh, repo, branch, path)
    if lines is None:
        return True  # an unread base counts as replaced
    floor = 1 if _top_doc(path) else REWRITE_MIN_LINES
    return lines >= floor and deleted >= REWRITE_SHARE * lines


def _rewritten(gh: Any, repo: str, branch: str, entry: Dict[str, Any],
               ticket_body: str) -> str:
    """The guarded doc this one file entry deletes, renames away or mostly
    replaces, or ""."""
    moved = _renamed_away(entry, ticket_body)
    if moved is not None:
        return moved
    path = str(entry.get("filename") or "")
    deleted = int(entry.get("deletions") or 0)
    if not deleted or not _guarded_doc(path) or _named(path, ticket_body):
        return ""
    gone = str(entry.get("status") or "").lower() in _GONE
    return path if gone or _mostly_replaced(gh, repo, branch, path, deleted) else ""


def rewritten_docs(gh: Any, repo: str, pr: Dict[str, Any], entries: Sequence[Dict[str, Any]],
                   ticket_body: str) -> List[str]:
    """The docs the pull request deletes, renames away, or replaces at least
    REWRITE_SHARE of, that its ticket does not name. A top-level README,
    AGENTS or CLAUDE file counts at any size, another doc from
    REWRITE_MIN_LINES lines, and a doc whose base cannot be read counts as
    replaced. A renamed entry is read by its previous_filename."""
    branch = str((pr.get("base") or {}).get("ref") or "main")
    found = (_rewritten(gh, repo, branch, entry, ticket_body) for entry in entries)
    return sorted(path for path in found if path)


def _shown(paths: Sequence[str]) -> str:
    return ", ".join(paths[:FILES_SHOWN]) + (
        " and %d more" % (len(paths) - FILES_SHOWN) if len(paths) > FILES_SHOWN else "")


def reasons(gh: Any, repo: str, pr: Dict[str, Any], ticket_body: str,
            entries: Optional[List[Dict[str, Any]]] = None) -> List[str]:
    """Why the pull request waits for the operator; [] when it may merge.
    `entries`, when given, are its changed files (filename, deletions,
    status): the patches are read only when one is a manifest."""
    out: List[str] = []
    steps = operator_steps(str(pr.get("body") or ""), ticket_body)
    if steps:
        out.append("it declares operator steps before merge: " + "; ".join(steps[:ASK_STEPS]))
    if entries is None:
        entries = pull_files(gh, repo, int(pr.get("number") or 0))
    rewritten = rewritten_docs(gh, repo, pr, entries, ticket_body)
    if rewritten:
        out.append("it deletes or mostly replaces %s, which its ticket does not name"
                   % _shown(rewritten))
    measured = measurement_reason(gh, repo, pr, ticket_body, entries)
    if measured:
        out.append(measured)
    if manifest_paths([str(e.get("filename") or "") for e in entries]):
        moved = placement_files(gh, repo, pr, entries if all("patch" in e for e in entries)
                                else None)
        if moved:
            out.append("it changes where workloads run or what they mount (node pins, "
                       "affinity, tolerations, volumes) in %s (%s)"
                       % (_shown(moved), _deployers(gh, repo, pr, moved)))
    return out


def measurement_reason(gh: Any, repo: str, pr: Dict[str, Any], ticket_body: str,
                       entries: List[Dict[str, Any]]) -> str:
    """Why a lane change on a health ticket waits: it edits the health read
    or a bound (bus/brief_check.py); "" otherwise. The patches decide a
    bound, so a list read without them is read again."""
    if not brief_check.is_health(ticket_body):
        return ""
    patched = entries if all("patch" in e for e in entries) else pull_files(
        gh, repo, int(pr.get("number") or 0))
    touched = brief_check.measurement_files(patched)
    if not touched:
        return ""
    return ("its ticket is a health ticket and it changes the health read or a bound "
            "(threshold, floor, ceiling or cutoff) in %s: a fix makes the affected fewer, "
            "not the measurement" % _shown(touched))


def ask(number: int, why: Sequence[str]) -> str:
    """The one exact ask for the ticket."""
    return ("Kind: decision\nPull request #%d waits for you: Loom will not merge it, because "
            "%s. Do or check each of these (the operator steps, whether a placement change "
            "is safe for the data and the nodes it lands on, whether a replaced doc should "
            "be), then merge #%d yourself, or close it if it should not land."
            % (number, "; and ".join(why), number))


GENERIC_WHY = "its body, its ticket or its manifests declare work an operator does first"


def hold(gh: Any, repo: str, ticket: int, number: int, why: Sequence[str],
         human: Callable[..., Any]) -> str:
    """Put the ticket to loom:human with the ask. Returns "asked", "already"
    (an earlier pass asked: the ticket is loom:human) or why the ask could
    not be written; the hold stands either way, since only the merge gate
    keeps the pull request from landing."""
    try:
        human(gh, repo, ticket, ask(number, why))
        return "asked"
    except ValueError as err:
        if "already loom:human" in str(err):
            return "already"
        first = str(err)
    # The quoted steps may hold text a comment must not carry (a private
    # hostname, say): ask without them.
    try:
        human(gh, repo, ticket, ask(number, [GENERIC_WHY]))
        return "asked"
    except ValueError as err:
        return "the ask was refused (%s; then %s)" % (first, err)
