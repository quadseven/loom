"""Muse's builder loop (docs/MUSE.md, Build; the fleet goal's duty 10).

Muse is a builder: each build tick it finishes its own open pull request,
or resumes the ticket it holds, or claims one new buildable ticket, writes
the failing test, fixes it, and opens a pull request as the agent-meta-muse
App. This module is the part of that loop a command can decide:

- `next` says what to work on, in this order: Muse's own open pull request
  (finish it first: bot threads, checks, the merge gate), else a ticket it
  already holds (claim:muse with its build marker), else the best new
  ticket by the pick rule, claimed on the spot.
- The pick rule (`pick`): an open issue in a public repo of LOOM_REPOS
  (the VM clones without a credential, and Muse's model may train on what
  it reads), filed by a trusted author (`trusted_author`), not a pull request,
  with no claim:* label, no lane:* label, no hold (needs:*, loom:human,
  needs-brain, awaiting-live-check, wait:*, epic, a goal home, an active
  Loom state), a stated Size of XS, S or M, at least one acceptance
  checkbox, and no open or merged pull request naming it. Highest priority first
  (a P0..P3 label or a `Priority:` line; none counts as P2), then oldest.
- `claim` adds claim:muse and a marked comment, then reads the labels back
  and lets go when another holder's claim landed at the same time.
- `push` pushes the muse/<n>-<slug> branch through the git-database API
  (bus.lane.push_branch), so the VM needs no git credential.
- `pr` opens the pull request from that branch with Muse's own body,
  refused without the standard sections and a full-form Refs line, or with
  a closing keyword.
- `release` lets a ticket go with a one-line reason.

Every write refuses unless the client writes as the agent-meta-muse App, so
a tick holding another App's credentials stops with the exact cause instead
of posting as the wrong identity.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.parse
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from . import hunt_plan, lane, notify, sweep, text, tickets
from .github import GitHub, GitHubError, auth_from_env

LOGIN = os.environ.get("LOOM_MUSE_BUILD_LOGIN", "agent-meta-muse[bot]")
CLAIM_LABEL = sweep.CLAIM_LABEL
CLAIM_MARKER = "<!-- muse:build v1 claim -->"
RELEASE_MARKER = "<!-- muse:build v1 release -->"
SIGNATURE = "-- muse"
SIZES = ("XS", "S", "M")
# How many search pages (100 issues each) a pick reads (GitHub's search
# stops at 1000), and how many
# eligible candidates it checks for an open pull request before giving up.
PAGES = 10
CHECKS = 10
HOLD_LABELS = {"epic", "loom:goal", "loom:backlog", "loom:human", "needs-brain",
               "awaiting-live-check", "loom:held", "loom:runaway", "loom:running",
               "loom:briefed", "loom:pr", "loom:ready", "loom:merged", "loom:shipped",
               "loom:failed", "loom:awaiting-evidence", "loom:triage", "blocked",
               "wontfix", "duplicate", "question"}
HOLD_PREFIXES = ("claim:", "lane:", "needs:", "wait:", "handoff:")
_PRIORITY_LINE = re.compile(r"(?im)^\s*Priority:\s*(P[0-3])\b")
_PARENT_TITLE = re.compile(r"(?i)^\s*(?:\[[^\]]*\]\s*)?(?:epic|goal|backlog)\b")
_BOX = re.compile(r"(?m)^\s*[-*]\s+\[ \]\s+\S")
_REFS = r"(?im)^Refs:?[ \t]+%s#%d\b"
# A repo role whose issues are trusted beside the briefing logins.
TRUSTED_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")
_SECTIONS = ("Why", "Summary", "Acceptance criteria", "Test plan", "Size", "Out of scope")


class WrongIdentity(RuntimeError):
    """The client does not write as the agent-meta-muse App."""


class ClaimLost(RuntimeError):
    """Another holder claimed the ticket at the same time."""


@dataclass
class Work:
    kind: str  # "pr", "claim" or "new"
    repo: str
    number: int
    title: str
    url: str
    branch: str = ""
    # The ticket the work builds: the issue itself, or for an open pull
    # request the issue its muse/<n>-<slug> branch names (0 when none).
    ticket: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "repo": self.repo, "number": self.number,
                "title": self.title, "url": self.url, "branch": self.branch,
                "ticket": self.ticket}


def branch_for(number: int, title: str) -> str:
    """muse/<n>-<slug>: never a loom/ branch, which the station treats as a
    lane's and would send fix rounds to."""
    return "muse/" + tickets.branch_for(number, title)[len("loom/"):]


