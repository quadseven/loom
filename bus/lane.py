"""GitHub-pulled work for remote lanes."""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote

from . import brain, change_class, hunt_plan, notify, pr_repair, tickets
from .github import GitHub, GitHubError, RateLimited, auth_from_env
from .labels import REMOTE_LANES

# One claim comment per ticket claim: the first time is the claim, and each
# heartbeat edits the same comment to add the newest beat as the third field
# and keep the claim time as the fourth. A comment from before that has the
# one time and no fourth field.
CLAIM_RE = re.compile(r"^<!-- loom:remote-claim v1 ([^ ]+) ([0-9]+) ([0-9.]+)(?: ([0-9.]+))? -->$")
CLAIM_AGE = 2 * 3600
PR_AGE = 3 * 3600
# push_branch's ref read: the sentinel a 404 leaves behind, so an empty or
# malformed 200 payload is never mistaken for "the branch is not there".
_NO_REF = object()


def _names(issue: Dict[str, Any]) -> List[str]:
    return [str(x.get("name") if isinstance(x, dict) else x)
            for x in issue.get("labels") or []]


def _missing_hold_label(issue: Dict[str, Any], lane: str) -> Optional[str]:
    """The first label a ticket held by this lane must carry that it lacks:
    loom:running, then the lane's own label. None when it carries both."""
    names = _names(issue)
    for label in ("loom:running", "lane:" + lane):
        if label not in names:
            return label
    return None


def _own_login(gh: GitHub) -> Optional[str]:
    """The App login this lane writes as, or None on a personal token or when
    GET /app cannot be read. Only this App can author as it, so trusting it
    lets no stranger in (loom#2008)."""
    login_of = getattr(gh, "app_login", None)
    if login_of is None:
        return None
    try:
        login = login_of()
    except RateLimited:
        raise
    except GitHubError:
        return None
    return login if isinstance(login, str) and login else None


def _comments(gh: GitHub, repo: str, number: int) -> List[Dict[str, Any]]:
    """The comments a LOOM_BRIEF_AUTHORS login or the lane's own App wrote: a
    stranger's claim or heartbeat marker neither holds a ticket nor is edited
    as the lane's. The lane's own login counts even when LOOM_BRIEF_AUTHORS
    omits it, or every beat would miss its last marker and post a new comment
    (loom#2008)."""
    authors = brain.trusted_authors()
    own = _own_login(gh)
    if own and own.lower() not in {a.lower() for a in authors}:
        authors = authors + [own]
    return brain.trusted_comments(gh.paginate("/repos/%s/issues/%d/comments" % (repo, number)),
                                  authors)


def _claims(comments: Sequence[Dict[str, Any]], lane: str) -> List[Tuple[int, float, float]]:
    """(index, newest beat, first claim time) for each claim comment of the lane."""
    out = []
    for index, comment in enumerate(comments):
        match = CLAIM_RE.fullmatch(str(comment.get("body") or "").strip())
        if match and match.group(1) == lane:
            try:
                beat = float(match.group(3))
                first = float(match.group(4)) if match.group(4) else beat
            except ValueError:
                continue
            out.append((index, beat, first))
    return out


def _claim_body(lane: str, number: int, beat: float, first: Optional[float] = None) -> str:
    tail = "" if first is None or first == beat else " %.3f" % first
    return "<!-- loom:remote-claim v1 %s %d %.3f%s -->" % (lane, number, beat, tail)


def _hand_back(gh: GitHub, repo: str, issue: Dict[str, Any], lane: str,
               reason: str) -> None:
    number = int(issue["number"])
    labels = [x for x in _names(issue) if x not in ("loom:running", "lane:" + lane)]
    labels.append("loom:queued")
    gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number), {"labels": labels})
    notify.post(gh, repo, number, "[transient] %s" % reason, notify.FAILURE)


def _pull_for(gh: GitHub, repo: str, branch: str) -> Optional[Dict[str, Any]]:
    """The pull request whose head is the ticket branch, open or closed, or
    None. One request: GitHub filters by head instead of listing every pull."""
    owner = repo.split("/", 1)[0]
    found = gh.paginate("/repos/%s/pulls?state=all&head=%s"
                        % (repo, quote("%s:%s" % (owner, branch), safe="")))
    return next((p for p in found if isinstance(p, dict)), None)


