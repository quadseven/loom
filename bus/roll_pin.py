"""Open and update source-pin pull requests when roll-shipped module repos merge."""
from __future__ import annotations

import base64
import json
import os
import re
import time
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple
from urllib.parse import quote

from . import hunt_plan, journal, text
from .github import GitHub, GitHubError


class Location(NamedTuple):
    path: str
    var: str = ""
    key: str = ""


class RollSpec(NamedTuple):
    roll: str
    deploy_repo: str
    modules: Dict[str, List[Location]]


def _parse_json_spec(content: str) -> Optional[RollSpec]:
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
    raw = match.group(1) if match else content.strip()
    if not (raw.startswith("{") and raw.endswith("}")):
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    roll = str(data.get("roll") or data.get("type") or "roll").strip()
    deploy_repo = str(data.get("deployment_repo") or data.get("deployment") or "").strip()
    raw_modules = data.get("modules") or data.get("pins") or data.get("targets")
    if not deploy_repo or not isinstance(raw_modules, dict):
        return None
    modules: Dict[str, List[Location]] = {}
    for mod, locs in raw_modules.items():
        if not isinstance(locs, list):
            continue
        locations: List[Location] = []
        for item in locs:
            if isinstance(item, dict) and item.get("path"):
                locations.append(Location(
                    path=str(item["path"]).strip(),
                    var=str(item.get("var") or item.get("variable") or "").strip(),
                    key=str(item.get("key") or "").strip(),
                ))
        if locations:
            modules[str(mod).strip()] = locations
    return RollSpec(roll=roll, deploy_repo=deploy_repo, modules=modules) if modules else None


def _parse_table_modules(content: str) -> Dict[str, List[Location]]:
    modules: Dict[str, List[Location]] = {}
    row_re = re.compile(r"^[ \t]*\|[ \t]*([^|]+?)[ \t]*\|[ \t]*([^|]+?)[ \t]*\|[ \t]*([^|]+?)[ \t]*\|", re.M)
    for m in row_re.finditer(content):
        col1, col2, col3 = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
        if col1.lower() in ("module", "repo", "---", ":---") or col1.startswith("-") or col1.startswith(":"):
            continue
        modules.setdefault(col1, []).append(Location(path=col2, var=col3))
    return modules


def resolve_module_repo(goal_repo: str, mod_key: str) -> str:
    """Resolve full owner/repo from a module key."""
    if "/" in mod_key:
        return mod_key
    owner = goal_repo.partition("/")[0]
    return "%s/%s" % (owner, mod_key) if owner else mod_key


def _parse_markdown_spec(content: str) -> Optional[RollSpec]:
    m_roll = re.search(r"^[ \t]*(?:Roll|Roll name):[ \t]*([A-Za-z0-9_-]+)", content, re.M | re.I)
    roll = m_roll.group(1).strip() if m_roll else "worldserver"
    m_dep = re.search(r"^[ \t]*(?:Deployment repo|Deployment):[ \t]*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)",
                      content, re.M | re.I)
    if not m_dep:
        return None
    deploy_repo = m_dep.group(1).strip()
    modules = _parse_table_modules(content)
    if not modules:
        bullet_re = re.compile(
            r"^[ \t]*-[ \t]+([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?):[ \t]*\n"
            r"((?:[ \t]+-[ \t]+[^\n]+\n?)+)", re.M)
        loc_re = re.compile(r"^[ \t]+-[ \t]+(?:path:[ \t]*)?(\S+?)(?:,[ \t]*(?:var|key):[ \t]*(\S+))?[ \t]*$", re.M)
        for m in bullet_re.finditer(content):
            mod_name = m.group(1).strip()
            loc_block = m.group(2)
            for lm in loc_re.finditer(loc_block):
                modules.setdefault(mod_name, []).append(
                    Location(path=lm.group(1).strip(), var=(lm.group(2) or "").strip()))
    return RollSpec(roll=roll, deploy_repo=deploy_repo, modules=modules) if modules else None


def parse_ship_document(content: str) -> Optional[RollSpec]:
    """Parse a ship document from JSON block or markdown specification."""
    spec = _parse_json_spec(content)
    if spec is not None:
        return spec
    return _parse_markdown_spec(content)


