"""The station: briefed ticket issues in, local briefs out, outcomes reported.

One pass (python3 -m bus station, run by launchd) does three things under one
lock: finishes any claim left half done, reports finished runs back to their
tickets, and claims the oldest trusted briefed ticket for each lane that can
take one. docs/BRAIN.md ("Claim and outcome", "Polling budget") is the contract.
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta
import errno
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import factory
from . import metrics
from . import revert
from . import spend
from . import text as _text
from . import tickets
from . import transcripts
from .brain import HUMAN_MARKER
from . import brain
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
# A lane tree (lanes/run_lane.sh clones one a brief attempt) whose brief is no
# longer waiting or running is removed once it is this old. On 2026-10-13 the
# 385 left behind held 7.8 GB and the disk filled.
TREE_KEEP = 3600
# A fix brief's answers to findings (lanes/run_lane.sh keep_answers).
ANSWER_FILE_RE = re.compile(r"^(PRRT_[A-Za-z0-9_-]{1,100})\.md$")
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
# goes back to briefed, and the factory moves it to the next lane. It follows
# the class prefix ("[limit] limit: ...") and, unlike a classed provider limit
# that _retry handles, never counts against LOOM_RETRY_MAX.
LIMIT = "limit:"
# The runner's reason when a fix brief's push was refused because another
# push moved the pull request's branch during the run. Not a failure: the
# ticket goes back to loom:pr, and the factory judges the new head.
BRANCH_MOVED = "branch moved:"
# Asks the review bot to review a pull request's head again; it resolves the
# threads it no longer raises. LOOM_REVIEW_AGAIN overrides it.
REVIEW_AGAIN = "/grug improve"
LANE_SAID_MAX = 2500
PATH_RE = _text.PATH_RE
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
REGRESSION = "loom:regression"
DISCORD_MAX = 2000
POST_TIMEOUT = 30
DIGEST_HEAD = "Asks waiting for you:"
# Open loom:human issues wait for the daily digest.
HUMAN = "loom:human"
HUMAN_HEAD = "Waiting on you"
HUMAN_SHOWN = 15
HUMAN_TITLE_MAX = 100
HUMAN_ASK_MAX = 140
# When every ask does not fit in the daily notification, the full digest is a
# comment on this issue in each ask's own repo (so a private repo's asks stay
# in that repo), and the notification links to it.
DIGEST_LABEL = "loom:digest"
DIGEST_TITLE = "Loom asks digest"
DIGEST_MARKER = "<!-- loom:digest v1 -->"
# Room kept for the mention, which _send adds in front.
MENTION_ROOM = 40
ASK_GOAL_REF_RE = re.compile(r"(?<![\w/])#([0-9]+)\b")
FIX_ROUNDS_DEFAULT = 3
REPORT_MARKER = "<!-- loom:report v1 -->"
# Reports come every 2 hours, so the goal homes are read at most this often,
# and only for comments since the last read (with an overlap; the ledger of
# comment ids keeps a report from going out twice).
REPORT_CHECK = 300
REPORT_OVERLAP = 3600
REPORT_FIRST_LOOKBACK = 24 * 3600
# A backlog goal home is triaged at most this often.
TRIAGE_INTERVAL = 3600
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
# Automatic retries (docs/BRAIN.md "Claim and outcome"). The runner starts
# each failure reason with its class; a reason without one is a work failure.
CLASS_RE = re.compile(r"^\[(limit|transient|work)\] ")
RETRY_CLASSES = ("limit", "transient")
RETRY_MAX = 2
RETRY_RE = re.compile(r"Automatic retry [0-9]+ of [0-9]+ \((?:limit|transient)\): "
                      r"moved from lane ([a-z0-9-]+) to lane ([a-z0-9-]+)\.")
# The brain's lane order for a retry, after the failed lane's own provider.
# claude is never chosen: it spends the operator's own subscription.
RETRY_ORDER = ["anthropic", "anthropic-2", "anthropic-3", "anthropic-4",
               "codex", "codex-2", "opencode-go", "agy"]
# Free lanes take only easy tickets on public repos.
FREE_LANES = ["openrouter", "zen", "zai", "workersai"]
# The model families each lane's runner accepts (lanes/run_lane.sh); codex
# takes any model outside the claude family.
CLAUDE_MODELS = ["claude*", "opus*", "sonnet*", "haiku*", "fable*"]
LANE_MODELS = {
    "claude": CLAUDE_MODELS,
    "anthropic": ["claude*"],
    "openrouter": ["openrouter/*:free"],
    "opencode-go": ["opencode-go/*"],
    "zen": ["opencode/*-free"],
    "zai": ["zai/glm-4.7-flash", "zai/glm-4.5-flash"],
    "workersai": ["cloudflare-workers-ai/@cf/*"],
    "agy": ["gemini-*", "claude-*", "gpt-oss-*"],
}
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
# Transcript uploads (docs/LANES.md, "Transcript uploads"): a run that fails
# to upload this many times is given up. One pass uploads at most
# UPLOAD_PER_PASS runs and stops at the first failure (the dashboard is
# likely down), so uploads cannot hold the station's lock for long.
UPLOAD_TRIES = 5
UPLOAD_PER_PASS = 10

SecretReader = Callable[[str, str], str]
Poster = Callable[[str, Dict[str, Any]], None]
# (URL, bearer token, body) -> None; raises on any failure.
Uploader = Callable[[str, str, Dict[str, Any]], None]
# A finished run: (ledger key, brain tick start or None, body builder).
Run = Tuple[str, Optional[float], Callable[[], Dict[str, Any]]]
# (method, path under the API, bot token, JSON body or None) -> parsed reply.
DiscordCall = Callable[[str, str, str, Optional[Dict[str, Any]]], Any]


MAX_FIX_BODY = 60000
# Below this share of any GitHub rate limit a pass does claims and reports
# only: the hourly budget is shared with the brains, and a station that spends
# it all stops claiming until the reset.
BUDGET_FLOOR = 0.15
# A repo with no briefed, running or pr ticket is scanned this often at most.
IDLE_SCAN = 600
# Conditional-request answers unused this long are removed.
CACHE_KEEP = 24 * 3600


class Settings:
    def __init__(self, home: str, repos: List[str], authors: List[str],
                 owners: List[str], status_repo: str = "", webhook_ssm: str = "",
                 mention: str = "", digest_hour: int = 9,
                 region: str = "us-east-1", retry_max: int = RETRY_MAX,
                 channel_id: str = "",
                 bot_token_ssm: str = "", default_goal: str = "",
                 anthropic: Optional[spend.Budget] = None,
                 factory_on: bool = False,
                 review_check: str = factory.DEFAULT_REVIEW_CHECK,
                 review_bot: str = factory.DEFAULT_REVIEW_BOT,
                 no_test_repos: Sequence[str] = (),
                 metrics_goal: str = "",
                 transcript_url: str = "",
                 transcript_token_ssm: str = "") -> None:
        self.home = home
        self.repos = repos
        self.authors = authors
        self.owners = owners
        self.status_repo = status_repo
        self.webhook_ssm = webhook_ssm
        self.mention = mention
        self.digest_hour = digest_hour
        self.region = region
        self.retry_max = retry_max
        self.channel_id = channel_id
        self.bot_token_ssm = bot_token_ssm
        self.default_goal = default_goal
        self.anthropic = anthropic or spend.Budget()
        # Off unless asked for, so a Settings built in code does not start
        # merging; from_env turns it on unless LOOM_FACTORY=0.
        self.factory_on = factory_on
        self.review_check = review_check
        self.review_bot = review_bot
        self.no_test_repos = list(no_test_repos)
        self.metrics_goal = metrics_goal
        self.transcript_url = transcript_url
        self.transcript_token_ssm = transcript_token_ssm

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
            retry_max = int(env.get("LOOM_RETRY_MAX", "").strip() or RETRY_MAX)
        except ValueError:
            retry_max = RETRY_MAX
        retry_max = max(retry_max, 0)
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
                   retry_max=retry_max,
                   channel_id=env.get("LOOM_DISCORD_CHANNEL_ID", "").strip(),
                   bot_token_ssm=env.get("LOOM_DISCORD_BOT_TOKEN_SSM", "").strip(),
                   default_goal=env.get("LOOM_DISCORD_DEFAULT_GOAL", "").strip(),
                   anthropic=budget,
                   factory_on=env.get("LOOM_FACTORY", "").strip() not in ("0", "off", "false"),
                   review_check=(env.get("LOOM_REVIEW_CHECK", "").strip()
                                 or factory.DEFAULT_REVIEW_CHECK),
                   review_bot=(env.get("LOOM_REVIEW_BOT", "").strip()
                               or factory.DEFAULT_REVIEW_BOT),
                   no_test_repos=words("LOOM_NO_TEST_REPOS"),
                   metrics_goal=env.get("LOOM_METRICS_GOAL", "").strip(),
                   transcript_url=env.get("LOOM_TRANSCRIPT_URL", "").strip(),
                   transcript_token_ssm=env.get("LOOM_TRANSCRIPT_TOKEN_SSM", "").strip())


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
    """A SecureString parameter, read the way lanes/run_lane.sh reads keys.
    The read is bounded by POST_TIMEOUT, so a hung call cannot stall a pass."""
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


# Answers about the body itself: sending the same body again cannot succeed.
# A 401 or 403 is about the token, so those retry like a network error.
REFUSED_BODY = (400, 413, 422)


class UploadRefused(OSError):
    """The dashboard read the body and refused it (REFUSED_BODY)."""

    def __init__(self, status: int) -> None:
        super().__init__("the transcript upload answered %d" % status)
        self.status = status


def post_transcript(url: str, token: str, body: Dict[str, Any]) -> None:
    """POST one transcript to the dashboard. Errors never carry the URL or
    the token."""
    if not url.startswith("https://"):
        raise OSError("the transcript URL is not https")
    req = urllib.request.Request(  # noqa: S310 - https only, checked above
        url, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "loom-station",
                 "Authorization": "Bearer " + token})
    try:
        with _OPENER.open(req, timeout=POST_TIMEOUT) as resp:  # noqa: S310
            status = resp.status
    except urllib.error.HTTPError as err:
        status = err.code
        err.close()
    except (urllib.error.URLError, OSError):
        raise OSError("the transcript upload failed to connect") from None
    if status in REFUSED_BODY:
        raise UploadRefused(status)
    if not 200 <= status < 300:
        raise OSError("the transcript upload answered %d" % status)


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


def _read_answer(path: str) -> str:
    """An answer file's text, redacted of credentials and local paths; empty
    unless it is a regular file with one link (never followed through a link)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as err:
        # ELOOP is O_NOFOLLOW refusing a link, which is the point; anything
        # else (permissions, I/O) is worth a line in the log.
        if err.errno not in (errno.ENOENT, errno.ELOOP):
            _say("could not read answer %s: %s" % (os.path.basename(path), err))
        return ""
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not (stat.S_ISREG(st.st_mode) and st.st_nlink == 1):
            return ""
        raw = fh.read(ANSWER_MAX).decode("utf-8", errors="replace")
    return SECRET_RE.sub("<redacted>", PATH_RE.sub("<path>", raw)).strip()


