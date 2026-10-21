"""Open or refresh a deployment source-pin PR for a roll-shipped module."""
from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Any
from urllib.parse import quote
from urllib.parse import urlsplit

from . import brain, health
from .github import GitHub

REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
BLOCK = re.compile(r"(?ms)^```loom-roll-pins\s*\n(.*?)\n```\s*$")
MARKER = "<!-- loom:roll-pin "
# A goal home whose body names a roll ("roll" or "rolls", not "rolling" or
# "rollback") ships through one, so it needs a roll ship document.
ROLL_WORD = re.compile(r"(?i)\brolls?\b")


def ships_by_roll(body: str) -> bool:
    return bool(ROLL_WORD.search(body))


def specs_from_env(raw: str) -> list[dict[str, str]]:
    data = json.loads(raw or "[]")
    if not isinstance(data, list):
        raise ValueError("LOOM_ROLL_SHIP_DOCS must be a JSON array")
    for item in data:
        if not isinstance(item, dict) or not all(isinstance(item.get(k), str)
                for k in ("goal", "repo", "path")):
            raise ValueError("each roll ship document needs goal, repo and path")
        goal_repo, sep, number = item["goal"].partition("#")
        if not sep or not REPO.fullmatch(goal_repo) or not number.isdigit() \
                or not REPO.fullmatch(item["repo"]) or not _path(item["path"]):
            raise ValueError("invalid roll ship document")
        if not _valid_gate_url(item.get("gate_url")):
            raise ValueError("roll gate_url must be an https URL")
    return data


def _valid_gate_url(gate_url: Any) -> bool:
    return gate_url is None or (isinstance(gate_url, str) and
                                urlsplit(gate_url).scheme == "https" and
                                bool(urlsplit(gate_url).hostname))


def _path(value: str) -> bool:
    return bool(value and not value.startswith("/") and
                all(part not in ("", ".", "..") for part in value.split("/")))


def _content(gh: GitHub, repo: str, path: str, ref: str = "") -> tuple[str, str]:
    url = "/repos/%s/contents/%s" % (repo, quote(path, safe="/"))
    if ref:
        url += "?ref=" + quote(ref, safe="")
    data = gh.rest("GET", url).data
    if not isinstance(data, dict) or not isinstance(data.get("content"), str):
        raise ValueError("source pin file is missing: %s" % path)
    return base64.b64decode(data["content"]).decode(), str(data["sha"])


def _manifest(text: str) -> dict[str, Any]:
    blocks = BLOCK.findall(text)
    if len(blocks) != 1:
        raise ValueError("ship document needs one loom-roll-pins block")
    data = json.loads(blocks[0])
    if not isinstance(data, dict) or not REPO.fullmatch(str(data.get("deployment_repo", ""))):
        raise ValueError("ship document needs a deployment_repo")
    if not isinstance(data.get("build_workflow"), str) or not data["build_workflow"].strip():
        raise ValueError("ship document needs a build_workflow")
    sources = data.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("ship document needs sources")
    for source in sources:
        _validate_source(source)
    return data


def _valid_pin(pin: Any) -> bool:
    if not isinstance(pin, dict) or not _path(str(pin.get("path", ""))):
        return False
    kind = pin.get("kind", "env")
    if kind == "env":
        return bool(KEY.fullmatch(str(pin.get("key", ""))))
    if kind == "dict_dev":
        return bool(KEY.fullmatch(str(pin.get("entry", ""))))
    return kind == "gitlink"


def _validate_source(source: Any) -> None:
    if not isinstance(source, dict) or not REPO.fullmatch(str(source.get("repo", ""))):
        raise ValueError("invalid roll source repo")
    if "revision_repo" in source or "revision_ref" in source:
        if not REPO.fullmatch(str(source.get("revision_repo", ""))) \
                or not isinstance(source.get("revision_ref"), str) \
                or not source["revision_ref"].strip():
            raise ValueError("revision_repo and revision_ref must be set together")
    pins = source.get("pins")
    if not isinstance(pins, list) or not pins or not all(_valid_pin(p) for p in pins):
        raise ValueError("invalid roll source pin")
    if len({p["path"] for p in pins}) != len(pins):
        raise ValueError("roll source pin paths must be unique")