def _fetch_file(gh: GitHub, repo: str, path: str) -> str:
    res = gh.rest("GET", "/repos/%s/contents/%s" % (repo, quote(path, safe="/")), ok=(404,))
    if res.status != 200:
        return ""
    data = res.data or {}
    raw = str(data.get("content") or "")
    return base64.b64decode(raw).decode("utf-8", errors="replace")


def goal_ship_document(gh: GitHub, goal_repo: str, goal_issue: Dict[str, Any]) -> Optional[RollSpec]:
    """Load and parse the ship document associated with a goal home issue."""
    body = str(goal_issue.get("body") or "")
    m = re.search(r"(?:Ship document|Roll document):[ \t]*(\S+)", body, re.I)
    if not m:
        m = re.search(r"\[(?:Ship|Roll) document\]\(([^)]+)\)", body, re.I)
    if m:
        target = m.group(1).strip()
        t_repo, _, t_path = target.partition(":") if ":" in target and not target.startswith("http") else (goal_repo, "", target)
        doc_text = _fetch_file(gh, t_repo, t_path)
        spec = parse_ship_document(doc_text)
        if spec is not None:
            return spec
    spec = parse_ship_document(body)
    if spec is not None:
        return spec
    for std_path in ("docs/ROLL.md", "docs/SHIP.md", "docs/roll.md", "docs/ship.md"):
        doc_text = _fetch_file(gh, goal_repo, std_path)
        if doc_text:
            spec = parse_ship_document(doc_text)
            if spec is not None:
                return spec
    return None


def match_module(repo: str, module_key: str) -> bool:
    """True if repo matches the module key (either full owner/name or short name)."""
    r_low = repo.lower()
    m_low = module_key.lower()
    return r_low == m_low or r_low.endswith("/" + m_low) or m_low.endswith("/" + r_low)


def latest_merge(gh: GitHub, module_repo: str) -> Optional[Tuple[str, str, str]]:
    """Return (sha, merged_at, pr_title) for the latest merge on module_repo."""
    pulls = gh.rest("GET", "/repos/%s/pulls?state=closed&sort=updated&direction=desc&per_page=5"
                    % module_repo, ok=(404,)).data or []
    for pr in pulls if isinstance(pulls, list) else []:
        if isinstance(pr, dict) and pr.get("merged_at"):
            sha = str(pr.get("merge_commit_sha") or "")
            if sha:
                return sha, str(pr["merged_at"]), str(pr.get("title") or "")
    data = gh.rest("GET", "/repos/%s" % module_repo, ok=(404,)).data or {}
    default_branch = str(data.get("default_branch") or "main")
    commits = gh.rest("GET", "/repos/%s/commits?sha=%s&per_page=1"
                      % (module_repo, quote(default_branch, safe="")), ok=(404,)).data or []
    if isinstance(commits, list) and commits and isinstance(commits[0], dict):
        sha = str(commits[0].get("sha") or "")
        if sha:
            return sha, "", "Commit %s" % sha[:12]
    return None


def update_location_content(content_str: str, loc: Location, new_sha: str) -> str:
    """Replace the pinned SHA for a location within a file."""
    target = loc.var or loc.key
    if not target:
        return content_str
    pattern_eq = r"^([ \t]*" + re.escape(target) + r"[ \t]*=[ \t]*['\"]?)[^'\"\s\n]+(['\"]?[ \t]*)$"
    if re.search(pattern_eq, content_str, re.M):
        return re.sub(pattern_eq, r"\g<1>" + new_sha + r"\g<2>", content_str, count=1, flags=re.M)
    pattern_docker = r"^([ \t]*(?:ARG|ENV)[ \t]+" + re.escape(target) + r"[ \t]*[= \t][ \t]*['\"]?)[^'\"\s\n]+(['\"]?[ \t]*)$"
    if re.search(pattern_docker, content_str, re.M):
        return re.sub(pattern_docker, r"\g<1>" + new_sha + r"\g<2>", content_str, count=1, flags=re.M)
    pattern_json = r"([\"']" + re.escape(target) + r"[\"'][ \t]*:[ \t]*[\"'])[^\"'\s\n]+([\"'])"
    if re.search(pattern_json, content_str):
        return re.sub(pattern_json, r"\g<1>" + new_sha + r"\g<2>", content_str, count=1)
    pattern_cmake = r"(set\([ \t]*" + re.escape(target) + r"[ \t]+['\"]?)[^'\"\s\)]+(['\"]?\))"
    if re.search(pattern_cmake, content_str):
        return re.sub(pattern_cmake, r"\g<1>" + new_sha + r"\g<2>", content_str, count=1)
    pattern_fallback = r"^(" + re.escape(target) + r"=).*?$"
    if re.search(pattern_fallback, content_str, re.M):
        return re.sub(pattern_fallback, lambda m: m.group(1) + '"' + new_sha + '"', content_str, count=1, flags=re.M)
    return content_str


