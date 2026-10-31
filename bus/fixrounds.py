"""One loom:fix comment per pull request, edited in place for each fix round.

Each fix round posted a new loom:fix comment on the ticket, and every new
comment emails the operator: one ticket sent two identical "Check failed"
emails within the hour for the same pull request (loom#1640). The first
round on a pull request is still a new comment, unchanged. Each later round
the station sends edits that comment into a fix-rounds comment, which sends
no email.

A fix-rounds comment shows one short block per round (its number, time and
reasons) and keeps each round's whole loom:fix body as plain text in a hidden
block of its own, with "&", "<", ">" and "#" escaped so nothing in the body
can end it or read as a marker or heading:

    <!-- loom:fix v1 -->
    Pull request: <repo>#<pull request>, branch <branch>
    <!-- loom:fix-rounds v1 -->
    ...one visible block per round...
    <!-- loom:fix-round 1 2026-10-25T14:03:00Z
    ...the round's loom:fix body, escaped...
    -->

Readers count rounds, read the fix key, the asked threads and the lane's fix
brief from one comment per round, as before: the GitHub client expands the
comment into one pseudo-comment per round (`expand`), its body that round's
original loom:fix body and its time the round's own, as it does the status
comment. A ticket whose rounds are older per-round comments keeps them, and
both forms read the same.

Trust. A comment is read as rounds only when a trusted login wrote it and it
is exactly as this Loom last wrote it: the station records the updated_at
GitHub answers each edit with in a ledger (`ledger_path`), and a comment whose
real updated_at differs was changed by someone else. Nothing in the body is
trusted for that. Such a comment stays one opaque loom:fix comment: it counts
as one round, and the lane's brief refuses it as an edited loom:fix comment.

Findings. Each thread a fix round answers has a record in a second ledger
(`findings_path`), keyed by thread and revision: the pull request head the
round worked on. The runner writes that head beside the answers, which is the
lane's pickup; the station refuses an answer whose head is no longer the pull
request's (`transition_finding`), and a finding is answered only when the
thread can be resolved on it. The runner's other reply route,
.loom/replies.json, resolves a thread only when the round's commits change the
thread's file (lanes/run_lane.sh post_replies).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from . import notify
from .tickets import FIX_MARKER

MARKER = "<!-- loom:fix-rounds v1 -->"
_STAMP = r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"
# A round's escaped body holds no ">", so the first "\n-->" ends it.
ROUND_RE = re.compile(r"^<!-- loom:fix-round ([0-9]+) (%s)\n([^>]*?)\n-->$" % _STAMP, re.M)
HEAD_RE = re.compile(r"<!-- loom:fix-head [0-9a-f]+ -->")
PR_RE = re.compile(r"^Pull request: ([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)#([0-9]+)\b", re.M)
HEAD = ("Loom edits this one comment for each fix round on this pull request, so a "
        "new round sends no new email.")
# Pseudo-comments made by expand carry the round number under this key.
EXPANDED = "_loom_fix_round"
# GitHub rejects a comment over 65536 characters.
MAX_CHARS = 64000
# Lines and characters of one round's reasons shown on the page.
SHOWN_LINES = 12
LINE_CHARS = 300
# The ledger keeps the newest edits only; an older comment reads opaque.
LEDGER_KEEP = 5000
_REVISION = re.compile(r"^[0-9a-f]{40}$")


def transition_finding(finding: Mapping[str, Any], action: str, revision: str,
                      current_revision: str, commit: str = "",
                      commit_files: Iterable[str] = (),
                      reason_accepted: bool = False) -> Optional[Dict[str, Any]]:
    """Apply one idempotent finding transition at its recorded PR revision.

    Answers require a pushed commit touching the finding's cited path, or an
    explicit reviewer acceptance. Stale revisions and skipped states refuse.

    bus/station.py calls this for each lane answer. It takes the commit's
    changed files as data and does no GitHub I/O, so callers fetch them first.
    """
    recorded = str(finding.get("revision") or "").lower()
    submitted = str(revision or "").lower()
    current = str(current_revision or "").lower()
    state = str(finding.get("state") or "pending")
    if not (_REVISION.fullmatch(recorded) and submitted == recorded == current):
        return None
    result = dict(finding)
    if action == "picked_up":
        if state == "pending":
            result["state"] = "picked_up"
        return result
    if action != "answered":
        return None
    if state == "answered":
        return result
    if state != "picked_up":
        return None
    path = str(finding.get("path") or "")
    if not reason_accepted and (not _REVISION.fullmatch(str(commit or "").lower())
                                or path not in set(commit_files)):
        return None
    result.update(state="answered", answer_revision=submitted)
    if commit:
        result["commit"] = str(commit).lower()
    return result


def is_rounds(body: Any) -> bool:
    text = str(body or "")
    return text.startswith(FIX_MARKER) and MARKER in text.split("\n", 3)[:3]


def _utc(at: float) -> str:
    return datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _escape(text: str) -> str:
    """A round's body with no ">" (so nothing ends its block), and no "<" or
    "#" (so a reader that searches the raw comment finds no marker, key or
    ### heading of an old round in it)."""
    return (text.replace("&", "&amp;").replace(">", "&gt;").replace("<", "&lt;")
            .replace("#", "&#35;"))


def _unescape(text: str) -> str:
    return (text.replace("&#35;", "#").replace("&lt;", "<").replace("&gt;", ">")
            .replace("&amp;", "&"))


def parse(body: Any) -> List[Tuple[int, str, str]]:
    """(round number, UTC time, the round's loom:fix body) for each round the
    comment holds, oldest first."""
    if not is_rounds(body):
        return []
    found = [(int(n), at, _unescape(text)) for n, at, text in ROUND_RE.findall(str(body))]
    return sorted(found, key=lambda r: r[0])


def pull_of(body: Any) -> Optional[Tuple[str, int]]:
    """(repo, number) of the pull request a loom:fix body names."""
    found = PR_RE.search(str(body or ""))
    return (found.group(1), int(found.group(2))) if found else None


def _login(comment: Dict[str, Any]) -> str:
    user = comment.get("user")
    return str(user.get("login") or "").lower() if isinstance(user, dict) else ""


def _ident(comment: Any) -> Optional[int]:
    ident = comment.get("id") if isinstance(comment, dict) else None
    return ident if isinstance(ident, int) and not isinstance(ident, bool) else None


# -- the ledger of this Loom's own edits ---------------------------------------

def ledger_path(home: str) -> str:
    return os.path.join(home, "state", "fix-rounds.json")


def ledger_from_env(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The station's ledger where LOOM_HOME names the station's home, for a
    reader on the same machine (the brain, the dashboard); None elsewhere."""
    home = (os.environ if env is None else env).get("LOOM_HOME", "").strip()
    return ledger_path(home) if home else None


def findings_path(home: str) -> str:
    return os.path.join(home, "state", "fix-findings.json")


def finding_key(thread: str, revision: str) -> str:
    return "%s@%s" % (thread, revision)


def load_findings(path: str) -> Dict[str, Dict[str, Any]]:
    """finding_key -> finding record; empty when it cannot be read, so a lost
    ledger starts each finding again at pending."""
    data = _read(path)
    return {str(k): v for k, v in data.items() if isinstance(v, dict)}


def record_finding(path: str, finding: Mapping[str, Any]) -> None:
    """Keep one finding's newest record. Raises OSError."""
    _store(path, finding_key(str(finding["id"]), str(finding["revision"])), dict(finding))


def _read(path: str) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _store(path: str, key: str, value: Any) -> None:
    """Move `key` to the newest end of the ledger at `path`, under its lock."""
    import fcntl
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".lock", "a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _read(path)
        data.pop(key, None)
        data[key] = value
        kept = dict(list(data.items())[-LEDGER_KEEP:])
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(kept, fh)
        os.replace(tmp, path)


def load_ledger(path: Optional[str]) -> Dict[str, str]:
    """comment id -> the updated_at GitHub answered this Loom's last edit
    with; empty when there is no ledger or it cannot be read."""
    if not path:
        return {}
    return {str(k): str(v) for k, v in _read(path).items()}


def record(path: str, ident: int, updated_at: str) -> None:
    """Note this Loom's edit of comment `ident`. Raises OSError."""
    _store(path, str(ident), updated_at)


def _verified(comment: Dict[str, Any], ledger: Mapping[str, str]) -> bool:
    """Whether the comment is exactly as this Loom last wrote it."""
    ident = _ident(comment)
    stamp = comment.get("updated_at")
    return ident is not None and bool(stamp) and ledger.get(str(ident)) == str(stamp)


def expand(items: Any, authors: Iterable[str], ledger: Mapping[str, str]) -> Any:
    """An issue-comment list with each fix-rounds comment one of `authors`
    wrote, still exactly as this Loom last wrote it (`ledger`), replaced by one
    pseudo-comment per round: its body the round's loom:fix body, its
    created_at and updated_at the round's time, everything else (id, user)
    the comment's. Any other comment, a look-alike or an edited one included,
    stays as it is. The list is sorted by time when every comment carries
    one, else each comment's rounds take its place. Calling it on an expanded
    list changes nothing."""
    allowed = {str(a).lower() for a in authors if a}

    def ours(c: Any) -> bool:
        return (isinstance(c, dict) and _login(c) in allowed and is_rounds(c.get("body"))
                and _verified(c, ledger))
    if not allowed or not isinstance(items, list) or not any(ours(c) for c in items):
        return items
    out: List[Any] = []
    for comment in items:
        if not ours(comment):
            out.append(comment)
            continue
        for number, at, text in parse(comment.get("body")):
            item: Dict[str, Any] = dict(comment)
            item.update(body=text, created_at=at, updated_at=at)
            item[EXPANDED] = number
            out.append(item)
    if all(isinstance(c, dict) and c.get("created_at") for c in out):
        out.sort(key=lambda c: str(c.get("created_at")))
    return out


def has_rounds(items: Any) -> bool:
    return isinstance(items, list) and any(
        isinstance(c, dict) and is_rounds(c.get("body")) for c in items)


# -- writing a round ------------------------------------------------------------

def _reasons(body: str) -> List[str]:
    """The visible lines of one round's loom:fix body: what it asks, without
    its markers, its pull request line and its hidden detail."""
    shown: List[str] = []
    for line in body.split("\n")[1:]:
        if line.startswith("<!-- loom:fix-detail"):
            break
        if line.startswith("<!--") or PR_RE.match(line):
            continue
        if line.strip() or (shown and shown[-1].strip()):
            shown.append(line[:LINE_CHARS])
    while shown and not shown[-1].strip():
        shown.pop()
    if len(shown) > SHOWN_LINES:
        shown = shown[:SHOWN_LINES] + ["- (%d more lines in this round)" % (len(shown) - SHOWN_LINES)]
    return shown or ["- (no reasons given)"]


def _slim(body: str) -> str:
    """A round's body cut to what the readers count from it: its markers, its
    pull request line, its key and head lines and its ### headings. Used on
    older rounds only when the comment would not fit otherwise."""
    keep = [line for line in body.split("\n")
            if line.startswith(("<!-- loom:fix", "Pull request: ", "### "))
            and not line.startswith("<!-- loom:fix-detail")]
    return "\n".join(keep) + "\n"


def render(pull: Tuple[str, int], branch: str, rounds: List[Tuple[int, str, str]]) -> Optional[str]:
    """The comment body for `rounds` (oldest first), or None when it cannot
    fit even with every round but the newest cut to its headings."""
    head = HEAD_RE.search(rounds[-1][2]) if rounds else None

    def body(items: List[Tuple[int, str, str]]) -> str:
        lines = [FIX_MARKER, "Pull request: %s#%d, branch %s" % (pull[0], pull[1], branch),
                 MARKER]
        if head:
            # The newest round's head in the clear: a reader of the raw comment
            # (one this Loom cannot vouch for) still sees one round on it.
            lines.append(head.group(0))
        lines += ["", HEAD, ""]
        for number, at, text in items:
            lines.append("Round %d, %s UTC:" % (number, at[:16].replace("T", " ")))
            lines.extend(_reasons(text))
            lines.append("")
        for number, at, text in items:
            lines += ["<!-- loom:fix-round %d %s" % (number, at), _escape(text.strip("\n")), "-->"]
        return "\n".join(lines) + "\n"
    items = list(rounds)
    text = body(items)
    for i in range(len(items) - 1):
        if len(text) <= MAX_CHARS:
            break
        number, at, old = items[i]
        items[i] = (number, at, _slim(old))
        text = body(items)
    return text if len(text) <= MAX_CHARS else None


def _branch(body: str) -> str:
    found = re.search(r"^Pull request: \S+#[0-9]+, branch (\S+)", body, re.M)
    return found.group(1) if found else ""


def _writers(gh: Any, authors: Iterable[str]) -> set:
    """Whose fix comment a new round may edit: this client's own App login
    when it has one (an App edits its own comments), else the trusted
    logins (a personal token writes as one of them)."""
    from .github import GitHubError  # the client imports this module
    own = getattr(gh, "app_login", None)
    login = None
    if callable(own):
        try:
            login = own()
        except GitHubError:
            login = None
    if isinstance(login, str) and login:
        return {login.lower()}
    return {str(a).lower() for a in authors if a}


def _rounds_of(comments: Any, writers: set, pull: Tuple[str, int],
               ledger: Mapping[str, str]) -> Optional[Tuple[int, List[Tuple[int, str, str]]]]:
    """(comment id, rounds oldest first) of the newest loom:fix comment one of
    `writers` wrote for `pull`: a fix-rounds comment's rounds, or a plain
    loom:fix comment GitHub reports unedited as round 1. None when there is
    none, or when that comment was changed by anyone but this Loom."""
    items = expand(comments if isinstance(comments, list) else [], writers, ledger)
    newest = None
    for c in items:
        if (_ident(c) is not None and _login(c) in writers
                and str(c.get("body") or "").startswith(FIX_MARKER)
                and pull_of(c.get("body")) == pull):
            newest = c
    if newest is None:
        return None
    ident = newest["id"]
    if newest.get(EXPANDED):
        rounds = [c for c in items if isinstance(c, dict) and c.get("id") == ident and c.get(EXPANDED)]
        return ident, sorted(((int(c[EXPANDED]), str(c.get("created_at")), str(c.get("body") or ""))
                              for c in rounds), key=lambda r: r[0])
    # Not expanded: a plain round, or a fix-rounds comment someone else changed.
    if is_rounds(newest.get("body")) or newest.get("updated_at") != newest.get("created_at"):
        return None
    at = str(newest.get("created_at") or "") or _utc(time.time())
    return ident, [(1, at, str(newest.get("body") or ""))]


def post_round(gh: Any, repo: str, number: int, body: str, authors: Iterable[str],
               ledger: Optional[str], comments: Optional[List[Any]] = None,
               now: Optional[float] = None) -> Any:
    """Add one fix round (a whole loom:fix body) to the ticket. The first
    round on a pull request is a new comment, as before. A later one edits the
    newest loom:fix comment this writer wrote for the same pull request into a
    fix-rounds comment holding every round, which sends no email, and records
    the edit in the `ledger` file. It posts a new comment instead when there is
    no ledger, when that comment was changed by anyone else, when the rounds
    no longer fit, or when the edit is refused. `comments` is the ticket's
    comment list when the caller has just read it (it is read here otherwise).
    Returns the GitHub client's response."""
    from .github import GitHubError, RateLimited  # the client imports this module
    at = _utc(time.time() if now is None else now)
    pull = pull_of(body)
    found = None
    known = load_ledger(ledger)
    if pull is not None and ledger:
        if comments is None:
            comments = gh.paginate("/repos/%s/issues/%d/comments" % (repo, int(number)))
        found = _rounds_of(comments, _writers(gh, authors), pull, known)
    if found is not None and pull is not None and ledger:
        ident, rounds = found
        rounds.append((rounds[-1][0] + 1, max(at, rounds[-1][1]), body))
        text = render(pull, _branch(body), rounds)
        if text is not None:
            try:
                resp = gh.rest("PATCH", "/repos/%s/issues/comments/%d" % (repo, ident),
                               {"body": text})
            except RateLimited:
                raise
            except GitHubError:
                resp = None  # refused (not ours to edit, or gone): post the round instead
            if resp is not None:
                stamp = resp.data.get("updated_at") if isinstance(resp.data, dict) else None
                if stamp:
                    try:
                        record(ledger, ident, str(stamp))
                    except OSError as err:
                        print("fix rounds: could not record the edit of comment %d: %s"
                              % (ident, err), file=sys.stderr)
                return resp
    return notify.post(gh, repo, number, body, notify.FIX_BRIEF)