def _open_or_merged_pull(gh: GitHub, repo: str, branch: str) -> Optional[Dict[str, Any]]:
    owner = repo.split("/", 1)[0]
    found = gh.paginate("/repos/%s/pulls?state=all&head=%s"
                        % (repo, quote("%s:%s" % (owner, branch), safe="")))
    for p in found:
        if isinstance(p, dict) and (p.get("state") == "open"
                                    or p.get("merged_at") is not None
                                    or p.get("merged") is True):
            return p
    return None


def _refs_line(repo: str, number: int) -> "re.Pattern[str]":
    """A `Refs #N` or `Refs OWNER/NAME#N` line of a pull request body that
    names this ticket in this repo (the form the station and the merge gate
    read). A body that only mentions the number elsewhere does not match."""
    return re.compile(r"^Refs (?:%s)?#%d\b" % (re.escape(repo), number), re.M)


def _closed_pulls(gh: GitHub, repo: str) -> List[Dict[str, Any]]:
    """The most recently updated closed pull requests of the repo, one page,
    the window the station's own reconciliation reads."""
    data = gh.rest("GET", "/repos/%s/pulls?state=closed&sort=updated"
                   "&direction=desc&per_page=100" % repo).data
    return [p for p in data if isinstance(p, dict)] if isinstance(data, list) else []


def _merged_pull_naming(repo: str, number: int,
                        closed: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """A merged pull request whose body carries a Refs line naming the ticket,
    whatever its branch is called: work that lands from a non-ticket branch
    (loom#412, loom#414) is invisible to the branch-name match."""
    line = _refs_line(repo, number)
    return next((p for p in closed
                 if (p.get("merged_at") is not None or p.get("merged") is True)
                 and line.search(str(p.get("body") or ""))), None)


def _to_pr(gh: GitHub, repo: str, issue: Dict[str, Any], lane: str) -> None:
    labels = [x for x in _names(issue) if x not in ("loom:running", "loom:queued")]
    gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, int(issue["number"])),
            {"labels": labels + ["loom:pr"]})


def _recover_and_count(gh: GitHub, repos: Sequence[str], lane: str,
                       now: float) -> int:
    """Tickets the remote lane holds and still works on. A held ticket whose
    branch already has a pull request moves to loom:pr; one with no pull
    request goes back to the queue when its last heartbeat is older than
    CLAIM_AGE or its first claim older than PR_AGE. The branch name is the
    primary match; when it finds nothing, a merged pull request whose body
    names the ticket in a Refs line counts the same way, so work that landed
    from another branch does not strand the ticket (loom#1149)."""
    active = 0
    for repo in repos:
        closed: Optional[List[Dict[str, Any]]] = None
        found = gh.paginate("/repos/%s/issues?state=open&labels=loom:running,lane:%s"
                            % (repo, quote(lane, safe="")))
        for issue in found:
            if not isinstance(issue, dict) or "pull_request" in issue:
                continue
            # The newest claim comment is this claim: an earlier claim of the
            # ticket, handed back since, must not age the one held now.
            held = _claims(_comments(gh, repo, int(issue["number"])), lane)
            if not held:
                continue
            _, last_beat, first_claim = held[-1]
            branch = tickets.branch_for(int(issue["number"]), str(issue.get("title") or ""))
            if _pull_for(gh, repo, branch) is not None:
                _to_pr(gh, repo, issue, lane)
                continue
            if closed is None:
                closed = _closed_pulls(gh, repo)
            if _merged_pull_naming(repo, int(issue["number"]), closed) is not None:
                _to_pr(gh, repo, issue, lane)
                continue
            if now - last_beat > CLAIM_AGE or now - first_claim > PR_AGE:
                _hand_back(gh, repo, issue, lane,
                           "remote lane claim expired without a pull request")
                continue
            active += 1
    return active


