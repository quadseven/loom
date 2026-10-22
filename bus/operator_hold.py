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

A held pull request is never merged by Loom. Its ticket goes to loom:human
with one exact ask, and the operator merges it by hand after the steps.
"""
from __future__ import annotations

import base64
import re
from typing import Any, Callable, Dict, List, Optional, Sequence
from urllib.parse import quote

from . import ship_paths

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


def reasons(gh: Any, repo: str, pr: Dict[str, Any], ticket_body: str,
            paths: Optional[Sequence[str]] = None) -> List[str]:
    """Why the pull request waits for the operator; [] when it may merge.
    `paths`, when given, are its changed files: the patches are read only
    when one is a manifest."""
    out: List[str] = []
    steps = operator_steps(str(pr.get("body") or ""), ticket_body)
    if steps:
        out.append("it declares operator steps before merge: " + "; ".join(steps[:ASK_STEPS]))
    if paths is None or manifest_paths(paths):
        moved = placement_files(gh, repo, pr)
        if moved:
            shown = ", ".join(moved[:FILES_SHOWN]) + (
                " and %d more" % (len(moved) - FILES_SHOWN) if len(moved) > FILES_SHOWN else "")
            out.append("it changes where workloads run or what they mount (node pins, "
                       "affinity, tolerations, volumes) in %s (%s)"
                       % (shown, _deployers(gh, repo, pr, moved)))
    return out


def ask(number: int, why: Sequence[str]) -> str:
    """The one exact ask for the ticket."""
    return ("Kind: decision\nPull request #%d waits for you: Loom will not merge it, because "
            "%s. Do the operator steps (or check that the placement change is safe for the "
            "data and the nodes it lands on), then merge #%d yourself."
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
