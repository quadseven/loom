"""Shadow mode: Loom runs as usual but records, instead of taking, every
irreversible GitHub action (docs/BRAIN.md, Shadow mode; loom#1515).

Held while a repo is in shadow:

- a pull request merge (PUT .../pulls/N/merge, POST .../merges, and the
  GraphQL merge and auto-merge mutations), from the factory, `bus.brain
  merge` (the station's and Muse's), the deploy-watch revert path and the
  roll pin;
- an issue close (PATCH state=closed, GraphQL closeIssue or an update to
  CLOSED) and an issue reopen (PATCH state=open, GraphQL reopenIssue),
  except Loom's own factory incidents, health signatures and invariant
  breach issues that carry no work label (an open incident holds the
  tickets behind it; a cleared breach is Loom's own report, and the
  invariants step reopens it if it comes back); a recurring breach stays
  held. Closing or reopening a pull request is let through: it can be
  undone, its branch stays, and the factory closes superseded lane pull
  requests so lanes do not work twice;
- a label that grants a merge or marks a ticket shipped
  (MERGE_LABELS), and a GraphQL label write, whose label ids it cannot read;
- an approving pull request review (REST or GraphQL);
- a write straight onto a branch Loom does not own (a ref moved, a file
  committed or deleted through the contents API, a merge-queue entry, a fork
  sync): code landed with no pull request is a merge;
- a branch deletion (DELETE .../git/refs/heads/B) other than Loom's own lock.

One close alone passes: the station's evidence close (bus/evidence_close.py,
operator ruling of 2026-10-31). The station names the one ticket in code
(evidence_close()), so no label, no argument and no other process can ask for
it, and the request is still recorded as acted. Merges stay held.

A request the hold cannot read (a label list, the item's current labels, a
body of an unknown shape, a read that failed in any way) is held as the
worst request it could be.

bus/github.py checks every request against classify() at its one choke point
(GitHub._rest) and raises ShadowHeld instead of sending a held one; `bus.brain
merge`, `bus.brain close` and the station's closes check active() first, so
their record carries the risk tier and the gates that passed; the invariants
step asks guard() before its closes and reopens, so a held one posts no
note either. The brain's
model rung holds a GitHub token of its own: lanes/mac/brain.sh puts a gh in
front of it that asks gh_guard() first (`python3 -m bus.shadow gh-guard`).

The switch fails closed. LOOM_SHADOW turns shadow off only when it reads
exactly 0, false, off or no; unset, empty, any other value, or an error while
reading it means shadow. LOOM_SHADOW_REPOS lists the repos still in shadow
(OWNER/NAME, space or comma separated); unset, empty, `*`, or any entry that
is not OWNER/NAME means every repo. A repo leaves shadow only when the
operator removes it from that list, or sets LOOM_SHADOW=0 for every repo.

Each held action is one line of an append-only JSONL ledger: LOOM_SHADOW_LEDGER,
else $LOOM_HOME/state/shadow/ledger.jsonl. The same action (repo, number,
action, head SHA, label) is written once, so a pass that tries again every few
minutes adds nothing. A ledger that cannot be written never lets the action
through: the hold does not depend on the record.

Each irreversible request GitHub accepts from Loom (its repo out of shadow, or
a close let through) is one line of acted.jsonl beside the ledger, cut to its
last ACTED_KEEP lines past ACTED_MAX; the station sends both files' newest
lines to the dashboard's /chain page. A failed write never stops a request.

    python3 -m bus.shadow report [--since 24h|7d|2026-10-22[T10:00:00Z]]
                                 [--repo OWNER/NAME] [--json]
    python3 -m bus.shadow status
    python3 -m bus.shadow prompt        # the brain tick's Shadow mode sentence
    python3 -m bus.shadow gh-guard [--lane] CWD GH-ARGS...   # exit 1: held
"""
from __future__ import annotations

import argparse
import calendar
import contextlib
import contextvars
import json
import os
import re
import subprocess
import sys
import time
import traceback
from typing import Any, Dict, Iterator, List, Mapping, NamedTuple, Optional, Sequence, Tuple
from urllib.parse import unquote

SWITCH = "LOOM_SHADOW"
REPOS = "LOOM_SHADOW_REPOS"
LEDGER = "LOOM_SHADOW_LEDGER"
# The only values that turn shadow off; anything else keeps it on.
OFF = ("0", "false", "off", "no")
REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")

MERGE, CLOSE, REOPEN, LABEL, DELETE_BRANCH, APPROVE = (
    "merge", "close", "reopen", "label", "delete-branch", "approve")
ACTIONS = (MERGE, CLOSE, REOPEN, LABEL, DELETE_BRANCH, APPROVE)
# Not a would-action: the operator's own Merge or Hold on a lane pull request
# held for them, answered on the dashboard (bus/answer.py, loom#1777), so the
# ledger pairs what Loom would have done with the operator's verdict. Its
# label is the verdict ("merge" or "hold"); it is never counted as an action.
VERDICT = "operator-verdict"
# Not a request the choke point holds: a label removal is reversible, so
# GitHub._rest sends it. The station's label pass (bus/label_pass.py,
# loom#2024) records its own removals as this action in shadow and sends
# none, so the operator reads what the pass would clean before it does.
UNLABEL = "unlabel"