def _latest_merge(gh: GitHub, repo: str) -> str:
    pulls = gh.rest("GET", "/repos/%s/pulls?state=closed&sort=updated&direction=desc&per_page=100"
                    % repo).data or []
    merged = [p for p in pulls if p.get("merged_at") and SHA.fullmatch(
        str(p.get("merge_commit_sha") or ""))]
    return str(max(merged, key=lambda p: p["merged_at"])["merge_commit_sha"]) if merged else ""


def _revision(gh: GitHub, source: dict[str, Any], merged_sha: str) -> str:
    if "revision_ref" not in source:
        return merged_sha
    ref = gh.rest("GET", "/repos/%s/git/ref/heads/%s" %
                  (source["revision_repo"], quote(source["revision_ref"], safe=""))).data
    revision = str((ref.get("object") or {}).get("sha") or "")
    if not SHA.fullmatch(revision):
        raise ValueError("roll revision ref has no commit SHA")
    return revision


def _replace_pin(text: str, key: str, revision: str) -> str:
    pattern = re.compile(r"^(" + re.escape(key) + r"\s*=\s*)(['\"]?)([0-9a-f]{40})\2([ \t]*)$", re.M)
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise ValueError("pin %s must occur exactly once with a SHA" % key)
    match = matches[0]
    return text[:match.start()] + match[1] + match[2] + revision + match[2] + match[4] + text[match.end():]


def _replace_dev(text: str, entry: str, revision: str) -> str:
    entry_re = re.compile(r'"' + re.escape(entry) + r'"\s*:\s*\{(.*?)\}', re.S)
    blocks = list(entry_re.finditer(text))
    if len(blocks) != 1:
        raise ValueError("drift entry %s must occur exactly once" % entry)
    block = blocks[0]
    field = re.compile(r'("dev"\s*:\s*["\'])([0-9a-f]{40})(["\'])')
    fields = list(field.finditer(block[1]))
    if len(fields) != 1 or fields[0][1][-1] != fields[0][3]:
        raise ValueError("drift dev field must occur exactly once with a SHA")
    old = fields[0]
    start = block.start(1) + old.start(2)
    return text[:start] + revision + text[start + 40:]


def _replace_text(text: str, pin: dict[str, str], revision: str) -> str:
    if pin.get("kind", "env") == "dict_dev":
        return _replace_dev(text, pin["entry"], revision)
    return _replace_pin(text, pin["key"], revision)


def _gitlink_sha(gh: GitHub, repo: str, commit: str, path: str) -> str:
    obj = gh.rest("GET", "/repos/%s/git/commits/%s" % (repo, commit)).data
    tree = gh.rest("GET", "/repos/%s/git/trees/%s?recursive=1" %
                   (repo, obj["tree"]["sha"])).data
    found = [entry for entry in tree["tree"] if entry.get("path") == path]
    if len(found) != 1 or found[0].get("mode") != "160000":
        raise ValueError("gitlink path is missing: %s" % path)
    return str(found[0]["sha"])