def location_has_sha(content_str: str, loc: Location, sha: str) -> bool:
    target = loc.var or loc.key
    if not target:
        return sha in content_str
    pattern = re.escape(target) + r".*?" + re.escape(sha)
    return bool(re.search(pattern, content_str))


def render_pr_body(roll: str, module: str, revision: str, pr_title: str) -> str:
    """Generate a source-pin pull request body that passes Hunt Plan."""
    title_bullet = "- %s" % pr_title if pr_title else "- Merged module revision"
    body = (
        "## Why\n"
        "The %s roll deployment needs a reviewed source-pin pull request for module %s.\n\n"
        "## Summary\n"
        "Update the module source pin to revision %s.\n\n"
        "## Merged module pull requests\n"
        "%s\n\n"
        "## Acceptance criteria\n"
        "- [ ] The pin names revision `%s`.\n"
        "- [ ] The image build starts on push.\n"
        "- [ ] The deployment is reviewed before merge.\n\n"
        "Size: S\n\n"
        "## Out of scope\n"
        "- Merging or deploying this pull request.\n"
        % (roll, module, revision[:12], title_bullet, revision[:12])
    )
    if hunt_plan.problems(body):
        raise ValueError("generated source-pin PR body fails Hunt Plan")
    return body


def get_file_content(gh: GitHub, repo: str, path: str, ref: str = "") -> Tuple[str, str]:
    endpoint = "/repos/%s/contents/%s" % (repo, quote(path, safe="/"))
    if ref:
        endpoint += "?ref=" + quote(ref, safe="")
    data = gh.rest("GET", endpoint).data
    if not isinstance(data, dict) or not isinstance(data.get("content"), str):
        raise ValueError("target %s is not a regular file" % path)
    return base64.b64decode(data["content"]).decode("utf-8", errors="replace"), str(data.get("sha") or "")


def find_build_run(gh: GitHub, repo: str, head_sha: str) -> Tuple[str, str]:
    """Find build check run or workflow run for commit; returns (run_id, html_url)."""
    try:
        checks_data = gh.rest("GET", "/repos/%s/commits/%s/check-runs"
                              % (repo, head_sha), ok=(404,)).data or {}
        check_runs = checks_data.get("check_runs") or []
        if check_runs and isinstance(check_runs[0], dict):
            first = check_runs[0]
            return str(first.get("id") or ""), str(first.get("html_url") or "")
    except GitHubError:
        pass
    try:
        wf_data = gh.rest("GET", "/repos/%s/actions/runs?head_sha=%s"
                          % (repo, head_sha), ok=(404,)).data or {}
        wf_runs = wf_data.get("workflow_runs") or []
        if wf_runs and isinstance(wf_runs[0], dict):
            first = wf_runs[0]
            return str(first.get("id") or ""), str(first.get("html_url") or "")
    except GitHubError:
        pass
    return "", ""


def report_build_run(gh: GitHub, goal_repo: str, goal_number: int,
                     run_id: str, run_url: str, pr_number: int, now: float,
                     log: Callable[[str], None]) -> None:
    """Report the build run in the goal home journal."""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    desc = ("build run %s (%s) for source-pin PR #%d" % (run_id, run_url, pr_number)
            if run_url else "build run %s for source-pin PR #%d" % (run_id, pr_number))
    journal_body = journal.render_journal(
        stamp, "station", shipped="none", briefed="none",
        waiting=desc, asks="none", requests=1, minutes=0
    )
    text.check(journal_body)
    gh.rest("POST", "/repos/%s/issues/%d/comments" % (goal_repo, goal_number), {"body": journal_body})
    log("source-pin: reported build run %s on %s#%d" % (run_id, goal_repo, goal_number))