def _candidates(gh: GitHub, repos: Sequence[str], lane: str) -> List[Tuple[str, Dict[str, Any]]]:
    """(repo, issue) for each public queued ticket assigned to the lane in one
    of its tiers, oldest first."""
    found: List[Tuple[str, str, Dict[str, Any]]] = []
    for repo in repos:
        if not tickets.REPO_RE.fullmatch(repo):
            raise ValueError("expected OWNER/NAME, got %r" % repo)
        info = gh.rest("GET", "/repos/%s" % repo).data
        if not isinstance(info, dict) or info.get("private") is not False:
            continue
        for item in gh.paginate("/repos/%s/issues?state=open&labels=loom:queued,lane:%s"
                                % (repo, quote(lane, safe=""))):
            if _takes(item, lane):
                found.append((str(item.get("created_at") or ""), repo, item))
    found.sort(key=lambda row: (row[0], row[1], int(row[2]["number"])))
    return [(repo, item) for _, repo, item in found]


def _takes(item: Any, lane: str) -> bool:
    if not isinstance(item, dict) or "pull_request" in item:
        return False
    names = _names(item)
    if "loom:queued" not in names or "lane:" + lane not in names:
        return False
    try:
        headers = tickets.parse(item.get("body"))[0]
    except ValueError:
        return False
    if not headers:
        return False  # no brief header at all: not a ticket a lane can run
    # The factory's reading (its default tier for a ticket with no
    # Difficulty: line), so a ticket it reserved for this lane is one the
    # lane takes (a reserved ticket sat unclaimed under four Muse lanes).
    from .factory import difficulty_of
    return difficulty_of(item) in REMOTE_LANES[lane]["tiers"]


def _take(gh: GitHub, repo: str, issue: Dict[str, Any], lane: str,
          now: float) -> Dict[str, Any]:
    number = int(issue["number"])
    labels = [x for x in _names(issue) if x != "loom:queued"] + ["loom:running"]
    gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number), {"labels": labels})
    notify.post(gh, repo, number, _claim_body(lane, number, now), notify.RECORD)
    info = gh.rest("GET", "/repos/%s" % repo).data
    default = str((info.get("default_branch") if isinstance(info, dict) else "") or "main")
    return {"repo": repo, "number": number, "base": default,
            "brief": tickets.render_brief(repo, issue, lane)}


