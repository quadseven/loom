"""The risk tier of a lane pull request, and whether Loom may merge it alone.

An audit of every lane pull request Loom merged from 2026-10-11 to 2026-10-22
found the lane pull requests the operator merged by hand (10-13 to 10-16) all
good, and from 10-19, when the station and brain Apps merged unattended, 16 of
22 in the private deployment repo damaging or useless. What broke was rarely
app code: a CI build workflow that lost its fallback on every other build
node, a runner-pool affinity on the wrong target, a cloud patch baseline that
turned every preview red, a systemd role deleted while its replacement ran
nowhere. The change class (bus/change_class.py) said logic for each; nothing
weighed what the change can break.

Each pull request gets one tier, the highest of its files and its ticket:

- docs-tests: only documentation and test files that no higher rule
  matches (a test-named manifest or IaC file is still deploy-config). A
  renamed file takes the higher tier of its old and new paths.
- app-code: application code and anything not named below.
- deploy-config: what deploys or configures a running system: manifests,
  Helm values, Dockerfiles, IaC (Pulumi, Terraform, Ansible), systemd units,
  runtime config files (*.conf, *.ini, *.cfg, *.env), dependency manifests,
  and anything under a deploy, production, infra or similar directory.
- security: CI workflows and actions (they run with secrets), CODEOWNERS,
  Loom's own .loom/ config, a sensitive-class pull request, and code whose
  path names auth, secrets, tokens, credentials, permissions, policy,
  sandboxing, TLS or the leak scan.
- cross-repo: it moves another repo's pin (a submodule gitlink, .gitmodules,
  a pin.env or *PINS* file) or its ticket names another repo of the owner.

OPERATOR tiers (by default deploy-config, security, cross-repo) are never
merged by Loom: the factory holds them for the operator with one ask, and
`bus.brain merge` refuses them. LOOM_AUTO_MERGE_TIERS lists the tiers Loom
may merge on its own (default "docs-tests,app-code"); every other tier waits
for the operator. A revert ticket's own pull request (bus/revert.py,
loom:revert) is exempt from tier, review and caps only when it is proven
(revert_refusal): a revert labeler, not a lane, applied the label, the body
names one commit the base branch carries, and the diff is that commit's exact
inverse. Otherwise the normal gates apply to it.

An auto tier merges only when it is clean (bus.brain merge): the review check
succeeded on the head commit (neutral is not enough), and no high or critical
review-bot finding, and no finding from a BAD_WORK rule (the review bot's
unmet-criterion and intent detectors, at any severity), is open or was
resolved with no commit after it. Every review thread is read, all pages; a
list that cannot be read in full refuses the merge.
Such a finding is never deferred as minor (bus/factory.py).

The gate reads review bodies as well as threads (loom#1933). A review
body is the verdict comment's sibling: the bot's findings also live in the
body of the review it submitted, and a merge that answers every thread can
still leave that body unanswered, which the bot's Sentinel later reports
as an abandoned review. So a bot review whose body carries findings and
that no pull-request comment answers holds the merge while any finding it
names is still open (its newest thread unresolved, or no thread at all).
When every finding it names reads resolved or addressed, the gate posts
one answer naming each finding's state and merges. The newest thread per
rule sets the state: an answer to an older duplicate thread is no answer
to the thread the bot opened last (the PR #1996 fixture). A high or
critical lint finding that reads addressed is not taken on its mark
either: its ruff rule is re-run on the head's version of its file, and a
finding that still hits, or whose check cannot run, holds the merge. A
Sentinel abandoned-review notice already left on a merged pull request is
settled by the factory (bus/factory.py settle_abandoned), which files one
follow-up and answers the notice only when the same re-check passes on
the merged branch. Auto-merges are
also capped: at most LOOM_AUTO_MERGE_PER_HOUR (default 4) loom/ pull
requests merged in the last hour across the owner, and at most
LOOM_AUTO_MERGE_PER_REPO_HOUR (default 2) in one repo. The counts come from
GitHub, so every brain and station reads the same numbers; a merge by hand of
a loom/ branch counts too. A count GitHub will not give refuses the merge.
"""
from __future__ import annotations

import base64
import fnmatch
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Container, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote

from . import change_class, status
from .github import GitHubError

DOCS_TESTS = "docs-tests"
APP_CODE = "app-code"
DEPLOY_CONFIG = "deploy-config"
SECURITY = "security"
CROSS_REPO = "cross-repo"
REVERT = "revert"
# Lowest risk first: a pull request takes the highest tier any part of it has.
TIERS = (DOCS_TESTS, APP_CODE, DEPLOY_CONFIG, SECURITY, CROSS_REPO)
DEFAULT_AUTO = (DOCS_TESTS, APP_CODE)
DEFAULT_PER_HOUR = 4
DEFAULT_PER_REPO_HOUR = 2
REVERT_LABEL = "loom:revert"
FILES_SHOWN = 4