def _names(item: Dict[str, Any]) -> List[str]:
    return [str(x.get("name") or "") for x in item.get("labels") or [] if isinstance(x, dict)]


def _repo_of(item: Dict[str, Any]) -> str:
    return str(item.get("repository_url") or "").split("/repos/")[-1]


def trusted_author(item: Dict[str, Any]) -> bool:
    """Whether the issue's author may set Muse's work: a briefing login, by
    the rule every Loom briefing check uses (tickets.brief_author_trusted:
    LOOM_BRIEF_AUTHORS, or LOOM_TRUSTED_BRIEFERS on a loom:ticket), or the
    repo's owner, a member or a collaborator. Anyone can file an issue on a
    public repo, and an autonomous builder never takes work from a stranger."""
    from . import brain
    if tickets.brief_author_trusted(item, brain.trusted_authors(), brain.trusted_briefers()):
        return True
    return str(item.get("author_association") or "").upper() in TRUSTED_ASSOCIATIONS


def _kind_hold(item: Dict[str, Any]) -> Optional[str]:
    """Why the issue is not a ticket Muse may take: a pull request, closed,
    a stranger's, or a parent (an epic or goal by title or sub-issues)."""
    if "pull_request" in item:
        return "a pull request"
    if str(item.get("state") or "open") != "open":
        return "closed"
    if not trusted_author(item):
        return "author %s is not trusted" % ((item.get("user") or {}).get("login") or "unknown")
    if _PARENT_TITLE.match(str(item.get("title") or "")):
        return "an epic or goal by its title"
    if int((item.get("sub_issues_summary") or {}).get("total") or 0) > 0:
        return "has sub-issues"
    return None


def _label_hold(item: Dict[str, Any]) -> Optional[str]:
    for name in _names(item):
        low = name.lower()
        if low in HOLD_LABELS or low.startswith(HOLD_PREFIXES):
            return "label " + name
    return None


def _body_hold(body: str) -> Optional[str]:
    """Why the body is not buildable in one tick: no size of XS to M, or no
    acceptance checkbox."""
    stated = hunt_plan.stated_size(body)
    try:
        size = hunt_plan.size_letter(stated) if stated else ""
    except ValueError:
        size = ""
    if size not in SIZES:
        return "size %s" % (size or "unstated")
    if not _BOX.search(body):
        return "no acceptance checkbox"
    return None


def hold(item: Dict[str, Any]) -> Optional[str]:
    """Why the pick rule leaves this issue alone, or None when it is buildable
    by its author, labels and body."""
    return (_kind_hold(item) or _label_hold(item)
            or _body_hold(str(item.get("body") or "")))


def priority(item: Dict[str, Any]) -> int:
    for name in _names(item):
        if re.fullmatch(r"P[0-3]", name.strip(), re.I):
            return int(name.strip()[1])
    found = _PRIORITY_LINE.search(str(item.get("body") or ""))
    return int(found.group(1)[1]) if found else 2


def _search(gh: Any, query: str, page: int = 1) -> List[Dict[str, Any]]:
    path = "/search/issues?" + urllib.parse.urlencode(
        {"q": query, "sort": "created", "order": "asc", "per_page": 100, "page": page})
    data = gh.rest("GET", path).data
    return list(data.get("items") or []) if isinstance(data, dict) else []


def _owners(repos: Sequence[str]) -> List[str]:
    return sorted({r.split("/")[0] for r in repos})