def _set_gitlinks(gh: GitHub, repo: str, branch: str, pins: list[dict[str, str]],
                  revision: str) -> bool:
    if not pins:
        return False
    ref_path = "/repos/%s/git/ref/heads/%s" % (repo, quote(branch, safe=""))
    head = gh.rest("GET", ref_path).data["object"]["sha"]
    obj = gh.rest("GET", "/repos/%s/git/commits/%s" % (repo, head)).data
    tree_sha = obj["tree"]["sha"]
    tree = gh.rest("GET", "/repos/%s/git/trees/%s?recursive=1" % (repo, tree_sha)).data
    current = {entry["path"]: entry["sha"] for entry in tree["tree"]
               if entry.get("mode") == "160000"}
    updates = [{"path": pin["path"], "mode": "160000", "type": "commit", "sha": revision}
               for pin in pins if current.get(pin["path"]) != revision]
    if not updates:
        return False
    if any(pin["path"] not in current for pin in pins):
        raise ValueError("gitlink path is missing")
    new_tree = gh.rest("POST", "/repos/%s/git/trees" % repo,
                       {"base_tree": tree_sha, "tree": updates}).data["sha"]
    new_commit = gh.rest("POST", "/repos/%s/git/commits" % repo,
                         {"message": "Update roll submodule pin", "tree": new_tree,
                          "parents": [head]}).data["sha"]
    gh.rest("PATCH", "/repos/%s/git/refs/heads/%s" %
            (repo, quote(branch, safe="")), {"sha": new_commit, "force": False})
    return True


def _branch(goal: str) -> str:
    return "loom/roll-pin-" + hashlib.sha256(goal.encode()).hexdigest()[:12]


def _journal_build(gh: GitHub, goal: str, deploy: str, pr: dict[str, Any],
                   head_sha: str, revision: str, workflow: str) -> None:
    goal_repo, number = goal.split("#", 1)
    if not SHA.fullmatch(head_sha):
        raise ValueError("roll pin branch has no commit SHA")
    by_file = workflow.endswith((".yml", ".yaml"))
    endpoint = ("/repos/%s/actions/workflows/%s/runs" %
                (deploy, quote(workflow, safe="")) if by_file
                else "/repos/%s/actions/runs" % deploy)
    runs = gh.rest("GET", "%s?head_sha=%s&per_page=100" %
                   (endpoint, head_sha)).data or {}
    found = next((run for run in runs.get("workflow_runs", [])
                  if run.get("head_sha") == head_sha and
                  (by_file or run.get("name") == workflow)
                  and run.get("html_url")), None)
    if not found:
        return
    marker = MARKER + head_sha + " -->"
    comments = gh.rest("GET", "/repos/%s/issues/%s/comments?per_page=100" %
                       (goal_repo, number)).data or []
    if any(marker in str(comment.get("body") or "") for comment in comments):
        return
    body = ("<!-- loom:journal v1 -->\n%s\nSource-pin pull request %s for module revision `%s` started build run %s."
            % (marker, pr["html_url"], revision, found["html_url"]))
    gh.rest("POST", "/repos/%s/issues/%s/comments" % (goal_repo, number), {"body": body})


def _post_build(gh: GitHub, goal: str, deploy: str, pr: dict[str, Any], branch: str,
                revision: str, workflow: str) -> None:
    ref = gh.rest("GET", "/repos/%s/git/ref/heads/%s" %
                  (deploy, quote(branch, safe=""))).data
    head_sha = str((ref.get("object") or {}).get("sha") or "")
    _journal_build(gh, goal, deploy, pr, head_sha, revision, workflow)


def _merged_build(gh: GitHub, goal: str, deploy: str, revision: str,
                  workflow: str) -> None:
    pulls = gh.rest("GET", "/repos/%s/pulls?state=closed&sort=updated&direction=desc&per_page=100"
                    % deploy).data or []
    branch = _branch(goal)
    pr = next((pr for pr in pulls if pr.get("merged_at") and
               (pr.get("head") or {}).get("ref") == branch and
               revision in str(pr.get("body") or "")), None)
    if pr:
        _journal_build(gh, goal, deploy, pr, str(pr.get("merge_commit_sha") or ""),
                       revision, workflow)


def _changed_text_pins(gh: GitHub, deploy: str, source: dict[str, Any], revision: str,
                       active: dict[str, Any] | None) -> list[dict[str, str]]:
    """The text pins the pull request carries: every one while it is open,
    else only those whose base file does not already name the revision."""
    changed = []
    for pin in source["pins"]:
        if pin.get("kind", "env") == "gitlink":
            continue
        text, _ = _content(gh, deploy, pin["path"])
        if text != _replace_text(text, pin, revision) or active:
            changed.append(pin)
    return changed