def _ensure_branch(gh: GitHub, repo: str, branch: str, base: str) -> None:
    ref = gh.rest("GET", "/repos/%s/git/ref/heads/%s" % (repo, quote(branch, safe="")), ok=(404,))
    if ref.status == 404:
        head_ref = gh.rest("GET", "/repos/%s/git/ref/heads/%s" % (repo, quote(base, safe=""))).data or {}
        sha = str((head_ref.get("object") or {}).get("sha") or head_ref.get("sha") or "")
        gh.rest("POST", "/repos/%s/git/refs" % repo,
                {"ref": "refs/heads/" + branch, "sha": sha})


def _update_files(gh: GitHub, repo: str, branch: str, locations: List[Location],
                  new_sha: str, module_key: str) -> None:
    by_path: Dict[str, List[Location]] = {}
    for loc in locations:
        by_path.setdefault(loc.path, []).append(loc)
    for path, locs in by_path.items():
        current_text, file_sha = get_file_content(gh, repo, path, ref=branch)
        updated_text = current_text
        for loc in locs:
            updated_text = update_location_content(updated_text, loc, new_sha)
        if updated_text != current_text:
            gh.rest("PUT", "/repos/%s/contents/%s" % (repo, quote(path, safe="/")), {
                "message": "Pin %s to %s" % (module_key, new_sha[:12]),
                "content": base64.b64encode(updated_text.encode()).decode(),
                "sha": file_sha,
                "branch": branch,
            })


def _open_or_patch_pull(gh: GitHub, repo: str, branch: str, base: str,
                        title: str, body: str, log: Callable[[str], None]) -> Tuple[int, bool]:
    pulls = gh.rest("GET", "/repos/%s/pulls?state=open&per_page=100" % repo).data or []
    active = next((pr for pr in pulls if isinstance(pr, dict)
                   and pr.get("head", {}).get("ref") == branch), None)
    if active:
        pr_num = int(active["number"])
        gh.rest("PATCH", "/repos/%s/pulls/%d" % (repo, pr_num), {"title": title, "body": body})
        log("source-pin: updated pull request #%d for %s" % (pr_num, repo))
        return pr_num, False
    made = gh.rest("POST", "/repos/%s/pulls" % repo,
                   {"title": title, "head": branch, "base": base, "body": body}).data
    pr_num = int(made["number"])
    log("source-pin: opened pull request #%d for %s" % (pr_num, repo))
    return pr_num, True


def pin_module(gh: GitHub, goal_repo: str, goal_issue: Dict[str, Any],
               spec: RollSpec, module_key: str, locations: List[Location],
               new_sha: str, pr_title: str, now: float,
               log: Callable[[str], None]) -> Tuple[int, str]:
    """Create or update the source-pin PR and report the build run; returns (pr_num, run_id)."""
    branch = "loom/source-pin-%s" % spec.roll
    base = str((gh.rest("GET", "/repos/%s" % spec.deploy_repo).data or {}).get("default_branch", "main"))
    _ensure_branch(gh, spec.deploy_repo, branch, base)
    _update_files(gh, spec.deploy_repo, branch, locations, new_sha, module_key)
    title = "Source pin: %s %s to %s" % (spec.roll, module_key, new_sha[:12])
    body = render_pr_body(spec.roll, module_key, new_sha, pr_title)
    pr_num, _ = _open_or_patch_pull(gh, spec.deploy_repo, branch, base, title, body, log)
    branch_ref = gh.rest("GET", "/repos/%s/git/ref/heads/%s" % (spec.deploy_repo, quote(branch, safe="")), ok=(404,)).data or {}
    head_sha = str((branch_ref.get("object") or {}).get("sha") or branch_ref.get("sha") or "")
    run_id, run_url = find_build_run(gh, spec.deploy_repo, head_sha)
    if run_id:
        report_build_run(gh, goal_repo, int(goal_issue["number"]), run_id, run_url, pr_num, now, log)
    return pr_num, run_id