def _open_on(node: Dict[str, Any], repo: str, pr: int) -> bool:
    """An unresolved review thread on this pull request of this repo."""
    where = node.get("pullRequest") or {}
    return (not node.get("isResolved")
            and (where.get("repository") or {}).get("nameWithOwner") == repo
            and where.get("number") == pr)


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


def _after_first_line(text: str) -> str:
    """The text after its first non-empty line, leading blank lines dropped."""
    lines = text.splitlines()
    first = next((i for i, line in enumerate(lines) if line.lstrip("#").strip()), len(lines))
    return "\n".join(lines[first + 1:]).strip("\n")


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


def _clip(text: str, limit: int) -> str:
    # A trailing backslash would escape the ellipsis in Discord's formatting.
    return text if len(text) <= limit else text[:limit - 3].rstrip(" \\") + "..."


def provider(lane: str) -> str:
    """The provider lane of a slot (anthropic-2 -> anthropic), as the runner
    maps it."""
    return re.sub(r"-[0-9]+$", "", lane)


def failure_class(reason: str) -> str:
    m = CLASS_RE.match(reason)
    return m.group(1) if m else "work"


def lane_takes_model(lane: str, model: str) -> bool:
    """Whether the lane's runner accepts the model (lanes/run_lane.sh)."""
    lane = provider(lane)
    if lane == "codex":
        return not any(fnmatch.fnmatchcase(model, p) for p in CLAUDE_MODELS)
    return any(fnmatch.fnmatchcase(model, p) for p in LANE_MODELS.get(lane, []))


def needs_public_repo(lane: str, model: str) -> bool:
    """Free lanes, and lanes whose provider trains on prompts, take public
    repos only."""
    lane = provider(lane)
    return (lane in FREE_LANES or lane == "agy"
            or (lane == "opencode-go" and fnmatch.fnmatchcase(model, "*muse-spark*contributor*")))


def retry_chain(comments: List[Any]) -> Tuple[int, set, Optional[Tuple[str, str]]]:
    """(automatic retries since the ticket last went to the brain or opened a
    pull request, the lanes those retries left, and (from, to) of the latest
    retry when no claim has followed it: a retry whose relabel did not
    happen yet)."""
    used, left, pending = 0, set(), None
    for c in comments:
        body = str(c.get("body") or "").strip() if isinstance(c, dict) else ""
        m = RETRY_RE.match(body)
        if m:
            used += 1
            left.add(m.group(1))
            pending = (m.group(1), m.group(2))
        elif body.startswith("Lane "):
            used, left, pending = 0, set(), None
        elif CLAIM_RE.fullmatch(body):
            pending = None
    return used, left, pending


def scrub(reason: str) -> str:
    text = _text.scrub_paths(" ".join(reason.split()))
    return text[:REASON_MAX]


def _fix_round_limit() -> int:
    try:
        return max(0, int(os.environ.get("LOOM_FIX_ROUNDS", str(FIX_ROUNDS_DEFAULT))))
    except ValueError:
        return FIX_ROUNDS_DEFAULT


def _linked_pr(issue: Dict[str, Any], repo: str, pr: Dict[str, Any]) -> bool:
    number = int(issue.get("number", 0))
    return bool(re.search(r"^Refs %s#%d[ \t]*$" % (re.escape(repo), number),
                          str(pr.get("body") or ""), re.M)
                or re.match(r"loom/%d(?:-|$)" % number,
                            str((pr.get("head") or {}).get("ref") or "")))


