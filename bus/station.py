"""The station: briefed ticket issues in, local briefs out, outcomes reported.

One pass (python3 -m bus station, run by launchd) does three things under one
lock: finishes any claim left half done, reports finished runs back to their
tickets, and claims the oldest trusted briefed ticket for each lane that can
take one. docs/BRAIN.md ("Claim and outcome", "Polling budget") is the contract.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from . import tickets
from .github import GitHub, GitHubError
from .labels import LANES

HEARTBEAT_MAX_AGE = 600
BRIEFED = "loom:briefed"
RUNNING = "loom:running"
CLAIM_RE = re.compile(r"Claimed for lane [a-z0-9-]+ \(body sha256 [0-9a-f]{12}\)")
ISSUE_LINE_RE = re.compile(r"^Issue: ([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)#([0-9]+)[ \t]*$", re.M)
FAILED_RE = re.compile(r"\bfailed: (.*)$")
# An absolute path: a run of non-space characters starting with a slash (not
# the slash inside a word such as owner/name).
PATH_RE = re.compile(r"(?<![A-Za-z0-9_.-])/\S*")
REASON_MAX = 300


class Settings:
    def __init__(self, home: str, repos: List[str], authors: List[str],
                 owners: List[str]) -> None:
        self.home = home
        self.repos = repos
        self.authors = authors
        self.owners = owners

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "Settings":
        env = os.environ if env is None else env
        home = env.get("LOOM_HOME") or os.path.join(os.path.expanduser("~"), "loom")

        def words(name: str) -> List[str]:
            return env.get(name, "").replace(",", " ").split()
        return cls(home, words("LOOM_REPOS"), words("LOOM_BRIEF_AUTHORS"),
                   words("LOOM_REPO_OWNERS"))


def _say(msg: str) -> None:
    print("station: " + msg, file=sys.stderr)


def _read_json(path: str, default: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _write_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def _sha12(body: Optional[str]) -> str:
    return hashlib.sha256((body or "").encode("utf-8")).hexdigest()[:12]


def _label_names(issue: Dict[str, Any]) -> List[str]:
    names = []
    for lab in issue.get("labels") or []:
        name = lab.get("name") if isinstance(lab, dict) else lab
        if name:
            names.append(str(name))
    return names


def _lane_of(issue: Dict[str, Any]) -> Optional[str]:
    """The one lane the issue names, or None when it names none or several."""
    lanes = [n[len("lane:"):] for n in _label_names(issue) if n.startswith("lane:")]
    return lanes[0] if len(lanes) == 1 and lanes[0] in LANES else None


def scrub(reason: str) -> str:
    text = PATH_RE.sub("<path>", " ".join(reason.split()))
    return text[:REASON_MAX]


class Station:
    def __init__(self, gh: GitHub, settings: Settings,
                 now: Optional[float] = None) -> None:
        self.gh = gh
        self.s = settings
        self.now = time.time() if now is None else now
        self.state = os.path.join(settings.home, "state", "station")

    # -- paths ---------------------------------------------------------
    def inbox(self, lane: str) -> str:
        return os.path.join(self.s.home, "briefs", lane)

    def _repo_ok(self, repo: str) -> bool:
        return repo in self.s.repos and bool(tickets.REPO_RE.fullmatch(repo))

    # -- reading GitHub ------------------------------------------------
    def _issue(self, repo: str, number: int) -> Dict[str, Any]:
        data = self.gh.rest("GET", "/repos/%s/issues/%d" % (repo, number)).data
        if not isinstance(data, dict):
            raise GitHubError("no issue %s#%d" % (repo, number))
        data.setdefault("repository_url", "%s/repos/%s" % (self.gh.api, repo))
        return data

    def _editor(self, repo: str, number: int) -> Optional[str]:
        owner, name = repo.split("/")
        data = self.gh.graphql(
            "query($owner:String!,$name:String!,$number:Int!){"
            "repository(owner:$owner,name:$name){issue(number:$number){"
            "editor{login}}}}", {"owner": owner, "name": name, "number": number})
        issue = ((data or {}).get("repository") or {}).get("issue")
        if issue is None:
            raise GitHubError("graphql returned no issue %s#%d" % (repo, number))
        editor = issue.get("editor")
        return str(editor["login"]) if editor and editor.get("login") else None

    def _check_trust(self, repo: str, issue: Dict[str, Any]) -> Tuple[bool, str]:
        number = int(issue["number"])
        events = self.gh.paginate("/repos/%s/issues/%d/events" % (repo, number))
        editor = self._editor(repo, number)
        return tickets.trusted(issue, events, editor, self.s.authors, self.s.owners)

    def _poll(self, repo: str) -> List[Dict[str, Any]]:
        path = os.path.join(self.state, repo.replace("/", ".") + ".json")
        saved = _read_json(path, {})
        if not isinstance(saved, dict):
            saved = {}
        stored = saved.get("issues") if isinstance(saved.get("issues"), list) else None
        resp = self.gh.rest(
            "GET", "/repos/%s/issues?labels=loom:briefed&state=open&per_page=100" % repo,
            etag=saved.get("etag") if stored is not None else None)
        if resp.status == 304 and stored is not None:
            items = stored
        else:
            items = resp.data if isinstance(resp.data, list) else []
            _write_json(path, {"etag": resp.etag, "issues": items})
        out = []
        for item in items:
            if isinstance(item, dict) and "pull_request" not in item:
                item.setdefault("repository_url", "%s/repos/%s" % (self.gh.api, repo))
                out.append(item)
        return out

    # -- once-only memory ----------------------------------------------
    def _skip_known(self, repo: str, issue: Dict[str, Any]) -> bool:
        """True when this issue was already refused at this updated_at."""
        rec = _read_json(os.path.join(self.state, "untrusted.json"), {}).get(
            "%s#%s" % (repo, issue["number"]))
        return bool(rec) and rec.get("updated_at") == issue.get("updated_at")

    def _refuse(self, repo: str, issue: Dict[str, Any], reason: str) -> None:
        """Log a refusal once per issue and body, and remember it."""
        path = os.path.join(self.state, "untrusted.json")
        seen = _read_json(path, {})
        if not isinstance(seen, dict):
            seen = {}
        key = "%s#%s" % (repo, issue["number"])
        sha = _sha12(issue.get("body"))
        old = seen.get(key) or {}
        if old.get("sha") != sha:
            _say("not claiming %s: %s" % (key, reason))
        seen[key] = {"sha": sha, "updated_at": issue.get("updated_at")}
        _write_json(path, seen)

    # -- claims ---------------------------------------------------------
    def _write_brief(self, lane: str, name: str, text: str) -> str:
        os.makedirs(self.inbox(lane), exist_ok=True)
        path = os.path.join(self.inbox(lane), name + ".claiming")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
        return path

    def _claim(self, repo: str, lane: str, issue: Dict[str, Any]) -> None:
        """Steps 2 and 3 for an issue already re-read and trusted."""
        number = int(issue["number"])
        comments = self.gh.paginate("/repos/%s/issues/%d/comments" % (repo, number))
        earlier = sum(1 for c in comments
                      if isinstance(c, dict) and CLAIM_RE.fullmatch(str(c.get("body") or "").strip()))
        attempt = earlier + 1
        name = tickets.ticket_name(repo, number, attempt)
        text = tickets.render_brief(repo, issue, lane, attempt)
        claiming = self._write_brief(lane, name, text)
        labels = [n for n in _label_names(issue) if n != BRIEFED]
        if RUNNING not in labels:
            labels.append(RUNNING)
        self.gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number),
                     {"labels": labels})
        self.gh.rest("POST", "/repos/%s/issues/%d/comments" % (repo, number),
                     {"body": "Claimed for lane %s (body sha256 %s)"
                      % (lane, _sha12(issue.get("body")))})
        os.replace(claiming, claiming[:-len(".claiming")] + ".md")
        _say("claimed %s#%d for lane %s as %s" % (repo, number, lane, name))

    def _still_ours(self, issue: Dict[str, Any], lane: str) -> bool:
        names = _label_names(issue)
        return (issue.get("state") == "open" and "pull_request" not in issue
                and BRIEFED in names and "lane:" + lane in names
                and _lane_of(issue) == lane)

    def finish_claims(self) -> None:
        for lane in LANES:
            folder = self.inbox(lane)
            try:
                entries = sorted(os.listdir(folder))
            except OSError:
                continue
            for entry in entries:
                if not entry.endswith(".claiming"):
                    continue
                path = os.path.join(folder, entry)
                try:
                    self._finish_one(lane, path)
                except (GitHubError, ValueError, OSError) as err:
                    _say("could not finish %s: %s" % (entry, err))

    def _finish_one(self, lane: str, path: str) -> None:
        with open(path, encoding="utf-8") as fh:
            m = ISSUE_LINE_RE.search(fh.read())
        if not m or not self._repo_ok(m.group(1)):
            _say("%s names no watched issue; leaving it" % os.path.basename(path))
            return
        repo, number = m.group(1), int(m.group(2))
        issue = self._issue(repo, number)
        names = _label_names(issue)
        if RUNNING in names:
            os.replace(path, path[:-len(".claiming")] + ".md")
            _say("finished claim of %s#%d for lane %s" % (repo, number, lane))
            return
        if not self._still_ours(issue, lane):
            os.remove(path)
            _say("dropped half-done claim of %s#%d: no longer briefed for lane %s"
                 % (repo, number, lane))
            return
        # The body may have changed since the brief was written: trust and
        # render the issue as it is now.
        try:
            tickets.parse(issue.get("body"))
            ok, reason = self._check_trust(repo, issue)
        except ValueError as err:
            ok, reason = False, "unusable brief: %s" % err
        if not ok:
            os.remove(path)
            self._refuse(repo, issue, reason)
            return
        self._claim(repo, lane, issue)

    def _heartbeat_ok(self, lane: str) -> bool:
        beat = _read_json(os.path.join(self.s.home, "state", "usage", lane + ".json"), None)
        if not isinstance(beat, dict) or beat.get("available") is not True:
            return False
        at = beat.get("at")
        if isinstance(at, bool) or not isinstance(at, (int, float)):
            return False
        return -60 <= self.now - at <= HEARTBEAT_MAX_AGE

    def _inbox_empty(self, lane: str) -> bool:
        try:
            names = os.listdir(self.inbox(lane))
        except OSError:
            return True
        return not any(n.endswith((".md", ".claiming")) for n in names)

    def claim_new(self) -> None:
        free = [lane for lane in LANES
                if self._heartbeat_ok(lane) and self._inbox_empty(lane)]
        if not free:
            return
        by_lane: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {l: [] for l in free}
        for repo in self.s.repos:
            if not tickets.REPO_RE.fullmatch(repo):
                _say("ignoring %r in LOOM_REPOS: not OWNER/NAME" % repo)
                continue
            try:
                found = self._poll(repo)
            except GitHubError as err:
                _say("could not list %s: %s" % (repo, err))
                continue
            for issue in found:
                lane = _lane_of(issue)
                if lane in by_lane:
                    by_lane[lane].append((repo, issue))
        for lane in free:
            cands = sorted(by_lane[lane], key=lambda c: (
                str(c[1].get("created_at") or ""), c[0], int(c[1]["number"])))
            for repo, issue in cands:
                try:
                    if self._try_ticket(repo, lane, issue):
                        break
                except (GitHubError, OSError) as err:
                    _say("could not claim %s#%s: %s" % (repo, issue["number"], err))

    def _try_ticket(self, repo: str, lane: str, issue: Dict[str, Any]) -> bool:
        if self._skip_known(repo, issue):
            return False
        try:
            tickets.parse(issue.get("body"))
        except ValueError as err:
            self._refuse(repo, issue, "unusable brief: %s" % err)
            return False
        ok, reason = self._check_trust(repo, issue)
        if not ok:
            self._refuse(repo, issue, reason)
            return False
        fresh = self._issue(repo, int(issue["number"]))
        if not self._still_ours(fresh, lane):
            _say("%s#%s changed before the claim; leaving it" % (repo, issue["number"]))
            return False
        if (fresh.get("body") or "") != (issue.get("body") or "") \
                or fresh.get("updated_at") != issue.get("updated_at"):
            _say("%s#%s changed since it was checked; next pass" % (repo, issue["number"]))
            return False
        self._claim(repo, lane, fresh)
        return True

    # -- outcomes -------------------------------------------------------
    def report_outcomes(self) -> None:
        path = os.path.join(self.state, "reported.json")
        reported = _read_json(path, {})
        if not isinstance(reported, dict):
            reported = {}
        for lane in LANES:
            for kind in ("done", "failed"):
                folder = os.path.join(self.inbox(lane), kind)
                try:
                    entries = sorted(os.listdir(folder))
                except OSError:
                    continue
                for entry in entries:
                    if not entry.endswith(".md"):
                        continue
                    key = "%s/%s" % (lane, entry[:-3])
                    if key in reported:
                        continue
                    try:
                        if self._report(lane, kind, os.path.join(folder, entry), entry[:-3]):
                            reported[key] = kind
                            _write_json(path, reported)
                    except (GitHubError, OSError) as err:
                        _say("could not report %s: %s" % (key, err))

    def _field(self, text: str, name: str) -> str:
        m = re.search(r"^%s:[ \t]*(.*?)[ \t]*$" % name, text, re.M)
        return m.group(1) if m else ""

    def _report(self, lane: str, kind: str, path: str, name: str) -> bool:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        m = ISSUE_LINE_RE.search(text)
        if not m:
            return True  # not a station brief: nothing to report
        repo, number = m.group(1), int(m.group(2))
        if not self._repo_ok(repo):
            return True
        if kind == "done":
            branch = self._field(text, "Branch")
            prs = self.gh.rest("GET", "/repos/%s/pulls?head=%s:%s&state=all"
                               % (repo, repo.split("/")[0], branch)).data
            url = prs[0].get("html_url") if isinstance(prs, list) and prs and isinstance(prs[0], dict) else None
            if not url:
                _say("no pull request found yet for %s (%s)" % (name, branch))
                return False
            self._relabel(repo, number, "loom:pr")
            self._comment(repo, number, "Lane %s opened a pull request: %s" % (lane, url))
            _say("reported %s#%d done: %s" % (repo, number, url))
            return True
        reason = self._failure_reason(lane, name)
        self._relabel(repo, number, "loom:failed")
        self._comment(repo, number, "Lane %s failed: %s" % (lane, reason))
        _say("reported %s#%d failed: %s" % (repo, number, reason))
        return True

    def _failure_reason(self, lane: str, name: str) -> str:
        log = os.path.join(self.s.home, "logs", lane, name + ".log")
        reason = ""
        try:
            with open(log, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    m = FAILED_RE.search(line)
                    if m:
                        reason = m.group(1)
        except OSError:
            pass
        return scrub(reason) or "no reason was recorded"

    def _relabel(self, repo: str, number: int, new: str) -> None:
        names = _label_names(self._issue(repo, number))
        if RUNNING not in names:
            return
        labels = [n for n in names if n != RUNNING]
        if new not in labels:
            labels.append(new)
        self.gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number),
                     {"labels": labels})

    def _comment(self, repo: str, number: int, body: str) -> None:
        self.gh.rest("POST", "/repos/%s/issues/%d/comments" % (repo, number),
                     {"body": body})

    # -- one pass --------------------------------------------------------
    def run_pass(self) -> None:
        self.finish_claims()
        self.report_outcomes()
        self.claim_new()


def run(settings: Settings, gh: Optional[GitHub] = None) -> int:
    """One pass. Exits 0 at once when another pass holds the lock."""
    if not settings.repos:
        _say("LOOM_REPOS is empty; nothing to do")
        return 0
    state = os.path.join(settings.home, "state")
    os.makedirs(state, exist_ok=True)
    fd = os.open(os.path.join(state, "station.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            _say("another pass is running")
            return 0
        if gh is None:
            from .github import token_from_env
            gh = GitHub(token_from_env())
        try:
            Station(gh, settings).run_pass()
        finally:
            _say("requests: %d" % gh.requests)
        return 0
    finally:
        os.close(fd)