# Paths that run with the repo's secrets or decide who may change what.
_SECURITY_PATHS = (".github/workflows/", ".github/actions/", ".loom/")
_SECURITY_NAMES = ("codeowners",)
_SECURITY_WORD = re.compile(
    r"(?:^|[/_.-])(?:auth|authn|authz|authenticat\w*|authori[sz]\w*|secrets?|tokens?|credentials?|creds|crypto|permissions?|rbac|acls?"
    r"|policy|policies|sandbox\w*|leak\w*|oauth|jwt|passwords?|keychain|tls|certs?|ssh"
    r"|firewall|iam|sudoers)(?:$|[/_.-])", re.I)
# Dependency manifests change what runs without a line of code.
_DEPENDENCY_NAMES = ("requirements.txt", "requirements-dev.txt", "pyproject.toml", "setup.py",
                     "setup.cfg", "package.json", "package-lock.json", "yarn.lock",
                     "pnpm-lock.yaml", "go.mod", "go.sum", "cargo.toml", "cargo.lock",
                     "gemfile", "gemfile.lock", "poetry.lock", "uv.lock")
_DEPLOY_DIRS = {"manifests", "k8s", "kubernetes", "kube", "helm", "charts", "deploy",
                "deploys", "deployment", "deployments", "production", "prod", "staging",
                "pulumi", "terraform", "ansible", "roles", "playbooks", "host_vars",
                "group_vars", "inventory", "systemd", "launchd", "cloud-init", "infra",
                "ops", "argocd", "flux", "overlays"}
_DEPLOY_GLOBS = ("dockerfile*", "containerfile*", "*.dockerfile", "docker-compose*.y*ml",
                 "compose*.y*ml", "*.tf", "*.tfvars", "*.hcl", "*.service", "*.timer",
                 "*.socket", "*.plist", "kustomization.y*ml", "chart.yaml", "values*.y*ml",
                 "*.env", "*.conf", "*.ini", "*.cfg", "fly.toml", "procfile", "*.nomad")
_PIN_GLOBS = (".gitmodules", "pin.env", "*pins*", "*pins*.env")
GITLINK_RE = re.compile(r"^[+-]Subproject commit [0-9a-f]{7,40}", re.M)
_OWNER_REPO_RE = re.compile(r"(?<![\w.-])([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9._-]+)")
# Lines of a ticket that cite rather than ask: they name repos without
# asking for a change there.
_CITATION_RE = re.compile(r"^\s*(?:Refs?|Part of|Rewritten from|Follows|See|Source)\b.*$",
                          re.I | re.M)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
# A "Base: owner/name" line names the branch a lane PR targets, never a repo.
_BASE_RE = re.compile(r"^\s*Base\s*:.*$", re.I | re.M)
# The status section Loom keeps on a ticket is Loom's own log, not the
# ticket's words: a line such as "Factory incident owner/loom#N is closed"
# names a repo without asking for a change there (loom#1959).
_STATUS_RE = re.compile("%s.*?%s" % (re.escape(status.BODY_BEGIN), re.escape(status.BODY_END)),
                        re.S)

# The review bot opens each finding with "<symbol> <severity> | _<category>_ |
# `<rule>` | <effort>".
FINDING_RE = re.compile(r"^\W*(critical|high|medium|low|info)\s*\|\s*_?([^|]*?)_?\s*\|"
                        r"\s*`([^`]+)`", re.I)
HIGH_SEVERITIES = ("critical", "high")
# The review bot's rules that say the work does not do what it claims: an
# open one blocks an auto-merge at any severity, and is never deferred.
# LOOM_ELDER_BLOCKING_RULES adds more (comma-separated); a finding whose
# category is bad-work blocks too.
BAD_WORK_RULES = ("intent-mismatch", "unmet-criterion", "dead-code", "claims-not-delivered",
                  "claimed-not-delivered", "measurement-change", "metric-not-cause",
                  "fabricated-evidence", "simulated-evidence", "stale-rework",
                  "ticked-unverified", "inert-mechanism", "doc-rewrite")
BAD_WORK_CATEGORY = "bad-work"