class Station:
    def __init__(self, gh: GitHub, settings: Settings,
                 now: Optional[float] = None,
                 read_secret: Optional[SecretReader] = None,
                 post: Optional[Poster] = None,
                 discord: Optional[DiscordCall] = None,
                 upload: Optional[Uploader] = None) -> None:
        self.gh = gh
        self.s = settings
        self.read_secret = read_secret or read_ssm
        self.post = post or post_webhook
        self.upload = upload or post_transcript
        # Where Claude Code and codex keep their sessions (the claude lane's
        # and the brain's transcripts, and codex rollouts).
        self.claude_projects = Path.home() / ".claude" / "projects"
        self.codex_sessions = Path.home() / ".codex" / "sessions"
        self.discord = discord or discord_call
        self._goals: Optional[List[Tuple[str, Dict[str, Any]]]] = None
        self.now = time.time() if now is None else now
        self.state = os.path.join(settings.home, "state", "station")
        self._spend: Optional[spend.Summary] = None
        # This pass's repo scans: the repos due, what was read of each, and
        # which held a ticket in flight.
        self._due: Optional[List[str]] = None
        self._scans: Dict[str, Any] = {}
        self._seen: Dict[str, set] = {}
        self._busy: set = set()
        self._pulls: Dict[str, List[Dict[str, Any]]] = {}
        # The GitHub budget as the last pass left it, and the reset it was
        # last reported low for.
        self._budget: Optional[Dict[str, Any]] = None
        self._told_low = False

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
        # A conditional read (bus/github.py keeps the last answer): an
        # unchanged list is a 304, which the budget does not count.
        resp = self.gh.rest(
            "GET", "/repos/%s/issues?labels=loom:briefed&state=open&per_page=100" % repo)
        items = resp.data if isinstance(resp.data, list) else []
        if resp.status != 304 and len(items) >= 100:
            _say("%s has 100 or more briefed tickets; only the first page is read" % repo)
        out = []
        for item in items:
            if isinstance(item, dict) and "pull_request" not in item:
                item.setdefault("repository_url", "%s/repos/%s" % (self.gh.api, repo))
                out.append(item)
        self._observe(repo, "briefed", bool(out))
        return out

    # -- which repos a pass scans -----------------------------------------
    def _inbox_repos(self) -> set:
        """Repos named by a brief waiting or running in a lane inbox."""
        found = set()
        for lane in LANES:
            try:
                entries = os.listdir(self.inbox(lane))
            except OSError:
                continue
            for entry in entries:
                if not entry.endswith((".md", ".claiming")):
                    continue
                try:
                    with open(os.path.join(self.inbox(lane), entry), encoding="utf-8") as fh:
                        m = ISSUE_LINE_RE.search(fh.read())
                except (OSError, ValueError):
                    continue
                if m:
                    found.add(m.group(1))
        return found

    def _scan_due(self, rec: Any) -> bool:
        if not isinstance(rec, dict) or rec.get("busy") is not False:
            return True
        at = rec.get("at")
        if isinstance(at, bool) or not isinstance(at, (int, float)):
            return True
        # A time in the future (a clock that stepped back) scans now.
        return not 0 <= self.now - at < IDLE_SCAN

    def scan_repos(self) -> List[str]:
        """The watched repos this pass scans: every repo with a ticket in
        flight (briefed, running or pr at its last scan, or a brief for it in
        a lane inbox), and an idle repo once IDLE_SCAN seconds have passed
        since its last scan."""
        if self._due is None:
            saved = _read_json(os.path.join(self.state, "scans.json"), {}, strict=False)
            self._scans = saved if isinstance(saved, dict) else {}
            local = self._inbox_repos()
            self._due = [r for r in self.s.repos if tickets.REPO_RE.fullmatch(r)
                         and (r in local or self._scan_due(self._scans.get(r)))]
        return self._due

    def _observe(self, repo: str, kind: str, busy: bool) -> None:
        self._seen.setdefault(repo, set()).add(kind)
        if busy:
            self._busy.add(repo)

    def record_scans(self) -> None:
        """Remember when each scanned repo was read and whether it held a
        ticket in flight. A due repo whose briefed list was not read this
        pass (no lane was free) is read now: an unchanged list is a 304."""
        due = self.scan_repos()
        for repo in due:
            if "briefed" not in self._seen.get(repo, set()):
                try:
                    self._poll(repo)
                except GitHubError as err:
                    _say("could not list %s: %s" % (repo, err))
        local = self._inbox_repos()
        record = {r: v for r, v in self._scans.items() if r in self.s.repos}
        for repo in due:
            seen = self._seen.get(repo, set())
            prev = record.get(repo)
            was_busy = not isinstance(prev, dict) or prev.get("busy") is not False
            # A list not read this pass (a failed read, or a step the budget
            # deferred) keeps what the last scan saw.
            unread = not {"briefed", "pr"} <= seen
            busy = repo in self._busy or repo in local or (unread and was_busy)
            record[repo] = {"at": self.now, "busy": busy}
        _write_json(os.path.join(self.state, "scans.json"), record)

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
        its fix comment cannot be used (refused, as an untrusted ticket is),
        or when its newest claim's brief is still waiting or running in a
        lane inbox (left for the next pass)."""
        number = int(issue["number"])
        comments = self.gh.paginate("/repos/%s/issues/%d/comments" % (repo, number))
        try:
            fix_source = self._fix_for(repo, comments)
        except ValueError as err:
            self._refuse(repo, issue, str(err))
            return False
        fix, fix_comment_id = fix_source if fix_source else (None, None)

        # A fix brief goes out only once every finding has a disposition.
        if fix is not None:
            # Only parsing sits in the try: parse_findings raises ValueError
            # solely through Finding() (its regexes admit only known
            # dispositions and numeric lines, so in practice a malformed ID),
            # and err names the cause. A ValueError from refusing is not
            # caught here and so cannot be reported as a bad finding.
            try:
                findings = fix.findings_list
            except ValueError as err:
                self._refuse(repo, issue, "invalid pre-roll finding: %s" % err)
                return False
            undispositioned = [f.id for f in findings if not f.disposition]
            if undispositioned:
                self._refuse(repo, issue, "pre-roll findings without dispositions: %s" % ",".join(undispositioned))
                return False

        earlier = sum(1 for c in comments
                      if isinstance(c, dict) and CLAIM_RE.fullmatch(str(c.get("body") or "").strip()))
        if earlier:
            # Two lanes on one ticket push to one branch, and the later push
            # is refused: the newest claim's run must end first.
            newest = tickets.ticket_name(repo, number, earlier)
            busy = [l for l in LANES
                    if os.path.exists(os.path.join(self.inbox(l), newest + ".md"))]
            if busy:
                _say("not claiming %s#%d: its brief %s is still in lane %s's inbox; next pass"
                     % (repo, number, newest, busy[0]))
                return False
        attempt = earlier + 1
        name = tickets.ticket_name(repo, number, attempt)
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
            self._record_slot_sample(False)
            return
        by_lane = self._claim_candidates(free)
        for lane in free:
            self._claim_for_lane(lane, by_lane[lane])
        self._record_slot_sample(any(by_lane[lane] and self._inbox_empty(lane)
                                     for lane in free))

    def _claim_for_lane(self, lane: str, found: List[Tuple[str, Dict[str, Any]]]) -> None:
        """Claim the oldest ticket for one free lane that can be claimed."""
        # A revert of a red default branch (bus/revert.py) goes first, then
        # a regression fix (bus.brain brief --regresses).
        cands = sorted(found, key=lambda c: (
            factory.REVERT_LABEL not in _label_names(c[1]),
            REGRESSION not in _label_names(c[1]),
            str(c[1].get("created_at") or ""), c[0], int(c[1]["number"])))
        for repo, issue in cands:
            try:
                if self._try_ticket(repo, lane, issue):
                    return
            except (GitHubError, OSError) as err:
                _say("could not claim %s#%s: %s" % (repo, issue["number"], err))

    def _claim_candidates(self, free: List[str]) -> Dict[str, List[Tuple[str, Dict[str, Any]]]]:
        by_lane: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {l: [] for l in free}
        for repo in self.s.repos:
            if not tickets.REPO_RE.fullmatch(repo):
                _say("ignoring %r in LOOM_REPOS: not OWNER/NAME" % repo)
        for repo in self.scan_repos():
            try:
                found = self._poll(repo)
            except GitHubError as err:
                _say("could not list %s: %s" % (repo, err))
                continue
            for issue in found:
                lane = _lane_of(issue)
                if lane in by_lane:
                    by_lane[lane].append((repo, issue))
        return by_lane

    def _record_slot_sample(self, idle: bool) -> None:
        if not self.s.metrics_goal:
            return
        path = os.path.join(self.state, "slot-samples.jsonl")
        os.makedirs(self.state, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": self.now, "idle": idle}) + "\n")

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

    @staticmethod
    def _pr_ticket_number(repo: str, pr: dict) -> Optional[int]:
        body = str(pr.get("body") or "")
        ref = re.search(r"^Refs %s#([0-9]+)[ \t]*$" % re.escape(repo), body, re.M)
        head = str((pr.get("head") or {}).get("ref") or "")
        branch = re.match(r"loom/([0-9]+)(?:-|$)", head)
        match = ref or branch
        return int(match.group(1)) if match else None

    @staticmethod
    def _terminal_label(pr: dict) -> Optional[str]:
        # The pulls list endpoint carries merged_at only; merged appears on
        # the single-PR endpoint, so both are accepted.
        if bool(pr.get("merged_at")) or pr.get("merged") is True:
            return "loom:merged"
        if pr.get("state") == "closed":
            return "loom:closed-unmerged"
        return None

    def _reconcile_pr_ticket(self, repo: str, pr: dict, tickets_by_number: dict) -> None:
        number = self._pr_ticket_number(repo, pr)
        ticket = tickets_by_number.get(number) if number is not None else None
        if ticket is None:
            return
        labels = _label_names(ticket)
        if any(state in labels for state in ("loom:merged", "loom:shipped", "loom:verified")):
            return
        target = self._terminal_label(pr)
        if target is None or target in labels:
            return
        state_names = ("queued", "briefed", "running", "pr", "failed", "merged",
                       "shipped", "verified", "closed-unmerged")
        states = {"loom:" + state for state in state_names}
        kept = [name for name in labels if name not in states]
        kept.append(target)
        self.gh.rest("PUT", "/repos/%s/issues/%s/labels" % (repo, ticket["number"]),
                     {"labels": kept})
        ticket["labels"] = [{"name": name} for name in kept]

    def _reconcile_repo(self, repo: str) -> None:
        tickets_by_number = {int(i["number"]): i for i in self._pr_tickets(repo)
                             if "loom:ticket" in _label_names(i)}
        if not tickets_by_number:
            return
        # Only a closed pull request changes a ticket, and only a ticket with
        # no open one: those PRs are the most recently updated closed ones,
        # so one page of them is read, never the repo's whole PR history.
        open_prs = [p for p in self._open_pulls(repo)
                    if isinstance(p, dict) and p.get("state", "open") == "open"]
        waiting = {n: t for n, t in tickets_by_number.items()
                   if not any(_linked_pr(t, repo, p) for p in open_prs)}
        if not waiting:
            return
        closed = self.gh.rest("GET", "/repos/%s/pulls?state=closed&sort=updated"
                              "&direction=desc&per_page=100" % repo).data
        for pr in closed if isinstance(closed, list) else []:
            if isinstance(pr, dict) and pr.get("state") == "closed":
                self._reconcile_pr_ticket(repo, pr, waiting)

    def reconcile_pull_requests(self) -> None:
        """Repair ticket state from the PR's authoritative terminal state.

        A GitHub failure on one repo is logged and the pass moves on; the
        next pass retries, since the repair is idempotent."""
        for repo in self.scan_repos():
            try:
                # Both the issues and pulls fetches are covered by this repo guard.
                self._reconcile_repo(repo)
            except (GitHubError, OSError) as err:
                _say("could not reconcile pull requests for %s: %s" % (repo, err))

    def hand_back(self) -> None:
        """Return completed lane PRs with failed checks or bot findings."""
        rounds = _fix_round_limit()
        for repo in self.scan_repos():
            issues = self._pr_tickets(repo)
            if not issues:
                continue
            prs = self._open_pulls(repo)
            for issue in issues:
                self._hand_back_ticket(repo, issue, prs, rounds)

    def _pr_tickets(self, repo: str) -> List[Dict[str, Any]]:
        issues = self.gh.rest("GET", "/repos/%s/issues?labels=loom:pr&state=open&per_page=100" % repo).data or []
        found = [i for i in issues if isinstance(i, dict) and "pull_request" not in i]
        self._observe(repo, "pr", bool(found))
        return found

    def _open_pulls(self, repo: str) -> List[Dict[str, Any]]:
        """The repo's open pull requests, read once a pass (reconciliation
        and hand-back change tickets, never pull requests). The same URL as
        the factory's read, so one kept answer serves both."""
        if repo not in self._pulls:
            data = self.gh.rest("GET", "/repos/%s/pulls?state=open&per_page=100" % repo).data
            self._pulls[repo] = data if isinstance(data, list) else []
        return self._pulls[repo]

    def _hand_back_ticket(self, repo: str, issue: Dict[str, Any], prs: List[Dict[str, Any]],
                          rounds: int) -> None:
        number = int(issue["number"])
        lane = _lane_of(issue)
        pr = next((p for p in prs if _linked_pr(issue, repo, p)), None)
        if not lane or not pr:
            return
        pr_number = int(pr["number"])
        detail = self.gh.rest("GET", "/repos/%s/pulls/%d" % (repo, pr_number)).data or {}
        sha = str((detail.get("head") or {}).get("sha") or "")
        if not sha:
            return
        comments = self.gh.paginate("/repos/%s/issues/%d/comments" % (repo, number))
        names = _label_names(issue)
        ready = self._ready_marker(comments)
        if self._fix_pending(names, comments, sha):
            return
        self._clear_stale_ready(repo, number, names, ready, sha)
        if "loom:briefed" in names or "loom:running" in names:
            return
        if not self._checks_finished(repo, sha):
            self._remove_ready_if_present(repo, number, names)
            return
        failed = brain._failing_checks(self.gh, repo, sha)
        threads = self._bot_threads(repo, pr_number)
        if not failed and not threads:
            self._mark_ready(repo, number, sha, names, ready)
            return
        self._remove_ready_if_present(repo, number, names)
        self._post_fix_or_escalate(repo, number, pr_number, lane, detail, sha,
                                   comments, names, failed, threads, rounds)

    def _ready_marker(self, comments: List[Any]) -> Optional[Tuple[Any, str]]:
        # The ticket keeps one ready-head comment of ours, edited on each new clean
        # head, so the markers never pile up; returns its id and the head it names.
        authors = {author.lower() for author in self.s.authors}
        pattern = re.compile(r"<!-- loom:ready-head ([0-9a-f]+) -->")
        found = None
        for comment in comments:
            if not isinstance(comment, dict) or tickets._login(comment.get("user")) not in authors:
                continue
            match = pattern.search(str(comment.get("body") or ""))
            if match:
                found = (comment.get("id"), match.group(1))
        return found

    def _fix_pending(self, names: List[str], comments: List[Any], sha: str) -> bool:
        if "loom:briefed" not in names and "loom:running" not in names:
            return False
        authors = {author.lower() for author in self.s.authors}
        marker = "<!-- loom:fix-head %s -->" % sha
        return any(isinstance(comment, dict)
                   and tickets._login(comment.get("user")) in authors
                   and str(comment.get("body") or "").startswith(tickets.FIX_MARKER)
                   and marker in str(comment.get("body") or "") for comment in comments)

    def _clear_stale_ready(self, repo: str, number: int, names: List[str],
                           ready: Optional[Tuple[Any, str]], sha: str) -> None:
        if "loom:ready" in names and (not ready or ready[1] != sha):
            self._remove_label(repo, number, "loom:ready")
            names.remove("loom:ready")

    def _checks_finished(self, repo: str, sha: str) -> bool:
        runs = (self.gh.rest("GET", "/repos/%s/commits/%s/check-runs?per_page=100" % (repo, sha)).data or {}).get("check_runs") or []
        statuses = (self.gh.rest("GET", "/repos/%s/commits/%s/status" % (repo, sha)).data or {}).get("statuses") or []
        return not any(run.get("status") != "completed" for run in runs) and not any(
            status.get("state") == "pending" for status in statuses)

    def _bot_threads(self, repo: str, pr_number: int) -> List[Dict[str, Any]]:
        threads = brain._open_threads(self.gh, repo, pr_number)
        return [thread for thread in threads if self._thread_starts_with_bot(thread)]

    @staticmethod
    def _thread_starts_with_bot(thread: Dict[str, Any]) -> bool:
        comments = (thread.get("comments") or {}).get("nodes") or []
        return bool(comments and
                    (comments[0].get("author") or {}).get("login") == "grug-tribe[bot]")

    def _remove_ready_if_present(self, repo: str, number: int, names: List[str]) -> None:
        if "loom:ready" in names:
            self._remove_label(repo, number, "loom:ready")

    def _mark_ready(self, repo: str, number: int, sha: str, names: List[str],
                    ready: Optional[Tuple[Any, str]]) -> None:
        if "loom:ready" not in names:
            self._add_label(repo, number, "loom:ready")
        body = {"body": "<!-- loom:ready-head %s -->" % sha}
        if not ready:
            self.gh.rest("POST", "/repos/%s/issues/%d/comments" % (repo, number), body)
        elif ready[1] != sha:
            self.gh.rest("PATCH", "/repos/%s/issues/comments/%s" % (repo, ready[0]), body)

    @staticmethod
    def _fix_round_count(comments: List[Any], authors: set) -> int:
        rounds_seen = set()
        for comment in comments:
            if (not isinstance(comment, dict)
                    or tickets._login(comment.get("user")) not in authors):
                continue
            head = Station._fix_head(str(comment.get("body") or ""))
            if head:
                rounds_seen.add(head)
        return len(rounds_seen)

    @staticmethod
    def _fix_head(body: str) -> Optional[str]:
        if not body.startswith(tickets.FIX_MARKER):
            return None
        match = re.search(r"<!-- loom:fix-head ([0-9a-f]+) -->", body)
        return match.group(1) if match else None

    def _post_fix_or_escalate(self, repo: str, number: int, pr_number: int, lane: str,
                              detail: Dict[str, Any], sha: str, comments: List[Any],
                              names: List[str], failed: List[Any], threads: List[Any],
                              rounds: int) -> None:
        authors = {author.lower() for author in self.s.authors}
        if self._fix_round_count(comments, authors) >= rounds:
            self._escalate_fix(repo, number, names)
            return
        self._post_fix_comment(repo, number, pr_number, detail, sha, failed, threads)
        self._move_fix_to_lane(repo, number, lane, names)

    def _escalate_fix(self, repo: str, number: int, names: List[str]) -> None:
        if "needs-brain" not in names:
            self._add_label(repo, number, "needs-brain")

    def _post_fix_comment(self, repo: str, number: int, pr_number: int,
                          detail: Dict[str, Any], sha: str, failed: List[Any],
                          threads: List[Any]) -> None:
        branch = str((detail.get("head") or {}).get("ref") or "")
        body = brain._fix_body(repo, pr_number, branch, "", failed, threads)
        marker = "\n\n<!-- loom:fix-head %s -->\n" % sha
        if len(body) > MAX_FIX_BODY:  # the API rejects comments over 65536 characters
            body = body[:MAX_FIX_BODY].rstrip() + "\n\n(findings truncated)"
        body = body.rstrip() + marker
        self.gh.rest("POST", "/repos/%s/issues/%d/comments" % (repo, number), {"body": body})

    def _move_fix_to_lane(self, repo: str, number: int, lane: str,
                          names: List[str]) -> None:
        labels = [name for name in names if name not in (
            "loom:pr", "loom:ready", "needs-brain", "loom:running", "loom:briefed")
                  and not name.startswith("lane:")]
        labels.extend(("loom:briefed", "lane:" + lane))
        self.gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number), {"labels": labels})

    def _add_label(self, repo: str, number: int, label: str) -> None:
        issue = self._issue(repo, number)
        names = _label_names(issue)
        if label not in names:
            names.append(label)
            self.gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number), {"labels": names})

    def _remove_label(self, repo: str, number: int, label: str) -> None:
        issue = self._issue(repo, number)
        names = [n for n in _label_names(issue) if n != label]
        self.gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number), {"labels": names})

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
        return self._report_failure(repo, number, lane, text, name)

    def _report_failure(self, repo: str, number: int, lane: str, text: str, name: str) -> bool:
        """Report a failed brief, keeping fix rounds on their existing pull request."""
        reason = self._failure_reason(lane, name)
        fix_pr = self._field(text, "Fix-PR")
        klass, bare = failure_class(reason), CLASS_RE.sub("", reason)
        # Thermal-limit briefs carry "limit:" with or without a class and
        # return to the factory. Provider limits have a class but no marker,
        # so they take the bounded retry path below.
        if bare.startswith(LIMIT):
            self._limited(repo, number, fix_pr.isdigit(), lane, bare)
            return True
        if fix_pr.isdigit() and bare.startswith(BRANCH_MOVED):
            self._branch_moved(repo, number, int(fix_pr), lane, bare)
            return True
        if fix_pr.isdigit() and bare.startswith(NO_COMMIT):
            self._post_answers(repo, int(fix_pr), lane, name, text)
            self._nothing_to_fix(repo, number, int(fix_pr), lane, name)
            return True
        if fix_pr:
            # A fresh attempt would open a second pull request for the ticket.
            self._relabel(repo, number, "loom:pr")
            self._comment(repo, number, "Lane %s failed: %s. This was a fix round on pull "
                          "request #%s; the ticket is back at loom:pr and the pull request "
                          "is unchanged." % (lane, reason, fix_pr))
            _say("reported %s#%d fix failed, back to loom:pr: %s" % (repo, number, reason))
            return True
        note = ""
        if klass in RETRY_CLASSES:
            note = self._retry(repo, number, lane, klass, reason)
            if not note:
                return True
            note = ". Not retried: %s." % note
        self._relabel(repo, number, "loom:failed")
        self._comment(repo, number, "Lane %s failed: %s%s" % (lane, reason, note))
        _say("reported %s#%d failed: %s" % (repo, number, reason))
        return True

    def _retry(self, repo: str, number: int, lane: str, klass: str, reason: str) -> str:
        """Brief a running ticket to another lane after a limit or transient
        failure. Its next claim is a new attempt, so a fresh branch. Returns ""
        when it moved, else why not."""
        issue = self._issue(repo, number)
        names = _label_names(issue)
        if RUNNING not in names:
            return "the ticket is no longer running"
        comments = self.gh.paginate("/repos/%s/issues/%d/comments" % (repo, number))
        used, left, pending = retry_chain(comments)
        if pending and pending[0] == lane:
            # An earlier pass commented this retry but did not relabel.
            target = pending[1]
        else:
            if used >= self.s.retry_max:
                return "%d of %d automatic retries used" % (used, self.s.retry_max)
            target = self._retry_lane(repo, issue, lane, klass, left | {lane})
            if target is None:
                return "no other eligible lane is available"
            # The comment comes first: it is the count of retries, and it tells
            # a pass that finds the relabel undone where the ticket was going.
            self._comment(repo, number, "Automatic retry %d of %d (%s): moved from lane %s "
                          "to lane %s. Lane %s failed: %s"
                          % (used + 1, self.s.retry_max, klass, lane, target, lane, reason))
        labels = [n for n in names if n != RUNNING and not n.startswith("lane:")]
        labels += [BRIEFED, "lane:" + target]
        self.gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number), {"labels": labels})
        _say("retrying %s#%d on lane %s after a %s failure on lane %s"
             % (repo, number, target, klass, lane))
        return ""

    def _retry_lane(self, repo: str, issue: Dict[str, Any], failed: str, klass: str,
                    skip: set) -> Optional[str]:
        """The next eligible lane: another slot of the failed lane's provider
        (not after a limit, which the slots share), then RETRY_ORDER, then free
        lanes for an easy ticket. Only available lanes whose runner takes the
        ticket's model and repo; an idle one before a busy one. Candidate
        eligibility is kept in _retry_eligible so this method selects by order."""
        try:
            headers = tickets.parse(issue.get("body"))[0]
        except ValueError as err:
            # Without Model and Difficulty no lane can be judged eligible, so
            # guessing would send the brief to a lane that refuses it.
            _say("not retrying %s#%s: its body does not parse: %s"
                 % (repo, issue.get("number"), err))
            return None
        model = headers.get("Model", "")
        order = self._retry_order(failed, klass, headers.get("Difficulty"))
        public: Dict[str, bool] = {}
        busy = None
        for cand in dict.fromkeys(order):
            if cand in skip or not self._retry_eligible(repo, cand, failed, klass, model, public):
                continue
            if self._inbox_empty(cand):
                return cand
            busy = busy or cand
        return busy

    def _retry_order(self, failed: str, klass: str, difficulty: Optional[str]) -> List[str]:
        same = [lane for lane in LANES if provider(lane) == provider(failed)]
        order = ([] if klass == "limit" else same) + RETRY_ORDER
        if difficulty == "easy":
            order += FREE_LANES
        return order

    def _retry_eligible(self, repo: str, cand: str, failed: str, klass: str, model: str,
                        public: Dict[str, bool]) -> bool:
        """Whether lane cand can take a retry: a known, available lane, not
        the failed lane's provider after a limit, whose runner takes the
        model and, for a public-only lane, a public repo. public caches the
        repo lookup across candidates."""
        if cand not in LANES or not self._heartbeat_ok(cand):
            return False
        if klass == "limit" and provider(cand) == provider(failed):
            return False
        if model and not lane_takes_model(cand, model):
            return False
        if needs_public_repo(cand, model):
            if repo not in public:
                public[repo] = self._public(repo)
            return public[repo]
        return True

    def _public(self, repo: str) -> bool:
        try:
            data = self.gh.rest("GET", "/repos/%s" % repo).data
        except GitHubError as err:
            _say("could not read whether %s is public: %s" % (repo, err))
            return False
        return isinstance(data, dict) and data.get("private") is False

    def _limited(self, repo: str, number: int, fix: bool, lane: str, reason: str) -> None:
        """A run the lane's provider refused (a limit, not a failure). The
        ticket goes back to briefed, where the factory moves it to the next
        lane; a fix brief's goes back to loom:pr, where the factory sends its
        pull request back again."""
        self._relabel(repo, number, "loom:pr" if fix else BRIEFED)
        self._comment(repo, number, "Lane %s is unavailable (%s). This is not a failure: "
                      "the ticket goes to the next lane." % (lane, reason))
        _say("reported %s#%d limited on lane %s: %s" % (repo, number, lane, reason))

    def _branch_moved(self, repo: str, number: int, pr: int, lane: str, reason: str) -> None:
        """A fix brief whose push was refused because the pull request's
        branch moved during the run. The model did nothing wrong, so no rung
        of the ladder is spent: the ticket returns to loom:pr, where the
        factory judges the new head and sends it back if it still needs a fix."""
        self._relabel(repo, number, "loom:pr")
        self._comment(repo, number, "Lane %s did not push its fix to #%d: the branch moved "
                      "during the run (%s). This is not a failure: the new head goes back "
                      "to review." % (lane, pr, reason))
        _say("reported %s#%d: branch moved on lane %s: %s" % (repo, number, lane, reason))

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
        try:
            for entry in entries:
                m = ANSWER_FILE_RE.match(entry)
                if not (m and m.group(1) in asked):
                    continue
                try:
                    self._answer(repo, pr, lane, m.group(1), os.path.join(folder, entry), strong)
                except GitHubError as err:
                    # One thread's failure must not stop the rest, and the
                    # report must still be recorded: a retried report would
                    # post the answers already posted a second time.
                    _say("answer to %s on %s#%d failed: %s" % (m.group(1), repo, pr, err))
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def _answer(self, repo: str, pr: int, lane: str, thread_id: str, path: str,
                strong: bool) -> None:
        said = _read_answer(path)
        node = (self.gh.graphql(THREAD_QUERY, {"id": thread_id}) or {}).get("node") or {}
        if not said or not _open_on(node, repo, pr):
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
            try:
                self.gh.graphql(RESOLVE_MUTATION, {"id": thread_id})
            except GitHubError as err:
                # The reply is posted; an open thread is read by the factory
                # as a finding that survived, which is the safe side.
                settled = False
                _say("could not resolve %s on %s#%d: %s" % (thread_id, repo, pr, err))
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
        # A conditional read, like _poll.
        items = self.gh.rest(
            "GET", "/repos/%s/issues?labels=%s&state=open&per_page=100" % (repo, NEEDS_OPERATOR)).data
        items = items if isinstance(items, list) else []
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

    def _gather_human(self) -> Optional[List[Tuple[str, Dict[str, Any]]]]:
        """(repo, issue) for every open loom:human issue, newest first; None
        when any repo could not be read (the digest waits for a whole list)."""
        found: List[Tuple[str, Dict[str, Any]]] = []
        for repo in self.s.repos:
            if not tickets.REPO_RE.fullmatch(repo):
                continue
            try:
                items = self.gh.paginate(
                    "/repos/%s/issues?labels=%s&state=open&per_page=100" % (repo, HUMAN))
            except GitHubError as err:
                _say("could not list %s issues in %s: %s" % (HUMAN, repo, err))
                return None
            found += [(repo, i) for i in items if isinstance(i, dict)
                      and "pull_request" not in i and i.get("html_url") and "number" in i]
        found.sort(key=lambda f: (str(f[1].get("created_at") or ""), int(f[1]["number"])),
                   reverse=True)
        return found

    def _human_ask(self, repo: str, issue: Dict[str, Any]) -> str:
        """The first line of the newest loom:human comment from an allowed
        login (the brain's ask), or "" when there is none."""
        allowed = {a.lower() for a in self.s.authors}
        # paginate follows the next links, so the newest comment is read however
        # long the thread is. Only the HUMAN_SHOWN listed issues are read, once a
        # day when the digest is due. A GitHubError is left to _human_text,
        # which holds the whole digest rather than send a list missing an ask.
        comments = self.gh.paginate("/repos/%s/issues/%d/comments?per_page=100"
                                    % (repo, int(issue["number"])))
        for c in reversed(comments):
            body = str(c.get("body") or "") if isinstance(c, dict) else ""
            if body.startswith(HUMAN_MARKER) and tickets._login(c.get("user")) in allowed:
                lines = [ln.strip() for ln in body[len(HUMAN_MARKER):].splitlines() if ln.strip()]
                return _clip(lines[0], HUMAN_ASK_MAX) if lines else ""
        return ""

    def _human_text(self, found: List[Tuple[str, Dict[str, Any]]]) -> Optional[str]:
        """The digest's "Waiting on you" section: the newest HUMAN_SHOWN
        issues grouped by repo, each with its ask; None when it cannot be read."""
        shown = found[:HUMAN_SHOWN]
        head = "%s (%d):" % (HUMAN_HEAD, len(found))
        if len(found) > len(shown):
            head = "%s (the %d newest of %d):" % (HUMAN_HEAD, len(shown), len(found))
        lines = [head]
        for repo in dict.fromkeys(r for r, _ in shown):
            lines.append(repo)
            for r, issue in shown:
                if r != repo:
                    continue
                title = _clip(" ".join(str(issue.get("title") or "").split()), HUMAN_TITLE_MAX)
                lines.append("- %s %s" % (title, issue["html_url"]))
                try:
                    ask = self._human_ask(repo, issue)
                except GitHubError as err:
                    _say("could not read the ask on %s#%s: %s" % (repo, issue["number"], err))
                    return None
                if ask:
                    lines.append("  %s" % ask)
        return "\n".join(lines)

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
        # A loom:human issue waits in the digest's own list, never alone.
        asks = [a for a in asks if HUMAN not in _label_names(a[1])]
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

    def _send_urgent(self, url: str, todo: List[Tuple[str, Dict[str, Any]]],
                     ledger: Tuple[str, Dict[str, Any], List[str]]) -> None:
        path, sent, urgent_sent = ledger
        for key, issue in todo:
            title = " ".join(str(issue.get("title") or "").split())
            if self._send(url, "Urgent ask: %s %s" % (title, issue["html_url"])):
                urgent_sent.append(key)
                sent["urgent"] = urgent_sent
                _write_json(path, sent)

    @staticmethod
    def _ask_rev(issue: Dict[str, Any]) -> str:
        """The revision of an ask: its title and body as the operator reads them."""
        return _sha12("%s\n%s" % (issue.get("title") or "", issue.get("body") or ""))

    def _plan_digest(self, asks: List[Tuple[str, str, Dict[str, Any]]],
                     covered_before: Any, today: str) -> Dict[str, Any]:
        """Today's digest, fixed before anything is sent so a retry sends the
        same one. Asks new or changed since the last sent digest go first,
        newest first, so they are not buried behind old ones."""
        prev = covered_before if isinstance(covered_before, dict) else {}
        covered = {k: self._ask_rev(i) for k, _, i in asks}
        new = [a for a in reversed(asks) if prev.get(a[0]) != covered[a[0]]]
        seen = [a for a in asks if prev.get(a[0]) == covered[a[0]]]
        plan: Dict[str, Any] = {
            "day": today, "covered": covered, "new": len(new),
            "lines": [self._line(i) for _, _, i in new + seen], "pages": {}}
        whole = "\n".join([self._digest_head(plan)] + plan["lines"])
        if len(whole) <= DISCORD_MAX - MENTION_ROOM:
            return plan
        repos: List[str] = []
        for _, repo, _ in asks:
            if repo not in repos:
                repos.append(repo)
        plan["pages"] = {repo: {"body": self._page_body(
            repo, [i for _, r, i in asks if r == repo], today), "url": ""}
            for repo in repos}
        plan["order"] = repos
        return plan

    def _page_body(self, repo: str, issues: List[Dict[str, Any]], today: str) -> str:
        """Every ask in one repo, grouped under the goal home its body names."""
        # _goal_issues keeps only objects with numeric issue numbers; a read
        # failure reaches _send_digest as GitHubError before any page is posted.
        goals = {int(g["number"]): g for g in self._goal_issues(repo)}
        groups: Dict[Optional[int], List[Dict[str, Any]]] = {}
        for issue in issues:
            refs = [int(n) for n in ASK_GOAL_REF_RE.findall(str(issue.get("body") or ""))]
            goal = next((n for n in refs if n in goals), None)
            groups.setdefault(goal, []).append(issue)
        out = [DIGEST_MARKER, "Asks waiting for the operator on %s: %d in this repo."
               % (today, len(issues))]
        for goal in sorted(groups, key=lambda g: (g is None, g or 0)):
            if goal is None:
                head = "Not filed under a goal"
            else:
                head = "%s (#%d)" % (" ".join(str(goals[goal].get("title") or "").split()), goal)
            out += ["", "## " + head.replace("@", "@\u200b"), ""]
            out += ["- #%d %s" % (int(i["number"]),
                                  " ".join(str(i.get("title") or "").split()).replace("@", "@\u200b"))
                    for i in groups[goal]]
        return "\n".join(out)

    @staticmethod
    def _digest_head(plan: Dict[str, Any]) -> str:
        return "%s %d, %d new since the last digest" % (DIGEST_HEAD, len(plan["lines"]), plan["new"])

    def _digest_text(self, plan: Dict[str, Any]) -> str:
        """The daily notification: as many asks as fit, then a link per repo
        to the full digest when some do not."""
        lines = plan["lines"]
        head = self._digest_head(plan)
        pages = plan.get("pages") or {}
        tail = ["Every ask, by goal:"] + [
            "- %s %s" % (repo, pages[repo].get("url") or "")
            for repo in plan.get("order") or []] if pages else []
        room = DISCORD_MAX - MENTION_ROOM - len(head) - sum(len(t) + 1 for t in tail)
        keep: List[str] = []
        size = len("- and 9999 more") + 1 if pages else 0
        for line in lines:
            size += len(line) + 1
            if size > room:
                break
            keep.append(line)
        if len(keep) < len(lines):
            keep.append("- and %d more" % (len(lines) - len(keep)))
        asks = "\n".join([head] + keep + tail) if lines else ""
        return "\n\n".join(part for part in (asks, plan.get("human") or "") if part)

    def _digest_issue(self, repo: str) -> int:
        found = self.gh.paginate("/repos/%s/issues?labels=%s&state=open&per_page=100"
                                 % (repo, DIGEST_LABEL))
        numbers = sorted(int(i["number"]) for i in found
                         if isinstance(i, dict) and "pull_request" not in i and "number" in i)
        if numbers:
            return numbers[0]
        made = self.gh.rest("POST", "/repos/%s/issues" % repo, {
            "title": DIGEST_TITLE, "labels": [DIGEST_LABEL],
            "body": "The station posts the full daily digest of asks here when "
                    "they do not all fit in the operator's notification."}).data
        if not isinstance(made, dict) or "number" not in made:
            raise GitHubError("no issue was created in %s" % repo)
        return int(made["number"])

    def _post_pages(self, plan: Dict[str, Any], ledger: Tuple[str, Dict[str, Any], List[str]]
                    ) -> bool:
        """Post each repo's full digest once; the link is kept in the plan, so
        a retry reuses a page already posted."""
        path, sent, _ = ledger
        for repo in plan.get("order") or []:
            page = plan["pages"][repo]
            if page.get("url"):
                continue
            try:
                number = self._digest_issue(repo)
                made = self.gh.rest("POST", "/repos/%s/issues/%d/comments" % (repo, number),
                                    {"body": page["body"]}).data
            except GitHubError as err:
                # Leave the fixed plan pending; forward_asks has already sent urgent asks.
                _say("daily digest pending: could not post its page in %s: %s; next pass"
                     % (repo, err))
                return False
            # A successful HTTP status can still carry an unusable reply.
            url = made.get("html_url") if isinstance(made, dict) else None
            if not url:
                _say("no digest page link from %s; next pass" % repo)
                return False
            page["url"] = str(url)
            _write_json(path, sent)
        return True

    def _new_digest_plan(self, keyed: List[Tuple[str, str, Dict[str, Any]]],
                         covered: Any, today: str) -> Optional[Dict[str, Any]]:
        """Build the day's fixed plan after every ask and human item can be read."""
        trust = self._check_asks(keyed, {k for k, _, _ in keyed})
        if trust is None:
            return None
        asks = [a for a in keyed if trust.get(a[0])]
        found = self._gather_human()
        if found is None:
            return None
        human = self._human_text(found) if found else ""
        if human is None or (not asks and not human):
            return None
        try:
            plan = self._plan_digest(asks, covered, today)
        except GitHubError as err:
            _say("could not read the goal homes for the digest: %s; next pass" % err)
            return None
        plan["human"] = human
        return plan

    def _send_digest(self, url: Optional[str], keyed: List[Tuple[str, str, Dict[str, Any]]],
                     ledger: Tuple[str, Dict[str, Any], List[str]], today: str) -> None:
        path, sent, _ = ledger
        plan = sent.get("pending")
        if not (isinstance(plan, dict) and plan.get("day") == today):
            plan = self._new_digest_plan(keyed, sent.get("covered"), today)
            if plan is None:
                return
            sent["pending"] = plan
            _write_json(path, sent)
        if url is None:
            url = self._webhook()
        if url is None or not self._post_pages(plan, ledger):
            return
        chunks = self._chunks(self._digest_text(plan), DISCORD_MAX - MENTION_ROOM)
        if all(self._send(url, chunk, mention=n == 0) for n, chunk in enumerate(chunks)):
            # What the operator was sent: each ask's revision and the links.
            sent["digest"] = today
            sent["covered"] = plan["covered"]
            sent["pages"] = {r: p["url"] for r, p in (plan.get("pages") or {}).items()}
            del sent["pending"]
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
        url = None
        # Urgent asks go out on their own: nothing the digest needs (a trust
        # check of an old ask, a page post) can hold them.
        trust = self._check_asks(keyed, urgent_keys) if urgent_keys else {}
        todo = [(k, i) for k, _, i in keyed if k in urgent_keys and (trust or {}).get(k)]
        if todo:
            url = self._webhook()
            if url is None:
                return
            self._send_urgent(url, todo, ledger)
        if digest_due:
            self._send_digest(url, keyed, ledger, today)

    # -- goal reports -----------------------------------------------------
    def _goal_issues(self, repo: str) -> List[Dict[str, Any]]:
        data = self.gh.paginate(
            "/repos/%s/issues?labels=loom:goal&state=open&per_page=100" % repo)
        return [i for i in (data if isinstance(data, list) else [])
                if isinstance(i, dict) and "pull_request" not in i
                and str(i.get("number")).isdigit()]

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
    def _chunks(text: str, size: int = DISCORD_MAX) -> List[str]:
        """The text in Discord-sized messages, cut at line breaks."""
        parts = []
        while len(text) > size:
            cut = text.rfind("\n", 0, size + 1)
            if cut <= 0:
                cut = size
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
        if where:
            # The line already shows the report's first line; the thread
            # starts after it, so Discord's preview never repeats it.
            body = _after_first_line(body)
        else:
            # No thread: the full report goes in the channel, so it is never
            # lost behind a line that does not expand.
            _say("no thread for the report line; the full report goes in the channel")
            where = chan
        link = self._report_link(issue, comment)
        for part in self._chunks((body + "\n" + link if body else link) if link else body):
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
            sent = False
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
                sent = True
            ledger[key] = ident
            last_id = ident
            if sent:
                # The report is out: persist the position before the next
                # comment, so a pass killed here (the installer restarted the
                # station mid-pass, 2026-10-13) re-sends nothing already sent.
                # The whole file is written atomically by _write_json.
                _write_json(path, ledger)
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

    # -- backlog triage --------------------------------------------------
    def _backlog_scan_due(self, state: Dict[str, Any]) -> bool:
        scanned = state.get("_scanned")
        return (not isinstance(scanned, (int, float)) or isinstance(scanned, bool)
                or not 0 <= self.now - scanned < TRIAGE_INTERVAL)

    def _backlog_goals(self, state: Dict[str, Any]
                       ) -> Optional[Dict[str, Tuple[str, int, Any]]]:
        """Backlog goal homes and their last triage time. The list is fetched
        once an hour; stale cache entries are dropped from *state*. Returns
        None when the list cannot be read, after logging the failure."""
        if not self._backlog_scan_due(state):
            goals: Dict[str, Tuple[str, int, Any]] = {}
            for key, last in state.items():
                if key == "_scanned":
                    continue
                repo, num = key.rsplit("#", 1)
                goals[key] = (repo, int(num), last)
            return goals
        try:
            homes = self._goal_homes()
        except (GitHubError, OSError) as err:
            _say("could not list goal homes for triage: %s" % err)
            return None
        goals = {}
        keep = {"_scanned"}
        for repo, goal in homes:
            title = str(goal.get("title") or "")
            if not title.startswith("Backlog:"):
                continue
            number = int(goal["number"])
            key = "%s#%d" % (repo, number)
            keep.add(key)
            goals[key] = (repo, number, state.get(key))
        for stale in [k for k in state if k not in keep]:
            del state[stale]
        state["_scanned"] = self.now
        return goals

    def _triage_recent(self, last: Any) -> bool:
        return (isinstance(last, (int, float)) and not isinstance(last, bool)
                and 0 <= self.now - last < TRIAGE_INTERVAL)

    def _run_triage(self, key: str, repo: str, number: int) -> None:
        lines = brain.triage(self.gh, repo, number, brain.TRIAGE_LIMIT)
        queued = sum(1 for line in lines if " queued under #" in line)
        triage_count = sum(1 for line in lines if " loom:triage" in line)
        held = sum(1 for line in lines if " held:" in line)
        _say("triage %s: %d queued, %d triage, %d held" % (
            key, queued, triage_count, held))

    def triage_backlogs(self) -> None:
        """Run brain.triage once an hour on every backlog goal home.

        A backlog goal is the goal home onboard opens: its title starts with
        "Backlog:". The last run time per goal is kept in triage.json so a
        restart does not repeat a run. Goal homes are re-read at most once an
        hour, so a pass that does no triage costs no extra GitHub calls."""
        path = os.path.join(self.state, "triage.json")
        state = _read_json(path, {}, strict=False)
        if not isinstance(state, dict):
            state = {}
        goals = self._backlog_goals(state)
        if goals is None:
            return
        for key, (repo, number, last) in goals.items():
            if self._triage_recent(last):
                continue
            try:
                self._run_triage(key, repo, number)
            except (GitHubError, OSError, ValueError) as err:
                _say("triage %s failed: %s" % (key, err))
                continue
            state[key] = self.now
        _write_json(path, state)

    # -- transcript uploads ---------------------------------------------
    def _transcript_runs(self) -> List[Run]:
        """(ledger key, brain tick start or None, body builder) for every
        finished run: each brief in a slot's done/ or failed/, and the last
        brain tick once state/brain.json says it ended (ok or failed, with
        the start time brain.sh records)."""
        home = Path(self.s.home)
        runs: List[Run] = []
        for lane, kind, path, name in self._finished_briefs():
            def body(lane=lane, kind=kind, path=path, name=name) -> Dict[str, Any]:
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
                return transcripts.run_body(home, lane, name, kind, self._field(text, "Model"),
                                            self.claude_projects, self.codex_sessions,
                                            issue=self._field(text, "Issue"))
            runs.append(("%s/%s" % (lane, name), None, body))
        tick = self._brain()
        last = tick[0] if tick else {}
        started, at = last.get("started"), last.get("at")
        if (last.get("outcome") in ("ok", "failed")
                and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                        for v in (started, at))):
            def brain_body() -> Dict[str, Any]:
                return transcripts.brain_body(home, started, at, last["outcome"],
                                              str(last.get("model") or ""), self.claude_projects)
            runs.append(("brain", float(started), brain_body))
        return runs

    @staticmethod
    def _uploaded(ledger: Dict[str, Any], key: str, tick: Optional[float]) -> Any:
        """The ledger's record of a run: "sent", "old", "gave up", or the
        number of failed tries so far. The brain keeps one entry, its last
        tick's."""
        rec = ledger.get(key)
        if tick is None:
            return rec if rec is not None else 0
        if isinstance(rec, dict) and rec.get("started") == tick:
            return rec.get("result", 0)
        return 0

    @staticmethod
    def _record(ledger: Dict[str, Any], key: str, tick: Optional[float], result: Any) -> None:
        ledger[key] = result if tick is None else {"started": tick, "result": result}

    def _transcript_token(self) -> Optional[str]:
        try:
            return self.read_secret(self.s.transcript_token_ssm, self.s.region)
        # Deliberately broad, as in _webhook: an error text must never carry
        # the secret; only the type is logged.
        except Exception as err:  # noqa: BLE001
            _say("could not read the transcript token parameter (%s); next pass"
                 % type(err).__name__)
            return None

    def upload_transcripts(self) -> None:
        """Upload each finished run's transcript to the dashboard once. A
        failed upload is tried again on the next pass, UPLOAD_TRIES times in
        all. The first pass records the runs already finished as old: only
        runs that finish after uploads are turned on are sent."""
        if not (self.s.transcript_url and self.s.transcript_token_ssm):
            return
        path = os.path.join(self.state, "transcripts.json")
        ledger = _read_json(path, None)
        first = ledger is None
        if first:
            ledger = {}
        if not isinstance(ledger, dict):
            raise StateError("state file %s does not hold an object" % path)
        token: Optional[str] = None
        sent = 0
        for key, tick, build in self._transcript_runs():
            rec = self._uploaded(ledger, key, tick)
            if first:
                self._record(ledger, key, tick, "old")
                continue
            if not isinstance(rec, int) or isinstance(rec, bool) or sent >= UPLOAD_PER_PASS:
                continue
            if token is None:
                token = self._transcript_token()
                if token is None:
                    return
            try:
                body = build()
            except ValueError as err:
                # A run with no ticket (a brief from before tickets) can never
                # be filed; skip it for good and keep going.
                _say("skipped the transcript of %s: %s" % (key, err))
                self._record(ledger, key, tick, "no ticket")
                _write_json(path, ledger)
                continue
            try:
                self.upload(self.s.transcript_url, token, body)
                result: Any = "sent"
                sent += 1
            except UploadRefused as err:
                # The dashboard is up and refused this body: give it up now and
                # send the next, so one bad run never blocks the rest.
                _say("the dashboard refused the transcript of %s (%d); given up"
                     % (key, err.status))
                self._record(ledger, key, tick, "refused")
                _write_json(path, ledger)
                continue
            # Deliberately broad, as in _send: an error text can carry the URL
            # or the token; only the type is logged, once, when giving up.
            except Exception as err:  # noqa: BLE001
                result = rec + 1
                if result >= UPLOAD_TRIES:
                    _say("gave up uploading the transcript of %s after %d tries (%s)"
                         % (key, UPLOAD_TRIES, type(err).__name__))
                    result = "gave up"
            self._record(ledger, key, tick, result)
            _write_json(path, ledger)
            if result != "sent":
                return
        if first:
            _write_json(path, ledger)

    def post_overdue_standups(self) -> None:
        """Post a data-only report when an open goal has gone overdue."""
        now = brain.datetime.fromtimestamp(self.now, brain.timezone.utc)
        authors = [a.lower() for a in self.s.authors]
        state_path = os.path.join(self.state, "standups.json")
        state = _read_json(state_path, {})
        if not isinstance(state, dict):
            raise StateError("state file %s does not hold an object" % state_path)
        checked = state.get("_checked")
        if isinstance(checked, (int, float)) and not isinstance(checked, bool) \
                and 0 <= self.now - checked < REPORT_CHECK:
            return
        state["_checked"] = self.now
        for repo, goal in self._goal_homes():
            self._post_overdue_goal_standup(repo, goal, state, authors, now)
        _write_json(state_path, state)

    def _post_overdue_goal_standup(self, repo: str, goal: Dict[str, Any],
                                   state: Dict[str, Any], authors: List[str],
                                   now: Any) -> None:
        number = int(goal["number"])
        key = "%s#%d" % (repo, number)
        sent = state.get(key)
        if isinstance(sent, (int, float)) and not isinstance(sent, bool) \
                and 0 <= self.now - sent < brain.REPORT_EVERY:
            return
        comments = self.gh.paginate("/repos/%s/issues/%d/comments?per_page=100" %
                                    (repo, number))
        node = {"comments": {"nodes": [
            {"body": c.get("body"), "createdAt": c.get("created_at"),
             "author": {"login": tickets._login(c.get("user"))}}
            for c in comments if isinstance(c, dict)]}}
        ledger = brain.goal_ledger(goal, node, now, authors)
        if ledger["state"] != "overdue":
            return
        children = self.gh.paginate("/repos/%s/issues/%d/sub_issues?per_page=100" %
                                    (repo, number))
        body = self._overdue_standup_body(children, self._epoch(str(ledger.get("report") or "")))
        brain._check_written_text(body)
        self.gh.rest("POST", "/repos/%s/issues/%d/comments" % (repo, number),
                     {"body": body})
        state[key] = self.now

    def _overdue_standup_body(self, children: List[Any], since: Optional[float] = None) -> str:
        groups = self._standup_ticket_groups(children, since)
        rows = self._standup_rows
        counts = [(len(groups[k]), word) for k, word in (
            ("merged", "merged"), ("verified", "verified"), ("sent_back", "shipped"),
            ("failed", "failed"), ("aged_pr", "stuck"), ("in_flight", "in flight"),
            ("asks", "waiting on you"))]
        summary = ", ".join("%d %s" % (n, word) for n, word in counts if n)
        first = ("%s since the last report." % summary) if summary else "Nothing moved since the last report."
        return "\n".join([REPORT_MARKER, first,
            "", "### Going well", "Merged: %s. Verified: %s. Shipped: %s." %
            (rows(groups["merged"]), rows(groups["verified"]),
             rows(groups["sent_back"])), "",
            "### Not going well", "Failed: %s." % rows(groups["failed"]), "",
            "### Still stuck", "Past the stuck age in PR: %s." %
            rows(groups["aged_pr"]), "",
            "### Next", "In flight: %s." % rows(groups["in_flight"]),
            "Needs difficulty: %s." % rows(groups["needs_difficulty"]), "",
            "### Needs you", "Open asks: %s." % rows(groups["asks"]), "",
            "Written by the station from ticket data, because no brain reported in time."])

    def _standup_ticket_groups(self, children: List[Any], since: Optional[float] = None
                               ) -> Dict[str, List[Dict[str, Any]]]:
        """Tickets by standup heading. Merged, verified and shipped count only
        tickets updated since the last report, so a standup says what changed."""
        groups: Dict[str, List[Dict[str, Any]]] = {key: [] for key in (
            "merged", "verified", "sent_back", "failed", "aged_pr", "in_flight",
            "needs_difficulty", "asks")}
        for issue in children:
            if isinstance(issue, dict):
                for group in self._standup_groups_of(issue, since):
                    groups[group].append(issue)
        return groups

    def _standup_groups_of(self, issue: Dict[str, Any], since: Optional[float]) -> List[str]:
        """The standup headings one ticket belongs under."""
        labels = {label.get("name") for label in issue.get("labels", [])}
        updated = self._epoch(str(issue.get("updated_at") or ""))
        recent = since is None or (updated is not None and updated >= since)
        found = [group for label, group in (("loom:merged", "merged"),
                                            ("loom:verified", "verified"),
                                            ("loom:shipped", "sent_back"))
                 if label in labels and recent]
        if issue.get("state") != "open" or "loom:ticket" not in labels:
            return found
        created = self._epoch(str(issue.get("created_at") or ""))
        aged = created is not None and self.now - created > factory.STUCK_AGE
        for group, hit in (("failed", "loom:failed" in labels),
                           ("aged_pr", "loom:pr" in labels and aged),
                           ("in_flight", bool(labels & {"loom:queued", "loom:briefed",
                                                        "loom:running", "loom:pr"})),
                           ("needs_difficulty", "loom:queued" in labels and
                            not factory.tier_lanes(issue, False)),
                           ("asks", bool(labels & {NEEDS_OPERATOR, HUMAN}))):
            if hit:
                found.append(group)
        return found

    @staticmethod
    def _standup_rows(items: List[Dict[str, Any]]) -> str:
        return ", ".join("#%s %s" % (issue.get("number"),
            " ".join(str(issue.get("title") or "").split())) for issue in items) or "none"

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
        self._factory().run()

    def assign_queued(self) -> None:
        """Give ready queued tickets a free lane after this pass's claims."""
        if self.s.factory_on:
            self._factory().assign_queued()

    def _factory(self) -> factory.Factory:
        repos = [r for r in self.s.repos if tickets.REPO_RE.fullmatch(r)]
        return factory.Factory(self.gh, repos, self.now, trusted=self.s.authors,
                               review_check=self.s.review_check,
                               review_bot=self.s.review_bot,
                               limited=self.limited_lanes(),
                               no_test_repos=self.s.no_test_repos,
                               available=[x for x in LANES if self._heartbeat_ok(x)])

    def watch_default_branches(self) -> None:
        """Revert a lane merge that turned a default branch red (bus/revert.py),
        every pass, with the factory: LOOM_FACTORY=0 turns both off."""
        if not self.s.factory_on:
            return
        repos = [r for r in self.s.repos if tickets.REPO_RE.fullmatch(r)]
        revert.Reverts(self.gh, repos, self.now, trusted=self.s.authors,
                       review_check=self.s.review_check, lanes=self._factory,
                       state_dir=self.state).run()

    def _dated_metrics(self, path: str) -> Dict[str, Tuple[int, int]]:
        dated = {}
        allowed = {author.lower() for author in self.s.authors}
        for comment in self.gh.paginate(path):
            body = str(comment.get("body") or "") if isinstance(comment, dict) else ""
            author = str((comment.get("user") or {}).get("login") or "").lower()
            if author not in allowed:
                continue
            match = re.match(r"<!-- loom:metrics v1 -->\n(\d{4}-\d{2}-\d{2}): (\d)/9 pass; streak (\d+)", body)
            if match:
                dated[match.group(1)] = (int(match.group(2)), int(match.group(3)))
        return dated

    def _post_metrics_comment(self, path: str, today: str,
                              dated: Dict[str, Tuple[int, int]]) -> None:
        try:
            numbers = metrics.Collector(self.gh, self.s.repos, self.s.home,
                                        self.now, self.s.review_check,
                                        self.s.authors).collect()
        except (GitHubError, OSError) as err:
            _say("metrics collection failed: %s" % err)
            self._metrics_notice(today, "failure",
                                 "Loom metrics %s: collection failed; retrying." % today)
            return
        passed = sum(value.passed for value in numbers)
        yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
        streak = dated.get(yesterday, (0, 0))[1] + 1 if passed == 9 else 0
        body = metrics.render(numbers, today, streak)
        # Outside the try above: a refused POST reaches run_pass, which logs
        # "post_metrics failed", and today stays unposted for the next pass.
        self.gh.rest("POST", path, {"body": body})
        dated[today] = (passed, streak)

    def _metrics_notice(self, today: str, key: str, line: str) -> None:
        ledger_path = os.path.join(self.state, "metrics.json")
        ledger = _read_json(ledger_path, {})
        if not isinstance(ledger, dict):
            raise StateError("metrics ledger is not an object")
        if ledger.get(key) == today:
            return
        if self._bot_reports():
            token = self._bot_token()
            if token is None:
                return
            ok, _ = self._bot(token, "POST", "/channels/%s/messages" % self.s.channel_id,
                              {"content": line, "allowed_mentions": {"parse": []}})
        elif self.s.webhook_ssm:
            url = self._webhook()
            if url is None:
                return
            ok = self._send(url, line, mention=False)
        else:
            return
        if ok:
            ledger[key] = today
            _write_json(ledger_path, ledger)

    def post_metrics(self) -> None:
        ref = re.fullmatch(r"([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)#([1-9][0-9]*)",
                           self.s.metrics_goal)
        if not ref or ref.group(1) not in self.s.repos:
            return
        today = time.strftime("%Y-%m-%d", time.localtime(self.now))
        if time.localtime(self.now).tm_hour < self.s.digest_hour:
            return
        path = "/repos/%s/issues/%d/comments" % (ref.group(1), int(ref.group(2)))
        # Read-then-post is safe: run() holds station.lock (flock) for the
        # whole pass, so no second pass can read the same day's comments
        # before this one has posted.
        dated = self._dated_metrics(path)
        if today not in dated:
            self._post_metrics_comment(path, today, dated)
        if today in dated:
            passed, streak = dated[today]
            line = "Loom metrics %s: %d/9 pass; streak %d/7." % (today, passed, streak)
            self._metrics_notice(today, "discord", line)

    def prune_trees(self) -> None:
        """Remove lane trees whose brief is no longer in a lane inbox (waiting
        or running) and that have not changed for TREE_KEEP seconds. A
        finished run's log stays in logs/; its tree is a clone the next
        attempt makes again."""
        root = os.path.join(self.s.home, "worktrees")
        try:
            names = os.listdir(root)
        except FileNotFoundError:
            return
        except OSError as err:
            _say("cannot list %s to remove finished lane trees: %s" % (root, err))
            return
        live = set()
        for lane in LANES:
            try:
                live |= {e[:-3] for e in os.listdir(self.inbox(lane)) if e.endswith(".md")}
            except FileNotFoundError:
                continue
            except OSError as err:
                # An inbox that cannot be read may hold a running brief:
                # remove nothing rather than a tree still in use.
                _say("cannot list lane %s's inbox, so no lane tree is removed: %s" % (lane, err))
                return
        removed = 0
        for name in names:
            path = os.path.join(root, name)
            try:
                st = os.lstat(path)
            except FileNotFoundError:
                continue
            except OSError as err:
                _say("cannot stat lane tree %s: %s" % (name, err))
                continue
            if name in live or self.now - st.st_mtime < TREE_KEEP:
                continue
            if stat.S_ISDIR(st.st_mode):
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.unlink(path)
            removed += 1
        if removed:
            _say("removed %d finished lane tree(s)" % removed)

    # -- the GitHub budget --------------------------------------------------
    def _budget_state(self) -> Dict[str, Any]:
        if self._budget is None:
            saved = _read_json(os.path.join(self.state, "budget.json"), {}, strict=False)
            self._budget = saved if isinstance(saved, dict) else {}
        return self._budget

    def _rates(self) -> Dict[str, Tuple[int, int, int]]:
        """resource -> (remaining, limit, reset) for every rate limit window
        still open: the last pass's, updated by this pass's responses."""
        rates: Dict[str, Tuple[int, int, int]] = {}
        saved = self._budget_state().get("rates")
        for resource, value in (saved.items() if isinstance(saved, dict) else ()):
            if (isinstance(value, list) and len(value) == 3
                    and all(isinstance(v, int) and not isinstance(v, bool) for v in value)):
                rates[str(resource)] = (value[0], value[1], value[2])
        rates.update(self.gh.rate)
        return {r: v for r, v in rates.items() if v[1] > 0 and v[2] > self.now}

    def budget_low(self) -> Optional[Tuple[str, int, int, int]]:
        """(resource, remaining, limit, reset) of the lowest rate limit when
        it is under BUDGET_FLOOR of its limit, else None."""
        low = [(v[0] / v[1], r) + v for r, v in self._rates().items()
               if v[0] < BUDGET_FLOOR * v[1]]
        return min(low)[1:] if low else None

    def _deferred(self) -> bool:
        """True when the budget is low: the step waits for the reset. Said
        once per rate limit window."""
        low = self.budget_low()
        if low is None:
            return False
        resource, remaining, limit, reset = low
        state = self._budget_state()
        if not self._told_low and state.get("told") != [resource, reset]:
            _say("GitHub %s budget low (%d of %d left, resets %s): claims and "
                 "reports only until then" % (resource, remaining, limit, self._utc(reset)))
        state["told"] = [resource, reset]
        self._told_low = True
        return True

    def end_pass(self) -> None:
        """Log this pass's requests, keep the rate limits for the next pass,
        and remove conditional-request answers nothing uses any more."""
        gh = self.gh
        kinds = ", ".join("%s %d" % (k, n) for k, n in
                          sorted(gh.kinds.items(), key=lambda kv: (-kv[1], kv[0])))
        line = "requests: %d, %d counted (%s), %d answered 304 (not counted)" % (
            gh.requests, gh.requests - gh.not_modified, kinds or "none", gh.not_modified)
        rates = self._rates()
        if "core" in rates:
            remaining, limit, reset = rates["core"]
            line += "; budget %d of %d left, resets %s" % (remaining, limit, self._utc(reset))
        _say(line)
        state = self._budget_state()
        state["rates"] = {r: list(v) for r, v in rates.items()}
        if self.budget_low() is None:
            state.pop("told", None)
        try:
            _write_json(os.path.join(self.state, "budget.json"), state)
            gh.prune_cache(CACHE_KEEP, self.now)
        except OSError as err:
            _say("could not keep the GitHub budget: %s" % err)

    def run_pass(self) -> None:
        self.finish_claims()
        self.report_outcomes()
        if not self._deferred():
            try:
                self.reconcile_pull_requests()
            except (GitHubError, OSError) as err:
                _say("pull request reconciliation failed: %s" % err)
        if not self._deferred():
            try:
                self.hand_back()
            except (GitHubError, OSError, ValueError) as err:
                _say("hand_back failed: %s" % err)
        self.claim_new()
        if not self._deferred():
            try:
                self.assign_queued()
            except (GitHubError, OSError) as err:
                _say("assign_queued failed: %s" % err)
        try:
            self.record_scans()
        except OSError as err:
            _say("could not record the repo scans: %s" % err)
        # Below the budget floor these wait for the reset; trees are local.
        for step in (self.publish_lanes, self.forward_asks, self.post_overdue_standups,
                     self.forward_reports, self.triage_backlogs, self.post_metrics,
                     self.watch_brain, self.upload_transcripts, self.relay_operator,
                     self.watch_default_branches, self.run_factory):
            if self._deferred():
                break
            try:
                step()
            except (GitHubError, OSError) as err:
                _say("%s failed: %s" % (step.__name__, err))
        try:
            self.prune_trees()
        except OSError as err:
            _say("prune_trees failed: %s" % err)


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
        # Record this pass's pid in the lock file while we hold it, so the
        # installer can see a pass is in flight (lanes/mac/lock.sh
        # station_busy) and wait before booting the station out mid-pass.
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, ("%d\n" % os.getpid()).encode("ascii"))
        if gh is None:
            from .github import token_from_env
            gh = GitHub(token_from_env())
        if gh.cache_dir is None:
            # Every GET is conditional: a list re-read each pass costs a 304.
            gh.cache_dir = os.path.join(state, "station", "etags")
        st = Station(gh, settings, **hooks)
        try:
            st.run_pass()
        except StateError as err:
            _say("stopping: %s" % err)
            return 1
        finally:
            st.end_pass()
        return 0
    finally:
        os.close(fd)