# Labels whose addition grants a merge or ships a ticket, each with the labels
# that mean the ticket is already past it (a verified ticket moved back to
# shipped by a failed audit is not a new ship).
MERGE_LABELS: Dict[str, Tuple[str, ...]] = {
    "ready-to-land": (),
    "loom:shipped": ("loom:verified",),
}
# A close of an issue with one of these is a decision about work: held.
WORK_LABELS = ("loom:ticket", "loom:goal", "loom:backlog", "loom:rewritten")
# A breach that keeps coming back stays open for a fix: its close is held.
HELD_SIGNAL_LABELS = ("loom:recurring",)
# An invariant breach's own issue, filed by the invariants step (its body
# carries INVARIANT_MARKER), closes and reopens in shadow on that step's
# rule. loom#1551 held it; on 2026-10-26 35 such issues the station had
# marked cleared stayed open and the operator asked why Loom did not clean
# up after itself. A close can be undone by the reopen the same rule makes.
INVARIANT_LABEL = "loom:invariant"
INVARIANT_MARKER = "<!-- loom:invariant v1 key="
# A factory incident whose check passed again closes in shadow too: holding
# it would hold every ticket it blocks (wait:incident). Only an issue with one
# of these, no work label and no held signal label is let through.
SIGNAL_LABELS = ("loom:factory",)
# loom:triage is on every open issue that is not yet a ticket, a person's
# too, so it marks a signal issue only on a health signature the watcher
# filed: its body carries the watcher's marker (bus/watch.py MARKER).
TRIAGE_LABEL = "loom:triage"
WATCH_MARKER = "<!-- loom:watch v1 -->"
# Loom's own coordination branches (bus/lock.py LOCK_BRANCH), never a PR head.
OWN_BRANCH_PREFIX = "loom-lock/"
# The branches Loom writes itself: lane, pin and roll pin work branches
# (bus/tickets.py branch_for, bus/pin.py, bus/roll_pin.py) and the lock. A
# write onto any other branch, the default branch above all, is a merge.
OWN_WRITE_PREFIXES = ("loom/", OWN_BRANCH_PREFIX)
_BRANCH_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*")

# GraphQL mutations held whatever their input says: each merges, closes,
# deletes a branch, writes onto a branch, or adds labels it names by id.
_GRAPHQL_ACTIONS = {
    "mergePullRequest": MERGE, "enablePullRequestAutoMerge": MERGE,
    "enqueuePullRequest": MERGE, "mergeBranch": MERGE, "updateRef": MERGE,
    "updateRefs": MERGE, "createCommitOnBranch": MERGE, "closeIssue": CLOSE,
    "reopenIssue": REOPEN,
    "deleteRef": DELETE_BRANCH, "addLabelsToLabelable": LABEL,
}
_GRAPHQL_HELD = re.compile(r"\b(%s)\b" % "|".join(_GRAPHQL_ACTIONS))
_GRAPHQL_UPDATE = re.compile(r"\bupdateIssue\b")
_GRAPHQL_LABEL_IDS = re.compile(r"\b(updateIssue|updatePullRequest)\b[\s\S]*\blabelIds\b")
_GRAPHQL_REVIEW = re.compile(r"\b(addPullRequestReview|submitPullRequestReview)\b")
_MERGE_PATH = re.compile(r"^/repos/([^/]+/[^/]+)/pulls/([0-9]+)/merge$")
_MERGES_PATH = re.compile(r"^/repos/([^/]+/[^/]+)/(merges|merge-upstream)$")
_ISSUE_PATH = re.compile(r"^/repos/([^/]+/[^/]+)/issues/([0-9]+)$")
_LABELS_PATH = re.compile(r"^/repos/([^/]+/[^/]+)/issues/([0-9]+)/labels$")
_REF_PATH = re.compile(r"^/repos/([^/]+/[^/]+)/git/refs/heads/(.+)$")
_CONTENTS_PATH = re.compile(r"^/repos/([^/]+/[^/]+)/contents/(.+)$")
_REVIEW_PATH = re.compile(r"^/repos/([^/]+/[^/]+)/pulls/([0-9]+)/reviews(?:/[0-9]+/events)?$")


class Request(NamedTuple):
    """A request shadow mode would hold, before its exemptions are read."""
    action: str
    repo: str          # "" for a GraphQL mutation (it names no repo in the path)
    number: int        # 0 when the request names none
    kind: str          # "pr", "issue" or "branch"
    detail: str        # the labels added, the branch deleted, or the mutation
    check: str         # "" (always held), "close" or "label": github.py reads the item
    labels: Tuple[str, ...] = ()   # every merge or ship label the request adds