def _stale_gitlinks(gh: GitHub, deploy: str, source: dict[str, Any], base: str,
                    revision: str, active: dict[str, Any] | None) -> list[dict[str, str]]:
    """The gitlink pins to set: every one while the pull request is open,
    else only those whose base commit does not already point at the revision."""
    gitlinks = [pin for pin in source["pins"] if pin.get("kind") == "gitlink"]
    if not gitlinks or active:
        return gitlinks
    base_ref = gh.rest("GET", "/repos/%s/git/ref/heads/%s" %
                       (deploy, quote(base, safe=""))).data
    base_sha = base_ref["object"]["sha"]
    return [pin for pin in gitlinks
            if _gitlink_sha(gh, deploy, base_sha, pin["path"]) != revision]


def _ensure_branch(gh: GitHub, deploy: str, branch: str, base: str) -> None:
    existing = gh.rest("GET", "/repos/%s/git/ref/heads/%s" %
                       (deploy, quote(branch, safe="")), ok=(404,))
    if existing.status == 404:
        base_ref = gh.rest("GET", "/repos/%s/git/ref/heads/%s" %
                           (deploy, quote(base, safe=""))).data
        gh.rest("POST", "/repos/%s/git/refs" % deploy,
                {"ref": "refs/heads/" + branch, "sha": base_ref["object"]["sha"]})


def _write_text_pins(gh: GitHub, deploy: str, branch: str, pins: list[dict[str, str]],
                     revision: str) -> bool:
    current = [(pin, *_content(gh, deploy, pin["path"], branch)) for pin in pins]
    wrote = False
    for pin, text, sha in current:
        updated = _replace_text(text, pin, revision)
        if updated != text:
            gh.rest("PUT", "/repos/%s/contents/%s" % (deploy, quote(pin["path"], safe="/")),
                    {"message": "Update roll source pin", "content": base64.b64encode(
                        updated.encode()).decode(), "sha": sha, "branch": branch})
            wrote = True
    return wrote


def _pull_plan(revision: str) -> str:
    return ("## Why\nA merged module change needs a server build.\n\n"
            "## Summary\nUpdate only the source pins declared by the goal ship document.\n\n"
            "## Acceptance criteria\n- [ ] Source pins name `%s`.\n"
            "- [ ] Only locations named by the ship document changed.\n"
            "- [ ] Review the build before merging.\n\nSize: S\n\n"
            "## Out of scope\n- Merging, proving or deploying this pull request.\n" % revision)


def _open_or_refresh(gh: GitHub, deploy: str, branch: str, base: str, revision: str,
                     active: dict[str, Any] | None, wrote: bool) -> dict[str, Any]:
    title = "Pin roll source to %s" % revision[:12]
    if active is None:
        return gh.rest("POST", "/repos/%s/pulls" % deploy,
                       {"title": title, "head": branch, "base": base,
                        "body": _pull_plan(revision)}).data
    if wrote:
        gh.rest("PATCH", "/repos/%s/pulls/%s" % (deploy, active["number"]),
                {"title": title, "body": _pull_plan(revision)})
    return active


def _update(gh: GitHub, goal: str, deploy: str, source: dict[str, Any],
            revision: str, workflow: str) -> None:
    branch = _branch(goal)
    base = gh.rest("GET", "/repos/%s" % deploy).data["default_branch"]
    pulls = gh.rest("GET", "/repos/%s/pulls?state=open&per_page=100" % deploy).data or []
    active = next((pr for pr in pulls if (pr.get("head") or {}).get("ref") == branch), None)
    changed = _changed_text_pins(gh, deploy, source, revision, active)
    gitlinks = _stale_gitlinks(gh, deploy, source, base, revision, active)
    if not changed and not gitlinks:
        return
    if not active:
        _ensure_branch(gh, deploy, branch, base)
    wrote = _write_text_pins(gh, deploy, branch, changed, revision)
    wrote = _set_gitlinks(gh, deploy, branch, gitlinks, revision) or wrote
    pr = _open_or_refresh(gh, deploy, branch, base, revision, active, wrote)
    _post_build(gh, goal, deploy, pr, branch, revision, workflow)


