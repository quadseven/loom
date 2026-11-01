"""The close audit: a close that names a commit proves it (loom#2426).

On 2026-10-31 a ticket closed saying a commit was on main. GitHub had no such
commit, the journal audit read the comments and called the close sound, and
the error stood for hours. An audit that reads what a comment says proves
nothing about what git holds, so this one fetches the commit.

`check` is the pure judgment. It takes the comments around a close and the
ticket's merged pull requests, finds each cited commit, and asks where it is:

- `sound`: every cited commit is on the default branch or came from a merged
  pull request the default branch carries;
- `differs`: a cited commit is neither on the default branch nor part of a
  merged pull request, but another cited commit or a merged pull request is
  on it;
- `unproven`: no cited commit is on the default branch and no merged pull
  request is;
- `uncited`: the close names no commit;
- `unknown`: a read failed or was cut short. Unknown is never sound.

`python3 -m bus.brain audit-close OWNER/NAME#N` prints these facts; an audit
line in a journal comes from that output. `run_pass` is the station's step: it
reads closes made by anyone but the station since it first ran, within a read
budget. A close that is `unproven` gets one comment and is reopened; a ticket
is reopened once, and a later unproven close of it gets the comment alone.
Nothing closed before the first pass is read. It makes no AI call.

A merged pull request counts as delivery only when its base is the audited repo
or a repo of this fleet (LOOM_REPOS), it names the ticket (`names_ticket`), and
the cross-reference predates its merge. Anyone can merge a pull request in a
repo of their own, and anyone can mention a ticket on a merged pull request.

Only the closer's own comments around the close are read as citing a commit.
A hash the closer wrote that names another repo in prose reads as absent here;
the reopen comment says so, and the reopen happens once.
"""
from __future__ import annotations

import calendar
import os
import re
import time
from typing import Any, Callable, Dict, List, Mapping, NamedTuple, Optional, Sequence, Tuple

from . import notify, shadow, tickets
from .github import GitHubError, RateLimited

SOUND, DIFFERS, UNPROVEN, UNCITED, UNKNOWN = "sound", "differs", "unproven", "uncited", "unknown"
ON, OFF, ABSENT, UNSEEN = ("on the default branch", "not on the default branch",
                           "not on GitHub", "lookup failed")

# Comments this long before a close, and just after it, are the close's words.
CLOSE_WINDOW = 15 * 60
CLOSE_GRACE = 60
MAX_CITED = 5
MAX_PULLS = 5
TIMELINE_PAGES = 3
PAGE = 100

# The station's step: how often, how far back, and what one pass may spend.
STATE_FILE = "close-audit.json"
EVERY = 600
LOOKBACK = 2 * 24 * 3600
READS_PER_PASS = 80
# The most one ticket can read, so a pass never starts what it cannot finish:
# 3 first reads, the timeline, each linked pull request and its merge commit,
# and for each cited commit its pull requests, one more merge commit and its
# place, with room for the client's own read of status events.
TICKET_READS = 3 + TIMELINE_PAGES + 2 * MAX_PULLS + 3 * MAX_CITED + 2
REOPENS_PER_PASS = 2
# A close that reads unknown this many times is left, and logged.
UNKNOWN_TRIES = 3
SEEN_KEPT = 2000

_HIDDEN = re.compile(r"<!--.*?-->|^(```|~~~).*?^\1[^\n]*$", re.S | re.M)
_URL = re.compile(r"https?://[^\s<>()\]]+")
# A commit in another repo: owner/name@hash.
_ELSEWHERE = re.compile(r"[\w.-]+/[\w.-]+@[0-9a-f]{7,40}\b")
_HASH = re.compile(r"(?<![0-9A-Za-z-])[0-9a-f]{7,40}(?![0-9A-Za-z-])")

Place = Callable[[str, str], str]


class Delivery(NamedTuple):
    """One merged pull request and where its merge commit is."""
    number: int
    head: str
    merge_commit: str
    place: str
    commits: Tuple[str, ...] = ()   # cited commits GitHub says came through it


class Citation(NamedTuple):
    sha: str
    place: str
    pull: int           # the merged pull request that delivered it, or 0
    merge_commit: str


