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
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import tickets
from .github import GitHub, GitHubError, _OPENER
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
LANES_MARKER = "<!-- loom:lanes v1 -->"
LANES_LABEL = "loom:lanes"
LANES_TITLE = "Loom lane status"
LANES_REPUBLISH = 3600
NEEDS_OPERATOR = "needs-operator"
URGENT = "loom:urgent"
DISCORD_MAX = 2000
POST_TIMEOUT = 30
DIGEST_HEAD = "Asks waiting for you:"
REPORT_MARKER = "<!-- loom:report v1 -->"
# A brain tick runs every 30 minutes; no tick ending well for this long means
# the brain has stopped, and the operator is told once.
BRAIN_STALE = 2 * 3600
BRAIN_OUTCOMES = ("ok", "skipped", "failed")
REMOTE_RE = re.compile(r"[:/]([A-Za-z0-9._-]+/[A-Za-z0-9._-]+?)(?:\.git)?/?$")

SecretReader = Callable[[str, str], str]
Poster = Callable[[str, Dict[str, Any]], None]


class Settings:
    def __init__(self, home: str, repos: List[str], authors: List[str],
                 owners: List[str], status_repo: str = "", webhook_ssm: str = "",
                 mention: str = "", digest_hour: int = 9,
                 region: str = "us-east-1") -> None:
        self.home = home
        self.repos = repos
        self.authors = authors
        self.owners = owners
        self.status_repo = status_repo
        self.webhook_ssm = webhook_ssm
        self.mention = mention
        self.digest_hour = digest_hour
        self.region = region

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "Settings":
        env = os.environ if env is None else env
        home = env.get("LOOM_HOME") or os.path.join(os.path.expanduser("~"), "loom")

        def words(name: str) -> List[str]:
            return env.get(name, "").replace(",", " ").split()
        try:
            hour = int(env.get("LOOM_DIGEST_HOUR", "").strip() or 9)
        except ValueError:
            hour = 9
        if not 0 <= hour <= 23:
            hour = 9
        return cls(home, words("LOOM_REPOS"), words("LOOM_BRIEF_AUTHORS"),
                   words("LOOM_REPO_OWNERS"),
                   status_repo=env.get("LOOM_STATUS_REPO", "").strip() or _origin_repo(),
                   webhook_ssm=env.get("LOOM_DISCORD_WEBHOOK_SSM", "").strip(),
                   mention=env.get("LOOM_DISCORD_MENTION", "").strip(),
                   digest_hour=hour,
                   region=env.get("LOOM_AWS_REGION", "").strip() or "us-east-1")


