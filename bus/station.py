"""The station: briefed ticket issues in, local briefs out, outcomes reported.

One pass (python3 -m bus station, run by launchd) does three things under one
lock: finishes any claim left half done, reports finished runs back to their
tickets, and claims the oldest trusted briefed ticket for each lane that can
take one. docs/BRAIN.md ("Claim and outcome", "Polling budget") is the contract.
"""
from __future__ import annotations

import calendar
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import factory
from . import spend
from . import text as _text
from . import tickets
from .github import GitHub, GitHubError, _OPENER
from .journal import REPLY_RE
from .labels import LANES

HEARTBEAT_MAX_AGE = 600
BRIEFED = "loom:briefed"
RUNNING = "loom:running"
CLAIM_RE = re.compile(r"Claimed for lane [a-z0-9-]+ \(body sha256 [0-9a-f]{12}\)")
ISSUE_LINE_RE = re.compile(r"^Issue: ([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)#([0-9]+)[ \t]*$", re.M)
FAILED_RE = re.compile(r"\bfailed: (.*)$")
# The runner's reason when a lane run ends with nothing committed.
NO_COMMIT = "agent made no commit"
# A fix brief's answers to findings (lanes/run_lane.sh keep_answers).
ANSWER_FILE_RE = re.compile(r"^(PRRT_[A-Za-z0-9_-]{4,80})\.md$")
ANSWER_MAX = 3000
STRONG_MODELS = frozenset(model for _, model in factory.LADDER)
THREAD_QUERY = ('query($id:ID!) { node(id:$id) { ... on PullRequestReviewThread { isResolved'
                ' pullRequest { number repository { nameWithOwner } }'
                ' comments(first:1) { nodes { body author { __typename login } } } } } }')
REPLY_MUTATION = ('mutation($id:ID!,$body:String!) { addPullRequestReviewThreadReply('
                  'input:{pullRequestReviewThreadId:$id,body:$body}) { comment { id } } }')
RESOLVE_MUTATION = ('mutation($id:ID!) { resolveReviewThread(input:{threadId:$id})'
                    ' { thread { isResolved } } }')
# The runner's reason, and its heartbeat's, when a lane's provider refused
# work for a while (the spark lane's thermal guard). Not a failure: the ticket
# goes back to briefed, and the factory moves it to the next lane.
LIMIT = "limit:"
# Asks the review bot to review a pull request's head again; it resolves the
# threads it no longer raises. LOOM_REVIEW_AGAIN overrides it.
REVIEW_AGAIN = "/grug improve"
LANE_SAID_MAX = 2500
# An absolute path: a run of non-space characters starting with a slash (not
# the slash inside a word such as owner/name).
PATH_RE = re.compile(r"(?<![A-Za-z0-9_.-])/\S*")
# Credential shapes that must not reach a public comment: provider key
# prefixes, bearer tokens, key=value secrets, and long unbroken token runs.
SECRET_RE = re.compile(
    r"(?i)\b(?:sk|pk|ghp|gho|ghs|ghu|github_pat|xox[a-z]|glpat|AKIA|ASIA)[-_A-Za-z0-9]{12,}"
    r"|\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"
    r"|\b[A-Za-z0-9_]*(?:token|secret|password|passwd|api[_-]?key)[A-Za-z0-9_]*\s*[=:]\s*\S+"
    r"|\b[A-Za-z0-9+/_-]{32,}={0,2}")
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
# Reports come every 2 hours, so the goal homes are read at most this often,
# and only for comments since the last read (with an overlap; the ledger of
# comment ids keeps a report from going out twice).
REPORT_CHECK = 300
REPORT_OVERLAP = 3600
REPORT_FIRST_LOOKBACK = 24 * 3600
# Through the bot, a report is one short line (the goal's short name and the
# report's first line) with a thread on it holding the full report.
SHORT_NAME_RE = re.compile(r"^[ \t]*Short name:[ \t]*(\S.*?)[ \t]*$", re.M | re.I)
SHORT_NAME_MAX = 60
SHORT_LINE_MAX = 300
THREAD_NAME_MAX = 100
THREAD_ARCHIVE_MINUTES = 1440
# The message flag that keeps Discord from unfurling links into preview cards.
SUPPRESS_EMBEDS = 1 << 2
# A brain tick runs every 30 minutes; no tick ending well for this long means
# the brain has stopped, and the operator is told once.
BRAIN_STALE = 2 * 3600
BRAIN_OUTCOMES = ("ok", "skipped", "failed")
# Every brain, on any machine, posts a journal on the goal homes; the station
# reads GitHub at most this often, and looks back this far.
JOURNAL_MARKER = "<!-- loom:journal v1 -->"
JOURNAL_CHECK = 600
JOURNAL_LOOKBACK = 24 * 3600
REMOTE_RE = re.compile(r"[:/]([A-Za-z0-9._-]+/[A-Za-z0-9._-]+?)(?:\.git)?/?$")
# Operator messages in the Discord channel become notes on a goal home, and
# the brain's replies go back to the channel (docs/LANES.md, "Talking to the
# brain"). The channel and the goal homes are read at most this often.
OPERATOR_MARKER = "<!-- loom:operator v1 discord=%s -->"
OPERATOR_CHECK = 30
# The first read of the channel relays only messages this recent.
OPERATOR_FIRST_LOOKBACK = 600
# Replies are read since the last read less this overlap; forwarded comment
# ids are kept this long so a reply goes out once, and only replies posted
# within REPLY_LOOKBACK are forwarded at all.
REPLY_OVERLAP = 3600
REPLY_LOOKBACK = 24 * 3600
DISCORD_API = "https://discord.com/api/v10"
DISCORD_EPOCH_MS = 1420070400000
CHECKMARK = "%E2%9C%85"  # U+2705, URL-encoded for the reactions path
GOAL_REF_RE = re.compile(r"(?<![A-Za-z0-9._/-])([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)#([0-9]+)\b")
NUMBER_REF_RE = re.compile(r"(?<![A-Za-z0-9._/-])#([0-9]+)\b")
TITLE_WORD_MIN = 4