class Report(NamedTuple):
    verdict: str
    citations: List[Citation]
    deliveries: List[Delivery]
    error: str


def epoch(value: Any) -> Optional[float]:
    if not isinstance(value, str):
        return None
    try:
        return float(calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")))
    except ValueError:
        return None


def _hashes(repo: str, body: str) -> List[str]:
    """The commit-shaped words of a comment, oldest mention first. Hidden
    markers, links outside this repo and owner/name@hash are not citations of
    this repo. A word with no digit or no letter reads as a word or a number."""
    def keep_own(found: "re.Match[str]") -> str:
        link = found.group(0)
        return link if "/%s/" % repo.lower() in link.lower() else " "
    text = _ELSEWHERE.sub(" ", _URL.sub(keep_own, _HIDDEN.sub(" ", body)))
    return [word for word in _HASH.findall(text)
            if re.search(r"\d", word) and re.search(r"[a-f]", word)]


def cited_commits(repo: str, closed_at: Any, comments: Sequence[Any]) -> Tuple[List[str], bool]:
    """(the distinct hashes the close's comments cite, whether more than
    MAX_CITED were dropped)."""
    closed = epoch(closed_at)
    found: List[str] = []
    for item in comments:
        at = epoch(item.get("created_at")) if isinstance(item, dict) else None
        if closed is None or at is None or not -CLOSE_WINDOW <= at - closed <= CLOSE_GRACE:
            continue
        found += [sha for sha in _hashes(repo, str(item.get("body") or "")) if sha not in found]
    return found[:MAX_CITED], len(found) > MAX_CITED


def fleet_repos(env: Optional[Mapping[str, str]] = None) -> List[str]:
    """The repos whose merged pull requests count, besides the audited one."""
    values = os.environ if env is None else env
    return str(values.get("LOOM_REPOS") or "").replace(",", " ").split()


def trusted(where: str, repo: str, fleet: Sequence[str]) -> bool:
    """Whether a pull request based in `where` can prove delivery."""
    return where.lower() in {name.lower() for name in [repo] + list(fleet)}


def names_ticket(pull: Mapping[str, Any], where: str, repo: str, number: int,
                 source: Optional[int]) -> bool:
    """Whether a pull request based in `where` names ticket `number` of `repo`
    the way this factory does: a Refs line, a closing keyword, or a lane's
    loom/<number> branch. `source` is the issue the ticket was rewritten from,
    which its Refs line may name instead. A bare #N names a ticket of `where`."""
    same = where.lower() == repo.lower()
    ref = r"(?:%s)%s#" % (re.escape(repo), "?" if same else "")
    body = str(pull.get("body") or "")
    wanted = [n for n in (number, source) if n is not None]
    if any(re.search(r"^Refs %s%d\b" % (ref, n), body, re.M | re.I) for n in wanted):
        return True
    if re.search(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+%s%d\b" % (ref, number),
                 body, re.I):
        return True
    head = pull.get("head") if isinstance(pull.get("head"), dict) else {}
    return same and bool(re.match(r"loom/%d(?:-|$)" % number, str(head.get("ref") or "")))


def _pull_repo(repo: str, pull: Mapping[str, Any]) -> str:
    base = pull.get("base") if isinstance(pull.get("base"), dict) else {}
    where = base.get("repo") if isinstance(base.get("repo"), dict) else {}
    return str(where.get("full_name") or repo)


def _delivery(repo: str, pull: Mapping[str, Any], place: Place) -> Delivery:
    head = pull.get("head") if isinstance(pull.get("head"), dict) else {}
    merge = str(pull.get("merge_commit_sha") or "")
    return Delivery(int(pull.get("number") or 0), str(head.get("sha") or ""), merge,
                    place(_pull_repo(repo, pull), merge) if merge else UNSEEN,
                    tuple(pull.get("cited") or ()))


def _citation(repo: str, sha: str, deliveries: Sequence[Delivery], place: Place) -> Citation:
    for pull in deliveries:
        if pull.head.startswith(sha) or pull.merge_commit.startswith(sha) \
                or sha in pull.commits:
            return Citation(sha, pull.place, pull.number, pull.merge_commit)
    return Citation(sha, place(repo, sha), 0, "")


def _verdict(citations: Sequence[Citation], deliveries: Sequence[Delivery], error: str) -> str:
    if error or UNSEEN in [c.place for c in citations] + [d.place for d in deliveries]:
        return UNKNOWN
    if any(c.place != ON for c in citations):
        held = [c.place for c in citations] + [d.place for d in deliveries]
        return DIFFERS if ON in held else UNPROVEN
    return SOUND if citations else UNCITED


def check(repo: str, closed_at: Any, comments: Sequence[Any],
          pulls: Sequence[Mapping[str, Any]], place: Place, error: str = "",
          fleet: Sequence[str] = ()) -> Report:
    """Judge one close. `pulls` are the ticket's merged pull requests as
    GitHub gives them, each with the cited commits it is known to carry under
    "cited"; `place(repo, sha)` says where a commit is. A pull request based
    outside the audited repo and `fleet` is left out (`trusted`)."""
    shas, cut = cited_commits(repo, closed_at, comments)
    deliveries = [_delivery(repo, pull, place) for pull in pulls
                  if trusted(_pull_repo(repo, pull), repo, fleet)]
    citations = [_citation(repo, sha, deliveries, place) for sha in shas]
    why = error or ("the close cites more than %d commits" % MAX_CITED if cut else "")
    return Report(_verdict(citations, deliveries, why), citations, deliveries, why)


def lines(report: Report) -> List[str]:
    """The facts an audit line carries: each cited hash, the merge commit, and
    whether the default branch has it."""
    said = ["Close audit: %s%s" % (report.verdict, ": " + report.error if report.error else "")]
    for item in report.citations:
        said.append("cited %s: %s%s" % (item.sha, item.place, (
            ", delivered by #%d, merge commit %s" % (item.pull, item.merge_commit)
            if item.pull else ", no merged pull request is known to carry it")))
    for pull in report.deliveries:
        said.append("merged #%d: head %s, merge commit %s, %s" % (
            pull.number, pull.head, pull.merge_commit, pull.place))
    if not report.citations:
        said.append("the close cites no commit")
    return said


def _failed(err: Exception) -> str:
    return "status %d" % err.status if isinstance(err, GitHubError) and err.status \
        else type(err).__name__


def _place(gh: Any, repo: str, branch: str, sha: str) -> str:
    """Where `sha` is. A failed read is UNSEEN, and proves nothing."""
    try:
        found = gh.rest("GET", "/repos/%s/compare/%s...%s?per_page=1" % (repo, branch, sha),
                        ok=(404,))
    except RateLimited:
        raise
    except (GitHubError, OSError, ValueError):
        return UNSEEN
    if found.status == 404:
        return ABSENT
    status = found.data.get("status") if isinstance(found.data, dict) else None
    if status in ("identical", "behind"):
        return ON
    return OFF if status in ("ahead", "diverged") else UNSEEN


def _before_merge(event: Mapping[str, Any], merged: Any) -> bool:
    """A cross-reference made after the merge says nothing about what merged."""
    made, done = epoch(event.get("created_at")), epoch(merged)
    return made is not None and done is not None and made <= done


def _referenced_before_merge(events: Sequence[Any], repo: str,
                             fleet: Sequence[str]) -> Dict[Tuple[str, int], str]:
    """(repo, number) -> merge time of each merged pull request in a trusted
    repo that a cross-reference named before it merged."""
    found: Dict[Tuple[str, int], str] = {}
    for event in events:
        if not isinstance(event, dict) or event.get("event") != "cross-referenced":
            continue
        ref = (event.get("source") or {}).get("issue") or {}
        merged = (ref.get("pull_request") or {}).get("merged_at")
        where = str((ref.get("repository") or {}).get("full_name") or "")
        if trusted(where, repo, fleet) and isinstance(ref.get("number"), int) \
                and _before_merge(event, merged):
            found[(where, ref["number"])] = str(merged)
    return found


def _merged_pulls(gh: Any, repo: str, number: int, fleet: Sequence[str],
                  source: Optional[int]) -> Tuple[List[Dict[str, Any]], str]:
    """(the newest MAX_PULLS merged pull requests that delivered a ticket: in a
    trusted repo, referenced before the merge and naming the ticket; why the
    timeline was not read to its end, or "")."""
    found: Dict[Tuple[str, int], str] = {}
    full = False
    for page in range(1, TIMELINE_PAGES + 1):
        events = gh.rest("GET", "/repos/%s/issues/%d/timeline?per_page=%d&page=%d"
                         % (repo, number, PAGE, page)).data
        events = events if isinstance(events, list) else []
        found.update(_referenced_before_merge(events, repo, fleet))
        full = len(events) >= PAGE
        if not full:
            break
    newest = sorted(found, key=lambda key: found[key], reverse=True)[:MAX_PULLS]
    pulls = [(key[0], gh.rest("GET", "/repos/%s/pulls/%d" % key).data) for key in newest]
    return ([p for where, p in pulls
             if isinstance(p, dict) and names_ticket(p, where, repo, number, source)],
            "the timeline is longer than the audit reads" if full else "")


def _carries(pull: Mapping[str, Any], sha: str) -> bool:
    head = str((pull.get("head") or {}).get("sha") or "")
    return head.startswith(sha) or str(pull.get("merge_commit_sha") or "").startswith(sha)


def _with_cited(gh: Any, repo: str, shas: Sequence[str],
                pulls: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """`pulls`, plus each merged pull request GitHub says a cited commit came
    through (an intermediate commit of a squash, or a pull request the ticket
    does not link), each naming the cited commits it carries."""
    found = {int(p.get("number") or 0): dict(p) for p in pulls}
    for sha in shas:
        if any(_carries(p, sha) for p in found.values()):
            continue
        listed = gh.rest("GET", "/repos/%s/commits/%s/pulls?per_page=%d" % (repo, sha, MAX_PULLS),
                         ok=(404, 422))
        for pull in listed.data if listed.status == 200 and isinstance(listed.data, list) else []:
            if isinstance(pull, dict) and pull.get("merged_at"):
                have = found.setdefault(int(pull.get("number") or 0), dict(pull))
                have["cited"] = list(have.get("cited") or []) + [sha]
                break     # one pull request per cited commit keeps the reads bounded
    return list(found.values())


class _Unreadable(ValueError):
    """Why a close cannot be audited, in words."""


def _closers_comments(gh: Any, repo: str, number: int, issue: Mapping[str, Any]
                      ) -> Tuple[List[Dict[str, Any]], str]:
    """(the comments the closer wrote around the close, why they may be
    fewer than that, or "")."""
    closed = epoch(issue.get("closed_at"))
    if closed is None:
        raise _Unreadable("the close could not be read")
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(closed - CLOSE_WINDOW))
    found = gh.rest("GET", "/repos/%s/issues/%d/comments?since=%s&per_page=%d"
                    % (repo, number, since, PAGE)).data
    if not isinstance(found, list):
        raise _Unreadable("the comments could not be read")
    closer = str((issue.get("closed_by") or {}).get("login") or "")
    own = [c for c in found if isinstance(c, dict)
           and str((c.get("user") or {}).get("login") or "") == closer]
    return own, "the comments are longer than the audit reads" if len(found) >= PAGE else ""


def _lookup(gh: Any, repo: str, branch: str, pulls: Sequence[Mapping[str, Any]]) -> Place:
    """`check`'s lookup: this repo against its default branch, and a merge
    commit in another repo against the default branch its pull request names."""
    def place(where: str, sha: str) -> str:
        if where.lower() == repo.lower():
            return _place(gh, where, branch, sha)
        for pull in pulls:
            if pull.get("merge_commit_sha") == sha:
                base = pull.get("base") or {}
                return _place(gh, where, str((base.get("repo") or {}).get("default_branch")
                                             or base.get("ref") or ""), sha)
        return UNSEEN
    return place


def _read_and_check(gh: Any, repo: str, number: int, issue: Mapping[str, Any],
                    fleet: Sequence[str]) -> Report:
    if issue.get("state") != "closed":
        raise _Unreadable("the ticket is not closed")
    branch = str((gh.rest("GET", "/repos/%s" % repo).data or {}).get("default_branch") or "")
    if not branch:
        raise _Unreadable("the default branch could not be read")
    own, long = _closers_comments(gh, repo, number, issue)
    pulls, cut = _merged_pulls(gh, repo, number, fleet,
                               tickets.rewritten_source(issue.get("body")))
    shas = cited_commits(repo, issue.get("closed_at"), own)[0]
    pulls = _with_cited(gh, repo, shas, pulls)
    return check(repo, issue.get("closed_at"), own, pulls, _lookup(gh, repo, branch, pulls),
                 error=long or cut, fleet=fleet)


def read_close(gh: Any, repo: str, number: int,
               issue: Optional[Mapping[str, Any]] = None,
               fleet: Sequence[str] = ()) -> Report:
    """Read a ticket's newest close from GitHub and judge it (`check`). Any
    failed read makes the report unknown. `issue` is the ticket when the
    caller already read it; `fleet` lists the other repos whose merged pull
    requests count."""
    try:
        known = issue or gh.rest("GET", "/repos/%s/issues/%d" % (repo, number)).data or {}
        return _read_and_check(gh, repo, number, known, fleet)
    except RateLimited:
        raise
    except _Unreadable as err:
        return Report(UNKNOWN, [], [], str(err))
    except (GitHubError, OSError, ValueError) as err:
        return Report(UNKNOWN, [], [], _failed(err))


def refusal(report: Report, reopened: bool = False) -> str:
    """The one comment on a close the audit refuses."""
    lead = ("This close cites a commit the default branch does not carry, and no merged "
            "pull request on the default branch is linked to it." if reopened else
            "Reopening: this close cites a commit the default branch does not carry, and no "
            "merged pull request on the default branch is linked to it. Close it again once "
            "the change has landed. A hash from another repo reads as absent here: link that "
            "pull request.")
    return "\n".join([lead, ""] + ["- " + line for line in lines(report)]
                     + ["", "-- loom close audit"])


class _Counted:
    """The client, counting each request a pass makes."""

    def __init__(self, gh: Any) -> None:
        self.gh = gh
        self.spent = 0

    def rest(self, method: str, path: str, *args: Any, **kwargs: Any) -> Any:
        self.spent += 1
        return self.gh.rest(method, path, *args, **kwargs)


class _Pass:
    """One station pass over the closes of its repos."""

    def __init__(self, gh: Any, state: Dict[str, Any], now: float, own: Callable[[], str],
                 say: Callable[[str], None], fleet: Sequence[str] = ()) -> None:
        self.fleet = fleet
        self.gh, self.state, self.now, self.own, self.say = _Counted(gh), state, now, own, say
        self.read = self.refused = self.reopens = self.waiting = 0
        self.seen = state.setdefault("seen", {})
        self.reopened = state.setdefault("reopened", {})
        self.posted = state.setdefault("posted", {})
        self.unknown = state.setdefault("unknown", {})

    def _left(self) -> int:
        return READS_PER_PASS - self.gh.spent

    def _closes(self, repo: str, since: float) -> List[Dict[str, Any]]:
        """The repo's closes as completed since `since` not read yet, oldest
        first. Whole hours, so the same URL serves an hour of passes."""
        hour = int(max(since, self.now - LOOKBACK) // 3600 * 3600)
        found = self.gh.rest("GET", "/repos/%s/issues?state=closed&sort=updated&direction=desc"
                             "&since=%s&per_page=%d" % (
                                 repo, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(hour)),
                                 PAGE)).data
        if isinstance(found, list) and len(found) >= PAGE:
            self.say("close audit: %s has more closes than one read shows" % repo)
        due = []
        for issue in found if isinstance(found, list) else []:
            closed = epoch(issue.get("closed_at")) if isinstance(issue, dict) else None
            if (closed is not None and closed >= since and "pull_request" not in issue
                    and issue.get("state_reason") == "completed"
                    and _key(repo, issue) not in self.seen):
                due.append(issue)
        return sorted(due, key=lambda issue: str(issue.get("closed_at")))

    def _refuse(self, repo: str, number: int, key: str, report: Report) -> None:
        ticket = "%s#%d" % (repo, number)
        self.refused += 1
        if self.reopened.get(ticket):
            notify.post(self.gh, repo, number, refusal(report, reopened=True), notify.AUDIT)
            self.say("close audit: %s is unproven again, reopened once already: leaving it"
                     % ticket)
            self.seen[key] = self.now
            return
        if shadow.active(repo):
            shadow.record_held(shadow.REOPEN, repo, number, "issue", source="close audit",
                               reason="; ".join(lines(report)))
            self.seen[key] = self.now
            return
        if not self.posted.get(key):
            notify.post(self.gh, repo, number, refusal(report), notify.REOPENED)
            self.posted[key] = self.now
        self.gh.rest("PATCH", "/repos/%s/issues/%d" % (repo, number), {"state": "open"})
        self.posted.pop(key, None)
        self.seen[key] = self.now
        self.reopened[ticket] = self.now
        self.reopens += 1
        self.say("close audit: reopened %s: %s" % (ticket, lines(report)[0]))

    def _ticket(self, repo: str, issue: Dict[str, Any]) -> None:
        number = int(issue["number"])
        key = _key(repo, issue)
        full = self.gh.rest("GET", "/repos/%s/issues/%d" % (repo, number)).data or {}
        closer, own = str((full.get("closed_by") or {}).get("login") or ""), self.own()
        if not closer or not own:
            return
        if closer == own:
            self.seen[key] = self.now
            return
        self.read += 1
        report = read_close(self.gh, repo, number, full, self.fleet)
        if report.verdict == UNKNOWN:
            self.unknown[key] = int(self.unknown.get(key) or 0) + 1
            self.say("close audit: %s#%d unknown: %s" % (repo, number, report.error or "a lookup "
                                                         "failed"))
            if self.unknown[key] >= UNKNOWN_TRIES:
                self.seen[key] = self.now
                del self.unknown[key]
        elif report.verdict == UNPROVEN and self.reopens < REOPENS_PER_PASS:
            self._refuse(repo, number, key, report)
        elif report.verdict != UNPROVEN:
            self.seen[key] = self.now

    def repo(self, repo: str) -> None:
        since = self.state.setdefault("since", {})
        if repo not in since:
            since[repo] = self.now
            return
        if self._left() < TICKET_READS:
            self.waiting += 1
            return
        try:
            closes = self._closes(repo, float(since[repo]))
        except RateLimited:
            raise
        except (GitHubError, OSError, ValueError) as err:
            self.say("close audit: %s could not be read: %s" % (repo, _failed(err)))
            return
        for issue in closes:
            if self._left() < TICKET_READS:
                self.waiting += 1
                continue
            try:
                self._ticket(repo, issue)
            except RateLimited:
                raise
            except (GitHubError, OSError, ValueError) as err:
                self.say("close audit: %s#%s failed: %s" % (repo, issue.get("number"),
                                                             _failed(err)))


def _key(repo: str, issue: Mapping[str, Any]) -> str:
    """One close of one ticket: a close after a reopen is a new one."""
    return "%s#%s@%s" % (repo, issue.get("number"), issue.get("closed_at"))


def run_pass(gh: Any, repos: Sequence[str], state: Dict[str, Any], now: float,
             own: Callable[[], str], say: Callable[[str], None]) -> None:
    """Audit the closes of `repos` that the station, whose login `own` gives,
    did not make, at most every EVERY seconds. A close whose closer cannot be
    told stays for a later pass. `state` is what the station keeps between
    passes."""
    last = state.get("last")
    if isinstance(last, (int, float)) and 0 <= now - last < EVERY:
        return
    state["last"] = now
    run = _Pass(gh, state, now, own, say, repos)
    try:
        for repo in repos:
            run.repo(repo)
    finally:
        seen = state.get("seen") or {}
        keep = sorted(seen, key=lambda key: seen[key])[-SEEN_KEPT:]
        state["seen"] = {key: seen[key] for key in keep}
        if run.read or run.waiting:
            say("close audit: %d closes read, %d refused, %d waiting; at most %d reads a pass"
                % (run.read, run.refused, run.waiting, READS_PER_PASS))