def _env(env: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    return os.environ if env is None else env


def scope(env: Optional[Mapping[str, str]] = None) -> Tuple[bool, Optional[frozenset]]:
    """(shadow on, the repos in shadow or None for every repo). Fails closed:
    any doubt reads as on, for every repo."""
    try:
        values = _env(env)
        raw = values.get(SWITCH)
        if raw is not None and str(raw).strip().lower() in OFF:
            return False, frozenset()
        listed = values.get(REPOS)
        names = [n for n in re.split(r"[\s,]+", str(listed or "")) if n]
        if not names or "*" in names or not all(REPO_RE.fullmatch(n) for n in names):
            return True, None
        return True, frozenset(n.lower() for n in names)
    except Exception:  # noqa: BLE001 - an unreadable switch is shadow
        return True, None


def active(repo: Optional[str] = None, env: Optional[Mapping[str, str]] = None) -> bool:
    """Whether `repo` is in shadow; with no repo, whether any is. Fails closed."""
    try:
        on, repos = scope(env)
        if not on:
            return False
        if repos is None or not repo:
            return True
        return str(repo).lower() in repos
    except Exception:  # noqa: BLE001
        return True


def describe(env: Optional[Mapping[str, str]] = None) -> str:
    on, repos = scope(env)
    if not on:
        return "shadow mode off (%s=0): Loom merges and closes" % SWITCH
    which = "every repo" if repos is None else ", ".join(sorted(repos))
    return "shadow mode on for %s: merges and closes are recorded, not taken" % which


# The brain tick's own words for "in shadow" (lanes/mac/brain.sh). The
# fallback the wrapper uses when `prompt` cannot run is this same sentence.
_HELD_WORDS = ("never merge, close an issue, approve a pull request, or add "
               "ready-to-land or loom:shipped by any route (gh, curl, the API); "
               "bus.brain merge and bus.brain close run their gates and record "
               "what they would do")


def prompt_line(env: Optional[Mapping[str, str]] = None) -> str:
    """The Shadow mode sentence of the brain tick's prompt, read from scope()
    so the prompt and the gates agree (loom#1960). The prompt used to say
    "unless LOOM_SHADOW is 0, never merge", which ignored LOOM_SHADOW_REPOS:
    the brain refused merges bus.brain merge would have made, in repos the
    operator had taken out of shadow. Fails closed to every repo."""
    try:
        on, repos = scope(env)
    except Exception:  # noqa: BLE001 - scope() already fails closed
        on, repos = True, None
    if not on:
        return ("Shadow mode (MUSE.md) is off (LOOM_SHADOW=0) for every repo: "
                "merge and close only through bus.brain merge and bus.brain "
                "close, once their gates pass.")
    if repos is None:
        return "Shadow mode (MUSE.md) is on for every repo: %s." % _HELD_WORDS
    held = ", ".join(sorted(repos))
    return ("Shadow mode (MUSE.md) is on only for %s (LOOM_SHADOW_REPOS): in "
            "those repos %s. Every other repo is out of shadow: merge and close "
            "there through bus.brain merge and bus.brain close once their gates "
            "pass, never any other way; a ready pull request there is not a shadow "
            "defer." % (held, _HELD_WORDS))


def api_path(path: Any) -> str:
    """A request path as GitHub routes it: no scheme, host, query or
    fragment, percent-escapes decoded, runs of slashes and a trailing slash
    gone. Every pattern here matches this form, so a path spelled another way
    is the same request."""
    path = str(path or "")
    if "://" in path:
        path = "/" + path.split("://", 1)[1].partition("/")[2]
    path = unquote(path.split("#", 1)[0].split("?", 1)[0])
    return re.sub(r"/{2,}", "/", path).rstrip("/")


def _own_branch(name: str, prefixes: Tuple[str, ...]) -> bool:
    """Whether `name` is a plain branch name under one of Loom's prefixes."""
    return (name.startswith(prefixes) and ".." not in name
            and _BRANCH_NAME.fullmatch(name) is not None)


def classify(method: str, path: str, body: Any) -> Optional[Request]:
    """The held action a REST or GraphQL request is, or None. Pure: the
    exemptions that need the item (a signal issue's close, a label already
    on) are github.py's, named by Request.check. A body of a shape it cannot
    read is held when the path takes a body that could be held."""
    path = api_path(path)
    for shape in _SHAPES.get(str(method or "").upper(), ()):
        found = shape(path, body)
        if found is not None:
            return found
    return None


def _pr_merge(path: str, body: Any) -> Optional[Request]:
    found = _MERGE_PATH.match(path)
    if not found:
        return None
    return Request(MERGE, found.group(1), int(found.group(2)), "pr", "", "")


def _branch_merge(path: str, body: Any) -> Optional[Request]:
    found = _MERGES_PATH.match(path)
    if not found:
        return None
    return Request(MERGE, found.group(1), 0, "branch", found.group(2) + " API", "")


def _issue_edit(path: str, body: Any) -> Optional[Request]:
    """A close, or a label write, through PATCH .../issues/N. A close that may
    go (a signal issue, a pull request) still holds a merge or ship label
    added in the same request, so the close carries them."""
    found = _ISSUE_PATH.match(path)
    if not found or not isinstance(body, dict):
        return None
    repo, number = found.group(1), int(found.group(2))
    labels = _label_request(repo, number, body["labels"]) if "labels" in body else None
    if labels is not None and not labels.check:
        return labels   # a label list it cannot read
    state = str(body.get("state") or "").strip().lower()
    if state == "closed":
        return Request(CLOSE, repo, number, "issue",
                       str(body.get("state_reason") or "completed"), "close",
                       labels.labels if labels else ())
    if state == "open":
        return Request(REOPEN, repo, number, "issue", "reopen", "close",
                       labels.labels if labels else ())
    return labels


def _labels_write(path: str, body: Any) -> Optional[Request]:
    found = _LABELS_PATH.match(path)
    if not found:
        return None
    return _label_request(found.group(1), int(found.group(2)),
                          body.get("labels") if isinstance(body, dict) else body)


def _ref_update(path: str, body: Any) -> Optional[Request]:
    found = _REF_PATH.match(path)
    if not found or _own_branch(found.group(2), OWN_WRITE_PREFIXES):
        return None
    return Request(MERGE, found.group(1), 0, "branch", "ref update of " + found.group(2), "")


def _contents_write(path: str, body: Any, verb: str = "write") -> Optional[Request]:
    found = _CONTENTS_PATH.match(path)
    branch = str(body.get("branch") or "") if isinstance(body, dict) else ""
    if not found or _own_branch(branch, OWN_WRITE_PREFIXES):
        return None
    return Request(MERGE, found.group(1), 0, "branch", "%s of %s on %s" % (
        verb, found.group(2), branch or "the default branch"), "")


def _contents_delete(path: str, body: Any) -> Optional[Request]:
    return _contents_write(path, body, "delete")


def _approving_review(path: str, body: Any) -> Optional[Request]:
    found = _REVIEW_PATH.match(path)
    if not found or (isinstance(body, dict)
                     and str(body.get("event") or "").strip().upper() != "APPROVE"):
        return None
    return Request(APPROVE, found.group(1), int(found.group(2)), "pr", "approving review", "")


def _branch_delete(path: str, body: Any) -> Optional[Request]:
    found = _REF_PATH.match(path)
    if not found or _own_branch(found.group(2), (OWN_BRANCH_PREFIX,)):
        return None
    return Request(DELETE_BRANCH, found.group(1), 0, "branch", found.group(2), "")


def _graphql(path: str, body: Any) -> Optional[Request]:
    return _graphql_request(body) if path.endswith("/graphql") else None


# The request shapes each method can take that shadow mode holds, in order.
_SHAPES: Dict[str, Tuple[Any, ...]] = {
    "PUT": (_pr_merge, _labels_write, _contents_write),
    "POST": (_branch_merge, _labels_write, _approving_review, _graphql),
    "PATCH": (_issue_edit, _ref_update),
    "DELETE": (_contents_delete, _branch_delete),
}


def _graphql_request(body: Any) -> Optional[Request]:
    if not isinstance(body, dict):
        return Request(MERGE, "", 0, "pr", "unreadable GraphQL request", "")
    text = str(body.get("query") or "")
    words = text + json.dumps(body.get("variables") or {}, default=str)
    held = _GRAPHQL_HELD.search(text)
    if held:
        name = held.group(1)
        action = _GRAPHQL_ACTIONS[name]
        kind = {MERGE: "pr", LABEL: "issue", CLOSE: "issue", REOPEN: "issue"}.get(action, "branch")
        return Request(action, "", 0, kind, name, "")
    if _GRAPHQL_LABEL_IDS.search(text):
        return Request(LABEL, "", 0, "issue", "label ids in a GraphQL update", "")
    if _GRAPHQL_REVIEW.search(text) and "APPROVE" in words:
        return Request(APPROVE, "", 0, "pr", "approving review (GraphQL)", "")
    if _GRAPHQL_UPDATE.search(text) and "CLOSED" in words:
        return Request(CLOSE, "", 0, "issue", "update to CLOSED", "")
    return None


def _label_request(repo: str, number: int, labels: Any) -> Optional[Request]:
    """The label write as a held Request when it adds any merge or ship label,
    naming every one of them; github.py then holds it if any is new. A label
    list of a shape this cannot read is held as it stands (fails closed)."""
    names = []
    for item in labels if isinstance(labels, list) else [None]:
        name = item.get("name") if isinstance(item, dict) else item
        if not isinstance(name, str):
            return Request(LABEL, repo, number, "issue", "unreadable label list", "")
        names.append(name.strip().lower())
    found = tuple(dict.fromkeys(n for n in names if n in MERGE_LABELS))
    if not found:
        return None
    return Request(LABEL, repo, number, "issue", ", ".join(found), "label", found)


def labels_of(item: Any) -> Optional[List[str]]:
    """The label names of an item as read, lower case; None when the item or
    its label list cannot be read (so nothing counts as already on)."""
    if not isinstance(item, dict) or not isinstance(item.get("labels"), list):
        return None
    names = []
    for x in item["labels"]:
        name = x.get("name") if isinstance(x, dict) else x
        if isinstance(name, str):
            names.append(name.strip().lower())
    return names


def label_is_new(label: str, have: Sequence[str]) -> bool:
    """Whether adding `label` to an item that has `have` is a new grant."""
    have_l = {str(h).strip().lower() for h in have}
    return label not in have_l and not any(x in have_l for x in MERGE_LABELS.get(label, ()))


def close_let_through(issue: Any) -> bool:
    """Whether a close or reopen of `issue` (as read just now; None when the
    read failed) goes through in shadow: a pull request (closed unmerged, it
    can be reopened), a factory incident or a watcher's health signature
    closing on its rule (no work label), or an invariant breach's own issue
    (its marker in the body, no work label, not recurring). An issue whose
    labels cannot be read is held."""
    if not isinstance(issue, dict):
        return False
    if "pull_request" in issue:
        return True
    names = labels_of(issue)
    if names is None or set(names) & set(WORK_LABELS + HELD_SIGNAL_LABELS):
        return False
    if set(names) & set(SIGNAL_LABELS):
        return True
    if INVARIANT_LABEL in names:
        return INVARIANT_MARKER in str(issue.get("body") or "")
    return TRIAGE_LABEL in names and WATCH_MARKER in str(issue.get("body") or "")


_evidence_ticket: "contextvars.ContextVar[Tuple[str, int]]" = contextvars.ContextVar(
    "loom_evidence_close", default=("", 0))


@contextlib.contextmanager
def evidence_close(repo: str, number: int) -> Iterator[None]:
    """Within this block the one close of `repo`#`number` is not held."""
    token = _evidence_ticket.set((str(repo).lower(), int(number)))
    try:
        yield
    finally:
        _evidence_ticket.reset(token)


def evidence_close_passes(found: Request) -> bool:
    """Whether `found` is the close evidence_close() names, carrying no merge
    or ship label."""
    return (found.action == CLOSE and found.check == "close" and not found.labels
            and _evidence_ticket.get() == (found.repo.lower(), found.number))


def held_after_read(found: Request, item: Any) -> Optional[Tuple[str, List[str]]]:
    """Whether `found` is still held once its item is read (None when the
    read failed or was not needed): None when it may go, else the action to
    record and the new merge or ship labels it adds. A close that may go is
    decided by the labels it carries; every label the item does not already
    have is new, all of them when its labels cannot be read."""
    action = found.action
    if found.check == "close" and close_let_through(item):
        if not found.labels:
            return None
        action = LABEL
    if action != LABEL:
        return action, []
    new_labels = list(found.labels)
    have = labels_of(item)
    if found.labels and have is not None:
        new_labels = [n for n in found.labels if label_is_new(n, have)]
        if not new_labels:
            return None
    return action, new_labels


def held_error_text(action: str, new_labels: Sequence[str], found: Request) -> str:
    """What a held request says: the action and the item it names."""
    what = "add %s to" % ", ".join(new_labels) if new_labels else action
    target = ("%s#%d" % (found.repo, found.number) if found.number
              else (found.repo or found.detail))
    return "shadow mode: would %s %s; recorded in the shadow ledger, not sent" % (what, target)


def ledger_path(env: Optional[Mapping[str, str]] = None) -> str:
    values = _env(env)
    explicit = str(values.get(LEDGER) or "").strip()
    if explicit:
        return explicit
    home = str(values.get("LOOM_HOME") or "").strip() or os.path.join(
        os.path.expanduser("~"), "loom")
    return os.path.join(home, "state", "shadow", "ledger.jsonl")


def acted_path(env: Optional[Mapping[str, str]] = None) -> str:
    """acted.jsonl beside the ledger: $LOOM_HOME/state/shadow/acted.jsonl
    by default, and a test's own folder when it points the ledger there."""
    return os.path.join(os.path.dirname(ledger_path(env)), "acted.jsonl")


# acted.jsonl grows past ACTED_MAX lines only to be cut to its last ACTED_KEEP.
ACTED_MAX = 1000
ACTED_KEEP = 500
_acted_warned = False


def record_acted(action: str, repo: str, number: int, kind: str, source: str = "",
                 now: Optional[float] = None) -> None:
    """Append one irreversible action GitHub accepted from Loom (its repo out
    of shadow, or a close let through) to acted.jsonl, for the dashboard's
    /chain audit. A failure of any kind is said once on stderr and never
    raised: the request already went."""
    global _acted_warned
    entry = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if now is None else now)),
             "action": action, "repo": repo or "", "number": int(number or 0), "kind": kind,
             "source": source or caller()}
    path = acted_path()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
        if len(lines) > ACTED_MAX:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines[-ACTED_KEEP:]) + "\n")
            os.replace(tmp, path)
    except Exception as err:  # noqa: BLE001 - the record never decides the request
        if not _acted_warned:
            _acted_warned = True
            print("shadow: could not write %s (%s); actions are not recorded as acted"
                  % (path, type(err).__name__), file=sys.stderr)


