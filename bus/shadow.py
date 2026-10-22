"""Shadow mode: Loom runs as usual but records, instead of taking, every
irreversible GitHub action (docs/BRAIN.md, Shadow mode; loom#1515).

Held while a repo is in shadow:

- a pull request merge (PUT .../pulls/N/merge, POST .../merges, and the
  GraphQL merge and auto-merge mutations), from the factory, `bus.brain
  merge` (the station's and Muse's), the deploy-watch revert path and the
  roll pin;
- an issue close (PATCH state=closed, GraphQL closeIssue or an update to
  CLOSED), except Loom's own signal issues (an invariant breach, a factory
  incident, a health signature) that carry no work label. Closing a pull
  request without merging it is let through: it can be reopened, its branch
  stays, and the factory closes superseded lane pull requests so lanes do
  not work twice;
- a label that grants a merge or marks a ticket shipped (MERGE_LABELS);
- a branch deletion (DELETE .../git/refs/heads/B) other than Loom's own lock.

bus/github.py checks every request against classify() at its one choke point
(GitHub._rest) and raises ShadowHeld instead of sending a held one; `bus.brain
merge`, `bus.brain close` and the station's closes check active() first, so
their record carries the risk tier and the gates that passed.

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

    python3 -m bus.shadow report [--since 24h|7d|2026-10-22[T10:00:00Z]]
                                 [--repo OWNER/NAME] [--json]
    python3 -m bus.shadow status
"""
from __future__ import annotations

import argparse
import calendar
import json
import os
import re
import sys
import time
import traceback
from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Sequence, Tuple

SWITCH = "LOOM_SHADOW"
REPOS = "LOOM_SHADOW_REPOS"
LEDGER = "LOOM_SHADOW_LEDGER"
# The only values that turn shadow off; anything else keeps it on.
OFF = ("0", "false", "off", "no")
REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")

MERGE, CLOSE, LABEL, DELETE_BRANCH = "merge", "close", "label", "delete-branch"
ACTIONS = (MERGE, CLOSE, LABEL, DELETE_BRANCH)

# Labels whose addition grants a merge or ships a ticket, each with the labels
# that mean the ticket is already past it (a verified ticket moved back to
# shipped by a failed audit is not a new ship).
MERGE_LABELS: Dict[str, Tuple[str, ...]] = {
    "ready-to-land": (),
    "loom:shipped": ("loom:verified",),
}
# A close of an issue with one of these is a decision about work: held.
WORK_LABELS = ("loom:ticket", "loom:goal", "loom:backlog", "loom:rewritten")
# An issue Loom files about itself closes on its own rule, in shadow too: an
# invariant that healed, a factory incident whose check passed again, a health
# signature not seen for a day. Holding those would hold the tickets they
# block. Only an issue with one of these and no work label is let through.
SIGNAL_LABELS = ("loom:invariant", "loom:factory", "loom:triage", "loom:recurring")
# Loom's own coordination branches (bus/lock.py LOCK_BRANCH), never a PR head.
OWN_BRANCH_PREFIX = "loom-lock/"

_GRAPHQL_HELD = re.compile(
    r"\b(mergePullRequest|enablePullRequestAutoMerge|mergeBranch|closeIssue|deleteRef)\b")
_GRAPHQL_UPDATE = re.compile(r"\bupdateIssue\b")
_MERGE_PATH = re.compile(r"^/repos/([^/]+/[^/]+)/pulls/([0-9]+)/merge$")
_MERGES_PATH = re.compile(r"^/repos/([^/]+/[^/]+)/merges$")
_ISSUE_PATH = re.compile(r"^/repos/([^/]+/[^/]+)/issues/([0-9]+)$")
_LABELS_PATH = re.compile(r"^/repos/([^/]+/[^/]+)/issues/([0-9]+)/labels$")
_REF_PATH = re.compile(r"^/repos/([^/]+/[^/]+)/git/refs/heads/(.+)$")


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


def classify(method: str, path: str, body: Any) -> Optional[Request]:
    """The held action a REST or GraphQL request is, or None. Pure: the
    exemptions that need the item (a signal issue's close, a label already
    on) are github.py's, named by Request.check."""
    method = str(method or "").upper()
    path = str(path or "").split("?", 1)[0].rstrip("/")
    if "://" in path:
        path = "/" + path.split("://", 1)[1].split("/", 1)[-1]
    if method == "PUT":
        found = _MERGE_PATH.match(path)
        if found:
            return Request(MERGE, found.group(1), int(found.group(2)), "pr", "", "")
    if method == "POST" and _MERGES_PATH.match(path):
        return Request(MERGE, _MERGES_PATH.match(path).group(1), 0, "branch", "merge API", "")
    if method == "PATCH":
        found = _ISSUE_PATH.match(path)
        if found and isinstance(body, dict) and str(body.get("state") or "").lower() == "closed":
            return Request(CLOSE, found.group(1), int(found.group(2)), "issue",
                           str(body.get("state_reason") or "completed"), "close")
        if found and isinstance(body, dict) and "labels" in body:
            held = _label_request(found.group(1), int(found.group(2)), body["labels"])
            if held:
                return held
    if method in ("POST", "PUT"):
        found = _LABELS_PATH.match(path)
        if found:
            held = _label_request(found.group(1), int(found.group(2)),
                                  body.get("labels") if isinstance(body, dict) else body)
            if held:
                return held
    if method == "DELETE":
        found = _REF_PATH.match(path)
        if found and not found.group(2).startswith(OWN_BRANCH_PREFIX):
            return Request(DELETE_BRANCH, found.group(1), 0, "branch", found.group(2), "")
    if method == "POST" and path.endswith("/graphql") and isinstance(body, dict):
        text = str(body.get("query") or "")
        held = _GRAPHQL_HELD.search(text)
        if held:
            name = held.group(1)
            action = (MERGE if "erge" in name else DELETE_BRANCH if name == "deleteRef"
                      else CLOSE)
            return Request(action, "", 0, "pr" if action == MERGE else "issue", name, "")
        if _GRAPHQL_UPDATE.search(text) and "CLOSED" in (
                text + json.dumps(body.get("variables") or {})):
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