def _naming_prs(gh: Any, repo: str, number: int) -> List[Dict[str, Any]]:
    """The pull requests whose cross-reference names the issue."""
    out = []
    for event in gh.paginate("/repos/%s/issues/%d/timeline?per_page=100" % (repo, number)):
        source = ((event.get("source") or {}).get("issue") or {}) if isinstance(event, dict) else {}
        if event.get("event") == "cross-referenced" and "pull_request" in source:
            out.append(source)
    return out


def _named_by_pr(gh: Any, repo: str, number: int) -> bool:
    """Whether an open pull request, or a merged one, names the issue: open
    is someone's live work, merged is built work waiting to be verified."""
    return any(pr.get("state") == "open" or (pr.get("pull_request") or {}).get("merged_at")
               for pr in _naming_prs(gh, repo, number))


def _merged_build(gh: Any, repo: str, number: int) -> Optional[int]:
    """The number of a merged pull request the build App wrote that names the
    issue, or None. Its ticket stays open (no closing keyword) until it is
    verified, so without this the builder would take it up again."""
    for pr in _naming_prs(gh, repo, number):
        login = str((pr.get("user") or {}).get("login") or "")
        if login.lower() == LOGIN.lower() and (pr.get("pull_request") or {}).get("merged_at"):
            return int(pr.get("number") or 0)
    return None


def pick(gh: Any, repos: Sequence[str]) -> Optional[Work]:
    """The best buildable ticket in `repos` by the pick rule, or None."""
    wanted = {r.lower() for r in repos}
    found: List[Dict[str, Any]] = []
    for owner in _owners(repos):
        query = ("org:%s is:public is:issue is:open no:assignee -label:epic -label:\"needs:human\" "
                 "-label:awaiting-live-check -label:\"loom:goal\" -label:\"loom:human\" "
                 "-label:\"loom:pr\" -label:\"loom:running\"" % owner)
        for page in range(1, PAGES + 1):
            items = _search(gh, query, page)
            found += [i for i in items if _repo_of(i).lower() in wanted and hold(i) is None]
            if len(items) < 100:
                break
    found.sort(key=lambda i: (priority(i), str(i.get("created_at") or "")))
    for item in found[:CHECKS]:
        repo, number = _repo_of(item), int(item["number"])
        if _named_by_pr(gh, repo, number):
            continue
        title = str(item.get("title") or "")
        return Work("new", repo, number, title, str(item.get("html_url") or ""),
                    branch_for(number, title), number)
    return None


def check_identity(gh: Any) -> str:
    """The login the client writes as, when it is the agent-meta-muse App.
    An App key names itself through GET /app; a minted installation token
    (LOOM_GITHUB_TOKEN) has no App JWT, so GraphQL's viewer names it."""
    login = getattr(gh, "app_login", lambda: None)()
    if not login and hasattr(gh, "graphql"):
        try:
            data = gh.graphql("{ viewer { login } }") or {}
        except GitHubError:
            data = {}
        login = str(((data.get("viewer") or {}) if isinstance(data, dict) else {})
                    .get("login") or "")
    if (login or "").lower() != LOGIN.lower():
        raise WrongIdentity(
            "the build client writes as %s, expected %s: set LOOM_GITHUB_APP_ID, "
            "LOOM_GITHUB_APP_INSTALLATION_ID and LOOM_GITHUB_APP_KEY_FILE (or _SSM) to the "
            "agent-meta-muse App, or LOOM_GITHUB_TOKEN to a token minted for it, for build "
            "commands" % (login or "an unknown login", LOGIN))
    return login


def _holds(gh: Any, repo: str, number: int) -> bool:
    """Whether the newest build marker on the issue, by the build App, is a
    claim rather than a release."""
    last = None
    for comment in gh.paginate("/repos/%s/issues/%d/comments?per_page=100" % (repo, number)):
        login = str((comment.get("user") or {}).get("login") or "")
        if login.lower() != LOGIN.lower():
            continue
        body = str(comment.get("body") or "")
        if body.startswith(CLAIM_MARKER):
            last = "claim"
        elif body.startswith(RELEASE_MARKER):
            last = "release"
    return last == "claim"