def read_acted(limit: int, path: Optional[str] = None) -> List[Dict[str, Any]]:
    """The last `limit` entries of acted.jsonl, oldest first; empty when it
    is missing. An unreadable line is skipped."""
    return read(path or acted_path())[-limit:] if limit > 0 else []


def link(repo: str, number: int, kind: str) -> str:
    if not repo or not number:
        return "https://github.com/%s" % repo if repo else ""
    return "https://github.com/%s/%s/%d" % (repo, "pull" if kind == "pr" else "issues", number)


def caller() -> str:
    """The first frame outside this module and bus/github.py: who asked."""
    for frame in reversed(traceback.extract_stack()[:-1]):
        name = os.path.basename(frame.filename)
        if name in ("shadow.py", "github.py"):
            continue
        return "%s:%d %s" % (name, frame.lineno, frame.name)
    return ""


def _key(entry: Mapping[str, Any]) -> Tuple[str, ...]:
    return tuple(str(entry.get(k) or "").lower()
                 for k in ("action", "repo", "number", "sha", "label"))


def record(action: str, repo: str, number: int, kind: str, *, tier: str = "",
           gates: Sequence[str] = (), reason: str = "", sha: str = "", label: str = "",
           source: str = "", env: Optional[Mapping[str, str]] = None,
           now: Optional[float] = None) -> Dict[str, Any]:
    """Append one would-action to the ledger, once per (action, repo, number,
    head SHA, label), and return it, with "new" set when this call wrote it.
    A failure to read or write the ledger, of any kind, is said on stderr
    and never raised: the caller holds the action either way."""
    entry = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                   time.gmtime(time.time() if now is None else now)),
             "repo": repo or "", "number": int(number or 0), "kind": kind,
             "action": action, "tier": tier or "", "gates": list(gates),
             "reason": " ".join(str(reason or "").split())[:500], "sha": sha or "",
             "label": label or "", "source": source or caller(),
             "url": link(repo or "", int(number or 0), kind)}
    path = ledger_path(env)
    try:
        if any(_key(old) == _key(entry) for old in read(path)):
            return dict(entry, new=False)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
    except Exception as err:  # noqa: BLE001 - the hold never depends on the record
        print("shadow: could not write the ledger %s (%s); the action is held anyway"
              % (path, type(err).__name__), file=sys.stderr)
    return dict(entry, new=True)


