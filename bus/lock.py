"""The brain's tick lock: a branch whose commit message names the holder.

Creating a ref that exists fails with 422, so only one holder wins.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from .github import GitHub, GitHubError

LOCK_BRANCH = "loom-lock/brain"
HOLDER_RE = re.compile(r"[A-Za-z0-9._-]+")
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _check_holder(holder: str) -> None:
    if not isinstance(holder, str) or not HOLDER_RE.fullmatch(holder):
        raise ValueError("holder may only use letters, digits, '.', '_' and '-'")


def _utc(now: Optional[datetime]) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _read_lock(gh: GitHub, repo: str) -> Optional[Tuple[str, Optional[datetime]]]:
    """Return (holder, expiry) of the current lock, or None when absent."""
    ref = gh.rest("GET", "/repos/%s/git/ref/heads/%s" % (repo, LOCK_BRANCH),
                  ok=(404,))
    if ref.status == 404:
        return None
    sha = ref.data["object"]["sha"]
    commit = gh.rest("GET", "/repos/%s/git/commits/%s" % (repo, sha), ok=(404,))
    if commit.status == 404:
        return None
    message = commit.data.get("message", "")
    holder = re.search(r"holder=(\S+)", message)
    expires = re.search(r"expires=(\S+)", message)
    when = None
    if expires:
        try:
            when = datetime.strptime(expires.group(1), TIME_FORMAT).replace(
                tzinfo=timezone.utc)
        except ValueError:
            when = None
    return (holder.group(1) if holder else "", when)


def _create_ref(gh: GitHub, repo: str, sha: str) -> bool:
    resp = gh.rest("POST", "/repos/%s/git/refs" % repo,
                   {"ref": "refs/heads/" + LOCK_BRANCH, "sha": sha}, ok=(422,))
    return resp.status != 422


def acquire(gh: GitHub, repo: str, holder: str, minutes: int = 15,
            now: Optional[datetime] = None) -> Tuple[bool, str]:
    _check_holder(holder)
    now = _utc(now)
    main = gh.rest("GET", "/repos/%s/git/ref/heads/main" % repo)
    base = main.data["object"]["sha"]
    tree = gh.rest("GET", "/repos/%s/git/commits/%s" % (repo, base)).data["tree"]["sha"]
    expires = (now + timedelta(minutes=minutes)).strftime(TIME_FORMAT)
    commit = gh.rest("POST", "/repos/%s/git/commits" % repo, {
        "message": "holder=%s expires=%s" % (holder, expires),
        "tree": tree,
        "parents": [],
    })
    sha = commit.data["sha"]
    if _create_ref(gh, repo, sha):
        return True, holder
    current = _read_lock(gh, repo)
    if current is not None:
        _, when = current
        # An unreadable expiry counts as held: never steal a lock we cannot read.
        if when is None or when > now:
            return False, current[0]
        gh.rest("DELETE", "/repos/%s/git/refs/heads/%s" % (repo, LOCK_BRANCH),
                ok=(404, 422))
    if _create_ref(gh, repo, sha):
        return True, holder
    current = _read_lock(gh, repo)
    return False, current[0] if current else ""


def release(gh: GitHub, repo: str, holder: str) -> bool:
    _check_holder(holder)
    current = _read_lock(gh, repo)
    if current is None or current[0] != holder or current[1] is None or current[1] <= datetime.now(timezone.utc):
        return False
    # Keep the lock ref as the durable last-tick record. Expire it now so the
    # next holder can replace it immediately through acquire's stale-lock path.
    ref_path = "/repos/%s/git/ref/heads/%s" % (repo, LOCK_BRANCH)
    ref = gh.rest("GET", ref_path)
    sha = ref.data["object"]["sha"]
    previous = gh.rest("GET", "/repos/%s/git/commits/%s" % (repo, sha)).data
    message = re.sub(r"expires=\S+", "expires=" + datetime.now(timezone.utc).strftime(TIME_FORMAT),
                     previous.get("message", ""))
    commit = gh.rest("POST", "/repos/%s/git/commits" % repo, {
        "message": message, "tree": previous["tree"]["sha"], "parents": [sha]})
    # Updated at git/refs/<ref>: the git/ref/<ref> read path answers 404 to a PATCH.
    gh.rest("PATCH", "/repos/%s/git/refs/heads/%s" % (repo, LOCK_BRANCH),
            {"sha": commit.data["sha"], "force": True})
    return True