def label_is_new(label: str, have: Sequence[str]) -> bool:
    """Whether adding `label` to an item that has `have` is a new grant."""
    have_l = {str(h).lower() for h in have}
    return label not in have_l and not any(x in have_l for x in MERGE_LABELS.get(label, ()))


def close_let_through(issue: Any) -> bool:
    """Whether a close of `issue` (as read just now; None when the read
    failed) goes through in shadow: a pull request (closed unmerged, it can
    be reopened), or Loom's own signal issue closing on its rule (a signal
    label and no work label)."""
    if not isinstance(issue, dict):
        return False
    if "pull_request" in issue:
        return True
    names = {str((x or {}).get("name") or "").lower() if isinstance(x, dict) else str(x).lower()
             for x in issue.get("labels") or []}
    return bool(names & set(SIGNAL_LABELS)) and not names & set(WORK_LABELS)


def ledger_path(env: Optional[Mapping[str, str]] = None) -> str:
    values = _env(env)
    explicit = str(values.get(LEDGER) or "").strip()
    if explicit:
        return explicit
    home = str(values.get("LOOM_HOME") or "").strip() or os.path.join(
        os.path.expanduser("~"), "loom")
    return os.path.join(home, "state", "shadow", "ledger.jsonl")


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
    A write failure is said on stderr and never raised: the caller holds the
    action either way."""
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
    except OSError as err:
        print("shadow: could not write the ledger %s (%s); the action is held anyway"
              % (path, type(err).__name__), file=sys.stderr)
    return dict(entry, new=True)


def read(path: str, since: float = 0.0, repo: str = "") -> List[Dict[str, Any]]:
    """The ledger's entries at or after `since` (epoch seconds), for `repo`
    when given. A missing ledger is empty; an unreadable line is skipped."""
    try:
        with open(path, encoding="utf-8") as fh:
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
    words = {MERGE: "merge", CLOSE: "close", LABEL: "ship or merge label",
             DELETE_BRANCH: "branch deletion"}
    parts = []
    for action in ACTIONS:
        n = sum(1 for e in entries if e.get("action") == action)
        if n:
            parts.append("%d %s%s" % (n, words[action], "" if n == 1 else "s"))
    return ", ".join(parts)


def digest_lines(since: float, now: float, path: Optional[str] = None,
                 env: Optional[Mapping[str, str]] = None) -> List[str]:
    """At most two lines for the report digest: what Loom would have done
    since the last digest and the command that lists it; nothing when shadow
    is off and nothing was recorded."""
    entries = read(path or ledger_path(env), since)
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
    lines = ["%d would-action%s since %s (%s)"
             % (len(entries), "" if len(entries) == 1 else "s", stamp, path)]
    for e in entries:
        what = str(e.get("action") or "")
        if e.get("label"):
            what += " " + str(e["label"])
        ref = "%s#%s" % (e.get("repo") or "?", e.get("number") or "?")
        extra = []
        if e.get("tier"):
            extra.append("tier=%s" % e["tier"])
        if e.get("sha"):
            extra.append("sha=%s" % str(e["sha"])[:12])
        lines.append("%s  would %-16s %-28s %s  %s" % (
            e.get("time"), what, ref, " ".join(extra), e.get("url") or ""))
        if e.get("gates"):
            lines.append("    gates passed: %s" % ", ".join(str(g) for g in e["gates"]))
        if e.get("reason"):
            lines.append("    reason: %s" % e["reason"])
        if e.get("source"):
            lines.append("    from: %s" % e["source"])
    tally: Dict[Tuple[str, str, str], int] = {}
    for e in entries:
        k = (str(e.get("repo") or "?"), str(e.get("tier") or "-"), str(e.get("action") or ""))
        tally[k] = tally.get(k, 0) + 1
    if tally:
        lines.append("By repo, tier and action:")
        for (repo, tier, action), n in sorted(tally.items()):
            lines.append("    %s tier=%s %s: %d" % (repo, tier, action, n))
    return lines


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m bus.shadow")
    sub = parser.add_subparsers(dest="command", required=True)
    rep = sub.add_parser("report", help="list the would-actions since a time, with links")
    rep.add_argument("--since", default="24h")
    rep.add_argument("--repo", default="")
    rep.add_argument("--ledger", default="")
    rep.add_argument("--json", action="store_true")
    sub.add_parser("status", help="say which repos are in shadow and where the ledger is")
    args = parser.parse_args(argv)
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