def record_held(action: str, repo: str, number: int, kind: str, **fields: Any) -> Dict[str, Any]:
    """record(), for a request that is held whatever happens to its record:
    if recording raises anything at all, the entry is made up from what is
    known and the failure said on stderr, so the caller still refuses the
    request with ShadowHeld."""
    try:
        return record(action, repo, number, kind, **fields)
    except Exception as err:  # noqa: BLE001 - the hold never depends on the record
        print("shadow: could not record %s %s#%s (%s); the action is held anyway"
              % (action, repo, number, type(err).__name__), file=sys.stderr)
        return {"action": action, "repo": repo or "", "number": number, "kind": kind,
                "label": str(fields.get("label") or ""), "new": True}


def guard(method: str, path: str, body: Any, item: Any, source: str = "",
          reason: str = "") -> Optional[Dict[str, Any]]:
    """The choke point's decision, asked before a write by a caller that
    already holds the item it names (`item`, as read; None when unread):
    None when the request may be sent, else the ledger entry it was recorded
    as. A caller asks first so a held close posts no note saying it closed;
    GitHub._rest still holds the request if it is sent anyway."""
    found = classify(method, path, body)
    if found is None or not active(found.repo or None):
        return None
    held = held_after_read(found, item if found.check else None)
    if held is None:
        return None
    action, new_labels = held
    why = reason or "%s %s%s" % (str(method).upper(), api_path(path),
                                 " (%s)" % found.detail if found.detail else "")
    entries = [record_held(action, found.repo, found.number, found.kind, reason=why,
                           label=label, source=source or caller())
               for label in (new_labels or [""])]
    return entries[0]