def holds_build_claim(gh: Any, repo: str, number: int) -> bool:
    """For the sweep: a claim:muse the builder holds is live work, not a dead
    sweep tick's leftover."""
    return _holds(gh, repo, number)


def claim(gh: Any, repo: str, number: int) -> None:
    check_identity(gh)
    item = gh.rest("GET", "/repos/%s/issues/%d" % (repo, number)).data or {}
    why = hold(item)
    if why:
        raise ClaimLost("%s#%d is not buildable: %s" % (repo, number, why))
    # The marker goes on before the label: a sweep that sees the label then
    # always sees the marker too, and leaves the issue to the builder.
    notify.post(gh, repo, number, (
        CLAIM_MARKER + "\nClaimed by Muse to build: a failing test first, then the fix, "
        "and a pull request from branch `%s` with a Refs line.\n\n%s"
        % (branch_for(number, str(item.get("title") or "")), SIGNATURE)), notify.RECORD)
    sweep.ensure_label(gh, repo, CLAIM_LABEL)
    sweep.claim(gh, repo, number, CLAIM_LABEL)
    after = gh.rest("GET", "/repos/%s/issues/%d" % (repo, number)).data or {}
    others = [n for n in _names(after) if n.lower().startswith("claim:") and n != CLAIM_LABEL]
    if others:
        release(gh, repo, number, "claimed at the same time by %s; let go" % ", ".join(others))
        raise ClaimLost("%s#%d was claimed at the same time (%s); let go"
                        % (repo, number, ", ".join(others)))


def release(gh: Any, repo: str, number: int, note: str) -> None:
    check_identity(gh)
    line = " ".join(str(note or "").split())
    if not line:
        raise ValueError("release needs a one-line --note")
    text.check(line)
    sweep.release(gh, repo, number, CLAIM_LABEL)
    notify.post(gh, repo, number, "%s\nReleased by Muse: %s\n\n%s"
                % (RELEASE_MARKER, line, SIGNATURE), notify.RECORD)


def _own_pr(gh: Any, repos: Sequence[str], login: str) -> Optional[Work]:
    """Muse's own open pull request in `repos`, with its branch and ticket."""
    app = login[:-len("[bot]")] if login.endswith("[bot]") else login
    wanted = {r.lower() for r in repos}
    for owner in _owners(repos):
        for pr in _search(gh, "org:%s is:pr is:open author:app/%s" % (owner, app)):
            repo, number = _repo_of(pr), int(pr["number"])
            if repo.lower() not in wanted:
                continue
            full = gh.rest("GET", "/repos/%s/pulls/%d" % (repo, number)).data or {}
            branch = str((full.get("head") or {}).get("ref") or "")
            named = re.match(r"muse/(\d+)-", branch)
            return Work("pr", repo, number, str(pr.get("title") or ""),
                        str(pr.get("html_url") or ""), branch, int(named.group(1)) if named else 0)
    return None


def _held_claim(gh: Any, repos: Sequence[str]) -> Optional[Work]:
    """A ticket Muse holds, letting go of any whose build has merged."""
    wanted = {r.lower() for r in repos}
    for owner in _owners(repos):
        for item in _search(gh, 'org:%s is:issue is:open label:"%s"' % (owner, CLAIM_LABEL)):
            repo, number = _repo_of(item), int(item["number"])
            if repo.lower() not in wanted or not _holds(gh, repo, number):
                continue
            merged = _merged_build(gh, repo, number)
            if merged:
                release(gh, repo, number, "built in pull request #%d, merged" % merged)
                continue
            title = str(item.get("title") or "")
            return Work("claim", repo, number, title, str(item.get("html_url") or ""),
                        branch_for(number, title), number)
    return None


def next_work(gh: Any, repos: Sequence[str]) -> Optional[Work]:
    """What Muse works on now: its own open pull request, else its held
    ticket, else a newly claimed one; None when nothing is buildable."""
    login = check_identity(gh)
    found = _own_pr(gh, repos, login) or _held_claim(gh, repos)
    if found is not None:
        return found
    work = pick(gh, repos)
    if work is None:
        return None
    claim(gh, work.repo, work.number)
    return work