def _env(env: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    return os.environ if env is None else env


def auto_tiers(env: Optional[Mapping[str, str]] = None) -> Tuple[str, ...]:
    """The tiers Loom may merge on its own (LOOM_AUTO_MERGE_TIERS); a name
    that is no tier is ignored, and an empty setting merges none."""
    raw = _env(env).get("LOOM_AUTO_MERGE_TIERS")
    if raw is None:
        return DEFAULT_AUTO
    return tuple(t for t in (x.strip().lower() for x in raw.split(",")) if t in TIERS)


def _count_setting(env: Optional[Mapping[str, str]], name: str, default: int) -> int:
    raw = str(_env(env).get(name, "")).strip()
    return int(raw) if raw.isdigit() else default


def blocking_rules(env: Optional[Mapping[str, str]] = None) -> Tuple[str, ...]:
    extra = [r.strip().lower() for r in str(_env(env).get("LOOM_ELDER_BLOCKING_RULES", "")).split(",")]
    return BAD_WORK_RULES + tuple(r for r in extra if r and r not in BAD_WORK_RULES)


def _name_matches(name: str, globs: Sequence[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, g) for g in globs)


def file_tier(path: str) -> str:
    """The tier of one changed file. The higher tiers are read first: a file
    is docs-tests only when no deploy, security or pin rule matches, so a
    test-named manifest, an IaC file under tests/ or a doc under a deploy
    directory is never low risk."""
    lowered = (path or "").strip().lower()
    lowered = lowered[2:] if lowered.startswith("./") else lowered
    name = lowered.rsplit("/", 1)[-1]
    if _name_matches(name, _PIN_GLOBS):
        return CROSS_REPO
    if (lowered.startswith(_SECURITY_PATHS) or name in _SECURITY_NAMES
            or _SECURITY_WORD.search(lowered)):
        return SECURITY
    if (name in _DEPENDENCY_NAMES or name.startswith("requirements")
            or _DEPLOY_DIRS & set(lowered.split("/")[:-1]) or _name_matches(name, _DEPLOY_GLOBS)):
        return DEPLOY_CONFIG
    if change_class.is_test(lowered) or change_class.is_docs(lowered):
        return DOCS_TESTS
    return APP_CODE


def entry_tier(entry: Mapping[str, Any]) -> str:
    """The tier of one changed-file entry: the higher of its path and, for a
    rename, the path it had (moving a manifest under tests/ is still a
    manifest change), and cross-repo for a submodule gitlink."""
    if GITLINK_RE.search(str(entry.get("patch") or "")):
        return CROSS_REPO
    paths = [str(entry.get("filename") or "")]
    if entry.get("previous_filename"):
        paths.append(str(entry["previous_filename"]))
    return max((file_tier(p) for p in paths), key=_rank)


def other_repos(repo: str, ticket_body: str) -> List[str]:
    """Other repos of the same owner the ticket's own words name (citation
    lines, Base: lines, HTML comments and Loom's status section aside)."""
    owner, _, name = repo.partition("/")
    text = _STATUS_RE.sub("", ticket_body or "")
    text = _BASE_RE.sub("", _CITATION_RE.sub("", _COMMENT_RE.sub("", text)))
    found = []
    for who, what in _OWNER_REPO_RE.findall(text):
        what = what.rstrip(".")
        if who.lower() == owner.lower() and what.lower() != name.lower():
            found.append("%s/%s" % (who, what))
    return list(dict.fromkeys(found))


def _rank(tier: str) -> int:
    return TIERS.index(tier)


def _shown(paths: Sequence[str]) -> str:
    return ", ".join(paths[:FILES_SHOWN]) + (
        " and %d more" % (len(paths) - FILES_SHOWN) if len(paths) > FILES_SHOWN else "")


def classify(repo: str, entries: Sequence[Mapping[str, Any]], ticket_body: str = "",
             sensitive: bool = False) -> Tuple[str, str]:
    """(tier, why) for a pull request with these changed-file entries (REST
    files: filename and, when read, patch). `sensitive` is the pull request's
    class:sensitive label."""
    best = DOCS_TESTS
    files: List[str] = []
    for entry in entries:
        path = str(entry.get("filename") or "")
        tier = entry_tier(entry)
        if _rank(tier) > _rank(best):
            best, files = tier, [path]
        elif tier == best:
            files.append(path)
    if not entries:
        # No file list proves nothing about risk: the operator merges it.
        best = SECURITY
    if sensitive and _rank(SECURITY) > _rank(best):
        return SECURITY, "it carries class:sensitive"
    repos = other_repos(repo, ticket_body)
    if repos and _rank(CROSS_REPO) > _rank(best):
        return CROSS_REPO, "its ticket names another repo (%s)" % _shown(repos)
    if not entries:
        return best, "its changed files could not be read"
    return best, "it changes %s" % _shown(files)


def is_revert_ticket(pr: Mapping[str, Any], ticket: Optional[Mapping[str, Any]]) -> bool:
    """The pull request says it is a revert and its ticket carries
    loom:revert. Neither proves it: see revert_exempt."""
    from . import revert
    names = {str((l or {}).get("name") if isinstance(l, dict) else l)
             for l in (ticket or {}).get("labels") or []}
    return REVERT_LABEL in names and revert.looks_like_revert(dict(pr))


def revert_labelers(env: Optional[Mapping[str, str]] = None) -> List[str]:
    """The logins whose loom:revert label is believed: LOOM_REVERT_LABELERS,
    else this Loom's trusted logins (LOOM_BRIEF_AUTHORS) other than a lane's
    (a login with "lane" in it): the lane that wrote the change must not be
    able to exempt it."""
    raw = _env(env).get("LOOM_REVERT_LABELERS")
    if raw is not None:
        return raw.replace(",", " ").split()
    authors = str(_env(env).get("LOOM_BRIEF_AUTHORS", "")).replace(",", " ").split()
    return [a for a in authors if "lane" not in a.lower()]


def _patch_lines(patch: str) -> Tuple[List[str], List[str]]:
    """(added, removed) lines of a unified diff hunk text, sorted."""
    lines = (patch or "").splitlines()
    added = sorted(l[1:] for l in lines if l.startswith("+") and not l.startswith("+++"))
    removed = sorted(l[1:] for l in lines if l.startswith("-") and not l.startswith("---"))
    return added, removed


def exact_inverse(entries: Sequence[Mapping[str, Any]],
                  commit_files: Sequence[Mapping[str, Any]]) -> bool:
    """Whether the pull request's files are the commit's files, each with
    the commit's added lines removed and its removed lines added. A rename,
    or a file GitHub sent no patch for, is not proven."""
    mine = {str(e.get("filename") or ""): e for e in entries}
    theirs = {str(f.get("filename") or ""): f for f in commit_files}
    if not mine or set(mine) != set(theirs):
        return False
    for name, entry in mine.items():
        other = theirs[name]
        if (entry.get("previous_filename") or other.get("previous_filename")
                or "patch" not in entry or "patch" not in other):
            return False
        added, removed = _patch_lines(str(entry["patch"]))
        if (added, removed) != tuple(reversed(_patch_lines(str(other["patch"])))):
            return False
    return True


def _labeled_by(gh: Any, repo: str, number: int) -> str:
    """Who last applied loom:revert to the ticket, from its events."""
    who = ""
    for event in gh.paginate("/repos/%s/issues/%d/events" % (repo, number)):
        if (isinstance(event, dict) and event.get("event") == "labeled"
                and (event.get("label") or {}).get("name") == REVERT_LABEL):
            who = str((event.get("actor") or {}).get("login") or "")
    return who


def revert_refusal(gh: Any, repo: str, pr: Mapping[str, Any], ticket: Mapping[str, Any],
                   entries: Sequence[Mapping[str, Any]],
                   env: Optional[Mapping[str, str]] = None) -> str:
    """"" when the pull request is exempt from tier, review and caps as a
    revert, else why not. Exempt means all of: it says it is a revert and its
    ticket carries loom:revert (is_revert_ticket); a revert labeler
    (revert_labelers) applied that label; its body names exactly one commit
    (`git revert <sha>`), which the pull request's base branch carries; and its
    diff is that commit's exact inverse. Anything that cannot be read is not
    exempt, and the normal gates apply."""
    from . import revert
    if not is_revert_ticket(pr, ticket):
        return "it is not a revert ticket's pull request"
    try:
        who = _labeled_by(gh, repo, int(ticket.get("number") or 0))
        if who.lower() not in {l.lower() for l in revert_labelers(env)}:
            return "loom:revert was applied by %s, not by the station" % (who or "nobody")
        shas = [k[4:] for k in revert.reverted_keys(dict(pr)) if k.startswith("sha:")]
        if len(set(shas)) != 1:
            return "its body does not name exactly one reverted commit"
        base = str((pr.get("base") or {}).get("ref") or "")
        compared = gh.rest("GET", "/repos/%s/compare/%s...%s"
                           % (repo, shas[0], quote(base, safe=""))).data or {}
        if not base or compared.get("status") not in ("ahead", "identical"):
            return "%s does not carry the reverted commit %s" % (base or "its base", shas[0])
        commit = gh.rest("GET", "/repos/%s/commits/%s" % (repo, shas[0])).data or {}
    except (GitHubError, ValueError, TypeError) as err:
        return "its revert could not be checked (%s)" % err
    if not exact_inverse(entries, [f for f in commit.get("files") or [] if isinstance(f, dict)]):
        return "its diff is not the exact inverse of %s" % shas[0]
    return ""


def lasting_hold(labels: Sequence[str], head_ref: str, mergeable: Optional[bool],
                 env: Optional[Mapping[str, str]] = None) -> str:
    """Why no brain can merge a pull request until a person moves it, else "".
    An operator tier label is the operator's to merge; a head outside loom/
    that conflicts with its base is its author's to update, since
    `bus.brain fix` sends only a loom/ head back to a lane. Such a merge is
    not stage work, so it is never deferred to the next brain (loom#2667).
    A mergeable GitHub has not computed yet (None) is no hold."""
    auto = auto_tiers(env)
    for name in labels:
        tier = name[len("tier:"):] if name.startswith("tier:") else ""
        if tier in TIERS and tier not in auto:
            return "waits on the operator (tier:%s, which Loom never merges)" % tier
    if mergeable is False and not head_ref.startswith("loom/"):
        return ("its head %s is not a loom branch and conflicts with its base, so only "
                "its author can update it" % (head_ref or "(unknown)"))
    return ""


def operator_why(tier: str, why: str, env: Optional[Mapping[str, str]] = None) -> str:
    """The hold reason for an operator tier, for operator_hold.ask."""
    return ("it is a %s change (%s), and Loom merges only %s changes on its own"
            % (tier, why, " and ".join(auto_tiers(env)) or "no"))


# -- review state --------------------------------------------------------
def finding(body: str) -> Optional[Tuple[str, str, str]]:
    """(severity, category, rule) from a review-bot finding's header, or None."""
    match = FINDING_RE.match(body or "")
    if not match:
        return None
    return (match.group(1).lower(), match.group(2).strip().strip("_").lower(),
            match.group(3).strip().lower())


def bad_work(body: str, env: Optional[Mapping[str, str]] = None) -> bool:
    """Whether a finding comes from a bad-work rule: at any severity it holds
    an auto-merge and is never deferred."""
    found = finding(body)
    return bool(found) and (found[2] in blocking_rules(env) or BAD_WORK_CATEGORY in found[1])


THREADS_QUERY = (
    "query($owner:String!,$name:String!,$number:Int!,$after:String) {"
    " repository(owner:$owner,name:$name) { pullRequest(number:$number) {"
    " reviewThreads(first:100,after:$after) { pageInfo { hasNextPage endCursor }"
    " nodes { id isResolved path comments(first:1) { nodes {"
    " author { __typename login } body createdAt } } } } } } }")
THREAD_PAGES = 20


def review_threads(gh: Any, repo: str, number: int) -> List[Mapping[str, Any]]:
    """Every review thread of the pull request, all pages. Raises ValueError
    when the whole list cannot be read: a finding past a page the read
    missed would let the merge through."""
    owner, _, name = repo.partition("/")
    threads: List[Mapping[str, Any]] = []
    after = None
    for _ in range(THREAD_PAGES):
        try:
            data = gh.graphql(THREADS_QUERY, {"owner": owner, "name": name,
                                              "number": number, "after": after}) or {}
        except GitHubError as err:
            raise ValueError("could not read the review threads (%s)" % err) from err
        page = (((data.get("repository") or {}).get("pullRequest") or {})
                .get("reviewThreads") or {})
        info = page.get("pageInfo")
        if not isinstance(info, dict) or not isinstance(page.get("nodes"), list):
            raise ValueError("could not read every review thread")
        threads += [t for t in page["nodes"] if isinstance(t, dict)]
        if not info.get("hasNextPage"):
            return threads
        after = info.get("endCursor")
        if not after:
            raise ValueError("could not read every review thread")
    raise ValueError("more than %d pages of review threads" % THREAD_PAGES)


def fix_committed(commits: Sequence[Mapping[str, Any]]) -> str:
    """The committedDate of the newest commit that is not a base merge, from
    a pull request's commit nodes (oldest first), else "". A merge commit
    (two parents, as GitHub's update branch makes) is newer than any finding
    but proves no fix (loom#2285)."""
    for node in reversed(list(commits)):
        commit = (node or {}).get("commit") or {}
        parents = (commit.get("parents") or {}).get("totalCount")
        if isinstance(parents, int) and parents < 2:
            return str(commit.get("committedDate") or "")
    return ""


def blocking_findings(threads: Sequence[Mapping[str, Any]], committed: str,
                      by_bot: Callable[[Mapping[str, Any]], bool],
                      env: Optional[Mapping[str, str]] = None,
                      held: Container[str] = ()) -> List[str]:
    """The review-bot findings that hold an auto-merge: high or critical, or
    from a bad-work rule, whose thread is open or was resolved with no commit
    after it (deferred, or resolved by hand without a fix). `threads` are
    every review thread (review_threads), `committed` the date of the newest
    commit that can be a fix (fix_committed), and `by_bot` says whether a
    comment is the review bot's. `held` names the resolved threads the
    station marked held for the head (bus/brain.py _station_held); it never
    exempts a high, critical or bad-work finding, so every finding returned
    here blocks whether held or not (loom#2519). `held` is therefore unused
    here; it stays in the signature until the plumbing through
    brain._refuse_risk_tier is removed. A missing time cannot show a fix
    followed."""
    out = []
    for thread in threads:
        comments = (thread.get("comments") or {}).get("nodes") or []
        root = (comments[0] if comments else None) or {}
        body = str(root.get("body") or "")
        found = finding(body)
        if not found or not by_bot(root) or not (found[0] in HIGH_SEVERITIES or bad_work(body, env)):
            continue
        created = str(root.get("createdAt") or "")
        if not thread.get("isResolved") or not committed or not created or created >= committed:
            out.append("%s %s" % (found[0], found[2]))
    return out


# -- review bodies and the lint re-check (loom#1933) ----------------------
REVIEW_ANSWER_MARKER = "<!-- loom:review-answer v1 -->"
_STATE_WORDS = {
    "addressed": "addressed (its thread is resolved, and a commit on this "
                 "pull request follows it)",
    "resolved": "resolved (its thread is resolved)",
    "open": "still open",
}
_LINT_RULE_RE = re.compile(r"^[a-z]{1,7}[0-9]+$")


def body_findings(body: str) -> List[Tuple[str, str, str]]:
    """The findings a review body carries, one per finding header line.
    Prose names no finding: a header line needs the severity, the category
    and the backticked rule of FINDING_RE, as a thread's root does."""
    out = []
    for line in str(body or "").splitlines():
        found = finding(line)
        if found:
            out.append(found)
    return out


def _thread_root(thread: Mapping[str, Any]) -> Mapping[str, Any]:
    comments = (thread.get("comments") or {}).get("nodes") or []
    return (comments[0] if comments else None) or {}


def thread_states(threads: Sequence[Mapping[str, Any]], committed: str,
                  by_bot: Callable[[Mapping[str, Any]], bool]) -> Dict[str, Tuple[str, str]]:
    """{rule: (state, path)} for the review bot's findings, the state set
    by the newest thread per rule: "open" (that thread is unresolved),
    "addressed" (resolved, and a commit on the pull request follows it:
    the thread opened before `committed`, the newest non-merge commit),
    or "resolved" (resolved, with no such commit read). A newer duplicate
    thread replaces an older one's state: an answer to a duplicate is no
    answer to the thread the bot opened last (loom#1933)."""
    states: Dict[str, Tuple[str, str]] = {}
    seen: Dict[str, str] = {}
    for thread in threads:
        root = _thread_root(thread)
        found = finding(str(root.get("body") or ""))
        if not found or not by_bot(root):
            continue
        created = str(root.get("createdAt") or "")
        if found[2] in seen and seen[found[2]] > created:
            continue
        seen[found[2]] = created
        if not thread.get("isResolved"):
            state = "open"
        elif committed and created and created < committed:
            state = "addressed"
        else:
            state = "resolved"
        states[found[2]] = (state, str(thread.get("path") or ""))
    return states


def pull_reviews(gh: Any, repo: str, number: int) -> List[Mapping[str, Any]]:
    """Every review on the pull request. Raises ValueError when the list
    cannot be read: a review the gate never saw is one it cannot answer."""
    try:
        data = gh.paginate("/repos/%s/pulls/%d/reviews" % (repo, number))
    except GitHubError as err:
        raise ValueError("could not read the pull request's reviews (%s)" % err) from err
    if not isinstance(data, list):
        raise ValueError("could not read the pull request's reviews")
    return [r for r in data if isinstance(r, dict)]


def pull_comments(gh: Any, repo: str, number: int) -> List[Mapping[str, Any]]:
    """Every comment on the pull request. Raises ValueError when the list
    cannot be read: an answer the gate never saw would be posted twice.
    The refusal carries the fleet hold's phrase for the same read
    (bus/brain.py _hold_blocker): one root cause, one wording."""
    try:
        data = gh.paginate("/repos/%s/issues/%d/comments" % (repo, number))
    except GitHubError as err:
        raise ValueError("pull request comments unreadable, so an unanswered "
                         "review body cannot be ruled out (%s)" % err) from err
    if not isinstance(data, list):
        raise ValueError("pull request comments unreadable, so an unanswered "
                         "review body cannot be ruled out")
    return [c for c in data if isinstance(c, dict)]


def _review_author(review: Mapping[str, Any]) -> Dict[str, Any]:
    """A REST review in the comment shape by_bot reads, its author the
    login without the "[bot]" suffix the REST API adds, the type as a
    typename."""
    user = review.get("user") or {}
    login = str(user.get("login") or "")
    if login.endswith("[bot]"):
        login = login[:-len("[bot]")]
    return {"author": {"__typename": "Bot" if user.get("type") == "Bot" else "User",
                       "login": login}}


def _body_answered(review: Mapping[str, Any],
                   comments: Sequence[Mapping[str, Any]]) -> bool:
    """A pull-request comment answers a review body when it links the
    review: its #pullrequestreview-<id> anchor or its URL. A reply inside
    a thread answers the thread, not the body, so only comments count."""
    marks = ["#pullrequestreview-%s" % review.get("id")]
    if review.get("html_url"):
        marks.append(str(review["html_url"]))
    return any(mark in str(c.get("body") or "")
               for c in comments for mark in marks)


def unanswered_bodies(gh: Any, repo: str, number: int,
                      threads: Sequence[Mapping[str, Any]], committed: str,
                      by_bot: Callable[[Mapping[str, Any]], bool]) -> List[Dict[str, Any]]:
    """The review bot's reviews on the pull request whose body carries
    findings and that no pull-request comment answers, each as
    {"review", "findings", "settled"}: findings a list of
    (finding, state, path) with the state from thread_states, and settled
    when no finding reads open. A finding with no thread reads open: only
    an answer settles it. A dismissed review is not outstanding, and
    neither is a person's review."""
    reviews = pull_reviews(gh, repo, number)
    comments = pull_comments(gh, repo, number)
    states = thread_states(threads, committed, by_bot)
    out = []
    for review in reviews:
        if str(review.get("state") or "").upper() == "DISMISSED":
            continue
        if not by_bot(_review_author(review)):
            continue
        found = body_findings(str(review.get("body") or ""))
        if not found or _body_answered(review, comments):
            continue
        rows = [(f,) + states.get(f[2], ("open", "")) for f in found]
        out.append({"review": review, "findings": rows,
                    "settled": all(state != "open" for _, state, _ in rows)})
    return out


def body_refusal(bodies: Sequence[Dict[str, Any]]) -> str:
    """Why unanswered review bodies hold the merge, or "" when every
    finding named in them reads settled."""
    out = []
    for body in bodies:
        if body["settled"]:
            continue
        review = body["review"]
        for found, state, _ in body["findings"]:
            if state == "open":
                out.append("%s %s in the review of %s is still open"
                           % (found[0], found[2],
                              review.get("submitted_at") or "an unknown time"))
    if not out:
        return ""
    return "unanswered review bodies hold the merge: %s" % "; ".join(out)


def answer_comment(bodies: Sequence[Dict[str, Any]]) -> str:
    """The gate's one answer to settled review bodies: it links each
    review (so the body reads answered from here on) and says how each
    finding it names reads on the head."""
    parts = [REVIEW_ANSWER_MARKER]
    for body in bodies:
        review = body["review"]
        lines = ["Answering the review of %s (%s): every finding in its body "
                 "reads settled on this head, so the merge gate answers it "
                 "here rather than leave the review body unanswered."
                 % (review.get("submitted_at") or "an unknown time",
                    review.get("html_url") or "")]
        for found, state, path in body["findings"]:
            where = " on `%s`" % path if path else ""
            lines.append("- %s `%s`%s: %s"
                         % (found[0], found[2], where, _STATE_WORDS[state]))
        parts.append("\n".join(lines))
    return "\n\n".join(parts) + "\n"


def lint_rule(rule: str) -> bool:
    """Whether a finding's rule is a ruff code (S110, E501), which a
    re-check can run, rather than one of the review bot's own word rules
    (dead-code, intent-mismatch), which no tool re-checks."""
    return bool(_LINT_RULE_RE.match(rule or ""))


def lint_targets(threads: Sequence[Mapping[str, Any]], committed: str,
                 by_bot: Callable[[Mapping[str, Any]], bool]) -> List[Tuple[str, str, str]]:
    """(severity, rule, path) for each high or critical lint finding whose
    thread reads addressed: resolved, with a commit on the pull request
    after it. Those findings are the ones a merge most trusts on a mark,
    so those are the ones the gate re-checks on the head (loom#1933)."""
    out = []
    seen = set()
    for thread in threads:
        root = _thread_root(thread)
        found = finding(str(root.get("body") or ""))
        if not found or not by_bot(root) or found[0] not in HIGH_SEVERITIES:
            continue
        if not lint_rule(found[2]) or not thread.get("isResolved"):
            continue
        created = str(root.get("createdAt") or "")
        if not committed or not created or created >= committed:
            continue
        key = (found[2], str(thread.get("path") or ""))
        if key not in seen:
            seen.add(key)
            out.append((found[0], found[2], str(thread.get("path") or "")))
    return out


def run_ruff(rule: str, path: str, text: str,
             runner: Optional[Callable[..., Any]] = None) -> Optional[bool]:
    """Run one ruff rule over `text` as the file at `path`: True when the
    rule still hits, False when it passes, None when the check could not
    run (no ruff, or ruff answered with an error): a check that cannot
    run proves nothing. `runner` is subprocess.run, injectable."""
    run = runner or subprocess.run
    try:
        done = run(["ruff", "check", "--select", rule.upper(), "--isolated",
                    "--no-cache", "--stdin-filename", path, "-"],
                   input=text, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode == 0:
        return False
    if done.returncode == 1:
        return True
    return None


def _head_text(gh: Any, repo: str, sha: str, path: str) -> Optional[str]:
    """The head's version of one file, or None when GitHub will not
    send it."""
    if not path:
        return None
    try:
        data = gh.rest("GET", "/repos/%s/contents/%s?ref=%s"
                       % (repo, quote(path, safe="/"), quote(sha, safe=""))).data
    except GitHubError:
        return None
    if not isinstance(data, dict) or data.get("encoding") != "base64":
        return None
    try:
        return base64.b64decode(str(data.get("content") or "")).decode("utf-8", "replace")
    except (ValueError, TypeError):
        return None


def lint_recheck_refusal(gh: Any, repo: str, sha: str,
                         threads: Sequence[Mapping[str, Any]], committed: str,
                         by_bot: Callable[[Mapping[str, Any]], bool],
                         run: Optional[Callable[..., Optional[bool]]] = None) -> str:
    """Why addressed high lint findings hold the merge, or "": each is
    re-checked with its own ruff rule on the head's version of its file
    (lint_targets). One that still hits, or whose check could not run,
    holds the merge: "addressed" was a mark on a thread, never a
    verification (loom#1933). `run` is run_ruff, injectable."""
    check = run or run_ruff
    hits, unknown = [], []
    for severity, rule, path in lint_targets(threads, committed, by_bot):
        text = _head_text(gh, repo, sha, path)
        verdict = None if text is None else check(rule, path, text)
        if verdict is True:
            hits.append("%s %s on %s" % (severity, rule, path))
        elif verdict is None:
            unknown.append("%s %s on %s" % (severity, rule, path))
    out = []
    if hits:
        out.append("addressed findings still hit on the head: %s" % "; ".join(hits))
    if unknown:
        out.append("addressed findings could not be re-checked on the head: %s"
                   % "; ".join(unknown))
    return "; ".join(out)


# -- caps ------------------------------------------------------------------
def _since(now: datetime) -> str:
    return (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def merged_last_hour(gh: Any, owner: str, now: datetime, repo: str = "") -> int:
    """loom/ pull requests merged in the hour before `now`, across the owner
    or in one repo, from GitHub's search. Raises when GitHub will not say."""
    scope = "repo:%s" % repo if repo else "user:%s" % owner
    query = "is:pr is:merged head:loom/ merged:>=%s %s" % (_since(now), scope)
    data = gh.rest("GET", "/search/issues?q=%s&per_page=1" % quote(query, safe="")).data
    if not isinstance(data, dict) or not isinstance(data.get("total_count"), int):
        raise ValueError("GitHub gave no count of recent merges")
    return int(data["total_count"])


def cap_refusal(gh: Any, repo: str, env: Optional[Mapping[str, str]] = None,
                now: Optional[datetime] = None) -> str:
    """Why the hourly caps refuse one more auto-merge in `repo`, or ""."""
    now = now or datetime.now(timezone.utc)
    per_repo = _count_setting(env, "LOOM_AUTO_MERGE_PER_REPO_HOUR", DEFAULT_PER_REPO_HOUR)
    per_hour = _count_setting(env, "LOOM_AUTO_MERGE_PER_HOUR", DEFAULT_PER_HOUR)
    try:
        in_repo = merged_last_hour(gh, repo.partition("/")[0], now, repo)
        if in_repo >= per_repo:
            return ("%d loom/ pull requests merged in %s in the last hour, the cap is %d "
                    "(LOOM_AUTO_MERGE_PER_REPO_HOUR)" % (in_repo, repo, per_repo))
        overall = merged_last_hour(gh, repo.partition("/")[0], now)
    except (GitHubError, ValueError) as err:
        # A count GitHub will not give refuses the merge: the cap fails closed.
        return "could not count the last hour's merges (%s)" % err
    if overall >= per_hour:
        return ("%d loom/ pull requests merged in the last hour, the cap is %d "
                "(LOOM_AUTO_MERGE_PER_HOUR)" % (overall, per_hour))
    return ""