SecretReader = Callable[[str, str], str]
Poster = Callable[[str, Dict[str, Any]], None]
# (method, path under the API, bot token, JSON body or None) -> parsed reply.
DiscordCall = Callable[[str, str, str, Optional[Dict[str, Any]]], Any]


class Settings:
    def __init__(self, home: str, repos: List[str], authors: List[str],
                 owners: List[str], status_repo: str = "", webhook_ssm: str = "",
                 mention: str = "", digest_hour: int = 9,
                 region: str = "us-east-1", channel_id: str = "",
                 bot_token_ssm: str = "", default_goal: str = "",
                 anthropic: Optional[spend.Budget] = None,
                 factory_on: bool = False,
                 review_check: str = factory.DEFAULT_REVIEW_CHECK,
                 review_bot: str = factory.DEFAULT_REVIEW_BOT) -> None:
        self.home = home
        self.repos = repos
        self.authors = authors
        self.owners = owners
        self.status_repo = status_repo
        self.webhook_ssm = webhook_ssm
        self.mention = mention
        self.digest_hour = digest_hour
        self.region = region
        self.channel_id = channel_id
        self.bot_token_ssm = bot_token_ssm
        self.default_goal = default_goal
        self.anthropic = anthropic or spend.Budget()
        # Off unless asked for, so a Settings built in code does not start
        # merging; from_env turns it on unless LOOM_FACTORY=0.
        self.factory_on = factory_on
        self.review_check = review_check
        self.review_bot = review_bot

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
        try:
            budget = spend.Budget.from_env(env)
        except ValueError as err:
            _say("ignoring the anthropic budget: %s" % err)
            budget = spend.Budget()
        return cls(home, words("LOOM_REPOS"), words("LOOM_BRIEF_AUTHORS"),
                   words("LOOM_REPO_OWNERS"),
                   status_repo=env.get("LOOM_STATUS_REPO", "").strip() or _origin_repo(),
                   webhook_ssm=env.get("LOOM_DISCORD_WEBHOOK_SSM", "").strip(),
                   mention=env.get("LOOM_DISCORD_MENTION", "").strip(),
                   digest_hour=hour,
                   region=env.get("LOOM_AWS_REGION", "").strip() or "us-east-1",
                   channel_id=env.get("LOOM_DISCORD_CHANNEL_ID", "").strip(),
                   bot_token_ssm=env.get("LOOM_DISCORD_BOT_TOKEN_SSM", "").strip(),
                   default_goal=env.get("LOOM_DISCORD_DEFAULT_GOAL", "").strip(),
                   anthropic=budget,
                   factory_on=env.get("LOOM_FACTORY", "").strip() not in ("0", "off", "false"),
                   review_check=(env.get("LOOM_REVIEW_CHECK", "").strip()
                                 or factory.DEFAULT_REVIEW_CHECK),
                   review_bot=(env.get("LOOM_REVIEW_BOT", "").strip()
                               or factory.DEFAULT_REVIEW_BOT))


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
        raise OSError("could not read the parameter from SSM")
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


class DiscordError(OSError):
    """A failed Discord call. Its message never carries the token."""


def discord_call(method: str, path: str, token: str,
                 body: Optional[Dict[str, Any]] = None) -> Any:
    """One Discord REST call as the bot. Errors name only the method and the
    status: the token, and the path's ids, stay out of the log."""
    req = urllib.request.Request(  # noqa: S310 - a fixed https host
        DISCORD_API + path, method=method,
        data=None if body is None else json.dumps(body).encode("utf-8"),
        headers={"Authorization": "Bot " + token,
                 "Content-Type": "application/json",
                 "User-Agent": "DiscordBot (https://github.com/quadseven/loom, 1)"})
    try:
        with _OPENER.open(req, timeout=POST_TIMEOUT) as resp:  # noqa: S310
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as err:
        status, raw = err.code, b""
        err.close()
    except (urllib.error.URLError, OSError):
        raise DiscordError("discord %s failed to connect" % method) from None
    if not 200 <= status < 300:
        raise DiscordError("discord %s answered %d" % (method, status))
    try:
        return json.loads(raw.decode("utf-8")) if raw.strip() else None
    except ValueError:
        return None


def _snowflake(at: float) -> int:
    """The smallest Discord id a message posted at this time can have."""
    return max(0, int(at * 1000) - DISCORD_EPOCH_MS) << 22


def _say(msg: str) -> None:
    print("station: " + msg, file=sys.stderr)


class StateError(Exception):
    """A state file that exists but cannot be read."""