def body_problems(body: str, repo: str, number: int) -> List[str]:
    """What a build pull request body lacks: the standard sections, a
    full-form Refs line for its ticket, and no closing keyword."""
    out = list(hunt_plan.problems(body))
    for name in _SECTIONS:
        if not re.search(r"(?im)^##[ \t]+%s[ \t]*$" % re.escape(name), body):
            out.append("missing ## %s section" % name)
    if not re.search(_REFS % (re.escape(repo), number), body):
        out.append("missing the line `Refs %s#%d`" % (repo, number))
    try:
        text.check(body)
    except ValueError as err:
        out.append(str(err))
    return sorted(set(out))


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def open_pr(gh: Any, repo: str, number: int, base: str, body: str,
            title: Optional[str] = None) -> Dict[str, Any]:
    check_identity(gh)
    item = gh.rest("GET", "/repos/%s/issues/%d" % (repo, number)).data or {}
    branch = branch_for(number, str(item.get("title") or ""))
    current = _git("branch", "--show-current")
    if current != branch:
        raise ValueError("current branch %r is not the build branch %r" % (current, branch))
    found = body_problems(body, repo, number)
    if found:
        raise ValueError("the pull request body is refused: " + "; ".join(found))
    made = gh.rest("POST", "/repos/%s/pulls" % repo, {
        "title": title or str(item.get("title") or ""), "head": branch, "base": base,
        "body": body}).data
    if not isinstance(made, dict) or "number" not in made:
        raise GitHubError("GitHub did not return the created pull request")
    return made


def _client() -> Any:
    return GitHub(auth_from_env())


def _ticket(raw: str) -> tuple:
    repo, _, n = raw.partition("#")
    if not tickets.REPO_RE.fullmatch(repo) or not n.isdigit():
        raise ValueError("expected OWNER/NAME#N, got %r" % raw)
    return repo, int(n)


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="python3 -m bus.muse_build")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("next", help="what to build now; claims a new ticket when needed")
    p.add_argument("--repos", default=os.environ.get("LOOM_REPOS", ""))
    p = sub.add_parser("push", help="push the muse/<n>-<slug> branch through the API")
    p.add_argument("ticket")
    p = sub.add_parser("pr", help="open the pull request from the build branch")
    p.add_argument("ticket")
    p.add_argument("--base", required=True)
    p.add_argument("--file", required=True)
    p.add_argument("--title")
    p = sub.add_parser("release", help="let a claimed ticket go")
    p.add_argument("ticket")
    p.add_argument("--note", required=True)
    args = parser.parse_args(argv)
    try:
        gh = _client()
        if args.command == "next":
            repos = [r for r in args.repos.replace(",", " ").split() if r]
            if not repos:
                raise ValueError("next needs --repos or LOOM_REPOS")
            work = next_work(gh, repos)
            if work is None:
                print("nothing buildable in %d repos by the pick rule (docs/MUSE.md, Build)"
                      % len(repos))
                return 1
            print(json.dumps(work.as_dict()))
            return 0
        repo, number = _ticket(args.ticket)
        if args.command == "push":
            check_identity(gh)
            item = gh.rest("GET", "/repos/%s/issues/%d" % (repo, number)).data or {}
            print(lane.push_branch(gh, repo, number,
                                   branch=branch_for(number, str(item.get("title") or ""))))
        elif args.command == "pr":
            with open(args.file, encoding="utf-8") as fh:
                made = open_pr(gh, repo, number, args.base, fh.read(), args.title)
            print(made.get("html_url") or made.get("number"))
        else:
            release(gh, repo, number, args.note)
        return 0
    except (WrongIdentity, ClaimLost, ValueError, GitHubError, OSError,
            subprocess.CalledProcessError) as err:
        print("muse_build %s: %s" % (args.command, err), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