def _origin_repo() -> str:
    """owner/name of this checkout's origin remote, or "" when unknown."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        out = subprocess.run(["git", "-C", root, "remote", "get-url", "origin"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    m = REMOTE_RE.search(out.stdout.strip()) if out.returncode == 0 else None
    return m.group(1) if m else ""


def read_ssm(name: str, region: str) -> str:
    """A SecureString parameter, read the way lanes/run_lane.sh reads keys."""
    out = subprocess.run(
        ["aws", "ssm", "get-parameter", "--region", region, "--name", name,
         "--with-decryption", "--query", "Parameter.Value", "--output", "text"],
        capture_output=True, text=True, timeout=POST_TIMEOUT)
    if out.returncode != 0 or not out.stdout.strip():
        raise OSError("could not read the webhook parameter from SSM")
    return out.stdout.strip()


def post_webhook(url: str, payload: Dict[str, Any]) -> None:
    """POST JSON to the webhook. Errors never carry the URL (it is a secret)."""
    if not url.startswith("https://"):
        raise OSError("the webhook URL is not https")
    req = urllib.request.Request(  # noqa: S310 - https only, checked above
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "loom-station"})
    try:
        with _OPENER.open(req, timeout=POST_TIMEOUT) as resp:  # noqa: S310
            status = resp.status
    except urllib.error.HTTPError as err:
        status = err.code
        err.close()
    except (urllib.error.URLError, OSError):
        raise OSError("the webhook post failed to connect") from None
    if not 200 <= status < 300:
        raise OSError("the webhook answered %d" % status)


def _say(msg: str) -> None:
    print("station: " + msg, file=sys.stderr)


class StateError(Exception):
    """A state file that exists but cannot be read."""


def _read_json(path: str, default: Any, strict: bool = True) -> Any:
    """The file's JSON, or `default` when it does not exist. A file that
    exists but cannot be read raises StateError when strict: the ledgers of
    refusals and reports would otherwise read as empty and every ticket would
    be told again. Heartbeats and the ETag cache are not strict (a heartbeat
    can be caught mid-write; a lost cache only costs one full read)."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as err:
        if strict:
            raise StateError("unreadable state file %s: %s" % (path, err)) from None
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
                 now: Optional[float] = None,
                 read_secret: Optional[SecretReader] = None,
                 post: Optional[Poster] = None) -> None:
        self.gh = gh
        self.s = settings
        self.read_secret = read_secret or read_ssm
        self.post = post or post_webhook
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
            "editor{login} lastEditedAt}}}", {"owner": owner, "name": name, "number": number})
        issue = ((data or {}).get("repository") or {}).get("issue")
        if issue is None:
            raise GitHubError("graphql returned no issue %s#%d" % (repo, number))
        editor = issue.get("editor")
        if editor and editor.get("login"):
            return str(editor["login"])
        # Edited, but GitHub names no editor (a deleted account): nobody on
        # the list can vouch for this body.
        return "(deleted account)" if issue.get("lastEditedAt") else None

    def _check_trust(self, repo: str, issue: Dict[str, Any]) -> Tuple[bool, str]:
        number = int(issue["number"])
        events = self.gh.paginate("/repos/%s/issues/%d/events" % (repo, number))
        editor = self._editor(repo, number)
        return tickets.trusted(issue, events, editor, self.s.authors, self.s.owners)

    def _poll(self, repo: str) -> List[Dict[str, Any]]:
        path = os.path.join(self.state, repo.replace("/", ".") + ".json")
        saved = _read_json(path, {}, strict=False)
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
            if len(items) >= 100:
                _say("%s has 100 or more briefed tickets; only the first page is read" % repo)
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
        beat = _read_json(os.path.join(self.s.home, "state", "usage", lane + ".json"),
                          None, strict=False)
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
    def _finished_briefs(self):
        """(lane, kind, path, name) for every brief in done/ or failed/."""
        for lane in LANES:
            for kind in ("done", "failed"):
                folder = os.path.join(self.inbox(lane), kind)
                try:
                    entries = sorted(os.listdir(folder))
                except OSError:
                    continue
                for entry in entries:
                    if entry.endswith(".md"):
                        yield lane, kind, os.path.join(folder, entry), entry[:-3]

    def report_outcomes(self) -> None:
        path = os.path.join(self.state, "reported.json")
        reported = _read_json(path, {})
        if not isinstance(reported, dict):
            raise StateError("state file %s does not hold an object" % path)
        for lane, kind, brief, name in self._finished_briefs():
            key = "%s/%s" % (lane, name)
            if key in reported:
                continue
            try:
                done = self._report(lane, kind, brief, name)
            except (GitHubError, OSError) as err:
                _say("could not report %s: %s" % (key, err))
                continue
            if done:
                reported[key] = kind
                _write_json(path, reported)

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

    # -- lane status ------------------------------------------------------
    @staticmethod
    def _utc(at: float) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(at))

    def _lane_rows(self) -> List[Tuple[str, str, str]]:
        rows = []
        for lane in LANES:
            beat = _read_json(os.path.join(self.s.home, "state", "usage", lane + ".json"),
                              None, strict=False)
            at = beat.get("at") if isinstance(beat, dict) else None
            when = (self._utc(at) if not isinstance(at, bool) and isinstance(at, (int, float))
                    else "-")
            rows.append((lane, "yes" if self._heartbeat_ok(lane) else "no", when))
        brain = self._brain()
        if brain is not None:
            ok_at = brain[1]
            rows.append(("brain", "yes" if self._brain_fresh(ok_at) else "no",
                         self._utc(ok_at) if ok_at is not None else "-"))
        return rows

    def _lanes_issue(self, repo: str) -> int:
        found = self.gh.rest("GET", "/repos/%s/issues?labels=%s&state=open&per_page=100"
                             % (repo, LANES_LABEL)).data
        for item in found if isinstance(found, list) else []:
            if isinstance(item, dict) and "pull_request" not in item:
                return int(item["number"])
        made = self.gh.rest("POST", "/repos/%s/issues" % repo, {
            "title": LANES_TITLE, "labels": [LANES_LABEL],
            "body": "The station keeps the comment below current with each "
                    "lane's availability."}).data
        if not isinstance(made, dict) or "number" not in made:
            raise GitHubError("no issue was created in %s" % repo)
        return int(made["number"])

    def _lanes_unchanged(self, repo: str, values: Dict[str, str]) -> bool:
        """Same yes/no values published to the same repo within the hour."""
        saved = _read_json(os.path.join(self.state, "lanes.json"), {}, strict=False)
        if not isinstance(saved, dict):
            return False
        last_at = saved.get("at")
        if isinstance(last_at, bool) or not isinstance(last_at, (int, float)):
            return False
        return (saved.get("repo") == repo and saved.get("values") == values
                and 0 <= self.now - last_at <= LANES_REPUBLISH)

    def _lanes_body(self, rows: List[Tuple[str, str, str]]) -> str:
        return "\n".join(
            [LANES_MARKER, "Updated " + self._utc(self.now), "",
             "| lane | available | heartbeat (UTC) |", "|---|---|---|"]
            + ["| %s | %s | %s |" % row for row in rows])

    def _upsert_lanes_comment(self, repo: str, number: int, body: str) -> Optional[int]:
        for c in self.gh.paginate("/repos/%s/issues/%d/comments" % (repo, number)):
            if isinstance(c, dict) and str(c.get("body") or "").startswith(LANES_MARKER):
                self.gh.rest("PATCH", "/repos/%s/issues/comments/%d" % (repo, int(c["id"])),
                             {"body": body})
                return int(c["id"])
        made = self.gh.rest("POST", "/repos/%s/issues/%d/comments" % (repo, number),
                            {"body": body}).data
        return int(made["id"]) if isinstance(made, dict) and "id" in made else None

    def publish_lanes(self) -> None:
        repo = self.s.status_repo
        if not repo:
            return
        if not tickets.REPO_RE.fullmatch(repo):
            _say("ignoring LOOM_STATUS_REPO %r: not OWNER/NAME" % repo)
            return
        rows = self._lane_rows()
        values = {lane: yn for lane, yn, _ in rows}
        if self._lanes_unchanged(repo, values):
            return
        number = self._lanes_issue(repo)
        comment_id = self._upsert_lanes_comment(repo, number, self._lanes_body(rows))
        _write_json(os.path.join(self.state, "lanes.json"),
                    {"repo": repo, "issue": number, "comment_id": comment_id,
                     "values": values, "at": self.now})
        _say("published lane status to %s#%d" % (repo, number))

    # -- asks -------------------------------------------------------------
    def _poll_asks(self, repo: str) -> List[Dict[str, Any]]:
        path = os.path.join(self.state, "asks-" + repo.replace("/", ".") + ".json")
        saved = _read_json(path, {}, strict=False)
        if not isinstance(saved, dict):
            saved = {}
        stored = saved.get("issues") if isinstance(saved.get("issues"), list) else None
        resp = self.gh.rest(
            "GET", "/repos/%s/issues?labels=%s&state=open&per_page=100" % (repo, NEEDS_OPERATOR),
            etag=saved.get("etag") if stored is not None else None)
        if resp.status == 304 and stored is not None:
            items = stored
        else:
            items = resp.data if isinstance(resp.data, list) else []
            _write_json(path, {"etag": resp.etag, "issues": items})
        return [i for i in items if isinstance(i, dict) and "pull_request" not in i
                and i.get("html_url") and "number" in i]

    def _ask_trusted(self, repo: str, issue: Dict[str, Any]) -> bool:
        allowed = {a.lower() for a in self.s.authors}
        if tickets._login(issue.get("user")) not in allowed:
            return False
        events = self.gh.paginate("/repos/%s/issues/%d/events" % (repo, int(issue["number"])))
        actor = tickets._latest_label_actor(events, lambda n: n == NEEDS_OPERATOR)
        return actor is not None and actor in allowed

    @staticmethod
    def _line(issue: Dict[str, Any]) -> str:
        return "- %s %s" % (" ".join(str(issue.get("title") or "").split()), issue["html_url"])

    def _send(self, url: str, content: str, mention: bool = True) -> bool:
        allowed: Dict[str, Any] = {"parse": []}
        if mention and self.s.mention.isdigit():
            content = "<@%s> %s" % (self.s.mention, content)
            allowed["users"] = [self.s.mention]
        try:
            self.post(url, {"content": content[:DISCORD_MAX], "allowed_mentions": allowed})
        # Deliberately broad: urllib's errors (a ValueError for a malformed
        # URL, for one) can carry the URL, which is a secret, and anything
        # this does not catch would reach the log with its message. Only the
        # exception's type is logged.
        except Exception as err:  # noqa: BLE001
            _say("could not post to the webhook (%s); next pass" % type(err).__name__)
            return False
        return True

    def _digest_text(self, lines: List[str]) -> str:
        room = DISCORD_MAX - 60  # the mention and the "and N more" line
        keep, size = [], len(DIGEST_HEAD)
        for line in lines:
            size += len(line) + 1
            if size > room:
                break
            keep.append(line)
        if len(keep) < len(lines):
            keep.append("- and %d more" % (len(lines) - len(keep)))
        return DIGEST_HEAD + "\n" + "\n".join(keep)

    def _gather_asks(self) -> Optional[List[Tuple[str, str, Dict[str, Any]]]]:
        """(key, repo, issue) for every open ask, oldest first; None when any
        repo could not be read (a partial list must not become a digest)."""
        asks: List[Tuple[str, Dict[str, Any]]] = []
        for repo in self.s.repos:
            if not tickets.REPO_RE.fullmatch(repo):
                continue
            try:
                asks += [(repo, i) for i in self._poll_asks(repo)]
            except GitHubError as err:
                _say("could not list asks in %s: %s" % (repo, err))
                return None
        asks.sort(key=lambda a: (str(a[1].get("created_at") or ""), a[0], int(a[1]["number"])))
        return [("%s#%s" % (r, i["number"]), r, i) for r, i in asks]

    def _check_asks(self, keyed: List[Tuple[str, str, Dict[str, Any]]],
                    wanted: set) -> Optional[Dict[str, bool]]:
        """Trust for the wanted asks; None when a check failed, which holds
        every send until the next pass."""
        trust: Dict[str, bool] = {}
        for key, repo, issue in keyed:
            if key not in wanted:
                continue
            try:
                trust[key] = self._ask_trusted(repo, issue)
            except GitHubError as err:
                _say("could not check ask %s: %s; holding sends until the next pass"
                     % (key, err))
                return None
        return trust

    def _webhook(self) -> Optional[str]:
        try:
            return self.read_secret(self.s.webhook_ssm, self.s.region)
        # Deliberately broad, as in _send: an error text must never carry the
        # secret; only the type is logged.
        except Exception as err:  # noqa: BLE001
            _say("could not read the webhook parameter (%s); next pass" % type(err).__name__)
            return None

    def _ask_ledger(self) -> Tuple[str, Dict[str, Any], List[str]]:
        path = os.path.join(self.state, "asks.json")
        sent = _read_json(path, {})
        if not isinstance(sent, dict):
            raise StateError("state file %s does not hold an object" % path)
        urgent_sent = sent.get("urgent") if isinstance(sent.get("urgent"), list) else []
        return path, sent, urgent_sent

    @staticmethod
    def _prune_urgent(ledger: Tuple[str, Dict[str, Any], List[str]], open_keys: set) -> None:
        """Forget urgent asks that are no longer open, so the ledger stays the
        size of the open asks (a reopened urgent ask is sent again)."""
        path, sent, urgent_sent = ledger
        kept = [k for k in urgent_sent if k in open_keys]
        if len(kept) != len(urgent_sent):
            urgent_sent[:] = kept
            sent["urgent"] = urgent_sent
            _write_json(path, sent)

    def _today_and_digest_due(self, sent: Dict[str, Any]) -> Tuple[str, bool]:
        local = time.localtime(self.now)
        today = time.strftime("%Y-%m-%d", local)
        return today, local.tm_hour >= self.s.digest_hour and sent.get("digest") != today

    def _deliver(self, url: str, todo: List[Tuple[str, Dict[str, Any]]],
                 digest: List[Dict[str, Any]], ledger: Tuple[str, Dict[str, Any], List[str]],
                 today: str) -> None:
        path, sent, urgent_sent = ledger
        for key, issue in todo:
            title = " ".join(str(issue.get("title") or "").split())
            if self._send(url, "Urgent ask: %s %s" % (title, issue["html_url"])):
                urgent_sent.append(key)
                sent["urgent"] = urgent_sent
                _write_json(path, sent)
        if digest and self._send(url, self._digest_text([self._line(i) for i in digest])):
            sent["digest"] = today
            _write_json(path, sent)

    def forward_asks(self) -> None:
        if not self.s.webhook_ssm:
            # Not configured: skip quietly; a line every minute would only grow
            # the station log (docs/LANES.md says how to turn it on).
            return
        ledger = self._ask_ledger()
        today, digest_due = self._today_and_digest_due(ledger[1])
        keyed = self._gather_asks()
        if keyed is None:
            return
        self._prune_urgent(ledger, {k for k, _, _ in keyed})
        urgent_keys = {k for k, _, i in keyed
                       if URGENT in _label_names(i) and k not in ledger[2]}
        if not urgent_keys and not digest_due:
            return
        trust = self._check_asks(keyed, {k for k, _, _ in keyed} if digest_due else urgent_keys)
        if trust is None:
            return
        todo = [(k, i) for k, _, i in keyed if k in urgent_keys and trust.get(k)]
        digest = [i for k, _, i in keyed if trust.get(k)] if digest_due else []
        if not todo and not digest:
            return
        url = self._webhook()
        if url is not None:
            self._deliver(url, todo, digest, ledger, today)

    # -- goal reports -----------------------------------------------------
    def _goal_issues(self, repo: str) -> List[Dict[str, Any]]:
        data = self.gh.paginate(
            "/repos/%s/issues?labels=loom:goal&state=open&per_page=100" % repo)
        return [i for i in (data if isinstance(data, list) else [])
                if isinstance(i, dict) and "pull_request" not in i
                and "number" in i]

    @staticmethod
    def _report_id(comment: Dict[str, Any]) -> int:
        try:
            return int(comment.get("id", 0))
        except (TypeError, ValueError):
            return 0

    def _report_text(self, issue: Dict[str, Any], comment: Dict[str, Any]) -> str:
        title = " ".join(str(issue.get("title") or "").split())
        body = str(comment.get("body") or "")[len(REPORT_MARKER):].lstrip("\r\n ")
        link = str(comment.get("html_url") or issue.get("html_url") or "")
        prefix = title + "\n"
        suffix = "\n" + link if link else ""
        room = DISCORD_MAX - len(prefix) - len(suffix)
        if len(body) > room:
            # Keep whole report lines and reserve the comment link at the end.
            body = body[:max(0, room)].rsplit("\n", 1)[0]
        return (prefix + body + suffix)[:DISCORD_MAX]

    def forward_reports(self) -> None:
        if not self.s.webhook_ssm:
            return
        path = os.path.join(self.state, "reports.json")
        ledger = _read_json(path, {})
        if not isinstance(ledger, dict):
            raise StateError("state file %s does not hold an object" % path)
        allowed = {a.lower() for a in self.s.authors}
        for repo in self.s.repos:
            if not tickets.REPO_RE.fullmatch(repo):
                continue
            issues = self._goal_issues(repo)
            comments: List[Tuple[int, Dict[str, Any], Dict[str, Any]]] = []
            for issue in issues:
                for comment in self.gh.paginate(
                        "/repos/%s/issues/%d/comments" % (repo, int(issue["number"]))):
                    if isinstance(comment, dict) and str(comment.get("body") or "").startswith(REPORT_MARKER):
                        comments.append((self._report_id(comment), issue, comment))
            comments.sort(key=lambda row: row[0])
            key = repo.replace("/", ".")
            last = ledger.get(key)
            if last is None:
                newest = max((ident for ident, _, _ in comments), default=0)
                ledger[key] = newest
                _write_json(path, ledger)
                continue
            try:
                last_id = int(last)
            except (TypeError, ValueError):
                last_id = 0
            pending = [(ident, issue, comment) for ident, issue, comment in comments
                       if ident > last_id]
            if not pending:
                continue
            url = self._webhook()
            if url is None:
                continue
            for ident, issue, comment in pending:
                author = str((comment.get("user") or {}).get("login") or "").lower()
                if author not in allowed:
                    # Advance past untrusted comments; they must never be replayed.
                    ledger[key] = ident
                    _write_json(path, ledger)
                    last_id = ident
                    continue
                if not self._send(url, self._report_text(issue, comment), mention=False):
                    break
                ledger[key] = ident
                _write_json(path, ledger)
                last_id = ident

    # -- the brain -------------------------------------------------------
    def _brain(self) -> Optional[Tuple[Dict[str, Any], Optional[float]]]:
        """The last brain tick (lanes/mac/brain.sh) and when a tick last ended
        well, or None when no brain tick runs on this machine."""
        state = os.path.join(self.s.home, "state")
        last = _read_json(os.path.join(state, "brain.json"), None, strict=False)
        if not isinstance(last, dict):
            return None
        try:
            ok_at: Optional[float] = os.path.getmtime(os.path.join(state, "brain-ok"))
        except OSError:
            ok_at = None
        return last, ok_at

    def _brain_fresh(self, ok_at: Optional[float]) -> bool:
        return ok_at is not None and self.now - ok_at <= BRAIN_STALE

    def _brain_alert_text(self, last: Dict[str, Any], ok_at: Optional[float]) -> str:
        at, outcome, code = last.get("at"), last.get("outcome"), last.get("exit")
        when = (self._utc(at) if not isinstance(at, bool) and isinstance(at, (int, float))
                else "an unknown time")
        outcome = outcome if outcome in BRAIN_OUTCOMES else "unknown"
        code = code if isinstance(code, int) and not isinstance(code, bool) else "?"
        since = ("since " + self._utc(ok_at)) if ok_at is not None else "yet"
        return ("Urgent: the Loom brain has not finished a tick %s, so no goal is "
                "moving. Last tick: %s, %s (exit %s). Its log is brain.log on the "
                "lane machine." % (since, when, outcome, code))

    def watch_brain(self) -> None:
        """Tell the operator once when the brain stops ticking, and once when
        it is back."""
        if not self.s.webhook_ssm:
            return
        brain = self._brain()
        if brain is None:
            return
        last, ok_at = brain
        path = os.path.join(self.state, "brain-alert.json")
        alert = _read_json(path, {}, strict=False)
        alerted = isinstance(alert, dict) and alert.get("sent") is True
        fresh = self._brain_fresh(ok_at)
        # A message is due only when the state moved: stale and not yet told,
        # or fresh again after an alert.
        if fresh != alerted:
            return
        url = self._webhook()
        if url is None:
            return
        if fresh:
            if self._send(url, "The Loom brain is ticking again (last good tick %s)."
                          % self._utc(ok_at or self.now)):
                _write_json(path, {})
        elif self._send(url, self._brain_alert_text(last, ok_at)):
            _write_json(path, {"sent": True, "at": self.now})

    # -- one pass --------------------------------------------------------
    def run_pass(self) -> None:
        self.finish_claims()
        self.report_outcomes()
        self.claim_new()
        for step in (self.publish_lanes, self.forward_asks, self.forward_reports,
                     self.watch_brain):
            try:
                step()
            except (GitHubError, OSError) as err:
                _say("%s failed: %s" % (step.__name__, err))


def run(settings: Settings, gh: Optional[GitHub] = None, **hooks: Any) -> int:
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
            Station(gh, settings, **hooks).run_pass()
        except StateError as err:
            _say("stopping: %s" % err)
            return 1
        finally:
            _say("requests: %d" % gh.requests)
        return 0
    finally:
        os.close(fd)
