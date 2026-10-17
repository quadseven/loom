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

from .github import GitHub, GitHubError, auth_from_env
from .hunt_plan import problems

SHA = re.compile(r"[0-9a-f]{40}\Z")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
VAR = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


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


def merged_titles(gh: GitHub, source: str, old: str, revision: str) -> list[str]:
    if not SHA.fullmatch(old):
        return []
    compared = gh.rest("GET", "/repos/%s/compare/%s...%s" % (source, old, revision)).data or {}
    titles = []
    for commit in compared.get("commits", []):
        sha = commit["sha"]
        pulls = gh.rest("GET", "/repos/%s/commits/%s/pulls" % (source, sha)).data or []
        titles.extend(pr["title"] for pr in pulls if pr.get("merged_at") and pr["title"] not in titles)
    return titles


def body(source: str, revision: str, titles: list[str]) -> str:
    lines = "\n".join("- %s" % title for title in titles) or "- No merged pull request title was returned."
    result = ("## Why\nThe dashboard deployment needs a reviewed pin for the latest "
              "green Loom revision.\n\n## Summary\nUpdate the Loom revision and verified "
              "tarball checksum.\n\n## Merged Loom pull requests\n%s\n\n"
              "## Acceptance criteria\n- [ ] The pin names revision `%s`.\n"
              "- [ ] The tarball checksum matches two independent downloads.\n"
              "- [ ] The dashboard deployment is reviewed before merge.\n\n"
              "Size: S\n\n## Out of scope\n- Merging or deploying this pull request.\n"
              % (lines, revision))
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
    pulls = gh.rest("GET", "/repos/%s/pulls?state=open&per_page=100" % repo).data or []
    active = next((pr for pr in pulls if pr.get("head", {}).get("ref") == branch), None)
    ref = gh.rest("GET", "/repos/%s/git/ref/heads/%s" % (repo, quote(branch, safe="")),
                  ok=(404,))
    exists = ref.status != 404
    current_text, file_sha = content(gh, repo, path, branch) if exists else (old_text, "")
    if value(current_text, spec["revision_var"]) == revision and (active or old_text == current_text):
        return
    digest = get_digest()
    updated = replace(replace(current_text, spec["revision_var"], revision),
                      spec["sha256_var"], digest)
    title = "Pin Loom dashboard to %s" % revision[:12]
    plan = body(source, revision, merged_titles(gh, source, old, revision))
    if not exists:
        head = gh.rest("GET", "/repos/%s/git/ref/heads/%s" % (repo, quote(base, safe=""))).data
        gh.rest("POST", "/repos/%s/git/refs" % repo,
                {"ref": "refs/heads/" + branch, "sha": head["object"]["sha"]})
        _, file_sha = content(gh, repo, path, branch)
    gh.rest("PUT", "/repos/%s/contents/%s" % (repo, quote(path, safe="/")),
            {"message": title, "content": base64.b64encode(updated.encode()).decode(),
             "sha": file_sha, "branch": branch})
    if active:
        gh.rest("PATCH", "/repos/%s/pulls/%s" % (repo, active["number"]),
                {"title": title, "body": plan})
        log("pin: updated pull request for %s" % repo)
    else:
        gh.rest("POST", "/repos/%s/pulls" % repo,
                {"title": title, "head": branch, "base": base, "body": plan})
        log("pin: opened pull request for %s" % repo)


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
        run(GitHub(auth_from_env()), source, revision,
            os.environ.get("LOOM_DEPLOY_CHECK", "unittest"), specs)
    except (GitHubError, KeyError, OSError, ValueError) as exc:
        print("pin: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