def read(path: str, since: float = 0.0, repo: str = "") -> List[Dict[str, Any]]:
    """The ledger's entries at or after `since` (epoch seconds), for `repo`
    when given. A missing ledger is empty; an unreadable line is skipped
    (bytes that are not UTF-8 included)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        if since and _epoch(entry.get("time")) < since:
            continue
        if repo and str(entry.get("repo") or "").lower() != repo.lower():
            continue
        out.append(entry)
    return out


def _epoch(value: Any) -> float:
    """A UTC time or date as epoch seconds; 0 for anything else."""
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%MZ", "%Y-%m-%d"):
        try:
            return float(calendar.timegm(time.strptime(text, fmt)))
        except ValueError:
            continue
    return 0.0


def parse_since(text: str, now: float) -> float:
    """24h, 7d, 30m, or an ISO date or UTC time, as epoch seconds."""
    text = str(text or "").strip()
    found = re.fullmatch(r"([0-9]+)([mhd])", text)
    if found:
        return now - int(found.group(1)) * {"m": 60, "h": 3600, "d": 86400}[found.group(2)]
    stamp = _epoch(text)
    if not stamp:
        raise ValueError("--since is 30m, 24h, 7d or an ISO date or UTC time: %r" % text)
    return stamp


def _counts(entries: Sequence[Mapping[str, Any]]) -> str:
    words = {MERGE: "merge", CLOSE: "close", REOPEN: "reopen", LABEL: "ship or merge label",
             DELETE_BRANCH: "branch deletion", APPROVE: "approving review",
             UNLABEL: "label removal"}
    parts = []
    for action in ACTIONS + (UNLABEL,):
        n = sum(1 for e in entries if e.get("action") == action)
        if n:
            parts.append("%d %s%s" % (n, words[action], "" if n == 1 else "s"))
    return ", ".join(parts)


def digest_lines(since: float, now: float, path: Optional[str] = None,
                 env: Optional[Mapping[str, str]] = None) -> List[str]:
    """At most two lines for the report digest: what Loom would have done
    since the last digest and the command that lists it; nothing when shadow
    is off and nothing was recorded."""
    entries = [e for e in read(path or ledger_path(env), since) if e.get("action") != VERDICT]
    on, repos = scope(env)
    if not on and not entries:
        return []
    which = "every repo" if repos is None else "%d repo%s" % (len(repos), "" if len(repos) == 1 else "s")
    head = ("Shadow mode (%s): " % which) if on else "Shadow ledger: "
    if not entries:
        return [head + "nothing would have merged or closed since the last digest."]
    touched = len({str(e.get("repo") or "") for e in entries})
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since))
    return [head + "Loom would have done %s in %d repo%s since the last digest; none was done."
            % (_counts(entries), touched, "" if touched == 1 else "s"),
            "Review: `python3 -m bus.shadow report --since %s`" % stamp]


def report_lines(entries: Sequence[Mapping[str, Any]], since: float, path: str) -> List[str]:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since))
    actions = [e for e in entries if e.get("action") != VERDICT]
    verdicts = len(entries) - len(actions)
    lines = ["%d would-action%s since %s (%s)%s"
             % (len(actions), "" if len(actions) == 1 else "s", stamp, path,
                ", and %d operator verdict%s" % (verdicts, "" if verdicts == 1 else "s") if verdicts else "")]
    for e in entries:
        lines += _entry_lines(e)
    return lines + _tally_lines(entries)


def _entry_lines(e: Mapping[str, Any]) -> List[str]:
    """One would-action (or operator verdict): its line, then its gates,
    reason and caller."""
    if e.get("action") == VERDICT:
        return _verdict_lines(e)
    what = str(e.get("action") or "")
    if e.get("label"):
        what += " " + str(e["label"])
    ref = "%s#%s" % (e.get("repo") or "?", e.get("number") or "?")
    extra = (["tier=%s" % e["tier"]] if e.get("tier") else []) + \
        (["sha=%s" % str(e["sha"])[:12]] if e.get("sha") else [])
    lines = ["%s  would %-16s %-28s %s  %s" % (
        e.get("time"), what, ref, " ".join(extra), e.get("url") or "")]
    if e.get("gates"):
        lines.append("    gates passed: %s" % ", ".join(str(g) for g in e["gates"]))
    for key, name in (("reason", "reason"), ("source", "from")):
        if e.get(key):
            lines.append("    %s: %s" % (name, e[key]))
    return lines


def _verdict_lines(e: Mapping[str, Any]) -> List[str]:
    """One operator verdict: its line, then its reason and source."""
    lines = ["%s  operator verdict %-8s %-28s %s  %s" % (
        e.get("time"), str(e.get("label") or ""), "%s#%s" % (e.get("repo") or "?", e.get("number") or "?"),
        ("sha=%s" % str(e["sha"])[:12]) if e.get("sha") else "", e.get("url") or "")]
    return lines + ["    %s: %s" % (name, e[key]) for key, name in (("reason", "reason"), ("source", "from"))
                    if e.get(key)]


def _tally_lines(entries: Sequence[Mapping[str, Any]]) -> List[str]:
    tally: Dict[Tuple[str, str, str], int] = {}
    for e in entries:
        if e.get("action") == VERDICT:
            continue
        k = (str(e.get("repo") or "?"), str(e.get("tier") or "-"), str(e.get("action") or ""))
        tally[k] = tally.get(k, 0) + 1
    if not tally:
        return []
    return ["By repo, tier and action:"] + [
        "    %s tier=%s %s: %d" % (repo, tier, action, n)
        for (repo, tier, action), n in sorted(tally.items())]


# -- the brain rung's gh ------------------------------------------------------
# lanes/mac/brain.sh runs the brain's model with a GitHub token, so the model
# could merge or close with gh itself. Its gh asks gh_guard() first.

# The worst body a write could carry: a body the guard cannot read is held
# whenever that path holds any body.
_WORST_BODY = {"state": "closed", "labels": [None], "event": "APPROVE"}
# gh api flags that take a value (the rest are switches).
_GH_API_VALUE_FLAGS = ("-X", "--method", "-H", "--header", "-f", "--raw-field", "-F",
                       "--field", "--input", "-q", "--jq", "-t", "--template", "--cache",
                       "-p", "--preview", "--hostname")
_PR_URL = re.compile(r"github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/(?:pull|issues)/([0-9]+)")


def _flag_values(args: Sequence[str], *names: str) -> List[str]:
    """Every value given to one of the flags `names` (--x v, --x=v, -Xv)."""
    out = []
    for i, arg in enumerate(args):
        for name in names:
            if arg == name and i + 1 < len(args):
                out.append(args[i + 1])
            elif name.startswith("--") and arg.startswith(name + "="):
                out.append(arg[len(name) + 1:])
            elif len(name) == 2 and arg.startswith(name) and len(arg) > 2 and not arg.startswith("--"):
                out.append(arg[2:])
    return out


def _gh_target(args: Sequence[str]) -> Tuple[str, int]:
    """The repo (-R/--repo or a URL) and number a gh pr or issue command names."""
    repo = (_flag_values(args, "-R", "--repo") or [""])[-1]
    number = 0
    for arg in args:
        url = _PR_URL.search(arg)
        if url:
            repo, number = repo or url.group(1), int(url.group(2))
        elif arg.isdigit() and not number:
            number = int(arg)
    return repo, number


def _gh_api_args(args: Sequence[str], cwd: str) -> Tuple[str, str, Dict[str, Any], bool]:
    """(method, path, body fields, body unreadable) of a gh api command."""
    req: Dict[str, Any] = {"method": "", "path": "", "fields": {}, "unreadable": False}
    i = 0
    while i < len(args):
        arg = args[i]
        name, eq, value = arg.partition("=")
        if arg.startswith("-X") and len(arg) > 2:
            req["method"] = arg[2:]
        elif name in _GH_API_VALUE_FLAGS:
            if not eq:
                i += 1
                value = args[i] if i < len(args) else ""
            _gh_api_flag(req, name, value, cwd)
        elif not arg.startswith("-") and not req["path"]:
            req["path"] = arg
        i += 1
    return req["method"], req["path"], req["fields"], req["unreadable"]


def _gh_api_flag(req: Dict[str, Any], name: str, value: str, cwd: str) -> None:
    """Apply one gh api flag that takes a value to the request being read."""
    if name in ("-X", "--method"):
        req["method"] = value
    elif name == "--input":
        req["fields"], unreadable = _gh_input(cwd, value)
        req["unreadable"] = unreadable or req["unreadable"]
        req["method"] = req["method"] or "POST"
    elif name in ("-f", "--raw-field", "-F", "--field"):
        req["unreadable"] = _gh_field(req["fields"], name, value) or req["unreadable"]


def _gh_field(fields: Dict[str, Any], flag: str, value: str) -> bool:
    """Add one -f/-F field to `fields`; True when its value is a file the
    guard does not read (-F key=@file)."""
    key, _, val = value.partition("=")
    if key.endswith("[]"):
        fields.setdefault(key[:-2], []).append(val)
    else:
        fields[key] = val
    return flag in ("-F", "--field") and val.startswith("@")


def _gh_input(cwd: str, name: str) -> Tuple[Dict[str, Any], bool]:
    """The JSON object of a gh api --input file, and whether it could not be
    read (stdin, "-", included: the guard cannot read it and leave it)."""
    try:
        with open(os.path.join(cwd, name), encoding="utf-8") as fh:
            loaded = json.load(fh)
    except (OSError, ValueError):
        return {}, True
    return (loaded, False) if isinstance(loaded, dict) else ({}, True)


def _gh_api_request(args: Sequence[str], cwd: str) -> Optional[Request]:
    method, path, fields, unreadable = _gh_api_args(args, cwd)
    method = (method or ("POST" if fields or unreadable else "GET")).upper()
    if method == "GET":
        return None
    path = path if path.startswith("/") else "/" + path
    if unreadable:
        # The worst body that path takes; a GraphQL body unread is held as is.
        body: Any = None if path.rstrip("/").endswith("/graphql") else _WORST_BODY
    else:
        body = fields
    found = classify(method, path, body)
    if found and "{" in found.repo:
        found = found._replace(repo="")   # gh fills in {owner}/{repo}: any repo
    return found


# gh commands held whatever their flags say: (group, command) -> (action,
# kind, check). An issue close or reopen reads the issue first (gh_guard), as
# the choke point does, so a factory incident closes in shadow too.
_GH_COMMANDS = {("pr", "merge"): (MERGE, "pr", ""), ("issue", "close"): (CLOSE, "issue", "close"),
                ("issue", "reopen"): (REOPEN, "issue", "close")}


def gh_request(argv: Sequence[str], cwd: str = ".") -> Optional[Request]:
    """The held action a gh command line (its arguments after `gh`) is, or
    None. It reads no item: gh_guard reads the issue an issue close or
    reopen names (Request.check), as the choke point does."""
    args = [str(a) for a in argv]
    group, sub = (args + ["", ""])[:2]
    if group == "api":
        return _gh_api_request(args[1:], cwd)
    rest = args[2:]
    repo, number = _gh_target(rest)
    if (group, sub) in _GH_COMMANDS:
        action, kind, check = _GH_COMMANDS[(group, sub)]
        if check:
            # The item read lets a close go only when the command names one
            # issue beyond doubt: else it is held unread, as it was.
            selector = _gh_issue_selector(rest)
            if selector is None:
                return Request(action, repo, number, kind, "gh %s %s" % (group, sub), "")
            repo, number = selector
        return Request(action, repo, number, kind, "gh %s %s" % (group, sub), check)
    if (group, sub) == ("pr", "review") and ("-a" in rest or "--approve" in rest):
        return Request(APPROVE, repo, number, "pr", "gh pr review --approve", "")
    if group in ("pr", "issue") and sub == "edit":
        return _gh_added_labels(repo, number, rest)
    return None


# The flags gh issue close and reopen take a value with (the rest are switches).
_GH_ISSUE_VALUE_FLAGS = ("-c", "--comment", "-r", "--reason", "-R", "--repo", "-b", "--body",
                         "--duplicate-of")
_GH_ISSUE_SWITCHES = ("-h", "--help")
_ISSUE_URL = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/([0-9]+)/?")


def _gh_issue_flag(args: Sequence[str], i: int) -> Optional[Tuple[str, str, int]]:
    """(flag, value, next index) of the gh issue close or reopen flag at
    args[i] (--x v, --x=v, -x v, -xv); None for a flag it does not know or
    one whose value is missing."""
    arg = args[i]
    if arg.startswith("--"):
        flag, eq, value = arg.partition("=")
    else:
        flag, value = arg[:2], arg[2:]
        eq = "=" if value else ""
    if flag in _GH_ISSUE_SWITCHES:
        return flag, "", i + 1
    if flag not in _GH_ISSUE_VALUE_FLAGS:
        return None
    if eq:
        return flag, value, i + 1
    if i + 1 >= len(args):
        return None
    return flag, args[i + 1], i + 2


def _gh_issue_args(args: Sequence[str]) -> Optional[Tuple[str, List[str]]]:
    """(the -R repo, the positionals) of a gh issue close or reopen, read
    the way gh reads them; None when a flag cannot be read."""
    repo, positionals, i = "", [], 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            return repo, positionals + list(args[i + 1:])
        if not arg.startswith("-") or arg == "-":
            positionals.append(arg)
            i += 1
            continue
        read = _gh_issue_flag(args, i)
        if read is None:
            return None
        flag, value, i = read
        if flag in ("-R", "--repo"):
            repo = value
    return repo, positionals


def _gh_issue_target(repo: str, target: str) -> Optional[Tuple[str, int]]:
    """(repo, number) of one selector, a number or an issue URL whose repo
    is the -R one when both name it; None otherwise."""
    if target.isdigit():
        number = int(target)
    else:
        url = _ISSUE_URL.fullmatch(target)
        if not url or (repo and repo.lower() != url.group(1).lower()):
            return None
        repo, number = url.group(1), int(url.group(2))
    if not REPO_RE.fullmatch(repo or "") or number <= 0:
        return None
    return repo, number


def _gh_issue_selector(args: Sequence[str]) -> Optional[Tuple[str, int]]:
    """(repo, number) of the one issue a gh issue close or reopen names;
    None when there is any doubt: a flag it does not know, no positional or
    more than one, a selector that is neither a number nor an issue URL, no
    repo, or a URL whose repo is not the -R one."""
    read = _gh_issue_args(args)
    if read is None or len(read[1]) != 1:
        return None
    return _gh_issue_target(read[0], read[1][0])


def _gh_added_labels(repo: str, number: int, args: Sequence[str]) -> Optional[Request]:
    added = [n.strip().lower() for v in _flag_values(args, "--add-label") for n in v.split(",")]
    found = tuple(dict.fromkeys(n for n in added if n in MERGE_LABELS))
    if not found:
        return None
    return Request(LABEL, repo, number, "issue", ", ".join(found), "label", found)


def _gh_item(found: Request) -> Any:
    """The issue a gh close or reopen names, read with gh api; None when the
    read fails in any way (held)."""
    try:
        out = subprocess.run(["gh", "api", "repos/%s/issues/%d" % (found.repo, found.number)],
                             capture_output=True, text=True, timeout=60, check=False)
        return json.loads(out.stdout) if out.returncode == 0 else None
    except Exception:  # noqa: BLE001 - any failed read holds the action
        return None


def gh_guard(cwd: str, argv: Sequence[str], source: str = "brain rung gh",
             read_item: bool = False) -> int:
    """0 when the gh command may run; 1, after recording it, when shadow mode
    holds it. Any doubt holds it. Only run_lane.sh's own closes (read_item,
    --lane) read the issue and let a factory incident go: the brain rung's
    model chooses its own arguments, so its closes and reopens are held."""
    try:
        found = gh_request(argv, cwd)
    except Exception as err:  # noqa: BLE001 - a command it cannot read is held
        found = Request(MERGE, "", 0, "pr", "unreadable gh command (%s)" % type(err).__name__, "")
    if found is None or not active(found.repo or None):
        return 0
    if (read_item and found.check == "close" and found.repo and found.number
            and held_after_read(found, _gh_item(found)) is None):
        return 0
    reason = "gh %s" % " ".join(argv)[:300]
    for label in found.labels or ("",):
        record_held(found.action, found.repo, found.number, found.kind, reason=reason,
                    label=label, source=source)
    print("shadow mode: would %s %s; recorded in the shadow ledger, not sent. Merge and "
          "close only through python3 -m bus.brain merge and close, which record them "
          "(docs/MUSE.md)." % (found.action, "%s#%d" % (found.repo, found.number)
                               if found.number else (found.repo or found.detail)),
          file=sys.stderr)
    return 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["gh-guard"]:
        # Before argparse: gh's own flags are not this command's.
        if len(argv) < 2:
            print("usage: python3 -m bus.shadow gh-guard CWD GH-ARGS...", file=sys.stderr)
            return 2
        if argv[1:2] == ["--lane"] and len(argv) >= 3:
            # run_lane.sh's own gh writes (its factory incident closes).
            return gh_guard(argv[2], argv[3:], source="run_lane.sh gh", read_item=True)
        return gh_guard(argv[1], argv[2:])
    parser = argparse.ArgumentParser(prog="python3 -m bus.shadow")
    sub = parser.add_subparsers(dest="command", required=True)
    rep = sub.add_parser("report", help="list the would-actions since a time, with links")
    rep.add_argument("--since", default="24h")
    rep.add_argument("--repo", default="")
    rep.add_argument("--ledger", default="")
    rep.add_argument("--json", action="store_true")
    sub.add_parser("status", help="say which repos are in shadow and where the ledger is")
    sub.add_parser("prompt", help="print the brain tick prompt's Shadow mode sentence")
    args = parser.parse_args(argv)
    if args.command == "prompt":
        print(prompt_line())
        return 0
    if args.command == "status":
        print(describe())
        print("ledger: %s" % ledger_path())
        return 0
    now = time.time()
    try:
        since = parse_since(args.since, now)
    except ValueError as err:
        print(err, file=sys.stderr)
        return 2
    path = args.ledger or ledger_path()
    entries = read(path, since, args.repo)
    if args.json:
        print(json.dumps(entries, indent=2, sort_keys=True))
    else:
        print("\n".join(report_lines(entries, since, path)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
