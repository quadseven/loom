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
Such a finding is never deferred as minor (bus/factory.py). Auto-merges are
also capped: at most LOOM_AUTO_MERGE_PER_HOUR (default 4) loom/ pull
requests merged in the last hour across the owner, and at most
LOOM_AUTO_MERGE_PER_REPO_HOUR (default 2) in one repo. The counts come from
GitHub, so every brain and station reads the same numbers; a merge by hand of
a loom/ branch counts too. A count GitHub will not give refuses the merge.
"""
from __future__ import annotations

import fnmatch
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote

from . import change_class
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
_BASE_RE = re.compile(r"^\s*Base\s*:.*$", re.I | re.M)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)

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


def other_repos(repo: str, ticket_body: str, default_branch: Optional[str] = None) -> List[str]:
    """Other repos of the same owner the ticket's own words name (citation
    lines and HTML comments aside)."""
    owner, _, name = repo.partition("/")
    text = _BASE_RE.sub("", _CITATION_RE.sub("", _COMMENT_RE.sub("", ticket_body or "")))
    found = []
    for who, what in _OWNER_REPO_RE.findall(text):
        what = what.rstrip(".")
        if who.lower() == owner.lower() and what.lower() != name.lower():
            if default_branch is not None:
                default_owner, _, default_repo = default_branch.partition("/")
                if who.lower() == default_owner.lower() and what.lower() == default_repo.lower():
                    continue
            found.append("%s/%s" % (who, what))
    return list(dict.fromkeys(found))


def _rank(tier: str) -> int:
    return TIERS.index(tier)


def _shown(paths: Sequence[str]) -> str:
    return ", ".join(paths[:FILES_SHOWN]) + (
        " and %d more" % (len(paths) - FILES_SHOWN) if len(paths) > FILES_SHOWN else "")


def classify(repo: str, entries: Sequence[Mapping[str, Any]], ticket_body: str = "",
              sensitive: bool = False, default_branch: Optional[str] = None) -> Tuple[str, str]:
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
    repos = other_repos(repo, ticket_body, default_branch)
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
    " nodes { isResolved comments(first:1) { nodes {"
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


def blocking_findings(threads: Sequence[Mapping[str, Any]], committed: str,
                      by_bot: Callable[[Mapping[str, Any]], bool],
                      env: Optional[Mapping[str, str]] = None) -> List[str]:
    """The review-bot findings that hold an auto-merge: high or critical, or
    from a bad-work rule, whose thread is open or was resolved with no commit
    after it (deferred, or resolved by hand without a fix). `threads` are
    every review thread (review_threads), `committed` the head commit's
    committedDate, and `by_bot` says whether a comment is the review bot's.
    A missing time cannot show a fix followed."""
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
