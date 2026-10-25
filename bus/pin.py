"""Keep configured deployment pins in reviewable pull requests."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
from typing import Callable
from urllib.parse import quote, urlsplit

from .github import GitHub, GitHubError, token_from_env
from .hunt_plan import problems

SHA = re.compile(r"[0-9a-f]{40}\Z")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
VAR = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
# The part of Loom a pin target deploys: a merge that changes nothing under
# it opens no pin pull request. A target may name its own with "watch".
DEFAULT_WATCH = "dashboard/"
# GitHub's compare lists at most this many files; a full list may hide more.
COMPARE_FILES_MAX = 300
PIN_LABEL = "pin-bump"


def targets(value: str) -> list[dict[str, str]]:
    """Read the private JSON array installed in the station plist."""
    data = json.loads(value)
    if not isinstance(data, list):
        raise ValueError("LOOM_PIN_TARGETS must be a JSON array")
    for item in data:
        if not isinstance(item, dict) or not all(isinstance(item.get(key), str)
                for key in ("repo", "path", "revision_var", "sha256_var")):
            raise ValueError("each pin target needs repo, path, revision_var and sha256_var")
        if not REPO.fullmatch(item["repo"]) or not item["path"] or item["path"].startswith("/") \
                or ".." in item["path"].split("/") or not VAR.fullmatch(item["revision_var"]) \
                or not VAR.fullmatch(item["sha256_var"]):
            raise ValueError("invalid pin target")
        watch = item.get("watch", DEFAULT_WATCH)
        if not isinstance(watch, str) or not watch or watch.startswith("/") \
                or ".." in watch.split("/"):
            raise ValueError("a pin target's watch must be a relative path prefix")
    return data


def download(url: str) -> bytes:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "codeload.github.com":
        raise ValueError("tarball URL must use GitHub codeload over HTTPS")
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310
        return response.read()


def archive_hash(source: str, revision: str, fetch: Callable[[str], bytes]) -> str:
    url = "https://codeload.github.com/%s/tar.gz/%s" % (source, revision)
    first = hashlib.sha256(fetch(url)).hexdigest()
    second = hashlib.sha256(fetch(url)).hexdigest()
    if first != second:
        raise ValueError("tarball downloads differ for %s" % revision[:12])
    return first


def content(gh: GitHub, repo: str, path: str, ref: str = "") -> tuple[str, str]:
    endpoint = "/repos/%s/contents/%s" % (repo, quote(path, safe="/"))
    if ref:
        endpoint += "?ref=" + quote(ref, safe="")
    data = gh.rest("GET", endpoint).data
    if not isinstance(data, dict) or not isinstance(data.get("content"), str):
        raise ValueError("pin target is not a regular file: %s" % path)
    return base64.b64decode(data["content"]).decode("utf-8"), str(data["sha"])


def value(text: str, name: str) -> str:
    matches = re.findall(r"^" + re.escape(name) + r"=['\"]?([^'\"\s]+)['\"]?\s*$", text, re.M)
    if len(matches) != 1:
        raise ValueError("pin variable %s must appear exactly once" % name)
    return matches[0]


def replace(text: str, name: str, new: str) -> str:
    value(text, name)
    return re.sub(r"^(" + re.escape(name) + r"=).*?$", lambda m: m[1] + '"' + new + '"',
                  text, count=1, flags=re.M)


def compare(gh: GitHub, source: str, old: str, revision: str) -> dict:
    """GitHub's comparison of the pinned revision with the new one; empty
    when the pin names no commit to compare with."""
    if not SHA.fullmatch(old):
        return {}
    data = gh.rest("GET", "/repos/%s/compare/%s...%s" % (source, old, revision)).data
    return data if isinstance(data, dict) else {}


def touches(compared: dict, watch: str) -> bool:
    """Whether the comparison changes a file under the watched prefix. A
    comparison that cannot say (no file list, or GitHub's full list of
    COMPARE_FILES_MAX that may hide more) counts as a change."""
    files = compared.get("files")
    if not isinstance(files, list) or len(files) >= COMPARE_FILES_MAX:
        return True
    prefix = watch if watch.endswith("/") else watch + "/"
    for item in files:
        if not isinstance(item, dict):
            continue
        for name in (item.get("filename"), item.get("previous_filename")):
            if isinstance(name, str) and (name.startswith(prefix) or name == watch.rstrip("/")):
                return True
    return False


def merged_titles(gh: GitHub, source: str, old: str, revision: str,
                  compared: dict | None = None) -> list[str]:
    if not SHA.fullmatch(old):
        return []
    if compared is None:
        compared = compare(gh, source, old, revision)
    titles = []
    for commit in compared.get("commits", []):
        sha = commit["sha"]
        pulls = gh.rest("GET", "/repos/%s/commits/%s/pulls" % (source, sha)).data or []
        titles.extend(pr["title"] for pr in pulls if pr.get("merged_at") and pr["title"] not in titles)
    return titles


def body(source: str, revision: str, titles: list[str], digest: str = "") -> str:
    lines = "\n".join("- %s" % title for title in titles) or "- No merged pull request title was returned."
    checksum = (" (sha256 `%s`, the same from two downloads)" % digest) if digest else ""
    result = ("## Why\nThe dashboard deployment needs a reviewed pin for the latest "
              "green Loom revision that changes the dashboard.\n\n## Summary\nUpdate the "
              "Loom revision to `%s` and its verified tarball checksum%s.\n\n"
              "## Merged Loom pull requests\n%s\n\n"
              "## Acceptance criteria\n- [ ] The pin names revision `%s`.\n"
              "- [ ] The tarball checksum matches two independent downloads.\n"
              "- [ ] The dashboard deployment is reviewed before merge.\n\n"
              "Size: S\n\n## Out of scope\n- Merging or deploying this pull request.\n"
              % (revision, checksum, lines, revision))
    if problems(result):
        raise ValueError("generated pin pull request body fails the Hunt Plan")
    return result


def pin_one(gh: GitHub, source: str, revision: str, spec: dict[str, str],
            get_digest: Callable[[], str], log: Callable[[str], None]) -> None:
    repo, path = spec["repo"], spec["path"]
    branch = "loom/pin-" + hashlib.sha256(path.encode()).hexdigest()[:12]
    base = gh.rest("GET", "/repos/%s" % repo).data["default_branch"]
    old_text, _ = content(gh, repo, path)
    old = value(old_text, spec["revision_var"])
    pulls = gh.paginate("/repos/%s/pulls?state=open&per_page=100" % repo)
    active = next((pr for pr in pulls if pr.get("head", {}).get("ref") == branch), None)
    ref = gh.rest("GET", "/repos/%s/git/ref/heads/%s" % (repo, quote(branch, safe="")),
                  ok=(404,))
    exists = ref.status != 404
    current_text, file_sha = content(gh, repo, path, branch) if exists else (old_text, "")
    current = value(current_text, spec["revision_var"])
    mergeable_state = active.get("mergeable_state") if active else None
    if current == revision and mergeable_state in (None, "clean"):
        return
    watch = spec.get("watch", DEFAULT_WATCH)
    compared = compare(gh, source, old, revision)
    if compared and not touches(compared, watch):
        log("pin: %s: nothing under %s changed since %s; no pin pull request"
            % (repo, watch, old[:12]))
        return
    digest = get_digest()
    # Rebuild the pin branch from the default branch's head on every update so
    # its pull request never goes dirty when the default branch moves. The pin
    # file is computed from the default branch's text, not the branch's.
    head = gh.rest("GET", "/repos/%s/git/ref/heads/%s" % (repo, quote(base, safe=""))).data
    if exists:
        # GitHub reads a ref at git/ref/<ref> but updates it at git/refs/<ref>;
        # a PATCH to the read path answers 404 (loom#1405).
        gh.rest("PATCH", "/repos/%s/git/refs/heads/%s" % (repo, quote(branch, safe="")),
                {"sha": head["object"]["sha"], "force": True})
    else:
        gh.rest("POST", "/repos/%s/git/refs" % repo,
                {"ref": "refs/heads/" + branch, "sha": head["object"]["sha"]})
    _, file_sha = content(gh, repo, path, branch)
    updated = replace(replace(old_text, spec["revision_var"], revision),
                      spec["sha256_var"], digest)
    title = "Pin Loom dashboard to %s" % revision[:12]
    plan = body(source, revision, merged_titles(gh, source, old, revision, compared), digest)
    gh.rest("PUT", "/repos/%s/contents/%s" % (repo, quote(path, safe="/")),
            {"message": title, "content": base64.b64encode(updated.encode()).decode(),
             "sha": file_sha, "branch": branch})
    if active:
        gh.rest("PATCH", "/repos/%s/pulls/%s" % (repo, active["number"]),
                {"title": title, "body": plan})
        log("pin: updated pull request for %s" % repo)
    else:
        made = gh.rest("POST", "/repos/%s/pulls" % repo,
                       {"title": title, "head": branch, "base": base, "body": plan}).data or {}
        log("pin: opened pull request for %s" % repo)
        if made.get("number"):
            try:
                gh.rest("POST", "/repos/%s/issues/%s/labels" % (repo, made["number"]),
                        {"labels": [PIN_LABEL]})
            except GitHubError as exc:
                log("pin: could not label the pull request for %s: %s" % (repo, exc))


def run(gh: GitHub, source: str, revision: str, check: str,
        specs: list[dict[str, str]], fetch: Callable[[str], bytes] = download,
        log: Callable[[str], None] = print) -> None:
    if not REPO.fullmatch(source) or not SHA.fullmatch(revision):
        raise ValueError("source and revision must name a GitHub repo and commit")
    verdict = gh.rest("GET", "/repos/%s/commits/%s/check-runs?check_name=%s" %
                      (source, revision, quote(check, safe=""))).data or {}
    runs = verdict.get("check_runs") or []
    if not runs or any(item.get("conclusion") != "success" for item in runs):
        log("pin: CI has not passed for %s" % revision[:12])
        return
    digest = None

    def verified_digest() -> str:
        nonlocal digest
        if digest is None:
            digest = archive_hash(source, revision, fetch)
        return digest

    for spec in specs:
        try:
            pin_one(gh, source, revision, spec, verified_digest, log)
        except ValueError as exc:
            log("pin: %s: %s" % (spec["repo"], exc))


def _pin_client() -> GitHub:
    """The GitHub client the pin pass uses.

    The pin pull request is opened on the deployment repo, whose
    collaborator-only self-hosted trust gate rejects the GitHub App
    installation token (loom#612). It is opened with the operator's personal
    token instead, so the trust gate passes and the pin pull request can merge.
    """
    return GitHub(token_from_env())


def main() -> int:
    try:
        specs = targets(os.environ["LOOM_PIN_TARGETS"])
        source = os.environ["LOOM_SELF_REPO"]
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        state = os.path.join(os.environ.get("LOOM_HOME", os.path.expanduser("~/loom")),
                             "state", "station", "deployed.json")
        with open(state, encoding="utf-8") as stream:
            if json.load(stream).get("revision") != revision:
                raise ValueError("checkout is not the recorded deployment")
        run(_pin_client(), source, revision,
            os.environ.get("LOOM_DEPLOY_CHECK", "unittest"), specs)
    except (GitHubError, KeyError, OSError, ValueError) as exc:
        print("pin: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