def _manual_pin_pull(gh: GitHub, goal: str, deploy: str,
                     manifest: dict[str, Any]) -> dict[str, Any] | None:
    paths = {pin["path"] for source in manifest["sources"] for pin in source["pins"]}
    pulls = gh.rest("GET", "/repos/%s/pulls?state=open&per_page=100" % deploy).data or []
    for pr in pulls:
        if (pr.get("head") or {}).get("ref") == _branch(goal):
            continue
        if not re.search(r"roll.*pin|pin.*roll", str(pr.get("title") or ""), re.I):
            continue
        if not re.search(r"family gate", str(pr.get("body") or ""), re.I):
            continue
        files = gh.paginate("/repos/%s/pulls/%d/files" % (deploy, int(pr["number"])))
        changed = {file.get("filename") for file in files}
        if changed and changed <= paths:
            return pr
    return None


def _poll_gate(gh: GitHub, spec: dict[str, str], deploy: str,
               authors: list[str], review_check: str,
               manual: dict[str, Any] | None = None) -> None:
    pulls = gh.rest("GET", "/repos/%s/pulls?state=open&per_page=100" % deploy).data or []
    pr = manual or next((p for p in pulls if (p.get("head") or {}).get("ref") == _branch(spec["goal"])), None)
    if pr is None:
        return
    gate = health.fetch(spec["gate_url"])
    if (not isinstance(gate, dict) or type(gate.get("open")) not in (bool, int)
            or gate["open"] not in (0, 1)):
        raise ValueError("roll family gate must answer with a boolean or 0/1 open field")
    if not gate["open"]:
        return
    env = {"LOOM_BRIEF_AUTHORS": " ".join(authors), "LOOM_REVIEW_CHECK": review_check}
    try:
        brain.merge(gh, deploy, int(pr["number"]), adopt=True, env=env)
    except ValueError:
        _gate_alert(gh, spec["goal"], deploy, pr)


def _gate_alert(gh: GitHub, goal: str, deploy: str, pr: dict[str, Any]) -> None:
    repo, number = goal.split("#", 1)
    sha = str((pr.get("head") or {}).get("sha") or "")
    if not SHA.fullmatch(sha):
        raise ValueError("roll pin pull request has no head SHA")
    marker = "<!-- loom:roll-gate pr=%d sha=%s -->" % (int(pr["number"]), sha)
    path = "/repos/%s/issues/%s/comments" % (repo, number)
    comments = gh.paginate(path)
    if any(marker in str(comment.get("body") or "") for comment in comments):
        return
    body = ("<!-- loom:journal v1 -->\n%s\nFamily gate is open; source-pin pull "
            "request %s#%d needs a brain merge review. Its merge checks or review "
            "rules did not pass." % (marker, deploy, int(pr["number"])))
    gh.rest("POST", path, {"body": body})


def run(gh: GitHub, documents: list[dict[str, str]],
        authors: list[str] | None = None, review_check: str = "") -> None:
    for spec in documents:
        text, _ = _content(gh, spec["repo"], spec["path"])
        manifest = _manifest(text)
        deploy = manifest["deployment_repo"]
        manual = _manual_pin_pull(gh, spec["goal"], deploy, manifest) if spec.get("gate_url") else None
        for source in manifest["sources"]:
            merged_sha = _latest_merge(gh, source["repo"])
            if merged_sha:
                revision = _revision(gh, source, merged_sha)
                if manual is None:
                    _update(gh, spec["goal"], deploy, source,
                            revision, manifest["build_workflow"])
                _merged_build(gh, spec["goal"], deploy,
                              revision, manifest["build_workflow"])
        if spec.get("gate_url"):
            _poll_gate(gh, spec, deploy, authors or [], review_check, manual)