def claim(gh: GitHub, repos: Sequence[str], lane: str = "muse",
          now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Claim the oldest public queued issue assigned to a configured lane."""
    if not repos:
        raise ValueError("claim requires --repos or LOOM_REPOS")
    if lane not in REMOTE_LANES:
        raise ValueError("unknown remote lane: %s" % lane)
    now = time.time() if now is None else now
    active = _recover_and_count(gh, repos, lane, now)
    candidates = _candidates(gh, repos, lane)
    skipped: List[str] = []
    claimed_result: Optional[Dict[str, Any]] = None

    if active >= REMOTE_LANES[lane]["slots"]:
        for _repo, issue in candidates:
            skipped.append("#%s skipped: lane slots full" % issue["number"])
    else:
        for repo, issue in candidates:
            branch = tickets.branch_for(int(issue["number"]), str(issue.get("title") or ""))
            pr = _open_or_merged_pull(gh, repo, branch)
            if pr is not None:
                skipped.append("#%s skipped: branch %s already has pull request #%s" %
                               (issue["number"], branch, pr.get("number", "")))
                continue
            recent = [at for _, at, _ in _claims(_comments(gh, repo, int(issue["number"])), lane)
                      if now - at < CLAIM_AGE]
            # One claim per ticket, whatever the lane's slot count: with
            # several slots, a ticket a hand-back left queued while its claim
            # is fresh would otherwise be claimed again by another run.
            if recent:
                skipped.append("#%s skipped: claim active" % issue["number"])
                continue
            claimed_result = _take(gh, repo, issue, lane, now)
            break

    summary = "scanned %d repos, found %d candidates" % (len(repos), len(candidates))
    if skipped:
        summary += ": " + "; ".join(skipped)
    print(summary, file=sys.stderr)
    return claimed_result


def heartbeat(gh: GitHub, repo: str, number: int, lane: str = "muse",
              now: Optional[float] = None) -> None:
    """Record a beat on the ticket's claim. The claim comment is edited in
    place, so a beat sends no email and the ticket gains no comment; a ticket
    with no claim comment of this lane gets one."""
    if lane not in REMOTE_LANES:
        raise ValueError("unknown remote lane: %s" % lane)
    now = time.time() if now is None else now
    issue = gh.rest("GET", "/repos/%s/issues/%d" % (repo, number)).data or {}
    if _missing_hold_label(issue, lane) is not None:
        raise ValueError("ticket is not held by this remote lane")
    comments = _comments(gh, repo, number)
    held = [c for c in _claims(comments, lane) if comments[c[0]].get("id")]
    if held:
        index, _, first = held[-1]
        gh.rest("PATCH", "/repos/%s/issues/comments/%d" % (repo, int(comments[index]["id"])),
                {"body": _claim_body(lane, number, now, first)})
        return
    notify.post(gh, repo, number, _claim_body(lane, number, now), notify.RECORD)


def confirm_claim(gh: GitHub, repo: str, number: int, lane: str = "muse",
                  now: Optional[float] = None) -> Dict[str, Any]:
    """Confirm a ticket reference is real and held by this lane before a run
    implements from it. A run takes its ticket from claim's own return; any
    other reference (a hand-off, a note, a message) is confirmed here first,
    so a reference to a ticket that does not exist stops the run before it
    spends work (loom#2136). Returns the ticket's repo, number, title and
    brief headers. Raises ValueError with the reason when the ticket is not
    this lane's to work. (Confirm, not verify: the glossary reserves Verify
    for proof on the live system after a change ships.)"""
    if lane not in REMOTE_LANES:
        raise ValueError("unknown remote lane: %s" % lane)
    now = time.time() if now is None else now
    try:
        issue = gh.rest("GET", "/repos/%s/issues/%d" % (repo, number)).data or {}
    except GitHubError as err:
        if err.status == 404:
            raise ValueError("ticket %s#%d does not exist" % (repo, number)) from err
        raise
    if "pull_request" in issue:
        raise ValueError("ticket %s#%d is a pull request, not a ticket" % (repo, number))
    if issue.get("state") != "open":
        raise ValueError("ticket %s#%d is not open" % (repo, number))
    missing = _missing_hold_label(issue, lane)
    if missing == "loom:running":
        raise ValueError("ticket %s#%d is not loom:running" % (repo, number))
    if missing is not None:
        raise ValueError("ticket %s#%d is not held by lane %s" % (repo, number, lane))
    live = False
    for _, beat, _ in _claims(_comments(gh, repo, number), lane):
        live = now - beat <= CLAIM_AGE
    if not live:
        raise ValueError("ticket %s#%d has no live claim comment from lane %s"
                         % (repo, number, lane))
    try:
        headers = tickets.parse(issue.get("body"))[0]
    except ValueError as err:
        raise ValueError("ticket %s#%d has an unreadable brief: %s"
                         % (repo, number, err)) from err
    if not headers:
        raise ValueError("ticket %s#%d has no brief headers" % (repo, number))
    return {"repo": repo, "number": number,
            "title": str(issue.get("title") or ""),
            "headers": headers}


def lane_heartbeat(gh: GitHub, repo: str, lane: str = "muse",
                   now: Optional[float] = None) -> None:
    """Publish remote worker availability on the station's lane status issue."""
    if lane not in REMOTE_LANES:
        raise ValueError("unknown remote lane: %s" % lane)
    now = time.time() if now is None else now
    issues = gh.paginate("/repos/%s/issues?state=open&labels=loom:lanes" % repo)
    issue = next((x for x in issues if isinstance(x, dict) and "pull_request" not in x), None)
    if issue is None:
        issue = gh.rest("POST", "/repos/%s/issues" % repo, {
            "title": "Loom lane status", "labels": ["loom:lanes"],
            "body": "The station keeps lane status current here."}).data
    if not isinstance(issue, dict) or "number" not in issue:
        raise ValueError("could not locate the lane status issue")
    body = "<!-- loom:remote-heartbeat v1 %s %.3f -->" % (lane, now)
    # One comment per lane, edited in place: a heartbeat every tick must not
    # grow the status issue by a comment each time.
    mine = re.compile(r"^<!-- loom:remote-heartbeat v1 %s [0-9.]+ -->$" % re.escape(lane))
    for comment in _comments(gh, repo, int(issue["number"])):
        if mine.fullmatch(str(comment.get("body") or "").strip()) and comment.get("id"):
            gh.rest("PATCH", "/repos/%s/issues/comments/%d" % (repo, int(comment["id"])),
                    {"body": body})
            return
    notify.post(gh, repo, issue["number"], body, notify.RECORD)


def _git(*args: str) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    if result.returncode:
        raise OSError("git %s failed: %s" % (args[0], result.stderr.strip()))
    return result.stdout.strip()


def _git_bytes(*args: str) -> bytes:
    result = subprocess.run(["git", *args], capture_output=True, check=False)
    if result.returncode:
        raise OSError("git %s failed" % (args[0],))
    return result.stdout


def _push_commit(gh: GitHub, repo: str, local_sha: str, local_parent: str,
                 api_parent: str) -> str:
    """Recreate one local commit through the git-database API. Returns the new
    remote commit SHA. Blob and tree SHAs are content-addressed, so re-running
    recreates identical objects."""
    tree_updates = []
    # --no-renames: a rename arrives as a D of the old path and an A of the
    # new one, never as one "R100 old new" entry. -z: fields are NUL-separated
    # and paths unquoted, so a tab or non-ASCII byte in a name stays intact.
    fields = _git("diff", "--name-status", "--no-renames", "-z",
                  local_parent, local_sha).split("\0")
    # strict=False: the output ends with a NUL, so the split leaves one empty
    # trailing field and the two slices differ in length by one.
    for status, path in zip(fields[0::2], fields[1::2], strict=False):
        if status.startswith("D"):
            tree_updates.append({"path": path, "mode": "100644",
                                 "type": "blob", "sha": None})
            continue
        blob_sha = _git("rev-parse", local_sha + ":" + path)
        content = _git_bytes("cat-file", "blob", blob_sha)
        made = gh.rest("POST", "/repos/%s/git/blobs" % repo, {
            "content": base64.b64encode(content).decode("ascii"),
            "encoding": "base64"}).data or {}
        if made.get("sha") != blob_sha:
            raise GitHubError("blob SHA mismatch for %s" % path)
        mode = _git("ls-tree", local_sha, path).split()[0]
        tree_updates.append({"path": path, "mode": mode,
                             "type": "blob", "sha": blob_sha})
    base_tree = _git("rev-parse", local_parent + "^{tree}")
    new_tree = (gh.rest("POST", "/repos/%s/git/trees" % repo, {
        "base_tree": base_tree, "tree": tree_updates}).data or {}).get("sha")
    if new_tree != _git("rev-parse", local_sha + "^{tree}"):
        raise GitHubError("tree SHA mismatch for commit %s" % local_sha[:12])
    author = {
        "name": _git("log", "-1", "--format=%an", local_sha),
        "email": _git("log", "-1", "--format=%ae", local_sha),
        "date": _git("log", "-1", "--format=%aI", local_sha),
    }
    commit = (gh.rest("POST", "/repos/%s/git/commits" % repo, {
        "message": _git("log", "-1", "--format=%B", local_sha),
        "tree": new_tree, "parents": [api_parent],
        "author": author, "committer": author}).data or {})
    if not commit.get("sha"):
        raise GitHubError("GitHub did not return the created commit")
    return str(commit["sha"])


def push_branch(gh: GitHub, repo: str, number: int) -> str:
    """Push the current branch through the git-database API and return the
    remote HEAD SHA. The lane has no git HTTPS credential, so `git push`
    cannot work; the API accepts the same token the GitHub client carries.
    The current branch must be the ticket's branch. Idempotent: pushing an
    up-to-date branch changes nothing."""
    issue = gh.rest("GET", "/repos/%s/issues/%d" % (repo, number)).data or {}
    branch = tickets.branch_for(number, str(issue.get("title") or ""))
    current = _git("branch", "--show-current")
    if current != branch:
        raise ValueError("current branch %r does not match the ticket branch %r"
                         % (current, branch))
    local_head = _git("rev-parse", "HEAD")
    # Read the branch through the singular ref endpoint. The plural
    # /git/refs/heads/{branch} path prefix-matches, so when earlier attempt
    # branches (the -r3/-r4 shape) share the branch name as a prefix it
    # answers a list, and the read crashed on .get (loom#2160). The singular
    # endpoint answers the one exact ref, or 404 when the branch is not
    # there; the PATCH below still updates at the plural path, which is the
    # endpoint GitHub defines for updates.
    try:
        ref = gh.rest("GET", "/repos/%s/git/ref/heads/%s"
                      % (repo, quote(branch, safe=""))).data
    except GitHubError as err:
        if err.status != 404:
            raise
        ref = _NO_REF
    obj = ref.get("object") if isinstance(ref, dict) else None
    if ref is _NO_REF:
        remote_head = ""
    elif (isinstance(obj, dict) and obj.get("sha")
            and ref.get("ref") in (None, "refs/heads/" + branch)):
        remote_head = str(obj["sha"])
    else:
        # A 200 that is not the one ref object (a list, an empty body, a
        # payload with no commit, or one naming a different ref) must stop
        # the push loudly: guessing a remote head here could update the
        # wrong ref or silently strand the lane's commits behind a success
        # report.
        raise GitHubError("the ref read for branch %s answered an unexpected "
                          "payload (%s); refusing to push"
                          % (branch, type(ref).__name__))
    if remote_head == local_head:
        return local_head
    if remote_head:
        base, exists = remote_head, True
    else:
        info = gh.rest("GET", "/repos/%s" % repo).data or {}
        default = str(info.get("default_branch") or "main")
        base, exists = _git("merge-base", "HEAD", "origin/" + default), False
    commits = _git("rev-list", "--reverse", base + "..HEAD").splitlines()
    api_parent, local_parent = base, base
    for sha in commits:
        api_parent = _push_commit(gh, repo, sha, local_parent, api_parent)
        local_parent = sha
    ref_path = "/repos/%s/git/refs/heads/%s" % (repo, quote(branch, safe=""))
    if exists:
        gh.rest("PATCH", ref_path, {"sha": api_parent, "force": False})
    else:
        try:
            gh.rest("POST", "/repos/%s/git/refs" % repo,
                    {"ref": "refs/heads/" + branch, "sha": api_parent})
        except GitHubError as err:
            if err.status != 422:
                raise
            gh.rest("PATCH", ref_path, {"sha": api_parent, "force": False})
    return api_parent


def _what(body: str) -> str:
    match = re.search(r"^## What\s*\n(.*?)(?=^## |\Z)", body, re.M | re.S)
    return match.group(1).strip() if match else ""


def pr_body(repo: str, issue: Dict[str, Any], kind: str, counts: Dict[str, int],
            lane: str = "muse") -> str:
    """The pull request body every lane writes (lanes/run_lane.sh pr_body,
    rendered by bus/pr_repair.py): the Intent section, Why, Summary,
    Acceptance criteria, Size, Out of scope, the Loom footer and a Refs line.
    Never a closing keyword: the ticket closes only once it is verified.
    ValueError when the body would fail the review bot's plan checks."""
    number = int(issue["number"])
    brief = tickets.render_brief(repo, issue, lane)
    model = os.environ.get("LOOM_%s_MODEL" % lane.upper().replace("-", "_"), lane)
    body = pr_repair._render_generated(brief, lane, model, tickets.ticket_name(repo, number))
    intent = change_class.render_intent(kind, change_class.behavior_lines(
        "", _what(str(issue.get("body") or ""))), counts)
    body = change_class.with_intent(body, intent)
    found = hunt_plan.problems(body)
    if found:
        raise ValueError("the pull request body would fail the review bot's plan checks: "
                         + "; ".join(found) + " (fix the ticket's brief sections)")
    return body


def open_pr(gh: GitHub, repo: str, number: int, base: str,
            title: Optional[str] = None) -> Dict[str, Any]:
    """Open a PR for the already-pushed loom ticket branch."""
    issue = gh.rest("GET", "/repos/%s/issues/%d" % (repo, number)).data or {}
    branch = _git("branch", "--show-current")
    if branch != tickets.branch_for(number, str(issue.get("title") or "")):
        raise ValueError("current branch does not match the ticket branch")
    files = _git("diff", "--name-only", "%s...HEAD" % base).splitlines()
    if not files:
        raise ValueError("branch has no changes against base")
    patterns_data = gh.rest("GET", "/repos/%s/contents/%s" %
                            (repo, quote(change_class.SENSITIVE_FILE, safe="/")), ok=(404,)).data
    patterns = []
    if isinstance(patterns_data, dict) and patterns_data.get("content"):
        import base64
        patterns = change_class.parse_patterns(base64.b64decode(patterns_data["content"]).decode("utf-8"))
    kind = change_class.classify(files, patterns)
    body = pr_body(repo, issue, kind, change_class.count_by_class(files, patterns))
    made = gh.rest("POST", "/repos/%s/pulls" % repo, {
        "title": title or str(issue.get("title") or ""), "head": branch,
        "base": base, "body": body}).data
    if not isinstance(made, dict) or "number" not in made:
        raise ValueError("GitHub did not return the created pull request")
    gh.rest("POST", "/repos/%s/issues/%d/labels" % (repo, made["number"]),
            {"labels": ["class:" + kind]})
    labels = [x for x in _names(issue) if x not in ("loom:running", "loom:queued")]
    labels.append("loom:pr")
    gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number), {"labels": labels})
    return made


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="python3 -m bus.lane")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("claim")
    p.add_argument("--lane", required=True)
    p.add_argument("--repos", default=os.environ.get("LOOM_REPOS", ""))
    p = sub.add_parser("heartbeat")
    p.add_argument("ticket", nargs="?", help="OWNER/NAME#N; omit to report lane availability")
    p.add_argument("--lane", required=True)
    p.add_argument("--repo", default=os.environ.get("LOOM_STATUS_REPO", ""))
    p.add_argument("--repos", default=os.environ.get("LOOM_REPOS", ""))
    p = sub.add_parser("pr")
    p.add_argument("ticket", help="OWNER/NAME#N")
    p.add_argument("--base", required=True)
    p.add_argument("--title")
    p = sub.add_parser("push")
    p.add_argument("ticket", help="OWNER/NAME#N")
    p = sub.add_parser("confirm-claim")
    p.add_argument("ticket", help="OWNER/NAME#N")
    p.add_argument("--lane", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "claim":
            repos = [r for r in args.repos.replace(",", " ").split() if r]
            if not repos:
                raise ValueError("claim requires --repos or LOOM_REPOS")
            gh = GitHub(auth_from_env())
            result = claim(gh, repos, args.lane)
            if result:
                print(json.dumps(result))
            return 0 if result else 1
        repo, _, raw = args.ticket.partition("#") if args.ticket else ("", "", "")
        if args.command == "heartbeat" and not args.ticket:
            status_repo = args.repo or (args.repos.replace(",", " ").split() or [""])[0]
            if not tickets.REPO_RE.fullmatch(status_repo):
                raise ValueError("heartbeat without a ticket requires --repo or LOOM_STATUS_REPO")
            gh = GitHub(auth_from_env())
            lane_heartbeat(gh, status_repo, args.lane)
            return 0
        if not tickets.REPO_RE.fullmatch(repo) or not raw.isdigit():
            raise ValueError("expected OWNER/NAME#N")
        number = int(raw)
        gh = GitHub(auth_from_env())
        if args.command == "heartbeat":
            heartbeat(gh, repo, number, args.lane)
        elif args.command == "confirm-claim":
            found = confirm_claim(gh, repo, number, args.lane)
            print("%s#%d: %s" % (found["repo"], found["number"], found["title"]))
            for key, value in found["headers"].items():
                print("%s: %s" % (key, value))
        elif args.command == "push":
            sha = push_branch(gh, repo, number)
            print(sha)
        else:
            made = open_pr(gh, repo, number, args.base, args.title)
            print(made.get("html_url", ""))
        return 0
    except (GitHubError, OSError, ValueError) as err:
        print("error: %s" % err, file=__import__("sys").stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
