"""The station: briefed ticket issues in, local briefs out, outcomes reported.

One pass (python3 -m bus station, run by launchd) does three things under one
lock: finishes any claim left half done, reports finished runs back to their
tickets, and claims the first trusted briefed ticket (by priority, then age)
for each lane that can take one. docs/BRAIN.md ("Claim and outcome", "Polling budget") is the contract.
"""
from __future__ import annotations

import calendar
import base64
from datetime import date, datetime, timedelta, timezone
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
from urllib.parse import quote
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from . import audit
from . import factory
from . import evidence
from . import handoff
from . import health
from . import invariants
from . import metrics
from . import proof
from . import rewrite
from . import roll_pin
from . import revert
from . import spend
from . import usage
from . import verify_data
from . import text as _text
from . import tickets
from . import transcripts
from . import watch
from .brain import HUMAN_MARKER
from . import brain
from .github import GitHub, GitHubError, _OPENER, _next_link, read_ssm
from .journal import REPLY_RE
from .labels import LANES
from .labels import REMOTE_LANES

HEARTBEAT_MAX_AGE = 600
BRIEFED = "loom:briefed"
RUNNING = "loom:running"
RUNAWAY = "loom:runaway"
# The marker on the comment the station posts when it reviews a runaway hold
# caused by one dead lane (loom#565): the release, or why the hold stays.
DEAD_LANE_MARKER = "<!-- loom:runaway-dead-lane v1 lane=%s -->"
CLAIM_RE = re.compile(r"Claimed for lane [a-z0-9-]+ \(body sha256 [0-9a-f]{12}\)")
ISSUE_LINE_RE = re.compile(r"^Issue: ([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)#([0-9]+)[ \t]*$", re.M)
# The comment a ticket carries once a ticket it waited on was closed as not
# planned and stopped holding it.
AFTER_RELEASE_RE = re.compile(r"^<!-- loom:after-release ([0-9]+) -->")
FAILED_RE = re.compile(r"\bfailed: (.*)$")
# The runner's reason when a lane run ends with nothing committed.
NO_COMMIT = "agent made no commit"
# A pull request a brief names in words, as a settle-findings ticket names the
# one its findings were left on.
NAMED_PR_RE = re.compile(r"\b(?:PR|pull request) #([0-9]+)\b")
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
SECRET_RE = _text.SECRET_RE
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
# The station's standup goes out this long before a goal's report turns two
# hours old. Posting only once a goal was overdue (past REPORT_EVERY and its
# grace) left every report 2.0 to 2.4 hours old before the next one, so the
# visibility number (each goal report at most 2 hours old) could never pass.
STANDUP_LEAD = 15 * 60
# The operator's two reading times (2026-10-14: "Twice a day. Morning and
# evening. 10am and 9pm"): one digest of every goal's reports each, instead of
# each report as it is posted. LOOM_REPORT_DIGEST_TIMES="" turns it off.
DIGEST_TIMES = "10:00,21:00"
DIGEST_FIRST_LOOKBACK = 12 * 3600
REPORT_OVERLAP = 3600
REPORT_FIRST_LOOKBACK = 24 * 3600
# A backlog goal home is triaged at most this often.
TRIAGE_INTERVAL = 3600
# Agent rewrites of loom:triage issues: per backlog goal home per hour, per
# pass (each one waits on an agent command), and how long before an issue
# whose rewrite did not produce a ticket is tried again.
REWRITE_PER_HOUR = 5
REWRITE_PER_PASS = 5
REWRITE_RETRY = 24 * 3600
REWRITE_TIMEOUT = 600
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
# Shipping Loom's own tickets at deploy: lanes/mac/station.sh records the
# revision it deployed and when in DEPLOYED_FILE, and each pass moves at most
# SHIP_PER_PASS merged tickets that revision carries to loom:shipped, oldest
# merge first. Their pull requests are looked for in at most SHIP_PAGES pages
# of the repo's most recently updated closed pull requests.
DEPLOYED_FILE = "deployed.json"
SHIP_PER_PASS = 20
SHIP_PAGES = 5
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
# The handoff packet is rebuilt at most this often, or sooner when a brain
# tick on this machine just ended (publish_handoff).
HANDOFF_EVERY = 600
# Shipping through a pin pull request (LOOM_PIN_SHIP_REPOS): a merged pull
# request in a deployment repo that adds a "# Published from <repo> ..., merge
# <sha>" comment line deploys that revision of the source repo, so it ships
# the merged tickets the revision carries. Pin pull requests merged within
# PIN_SHIP_LOOKBACK seconds are read, each until it has shipped all it
# carries and for PIN_SHIP_SETTLE seconds, so a ticket reconciled to
# loom:merged just after the pin merged still ships (PIN_SHIPS_FILE keeps the
# finished ones).
PUBLISHED_RE = re.compile(r"^\+\s*#\s*Published from\s+([A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)?)"
                          r"[\s,][^\n]*?\bmerge\s+([0-9a-f]{7,40})\b", re.M)
PIN_SHIP_LOOKBACK = 7 * 24 * 3600
PIN_SHIP_SETTLE = 3600
PIN_SHIPS_FILE = "pin_ships.json"
# Adopting open pull requests no ticket links (decision 0002, item 1): each
# pass files at most one "Land or supersede" ticket per scanned repo, oldest
# pull request first, and marks the pull request with ADOPTED_MARKER so it is
# never adopted twice. The ticket's body carries ADOPTS_MARKER too, so a
# marker comment that failed to post files no second ticket. ADOPTIONS_FILE
# keeps, per repo, the pull requests known to be adopted (so each one's
# comments are read once), the count and refusal last logged (each logged
# when it changes), and when a brief was last refused: the repo is tried
# again ADOPT_RETRY seconds later, not every pass.
ADOPTED_MARKER = "<!-- loom:adopted v1 ticket=%s#%d -->"
ADOPTED_RE = re.compile(r"<!-- loom:adopted v1 ticket=[A-Za-z0-9._-]+/[A-Za-z0-9._-]+#[0-9]+ -->")
ADOPTS_MARKER = "<!-- loom:adopts pr=%d -->"
ADOPT_TITLE = "Land or supersede #%d"
ADOPT_TITLE_MAX = 200
ADOPTIONS_FILE = "adoptions.json"
# Issues another operator's Loom owns, each logged once (docs/LANES.md).
OWNED_FILE = "owned.json"
ADOPT_RETRY = 600
REMOTE_RE = re.compile(r"[:/]([A-Za-z0-9._-]+/[A-Za-z0-9._-]+?)(?:\.git)?/?$")
# Automatic retries (docs/BRAIN.md "Claim and outcome"). The runner starts
# each failure reason with its class; a reason without one is a work failure.
# A [factory] brief is not a failure at all: the validation environment the
# factory broke (a contract the runner cannot read, a check it cannot run,
# a base check already red), so the ticket stays running on its lane and
# keeps its rung, and the brain re-queues it when the incident closes.
CLASS_RE = re.compile(r"^\[(limit|transient|work|factory)\] ")
RETRY_CLASSES = ("limit", "transient")
RETRY_MAX = 2
# Where the runner files its validation incidents (lanes/run_lane.sh
# FACTORY_REPO), and the marker and ticket line their bodies carry.
FACTORY_REPO = "quadseven/loom"
FACTORY_LABEL = "loom:factory"
INCIDENT_MARKER = "<!-- loom:factory-incident v1 -->"
HELD_FOR_RE = re.compile(r"factory incident ([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)#([0-9]+)")
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
    "spark": ["spark/?*"],
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
VERIFY_INTERVAL = 6 * 3600
# Fewest characters a verifier's "missing evidence" may have to earn the
# loom:awaiting-evidence label: a reason, not a word.
MISSING_MIN = 20
VERIFY_FILE = "verify-runs.json"
# Lanes that spend Anthropic models never take a verify run; the free and
# subscription lanes do (the claude lane spends the operator's subscription).
VERIFY_SKIP = ("anthropic", "claude")

# A live criterion passes only on something seen: a log line, a response, a
# page, with the time it was seen. A lane that read the code and wrote what
# the logs "would show" proved nothing (2026-10-15: a lane proof passed a
# live criterion that way and was moved back by hand).
_LIVE_CRITERION = re.compile(r"^\s*live\b", re.I)
_SEEN_AT = re.compile(r"\b\d{4}-\d\d-\d\d[T ]\d\d:\d\d|\b\d\d:\d\d(?::\d\d)?Z\b")
_HYPOTHETICAL = re.compile(
    r"\b(would|should|will|expected|expects?|could|might|once deployed|when deployed)\b", re.I)


def unseen_live_evidence(record: Dict[str, Any]) -> str:
    """Why a lane's proof does not show its live criteria were observed, or
    "" when every live criterion's evidence names a time it was seen and
    does not speak in the conditional."""
    for item in record.get("criteria") or []:
        if not isinstance(item, dict) or not _LIVE_CRITERION.match(str(item.get("criterion") or "")):
            continue
        evidence = str(item.get("evidence") or "")
        if _HYPOTHETICAL.search(evidence):
            return ("the evidence for a live criterion says what would happen (%r), "
                    "not what was seen" % _HYPOTHETICAL.search(evidence).group(0))
        if not _SEEN_AT.search(evidence):
            return "the evidence for a live criterion names no time it was seen"
    return ""
NOTE_MAX = 1500


def verify_due(issues: Sequence[Tuple[str, Dict[str, Any]]],
               attempts: Dict[str, Any], now: float) -> List[Tuple[str, Dict[str, Any]]]:
    """Shipped tickets without a proof, oldest ship event first, outside cooldown."""
    due = []
    for repo, issue in issues:
        labels = _label_names(issue)
        key = "%s#%s" % (repo, issue.get("number"))
        if (issue.get("state") != "open" or "loom:ticket" not in labels
                or "loom:shipped" not in labels or "loom:verified" in labels
                or issue.get("has_proof")):
            continue
        last = attempts.get(key)
        if isinstance(last, (int, float)) and now - float(last) < VERIFY_INTERVAL:
            continue
        due.append((repo, issue))
    return sorted(due, key=lambda item: (
        float(item[1].get("shipped_at", 0) or 0),
        item[0], int(item[1].get("number", 0))))


def parse_digest_times(raw: str) -> Tuple[Tuple[int, int], ...]:
    """Local HH:MM times from a comma or space separated list, sorted; any
    entry that is not a valid time is left out."""
    times = set()
    for item in str(raw or "").replace(",", " ").split():
        m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", item)
        if m:
            times.add((int(m.group(1)), int(m.group(2))))
    return tuple(sorted(times))


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
                 transcript_token_ssm: str = "",
                 watched: Optional[List[Tuple[str, str]]] = None,
                 rewrite_cmd: str = "",
                 rewrite_private_cmd: str = "",
                 self_repo: str = "",
                 invariant_goal: str = "",
                 plan_check: str = invariants.DEFAULT_PLAN_CHECK,
                 report_digest: Sequence[Tuple[int, int]] = (),
                 health_urls: str = "",
                 health_every: int = health.EVERY,
                 evidence_cmds: Optional[Dict[str, str]] = None,
                 roll_ship_docs: Optional[List[Dict[str, str]]] = None,
                 pin_ship_repos: Sequence[str] = ()) -> None:
        self.home = home
        # Deployment repos whose merged pin pull requests ship the tickets
        # of the source revision they publish (ship_pins).
        self.pin_ship_repos = list(pin_ship_repos)
        # "owner/name#goal=https://..." entries: each goal home's health
        # endpoint (bus/health.py, docs/HEALTH.md), read every health_every s.
        self.health_urls = health_urls
        self.health_every = health_every
        self.evidence_cmds = evidence_cmds or {}
        self.roll_ship_docs = roll_ship_docs or []
        # Local times (hour, minute) to send one digest of every goal's
        # reports; empty forwards each report as it is posted.
        self.report_digest = tuple(report_digest)
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
        # (component, log path) pairs bus/watch.py reads each pass.
        self.watched = watch.components(home) if watched is None else watched
        self.rewrite_cmd = rewrite_cmd
        self.rewrite_private_cmd = rewrite_private_cmd
        # The repo this checkout deploys from (LOOM_SELF_REPO, default the
        # lanes repo): its merged tickets ship when the station deploys them.
        self.self_repo = self_repo or status_repo
        # owner/name#N: the goal home invariant breaches are filed on
        # (LOOM_INVARIANT_GOAL); empty means LOOM_METRICS_GOAL when it is in
        # the self repo, else the self repo's one goal home that is not a
        # backlog.
        self.invariant_goal = invariant_goal
        # The review bot's check that reads a pull request's plan (its body).
        self.plan_check = plan_check

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "Settings":
        env = os.environ if env is None else env
        home = env.get("LOOM_HOME") or os.path.join(os.path.expanduser("~"), "loom")

        def words(name: str) -> List[str]:
            return env.get(name, "").replace(",", " ").split()
        report_digest = parse_digest_times(env.get("LOOM_REPORT_DIGEST_TIMES", DIGEST_TIMES))
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
            watched = watch.components(home, env.get("LOOM_WATCH", ""))
        except ValueError as err:
            _say("ignoring LOOM_WATCH: %s" % err)
            watched = watch.components(home)
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
                   report_digest=report_digest,
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
                   transcript_token_ssm=env.get("LOOM_TRANSCRIPT_TOKEN_SSM", "").strip(),
                   watched=watched,
                   rewrite_cmd=env.get("LOOM_REWRITE_CMD", "").strip(),
                   rewrite_private_cmd=env.get("LOOM_REWRITE_PRIVATE_CMD", "").strip(),
                   self_repo=env.get("LOOM_SELF_REPO", "").strip(),
                   invariant_goal=env.get("LOOM_INVARIANT_GOAL", "").strip(),
                   plan_check=(env.get("LOOM_PLAN_CHECK", "").strip()
                               or invariants.DEFAULT_PLAN_CHECK),
                   evidence_cmds=_evidence_commands(env),
                   health_urls=env.get("LOOM_HEALTH_URLS", "").strip(),
                   health_every=_seconds(env.get("LOOM_HEALTH_EVERY", ""), health.EVERY),
                   roll_ship_docs=roll_pin.specs_from_env(env.get("LOOM_ROLL_SHIP_DOCS", "")),
                   pin_ship_repos=words("LOOM_PIN_SHIP_REPOS"))


def _evidence_commands(env: Dict[str, str]) -> Dict[str, str]:
    try:
        commands = json.loads(env.get("LOOM_EVIDENCE_CMDS", "{}") or "{}")
        if not isinstance(commands, dict) or not all(
                isinstance(repo, str) and isinstance(command, str)
                for repo, command in commands.items()):
            raise ValueError("expected a JSON object of repo to command")
    except (ValueError, TypeError) as err:
        _say("ignoring LOOM_EVIDENCE_CMDS: %s" % err)
        return {}
    return commands


def _seconds(raw: str, default: int) -> int:
    """A whole number of seconds of at least 60, else default."""
    raw = raw.strip()
    return int(raw) if raw.isdigit() and int(raw) >= 60 else default


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


def _checkout_head() -> str:
    """The commit this checkout is at, or "" when git cannot say."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        out = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _is_ancestor(commit: str, revision: str) -> bool:
    """The commit is the revision or one of its ancestors in this checkout,
    the one lanes/mac/station.sh deploys. An unknown commit is not."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        out = subprocess.run(["git", "-C", root, "merge-base", "--is-ancestor", commit, revision],
                             capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0


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


_PART_OF_RE = re.compile(r"^Part of #(\d+)(?!\d)", re.M)


def _ticket_goal(issue: Dict[str, Any]) -> Optional[int]:
    """The goal a ticket names: its Part of #N line, else the parent of its
    sub-issue link, else None."""
    match = _PART_OF_RE.search(str(issue.get("body") or ""))
    if match:
        return int(match.group(1))
    match = re.search(r"/issues/(\d+)(?:[/?#]|$)", str(issue.get("parent_issue_url") or ""))
    return int(match.group(1)) if match else None


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


def model_lanes(model: str) -> List[str]:
    """Compatible lanes, with the model ladder's lanes first when applicable."""
    rung = next((lanes for lanes, rung_model in factory.LADDER if rung_model == model), ())
    return [lane for lane in dict.fromkeys([*rung, *LANES])
            if lane not in REMOTE_LANES and lane_takes_model(lane, model)]


def remote_beat_fresh(lane: str, at: Optional[float], now: float) -> bool:
    """Whether a remote lane's last heartbeat still counts as up: within its
    configured beat_max (labels.REMOTE_LANES), else HEARTBEAT_MAX_AGE."""
    window = REMOTE_LANES.get(lane, {}).get("beat_max", HEARTBEAT_MAX_AGE)
    return at is not None and -60 <= now - at <= window