def _needs_update(gh: GitHub, deploy_repo: str, branch: str, base: str,
                  locations: List[Location], new_sha: str) -> bool:
    ref = gh.rest("GET", "/repos/%s/git/ref/heads/%s" % (deploy_repo, quote(branch, safe="")), ok=(404,))
    active_ref = branch if ref.status == 200 else base
    for loc in locations:
        try:
            content_str, _ = get_file_content(gh, deploy_repo, loc.path, ref=active_ref)
        except (ValueError, GitHubError):
            return True
        if not location_has_sha(content_str, loc, new_sha):
            return True
    return False


def _check_pending_build_run(gh: GitHub, goal_repo: str, goal_issue: Dict[str, Any],
                             spec: RollSpec, branch: str, entry: Dict[str, Any],
                             now: float, log: Callable[[str], None]) -> bool:
    """If build run wasn't reported yet, check if it is available now and report it."""
    if entry.get("reported_run"):
        return False
    branch_ref = gh.rest("GET", "/repos/%s/git/ref/heads/%s" % (spec.deploy_repo, quote(branch, safe="")), ok=(404,)).data or {}
    head_sha = str((branch_ref.get("object") or {}).get("sha") or branch_ref.get("sha") or "")
    if not head_sha:
        return False
    run_id, run_url = find_build_run(gh, spec.deploy_repo, head_sha)
    if not run_id:
        return False
    pr_num = entry.get("pr") or 0
    if not pr_num:
        pulls = gh.rest("GET", "/repos/%s/pulls?state=open&per_page=100" % spec.deploy_repo).data or []
        active = next((pr for pr in pulls if isinstance(pr, dict) and pr.get("head", {}).get("ref") == branch), None)
        if active:
            pr_num = int(active["number"])
    report_build_run(gh, goal_repo, int(goal_issue["number"]), run_id, run_url, pr_num, now, log)
    entry["reported_run"] = run_id
    return True


def run_station(gh: GitHub, goal_homes: Sequence[Tuple[str, Dict[str, Any]]],
                state_path: str, now: float, log: Callable[[str], None] = print) -> int:
    """Inspect goal homes for roll-shipped module merges and update source pins."""
    state: Dict[str, Any] = {}
    if os.path.isfile(state_path):
        try:
            with open(state_path, encoding="utf-8") as fh:
                state = json.load(fh)
        except (OSError, ValueError):
            state = {}
    updated_count = 0
    for goal_repo, goal_issue in goal_homes:
        spec = goal_ship_document(gh, goal_repo, goal_issue)
        if spec is None:
            continue
        for mod_key, locations in spec.modules.items():
            mod_repo = resolve_module_repo(goal_repo, mod_key)
            merge_info = latest_merge(gh, mod_repo)
            if not merge_info:
                continue
            sha, _, pr_title = merge_info
            branch = "loom/source-pin-%s" % spec.roll
            base = str((gh.rest("GET", "/repos/%s" % spec.deploy_repo).data or {}).get("default_branch", "main"))
            entry = state.get(mod_key)
            if not isinstance(entry, dict):
                entry = {"sha": str(entry or ""), "reported_run": "", "pr": 0}
            if not _needs_update(gh, spec.deploy_repo, branch, base, locations, sha):
                if entry.get("sha") == sha and _check_pending_build_run(gh, goal_repo, goal_issue, spec, branch, entry, now, log):
                    state[mod_key] = entry
                    updated_count += 1
                continue
            pr_num, run_id = pin_module(gh, goal_repo, goal_issue, spec, mod_key, locations, sha, pr_title, now, log)
            state[mod_key] = {"sha": sha, "reported_run": run_id, "pr": pr_num}
            updated_count += 1
    if updated_count > 0:
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        try:
            with open(state_path + ".tmp", "w", encoding="utf-8") as fh:
                json.dump(state, fh)
            os.replace(state_path + ".tmp", state_path)
        except OSError as err:
            log("source-pin: could not save state: %s" % err)
    return updated_count