def _bot_minor(comment: Dict[str, Any], bot: str) -> bool:
    """The review bot's own finding (its app, not a person with its login)
    whose severity, the first field of its first line, is medium or low."""
    author = comment.get("author") or {}
    if author.get("__typename") != "Bot" or str(author.get("login") or "").lower() != bot.lower():
        return False
    severity = str(comment.get("body") or "").split("\n", 1)[0].split("|", 1)[0].lower()
    return bool(re.search(r"\b(medium|low)\b", severity)) and not re.search(
        r"\b(critical|high)\b", severity)


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
                 post: Optional[Poster] = None,
                 discord: Optional[DiscordCall] = None) -> None:
        self.gh = gh
        self.s = settings
        self.read_secret = read_secret or read_ssm
        self.post = post or post_webhook
        self.discord = discord or discord_call
        self._goals: Optional[List[Tuple[str, Dict[str, Any]]]] = None
        self.now = time.time() if now is None else now
        self.state = os.path.join(settings.home, "state", "station")
        self._spend: Optional[spend.Summary] = None

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

    def _fix_for(self, repo: str, comments: List[Any]) -> Optional[Tuple[tickets.Fix, Any]]:
        """Return the newest trusted fix after the last claim, regardless of
        later non-fix comments. Raises ValueError for an unusable fix."""
        allowed = {a.lower() for a in self.s.authors}
        last_claim, newest_fix = -1, None
        for i, c in enumerate(comments):
            if not isinstance(c, dict):
                continue
            body = str(c.get("body") or "")
            if CLAIM_RE.fullmatch(body.strip()):
                last_claim = i
            if (tickets._login(c.get("user")) in allowed
                    and body.startswith(tickets.FIX_MARKER)):
                newest_fix = (i, c, body)
        if newest_fix is None or newest_fix[0] < last_claim:
            return None
        # GitHub keeps no editor for comments: one changed after it was
        # posted may carry someone else's words.
        if newest_fix[1].get("updated_at") != newest_fix[1].get("created_at"):
            raise ValueError("the loom:fix comment was edited after it was posted")
        return tickets.parse_fix(repo, newest_fix[2]), newest_fix[1].get("id")

    def _claim(self, repo: str, lane: str, issue: Dict[str, Any]) -> bool:
        """Steps 2 and 3 for an issue already re-read and trusted. False when
        its fix comment cannot be used (refused, as an untrusted ticket is)."""
        number = int(issue["number"])
        comments = self.gh.paginate("/repos/%s/issues/%d/comments" % (repo, number))
        try:
            fix_source = self._fix_for(repo, comments)
        except ValueError as err:
            self._refuse(repo, issue, str(err))
            return False
        earlier = sum(1 for c in comments
                      if isinstance(c, dict) and CLAIM_RE.fullmatch(str(c.get("body") or "").strip()))
        attempt = earlier + 1
        name = tickets.ticket_name(repo, number, attempt)
        fix, fix_comment_id = fix_source if fix_source else (None, None)
        text = tickets.render_brief(repo, issue, lane, attempt, fix)
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
        if fix:
            _say("rendered %s#%d fix brief from loom:fix comment %s"
                 % (repo, number, fix_comment_id if fix_comment_id is not None else "with unknown id"))
        _say("claimed %s#%d for lane %s as %s" % (repo, number, lane, name))
        return True

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
        if not self._claim(repo, lane, issue):
            os.remove(path)

    def _anthropic_spend(self) -> spend.Summary:
        if self._spend is None:
            self._spend = spend.summarize(self.s.home, self.s.anthropic, self.now)
        return self._spend

    def _heartbeat_ok(self, lane: str) -> bool:
        # Credit under the floor stops the anthropic slots even before their
        # runner next reports it.
        if lane.split("-")[0] == "anthropic" and self._anthropic_spend().under_floor():
            return False
        beat = _read_json(os.path.join(self.s.home, "state", "usage", lane + ".json"),
                          None, strict=False)
        if not isinstance(beat, dict) or beat.get("available") is not True:
            return False
        at = beat.get("at")
        if isinstance(at, bool) or not isinstance(at, (int, float)):
            return False
        return -60 <= self.now - at <= HEARTBEAT_MAX_AGE

    def limited_lanes(self) -> List[str]:
        """Lanes whose fresh heartbeat says unavailable at a limit."""
        out = []
        for lane in LANES:
            beat = _read_json(os.path.join(self.s.home, "state", "usage", lane + ".json"),
                              None, strict=False)
            if not isinstance(beat, dict) or beat.get("available") is not False:
                continue
            at = beat.get("at")
            if isinstance(at, bool) or not isinstance(at, (int, float)):
                continue
            if (-60 <= self.now - at <= HEARTBEAT_MAX_AGE
                    and str(beat.get("reason") or "").startswith(LIMIT)):
                out.append(lane)
        return out

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
            headers, _ = tickets.parse(issue.get("body"))
        except ValueError as err:
            self._refuse(repo, issue, "unusable brief: %s" % err)
            return False
        hold = tickets.not_before(headers)
        if hold is not None and self.now < hold:
            _say("%s#%s is held until %s" % (repo, issue["number"], headers["Not before"]))
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
        return self._claim(repo, lane, fresh)

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
            if self._field(text, "Fix-PR").isdigit():
                self._post_answers(repo, int(self._field(text, "Fix-PR")), lane, name, text)
            verb = "pushed fixes to" if self._field(text, "Fix-PR") else "opened"
            self._comment(repo, number, "Lane %s %s a pull request: %s" % (lane, verb, url))
            _say("reported %s#%d done: %s" % (repo, number, url))
            return True
        reason = self._failure_reason(lane, name)
        fix_pr = self._field(text, "Fix-PR")
        if reason.startswith(LIMIT):
            self._limited(repo, number, fix_pr.isdigit(), lane, reason)
            return True
        if fix_pr.isdigit() and reason.startswith(NO_COMMIT):
            self._post_answers(repo, int(fix_pr), lane, name, text)
            self._nothing_to_fix(repo, number, int(fix_pr), lane, name)
            return True
        self._relabel(repo, number, "loom:failed")
        self._comment(repo, number, "Lane %s failed: %s" % (lane, reason))
        _say("reported %s#%d failed: %s" % (repo, number, reason))
        return True

    def _limited(self, repo: str, number: int, fix: bool, lane: str, reason: str) -> None:
        """A run the lane's provider refused (a limit, not a failure). The
        ticket goes back to briefed, where the factory moves it to the next
        lane; a fix brief's goes back to loom:pr, where the factory sends its
        pull request back again."""
        self._relabel(repo, number, "loom:pr" if fix else BRIEFED)
        self._comment(repo, number, "Lane %s is unavailable (%s). This is not a failure: "
                      "the ticket goes to the next lane." % (lane, reason))
        _say("reported %s#%d limited on lane %s: %s" % (repo, number, lane, reason))

    def _post_answers(self, repo: str, pr: int, lane: str, name: str, brief: str) -> None:
        """Post each answer the lane wrote instead of a fix as the reply on its
        thread. Only a thread the brief asked about, on this pull request,
        gets one. It is resolved when the round ran on a ladder model and the
        thread is the review bot's own medium or low finding; otherwise it
        stays open, and the factory reads it as a finding that survived."""
        folder = os.path.join(self.inbox(lane), "answers", name)
        try:
            entries = sorted(os.listdir(folder))
        except OSError:
            return
        asked = set(factory.FIX_THREAD_RE.findall(brief))
        strong = self._field(brief, "Model") in STRONG_MODELS
        for entry in entries:
            m = ANSWER_FILE_RE.match(entry)
            if m and m.group(1) in asked:
                self._answer(repo, pr, lane, m.group(1), os.path.join(folder, entry), strong)
        shutil.rmtree(folder, ignore_errors=True)

    def _answer(self, repo: str, pr: int, lane: str, thread_id: str, path: str,
                strong: bool) -> None:
        with open(path, encoding="utf-8", errors="replace") as fh:
            said = SECRET_RE.sub("<redacted>", PATH_RE.sub("<path>", fh.read(ANSWER_MAX))).strip()
        node = (self.gh.graphql(THREAD_QUERY, {"id": thread_id}) or {}).get("node") or {}
        where = node.get("pullRequest") or {}
        if (not said or node.get("isResolved")
                or (where.get("repository") or {}).get("nameWithOwner") != repo
                or where.get("number") != pr):
            return
        body = "Lane %s answered this finding instead of changing the code:\n\n%s" % (
            lane, "\n".join("> " + line for line in said.splitlines()))
        try:
            _text.check(body)
        except ValueError as err:
            _say("answer to %s on %s#%d not posted: %s" % (thread_id, repo, pr, err))
            return
        self.gh.graphql(REPLY_MUTATION, {"id": thread_id, "body": body})
        root = ((node.get("comments") or {}).get("nodes") or [{}])[0] or {}
        settled = strong and _bot_minor(root, self.s.review_bot)
        if settled:
            self.gh.graphql(RESOLVE_MUTATION, {"id": thread_id})
        _say("posted lane %s's answer on %s#%d thread %s%s" % (
            lane, repo, pr, thread_id, " and resolved it" if settled else ""))

    def _nothing_to_fix(self, repo: str, number: int, pr: int, lane: str, name: str) -> None:
        """A fix brief that ended with nothing committed. Most often the lane
        found every thread already fixed (a stale review thread) and said
        so. Its explanation goes on the pull request, the review bot is asked
        to review the head again (it resolves what it no longer raises), and
        the ticket returns to loom:pr. If the review still stands, the factory
        sends it back a rung up the model ladder (bus/factory.py), so this
        never loops on one lane."""
        said = self._lane_said(lane, name)
        body = "Lane %s made no change for this fix." % lane
        if said:
            body = "Lane %s made no change for this fix. It said:\n\n%s" % (
                lane, "\n".join("> " + line for line in said.splitlines()))
        try:
            _text.check(body)
        except ValueError:
            body = ("Lane %s made no change for this fix; its explanation did not "
                    "pass the text checks and is in the lane log." % lane)
        self._comment(repo, pr, body)
        self._comment(repo, pr, os.environ.get("LOOM_REVIEW_AGAIN", "").strip() or REVIEW_AGAIN)
        self._relabel(repo, number, "loom:pr")
        self._comment(repo, number, "Lane %s made no change for the fix to #%d and said why "
                      "on the pull request; the review bot was asked to review it again."
                      % (lane, pr))
        _say("reported %s#%d: no change for #%d; review requested" % (repo, number, pr))

    def _lane_said(self, lane: str, name: str) -> str:
        """The agent's own words from the run log: every line that is not a
        runner line, with local paths and credential shapes removed, capped."""
        log = os.path.join(self.s.home, "logs", lane, name + ".log")
        try:
            with open(log, encoding="utf-8", errors="replace") as fh:
                lines = [line.rstrip() for line in fh]
        except OSError:
            return ""
        said = [SECRET_RE.sub("<redacted>", PATH_RE.sub("<path>", line)) for line in lines
                if not re.match(r"^\[\d{4}-\d\d-\d\dT", line) and not line.startswith("rsync(")]
        text = "\n".join(said).strip()
        if len(text) > LANE_SAID_MAX:
            text = text[:LANE_SAID_MAX].rsplit("\n", 1)[0] + "\n(cut; the rest is in the lane log)"
        return text

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

    def _spend_shown(self) -> Optional[spend.Summary]:
        """The anthropic spend, once there is a budget or a recorded run."""
        summary = self._anthropic_spend()
        if summary.runs or summary.today or self.s.anthropic.usd is not None:
            return summary
        return None

    def _lanes_body(self, rows: List[Tuple[str, str, str]]) -> str:
        summary = self._spend_shown()
        return "\n".join(
            [LANES_MARKER, "Updated " + self._utc(self.now), "",
             "| lane | available | heartbeat (UTC) |", "|---|---|---|"]
            + ["| %s | %s | %s |" % row for row in rows]
            + (["", summary.line()] if summary is not None else []))

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
        summary = self._spend_shown()
        if summary is not None:
            # A new run (or a budget change) republishes; the clock alone waits
            # for the hourly republish.
            values["anthropic spend"] = "%d runs, $%.2f, budget %s" % (
                summary.runs, summary.spent, self.s.anthropic.usd)
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
            self.post(url, {"content": content[:DISCORD_MAX], "allowed_mentions": allowed,
                            "flags": SUPPRESS_EMBEDS})
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

    @staticmethod
    def _report_body(comment: Dict[str, Any]) -> str:
        return str(comment.get("body") or "")[len(REPORT_MARKER):].lstrip("\r\n ")

    @staticmethod
    def _report_link(issue: Dict[str, Any], comment: Dict[str, Any]) -> str:
        return str(comment.get("html_url") or issue.get("html_url") or "")

    def _report_text(self, issue: Dict[str, Any], comment: Dict[str, Any]) -> str:
        title = " ".join(str(issue.get("title") or "").split())
        body = self._report_body(comment)
        link = self._report_link(issue, comment)
        prefix = title + "\n"
        suffix = "\n" + link if link else ""
        room = DISCORD_MAX - len(prefix) - len(suffix)
        if len(body) > room:
            # Keep whole report lines and reserve the comment link at the end.
            body = body[:max(0, room)].rsplit("\n", 1)[0]
        return (prefix + body + suffix)[:DISCORD_MAX]

    @staticmethod
    def _short_name(issue: Dict[str, Any]) -> str:
        """A "Short name:" line in the goal home body, else its title."""
        m = SHORT_NAME_RE.search(str(issue.get("body") or ""))
        name = " ".join((m.group(1) if m else str(issue.get("title") or "")).split())
        # Anyone who can edit the goal home writes this text: keep its
        # markdown out of the bold it is shown in.
        name = re.sub(r"[*_`~|\\]", "", name).strip()
        return name[:SHORT_NAME_MAX] or "#%s" % issue.get("number")

    @staticmethod
    def _chunks(text: str) -> List[str]:
        """The text in Discord-sized messages, cut at line breaks."""
        parts = []
        while len(text) > DISCORD_MAX:
            cut = text.rfind("\n", 0, DISCORD_MAX + 1)
            if cut <= 0:
                cut = DISCORD_MAX
            parts.append(text[:cut])
            text = text[cut:].lstrip("\n")
        if text.strip():
            parts.append(text)
        return parts

    def _bot_reports(self) -> bool:
        return self.s.channel_id.isdigit() and bool(self.s.bot_token_ssm)

    def _report_sender(self) -> Optional[Callable[[Dict[str, Any], Dict[str, Any]], bool]]:
        """A function that sends one report and says whether it went out: as
        the bot when its settings are set, else through the webhook. None
        when the secret could not be read."""
        if self._bot_reports():
            token = self._bot_token()
            if token is None:
                return None
            return lambda issue, comment: self._report_by_bot(token, issue, comment)
        url = self._webhook()
        if url is None:
            return None
        return lambda issue, comment: self._send(
            url, self._report_text(issue, comment), mention=False)

    def _report_by_bot(self, token: str, issue: Dict[str, Any],
                       comment: Dict[str, Any]) -> bool:
        """One short line in the channel, then a thread on it holding the full
        report and the comment link, so tapping the line expands it."""
        chan = self.s.channel_id
        name = self._short_name(issue)
        body = self._report_body(comment)
        first = next((line.lstrip("#").strip() for line in body.splitlines()
                      if line.lstrip("#").strip()), "")
        line = ("**%s** %s" % (name, first)).strip()
        if len(line) > SHORT_LINE_MAX:
            line = line[:SHORT_LINE_MAX - 4].rstrip() + " ..."
        ok, made = self._bot(token, "POST", "/channels/%s/messages" % chan, {
            "content": line, "allowed_mentions": {"parse": []}})
        if not ok:
            return False
        # The line is out: from here a failure is logged and not retried, so
        # the line is never posted twice.
        msg_id = str(made.get("id") or "") if isinstance(made, dict) else ""
        where = self._report_thread(token, msg_id, name) if msg_id.isdigit() else ""
        if not where:
            # No thread: the full report goes in the channel, so it is never
            # lost behind a line that does not expand.
            _say("no thread for the report line; the full report goes in the channel")
            where = chan
        link = self._report_link(issue, comment)
        for part in self._chunks(body + ("\n" + link if link else "")):
            ok, _ = self._bot(token, "POST", "/channels/%s/messages" % where, {
                "content": part, "allowed_mentions": {"parse": []}})
            if not ok:
                break
        return True

    def _report_thread(self, token: str, msg_id: str, name: str) -> str:
        """The id of a new thread on the report line, or "" when Discord
        would not make one."""
        when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(self.now))
        ok, thread = self._bot(token, "POST", "/channels/%s/messages/%s/threads"
                               % (self.s.channel_id, msg_id), {
                                   "name": ("%s %s" % (name, when))[:THREAD_NAME_MAX],
                                   "auto_archive_duration": THREAD_ARCHIVE_MINUTES})
        thread_id = str(thread.get("id") or "") if isinstance(thread, dict) else ""
        return thread_id if ok and thread_id.isdigit() else ""

    def _report_comments(self, repo: str, issues: List[Dict[str, Any]], since: str
                         ) -> List[Tuple[int, Dict[str, Any], Dict[str, Any]]]:
        comments = []
        for issue in issues:
            path = "/repos/%s/issues/%d/comments?since=%s" % (
                repo, int(issue["number"]), since)
            for comment in self.gh.paginate(path):
                if (isinstance(comment, dict)
                        and str(comment.get("body") or "").startswith(REPORT_MARKER)):
                    comments.append((self._report_id(comment), issue, comment))
        comments.sort(key=lambda row: row[0])
        return comments

    def _forward_repo_reports(self, repo: str, comments: List[Tuple[int, Dict[str, Any], Dict[str, Any]]],
                              ledger: Dict[str, Any], path: str, allowed: set) -> bool:
        key = repo.replace("/", ".")
        if key not in ledger:
            ledger[key] = max((ident for ident, _, _ in comments), default=0)
            return True
        try:
            last_id = int(ledger[key])
        except (TypeError, ValueError):
            last_id = 0
        send = None
        secret_checked = False
        for ident, issue, comment in comments:
            if ident <= last_id:
                continue
            author = str((comment.get("user") or {}).get("login") or "").lower()
            if author in allowed:
                if not secret_checked:
                    send = self._report_sender()
                    secret_checked = True
                if send is None:
                    # Unreadable secret: keep the position and the read time,
                    # so the next pass reads this window again.
                    return False
                if not send(issue, comment):
                    _write_json(path, ledger)
                    return False
            ledger[key] = ident
            last_id = ident
        return True

    def _report_window(self, checked: Any) -> Optional[str]:
        """The time to read reports from, or None when the last read is too
        recent to read again."""
        if not isinstance(checked, (int, float)) or isinstance(checked, bool):
            start = self.now - REPORT_FIRST_LOOKBACK
        elif 0 <= self.now - checked < REPORT_CHECK:
            return None
        else:
            start = checked - REPORT_OVERLAP
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start))

    def forward_reports(self) -> None:
        if not (self.s.webhook_ssm or self._bot_reports()):
            return
        path = os.path.join(self.state, "reports.json")
        ledger = _read_json(path, {})
        if not isinstance(ledger, dict):
            raise StateError("state file %s does not hold an object" % path)
        since = self._report_window(ledger.get("_checked"))
        if since is None:
            return
        allowed = {a.lower() for a in self.s.authors}
        # Forget repos that left LOOM_REPOS, so the ledger stays their size.
        keep = {r.replace("/", ".") for r in self.s.repos} | {"_checked"}
        for stale in [k for k in ledger if k not in keep]:
            del ledger[stale]
        for repo in self.s.repos:
            if not tickets.REPO_RE.fullmatch(repo):
                continue
            issues = self._goal_issues(repo)
            comments = self._report_comments(repo, issues, since)
            if not self._forward_repo_reports(repo, comments, ledger, path, allowed):
                return
        ledger["_checked"] = self.now
        _write_json(path, ledger)

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

    def _brain_alert_text(self, last: Dict[str, Any], ok_at: Optional[float],
                          journal: Optional[Dict[str, Any]] = None) -> str:
        at, outcome, code = last.get("at"), last.get("outcome"), last.get("exit")
        when = (self._utc(at) if not isinstance(at, bool) and isinstance(at, (int, float))
                else "an unknown time")
        outcome = outcome if outcome in BRAIN_OUTCOMES else "unknown"
        code = code if isinstance(code, int) and not isinstance(code, bool) else "?"
        since = ("since " + self._utc(ok_at)) if ok_at is not None else "yet"
        seen = ("Newest journal: %s on %s." % (self._utc(journal["at"]), journal["goal"])
                if journal else "No journal from an allowed author in the last day.")
        return ("Urgent: the Loom brain has not finished a tick %s, so no goal is "
                "moving. %s Last tick on the lane machine: %s, %s (exit %s). Its log "
                "is brain.log there." % (since, seen, when, outcome, code))

    @staticmethod
    def _epoch(stamp: str) -> Optional[float]:
        try:
            return calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))
        except (ValueError, TypeError):
            return None

    def _newest_journal(self) -> Optional[Dict[str, Any]]:
        """The newest journal comment from an allowed login on any open
        loom:goal issue in LOOM_REPOS, as {"at", "goal"}, or None when there is
        none in the last day. Raises GitHubError when GitHub cannot be read."""
        since = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                              time.gmtime(self.now - JOURNAL_LOOKBACK))
        best: Optional[Dict[str, Any]] = None
        for repo in self.s.repos:
            if not tickets.REPO_RE.fullmatch(repo):
                continue
            for goal in self.gh.paginate(
                    "/repos/%s/issues?labels=loom:goal&state=open" % repo):
                if not isinstance(goal, dict) or "number" not in goal:
                    continue
                number = int(goal["number"])
                for c in self.gh.paginate(
                        "/repos/%s/issues/%d/comments?since=%s" % (repo, number, since)):
                    login = (c.get("user") or {}).get("login")
                    body = str(c.get("body") or "").lstrip()
                    at = self._epoch(c.get("created_at", ""))
                    # since= filters on the last edit, so an old journal
                    # edited today still comes back: check when it was posted.
                    if (login in self.s.authors and body.startswith(JOURNAL_MARKER)
                            and at is not None and self.now - at <= JOURNAL_LOOKBACK
                            and (best is None or at > best["at"])):
                        best = {"at": at, "goal": "%s#%d" % (repo, number)}
        return best

    def _journal(self) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """(read ok, newest journal), re-read at most every JOURNAL_CHECK
        seconds and kept in the station state between reads."""
        path = os.path.join(self.state, "brain-journal.json")
        cache = _read_json(path, {}, strict=False)
        checked = cache.get("checked") if isinstance(cache, dict) else None
        if (isinstance(checked, (int, float)) and not isinstance(checked, bool)
                and 0 <= self.now - checked < JOURNAL_CHECK):
            journal = cache.get("journal")
            return True, journal if isinstance(journal, dict) else None
        try:
            journal = self._newest_journal()
        except GitHubError as err:
            _say("brain journal read failed: %s" % err)
            return False, None
        _write_json(path, {"checked": self.now, "journal": journal})
        return True, journal

    def _brain_state(self, ok_at: Optional[float]
                     ) -> Optional[Tuple[bool, Optional[Dict[str, Any]]]]:
        """(fresh, newest journal), or None when GitHub cannot be read. The
        journal is read only when the local tick files say stale."""
        if self._brain_fresh(ok_at):
            return True, None
        read, journal = self._journal()
        if not read:
            return None
        return bool(journal and self.now - journal["at"] <= BRAIN_STALE), journal

    def _tell_brain(self, url: str, path: str, fresh: bool, last: Dict[str, Any],
                    ok_at: Optional[float], journal: Optional[Dict[str, Any]]) -> None:
        if fresh:
            good = max(ok_at or 0, journal["at"] if journal else 0) or self.now
            if self._send(url, "The Loom brain is ticking again (last good tick %s)."
                          % self._utc(good)):
                _write_json(path, {})
        elif self._send(url, self._brain_alert_text(last, ok_at, journal)):
            _write_json(path, {"sent": True, "at": self.now})

    def watch_brain(self) -> None:
        """Tell the operator once when the brain stops ticking, and once when
        it is back. The brain is fresh when the lane machine's own tick files
        say so or a journal on any goal is at most BRAIN_STALE old."""
        if not self.s.webhook_ssm:
            return
        brain = self._brain()
        if brain is None:
            return
        last, ok_at = brain
        state = self._brain_state(ok_at)
        if state is None:
            return
        fresh, journal = state
        path = os.path.join(self.state, "brain-alert.json")
        alert = _read_json(path, {}, strict=False)
        alerted = isinstance(alert, dict) and alert.get("sent") is True
        # A message is due only when the state moved: stale and not yet told,
        # or fresh again after an alert.
        if fresh != alerted:
            return
        url = self._webhook()
        if url is not None:
            self._tell_brain(url, path, fresh, last, ok_at, journal)

    # -- talking to the brain --------------------------------------------
    @staticmethod
    def _num(value: Any) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    def relay_operator(self) -> None:
        """Relay the operator's new Discord messages to goal homes as
        loom:operator notes, and forward the brain's loom:reply comments back
        to the channel as replies. Each runs at most every OPERATOR_CHECK
        seconds; with any setting empty nothing runs."""
        if not (self.s.channel_id.isdigit() and self.s.bot_token_ssm
                and self.s.mention.isdigit()):
            return
        path = os.path.join(self.state, "operator.json")
        ledger = _read_json(path, {})
        if not isinstance(ledger, dict):
            raise StateError("state file %s does not hold an object" % path)
        checked = ledger.get("checked")
        if self._num(checked) and 0 <= self.now - checked < OPERATOR_CHECK:
            return
        token = self._bot_token()
        if token is None:
            return
        ledger["checked"] = self.now
        try:
            self._relay_messages(token, ledger, path)
            self._forward_replies(token, ledger, path)
        finally:
            _write_json(path, ledger)

    def _bot_token(self) -> Optional[str]:
        try:
            return self.read_secret(self.s.bot_token_ssm, self.s.region)
        # Deliberately broad, as in _webhook: only the type is logged.
        except Exception as err:  # noqa: BLE001
            _say("could not read the bot token parameter (%s); next pass"
                 % type(err).__name__)
            return None

    def _bot(self, token: str, method: str, path: str,
             body: Optional[Dict[str, Any]] = None) -> Tuple[bool, Any]:
        """(ok, reply) for one Discord call. A DiscordError's text is ours and
        never carries the token; any other error is logged by type only.
        Every message the bot posts suppresses link previews."""
        if method == "POST" and body is not None and "content" in body:
            body = dict(body, flags=SUPPRESS_EMBEDS)
        try:
            return True, self.discord(method, path, token, body)
        except Exception as err:  # noqa: BLE001
            why = str(err) if type(err) is DiscordError else type(err).__name__
            _say("discord call failed (%s); next pass" % why)
            return False, None

    def _goal_homes(self) -> List[Tuple[str, Dict[str, Any]]]:
        """(repo, issue) for every open loom:goal issue in LOOM_REPOS, read
        once per pass. Raises GitHubError: a partial list could send a note
        to the wrong goal."""
        if self._goals is None:
            self._goals = [(repo, issue) for repo in self.s.repos
                           if tickets.REPO_RE.fullmatch(repo)
                           for issue in self._goal_issues(repo)]
        return self._goals

    def _goal_for(self, text: str) -> Optional[Tuple[str, int, str, bool]]:
        """(repo, number, link, named) of the goal the message names: an
        owner/name#N or a #N of a goal home in LOOM_REPOS, else a word found
        in one goal title only, else LOOM_DISCORD_DEFAULT_GOAL (not named)."""
        goals = {(repo, int(i["number"])): i for repo, i in self._goal_homes()}

        def found(key: Tuple[str, int], named: bool = True) -> Tuple[str, int, str, bool]:
            return key[0], key[1], str((goals.get(key) or {}).get("html_url") or ""), named
        for m in GOAL_REF_RE.finditer(text):
            if (m.group(1), int(m.group(2))) in goals:
                return found((m.group(1), int(m.group(2))))
        numbers = {int(m.group(1)) for m in NUMBER_REF_RE.finditer(text)}
        hits = [key for key in goals if key[1] in numbers]
        if len(hits) == 1:
            return found(hits[0])
        holders: Dict[str, set] = {}
        for key, issue in goals.items():
            for word in set(re.findall(r"[a-z0-9]+", str(issue.get("title") or "").lower())):
                if len(word) >= TITLE_WORD_MIN:
                    holders.setdefault(word, set()).add(key)
        named = {next(iter(holders[w])) for w in set(re.findall(r"[a-z0-9]+", text.lower()))
                 if len(holders.get(w, ())) == 1}
        if len(named) == 1:
            return found(named.pop())
        m = GOAL_REF_RE.fullmatch(self.s.default_goal)
        if m and m.group(1) in self.s.repos and tickets.REPO_RE.fullmatch(m.group(1)):
            return found((m.group(1), int(m.group(2))), named=False)
        return None

    def _relay_messages(self, token: str, ledger: Dict[str, Any], path: str) -> None:
        after = str(ledger.get("after") or "")
        if not after.isdigit():
            # First read: start a little before now rather than relaying the
            # channel's whole history.
            after = str(_snowflake(self.now - OPERATOR_FIRST_LOOKBACK))
            ledger["after"] = after
        chan = self.s.channel_id
        ok, data = self._bot(token, "GET", "/channels/%s/messages?after=%s&limit=100"
                             % (chan, after))
        if not ok or not isinstance(data, list):
            return
        messages = sorted((m for m in data if isinstance(m, dict)
                           and str(m.get("id") or "").isdigit()
                           and int(m["id"]) > int(after)),
                          key=lambda m: int(m["id"]))
        for msg in messages:
            said = self._operator_said(msg)
            if said is not None:
                self._relay_one(token, str(msg["id"]), *said)
            ledger["after"] = str(msg["id"])
            _write_json(path, ledger)

    def _operator_said(self, msg: Dict[str, Any]) -> Optional[Tuple[str, List[str]]]:
        """(text, attachment links) of a message the operator wrote, or None
        for anyone else's message and for an empty one."""
        author = msg.get("author") if isinstance(msg.get("author"), dict) else {}
        if str(author.get("id") or "") != self.s.mention or author.get("bot"):
            return None
        text = str(msg.get("content") or "").strip()
        files = msg.get("attachments") if isinstance(msg.get("attachments"), list) else []
        urls = [str(f.get("url")) for f in files if isinstance(f, dict)
                and str(f.get("url") or "").startswith("https://")]
        return (text, urls) if text or urls else None

    def _relay_one(self, token: str, msg_id: str, text: str, urls: List[str]) -> None:
        """Note one operator message, with a link to each attachment, on its
        goal home, then acknowledge it in the channel. A GitHub error
        propagates before the ledger moves, so the message is tried again
        next pass; the acknowledgements are best effort once the note is
        posted."""
        chan = self.s.channel_id
        goal = self._goal_for(text)
        if goal is None:
            self._bot(token, "POST", "/channels/%s/messages" % chan, {
                "content": "Not noted: name the goal as owner/name#N or #N.",
                "message_reference": {"message_id": msg_id, "fail_if_not_exists": False},
                "allowed_mentions": {"parse": []}})
            _say("operator message %s names no goal; not noted" % msg_id)
            return
        repo, number, goal_link, named = goal
        note = text
        if urls:
            # Discord's attachment links, so the brain can open a screenshot.
            note = (text + "\n\n" if text else "") + "Attachments:\n" + "\n".join(
                "- " + url for url in urls)
        try:
            # The note lands on GitHub as written: the shared checks keep out
            # the review bot's at-handle and closing keywords next to issues.
            _text.check(note)
        except ValueError as err:
            self._bot(token, "POST", "/channels/%s/messages" % chan, {
                "content": "Not noted: %s. Reword it and send again." % err,
                "message_reference": {"message_id": msg_id, "fail_if_not_exists": False},
                "allowed_mentions": {"parse": []}})
            _say("operator message %s refused by the text checks" % msg_id)
            return
        made = self.gh.rest("POST", "/repos/%s/issues/%d/comments" % (repo, number),
                            {"body": OPERATOR_MARKER % msg_id + "\n" + note}).data
        link = str((made or {}).get("html_url") or "") if isinstance(made, dict) else ""
        link = link or goal_link
        _say("noted operator message %s on %s#%d" % (msg_id, repo, number))
        self._bot(token, "PUT", "/channels/%s/messages/%s/reactions/%s/@me"
                  % (chan, msg_id, CHECKMARK))
        self._bot(token, "POST", "/channels/%s/messages" % chan, {
            "content": " ".join(filter(None, [
                "Noted for the brain", "on %s#%d" % (repo, number) if named else "", link])),
            "message_reference": {"message_id": msg_id, "fail_if_not_exists": False},
            "allowed_mentions": {"parse": []}})

    @staticmethod
    def _reply_text(body: str, link: str) -> str:
        suffix = "\n" + link if link else ""
        room = DISCORD_MAX - len(suffix)
        if len(body) > room:
            body = body[:max(0, room - 4)].rstrip() + " ..."
        return (body + suffix)[:DISCORD_MAX]

    def _forward_replies(self, token: str, ledger: Dict[str, Any], path: str) -> None:
        """Send each trusted loom:reply comment on a goal home to the channel
        once, as a reply to the Discord message it names."""
        last = ledger.get("replies_at")
        start = (last if self._num(last) else self.now) - REPLY_OVERLAP
        since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start))
        sent = ledger.get("forwarded") if isinstance(ledger.get("forwarded"), dict) else {}
        # Ids are kept as long as their reply could still be forwarded.
        sent = {k: v for k, v in sent.items()
                if self._num(v) and self.now - v <= REPLY_LOOKBACK}
        ledger["forwarded"] = sent
        allowed = {a.lower() for a in self.s.authors}
        found = []
        for repo, goal in self._goal_homes():
            for c in self.gh.paginate("/repos/%s/issues/%d/comments?since=%s"
                                      % (repo, int(goal["number"]), since)):
                if not isinstance(c, dict):
                    continue
                body = str(c.get("body") or "")
                m = REPLY_RE.match(body)
                login = str((c.get("user") or {}).get("login") or "").lower()
                ident = self._report_id(c)
                # since= filters on the last edit: check when it was posted.
                at = self._epoch(c.get("created_at", ""))
                if (m and login in allowed and ident and str(ident) not in sent
                        and at is not None and self.now - at <= REPLY_LOOKBACK):
                    link = str(c.get("html_url") or goal.get("html_url") or "")
                    found.append((ident, m.group(1), body[m.end():].strip(), link, at))
        for ident, msg_id, text, link, at in sorted(found, key=lambda row: row[0]):
            ok, _ = self._bot(token, "POST", "/channels/%s/messages" % self.s.channel_id, {
                "content": self._reply_text(text, link),
                "message_reference": {"message_id": msg_id, "fail_if_not_exists": False},
                "allowed_mentions": {"parse": [], "replied_user": True}})
            if not ok:
                return  # replies_at stays, so the next pass reads this window again
            sent[str(ident)] = at
            _write_json(path, ledger)
            _say("forwarded brain reply %d to the channel" % ident)
        ledger["replies_at"] = self.now

    # -- one pass --------------------------------------------------------
    def run_factory(self) -> None:
        """Merge ready lane pull requests, send the rest back, and move stuck
        tickets up the model ladder (bus/factory.py), at most every
        factory.FACTORY_CHECK seconds. LOOM_FACTORY=0 turns it off."""
        if not self.s.factory_on or not factory.due(self.state, self.now):
            return
        repos = [r for r in self.s.repos if tickets.REPO_RE.fullmatch(r)]
        factory.Factory(self.gh, repos, self.now, trusted=self.s.authors,
                        review_check=self.s.review_check,
                        review_bot=self.s.review_bot,
                        limited=self.limited_lanes()).run()

    def run_pass(self) -> None:
        self.finish_claims()
        self.report_outcomes()
        self.claim_new()
        for step in (self.publish_lanes, self.forward_asks, self.forward_reports,
                     self.watch_brain, self.relay_operator, self.run_factory):
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