def needs_public_repo(lane: str, model: str) -> bool:
    """Free lanes, and lanes whose provider trains on prompts, take public
    repos only."""
    lane = provider(lane)
    return (lane in FREE_LANES or lane in REMOTE_LANES or lane == "agy"
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


def _refs_numbers(repo: str, body: str) -> List[int]:
    """The ticket numbers a pull request body names with a `Refs` line, in
    the full `Refs OWNER/NAME#N` form or the same-repo `Refs #N` form."""
    return [int(n) for n in re.findall(r"^Refs (?:%s)?#([0-9]+)[ \t]*$" % re.escape(repo),
                                       body, re.M)]


def _linked_pr(issue: Dict[str, Any], repo: str, pr: Dict[str, Any]) -> bool:
    number = int(issue.get("number", 0))
    return bool(number in _refs_numbers(repo, str(pr.get("body") or ""))
                or re.match(r"loom/%d(?:-|$)" % number,
                            str((pr.get("head") or {}).get("ref") or "")))


def adoption_body(pr: int, goal: int) -> str:
    """The brief for an adoption ticket. It names the pull request in words
    and #N, never in a Refs line: the lane's own pull request carries the
    Refs line to this ticket."""
    return "\n".join([
        "## Why", "",
        "Pull request #%d is open in this repo, but no Loom ticket links it, so the station "
        "never lands it, sends it back or reconciles it: its checks and review findings have "
        "no owner. The station adopted it with this ticket (decision 0002, item 1)." % pr, "",
        "## What", "",
        "Land or supersede pull request #%d." % pr, "",
        "- If its change is still wanted, take its diff onto this ticket's fresh `loom/` "
        "branch, bring it up to date with the default branch, make the checks and the review "
        "pass, and open the pull request with a `Refs` line to this ticket. Then close pull "
        "request #%d with a comment naming the new pull request." % pr,
        "- If its change is already on the default branch or no longer applies, close pull "
        "request #%d as superseded with a comment giving the reason, change nothing, and give "
        "that reason as the run's answer, so it is posted on this ticket and a brain moves the "
        "ticket to merged with that note." % pr, "",
        "## Acceptance criteria", "",
        "- [ ] The change from pull request #%d reaches the default branch through a pull "
        "request with a `Refs` line to this ticket, or it is shown to be on the default branch "
        "already or no longer needed." % pr,
        "- [ ] The new pull request's checks pass and every review thread on it is answered.",
        "- [ ] Pull request #%d is closed with a comment naming the pull request that replaced "
        "it or the reason it was superseded." % pr, "",
        "## Size", "", "M", "",
        "## Out of scope", "",
        "Changes beyond what pull request #%d makes, other than what its checks and review "
        "require." % pr, "",
        "## Definition of done", "",
        "Pull request #%d is closed, and its change is merged through this ticket's pull "
        "request or recorded here as superseded." % pr, "",
        ADOPTS_MARKER % pr, "",
        brain.part_of_line(goal), ""])


def adoption_comment(repo: str, ticket: int) -> str:
    """The marker the station leaves on a pull request it adopted."""
    return ("%s\nLoom adopted this pull request: no ticket linked it, so ticket #%d now owns "
            "it. The lane that takes the ticket lands this change on a `loom/` branch, or "
            "closes this pull request as superseded with the reason."
            % (ADOPTED_MARKER % (repo, ticket), ticket))


class Station:
    def __init__(self, gh: GitHub, settings: Settings,
                 now: Optional[float] = None,
                 read_secret: Optional[SecretReader] = None,
                 post: Optional[Poster] = None,
                 discord: Optional[DiscordCall] = None,
                 upload: Optional[Uploader] = None,
                 ancestor: Optional[Callable[[str, str], bool]] = None,
                 head: Optional[Callable[[], str]] = None) -> None:
        self.gh = gh
        self.s = settings
        self.read_secret = read_secret or read_ssm
        self.post = post or post_webhook
        self.upload = upload or post_transcript
        self.ancestor = ancestor or _is_ancestor
        self.head = head or _checkout_head
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
        # The tickets named by After: lines, read once a pass.
        self._depends: Dict[str, Optional[Dict[str, Any]]] = {}
        self._remote_beats: Dict[str, Optional[float]] = {}
        # Whether each repo is public, and the repo invariant breaches are
        # filed in: what a public issue may name (_invariant_ref).
        self._publics: Dict[str, bool] = {}
        self._invariant_repo = ""
        # The GitHub budget as the last pass left it, and the reset it was
        # last reported low for.
        self._budget: Optional[Dict[str, Any]] = None
        self._told_low = False
        # This pass's unproven-work gate refusals, per goal (loom#564):
        # goal key -> the count and limit the gate hit, the briefed tickets
        # it holds, and the lanes it left idle.
        self._gate_holds: Dict[str, Dict[str, Any]] = {}
        self._parents: Dict[Tuple[str, int], Dict[str, Any]] = {}
        # This pass's closed pull requests and default branches, per repo:
        # the hand-revert scan reads the same pages as the ship checks.
        self._closed_cache: Dict[str, List[Dict[str, Any]]] = {}
        self._branch_cache: Dict[str, str] = {}
        # Repos whose claim:<login> label this pass has made sure of.
        self._claim_labels: set = set()

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

    # -- another operator's issues (docs/LANES.md, Two Looms) -------------
    def _note_owned(self, key: str, why: str) -> None:
        """Log once per issue and reason that another operator owns it."""
        path = os.path.join(self.state, OWNED_FILE)
        seen = _read_json(path, {}, strict=False)
        seen = seen if isinstance(seen, dict) else {}
        if seen.get(key) != why:
            _say("leaving %s alone: %s" % (key, why))
            seen[key] = why
            _write_json(path, seen)

    def _owned_elsewhere(self, repo: str, issue: Dict[str, Any]) -> bool:
        """True when the issue or pull request is assigned to a login outside
        LOOM_BRIEF_AUTHORS: another operator's Loom owns it."""
        owner = tickets.owned_elsewhere(issue, self.s.authors)
        if owner is None:
            return False
        self._note_owned("%s#%s" % (repo, issue.get("number")),
                         "assigned to %s, not a LOOM_BRIEF_AUTHORS login" % owner)
        return True

    def _goal_elsewhere(self, repo: str, issue: Dict[str, Any]) -> bool:
        """True when the ticket's goal home names another operator."""
        goal = self._goal_of(repo, issue)
        if goal is None:
            return False
        home = self._parent(repo, goal)
        if tickets.goal_ours(home, self.s.authors):
            return False
        self._note_owned("%s#%s" % (repo, issue.get("number")),
                         "its goal #%d names operator %s" % (goal, tickets.goal_operator(home)))
        return True

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
        has_contract, checks = self._validation_checks(repo)
        text = tickets.render_brief(repo, issue, lane, attempt, fix,
                                    checks, has_contract)
        claiming = self._write_brief(lane, name, text)
        self._assign_operator(repo, issue)
        labels = [n for n in _label_names(issue) if n != BRIEFED]
        if RUNNING not in labels:
            labels.append(RUNNING)
        mark = self._claim_label(repo)
        if mark and mark not in [n.lower() for n in labels]:
            labels.append(mark)
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
        # A claim for the goal succeeded, so the unproven-work gate no longer
        # holds it and its refusal tracking clears (loom#564).
        goal = self._goal_of(repo, issue)
        if goal is not None:
            self._gate_holds.pop("%s#%d" % (repo, goal), None)
        return True

    def _assign_operator(self, repo: str, issue: Dict[str, Any]) -> None:
        """Assign a claimed issue to this Loom's operator login, so another
        operator's Loom sees the claim before it acts. A refused assignment
        is logged and the claim goes on."""
        login = tickets.operator_login(self.s.authors)
        if not login or login.lower() in tickets.assignees(issue):
            return
        number = int(issue["number"])
        try:
            self.gh.rest("POST", "/repos/%s/issues/%d/assignees" % (repo, number),
                         {"assignees": [login]})
        except GitHubError as err:
            _say("could not assign %s#%d to %s: %s" % (repo, number, login, err))

    def _claim_label(self, repo: str) -> Optional[str]:
        """The claim:<operator login> label a claim adds, created in the repo
        the first time this pass needs it; None without an operator login."""
        login = tickets.operator_login(self.s.authors)
        if not login:
            return None
        name = tickets.claim_label(login)
        if repo not in self._claim_labels:
            try:
                self.gh.rest("POST", "/repos/%s/labels" % repo,
                             {"name": name, "color": "c5def5",
                              "description": "Claimed by this operator's Loom or sessions"})
            except GitHubError as err:
                if err.status != 422:  # 422: the label already exists
                    _say("could not create label %s in %s: %s" % (name, repo, err))
            self._claim_labels.add(repo)
        return name

    def _contents_text(self, repo: str, path: str) -> Optional[str]:
        """Read a text file through GitHub contents; None means it is absent."""
        data = self.gh.rest("GET", "/repos/%s/contents/%s" %
                            (repo, quote(path, safe="/")), ok=(404,)).data
        if not isinstance(data, dict) or not data.get("content"):
            return None
        return base64.b64decode(data["content"]).decode("utf-8")

    def _validation_checks(self, repo: str) -> Tuple[bool, List[Tuple[str, str]]]:
        """Return whether the repo has a validation contract and its PR run steps."""
        if self._contents_text(repo, ".loom/validation.conf") is not None:
            return True, []
        listing = self.gh.rest("GET", "/repos/%s/contents/.github/workflows" % repo,
                               ok=(404,)).data
        found: List[Tuple[str, str]] = []
        for item in listing if isinstance(listing, list) else []:
            path = str(item.get("path") or "") if isinstance(item, dict) else ""
            if not path.endswith((".yml", ".yaml")):
                continue
            workflow = self._contents_text(repo, path)
            has_pull_request = workflow is not None and bool(re.search(
                r"(?m)^\s*pull_request\s*(?::|,|\])|^\s*on:\s*\[[^\]]*pull_request",
                workflow))
            if not has_pull_request:
                continue
            lines = workflow.splitlines()
            index = 0
            step_name = ""
            run_number = 0
            while index < len(lines):
                if re.match(r"^\s*-\s+", lines[index]):
                    step_name = ""
                named = re.match(r"^\s*-\s*name:\s*(.+?)\s*$", lines[index])
                if named:
                    step_name = named.group(1).strip("\"'")
                match = re.match(r"^\s*(?:-\s*)?run:\s*(.*)$", lines[index])
                if not match:
                    index += 1
                    continue
                run_number += 1
                command = match.group(1).strip()
                indent = len(lines[index]) - len(lines[index].lstrip())
                index += 1
                if command in ("|", ">", "|-", ">-"):
                    block = []
                    while index < len(lines):
                        line = lines[index]
                        if line.strip() and len(line) - len(line.lstrip()) <= indent:
                            break
                        block.append(line.strip())
                        index += 1
                    command = " ".join(part for part in block if part)
                if command:
                    source = "%s, step %s" % (path, step_name or str(run_number))
                    found.append((source, command))
        return False, found

    def file_validation_contracts(self) -> None:
        """Ask each goal home for a validation contract while its repo lacks one."""
        title = "Add the repository validation contract"
        body = ("## Why\nLanes need the repository's checks before opening pull requests.\n\n"
                "## What\nAdd `.loom/validation.conf` with the lint, format, type and test "
                "commands that the pull-request workflows run, in that order. For every "
                "command, record the workflow file and step it comes from. The contract "
                "must pass on the default branch in this ticket's pull request.\n\n"
                "## Acceptance criteria\n- [ ] The contract lists the repository's lint, "
                "format, type and test commands in order, with workflow file and step.\n"
                "- [ ] Every listed command matches a run step in a pull-request workflow.\n"
                "- [ ] The ticket pull request passes the contract on the default branch.\n\n"
                "## Size\nS\n\n## Out of scope\nWriting the contract by the station.\n")
        for repo, goal in self._goal_homes():
            if not str(goal.get("title") or "").startswith("Backlog:"):
                continue
            if self._contents_text(repo, ".loom/validation.conf") is not None:
                continue
            issues = self.gh.rest("GET", "/repos/%s/issues?labels=loom:ticket&state=open&per_page=100"
                                  % repo).data or []
            open_issues = issues if isinstance(issues, list) else []
            pending = any(isinstance(issue, dict) and "pull_request" not in issue
                          and str(issue.get("title") or "") == title
                          for issue in open_issues)
            if pending:
                continue
            brain.brief(self.gh, repo, int(goal["number"]), title, body,
                        difficulty="medium")

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
        if lane in REMOTE_LANES:
            if lane not in self._remote_beats:
                self._remote_beats[lane] = self._remote_heartbeat(lane)
            return remote_beat_fresh(lane, self._remote_beats[lane], self.now)
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

    def _remote_heartbeat(self, lane: str) -> Optional[float]:
        """Latest heartbeat posted by a remote worker on the lane status issue."""
        saved = _read_json(os.path.join(self.state, "remote-status.json"), {}, strict=False)
        cached = saved.get(lane) if isinstance(saved, dict) else None
        if (isinstance(cached, dict) and isinstance(cached.get("checked_at"), (int, float))
                and 0 <= self.now - cached["checked_at"] <= HEARTBEAT_MAX_AGE):
            value = cached.get("heartbeat")
            return value if isinstance(value, (int, float)) else None
        heartbeat = self._read_remote_heartbeat(lane)
        counts = self._read_remote_claim_count(lane) if heartbeat is not None else 0
        if not isinstance(saved, dict):
            saved = {}
        saved[lane] = {"checked_at": self.now, "heartbeat": heartbeat, "claims": counts}
        _write_json(os.path.join(self.state, "remote-status.json"), saved)
        return heartbeat

    def _read_remote_heartbeat(self, lane: str) -> Optional[float]:
        repo = self.s.status_repo
        if not repo:
            return None
        found = self.gh.rest("GET", "/repos/%s/issues?labels=%s&state=open&per_page=100"
                             % (repo, LANES_LABEL)).data
        issues = [x for x in found if isinstance(x, dict) and "pull_request" not in x] \
            if isinstance(found, list) else []
        if not issues:
            return None
        latest = None
        pattern = re.compile(r"^<!-- loom:remote-heartbeat v1 ([a-z0-9-]+) ([0-9.]+) -->$")
        for comment in self.gh.paginate("/repos/%s/issues/%d/comments" %
                                        (repo, int(issues[0]["number"]))):
            match = pattern.fullmatch(str(comment.get("body") or "").strip()) \
                if isinstance(comment, dict) else None
            if match and match.group(1) == lane:
                try:
                    at = float(match.group(2))
                except ValueError:
                    continue
                latest = at if latest is None else max(latest, at)
        return latest

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
        free = [lane for lane in LANES if lane not in REMOTE_LANES
                if self._heartbeat_ok(lane) and self._inbox_empty(lane)]
        # This pass's unproven-work gate refusals (loom#564).
        self._gate_holds = {}
        # A runaway held because one dead lane kept failing it goes back to a
        # free lane of its tier on its own (loom#565), before claims.
        self._release_dead_lane_runaways(free)
        if not free:
            self._claim_candidates([])
            self._record_slot_sample(False)
            return
        cands = self._claim_candidates(free)
        before = {lane: len(cands[lane]) for lane in free}
        by_lane = self._apply_unproven_gate(cands)
        # The lanes that had briefed tickets and the gate left with nothing:
        # they sit idle while the gate holds the work (loom#564).
        idle = [lane for lane in free if before[lane] and not by_lane[lane]]
        for hold in self._gate_holds.values():
            hold["idle"] = len(idle)
        for lane in free:
            self._claim_for_lane(lane, by_lane[lane], free)
        self._record_slot_sample(any(by_lane[lane] and self._inbox_empty(lane)
                                     for lane in free))
        self._hand_verifies([lane for lane in free if not by_lane[lane]])

    # -- dead-lane runaway release (loom#565) ------------------------------
    def _open_lane_no_success(self) -> Dict[str, Tuple[int, str]]:
        """Lane -> (issue number, title) for each open runs-and-none-done
        invariant filed on the invariant goal home."""
        goal = self._invariant_goal()
        if goal is None:
            return {}
        repo, goal_no = goal
        found: Dict[str, Tuple[int, str]] = {}
        for issue in self._open_breaches(repo, goal_no):
            match = invariants.MARKER_RE.search(str(issue.get("body") or ""))
            if not match:
                continue
            key = match.group(1)
            if not key.startswith("lane-no-success/"):
                continue
            # The breach key hashes the lane name; the title names it.
            title = str(issue.get("title") or "")
            lane = re.search(r"^Lane ([a-z0-9-]+):", title)
            if lane and key == "lane-no-success/%s" % hashlib.sha256(
                    lane.group(1).encode()).hexdigest()[:12]:
                found[lane.group(1)] = (int(issue["number"]), title)
        return found

    def _release_dead_lane_runaways(self, free: List[str]) -> None:
        """Release runaway holds caused by one dead lane: every attempt since
        the ticket's last brief ran on a lane with an open runs-and-none-done
        invariant. The hold is released and the ticket briefed to a free lane
        of its difficulty tier, with a comment naming the lane fault. This is
        not a failure: no rung marker, no ladder move, no failure recorded.
        With no free lane in its tier the ticket stays held and the comment
        says why. A runaway whose attempts spread across lanes keeps its hold
        for the brain, as before."""
        runaways: List[Tuple[str, Dict[str, Any]]] = []
        # The per-repo runaway listing only runs when a lane is actually
        # dead: with no open runs-and-none-done invariant there is nothing
        # to release.
        dead = self._open_lane_no_success()
        if not dead:
            return
        for repo in self.scan_repos():
            try:
                found = self.gh.paginate("/repos/%s/issues?labels=%s&state=open"
                                         % (repo, RUNAWAY))
            except GitHubError as err:
                _say("could not list runaway tickets in %s: %s" % (repo, err))
                continue
            runaways += [(repo, i) for i in found
                         if isinstance(i, dict) and "pull_request" not in i]
        if not runaways:
            return
        used = set()
        for repo, issue in runaways:
            try:
                self._release_one_runaway(repo, issue, free, dead, used)
            except (GitHubError, OSError) as err:
                _say("could not review runaway %s#%s: %s" % (repo, issue.get("number"), err))

    def _release_one_runaway(self, repo: str, issue: Dict[str, Any], free: List[str],
                             dead: Dict[str, Tuple[int, str]], used: Set[str]) -> None:
        number = int(issue["number"])
        names = _label_names(issue)
        if "loom:pr" in names:
            # A runaway held on an open pull request stays with the brain:
            # re-briefing it would orphan the fix-round machinery.
            return
        comments = [c for c in self.gh.paginate("/repos/%s/issues/%d/comments" % (repo, number))
                    if isinstance(c, dict)]
        lane = factory.dead_lane(comments)
        if lane is None or lane not in dead:
            return
        inv_number, inv_title = dead[lane]
        marker = DEAD_LANE_MARKER % lane
        if any(str(c.get("body") or "").startswith(marker) for c in comments):
            return
        if repo not in self._publics:
            self._publics[repo] = self._public(repo)
        tier = factory.tier_lanes(issue, not self._publics[repo])
        target = next((x for x in tier if x in free and x not in used), None)
        if target is None:
            self._comment(repo, number,
                          "%s\nRunaway hold reviewed: every attempt since the last brief ran "
                          "on lane %s, which has an open runs-and-none-done invariant "
                          "(#%d); but no lane of its difficulty tier is free, so the hold "
                          "stays." % (marker, lane, inv_number))
            _say("%s#%d stays held: its dead lane %s has no free tier lane"
                 % (repo, number, lane))
            return
        names = [n for n in names
                 if n != RUNAWAY and n != RUNNING and not n.startswith("lane:")]
        if BRIEFED not in names:
            names.append(BRIEFED)
        names.append("lane:" + target)
        self.gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number), {"labels": names})
        issue["labels"] = [{"name": n} for n in names]
        self._comment(repo, number,
                      "%s\nRunaway hold released: every attempt since the last brief ran on "
                      "lane %s, which has an open runs-and-none-done invariant (#%d: %s). "
                      "This is not a failure: the ticket is briefed to lane %s, in its "
                      "difficulty tier." % (marker, lane, inv_number, inv_title, target))
        used.add(target)
        _say("released runaway %s#%d from dead lane %s to lane %s" % (repo, number, lane, target))

    def _parent(self, repo: str, number: int) -> Dict[str, Any]:
        """An issue a ticket names as its parent, read once a pass; empty
        when it cannot be read."""
        key = (repo, number)
        if key not in self._parents:
            try:
                data = self.gh.rest("GET", "/repos/%s/issues/%d" % key).data
            except GitHubError:
                data = None
            self._parents[key] = data if isinstance(data, dict) else {}
        return self._parents[key]

    def _goal_of(self, repo: str, issue: Dict[str, Any]) -> Optional[int]:
        """The goal a ticket belongs to (decision 0003: a goal holds epics, an
        epic holds tickets). The issue the ticket names counts when it is an
        open loom:goal; when it is an epic, the goal that epic names counts.
        Anything else is no goal, never the parent's own number: a ticket
        under an epic once handed the epic to the unproven-work check, which
        raised and ended the pass."""
        parent = _ticket_goal(issue)
        if parent is None:
            return None
        data = self._parent(repo, parent)
        names = _label_names(data)
        if data.get("state") == "open" and "loom:goal" in names:
            return parent
        if "epic" not in [name.lower() for name in names]:
            return None
        goal = _ticket_goal(data)
        if goal is None or goal == parent:
            return None
        home = self._parent(repo, goal)
        if home.get("state") == "open" and "loom:goal" in _label_names(home):
            return goal
        return None

    def _unproven_gate(self, repo: str, issue: Dict[str, Any], limit: int,
                       counts: Dict[Tuple[str, int], int]) -> Optional[str]:
        """Why the station must not claim this build ticket, or None when the
        claim may go (loom#483). Regressions, reverts and urgent tickets are
        never gated: the limit holds back new work, not repairs."""
        names = set(_label_names(issue))
        if names & {REGRESSION, factory.REVERT_LABEL, URGENT}:
            return None
        goal = self._goal_of(repo, issue)
        if goal is None:
            return None
        key = (repo, goal)
        if key not in counts:
            try:
                counts[key] = brain.goal_unproven(self.gh, repo, goal)
            except ValueError as err:
                # One ticket with a bad goal is skipped this pass; it must
                # never end the pass for every other ticket.
                return "its goal #%d cannot be read: %s" % (goal, err)
        held = counts[key]
        if held <= limit:
            return None
        return ("goal #%d has %d tickets shipped without a proof "
                "(limit %d); the goal verifies them before the station starts more"
                % (goal, held, limit))

    def _apply_unproven_gate(self, by_lane: Dict[str, List[Tuple[str, Dict[str, Any]]]]
                             ) -> Dict[str, List[Tuple[str, Dict[str, Any]]]]:
        """Drop the build tickets the unproven-work limit blocks, counting each
        (repo, goal) once per pass. A lane left with nothing is handed to
        _hand_verifies, so free slots run verify runs instead. The refusals
        are recorded per goal: a goal refused for an hour gets its own
        invariant (loom#564)."""
        limit = brain.unverified_limit()
        counts: Dict[Tuple[str, int], int] = {}
        gated = {lane: [] for lane in by_lane}
        for lane, found in by_lane.items():
            for repo, issue in found:
                reason = self._unproven_gate(repo, issue, limit, counts)
                if reason is None:
                    gated[lane].append((repo, issue))
                else:
                    self._record_gate_hold(repo, issue, limit, counts)
                    _say("not claiming %s#%s for lane %s: %s"
                         % (repo, issue.get("number"), lane, reason))
        return gated

    def _record_gate_hold(self, repo: str, issue: Dict[str, Any], limit: int,
                         counts: Dict[Tuple[str, int], int]) -> None:
        """Remember the unproven-work gate's refusal of one briefed ticket,
        per goal, so an hour of refusals becomes an invariant (loom#564)."""
        goal = self._goal_of(repo, issue)
        if goal is None or (repo, goal) not in counts:
            return
        key = "%s#%d" % (repo, goal)
        hold = self._gate_holds.setdefault(
            key, {"repo": repo, "goal": goal, "count": counts[(repo, goal)],
                  "limit": limit, "tickets": {}, "idle": 0})
        hold["count"] = counts[(repo, goal)]
        hold["limit"] = limit
        hold["tickets"][int(issue["number"])] = str(issue.get("title") or "")

    def _claim_for_lane(self, lane: str, found: List[Tuple[str, Dict[str, Any]]],
                        free: List[str]) -> None:
        """Claim the first ticket in work order for one free lane that can be
        claimed."""
        # A revert of a red default branch (bus/revert.py) goes first, then
        # a regression fix (bus.brain brief --regresses), then the rest in
        # tickets.work_order: urgent, then priority, then the oldest.
        cands = sorted(found, key=lambda c: (
            factory.REVERT_LABEL not in _label_names(c[1]),
            REGRESSION not in _label_names(c[1])) + tickets.work_order(*c))
        for repo, issue in cands:
            try:
                if self._try_ticket(repo, lane, issue, free):
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
                self._add_claim_candidate(by_lane, free, repo, issue)
        return by_lane

    def _add_claim_candidate(self, by_lane: Dict[str, List[Tuple[str, Dict[str, Any]]]],
                             free: List[str], repo: str, issue: Dict[str, Any]) -> None:
        lane = _lane_of(issue)
        if lane in by_lane:
            by_lane[lane].append((repo, issue))
            return
        if not lane:
            return
        model = re.search(r"^Model:\s*(\S+)", str(issue.get("body") or ""), re.M)
        if not model or lane_takes_model(lane, model.group(1)):
            return
        if free:
            by_lane[free[0]].append((repo, issue))
            return
        try:
            self._try_ticket(repo, lane, issue, free)
        except (GitHubError, OSError) as err:
            _say("could not queue %s#%s: %s" % (repo, issue["number"], err))

    # -- verify runs ------------------------------------------------------
    def _tickets_open(self, repo: str) -> List[Dict[str, Any]]:
        """The repo's open tickets filed by a trusted author."""
        allowed = {a.lower() for a in self.s.authors}
        found = self.gh.paginate("/repos/%s/issues?labels=loom:ticket&state=open" % repo)
        return [i for i in found if isinstance(i, dict) and "pull_request" not in i
                and tickets._login(i.get("user")) in allowed
                and not self._owned_elsewhere(repo, i)]

    def _queued_lanes(self, repo: str, issues: List[Dict[str, Any]]) -> set:
        """The lanes a queued ticket of the repo may be given: its model's
        lanes, else its difficulty tier's."""
        lanes: set = set()
        for issue in issues:
            if "loom:queued" not in _label_names(issue):
                continue
            model = re.search(r"^Model:\s*(\S+)", str(issue.get("body") or ""), re.M)
            if model:
                lanes.update(model_lanes(model.group(1)))
            else:
                lanes.update(factory.lanes_of(factory.difficulty_of(issue), not self._public(repo)))
        return lanes

    def _proven(self, repo: str, number: int) -> bool:
        """A trusted proof stands on the ticket: a failed audit after it
        (bus/audit.py) makes the ticket due again."""
        comments = self.gh.paginate("/repos/%s/issues/%d/comments" % (repo, number))
        return audit.proven(comments, self.s.authors)

    def _shipped_stamp(self, repo: str, issue: Dict[str, Any]) -> float:
        """When loom:shipped was last applied; the issue's update time if the
        events do not say."""
        events = self.gh.paginate("/repos/%s/issues/%d/events" % (repo, int(issue["number"])))
        stamps = [metrics.stamp(e.get("created_at")) for e in events
                  if isinstance(e, dict) and e.get("event") == "labeled"
                  and (e.get("label") or {}).get("name") == "loom:shipped"]
        return max(stamps) if stamps else metrics.stamp(issue.get("updated_at"))

    def _verify_repos(self) -> List[str]:
        return [repo for repo in self.s.repos if tickets.REPO_RE.fullmatch(repo)]

    def _verify_pool(self, attempts: Dict[str, Any]
                     ) -> Tuple[List[Tuple[str, Dict[str, Any]]], set]:
        """(the tickets due a verify run, oldest ship first; the lanes queued
        build work could take, or a failed audit holds)."""
        held = set(self._audit_holds())
        shipped: List[Tuple[str, Dict[str, Any]]] = []
        for repo in self._verify_repos():
            issues = self._tickets_open(repo)
            held |= self._queued_lanes(repo, issues)
            shipped += [(repo, i) for i in issues]
        # The cheap filters first: the events and comments of a ticket are
        # read only when its cooldown is over.
        found = []
        for repo, issue in verify_due(shipped, attempts, self.now):
            issue["shipped_at"] = self._shipped_stamp(repo, issue)
            issue["has_proof"] = self._proven(repo, int(issue["number"]))
            found.append((repo, issue))
        return verify_due(found, attempts, self.now), held

    def _audit_holds(self) -> Dict[str, float]:
        """The lanes a failed audit of their proof holds from verify runs,
        with when each hold ends (bus/audit.py)."""
        holds = audit.held(self.gh, self._verify_repos(), self.s.authors, self.now)
        for lane, until in sorted(holds.items()):
            _say("lane %s takes no verify run until %s: a proof it made failed its audit"
                 % (lane, time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(until))))
        return holds

    def _hand_verifies(self, idle: List[str]) -> None:
        """Give each idle slot that may verify, has no build work and is
        empty one shipped ticket to verify. The ticket keeps its labels: the
        station moves it when the result comes back (_report_verify)."""
        lanes = [x for x in idle if provider(x) not in VERIFY_SKIP and self._inbox_empty(x)]
        if not lanes:
            return
        path = os.path.join(self.state, VERIFY_FILE)
        attempts = _read_json(path, {}, strict=False)
        attempts = attempts if isinstance(attempts, dict) else {}
        try:
            pool, held = self._verify_pool(attempts)
        except GitHubError as err:
            _say("could not list tickets to verify: %s" % err)
            return
        for lane in lanes:
            if lane in held:
                continue
            for repo, issue in list(pool):
                if needs_public_repo(lane, "") and not self._public(repo):
                    continue
                pool.remove((repo, issue))
                attempts["%s#%s" % (repo, issue["number"])] = self.now
                _write_json(path, attempts)
                self._write_verify_brief(repo, lane, issue)
                break

    def _write_verify_brief(self, repo: str, lane: str, issue: Dict[str, Any]) -> None:
        number = int(issue["number"])
        self._clear_awaiting(repo, number)
        stamp = int(self.now)
        text = tickets.render_verify_brief(
            repo, issue, lane, stamp, brain.live_criterion(issue.get("body")))
        text += self._verify_github_data(repo, issue)
        text += evidence.section(self.s.evidence_cmds, repo,
                                 brain.live_criterion(issue.get("body"), limit=None))
        claiming = self._write_brief(lane, tickets.verify_name(repo, number, stamp), text)
        os.replace(claiming, claiming[:-len(".claiming")] + ".md")
        _say("handed %s#%d to lane %s to verify" % (repo, number, lane))

    def _verify_github_data(self, repo: str, issue: Dict[str, Any]) -> str:
        """The verify brief's GitHub section: the lane has no network, so the
        station reads the ticket's merged pull requests, linked the way
        reconcile links them, with their checks, files and added tests. A
        GitHub failure is one line in the section, never a lost brief."""
        branch, pulls, error = "", [], ""
        read: List[Dict[str, Any]] = []
        try:
            branch = self._default_branch(repo)
            linked = sorted((dict(pr) for pr in self._closed_pulls(repo)
                             if _linked_pr(issue, repo, pr)),
                            key=lambda pr: str(pr.get("merged_at") or ""), reverse=True)
            # Only the pull requests the section shows are read in full.
            for pr in linked[:verify_data.MAX_PULLS]:
                sha = str(pr["merge_commit_sha"])
                runs = self.gh.rest("GET", "/repos/%s/commits/%s/check-runs?per_page=100"
                                    % (repo, sha)).data
                pr["check_runs"] = (runs.get("check_runs") if isinstance(runs, dict) else []) or []
                pr["on_branch"] = self._carries(repo, sha, quote(branch, safe="/")) \
                    if branch else None
                pr["files"] = self.gh.rest("GET", "/repos/%s/pulls/%d/files?per_page=100"
                                           % (repo, int(pr["number"]))).data or []
                read.append(pr)
            pulls = read + linked[verify_data.MAX_PULLS:]
        except (GitHubError, OSError, ValueError) as err:
            # Only fully read pull requests are shown after a failure.
            pulls = read
            error = "status %d" % err.status if isinstance(err, GitHubError) and err.status \
                else type(err).__name__
            _say("could not read GitHub data for the verify brief of %s#%s: %s"
                 % (repo, issue.get("number"), error))
        return verify_data.section(branch, str(issue.get("body") or ""), pulls, error)

    @staticmethod
    def _note_text(raw: Any) -> str:
        """Lane-written text made safe to post: no credentials, paths, mentions
        or non-ASCII, and nothing the text checks refuse."""
        clean = SECRET_RE.sub("<redacted>", scrub(str(raw)))
        clean = _clip(clean.replace("@", "(at)").encode("ascii", "replace").decode(), NOTE_MAX)
        try:
            _text.check(clean)
        except ValueError:
            return "(withheld: the text failed the text checks)"
        return clean

    def _verify_note(self, repo: str, number: int, lane: str, why: str) -> None:
        self._comment(repo, number, "Lane %s checked this shipped ticket for proof and could "
                      "not verify it. %s The ticket stays shipped; it is tried again after "
                      "%d hours." % (lane, why, VERIFY_INTERVAL // 3600))
        _say("verify of %s#%d left it shipped: %s" % (repo, number, why))

    def _await_evidence(self, repo: str, number: int, missing: Any) -> None:
        """Mark a ticket a verifier found waiting on live evidence, so it does
        not hold the goal's unproven-work limit while the world catches up. It
        stays shipped and is tried again after its cooldown, and the label is
        cleared when that retry is handed out so each run re-earns it. The
        reason is lane output: it must be a concrete sentence, not a bare word
        (brain.AWAITING_CAP_FACTOR caps how many tickets a goal may exclude)."""
        if not isinstance(missing, str) or len(missing.strip()) < MISSING_MIN:
            _say("not labelling %s#%d: the missing evidence is not stated" % (repo, number))
            return
        try:
            self.gh.rest("POST", "/repos/%s/issues/%d/labels" % (repo, number),
                         {"labels": [brain.AWAITING_EVIDENCE]})
        except GitHubError as err:
            _say("could not label %s#%d as awaiting evidence: %s" % (repo, number, err))

    def _clear_awaiting(self, repo: str, number: int) -> None:
        """Drop the awaiting-evidence label when the ticket is handed out to be
        verified again, so the new run decides afresh."""
        try:
            self.gh.rest("DELETE", "/repos/%s/issues/%d/labels/%s"
                         % (repo, number, quote(brain.AWAITING_EVIDENCE, safe="")), ok=(404,))
        except GitHubError as err:
            _say("could not clear the awaiting label on %s#%d: %s" % (repo, number, err))

    def _read_verify(self, lane: str, name: str) -> Tuple[Optional[Dict[str, Any]], str]:
        """(the lane's verify.json, "") or (None, why it cannot be used)."""
        path = os.path.join(self.s.home, "logs", lane, name + ".verify.json")
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return None, "The run wrote no verify.json."
        except ValueError:
            return None, "Its verify.json is not valid JSON."
        if not isinstance(data, dict):
            return None, "Its verify.json is not a JSON object."
        return data, ""

    def _report_verify(self, lane: str, kind: str, repo: str, number: int, name: str) -> bool:
        """Apply a finished verify run. A failed run changes nothing: the
        ticket is due again once its cooldown is over."""
        if kind == "failed":
            _say("verify run %s failed; %s#%d stays shipped" % (name, repo, number))
            return True
        result, why = self._read_verify(lane, name)
        if result is None:
            self._verify_note(repo, number, lane, why)
        elif set(result) == {"missing"}:
            self._verify_note(repo, number, lane, "Missing evidence: %s" % self._note_text(
                result["missing"]))
            self._await_evidence(repo, number, result["missing"])
        else:
            self._apply_proof(lane, repo, number, result, self._verify_model(lane, name))
        return True

    def _verify_model(self, lane: str, name: str) -> str:
        """The model the verify run used, as the runner recorded it next to
        verify.json; "unknown" when it recorded none."""
        path = os.path.join(self.s.home, "logs", lane, name + ".verify.model")
        try:
            with open(path, encoding="utf-8") as fh:
                model = fh.read().strip()
        except FileNotFoundError:
            model = ""
        if not tickets.MODEL_RE.fullmatch(model):
            _say("verify run %s recorded no usable model; its proof says unknown" % name)
            return "unknown"
        return model

    def _apply_proof(self, lane: str, repo: str, number: int, record: Dict[str, Any],
                     model: str) -> None:
        """Check the record with bus/proof.py, tick the criteria and move the
        ticket to verified with it, marked verified_by "<lane>/<model>"; a
        refused record leaves the ticket shipped with one note."""
        issue = self._issue(repo, number)
        names = _label_names(issue)
        if issue.get("state") != "open" or "loom:shipped" not in names or "loom:verified" in names:
            _say("%s#%d is no longer shipped; its verify result is dropped" % (repo, number))
            return
        body = str(issue.get("body") or "")
        ticked = proof.tick(body)
        try:
            proof.check(record, ticked)
        except ValueError as err:
            self._verify_note(repo, number, lane, "Its proof was refused: %s." % self._note_text(err))
            return
        if _text.CREDENTIAL_RE.search(proof.render(record, ticked)):
            self._verify_note(repo, number, lane, "Its proof holds text shaped like a credential.")
            return
        unseen = unseen_live_evidence(record)
        if unseen:
            self._verify_note(repo, number, lane, "Its proof was refused: %s." % unseen)
            return
        self.gh.rest("PATCH", "/repos/%s/issues/%d" % (repo, number), {"body": ticked})
        try:
            brain.move(self.gh, repo, number, "verified", record=record,
                       verified_by="%s/%s" % (lane, model))
        except ValueError as err:
            self.gh.rest("PATCH", "/repos/%s/issues/%d" % (repo, number), {"body": body})
            self._verify_note(repo, number, lane, "Its proof was refused: %s." % self._note_text(err))
            return
        _say("verified %s#%d from the proof lane %s gathered" % (repo, number, lane))

    def _record_slot_sample(self, idle: bool) -> None:
        if not self.s.metrics_goal:
            return
        path = os.path.join(self.state, "slot-samples.jsonl")
        os.makedirs(self.state, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": self.now, "idle": idle}) + "\n")

    def _dependency(self, repo: str, number: int) -> Optional[Dict[str, Any]]:
        """The ticket an After: line names, read once a pass and kept for the
        next lane's pass; None when it cannot be read, which holds."""
        key = "%s#%d" % (repo, number)
        if key not in self._depends:
            try:
                self._depends[key] = self._issue(repo, number)
            except GitHubError as err:
                _say("could not read %s: %s" % (key, err))
                self._depends[key] = None
        return self._depends[key]

    def _release_hold(self, repo: str, issue: Dict[str, Any],
                      released: List[tickets.After]) -> None:
        """Say on the ticket that an After: line no longer holds it, once per
        ticket that was closed as not planned."""
        number = int(issue["number"])
        said = {m.group(1) for c in self.gh.paginate(
            "/repos/%s/issues/%d/comments" % (repo, number)) if isinstance(c, dict)
            for m in [AFTER_RELEASE_RE.match(str(c.get("body") or ""))] if m}
        for dep in released:
            if str(dep.number) in said:
                continue
            self.gh.rest("POST", "/repos/%s/issues/%d/comments" % (repo, number), {
                "body": "<!-- loom:after-release %d -->\nNot held by After: any more: "
                        "%s was closed as not planned, so this ticket no longer waits "
                        "for it." % (dep.number, dep.short)})
            _say("%s#%d is no longer held by %s, closed as not planned"
                 % (repo, number, dep.short))

    def _try_ticket(self, repo: str, lane: str, issue: Dict[str, Any],
                    free: List[str]) -> bool:
        if RUNAWAY in _label_names(issue):
            return False
        if self._owned_elsewhere(repo, issue):
            return False
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
        if self._goal_elsewhere(repo, issue):
            return False
        waiting, released = tickets.after_holds(repo, headers, self._dependency)
        if released and not waiting:
            self._release_hold(repo, issue, released)
        if waiting:
            _say("%s#%s waits for %s" % (repo, issue["number"], ", ".join(
                "%s (%s)" % (a.short, a.why) for a in waiting)))
            return False
        fresh = self._issue(repo, int(issue["number"]))
        labelled_lane = _lane_of(issue)
        if not self._still_ours(fresh, labelled_lane or lane):
            _say("%s#%s changed before the claim; leaving it" % (repo, issue["number"]))
            return False
        if self._owned_elsewhere(repo, fresh):
            return False
        if (fresh.get("body") or "") != (issue.get("body") or "") \
                or fresh.get("updated_at") != issue.get("updated_at"):
            _say("%s#%s changed since it was checked; next pass" % (repo, issue["number"]))
            return False
        model = headers.get("Model", "")
        if model and labelled_lane and not lane_takes_model(labelled_lane, model):
            lane = self._route_model_claim(repo, fresh, labelled_lane, model, free)
            if lane is None:
                return False
        return self._claim(repo, lane, fresh)

    def _route_model_claim(self, repo: str, issue: Dict[str, Any], lane: str,
                           model: str, free: List[str]) -> Optional[str]:
        target = next((candidate for candidate in model_lanes(model)
                       if candidate in free and self._inbox_empty(candidate)
                       and (not needs_public_repo(candidate, model) or self._public(repo))), None)
        number = int(issue["number"])
        names = [name for name in _label_names(issue) if not name.startswith("lane:")]
        if target is None:
            names = [name for name in names if name != BRIEFED] + ["loom:queued"]
            self.gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number),
                         {"labels": names})
            self._comment(repo, number, "Model %s waits for a free compatible lane." % model)
            _say("%s#%d waits: no free lane takes model %s" % (repo, number, model))
            return None
        names.append("lane:" + target)
        self.gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number),
                     {"labels": names})
        issue["labels"] = [{"name": name} for name in names]
        _say("moved %s#%d from lane %s to lane %s for model %s"
             % (repo, number, lane, target, model))
        return target

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
        refs = _refs_numbers(repo, str(pr.get("body") or ""))
        if refs:
            return refs[0]
        head = str((pr.get("head") or {}).get("ref") or "")
        branch = re.match(r"loom/([0-9]+)(?:-|$)", head)
        return int(branch.group(1)) if branch else None

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
        if target == "loom:merged" and "loom:regression" in labels:
            events = self.gh.paginate("/repos/%s/issues/%d/events" % (repo, number))
            if tickets.merged_before_regression_requeue(ticket, pr.get("merged_at"), events):
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
        # Only a closed pull request changes a ticket. Those PRs are the most
        # recently updated closed ones, so one page of them is read, never the
        # repo's whole PR history. A merged one delivers the ticket even while
        # a second PR for it is still open (a duplicate lane run would
        # otherwise keep taking fix rounds on work already on main). A merge
        # before a regression re-queue cannot deliver its newer attempt. A
        # closed unmerged PR only counts with no open PR left.
        open_prs = [p for p in self._open_pulls(repo)
                    if isinstance(p, dict) and p.get("state", "open") == "open"]
        trusted_authors = {name.lower() for name in self.s.authors}
        for pr in open_prs:
            author = tickets._login(pr.get("user"))
            head_repo = str(((pr.get("head") or {}).get("repo") or {}).get("full_name") or "")
            if author not in trusted_authors or head_repo != repo:
                continue
            number = self._pr_ticket_number(repo, pr)
            if number is None:
                continue
            ticket = tickets_by_number.get(number)
            if ticket is None:
                try:
                    ticket = self._issue(repo, number)
                except GitHubError as err:
                    if err.status == 404:
                        continue
                    raise
                if "pull_request" in ticket or "loom:ticket" not in _label_names(ticket):
                    continue
                tickets_by_number[number] = ticket
            labels = _label_names(ticket)
            if not set(labels).intersection(("loom:queued", "loom:briefed")):
                continue
            if not _linked_pr(ticket, repo, pr):
                continue
            trusted, _ = self._check_trust(repo, ticket)
            if not trusted:
                continue
            number = int(ticket["number"])
            kept = [name for name in labels if name not in ("loom:queued", "loom:briefed")]
            kept.append("loom:pr")
            self.gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number),
                         {"labels": kept})
            ticket["labels"] = [{"name": name} for name in kept]
            self._comment(repo, number, "Linked pull request #%d is open." % int(pr["number"]))
        if not tickets_by_number:
            return
        waiting = {n: t for n, t in tickets_by_number.items()
                   if not any(_linked_pr(t, repo, p) for p in open_prs)}
        closed = self.gh.rest("GET", "/repos/%s/pulls?state=closed&sort=updated"
                              "&direction=desc&per_page=100" % repo).data
        for pr in closed if isinstance(closed, list) else []:
            if isinstance(pr, dict) and pr.get("state") == "closed":
                pool = tickets_by_number if self._terminal_label(pr) == "loom:merged" else waiting
                self._reconcile_pr_ticket(repo, pr, pool)

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

    # -- adopting pull requests no ticket links ----------------------------
    def adopt_untracked_pulls(self) -> None:
        """File an adoption ticket for the oldest open pull request in each
        scanned repo that no open ticket links, at most one per repo a pass
        (decision 0002, item 1). The station never closes the pull request:
        the lane that takes the ticket does. A GitHub failure on one repo is
        logged and the pass moves on."""
        path = os.path.join(self.state, ADOPTIONS_FILE)
        ledger = _read_json(path, {}, strict=False)
        if not isinstance(ledger, dict):
            ledger = {}
        for repo in self.scan_repos():
            rec = ledger.get(repo)
            rec = rec if isinstance(rec, dict) else {}
            try:
                self._adopt_repo(repo, rec)
            except (GitHubError, OSError) as err:
                _say("could not adopt pull requests for %s: %s" % (repo, err))
            ledger[repo] = rec
        _write_json(path, {r: v for r, v in ledger.items() if r in self.s.repos})

    def _adopt_repo(self, repo: str, rec: Dict[str, Any]) -> None:
        pulls = self._adoptable_pulls(repo)
        # The open tickets are read only when a pull request may need one.
        open_tickets: List[Dict[str, Any]] = []
        if pulls:
            issues = self.gh.paginate("/repos/%s/issues?labels=loom:ticket&state=open"
                                      "&per_page=100" % repo)
            open_tickets = [i for i in issues if isinstance(i, dict) and "pull_request" not in i
                            and "loom:ticket" in _label_names(i)]
        pulls = [pr for pr in pulls if not any(_linked_pr(t, repo, pr) for t in open_tickets)]
        # The pull requests known to be adopted: remembered from an earlier
        # pass, or named by an open ticket's ADOPTS_MARKER. Only those still
        # open and untracked are kept.
        numbers = [int(pr["number"]) for pr in pulls]
        known = {n for n in rec.get("adopted") or [] if isinstance(n, int) and n in numbers}
        known |= {n for n in numbers
                  if any(ADOPTS_MARKER % n in str(t.get("body") or "") for t in open_tickets)}
        rec["adopted"] = sorted(known)
        if not self._adoption_backing_off(rec):
            self._adopt_one(repo, rec, [pr for pr in pulls if int(pr["number"]) not in known])
        count = [len(pulls), len(rec["adopted"])]
        if rec.get("count") != count:
            _say("adoption %s: %d open pull requests no ticket links, %d of them adopted"
                 % (repo, count[0], count[1]))
            rec["count"] = count

    def _adoption_backing_off(self, rec: Dict[str, Any]) -> bool:
        """The repo's last brief was refused less than ADOPT_RETRY ago: no
        comments are read and nothing is briefed until then."""
        at = rec.get("refused_at")
        return (bool(rec.get("refused")) and self._num(at)
                and 0 <= self.now - at < ADOPT_RETRY)

    def _adopt_one(self, repo: str, rec: Dict[str, Any], waiting: List[Dict[str, Any]]) -> None:
        """Adopt the oldest waiting pull request that carries no adopted
        marker. Comments are read oldest first and only until that one is
        found; a marked pull request is remembered, so its comments are read
        once."""
        pr = None
        for candidate in waiting:
            if not self._marked_adopted(repo, int(candidate["number"])):
                pr = candidate
                break
            rec["adopted"] = sorted(set(rec["adopted"]) | {int(candidate["number"])})
        if pr is None:
            rec.pop("refused", None)
            rec.pop("refused_at", None)
            return
        number = int(pr["number"])
        goal = self._adoption_goal(repo)
        if goal is None:
            self._adoption_refused(repo, rec, "no goal home to file adoption tickets on")
            return
        try:
            url = brain.brief(self.gh, repo, goal, self._adoption_title(repo, pr),
                              adoption_body(number, goal), difficulty="medium")
        except ValueError as err:
            # The goal is at its cap or holds too much unproven work: tried
            # again after ADOPT_RETRY.
            self._adoption_refused(repo, rec, "#%d: %s" % (number, err))
            return
        rec.pop("refused", None)
        rec.pop("refused_at", None)
        rec["adopted"] = sorted(set(rec["adopted"]) | {number})
        ticket = url.rstrip("/").rpartition("/")[2]
        if not ticket.isdigit():
            # The ticket's ADOPTS_MARKER still keeps the next pass from
            # filing a second one.
            _say("adopted %s#%d, but GitHub named no ticket number to mark it with" % (repo, number))
            return
        self._comment(repo, number, adoption_comment(repo, int(ticket)))
        _say("adopted %s#%d as ticket %s#%s" % (repo, number, repo, ticket))

    def _adoptable_pulls(self, repo: str) -> List[Dict[str, Any]]:
        """The repo's open pull requests that are not drafts and come from a
        trusted author on a branch of the repo itself, oldest first. Whether
        an open ticket links one (a Refs line or a loom/N branch) is checked
        by the caller."""
        authors = {a.lower() for a in self.s.authors}
        found = [pr for pr in self._open_pulls(repo)
                 if isinstance(pr, dict) and pr.get("state", "open") == "open"
                 and not pr.get("draft") and tickets._login(pr.get("user")) in authors
                 and brain._same_repo(repo, pr, {}) and not self._owned_elsewhere(repo, pr)]
        return sorted(found, key=lambda pr: (str(pr.get("created_at") or ""),
                                             int(pr.get("number") or 0)))

    def _marked_adopted(self, repo: str, number: int) -> bool:
        """A trusted author marked the pull request adopted."""
        authors = {a.lower() for a in self.s.authors}
        comments = self.gh.paginate("/repos/%s/issues/%d/comments" % (repo, number))
        return any(isinstance(c, dict) and tickets._login(c.get("user")) in authors
                   and ADOPTED_RE.search(str(c.get("body") or "")) for c in comments)

    def _adoption_goal(self, repo: str) -> Optional[int]:
        """The goal home the repo's adoption tickets are filed under: its
        oldest open goal home that is not a backlog, else its backlog home;
        None when the repo has none."""
        homes = sorted((str(i.get("title") or "").startswith("Backlog:"), int(i["number"]))
                       for r, i in self._goal_homes() if r == repo)
        return homes[0][1] if homes else None

    @staticmethod
    def _adoption_title(repo: str, pr: Dict[str, Any]) -> str:
        """"Land or supersede #N: <title>", or without the title when it is
        text a brief refuses (a closing keyword, another repo's issue)."""
        bare = ADOPT_TITLE % int(pr["number"])
        title = " ".join(str(pr.get("title") or "").split())
        if not title:
            return bare
        full = "%s: %s" % (bare, _clip(title, ADOPT_TITLE_MAX))
        try:
            _text.check(full)
        except ValueError:
            return bare
        if any(m.group(1).lower() != repo.lower() for m in brain._REPO_REF.finditer(full)):
            return bare
        return full

    def _adoption_refused(self, repo: str, rec: Dict[str, Any], why: str) -> None:
        if rec.get("refused") != why:
            _say("adoption %s: could not file an adoption ticket: %s" % (repo, why))
        rec["refused"] = why
        rec["refused_at"] = self.now

    # -- hand-made reverts -----------------------------------------------
    def _closed_pulls(self, repo: str) -> List[Dict[str, Any]]:
        """Every merged pull request on the newest SHIP_PAGES closed-PR
        pages, cached for the pass. Unlike _merged_pulls this never stops
        early: a ticket is only fully reverted when none of its pull
        requests survives."""
        if repo not in self._closed_cache:
            pulls: List[Dict[str, Any]] = []
            url: Optional[str] = ("/repos/%s/pulls?state=closed&sort=updated"
                                  "&direction=desc&per_page=100" % repo)
            for _ in range(SHIP_PAGES):
                resp = self.gh.rest("GET", url)
                for pr in resp.data if isinstance(resp.data, list) else []:
                    if isinstance(pr, dict) and pr.get("merged_at") \
                            and COMMIT_RE.fullmatch(str(pr.get("merge_commit_sha") or "")):
                        pulls.append(pr)
                url = _next_link(resp.headers.get("link", ""))
                if not url:
                    break
            self._closed_cache[repo] = pulls
        return self._closed_cache[repo]

    def _default_branch(self, repo: str) -> str:
        """The repo's default branch, cached for the pass."""
        if repo not in self._branch_cache:
            data = self.gh.rest("GET", "/repos/%s" % repo).data or {}
            self._branch_cache[repo] = str(data.get("default_branch") or "")
        return self._branch_cache[repo]

    def _hand_reverts(self, repo: str, pulls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """The hand-made revert pull requests among `pulls`, newest first."""
        branch = self._default_branch(repo)
        if not branch:
            return []
        return [pr for pr in pulls if revert.is_hand_revert(pr, self.now, branch)]

    def _ticket_pulls(self, repo: str, wanted: set) -> Dict[int, List[Dict[str, Any]]]:
        """Every merged pull request of each wanted ticket."""
        groups: Dict[int, List[Dict[str, Any]]] = {}
        for pr in self._closed_pulls(repo):
            number = self._pr_ticket_number(repo, pr)
            if number in wanted:
                groups.setdefault(number, []).append(pr)
        return groups

    def _undone_by(self, pr: Dict[str, Any], hands: List[Dict[str, Any]],
                   revision: str,
                   carries: Callable[[str, str], bool]) -> Optional[Dict[str, Any]]:
        """The hand-made revert that undid the merged pull request on the
        default branch and that `revision` carries, or None."""
        for hand in hands:
            if revert.undoes(hand, pr) \
                    and carries(str(hand.get("merge_commit_sha") or ""), revision):
                return hand
        return None

    def _all_reverted(self, prs: List[Dict[str, Any]], hands: List[Dict[str, Any]],
                      revision: str,
                      carries: Callable[[str, str], bool]) -> bool:
        """Every one of the ticket's merged pull requests was undone by a
        hand-made revert the revision carries."""
        return bool(prs) and bool(hands) and all(
            self._undone_by(pr, hands, revision, carries) is not None for pr in prs)

    def unwind_reverts(self) -> None:
        """Move each open merged or shipped ticket without a proof whose
        every merged pull request a hand-made revert undid on the default
        branch back to loom:briefed on its lane, once per revert pull
        request, so the next attempt starts from the review findings."""
        allowed = {a.lower() for a in self.s.authors}
        for repo in [r for r in self.s.repos if tickets.REPO_RE.fullmatch(r)]:
            try:
                self._unwind_repo(repo, allowed)
            except (GitHubError, OSError, ValueError) as err:
                _say("unwind_reverts %s failed: %s" % (repo, err))

    def _unwind_repo(self, repo: str, allowed: set) -> None:
        landed: Dict[int, Dict[str, Any]] = {}
        for label in ("loom:merged", "loom:shipped"):
            data = self.gh.paginate("/repos/%s/issues?labels=%s&state=open&per_page=100"
                                    % (repo, label))
            for issue in data:
                names = _label_names(issue)
                if isinstance(issue, dict) and "pull_request" not in issue \
                        and "loom:ticket" in names and label in names \
                        and "loom:verified" not in names:
                    landed[int(issue["number"])] = issue
        if not landed:
            return
        pulls = self._closed_pulls(repo)
        hands = self._hand_reverts(repo, pulls)
        if not hands:
            return
        groups = self._ticket_pulls(repo, set(landed))
        for number in sorted(landed):
            prs = groups.get(number, [])
            if self._all_undone(prs, hands):
                self._unwind_ticket(repo, landed[number], prs, hands, allowed)

    def _match(self, pr: Dict[str, Any],
               hands: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """The hand-made revert that undid the merged pull request, or None."""
        return next((hand for hand in hands if revert.undoes(hand, pr)), None)

    def _all_undone(self, prs: List[Dict[str, Any]],
                    hands: List[Dict[str, Any]]) -> bool:
        """Every one of the ticket's merged pull requests was undone by a
        hand-made revert on the default branch."""
        return bool(prs) and all(self._match(pr, hands) is not None for pr in prs)

    def _unwind_ticket(self, repo: str, issue: Dict[str, Any],
                       prs: List[Dict[str, Any]], hands: List[Dict[str, Any]],
                       allowed: set) -> None:
        number = int(issue["number"])
        lane = factory._lane(issue)
        if not lane:
            _say("%s#%d is fully reverted but has no lane; left for the brain" % (repo, number))
            return
        undone = {int(hand["number"]): hand for pr in prs
                  for hand in [self._match(pr, hands)] if hand is not None}
        comments = self.gh.paginate("/repos/%s/issues/%d/comments" % (repo, number))
        seen = {m.group(1) for c in comments if isinstance(c, dict)
                and str((c.get("user") or {}).get("login") or "").lower() in allowed
                for m in [re.search(r"loom:hand-revert (\d+)", str(c.get("body") or ""))]
                if m}
        fresh = {n: hand for n, hand in undone.items() if str(n) not in seen}
        if not fresh:
            return
        markers = "\n".join(revert.HAND_REVERT_MARKER % n for n in sorted(fresh))
        names = ", ".join("#%d" % n for n in sorted(fresh))
        whys = []
        for n in sorted(fresh):
            why = revert.why_section(str(fresh[n].get("body") or ""))
            quoted = "\n".join(brain._quoted(why)) if why else "    (no Why section)"
            whys.append("Pull request #%d's Why:\n%s" % (n, quoted))
        note = ("%s\nA hand-made revert pull request undid every merged pull request of "
                "this ticket on the default branch (%s), so it goes back to lane %s to "
                "redo the change.\n\n%s" % (markers, names, lane, "\n\n".join(whys)))
        brain._check_written_text(note)
        brain.move(self.gh, repo, number, "briefed", lane=lane, note=note)
        _say("%s: hand reverts %s undid #%d; moved back to lane %s"
             % (repo, names, number, lane))

    # -- shipping at deploy ----------------------------------------------
    def _deployed(self) -> Optional[Tuple[str, float]]:
        """The revision lanes/mac/station.sh last deployed and when, or None
        when it has recorded no successful deploy (LOOM_DEPLOY=0, or none yet)."""
        rec = _read_json(os.path.join(self.state, DEPLOYED_FILE), None, strict=False)
        if not isinstance(rec, dict):
            return None
        revision, at = rec.get("revision"), rec.get("at")
        if not isinstance(revision, str) or not COMMIT_RE.fullmatch(revision) \
                or isinstance(at, bool) or not isinstance(at, (int, float)):
            return None
        return revision, float(at)

    def _merged_pulls(self, repo: str, wanted: set,
                      seen: Optional[List[Dict[str, Any]]] = None) -> Dict[int, Dict[str, Any]]:
        """Each wanted ticket's latest merged pull request, found by its Refs
        line or loom branch in the most recently updated closed pull requests
        (the URL reconciliation reads), stopping once every one is found.
        Every merged pull request read is added to seen when it is given."""
        found: Dict[int, Dict[str, Any]] = {}
        url: Optional[str] = ("/repos/%s/pulls?state=closed&sort=updated"
                              "&direction=desc&per_page=100" % repo)
        for _ in range(SHIP_PAGES):
            resp = self.gh.rest("GET", url)
            for pr in resp.data if isinstance(resp.data, list) else []:
                if not isinstance(pr, dict) or not pr.get("merged_at") \
                        or not COMMIT_RE.fullmatch(str(pr.get("merge_commit_sha") or "")):
                    continue
                if seen is not None:
                    seen.append(pr)
                number = self._pr_ticket_number(repo, pr)
                if number in wanted and (number not in found or str(pr["merged_at"])
                                         > str(found[number]["merged_at"])):
                    found[number] = pr
            url = _next_link(resp.headers.get("link", ""))
            if not url or wanted <= set(found):
                break
        return found

    def ship_deployed(self) -> None:
        """Move each open merged ticket of the repo this checkout deploys
        (LOOM_SELF_REPO) whose pull request's merge commit the deployed
        revision carries to loom:shipped, with a note naming the revision and
        the deploy time. Other repos keep their own ship rules (docs/BRAIN.md)."""
        repo = self.s.self_repo
        deployed = self._deployed()
        if deployed is None or not tickets.REPO_RE.fullmatch(repo or ""):
            return
        revision, at = deployed
        merged = self._merged_tickets(repo)
        if not merged:
            return
        latest = self._merged_pulls(repo, merged)
        groups = self._ticket_pulls(repo, set(latest))
        hands = self._hand_reverts(repo, self._closed_pulls(repo))
        carried = sorted((str(pr["merged_at"]), number, pr)
                         for number, pr in latest.items()
                         if self.ancestor(str(pr["merge_commit_sha"]), revision)
                         and not self._all_reverted(groups.get(number) or [pr], hands,
                                                    revision, self.ancestor))
        for _, number, pr in carried[:SHIP_PER_PASS]:
            self._ship_one(repo, number, pr,
                           "the station deployed revision %s at %s" % (
                               revision[:12], self._utc(at)),
                           "deployed %s" % revision[:12])

    def _merged_tickets(self, repo: str) -> set:
        """The numbers of the repo's open merged tickets."""
        listed = self.gh.rest("GET", "/repos/%s/issues?labels=loom:merged&state=open"
                              "&per_page=100" % repo).data
        return {int(i["number"]) for i in (listed if isinstance(listed, list) else [])
                if isinstance(i, dict) and "pull_request" not in i
                and "loom:ticket" in _label_names(i)}

    def _ship_one(self, repo: str, number: int, pr: Dict[str, Any], how: str,
                  what: str) -> bool:
        """Move one merged ticket to loom:shipped with a note naming how its
        pull request's merge commit was deployed; whether it moved."""
        note = "Shipped: %s, which carries pull request #%d (merge commit %s)." % (
            how, int(pr["number"]), str(pr["merge_commit_sha"])[:12])
        try:
            brain.move(self.gh, repo, number, "shipped", note=note)
        except (GitHubError, ValueError) as err:
            _say("could not ship %s#%d: %s" % (repo, number, err))
            return False
        _say("shipped %s#%d: %s carries #%d" % (repo, number, what, int(pr["number"])))
        return True

    # -- shipping through a pin pull request ------------------------------
    def ship_pins(self) -> None:
        """Move the open merged tickets each recently merged pin pull request
        of a deployment repo (LOOM_PIN_SHIP_REPOS) deploys to loom:shipped:
        those of the source repo its published-from comment names whose pull
        request's merge commit the published revision carries, unless a later
        revert pull request the revision also carries undid it. At most
        SHIP_PER_PASS move a pass; a pin pull request is read again until
        everything it carries has moved and it merged PIN_SHIP_SETTLE ago."""
        repos = [r for r in self.s.pin_ship_repos if tickets.REPO_RE.fullmatch(r)]
        if not repos:
            return
        path = os.path.join(self.state, PIN_SHIPS_FILE)
        saved = _read_json(path, {}, strict=False)
        done = saved if isinstance(saved, dict) else {}
        cutoff = self.now - PIN_SHIP_LOOKBACK
        done = {k: v for k, v in done.items() if (self._epoch(str(v)) or 0) >= cutoff}
        left = [SHIP_PER_PASS]
        try:
            for deploy in repos:
                for pin in self._merged_pins(deploy, cutoff):
                    key = "%s#%d" % (deploy, int(pin["number"]))
                    if key in done:
                        continue
                    try:
                        finished = self._ship_pin(deploy, pin, left)
                    except GitHubError as err:
                        _say("could not ship pin pull request %s: %s" % (key, err))
                        continue
                    settled = self.now - (self._epoch(str(pin["merged_at"])) or self.now)
                    if finished and settled >= PIN_SHIP_SETTLE:
                        done[key] = str(pin["merged_at"])
        finally:
            _write_json(path, done)

    def _merged_pins(self, deploy: str, cutoff: float) -> List[Dict[str, Any]]:
        """The deployment repo's pull requests merged since cutoff, oldest
        first, from its most recently updated closed pull requests."""
        data = self.gh.rest("GET", "/repos/%s/pulls?state=closed&sort=updated"
                            "&direction=desc&per_page=100" % deploy).data
        pins = [pr for pr in (data if isinstance(data, list) else [])
                if isinstance(pr, dict) and isinstance(pr.get("number"), int)
                and (self._epoch(str(pr.get("merged_at") or "")) or 0) >= cutoff]
        return sorted(pins, key=lambda pr: str(pr["merged_at"]))

    def _ship_pin(self, deploy: str, pin: Dict[str, Any], left: List[int]) -> bool:
        """Ship what one merged pin pull request publishes; whether every
        ticket it carries has moved (a pull request that publishes nothing
        has nothing left to ship)."""
        files = self.gh.paginate("/repos/%s/pulls/%d/files" % (deploy, int(pin["number"])))
        published = sorted({m.groups() for f in files if isinstance(f, dict)
                            for m in PUBLISHED_RE.finditer(str(f.get("patch") or ""))})
        finished = True
        for name, short in published:
            source = self._pin_source(deploy, name)
            if not source:
                _say("pin pull request %s#%d publishes %s, not a watched repo" % (
                    deploy, int(pin["number"]), name))
                continue
            finished = self._ship_published(source, short, deploy, pin, left) and finished
        return finished

    def _pin_source(self, deploy: str, name: str) -> str:
        """The watched repo a published-from comment names: owner/name as
        written, else the one watched repo of that name, else the deployment
        repo's owner's."""
        if "/" in name:
            return name if self._repo_ok(name) else ""
        named = [r for r in self.s.repos if r.split("/")[-1] == name and self._repo_ok(r)]
        if len(named) == 1:
            return named[0]
        same = deploy.split("/")[0] + "/" + name
        return same if self._repo_ok(same) else ""

    def _carries(self, repo: str, commit: str, revision: str) -> bool:
        """The commit is the revision or one of its ancestors in the repo on
        GitHub (the compare API); an unknown commit is not."""
        resp = self.gh.rest("GET", "/repos/%s/compare/%s...%s" % (repo, commit, revision),
                            ok=(404,))
        data = resp.data if resp.status != 404 and isinstance(resp.data, dict) else {}
        return data.get("status") in ("ahead", "identical")

    def _ship_published(self, source: str, short: str, deploy: str, pin: Dict[str, Any],
                        left: List[int]) -> bool:
        commit = self.gh.rest("GET", "/repos/%s/commits/%s" % (source, short),
                              ok=(404, 422))
        revision = str((commit.data or {}).get("sha") or "") \
            if commit.status not in (404, 422) and isinstance(commit.data, dict) else ""
        if not COMMIT_RE.fullmatch(revision):
            _say("pin pull request %s#%d names %s merge %s, not a commit there" % (
                deploy, int(pin["number"]), source, short))
            return True
        merged = self._merged_tickets(source)
        if not merged:
            return True
        seen: List[Dict[str, Any]] = []
        pulls = self._merged_pulls(source, merged, seen)
        groups = self._ticket_pulls(source, set(pulls))
        hands = self._hand_reverts(source, self._closed_pulls(source))
        carried = sorted((str(pr["merged_at"]), number, pr) for number, pr in pulls.items()
                         if self._carries(source, str(pr["merge_commit_sha"]), revision)
                         and not self._reverted(source, pr, seen, revision)
                         and not self._all_reverted(groups.get(number) or [pr], hands,
                                                    revision, self._carries))
        how = "pin pull request %s#%d merged at %s, publishing %s revision %s" % (
            deploy, int(pin["number"]), str(pin["merged_at"]).replace("T", " "),
            source, revision[:12])
        finished = True
        for _, number, pr in carried:
            if left[0] <= 0:
                return False
            left[0] -= 1
            finished = self._ship_one(source, number, pr, how, "pin %s#%d" % (
                deploy, int(pin["number"]))) and finished
        return finished

    def _reverted(self, repo: str, pr: Dict[str, Any], seen: List[Dict[str, Any]],
                  revision: str) -> bool:
        """A later merged pull request the revision carries reverts this one:
        its body says "Reverts <repo>#<n>" or its title is 'Revert "<title>"',
        the forms GitHub's revert button writes."""
        number, title = int(pr["number"]), str(pr.get("title") or "")
        body = re.compile(r"\bReverts\s+(?:%s)?#%d\b" % (re.escape(repo), number))
        for other in seen:
            if other is pr or str(other.get("merged_at")) <= str(pr["merged_at"]):
                continue
            if body.search(str(other.get("body") or "")) or (
                    title and str(other.get("title") or "") == 'Revert "%s"' % title):
                if self._carries(repo, str(other["merge_commit_sha"]), revision):
                    return True
        return False

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

    def release_factory_held(self) -> None:
        """Re-queue tickets a factory incident held once that incident is
        closed: they kept their rung and need no re-brief. A claim newer
        than the incident's close means the lane picked the ticket back up
        itself, so the release leaves it alone. A failed read of the
        incidents releases nothing this pass; the holds wait for the next."""
        held = self._held_tickets()
        if not held:
            return
        try:
            closed_incidents = self._closed_incidents()
        except GitHubError as err:
            _say("factory release: could not read the closed incidents: %s" % err)
            return
        for key, info in list(held.items()):
            self._release_held(key, info, closed_incidents)

    def _closed_incidents(self) -> Dict[str, Dict[str, Any]]:
        """Closed factory incidents the runner filed, by "owner/repo#n"."""
        closed: Dict[str, Dict[str, Any]] = {}
        for incident in self.gh.paginate(
                "/repos/%s/issues?state=closed&labels=%s&per_page=100" % (FACTORY_REPO, FACTORY_LABEL)):
            if isinstance(incident, dict) and INCIDENT_MARKER in str(incident.get("body") or ""):
                closed["%s#%d" % (FACTORY_REPO, incident.get("number"))] = incident
        return closed

    def _release_held(self, key: str, info: Any,
                      closed_incidents: Dict[str, Dict[str, Any]]) -> None:
        """Re-queue one held ticket if its incident is closed and named on
        it; forget it once it is no longer running."""
        repo, _, num = key.rpartition("#")
        number = int(num) if num.isdigit() else 0
        ref = info.get("incident") if isinstance(info, dict) else None
        incident = closed_incidents.get(ref) if ref else None
        try:
            issue = self.gh.rest("GET", "/repos/%s/issues/%d" % (repo, number)).data
            comments = self.gh.paginate(
                "/repos/%s/issues/%d/comments?per_page=100" % (repo, number))
        except GitHubError as err:
            _say("factory release: could not read %s#%d: %s" % (repo, number, err))
            return
        if not isinstance(issue, dict) or RUNNING not in _label_names(issue):
            self._forget_held(key)
            return
        if incident is None or not any(isinstance(c, dict) and ref in str(c.get("body") or "")
                                       for c in comments):
            return
        if self._claimed_since(comments, incident):
            return
        self._relabel(repo, number, "loom:queued")
        self._comment(repo, number,
                      "Factory incident %s is closed: the ticket is back in the queue "
                      "on its lane." % ref)
        self._forget_held(key)
        _say("factory release: %s#%d re-queued after incident %s closed" % (repo, number, ref))

    def _claimed_since(self, comments: List[Any], incident: Dict[str, Any]) -> bool:
        """A lane claim comment is newer than the incident's close."""
        from . import factory as _factory
        closed = self._epoch(str(incident.get("closed_at") or ""))
        return closed is not None and any(
            isinstance(c, dict)
            and _factory.CLAIMED_RE.fullmatch(str(c.get("body") or "").strip())
            and (self._epoch(str(c.get("created_at") or "")) or 0) > closed
            for c in comments)

    def _held_tickets(self) -> Dict[str, Any]:
        """Tickets the station held for a factory incident and has not yet
        re-queued: key "%s#%d" to {"incident": "owner/repo#n"}."""
        held = _read_json(os.path.join(self.state, "factory_held.json"), {})
        return held if isinstance(held, dict) else {}

    def _note_held(self, repo: str, number: int, incident: Optional[str]) -> None:
        held = self._held_tickets()
        held["%s#%d" % (repo, number)] = {"incident": incident, "at": self.now}
        _write_json(os.path.join(self.state, "factory_held.json"), held)

    def _forget_held(self, key: str) -> None:
        held = self._held_tickets()
        if key in held:
            del held[key]
            _write_json(os.path.join(self.state, "factory_held.json"), held)

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
        if brain._pending_brain_fix(comments, self.s.authors):
            self._move_fix_to_lane(repo, number, lane, names)
            return
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
        if self._field(text, "Deliverable") == "verify":
            return self._report_verify(lane, kind, repo, number, name)
        if kind == "done":
            if self._field(text, "Deliverable") == "answer":
                _say("reported %s#%d answer posted by lane %s" % (repo, number, lane))
                return True
            if not self._field(text, "Fix-PR") and self._answered(lane, name):
                prs = {int(n) for n in NAMED_PR_RE.findall(text)}
                self._post_thread_answers(repo, prs, lane, name, text, None)
                _say("reported %s#%d answered by lane %s: nothing to change" % (repo, number, lane))
                return True
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
        if self._flag_repeated_failure(repo, number, lane, reason):
            return True
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
        if klass == "factory":
            # The validation environment the factory broke, not the ticket's
            # work: the ticket stays running on its lane and keeps its rung.
            # release_factory_held re-queues it when the incident closes.
            match = HELD_FOR_RE.search(bare)
            self._note_held(repo, number,
                            "%s#%s" % (match.group(1), match.group(2)) if match else None)
            self._comment(repo, number,
                          "Lane %s held: %s. The ticket keeps its rung and returns to the "
                          "queue when the factory incident closes." % (lane, bare))
            _say("held %s#%d for the factory: %s" % (repo, number, reason))
            return True
        if fix_pr:
            # A fresh attempt would open a second pull request for the ticket.
            # A work failure goes to loom:failed, where the factory climbs the
            # ladder and sends the fix to the next rung on the same pull request
            # (factory.failed). Back at loom:pr the head is unchanged, so
            # hand-back sent the same fix to the same lane every pass: 16
            # rounds in 77 minutes on 2026-10-14. A transient failure retries.
            state = "loom:failed" if klass == "work" else "loom:pr"
            self._relabel(repo, number, state)
            self._comment(repo, number, "Lane %s failed: %s. This was a fix round on pull "
                          "request #%s; the ticket is back at %s and the pull request "
                          "is unchanged." % (lane, reason, fix_pr, state))
            _say("reported %s#%d fix failed, back to %s: %s" % (repo, number, state, reason))
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

    def _flag_repeated_failure(self, repo: str, number: int, lane: str, reason: str) -> bool:
        """Hold a ticket after the same consecutive failure or three attempts in
        six hours, and record its attempt table."""
        if failure_class(reason) == "factory":
            # An incident is the factory's problem repeating, not the lane's:
            # it must never flag the ticket or cost it a rung.
            return False
        authors = {author.lower() for author in self.s.authors}
        comments = [c for c in self.gh.paginate("/repos/%s/issues/%d/comments" % (repo, number))
                    if isinstance(c, dict) and tickets._login(c.get("user")).lower() in authors]
        attempts = factory.attempts(comments, reason)
        failures = [a[3] for a in attempts[:-1] if a[3]]
        reason_repeat = bool(failures) and failures[-1] == reason
        recent = [a for a in attempts if a[0] >= self.now - 6 * 3600]
        if not reason_repeat and len(recent) < 3:
            return False
        issue = self._issue(repo, number)
        self.gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number),
                     {"labels": sorted(set(_label_names(issue)) | {RUNAWAY})})
        trigger = "repeated failure reason" if reason_repeat else "three attempts within six hours"
        self._comment(repo, number, factory.runaway_comment(
            trigger, attempts, factory.issue_model(issue)))
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
        """Post each answer a fix round's lane wrote instead of a fix. Only a
        thread the brief asked about, on its pull request, gets one."""
        asked = set(factory.FIX_THREAD_RE.findall(brief))
        self._post_thread_answers(repo, {pr}, lane, name, brief, asked)

    def _answered(self, lane: str, name: str) -> bool:
        """Did the runner report this run answered: it committed nothing
        because its agent found the work already done or no longer applying
        (lanes/run_lane.sh, no_change_reason)? Read from the runner's marker
        file, never from the log, which carries the agent's own output."""
        return os.path.isfile(os.path.join(self.s.home, "logs", lane, name + ".answered"))

    def _post_thread_answers(self, repo: str, prs: Set[int], lane: str, name: str,
                             brief: str, asked: Optional[Set[str]]) -> None:
        """Post each answer the lane wrote as the reply on its thread: an open
        thread on one of prs and, unless asked is None, one the brief asked
        about. It is resolved when the round ran on a ladder model and the
        thread is the review bot's own medium or low finding; otherwise it
        stays open, and the factory reads it as a finding that survived."""
        folder = os.path.join(self.inbox(lane), "answers", name)
        try:
            entries = sorted(os.listdir(folder))
        except OSError:
            return
        strong = self._field(brief, "Model") in STRONG_MODELS
        try:
            for entry in entries:
                m = ANSWER_FILE_RE.match(entry)
                if not (m and (asked is None or m.group(1) in asked)):
                    continue
                try:
                    self._answer(repo, prs, lane, m.group(1), os.path.join(folder, entry), strong)
                except GitHubError as err:
                    # One thread's failure must not stop the rest, and the
                    # report must still be recorded: a retried report would
                    # post the answers already posted a second time.
                    _say("answer to %s on %s failed: %s" % (m.group(1), repo, err))
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def _answer(self, repo: str, prs: Set[int], lane: str, thread_id: str, path: str,
                strong: bool) -> None:
        said = _read_answer(path)
        node = (self.gh.graphql(THREAD_QUERY, {"id": thread_id}) or {}).get("node") or {}
        pr = next((n for n in sorted(prs) if _open_on(node, repo, n)), None)
        if not said or pr is None:
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
            if lane in REMOTE_LANES:
                at = self._remote_heartbeat(lane)
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
        body = "\n".join(
            [LANES_MARKER, "Updated " + self._utc(self.now), "",
             "| lane | available | heartbeat (UTC) |", "|---|---|---|"]
            + ["| %s | %s | %s |" % row for row in rows]
            + ["", "Remote claims: muse %d/%d" % (
                self._remote_claim_count("muse"), REMOTE_LANES["muse"]["slots"])]
            + (["", summary.line()] if summary is not None else []))
        return body

    def _remote_claim_count(self, lane: str) -> int:
        saved = _read_json(os.path.join(self.state, "remote-status.json"), {}, strict=False)
        cached = saved.get(lane) if isinstance(saved, dict) else None
        if (isinstance(cached, dict) and isinstance(cached.get("checked_at"), (int, float))
                and 0 <= self.now - cached["checked_at"] <= HEARTBEAT_MAX_AGE):
            claims = cached.get("claims")
            return int(claims) if isinstance(claims, int) else 0
        if self._remote_heartbeat(lane) is None:
            return 0
        saved = _read_json(os.path.join(self.state, "remote-status.json"), {}, strict=False)
        cached = saved.get(lane) if isinstance(saved, dict) else None
        return int(cached.get("claims", 0)) if isinstance(cached, dict) else 0

    def _read_remote_claim_count(self, lane: str) -> int:
        count = 0
        for repo in self.s.repos:
            found = self.gh.rest("GET", "/repos/%s/issues?state=open&labels=loom:running,lane:%s"
                                 % (repo, lane)).data
            if isinstance(found, list):
                count += sum(1 for item in found if isinstance(item, dict)
                             and "pull_request" not in item)
        return count

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
        found = [i for i in (data if isinstance(data, list) else [])
                 if isinstance(i, dict) and "pull_request" not in i
                 and str(i.get("number")).isdigit()]
        # A goal home that names another operator is that operator's Loom's.
        ours = []
        for issue in found:
            if tickets.goal_ours(issue, self.s.authors):
                ours.append(issue)
            else:
                self._note_owned("%s#%s" % (repo, issue["number"]),
                                 "goal home names operator %s" % tickets.goal_operator(issue))
        return ours

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

    def _due_digest(self) -> Optional[Tuple[str, str]]:
        """(slot id, label) of the latest digest time today that has passed,
        or None before the first one."""
        local = time.localtime(self.now)
        passed = [(h, m) for h, m in self.s.report_digest
                  if (local.tm_hour, local.tm_min) >= (h, m)]
        if not passed:
            return None
        h, m = passed[-1]
        label = "morning" if h < 12 else ("afternoon" if h < 18 else "evening")
        return "%s %02d:%02d" % (time.strftime("%Y-%m-%d", local), h, m), label

    def _digest_lines(self, since: str, after_id: int = 0) -> Tuple[List[str], int, int]:
        """One line per goal home whose latest report since `since` (and with
        a comment id above after_id, so no report is told twice) says
        something moved, the count of goal homes with nothing to say, and the
        highest report id read."""
        moving, quiet, top = [], 0, after_id
        allowed = {a.lower() for a in self.s.authors}
        for repo in self.s.repos:
            if not tickets.REPO_RE.fullmatch(repo):
                continue
            issues = self._goal_issues(repo)
            latest: Dict[int, Tuple[Dict[str, Any], Dict[str, Any]]] = {}
            for ident, issue, comment in self._report_comments(repo, issues, since):
                author = str((comment.get("user") or {}).get("login") or "").lower()
                if author in allowed and ident > after_id:
                    latest[int(issue["number"])] = (issue, comment)
                    top = max(top, ident)
            quiet += len(issues) - len(latest)
            for issue, comment in latest.values():
                body = self._report_body(comment)
                first = next((x.lstrip("#").strip() for x in body.splitlines()
                              if x.lstrip("#").strip()), "")
                if not first or first.lower().startswith("nothing moved"):
                    quiet += 1
                    continue
                moving.append("- **%s** %s <%s>" % (
                    self._short_name(issue), first[:SHORT_LINE_MAX],
                    self._report_link(issue, comment)))
        return moving, quiet, top

    def _digest_factory_line(self) -> str:
        ref = re.fullmatch(r"([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)#([1-9][0-9]*)",
                           self.s.metrics_goal)
        if not ref:
            return ""
        path = "/repos/%s/issues/%d/comments" % (ref.group(1), int(ref.group(2)))
        dated = self._dated_metrics(path)
        if not dated:
            return ""
        day = max(dated)
        passed, streak = dated[day]
        return "Factory %s: %d/9 numbers pass; streak %d of 7." % (day, passed, streak)

    def _digest_send(self, text: str) -> bool:
        """Every chunk of the digest to the channel, as the bot or through
        the webhook; False when any chunk failed or the secret is unreadable."""
        if self._bot_reports():
            token = self._bot_token()
            if token is None:
                return False
            for part in self._chunks(text):
                ok, _ = self._bot(token, "POST", "/channels/%s/messages" % self.s.channel_id,
                                  {"content": part, "allowed_mentions": {"parse": []},
                                   "flags": SUPPRESS_EMBEDS})
                if not ok:
                    return False
            return True
        url = self._webhook()
        if url is None:
            return False
        return all(self._send(url, part, mention=False) for part in self._chunks(text))

    def send_report_digest(self) -> None:
        """At each digest time, one message: the goals that moved since the
        last digest with their report's first line and link, a count of the
        quiet ones, and the factory's daily numbers. Sent once per time; a
        failed send is tried again on the next pass."""
        due = self._due_digest()
        path = os.path.join(self.state, "reports.json")
        ledger = _read_json(path, {})
        if not isinstance(ledger, dict):
            raise StateError("state file %s does not hold an object" % path)
        if "_digest_slot" not in ledger:
            # First pass with a digest: start from now, and count a digest
            # time already past today as sent, so switching on mid-day sends
            # nothing until the next digest time.
            ledger["_digest_slot"] = due[0] if due else ""
            ledger["_digest_since"] = self.now
            _write_json(path, ledger)
            return
        if due is None:
            return
        slot, label = due
        if ledger.get("_digest_slot") == slot:
            return
        last = ledger.get("_digest_since")
        start = last if isinstance(last, (int, float)) and not isinstance(last, bool) \
            else self.now - DIGEST_FIRST_LOOKBACK
        since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start))
        seen = ledger.get("_digest_last_id")
        after = seen if isinstance(seen, int) and not isinstance(seen, bool) else 0
        moving, quiet, top = self._digest_lines(since, after)
        lines = ["**Loom %s digest** (%s)" % (label, slot[11:])]
        if moving:
            lines.append("Moving, %d goal%s:" % (len(moving), "" if len(moving) == 1 else "s"))
            lines += moving
        else:
            lines.append("No goal moved since the last digest.")
        if quiet:
            lines.append("Quiet: %d goal%s, nothing moved." % (quiet, "" if quiet == 1 else "s"))
        factory_line = self._digest_factory_line()
        if factory_line:
            lines.append(factory_line)
        if not self._digest_send("\n".join(lines)):
            _say("report digest %s not sent; next pass tries again" % slot)
            return
        ledger["_digest_slot"] = slot
        ledger["_digest_since"] = self.now
        ledger["_digest_last_id"] = top
        _write_json(path, ledger)
        _say("sent the %s report digest: %d moving, %d quiet" % (label, len(moving), quiet))

    def forward_reports(self) -> None:
        if not (self.s.webhook_ssm or self._bot_reports()):
            return
        if self.s.report_digest:
            self.send_report_digest()
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
                if key.startswith("_"):
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
        fallback = []
        repos_with_backlog = set()
        for repo, goal in homes:
            title = str(goal.get("title") or "")
            if not title.startswith("Backlog:"):
                continue
            repos_with_backlog.add(repo)
            number = int(goal["number"])
            key = "%s#%d" % (repo, number)
            keep.add(key)
            goals[key] = (repo, number, state.get(key))
        for repo, goal in homes:
            if repo in repos_with_backlog or str(goal.get("title") or "").startswith("Backlog:"):
                continue
            number = int(goal["number"])
            key = "%s#%d" % (repo, number)
            keep.add(key)
            goals[key] = (repo, number, state.get(key))
            fallback.append(key)
        if fallback:
            keep.add("_fallback")
            state["_fallback"] = fallback
        else:
            state.pop("_fallback", None)
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
        fallback = set(state.get("_fallback", []))
        for key, (repo, number, last) in goals.items():
            if self._triage_recent(last):
                continue
            try:
                if key in fallback:
                    queued = self.gh.paginate(
                        "/repos/%s/issues?labels=loom:queued&state=open&per_page=100" % repo)
                    if len(queued if isinstance(queued, list) else []) >= 3:
                        continue
                    lines = brain.triage(self.gh, repo, number, brain.TRIAGE_LIMIT,
                                         comment_limit=5)
                    counts = (sum(" queued under #" in line for line in lines),
                              sum(" loom:triage" in line for line in lines),
                              sum(" held:" in line for line in lines))
                    _say("triage %s: %d queued, %d triage, %d held (goal home)" %
                         (key, *counts))
                else:
                    self._run_triage(key, repo, number)
            except (GitHubError, OSError, ValueError) as err:
                _say("triage %s failed: %s" % (key, err))
                continue
            state[key] = self.now
        _write_json(path, state)
        self.file_validation_contracts()

    # -- agent rewrites of loom:triage issues ----------------------------
    def _rewrite_goals(self) -> List[Tuple[str, str, int]]:
        """(ledger key, repo, goal) for the backlog goal homes triage knows."""
        state = _read_json(os.path.join(self.state, "triage.json"), {}, strict=False)
        goals = []
        for key in state if isinstance(state, dict) else []:
            if key.startswith("_"):
                continue
            repo, _, num = key.rpartition("#")
            if repo and num.isdigit():
                goals.append((key, repo, int(num)))
        return goals

    def _rewrite_command(self, repo: str, cache: Dict[str, str]) -> str:
        """The agent command for a repo; "" skips it. A repo that cannot be
        shown to be public counts as private."""
        if repo not in cache:
            cache[repo] = rewrite.command_for(
                self.s.rewrite_cmd, self.s.rewrite_private_cmd, not self._public(repo))
        return cache[repo]

    def _rewrite_goal(self, key: str, repo: str, goal: int, rec: Dict[str, Any],
                      budget: int, cmds: Dict[str, str]) -> int:
        """Rewrite oldest-first what the goal's hourly allowance and the
        pass allow; returns how many rewrites ran."""
        runs = [t for t in rec.get("runs", []) if isinstance(t, (int, float))
                and 0 <= self.now - t < 3600]
        rec["runs"] = runs
        tried = rec.setdefault("tried", {})
        for number in [n for n, at in tried.items() if not isinstance(at, (int, float))
                       or not 0 <= self.now - at < REWRITE_RETRY]:
            del tried[number]
        room = min(REWRITE_PER_HOUR - len(runs), budget)
        if room <= 0:
            return 0
        cmd = self._rewrite_command(repo, cmds)
        if not cmd:
            _say("rewrite %s: no agent command for this repo; skipped" % key)
            return 0
        done = 0
        for issue in rewrite.candidates(self.gh, repo):
            if done >= room or self._deferred():
                break
            number = int(issue["number"])
            if str(number) in tried:
                continue
            runs.append(self.now)
            tried[str(number)] = self.now
            done += 1
            try:
                action = rewrite.rewrite_issue(self.gh, repo, goal, issue, cmd,
                                               REWRITE_TIMEOUT)
            except rewrite.CommandFailure as err:
                tried.pop(str(number), None)
                _say("rewrite %s#%d wrote nothing: %s" % (repo, number, err))
                continue
            except (rewrite.Invalid, ValueError, GitHubError, OSError) as err:
                _say("rewrite %s#%d wrote nothing: %s" % (repo, number, err))
                continue
            _say("rewrite %s#%d: %s" % (repo, number, action))
        return done

    def rewrite_triaged(self) -> None:
        """Rewrite loom:triage issues in backlog goal homes with the agent
        command from LOOM_REWRITE_CMD (public repos) or
        LOOM_REWRITE_PRIVATE_CMD (private ones). At most REWRITE_PER_HOUR a
        goal an hour, oldest first; every attempt is kept in rewrite.json, so
        an issue that did not become a ticket waits REWRITE_RETRY before
        another try."""
        if not (self.s.rewrite_cmd or self.s.rewrite_private_cmd):
            return
        path = os.path.join(self.state, "rewrite.json")
        state = _read_json(path, {}, strict=False)
        if not isinstance(state, dict):
            state = {}
        goals = self._rewrite_goals()
        for stale in [k for k in state if k not in {g[0] for g in goals}]:
            del state[stale]
        budget = REWRITE_PER_PASS
        cmds: Dict[str, str] = {}
        for key, repo, goal in goals:
            rec = state.get(key)
            if not isinstance(rec, dict):
                rec = state[key] = {}
            try:
                budget -= self._rewrite_goal(key, repo, goal, rec, budget, cmds)
            except (GitHubError, OSError) as err:
                _say("rewrite %s failed: %s" % (key, err))
            _write_json(path, state)
            if budget <= 0 or self._deferred():
                break

    # -- health findings -------------------------------------------------
    def health_intake(self) -> None:
        """Read each goal home's health endpoint (LOOM_HEALTH_URLS) at most
        every LOOM_HEALTH_EVERY seconds and turn its findings into ranked
        tickets (bus/health.py). bus.brain status reads the ledger it keeps
        (health.ledger_path) for its Health findings section."""
        if not self.s.health_urls:
            return
        path = health.ledger_path(self.s.home)
        state = _read_json(path, {}, strict=False)
        if not isinstance(state, dict):
            state = {}
        last = state.get("last")
        if isinstance(last, (int, float)) and 0 <= self.now - last < self.s.health_every:
            return
        sources, problems = health.parse_sources(self.s.health_urls)
        for problem in problems:
            _say("ignoring a LOOM_HEALTH_URLS entry: %s" % problem)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.now))
        health.Intake(self.gh, state, self.now, stamp, _say).run(sources)
        _write_json(path, state)

    # -- log watch -------------------------------------------------------
    def watch_logs(self) -> None:
        """Read each component's new log lines and file one loom:triage issue
        in the lanes repo per new repeated error (bus/watch.py). The logs are
        read every pass; below the budget floor filing waits for the reset."""
        repo = self.s.status_repo
        if not repo or not tickets.REPO_RE.fullmatch(repo):
            return
        watch.Watcher(self.gh, repo, self.s.watched, os.path.join(self.state, "watch.json"),
                      self.now, _say, write=not self._deferred()).run()

    # -- invariants -------------------------------------------------------
    def check_invariants(self) -> None:
        """Check the factory's own invariants on what the station reads
        (bus/invariants.py), at most every invariants.CHECK_EVERY seconds,
        and file one loom:invariant issue per breach on the invariant goal
        home. Below the budget floor nothing is filed."""
        path = os.path.join(self.state, "invariants.json")
        state = invariants.load(path, _say)
        checked = state.get("checked")
        if self._num(checked) and 0 <= self.now - checked < invariants.CHECK_EVERY:
            return
        state["checked"] = self.now
        goal = self._invariant_goal()
        self._invariant_repo = goal[0] if goal else ""
        facts = self._invariant_facts(state)
        breaches = invariants.check(facts, _say)
        if breaches and goal is None:
            _say("invariants: %d broken, but no goal home to file them on; set "
                 "LOOM_INVARIANT_GOAL" % len(breaches))
        if goal is not None:
            issues = state.get("issues")
            if not isinstance(issues, dict):
                issues = state["issues"] = {}
            invariants.Filer(self.gh, goal[0], goal[1], issues, self.now, _say,
                             write=not self._deferred()).run(breaches)
        invariants.save(path, state)

    def _invariant_goal(self) -> Optional[Tuple[str, int]]:
        """(repo, number) of the goal home breaches are filed on: the
        LOOM_INVARIANT_GOAL setting, else LOOM_METRICS_GOAL when it is in the
        self repo, else the self repo's one open goal home that is not a
        backlog; None when none of these names one."""
        spec = re.compile(r"([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)#([1-9][0-9]*)")
        ref = spec.fullmatch(self.s.invariant_goal)
        if ref:
            return ref.group(1), int(ref.group(2))
        ref = spec.fullmatch(self.s.metrics_goal)
        if ref and ref.group(1) == self.s.self_repo:
            return ref.group(1), int(ref.group(2))
        repo = self.s.self_repo
        if not repo or not tickets.REPO_RE.fullmatch(repo):
            return None
        homes = [i for i in self._goal_issues(repo)
                 if not str(i.get("title") or "").startswith("Backlog:")]
        return (repo, int(homes[0]["number"])) if len(homes) == 1 else None

    def _invariant_ref(self, repo: str, number: int, noun: str = "issue") -> invariants.Ref:
        """A Ref whose label a public issue may carry: #N in the repo issues
        are filed in, the repo named for another public one, and a private
        repo named in words only."""
        key = "%s#%d" % (repo, number)
        if repo == self._invariant_repo:
            return invariants.Ref(key, ("%s #%d" % (noun, number)) if noun != "issue" else "#%d" % number)
        if repo not in self._publics:
            self._publics[repo] = self._public(repo)
        where = repo if self._publics[repo] else "a private repo"
        return invariants.Ref(key, "%s %d of %s" % (noun, number, where))

    def _invariant_held(self, held: Any) -> Dict[str, float]:
        """Since when each lane now at a limit has been held: kept from the
        last pass, now for a lane newly at a limit."""
        held = {k: v for k, v in held.items() if self._num(v)} if isinstance(held, dict) else {}
        return {lane: held.get(lane, self.now) for lane in self.limited_lanes()}

    def _invariant_gate_held(self, held: Any) -> Dict[str, Dict[str, Any]]:
        """The goals the unproven-work gate keeps refusing: kept from the last
        pass while the gate keeps refusing them, now for a goal newly refused
        (loom#564). The since time starts on the first refusal, so an hour of
        refusals yields one invariant per goal. A claim for the goal, or a
        pass the gate no longer refuses it, drops it and its invariant
        clears."""
        prior = held if isinstance(held, dict) else {}
        out = {}
        for key, info in self._gate_holds.items():
            if not isinstance(info, dict):
                continue
            old = prior.get(key) if isinstance(prior.get(key), dict) else {}
            since = old.get("since") if self._num(old.get("since")) else self.now
            tickets = info.get("tickets")
            out[key] = {"repo": info["repo"], "goal": info["goal"],
                        "since": since, "count": info["count"],
                        "limit": info["limit"],
                        "tickets": tickets if isinstance(tickets, dict) else {},
                        "idle": info.get("idle", 0) if self._num(info.get("idle")) else 0}
        return out

    def _unassociated(self, repo: str, goal: Dict[str, Any]) -> int:
        """Tickets naming the goal without a sub-issue link or its Part of line. Read (one
        search) only for a goal holding SUB_ISSUES_MAX or more sub-issues, where
        a refused sub-issue link leaves the Part of line as the only one."""
        total = int((goal.get("sub_issues_summary") or {}).get("total") or 0)
        if total < invariants.SUB_ISSUES_MAX:
            return 0
        return len(brain.unassociated_tickets(self.gh, repo, int(goal["number"])))

    def _invariant_goal_facts(self, facts: invariants.Facts) -> None:
        """Goal homes with their unlinked tickets, and their journals and
        marker posts; a source that cannot be read stays None."""
        try:
            homes = self._goal_homes()
        except (GitHubError, OSError) as err:
            _say("invariants: could not read the goal homes: %s" % err)
            return
        try:
            facts.goals = [(self._invariant_ref(repo, int(g["number"])), self._unassociated(repo, g))
                           for repo, g in homes]
        except (GitHubError, OSError) as err:
            _say("invariants: could not read the goal tickets: %s" % err)
        try:
            facts.journals, facts.posts = self._invariant_comments(homes)
        except (GitHubError, OSError) as err:
            _say("invariants: could not read the goal home comments: %s" % err)

    def _invariant_facts(self, state: Dict[str, Any]) -> invariants.Facts:
        """What the invariants read, each source on its own: one that cannot
        be read stays None and its invariants find nothing this pass."""
        facts = invariants.Facts(now=self.now)
        for name, read in (("runs", self._invariant_runs),
                           ("beats", lambda: {lane: self._remote_heartbeat(lane)
                                              for lane in REMOTE_LANES})):
            try:
                setattr(facts, name, read())
            except (GitHubError, OSError) as err:
                _say("invariants: could not read %s: %s" % (name, err))
        facts.held = state["held"] = self._invariant_held(state.get("held"))
        gate_held = self._invariant_gate_held(state.get("gate_held"))
        state["gate_held"] = gate_held
        facts.gate_held = {
            key: invariants.GateHold(
                goal=self._invariant_ref(info["repo"], info["goal"], noun="goal"),
                since=info["since"], count=info["count"], limit=info["limit"],
                tickets=tuple(self._invariant_ref(info["repo"], int(number))
                              for number in sorted(info["tickets"], key=int)),
                idle=info["idle"])
            for key, info in gate_held.items()}
        self._invariant_goal_facts(facts)
        seen = state.get("brain_seen")
        newest = max([j.at for j in facts.journals or []] + ([seen] if self._num(seen) else []),
                     default=None)
        if newest is not None:
            facts.brain_seen = state["brain_seen"] = newest
        if any(self.now - since > invariants.HELD_MAX for since in facts.held.values()):
            try:
                facts.queued = self._invariant_queued()
            except (GitHubError, OSError) as err:
                _say("invariants: could not read the queued tickets: %s" % err)
        try:
            facts.checks = self._invariant_checks()
        except (GitHubError, OSError) as err:
            _say("invariants: could not read the plan checks: %s" % err)
        return facts

    def _invariant_runs(self) -> List[invariants.Run]:
        """Each lane run that ended in the last day, timed by its log (the
        brief keeps the time it was written when it moves)."""
        runs = []
        for lane, kind, path, name in self._finished_briefs():
            log = os.path.join(self.s.home, "logs", lane, name + ".log")
            try:
                at = os.path.getmtime(log)
            except OSError:
                at = os.path.getmtime(path)
            if not 0 <= self.now - at <= invariants.DAY:
                continue
            reason = self._failure_reason(lane, name) if kind == "failed" else ""
            runs.append(invariants.Run(lane, kind == "done", at, reason))
        return runs

    def _invariant_comments(self, homes: List[Tuple[str, Dict[str, Any]]]
                            ) -> Tuple[List[invariants.Journal], List[invariants.Posted]]:
        """The journals and marker comments trusted logins posted on each goal
        home in the last day. The since time is whole hours, so the same URL
        (and its kept answer) serves an hour of passes."""
        since = int((self.now - invariants.DAY) // 3600 * 3600)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since))
        authors = {a.lower() for a in self.s.authors}
        journals, posts = [], []
        for repo, goal in homes:
            ref = self._invariant_ref(repo, int(goal["number"]))
            for c in self.gh.paginate("/repos/%s/issues/%d/comments?since=%s&per_page=100"
                                      % (repo, int(goal["number"]), stamp)):
                if not isinstance(c, dict) or tickets._login(c.get("user")).lower() not in authors:
                    continue
                at = self._epoch(str(c.get("created_at") or ""))
                body = str(c.get("body") or "").lstrip()
                if at is None or not 0 <= self.now - at <= invariants.DAY:
                    continue
                if body.startswith(JOURNAL_MARKER):
                    tick = re.search(r"^Tick (\S+),", body, re.M)
                    journals.append(invariants.Journal(
                        at, ref, handoff.journal_holder({"body": body}),
                        tick.group(1) if tick else "", invariants.failed_steps(body)))
                elif body.startswith(metrics.MARKER):
                    day = re.match(re.escape(metrics.MARKER) + r"\n(\d{4}-\d{2}-\d{2}):", body)
                    posts.append(invariants.Posted(ref, "metrics", at, day.group(1) if day
                                                   else time.strftime("%Y-%m-%d", time.gmtime(at))))
                elif body.startswith(REPORT_MARKER):
                    posts.append(invariants.Posted(ref, "report", at, ""))
        return journals, posts

    def _invariant_queued(self) -> List[Tuple[invariants.Ref, Tuple[str, ...]]]:
        """Each trusted queued ticket and the lanes of its difficulty tier."""
        authors = {a.lower() for a in self.s.authors}
        out = []
        for repo in self.s.repos:
            if not tickets.REPO_RE.fullmatch(repo):
                continue
            for issue in self.gh.paginate("/repos/%s/issues?labels=loom:ticket&state=open" % repo):
                if (not isinstance(issue, dict) or "pull_request" in issue
                        or "loom:queued" not in _label_names(issue)
                        or tickets._login(issue.get("user")).lower() not in authors):
                    continue
                ref = self._invariant_ref(repo, int(issue["number"]))
                if repo not in self._publics:
                    self._publics[repo] = self._public(repo)
                out.append((ref, factory.tier_lanes(issue, not self._publics[repo])))
        return out

    def _invariant_checks(self) -> List[invariants.Check]:
        """The newest completed plan check on each open, trusted lane pull
        request's head commit."""
        authors = {a.lower() for a in self.s.authors}
        out = []
        for repo in self.s.repos:
            if not tickets.REPO_RE.fullmatch(repo):
                continue
            for pr in self._open_pulls(repo):
                if self._trusted_lane_pull(pr, authors):
                    check = self._plan_check_of(repo, pr)
                    if check is not None:
                        out.append(check)
        return out

    @staticmethod
    def _trusted_lane_pull(pr: Dict[str, Any], authors: set) -> bool:
        head = pr.get("head") or {}
        return bool(not pr.get("draft") and str(head.get("ref") or "").startswith("loom/")
                    and head.get("sha") and tickets._login(pr.get("user")).lower() in authors)

    def _plan_check_of(self, repo: str, pr: Dict[str, Any]) -> Optional[invariants.Check]:
        """The newest completed plan check run on the pull request's head, or None."""
        data = self.gh.rest("GET", "/repos/%s/commits/%s/check-runs?check_name=%s&per_page=100"
                            % (repo, pr["head"]["sha"], quote(self.s.plan_check, safe=""))).data
        runs = [r for r in (data or {}).get("check_runs") or []
                if isinstance(r, dict) and r.get("name") == self.s.plan_check
                and r.get("status") == "completed"
                and self._epoch(str(r.get("completed_at") or "")) is not None]
        if not runs:
            return None
        run = max(runs, key=lambda r: str(r.get("completed_at")))
        return invariants.Check(
            self._invariant_ref(repo, int(pr["number"]), "pull request"),
            self.s.plan_check, str(run.get("conclusion") or ""),
            self._epoch(str(run["completed_at"])) or 0.0,
            str((run.get("output") or {}).get("title") or ""))

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
                and 0 <= self.now - sent < brain.REPORT_EVERY - STANDUP_LEAD:
            return
        comments = self.gh.paginate("/repos/%s/issues/%d/comments?per_page=100" %
                                    (repo, number))
        node = {"comments": {"nodes": [
            {"body": c.get("body"), "createdAt": c.get("created_at"),
             "author": {"login": tickets._login(c.get("user"))}}
            for c in comments if isinstance(c, dict)]}}
        ledger = brain.goal_ledger(goal, node, now, authors)
        last = self._epoch(str(ledger.get("report") or ""))
        due = (ledger["state"] == "overdue"
               or (last is not None and self.now - last >= brain.REPORT_EVERY - STANDUP_LEAD))
        if not due:
            return
        children = brain.goal_children(self.gh, repo, number)
        breaches = (self._open_breaches(repo, number)
                    if (repo, number) == self._invariant_goal() else None)
        body = self._overdue_standup_body(children, self._epoch(str(ledger.get("report") or "")),
                                          breaches,
                                          gate_hold=self._gate_hold_for(repo, number))
        brain._check_written_text(body)
        self.gh.rest("POST", "/repos/%s/issues/%d/comments" % (repo, number),
                     {"body": body})
        state[key] = self.now

    def _open_breaches(self, repo: str, goal: int) -> List[Dict[str, Any]]:
        """The open loom:invariant issues filed on the goal home."""
        return [i for i in self.gh.paginate("/repos/%s/issues?labels=%s&state=open"
                                            % (repo, invariants.LABEL))
                if isinstance(i, dict) and "pull_request" not in i
                and brain._names_goal(i.get("body"), goal)]

    def _gate_hold_for(self, repo: str, goal: int) -> Optional[Dict[str, Any]]:
        """The unproven-work gate's persisted hold on this goal home, if the
        gate is still holding it (loom#564). Malformed state is ignored: the
        standup must never break a pass on a state read."""
        held = invariants.load(os.path.join(self.state, "invariants.json"),
                               _say).get("gate_held")
        if not isinstance(held, dict):
            return None
        info = held.get("%s#%d" % (repo, goal))
        if not isinstance(info, dict) \
                or not all(self._num(info.get(k)) for k in ("goal", "count", "limit", "idle")) \
                or not isinstance(info.get("tickets"), dict):
            return None
        return info

    def _overdue_standup_body(self, children: List[Any], since: Optional[float] = None,
                              breaches: Optional[List[Dict[str, Any]]] = None,
                              gate_hold: Optional[Dict[str, Any]] = None) -> str:
        groups = self._standup_ticket_groups(children, since)
        rows = self._standup_rows
        counts = [(len(groups[k]), word) for k, word in (
            ("merged", "merged"), ("verified", "verified"), ("sent_back", "shipped"),
            ("failed", "failed"), ("aged_pr", "stuck"), ("in_flight", "in flight"),
            ("asks", "waiting on you"))]
        summary = ", ".join("%d %s" % (n, word) for n, word in counts if n)
        first = ("%s since the last report." % summary) if summary else "Nothing moved since the last report."
        stuck = "Past the stuck age in PR: %s." % rows(groups["aged_pr"])
        if gate_hold is not None:
            stuck += (" Held by the unproven-work gate: goal #%d -- %d tickets shipped "
                      "without a proof (limit %d), %d briefed tickets waiting, "
                      "%d lanes idle." % (gate_hold["goal"], gate_hold["count"],
                                          gate_hold["limit"], len(gate_hold["tickets"]),
                                          gate_hold["idle"]))
        return "\n".join([REPORT_MARKER, first,
            "", "### Going well", "Merged: %s. Verified: %s. Shipped: %s." %
            (rows(groups["merged"]), rows(groups["verified"]),
             rows(groups["sent_back"])), "",
            "### Not going well", "Failed: %s." % rows(groups["failed"])]
            + (["Invariants broken: %s." % rows(breaches)] if breaches is not None else [])
            + ["",
            "### Still stuck", stuck, "",
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
        # A tick that ran no agent says why (no brain lane has usage).
        reason = last.get("reason")
        why = (" because %s" % " ".join(reason.split())[:300]
               if isinstance(reason, str) and reason.strip() else "")
        seen = ("Newest journal: %s on %s." % (self._utc(journal["at"]), journal["goal"])
                if journal else "No journal from an allowed author in the last day.")
        return ("Urgent: the Loom brain has not finished a tick %s, so no goal is "
                "moving. %s Last tick on the lane machine: %s, %s (exit %s)%s. Its log "
                "is brain.log there." % (since, seen, when, outcome, code, why))

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
        """Fix forward after a red lane merge, then revert if it stays red.
        Runs with the factory; LOOM_FACTORY=0 turns both off."""
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
        if today in dated and not self.s.report_digest:
            # With a report digest the day's numbers ride in it instead.
            passed, streak = dated[today]
            line = "Loom metrics %s: %d/9 pass; streak %d/7." % (today, passed, streak)
            self._metrics_notice(today, "discord", line)

    # -- handoff packet --------------------------------------------------
    def _handoff_home(self) -> Optional[Tuple[str, int]]:
        """(repo, number) of the Loom goal home: the status repo's one open
        goal home that is not a backlog; None when there is not exactly one."""
        repo = self.s.status_repo
        if not repo or not tickets.REPO_RE.fullmatch(repo):
            return None
        homes = [i for i in self._goal_issues(repo)
                 if not str(i.get("title") or "").startswith("Backlog:")]
        return (repo, int(homes[0]["number"])) if len(homes) == 1 else None

    def _shown(self, repo: str, home: str) -> bool:
        """Whether the packet on `home` may name the repo and quote its text:
        its own repo, or a public one. A name that is not OWNER/NAME is never
        shown, and is never asked about."""
        if not isinstance(repo, str) or not tickets.REPO_RE.fullmatch(repo):
            return False
        if repo == home:
            return True
        if repo not in self._publics:
            self._publics[repo] = self._public(repo)
        return self._publics[repo]

    def _handoff_rules(self, home: str) -> Tuple[str, List[str]]:
        """(commit, Rules lines) of docs/MUSE.md at this checkout's HEAD, read
        from GitHub so the packet quotes only what is public there; no lines
        when the commit is not on GitHub or the repo may not be quoted."""
        commit = self.head()
        repo = self.s.self_repo
        if not COMMIT_RE.fullmatch(commit or "") or not tickets.REPO_RE.fullmatch(repo or ""):
            return "", []
        if not self._shown(repo, home):
            return commit, []
        # A commit GitHub does not have answers 404 (or 422): no rules then.
        data = self.gh.rest("GET", "/repos/%s/contents/docs/MUSE.md?ref=%s" % (repo, commit),
                            ok=(404, 422)).data
        if not isinstance(data, dict) or not data.get("content"):
            return commit, []
        doc = base64.b64decode(data["content"]).decode("utf-8", "replace")
        return commit, handoff.rules_section(doc)

    def _handoff_goals(self, home: str, saved: Dict[str, Any]
                       ) -> Tuple[List[handoff.Goal], int, Dict[str, Any]]:
        """Every goal home's trusted journals (handoff.compact), the repos that
        could not be read, and what to keep for the next build. A home whose
        `updated_at` and comment count match the last build is not read
        again: its kept journals serve. A repo that fails is counted and
        skipped, so one unreadable repo does not freeze the packet."""
        goals: List[handoff.Goal] = []
        kept: Dict[str, Any] = {}
        unread = 0
        for repo in self.s.repos:
            if not tickets.REPO_RE.fullmatch(repo):
                continue
            try:
                issues = self._goal_issues(repo)
                read = [(int(i["number"]), self._handoff_home_journals(repo, i, saved))
                        for i in issues]
            except GitHubError as err:
                _say("handoff packet: could not read the goal homes of %s: %s" % (repo, err))
                unread += 1
                continue
            for number, entry in read:
                kept["%s#%d" % (repo, number)] = entry
                # Kept journals are filtered again: only allowed logins count.
                goals.append(handoff.Goal(repo, number, self._shown(repo, home),
                                          handoff.journals(entry["journals"], self.s.authors)))
        return goals, unread, kept

    def _handoff_home_journals(self, repo: str, issue: Dict[str, Any],
                               saved: Dict[str, Any]) -> Dict[str, Any]:
        """One goal home's kept entry: its updated_at, comment count and
        compact journals, read again only when the first two changed. A
        re-read also finds the packet comment when this is the Loom home."""
        number = int(issue["number"])
        stamp = [str(issue.get("updated_at") or ""), issue.get("comments")]
        old = saved.get("%s#%d" % (repo, number))
        if (isinstance(old, dict) and old.get("stamp") == stamp and stamp[0]
                and isinstance(old.get("journals"), list)):
            return old
        comments = [c for c in self.gh.paginate(
            "/repos/%s/issues/%d/comments?per_page=100" % (repo, number)) if isinstance(c, dict)]
        found = handoff.journals(
            [{"body": c.get("body"), "at": c.get("created_at"),
              "login": (c.get("user") or {}).get("login")} for c in comments], self.s.authors)
        return {"stamp": stamp, "journals": [handoff.compact(j) for j in found],
                "packet": self._packet_comment(comments)}

    def _packet_comment(self, comments: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """{"id", "body"} of the first packet comment an allowed login posted
        among `comments`, or None."""
        allowed = {a.lower() for a in self.s.authors}
        for c in comments:
            body = str(c.get("body") or "")
            if tickets._login(c.get("user")).lower() in allowed and body.startswith(
                    handoff.HANDOFF_MARKER):
                return {"id": int(c["id"]), "body": body}
        return None

    def _brain_ended(self) -> Any:
        """The "at" of the lane machine's last brain tick (state/brain.json),
        or None: a new value means a tick just ended."""
        last = _read_json(os.path.join(self.s.home, "state", "brain.json"), None, strict=False)
        return last.get("at") if isinstance(last, dict) else None

    def _handoff_due(self, saved: Dict[str, Any], ended: Any, authors: List[str]) -> bool:
        """Whether the packet is due a rebuild: HANDOFF_EVERY seconds since the
        last build, a brain tick that just ended, or new allowed logins."""
        built = saved.get("built")
        return not (self._num(built) and 0 <= self.now - built < HANDOFF_EVERY
                    and saved.get("brain_at") == ended and saved.get("authors") == authors)

    def _write_handoff(self, home: Tuple[str, int], entry: Dict[str, Any], body: str) -> None:
        """Edit the kept packet comment to `body`, or post one when none is
        kept or the kept one was deleted, and keep its id and text in
        `entry`. Nothing is written when the text is the same."""
        packet = entry.get("packet") if isinstance(entry.get("packet"), dict) else None
        if packet is not None and packet.get("body") == body:
            return
        made = None
        if packet is not None:
            made = self.gh.rest("PATCH", "/repos/%s/issues/comments/%d" % (home[0], int(packet["id"])),
                                {"body": body}, ok=(404,))
        if made is None or made.status == 404:
            made = self.gh.rest("POST", "/repos/%s/issues/%d/comments" % home, {"body": body})
            data = made.data if isinstance(made.data, dict) else {}
            packet = {"id": int(data["id"])} if "id" in data else None
        if packet is not None:
            packet["body"] = body
        entry["packet"] = packet
        _say("handoff packet updated on %s#%d" % home)

    def publish_handoff(self) -> None:
        """Keep the handoff packet (handoff.packet) in one comment on the Loom
        goal home, marked HANDOFF_MARKER: posted once, then edited in place,
        and only when its text changed. It is rebuilt at most every
        HANDOFF_EVERY seconds, or sooner when a brain tick just ended, so a
        pass does not read every goal home's comments."""
        path = os.path.join(self.state, "handoff.json")
        saved = _read_json(path, {}, strict=False)
        saved = saved if isinstance(saved, dict) else {}
        ended = self._brain_ended()
        authors = sorted({a.lower() for a in self.s.authors})
        if not self._handoff_due(saved, ended, authors):
            return
        home = self._handoff_home()
        if home is None:
            return
        repo = home[0]
        homes = saved.get("homes") if isinstance(saved.get("homes"), dict) else {}
        if saved.get("authors") != authors:
            # The allowed logins changed: nothing kept under the old list
            # (journals or the packet comment) is trusted.
            homes = {}
        goals, unread, kept = self._handoff_goals(repo, homes)
        body = handoff.packet(goals, brain.tick_step(self.gh, repo), self._handoff_rules(repo),
                              datetime.fromtimestamp(self.now, timezone.utc),
                              lambda r: self._shown(r, repo), unread)
        _text.check(body)
        key = "%s#%d" % home
        if key not in kept:
            kept[key] = self._handoff_home_journals(repo, {"number": home[1]}, {})
        self._write_handoff(home, kept[key], body)
        _write_json(path, {"built": self.now, "brain_at": ended, "authors": authors,
                           "homes": kept})

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
            line += "; %sbudget %d of %d left, resets %s" % (
                "GitHub App " if gh.auth == "app" else "", remaining, limit, self._utc(reset))
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

    def roll_source_pins(self) -> None:
        if self.s.roll_ship_docs:
            try:
                roll_pin.run(self.gh, self.s.roll_ship_docs, self.s.authors,
                             self.s.review_check)
            except ValueError as err:
                raise GitHubError("invalid roll ship document or pin: %s" % err) from err
            except health.NotLooked as err:
                raise GitHubError("roll family gate could not be read: %s" % err) from err

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
            except (GitHubError, OSError) as err:
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
        for step in (self.roll_source_pins, self.ship_deployed, self.ship_pins, self.unwind_reverts,
                     self.adopt_untracked_pulls, self.publish_lanes, self.forward_asks, self.post_overdue_standups,
                     self.forward_reports, self.triage_backlogs, self.rewrite_triaged,
                     self.health_intake,
                     self.post_metrics, self.publish_handoff,
                     self.watch_brain, self.upload_transcripts, self.relay_operator,
                     self.watch_default_branches, self.run_factory, self.check_invariants,
                     self.release_factory_held):
            if self._deferred():
                break
            try:
                step()
            except (GitHubError, OSError, ValueError) as err:
                _say("%s failed: %s" % (step.__name__, err))
        try:
            self.watch_logs()
        except (GitHubError, OSError) as err:
            _say("watch_logs failed: %s" % err)
        try:
            self.prune_trees()
        except OSError as err:
            _say("prune_trees failed: %s" % err)
        try:
            records = usage.collect(self.s.home, now=self.now)
            usage.write(self.s.home, records)
            self.upload_usage_limits(records)
        except OSError as err:
            _say("could not write usage limits: %s" % err)

    def upload_usage_limits(self, records: List[Dict[str, Any]]) -> None:
        """Send the current limits snapshot using the transcript upload token."""
        if not (self.s.transcript_url and self.s.transcript_token_ssm):
            return
        url = self.s.transcript_url.removesuffix("/api/transcripts") + "/api/usage"
        token = self._transcript_token()
        if token is None:
            return
        try:
            self.upload(url, token, records)
        except UploadRefused as err:
            _say("the dashboard refused the usage snapshot (%d)" % err.status)
        except Exception as err:  # noqa: BLE001
            _say("usage snapshot upload failed (%s)" % type(err).__name__)


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
            from .github import auth_from_env
            gh = GitHub(auth_from_env())
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
