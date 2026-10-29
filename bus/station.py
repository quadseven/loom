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
import faulthandler
import fcntl
import fnmatch
import gzip
import hashlib
import json
import math
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
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import answer
from . import audit
from . import bench
from . import ci_gap
from . import converge
from . import brain_memory
from . import brain_hold
from . import claimed
from . import fabric
from . import fixrounds
from . import flake
from . import rebuttal
from . import runners
from . import scope_fit
from . import shadow
from . import urgency
from . import stale_claims
from . import label_pass
from . import factory
from . import friction_intake
from . import evidence
from . import handoff
from . import health
from . import invariants
from . import metrics
from . import naming
from . import notify
from . import proof
from . import quiet as quiet_rule
from . import rewrite
from . import operator_hold
from . import ship_paths
from . import roll_pin
from . import revert
from . import spend
from . import status
from . import usage
from . import verify_data
from . import text as _text
from . import tickets
from . import transcripts
from . import unmeetable
from . import brief_check
from . import criteria_gate
from . import live_adapter, live_proof
from . import deploy_watch
from . import scorecard
from . import watch
from . import weakened
from . import labels
from .brain import HUMAN_MARKER
from . import brain
from .github import (GitHub, GitHubError, RateLimited, TargetReached, _OPENER,
                     _next_link, read_ssm, rest_login, unreadable)
from .journal import REPLY_RE
from .labels import LANES
from .labels import REMOTE_LANES

HEARTBEAT_MAX_AGE = 600
BRIEFED = "loom:briefed"
RUNNING = "loom:running"
RUNAWAY = "loom:runaway"
# How often the station moves the tickets that sit under a goal home and
# under no epic into an open intake epic of that goal (decision 0003).
OUTSIDE_EPIC_EVERY = 3600
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
# Elder review check output parsing: individual markings like "removed-guarded-path"
# are listed in the summary. Each marking line looks like:
# "- `removed-guarded-path`: This PR deletes \`path/to/file.md\`, which removes KIND. /grug accept-removal path/to/file.md SHA"
# The accept-removal line may be on the same line or the next line.
_ELDER_MARKING_RE = re.compile(r"^\s*-\s+`([a-z0-9-]+)`:\s*(.*)$", re.M)
_ACCEPT_REMOVAL_RE = re.compile(r"/grug accept-removal\s+(\S+)\s+(\S+)")
# A pull request a brief names in words, as a settle-findings ticket names the
# one its findings were left on.
NAMED_PR_RE = re.compile(r"\b(?:PR|pull request) #([0-9]+)\b")
# A lane tree (lanes/run_lane.sh clones one a brief attempt) whose brief is no
# longer waiting or running is removed once it is this old. On 2026-10-13 the
# 385 left behind held 7.8 GB and the disk filled.
TREE_KEEP = 3600
# The loom home is kept bounded (loom#643): a lane's logs (logs/<slot>/) are
# removed once nothing in them changed for LOOM_LOG_KEEP_DAYS days (0 keeps
# them all), unless a brief of that name is waiting or running or its
# transcript has not reached the dashboard yet. Every DISK_EVERY seconds the
# old logs go, finished runs' session stores are compressed, and the home's
# size is logged. On 2026-10-17 logs/ held 4.2 GB, growing 0.6 GB a day, and
# the disk was down to 8 GB free.
LOG_KEEP_DAYS = 7
# While the home's disk has less than LOOM_MIN_FREE_GB free the station claims
# no new work (loom#1808): on 2026-10-25 the disk fell to 300 MB free and every
# lane run failed at once. The older name LOOM_DISK_FREE_MIN_GB is still read.
DISK_FREE_MIN_GB = 15.0
DISK_GUARD_FILE = "disk-guard.json"
# A finished opencode run's session store under its log folder (lanes/
# run_lane.sh archive_transcripts): 100-440 MB a run on 2026-10-25, 14 GB of
# the 17 GB in logs/ (loom#1808). Compressed once its transcript is uploaded.
SESSION_DB_PARTS = (".local", "share", "opencode", "opencode.db")
# The ticket states after which a lane tree serves no fix round: its tree,
# check copy and idle run scratch go at once, not after TREE_KEEP.
TREE_DONE_STATES = frozenset(("loom:merged", "loom:shipped", "loom:verified",
                              "loom:closed-unmerged"))
# A lane's run scratch (run/<slot>-home, -check, -check-default) younger than
# this is never removed: a runner may have just made it.
RUN_SCRATCH_SETTLE = 600
# The folders an idle lane's run home drops each pass: package caches and
# temp files a run rebuilds.
RUN_HOME_CACHES = (".cache", ".cargo", "tmp")
DISK_EVERY = 3600
# Local Time Machine snapshots keep every lane tree the station removed:
# on 2026-10-26 free space fell from 50 to 27 GB in three hours while loom
# home held under 15 GB, and thinning them freed 40 GB. Below twice the
# floor the station asks for them to be thinned, at most this often.
THIN_EVERY = 1800
THIN_FILE = "snapshot-thin.json"
GB = 1024 ** 3
# A fix brief's answers to findings (lanes/run_lane.sh keep_answers).
ANSWER_FILE_RE = re.compile(r"^(PRRT_[A-Za-z0-9_-]{1,100})\.md$")
ANSWER_MAX = 3000
STRONG_MODELS = frozenset(model for _, model in factory.CLIMB)
THREAD_QUERY = ('query($id:ID!) { node(id:$id) { ... on PullRequestReviewThread { isResolved path'
                ' pullRequest { number headRefOid repository { nameWithOwner } }'
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
# The runner's reason when it parked a fix brief unrun because its pull
# request is no longer open (lanes/run_lane.sh, fix_pr_open). A MERGED one is
# the ticket's work landed, not a failure (loom#1178).
PR_GONE_RE = re.compile(r"pull request #([0-9]+) is no longer open on \S+ \((MERGED|CLOSED)\b")
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
# A usage snapshot that fails once is sent again after this pause (loom#345).
USAGE_RETRY_PAUSE = 2
DIGEST_HEAD = "Asks waiting for you:"
# Open loom:human issues wait for the daily digest.
HUMAN = "loom:human"
# Why a ticket waits (bus/labels.py): one wait:* label at a time.
WAIT = "wait:"
HUMAN_HEAD = "Waiting on you"
HUMAN_SHOWN = 5
# A report first line whose only count is work in flight: the digest counts
# that goal as quiet.
IN_FLIGHT_ONLY_RE = re.compile(r"\A\d+ in flight( since the last report)?\.?\Z", re.I)
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
# A ticket whose fix rounds stop (bus/converge.py: no rung left above a stuck
# round, or the round cap reached on its rung) is left loom:failed with this
# note and labels.HANDOFF, the pool the second operator's session works.
FIX_STOP_TEXT = ("The station stopped the fix rounds on pull request #%d: %s. Still failing: %s. "
                 "The ticket is left loom:failed with the %s label for the second operator's "
                 "session, which claims it before working it.")
REPORT_MARKER = "<!-- loom:report v1 -->"
# The last line of a quiet standup the station edits in place, and of a
# standup that repeats an earlier one word for word.
QUIET_STAMP = quiet_rule.HEARTBEAT_STAMP
SAME_STAMP = "Unchanged as of %s; this report is edited in place until something changes."
# Reports come at the reading times (brain.REPORT_TIMES), so the goal homes are read at most this often,
# and only for comments since the last read (with an overlap; the ledger of
# comment ids keeps a report from going out twice).
REPORT_CHECK = 300
# The station's standup goes out this long before a goal's report is due
# (brain.next_report_due, the next reading time), so the operator finds it at
# the reading time. Posting only once a goal was overdue left every report
# late for the visibility number.
STANDUP_LEAD = 15 * 60
# A goal quiet since its last report (bus/quiet.py) is owed no standup; it is
# read again this often, so a standup still goes out soon after it moves.
QUIET_RECHECK = 3600
# The last line of every station standup, which is how the station finds its
# own report to edit for a quiet goal's heartbeat.
# A standup line names at most this many tickets, then "and N more".
STANDUP_ROWS_MAX = 5
STANDUP_FOOTER = "Written by the station from ticket data, because no brain reported in time."
# The operator's two reading times (2026-10-14: "Twice a day. Morning and
# evening. 10am and 9pm"): one digest of every goal's reports each, instead of
# each report as it is posted. LOOM_REPORT_DIGEST_TIMES="" turns it off. The
# goal reports on GitHub are due at the same times (brain.REPORT_TIMES).
DIGEST_TIMES = ",".join("%02d:%02d" % t for t in brain.REPORT_TIMES)
DIGEST_FIRST_LOOKBACK = 12 * 3600
REPORT_OVERLAP = 3600
REPORT_FIRST_LOOKBACK = 24 * 3600
# A backlog goal home is triaged at most this often.
TRIAGE_INTERVAL = 3600
# Agent rewrites of loom:triage issues: per backlog goal home per hour, per
# pass (each one waits on an agent command), and how long before an issue
# whose rewrite did not produce a ticket is tried again.
REWRITE_PER_HOUR = 5
# A brief that has waited this long in a down lane's inbox stops blocking its
# ticket's next claim (_return_dead_brief).
DEAD_BRIEF_AGE = 3600
# A runner stamps state/usage/<slot>.alive every minute while it lives
# (lanes/run_lane.sh, loom#1281); a stamp this old or less says its runner is
# running, whatever its heartbeat's age. Several missed stamps still count.
ALIVE_MAX_AGE = 300
REWRITE_PER_PASS = 5
# A goal at its in-flight cap this long is stuck: the rewrite skip line and
# the dashboard's Needs-you page say so, with how many of its in-flight
# tickets wait with no lane (loom#1831). A record the rewrite step has not
# refreshed in GOAL_CAP_SEEN is not sent.
GOAL_CAP_STUCK = 6 * 3600
GOAL_CAP_SEEN = 2 * 3600
GOAL_CAPS_PATH = "/api/goal-caps"
GOAL_CAPS_EVERY = 600
# needs-brain holds older than a day (loom#1872): each run's counts, a repo a
# row, go to the dashboard's Needs-you page; the run's stamp is kept here.
BRAIN_HOLDS_PATH = "/api/brain-holds"
BRAIN_HOLDS_FILE = "brain-holds.json"
# The dashboard's /chain page: each repo's shadow mode and the last actions
# Loom held and took, from bus.shadow, every CHAIN_EVERY seconds; the
# send's stamp is kept here. The dashboard pod has neither the station's
# shadow switch nor its state folder, so the station is the one source.
CHAIN_PATH = "/api/chain"
CHAIN_FILE = "chain.json"
CHAIN_EVERY = 600
CHAIN_ROWS = 20
# A recorded ticket wait the factory has not repeated for this long is gone:
# the ticket moved, or left the factory (invariants.ticket_waiting).
WAIT_UNSEEN = 3600
# A wait reason reaches a public incident: only these characters, this long.
WAIT_REASON_RE = re.compile(r"[A-Za-z0-9 ,:.()/@_-]{1,160}")
# Rewrites across every backlog goal in any hour: each one comments on its
# issue or files a ticket, and both email the operator. Per goal alone, ten
# backlog goals let about 80 through in ten minutes (2026-10-18).
REWRITE_STATION_PER_HOUR = 6
REWRITE_STATION_KEY = "_station"
# Live-line restates have their own hourly allowance: an edit of the ticket
# body, never a comment, so no email. Sharing REWRITE_STATION_PER_HOUR, the
# rewrite step spent all six calls every hour and no shipped ticket was ever
# restated (2026-10-19).
RESTATE_PER_HOUR = 4
# Done tickets closed per pass (close_done_tickets), so a backlog of them
# drains over a few passes instead of one burst of API writes.
CLOSE_DONE_PER_PASS = 25
# A ticket carrying one of these stays open for the brain whatever its state.
# An open loom:rewritten source whose ticket is closed is settled at most
# once an hour, this many a run (settle_orphan_sources); a source's ticket is
# read from its rewrite comment once and kept in SOURCES_FILE.
SETTLE_EVERY = 3600
SETTLE_PER_RUN = 25
REPO_ACCESS_EVERY = 3600
REPO_ACCESS_FILE = "repo_access.json"
SOURCES_FILE = "sources.json"
# The station's live checks of shipped tickets' criteria (bus/criteria_gate.py):
# ticket -> criterion key -> {"result", "at", "seen"}. A failed or missing
# check runs again after LIVE_CHECK_RETRY; at most LIVE_CHECKS_PER_PASS run
# a pass.
LIVE_CHECKS_FILE = "live-checks.json"
LIVE_CHECK_RETRY = 6 * 3600
LIVE_CHECKS_PER_PASS = 5
# The read-only live proof of shipped tickets (bus/live_proof.py): ticket ->
# {"contract", "next_at", "said"}. A ticket is proved again at its next_at, or
# once its criteria change when no proof Loom runs can change the answer; at
# most LIVE_PROOFS_PER_PASS a pass.
LIVE_PROOF_FILE = "live-proof.json"
LIVE_PROOFS_PER_PASS = 5
# Asks for unverified criteria are emails: at most this many an hour.
LIVE_ASKS_PER_HOUR = 4
SETTLE_LANDED = ("loom:merged", "loom:shipped", "loom:verified")
REWRITTEN_AS_RE = re.compile(r"Rewritten as ticket #(\d+),")
CLOSE_DONE_HOLDS = ("loom:regression", "loom:urgent", RUNAWAY)
CLOSE_DONE_LABEL = "force-close"
RESTATE_KEY = "_live_station"
REWRITE_RETRY = 24 * 3600
REWRITE_TIMEOUT = 600
# Rewrite agent jobs (rewrite.start_job): the pass that starts them waits at
# most REWRITE_WAIT seconds, polling every REWRITE_POLL, and leaves the rest
# to a later pass; a job with no result REWRITE_JOB_GRACE past its timeout is
# dropped as failed. Inline, five model runs held one pass for minutes
# (loom#1663).
REWRITE_WAIT = 10
REWRITE_POLL = 0.2
REWRITE_JOB_GRACE = 300
REWRITE_JOBS_KEY = "_jobs"
REWRITE_JOBS_DIR = "rewrite-jobs"
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
# A merged ticket _merged_pulls did not match: at most this many of its
# cross-referencing merged pull requests are read, and one with no match is
# not looked up again for SHIP_LOOKUP_RETRY (SHIP_LOOKUPS_FILE).
SHIP_XREF_MAX = 3
SHIP_LOOKUP_RETRY = 6 * 3600
SHIP_LOOKUPS_FILE = "ship_lookups.json"
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
# An open issue at one of these states whose linked pull request merged and
# deployed ships too: only loom:pr tickets are reconciled from their pull
# request, so a triage or queued issue a pull request named sat there after
# the deploy (loom#2025).
EARLY_SHIP_STATES = ("loom:triage", "loom:queued")
SETTLE_LANDED_SET = frozenset(SETTLE_LANDED)
# A ticket the ship rules found deployed but that stayed at loom:merged (its
# move failed) is kept in DEPLOYED_SEEN_FILE with when it was first found;
# past MERGED_UNSHIPPED_AFTER it is a merged-not-shipped invariant breach.
DEPLOYED_SEEN_FILE = "deployed_seen.json"
# The handoff packet is rebuilt at most this often, or sooner when a brain
# tick on this machine just ended (publish_handoff).
HANDOFF_EVERY = 600
# Shipping on merge (LOOM_SHIP_ON_MERGE): a repo nothing deploys ships a
# ticket when its pull request merges. Shipping on a workflow
# (LOOM_SHIP_WORKFLOWS, OWNER/NAME=WORKFLOW_FILE words): a repo with its own
# deploy workflow ships a ticket once a successful run of that workflow ran
# on its pull request's merge commit, or the newest successful run on the
# default branch carries it. Only push runs on the default branch of the repo
# itself count: a pull request run, or one from a fork's head repository, can
# sit at the same commit without deploying anything. At most SHIP_PER_PASS
# move per rule a pass.
SHIP_RUNS_URL = ("/repos/%s/actions/workflows/%s/runs?status=success&event=push"
                 "&branch=%s&per_page=30")
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
# Shipping through a downstream roll (loom#2025): a repo that deploys only when
# another repo rolls it declares that in ROLL_SIGNAL_FILE on its default
# branch, one roll record a line: `<OWNER/NAME> <path> <key>`. The record is
# the file at <path> on that repo's default branch; its `<key>=<sha>` (or
# `<key>: <sha>`) line names the source commit the roll deployed. Each pass,
# the repo's open loom:merged tickets whose merge commit that commit carries
# move to loom:shipped.
ROLL_SIGNAL_FILE = ".loom/ship-roll"
# Each repo's ROLL_SIGNAL_FILE is read at most every ROLL_SIGNAL_EVERY seconds
# (ROLL_SIGNALS_FILE keeps the text, or None for a repo without one), so a
# repo that declares no roll costs one request an hour, not two a pass.
ROLL_SIGNALS_FILE = "roll_signals.json"
ROLL_SIGNAL_EVERY = 3600
ROLL_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,99}")
PIN_SHIP_SETTLE = 3600
PIN_SHIPS_FILE = "pin_ships.json"
SHIP_PATH_FILES = "ship_path_files.json"
SHIP_PATH_MANUAL = "ship_path_manual.json"
# The pull request files API returns at most this many files.
PULL_FILES_MAX = 3000
# Adopting open pull requests no ticket links (decision 0002, item 1): each
# pass files at most one "Land or supersede" ticket per scanned repo, oldest
# pull request first, and marks the pull request with ADOPTED_MARKER so it is
# never adopted twice. The ticket's body carries ADOPTS_MARKER too, so a
# marker comment that failed to post files no second ticket. ADOPTIONS_FILE
# keeps, per repo, the pull requests known to be adopted (so each one's
# comments are read once), the count and refusal last logged (each logged
# when it changes), and when a brief was last refused: the repo is tried
# again ADOPT_RETRY seconds later, not every pass. A pull request is adopted
# only once it is ADOPT_GRACE old, and is re-read just before its ticket is
# filed: one closed or merged since the pass read the open list gets no ticket.
ADOPTED_MARKER = "<!-- loom:adopted v1 ticket=%s#%d -->"
ADOPTED_RE = re.compile(r"<!-- loom:adopted v1 ticket=[A-Za-z0-9._-]+/[A-Za-z0-9._-]+#[0-9]+ -->")
ADOPTS_MARKER = "<!-- loom:adopts pr=%d -->"
ADOPT_TITLE = "Land or supersede #%d"
ADOPTIONS_FILE = "adoptions.json"
# Issues another operator's Loom owns, each logged once (docs/LANES.md).
OWNED_FILE = "owned.json"
# The untrusted labellers a briefed ticket was refused for, with when (loom#660).
LABELER_FILE = "labeler_refusals.json"
ADOPT_RETRY = 600
# Over 2026-10-13 to 2026-10-20 the station filed 64 adoption tickets: 49
# within an hour of the pull request opening (median 13 minutes), and 45 of
# the 54 that merged did so within 2 hours of their ticket. Those pull
# requests had a person or a session driving them, so the ticket was only
# bookkeeping; a pull request that really stalls is still adopted after this.
ADOPT_GRACE = 2 * 3600
REMOTE_RE = re.compile(r"[:/]([A-Za-z0-9._-]+/[A-Za-z0-9._-]+?)(?:\.git)?/?$")
# Automatic retries (docs/BRAIN.md "Claim and outcome"). The runner starts
# each failure reason with its class; a reason without one is a work failure.
# A [factory] brief is not a failure at all: the validation environment the
# factory broke (a contract the runner cannot read, a check it cannot run,
# a base check already red), so the ticket stays running on its lane and
# keeps its rung, and the brain re-queues it when the incident closes.
# An [auth] one is the operator's key, which no lane can renew: it is not a
# failure, not an attempt spent and never a runaway.
CLASS_RE = re.compile(r"^\[(limit|transient|work|factory|auth)\] ")
RETRY_CLASSES = ("limit", "transient")
RETRY_MAX = 2
# Where the runner files its validation incidents (lanes/run_lane.sh
# FACTORY_REPO), and the marker and ticket line their bodies carry.
FACTORY_REPO = "quadseven/loom"
INCIDENT_MARKER = "<!-- loom:factory-incident v1 -->"
# The note that says the station unticked a decision box a lane ticked.
DECISION_REVERT_MARKER = "<!-- loom:decision-revert v1 -->"
DECISION_ASK = ("Kind: decision\nA merged pull request already carries this ticket's work, "
                "but these boxes ask for an operator decision: %s. Decide, tick each box, "
                "and move the ticket on, or say what to change.")
# A no-change answer that leaves a code acceptance box unproven (loom#2032).
NO_CHANGE_UNPROVEN_NOTE = ("The answer does not prove every acceptance box, so the ticket is "
                           "back at loom:queued instead of settling. Not proven: %s")
# A base check that failed before the agent ran: it could not start (exit
# 125 to 127) or it was red on the unmodified repo or default branch. On a
# cluster runner that is often the runner image (a tool the repo's check
# calls is not installed, which a script may report as exit 1), and the
# runner cannot tell it from a red base: another lane runs the check, and
# holds the ticket behind an incident if the base is red there too.
CHECK_CANNOT_RUN_RE = re.compile(r"the check exited 12[5-7] on the unmodified base"
                                 r"|base check exited [0-9]+ on the (?:unmodified repo"
                                 r"|default branch)")
HELD_FOR_RE = re.compile(r"factory incident ([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)#([0-9]+)")
# A [factory] hold whose reason names no incident (the incident filing failed)
# has nothing that can close it, so the station releases it after this long:
# the ticket is not held forever.
HELD_NO_INCIDENT_FOR = 2 * 3600
RETRY_RE = re.compile(r"Automatic retry [0-9]+ of [0-9]+ \((?:limit|transient)\): "
                      r"moved from lane ([a-z0-9-]+) to lane ([a-z0-9-]+)\.")
# The brain's lane order for a retry, after the failed lane's own provider.
# claude is never chosen: it spends the operator's own subscription.
RETRY_ORDER = ["anthropic", "anthropic-2", "anthropic-3", "anthropic-4",
               "codex", "codex-2", "opencode-go", "agy"]
# Free lanes take only easy tickets on public repos.
FREE_LANES = ["openrouter", "zen", "zai", "workersai"]
# The model families each lane's runner accepts (lanes/run_lane.sh); codex
# takes any model outside the claude family. agy takes gemini-3.8-flash only:
# its runner drops any other gemini, claude or gpt-oss pin and runs flash
# (LOOM_AGY_ONLY_MODEL, the operator's rule of 2026-10-21), so a ticket
# pinned to Opus routed to agy ran flash, not Opus.
CLAUDE_MODELS = ["claude*", "opus*", "sonnet*", "haiku*", "fable*"]
LANE_MODELS = {
    "claude": CLAUDE_MODELS,
    "anthropic": ["claude*"],
    "openrouter": ["openrouter/*:free"],
    # Only the allow-listed models (tickets.OPENCODE_GO_MODELS), so a pin to
    # a low-allowance Go model is never routed to an opencode-go lane.
    "opencode-go": list(tickets.OPENCODE_GO_MODELS),
    "zen": ["opencode/*-free"],
    "zai": ["zai/glm-4.7-flash", "zai/glm-4.5-flash"],
    "workersai": ["cloudflare-workers-ai/@cf/*"],
    "spark": ["spark/?*"],
    "free": ["free/?*"],
    "agy": ["gemini-3.8-flash*"],
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
# Between brain ticks the station sends new done markers to the dashboard at
# most this often (post_brain_memory), so a commitment another brain
# finished closes without waiting for this machine's next tick.
CLOSE_EVERY = 600
# The rules digest's source for docs/MUSE.md's Rules section.
MUSE_RULES = "docs/MUSE.md"

SecretReader = Callable[[str, str], str]
Poster = Callable[[str, Dict[str, Any]], None]
# (URL, bearer token, body) -> None; raises on any failure.
Uploader = Callable[[str, str, Dict[str, Any]], None]
# (URL, bearer token) -> the dashboard's handoff packet; OSError on any failure.
Fetcher = Callable[[str, str], Dict[str, Any]]
# A finished run: (ledger key, brain tick start or None, body builder).
Run = Tuple[str, Optional[float], Callable[[], Dict[str, Any]]]
# (method, path under the API, bot token, JSON body or None) -> parsed reply.
DiscordCall = Callable[[str, str, str, Optional[Dict[str, Any]]], Any]


MAX_FIX_BODY = 60000
# Below this share of any GitHub rate limit a pass does claims and reports
# only: the hourly budget is shared with the brains, and a station that spends
# it all stops claiming until the reset.
BUDGET_FLOOR = 0.15
PASS_REQUEST_TARGET = 500
# A repo with no briefed, running or pr ticket is scanned this often at most.
IDLE_SCAN = 600
# Conditional-request answers unused this long are removed.
CACHE_KEEP = 24 * 3600
VERIFY_INTERVAL = 6 * 3600
# A Luna review verdict's findings (loom#2142): at most this many per list,
# each finding's text collapsed and capped, so a lane's review.json can
# never turn the verdict comment into a wall.
REVIEW_FINDINGS_MAX = 50
REVIEW_FINDING_CHARS = 500
# A shipped ticket whose Live box has seen no matching log line this many days
# after it shipped waits on a rare event, not on a verifier: it gets
# wait:live-event and is held apart from open work (loom#1220).
VERIFY_WAIT_DAYS = 7
# The hours of log an evidence command is documented to search (.env.example).
EVIDENCE_WINDOW_HOURS = 24.0
# Fewest characters a verifier's "missing evidence" may have to earn the
# loom:awaiting-evidence label: a reason, not a word.
MISSING_MIN = 20
VERIFY_FILE = "verify-runs.json"
# Live boxes that can never be met as written (bus/unmeetable.py): ticket ->
# {"key": box text key, "reasons": [...], "at": checked}. A flagged box gets
# one restate ask and no verify lane until its text changes; a clean result
# is checked again after UNMEETABLE_RECHECK. Code searches have their own
# small GitHub budget, so at most UNMEETABLE_SEARCHES run a pass.
UNMEETABLE_FILE = "unmeetable-live.json"
UNMEETABLE_RECHECK = 24 * 3600
UNMEETABLE_SEARCHES = 4
# GitHub's code search index skips files of 384 KB and over, so a search
# that finds nothing says nothing about them: the station reads each such
# file itself (at most UNMEETABLE_BIG_READS a repo, else the answer is
# unknown) before it calls a string absent. The margin covers KB vs KiB.
UNINDEXED_BYTES = 350 * 1000
UNMEETABLE_BIG_READS = 8
# Files no log line is printed from; never read.
UNREAD_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".pdf", ".zip",
                   ".gz", ".tgz", ".bz2", ".xz", ".jar", ".whl", ".woff", ".woff2",
                   ".ttf", ".otf", ".mp3", ".mp4", ".webm", ".mov", ".so", ".dylib",
                   ".a", ".o", ".bin", ".exe", ".dll", ".wasm", ".sqlite", ".db", ".pyc")
LIVE_EVENT_MARKER = "<!-- loom:wait-live-event v1 -->"
# A shipped ticket whose proof was refused this many times leaves the verify
# queue and goes to the brain with the refusals listed (loom#1307): the
# retries every VERIFY_INTERVAL had handed one ticket out 18 times.
VERIFY_REFUSAL_CAP = 3
# Per ticket, the reasons its proofs were refused.
VERIFY_REFUSED_FILE = "verify-refused.json"
# A shipped ticket still unverified this long after its ship takes a lane
# ahead of build work (loom#964): verify used to run only on a lane with
# nothing else to do, so a busy factory never verified. At most one lane is
# taken per VERIFY_RESERVE_EVERY, and the last check is kept here.
VERIFY_RESERVE_AFTER = 2 * 3600
VERIFY_RESERVE_EVERY = 1800
VERIFY_RESERVE_FILE = "verify-reserve.json"
# The Fabric lead for a ticket's merged work (loom#1437): at most FABRIC_ASKS
# questions a pass, each up to bus/fabric.py's TIMEOUT, and the first
# FABRIC_CITES cited pull requests checked against GitHub. FABRIC_ASKED_FILE
# keeps the body each ticket was asked about, so a ticket is asked once.
# The step loop's share of a pass: past this many seconds from the pass's
# start, the remaining steps wait for the next pass (STEP_ROTATION_FILE keeps
# where it stopped). On 2026-10-24 every pass ran 25 to 40 minutes.
PASS_STEP_BUDGET = 300
# A whole pass's wall-clock bound (LOOM_PASS_DEADLINE, seconds; 0 turns it
# off). Past it faulthandler writes every thread's Python stack to the station
# log and exits, so launchd starts a fresh pass (loom#1921): on 2026-10-26 a
# pass sat for 27 minutes with nothing in the log to say where. The default
# sits above a legitimate daily metrics build (about 30 minutes on 2026-10-26)
# until that build is bounded on its own (loom#1924). DEADLINE_MARKER_FILE,
# written when the deadline is armed and removed when the pass ends, lets the
# next pass say the last one overran.
PASS_DEADLINE = 3600
DEADLINE_MARKER_FILE = "pass-deadline.json"
STEP_ROTATION_FILE = "step-rotation.json"
# A pass step that takes this long is logged with its time and requests, so a
# long pass names its cause: on 2026-10-24 passes ran 25 to 98 minutes and
# 3000 to 6300 requests, and nothing said which step spent them.
SLOW_STEP_SECONDS = 60
# A loom:ticket with no state label is queued (loom#1653), a few an hour.
STATELESS_EVERY = 3600
STATELESS_PER_RUN = 5
STATELESS_FILE = "stateless.json"
STATELESS_TEXT = ("Queued: this ticket carried no state label, so nothing would route it "
                  "to a lane (loom#1653).")
# Labels that mean someone or something already holds a ticket: any Loom
# state or hold, a person's claim, an operator ask, or a wait.
_HOLD_LABELS = {"loom:" + state for state in brain.STATES} | {
    "loom:triage", "loom:human", "loom:held", "loom:ready", "loom:awaiting-evidence",
    "loom:closed-unmerged", "needs-operator", "needs-brain"}


def _holds_ticket(name: str) -> bool:
    return name in _HOLD_LABELS or name.startswith(("claim:", "wait:"))


FABRIC_ASKS = 2
FABRIC_CITES = 3
FABRIC_ASKED_FILE = "fabric-asked.json"
# Each question is a whole-library Fabric chat, the costliest call there is.
# The ledger keys a ticket on its body without the status section: hashing the
# whole body re-asked a ticket after every status event and drained the
# workspace's monthly AI allowance in about four days (2026-10-24). A day's
# asks stop at FABRIC_ASKS_PER_DAY whatever the ledger says, leaving most
# of the allowance to the scheduled review jobs.
FABRIC_ASKS_PER_DAY = 10
FABRIC_QUESTION = (
    "A ticket in the GitHub repo %s is about to be built.\n\nTitle: %s\n\n"
    "Acceptance criteria:\n%s\n\n"
    "Which merged pull requests in that repo already do this work? Cite only "
    "pull request cards you can see, each as a link to its card. If none does, "
    "answer: none.")

# Per-lane transient verify failures and the holds they earn (loom#714).
VERIFY_LANES_FILE = "verify-lanes.json"
VERIFY_STRIKES = 2
VERIFY_LANE_HOLD = 3600
# Lanes that spend Anthropic models never take a verify run; the free and
# subscription lanes do (the claude lane spends the operator's subscription).
VERIFY_SKIP = ("anthropic", "claude")

# A live criterion passes only on something seen: a log line, a response, a
# page, with the time it was seen. A lane that read the code and wrote what
# the logs "would show" proved nothing (2026-10-15: a lane proof passed a
# live criterion that way and was moved back by hand).
_LIVE_CRITERION = tickets.LIVE_CRITERION_RE
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
               attempts: Dict[str, Any], now: float,
               refused: Optional[Dict[str, Any]] = None
               ) -> List[Tuple[str, Dict[str, Any]]]:
    """Shipped tickets without a proof, oldest ship event first, outside
    cooldown. A ticket whose proof was refused VERIFY_REFUSAL_CAP times
    (`refused`: ticket -> reasons) is left to the brain."""
    due = []
    for repo, issue in issues:
        labels = _label_names(issue)
        needles = live_needles(issue.get("body"))
        if needles is not None and not needles:
            continue
        key = "%s#%s" % (repo, issue.get("number"))
        if (issue.get("state") != "open" or "loom:ticket" not in labels
                or "loom:shipped" not in labels or "loom:verified" in labels
                or issue.get("has_proof")):
            continue
        last = attempts.get(key)
        if isinstance(last, (int, float)) and now - float(last) < VERIFY_INTERVAL:
            continue
        reasons = (refused or {}).get(key)
        if isinstance(reasons, list) and len(reasons) >= VERIFY_REFUSAL_CAP:
            continue
        due.append((repo, issue))
    # A ticket the health pass has proved absent for 24 hours goes first: it
    # needs a reader, not a long wait behind older shipped work.
    return sorted(due, key=lambda item: (
        not item[1].get("health_proof"),
        float(item[1].get("shipped_at", 0) or 0),
        item[0], int(item[1].get("number", 0))))


def live_needles(body: Optional[str]) -> Optional[List[str]]:
    """Needles for a Live criterion, or None when the ticket has no Live line."""
    live = brain.live_criterion(body, limit=None)
    if live == "(no Live criterion on the ticket)":
        return None
    return evidence.needles(live)


def _evidence_log_path(repo: str, command: str, log_root: str) -> Optional[str]:
    """Get the log file path for a repo and evidence command, or None.

    Only a path that resolves inside the configured log root is returned, so
    an evidence command cannot read a log outside it. A command that is not a
    file path (an absolute path or a name ending in ".log") returns None.
    """
    if not (os.path.isabs(command) or command.endswith(".log")):
        return None
    root = os.path.realpath(log_root)
    path = os.path.realpath(command)
    if not path.startswith(root + os.sep):
        return None
    return path


def _earliest_evidence_record(evidence_cmds: Dict[str, str], repo: str,
                              log_root: str) -> Optional[datetime]:
    """Get the earliest record from the evidence source for the given repo, or None.
    
    This reads the log file's modification time as a proxy for when the log
    started being written. If the command is not configured for this repo or
    the log file does not exist, return None; any other filesystem error
    (a permission or directory problem) is raised. The log path must live inside
    the configured log root.
    """
    command = evidence_cmds.get(repo)
    if not command:
        return None
    log_path = _evidence_log_path(repo, command, log_root)
    if not log_path:
        return None
    try:
        mtime = os.path.getmtime(log_path)
        return datetime.fromtimestamp(mtime, tz=timezone.utc)
    except (FileNotFoundError, NotADirectoryError):
        return None


def _unproven_host_checks(body: Any) -> List[str]:
    """The ticket's unticked criteria that only a check on a live host or
    network can meet (operator_hold.network_check): a merge proves none of
    them. A Live line keeps its own close rule (_done_reason)."""
    return [text for checked, text in proof.criteria(str(body or ""))
            if not checked and operator_hold.network_check(text)]


def _live_time_window(live: str) -> Optional[Tuple[Optional[datetime], Optional[datetime]]]:
    """Extract the time window from a Live criterion, or None when none exists.
    
    Returns (window_start, window_end) as datetime objects (either may be None),
    or None when the criterion has no time window (e.g., "Live: prints `foo`").
    """
    after_pattern = re.compile(r"on\s+or\s+after\s+(\d{4}-\d\d-\d\d)", re.I)
    before_pattern = re.compile(r"before\s+(\d{4}-\d\d-\d\d)", re.I)
    
    after_match = after_pattern.search(live)
    before_match = before_pattern.search(live)
    
    if not after_match and not before_match:
        return None
    
    try:
        window_start = (datetime.fromisoformat(after_match.group(1)).replace(tzinfo=timezone.utc)
                        if after_match else None)
        window_end = (datetime.fromisoformat(before_match.group(1)).replace(tzinfo=timezone.utc)
                      if before_match else None)
        return window_start, window_end
    except ValueError:
        return None


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
                 scope_url: str = "",
                 scope_model: str = "",
                 metrics_goal: str = "",
                 transcript_url: str = "",
                 fabric_key_ssm: str = "",
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
                 health_diagnose: Optional[Dict[str, str]] = None,
                 friction_mode: str = "",
                 evidence_cmds: Optional[Dict[str, str]] = None,
                 evidence_window_hours: float = EVIDENCE_WINDOW_HOURS,
                 live_evidence_repos: Sequence[str] = (),
                 live_evidence_env: Optional[Dict[str, str]] = None,
                 roll_ship_docs: Optional[List[Dict[str, str]]] = None,
                 pin_ship_repos: Sequence[str] = (),
                 briefers: Sequence[str] = (),
                 ship_on_merge: Sequence[str] = (),
                 ship_workflows: Optional[Dict[str, str]] = None,
                 ship_path_workflows: Sequence[str] = (),
                 no_topup: Sequence[str] = (),
                 log_keep_days: int = LOG_KEEP_DAYS,
                 disk_free_min_gb: float = DISK_FREE_MIN_GB,
                 settle_claimed: str = "off",
                 settle_claimed_max: int = claimed.PER_RUN,
                 claim_idle_hours: int = 0,
                 agy_workdir: str = "",
                 thin_snapshots: bool = False,
                 urgency_config: Optional[urgency.Config] = None,
                 pass_deadline: int = 0) -> None:
        self.home = home
        # LOOM_PASS_DEADLINE: seconds a whole pass may run before it dumps its
        # stack and exits (PASS_DEADLINE). 0, the default for a Settings
        # built in code, arms nothing.
        self.pass_deadline = pass_deadline
        # LOOM_URGENCY_*: score each ask before it is routed (bus/urgency.py,
        # loom#1787). Off for a Settings built in code; from_env defaults to
        # shadow, which records scores and still routes by the label rules.
        self.urgency = urgency_config or urgency.Config(mode=urgency.OFF)
        # LOOM_THIN_SNAPSHOTS: below twice the free-space floor, ask macOS to
        # thin its local Time Machine snapshots before claims pause. On, from
        # from_env, unless "0"; off for a Settings built in code.
        self.thin_snapshots = thin_snapshots
        # The agy lane's tree folder (lanes/run_lane.sh WORKDIR for LANE=agy),
        # pruned beside home/worktrees; empty, the default for a Settings
        # built in code, prunes home/worktrees only.
        self.agy_workdir = agy_workdir
        # LOOM_CLAIM_IDLE_HOURS: another operator's claim its claimer has left
        # alone this long comes back to this Loom (bus/stale_claims.py); 0, the
        # default for a Settings built in code, is off; from_env defaults to 24.
        self.claim_idle_hours = claim_idle_hours
        # LOOM_SETTLE_CLAIMED: "on" settles operator-claimed issues whose pull
        # requests merged, "dry" (from_env's default) logs what it would do,
        # "off" skips it; off for a Settings built in code, like the factory
        # (bus/claimed.py). LOOM_SETTLE_CLAIMED_MAX caps the issues a run settles.
        self.settle_claimed = settle_claimed
        self.settle_claimed_max = settle_claimed_max
        # LOOM_NO_TOPUP: provider lanes (anthropic) whose credit the operator
        # never tops up. Their slots held out of credit wait for the credit
        # to renew, so the lane-held invariant skips them. Empty (the
        # default) keeps the invariant on every held lane.
        self.no_topup = [lane.lower() for lane in no_topup]
        # Days a lane's logs are kept (0: forever), and the free disk under
        # which every pass warns (Station.tend_disk).
        self.log_keep_days = log_keep_days
        self.disk_free_min_gb = disk_free_min_gb
        # Repos whose merged tickets ship at merge (ship_on_merge), and repos
        # whose merged tickets ship when their deploy workflow succeeds on the
        # merge commit, repo to workflow file (ship_workflows).
        self.ship_on_merge = list(ship_on_merge)
        self.ship_workflows = dict(ship_workflows or {})
        # Repos with many path-scoped deploy workflows (ship_paths.py).
        self.ship_path_workflows = list(ship_path_workflows)
        # Deployment repos whose merged pin pull requests ship the tickets
        # of the source revision they publish (ship_pins).
        self.pin_ship_repos = list(pin_ship_repos)
        # "owner/name#goal=https://..." entries: each goal home's health
        # endpoint (bus/health.py, docs/HEALTH.md), read every health_every s.
        self.health_urls = health_urls
        self.health_every = health_every
        # "owner/name#goal" -> the goal home's read-only diagnosis command,
        # whose output every health brief quotes (docs/HEALTH.md, loom#1243).
        self.health_diagnose = health_diagnose or {}
        # LOOM_FRICTION_INTAKE: "on" files a ticket for a slowdown the tick
        # journals keep naming, "dry" logs what it would file, "" is off
        # (bus/friction_intake.py).
        self.friction_mode = friction_mode
        self.evidence_cmds = evidence_cmds or {}
        # LOOM_EVIDENCE_WINDOW_HOURS: the hours of log the evidence command
        # searches, which bounds the absence a verify can prove (a command that
        # prints loom-evidence-window-hours: N overrides it per run).
        self.evidence_window_hours = evidence_window_hours
        # LOOM_LIVE_EVIDENCE_REPOS: repos whose .loom/live-evidence.json
        # adapter the live proof may run (bus/live_adapter.py, loom#1770);
        # LOOM_LIVE_EVIDENCE_PASS_ENV names the station variables (a
        # read-only kubeconfig) an adapter is handed. Empty is off.
        self.live_evidence_repos = list(live_evidence_repos)
        self.live_evidence_env = dict(live_evidence_env or {})
        self.roll_ship_docs = roll_ship_docs or []
        # Local times (hour, minute) to send one digest of every goal's
        # reports; empty forwards each report as it is posted.
        self.report_digest = tuple(report_digest)
        self.repos = repos
        self.authors = authors
        # LOOM_TRUSTED_BRIEFERS: another operator's logins whose loom:ticket
        # briefs this Loom builds (tickets.brief_author_trusted). Ownership,
        # journals, proofs, adoption and merges read authors alone.
        self.briefers = list(briefers)
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
        # The keyless chat route and model the scope check asks (bus/scope_fit.py);
        # no URL, no check.
        self.scope_url = scope_url
        self.scope_model = scope_model
        self.metrics_goal = metrics_goal
        self.transcript_url = transcript_url
        # SSM name of the Fabric workspace key (bus/fabric.py); empty, no question.
        self.fabric_key_ssm = fabric_key_ssm
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
                   scope_url=env.get("LOOM_SCOPE_CHECK_URL", "").strip(),
                   scope_model=env.get("LOOM_SCOPE_CHECK_MODEL", "").strip(),
                   metrics_goal=env.get("LOOM_METRICS_GOAL", "").strip(),
                   transcript_url=env.get("LOOM_TRANSCRIPT_URL", "").strip(),
                   fabric_key_ssm=env.get("LOOM_FABRIC_KEY_SSM", "").strip(),
                   transcript_token_ssm=env.get("LOOM_TRANSCRIPT_TOKEN_SSM", "").strip(),
                   watched=watched,
                   rewrite_cmd=env.get("LOOM_REWRITE_CMD", "").strip(),
                   rewrite_private_cmd=env.get("LOOM_REWRITE_PRIVATE_CMD", "").strip(),
                   self_repo=env.get("LOOM_SELF_REPO", "").strip(),
                   invariant_goal=env.get("LOOM_INVARIANT_GOAL", "").strip(),
                   plan_check=(env.get("LOOM_PLAN_CHECK", "").strip()
                               or invariants.DEFAULT_PLAN_CHECK),
                   evidence_cmds=_evidence_commands(env),
                   evidence_window_hours=_hours(env.get("LOOM_EVIDENCE_WINDOW_HOURS", ""),
                                                EVIDENCE_WINDOW_HOURS),
                   live_evidence_repos=live_adapter.opted_in(
                       env.get("LOOM_LIVE_EVIDENCE_REPOS", "")),
                   live_evidence_env=live_adapter.passed_env(
                       env.get("LOOM_LIVE_EVIDENCE_PASS_ENV", ""), env),
                   health_urls=env.get("LOOM_HEALTH_URLS", "").strip(),
                   health_every=_seconds(env.get("LOOM_HEALTH_EVERY", ""), health.EVERY),
                   health_diagnose=_health_diagnose(env),
                   friction_mode=friction_intake.mode(env.get("LOOM_FRICTION_INTAKE", "")),
                   roll_ship_docs=roll_pin.specs_from_env(env.get("LOOM_ROLL_SHIP_DOCS", "")),
                   pin_ship_repos=words("LOOM_PIN_SHIP_REPOS"),
                   briefers=words("LOOM_TRUSTED_BRIEFERS"),
                   ship_on_merge=words("LOOM_SHIP_ON_MERGE"),
                   ship_workflows=_ship_workflows(words("LOOM_SHIP_WORKFLOWS")),
                   ship_path_workflows=words("LOOM_SHIP_PATH_WORKFLOWS"),
                   no_topup=words("LOOM_NO_TOPUP"),
                   log_keep_days=_keep_days(env.get("LOOM_LOG_KEEP_DAYS", "")),
                   disk_free_min_gb=_free_floor(env.get("LOOM_MIN_FREE_GB", "").strip()
                                                or env.get("LOOM_DISK_FREE_MIN_GB", "")),
                   settle_claimed=claimed.mode(env.get("LOOM_SETTLE_CLAIMED", "")),
                   settle_claimed_max=claimed.per_run(env.get("LOOM_SETTLE_CLAIMED_MAX", "")),
                   claim_idle_hours=stale_claims.idle_hours(env.get("LOOM_CLAIM_IDLE_HOURS", "")),
                   agy_workdir=_agy_workdir(env),
                   thin_snapshots=env.get("LOOM_THIN_SNAPSHOTS", "1").strip() != "0",
                   urgency_config=urgency.Config.from_env(env),
                   pass_deadline=_pass_deadline(env.get("LOOM_PASS_DEADLINE", "")))


def _agy_workdir(env: Dict[str, str]) -> str:
    """The agy lane's tree folder, with lanes/run_lane.sh's defaults."""
    if env.get("LOOM_AGY_WORKDIR"):
        return env["LOOM_AGY_WORKDIR"]
    home = env.get("LOOM_AGY_HOME") or "/Users/%s" % (env.get("LOOM_AGY_USER") or "loom")
    return os.path.join(home, "lanes")


def _keep_days(raw: str) -> int:
    """LOOM_LOG_KEEP_DAYS: a whole number of days, 0 for forever; anything
    else is said once and the default kept."""
    raw = raw.strip()
    if not raw:
        return LOG_KEEP_DAYS
    if raw.isdigit():
        return int(raw)
    _say("ignoring LOOM_LOG_KEEP_DAYS %r: not a whole number of days" % raw)
    return LOG_KEEP_DAYS


def _free_floor(raw: str) -> float:
    """LOOM_MIN_FREE_GB (or the older LOOM_DISK_FREE_MIN_GB): a non-negative
    number of GB, else the default."""
    raw = raw.strip()
    if not raw:
        return DISK_FREE_MIN_GB
    try:
        value = float(raw)
    except ValueError:
        value = -1.0
    if not (value >= 0 and math.isfinite(value)):
        _say("ignoring LOOM_MIN_FREE_GB %r: not a number of GB" % raw)
        return DISK_FREE_MIN_GB
    return value


def _gb(n: float) -> str:
    return "%.1f GB" % (n / GB)


def _thin_snapshots(path: str, want: int) -> bool:
    """Ask macOS to thin the local snapshots on path's volume until about
    want bytes are free (tmutil, urgency 4, no root needed)."""
    try:
        done = subprocess.run(["tmutil", "thinlocalsnapshots", path, str(want), "4"],
                              capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def _tree_stat(path: str) -> Tuple[float, int]:
    """(newest mtime, bytes on disk) of a path and, for a real directory,
    everything under it. Links are counted as themselves, never followed."""
    st = os.lstat(path)
    newest, size = st.st_mtime, _disk_bytes(st)
    if stat.S_ISDIR(st.st_mode):
        for root, dirs, files in os.walk(path, followlinks=False):
            for name in dirs + files:
                try:
                    st = os.lstat(os.path.join(root, name))
                except OSError:
                    continue
                newest, size = max(newest, st.st_mtime), size + _disk_bytes(st)
    return newest, size


def _disk_bytes(st: os.stat_result) -> int:
    blocks = getattr(st, "st_blocks", None)
    return blocks * 512 if isinstance(blocks, int) else st.st_size


def _remove(path: str) -> None:
    """Remove a file, a link (never what it points at) or a whole folder."""
    if stat.S_ISDIR(os.lstat(path).st_mode):
        shutil.rmtree(path, ignore_errors=True)
    else:
        os.unlink(path)


def _gzip_session_db(run: str) -> Optional[Tuple[int, int]]:
    """gzip a run log folder's opencode.db (SESSION_DB_PARTS) in place:
    (bytes before, bytes after), or None when it holds no store. Every folder
    on the way must be a real folder and the store a regular file: nothing
    is followed through a link. The store's and its folder's times are
    kept."""
    parent = run
    for part in SESSION_DB_PARTS[:-1]:
        try:
            st = os.lstat(parent)
        except FileNotFoundError:
            return None
        if not stat.S_ISDIR(st.st_mode):
            return None
        parent = os.path.join(parent, part)
    try:
        folder = os.lstat(parent)
        if not stat.S_ISDIR(folder.st_mode):
            return None
        db = os.path.join(parent, SESSION_DB_PARTS[-1])
        st = os.lstat(db)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    target = db + ".gz"
    tmp = target + ".tmp"
    fd = os.open(db, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        with os.fdopen(fd, "rb") as src, gzip.open(tmp, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
        os.utime(tmp, (st.st_atime, st.st_mtime))
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.unlink(db)
    for side in ("-wal", "-shm"):
        try:
            os.unlink(db + side)
        except FileNotFoundError:
            pass
    os.utime(parent, (folder.st_atime, folder.st_mtime))
    return st.st_size, os.lstat(target).st_size


def _ship_workflows(items: List[str]) -> Dict[str, str]:
    """OWNER/NAME=WORKFLOW_FILE words as a map; a malformed word is skipped
    and said once."""
    found: Dict[str, str] = {}
    for item in items:
        repo, _, workflow = item.partition("=")
        if tickets.REPO_RE.fullmatch(repo) and re.fullmatch(r"[\w.-]+\.ya?ml", workflow):
            found[repo] = workflow
        else:
            _say("ignoring %r in LOOM_SHIP_WORKFLOWS: not OWNER/NAME=WORKFLOW.yml" % item)
    return found


_PULL_REQUEST_TRIGGER = re.compile(
    r"(?m)^\s*pull_request\s*(?::|,|\])|^\s*on:\s*\[[^\]]*pull_request")


def _has_pull_request_trigger(workflow: str) -> bool:
    """Whether a workflow's text runs on pull_request."""
    return bool(_PULL_REQUEST_TRIGGER.search(workflow))


def _is_code_scanning_check(name: str, run: Dict[str, Any]) -> bool:
    """Whether a check run is code scanning, not the repo's own lane CI."""
    low = str(name).lower()
    if "code scanning" in low or "codeql" in low:
        return True
    app = run.get("app") or {}
    return str(app.get("slug") or "").lower() in ("codeql", "code-scanning")


def _block_scalar(lines: List[str], index: int, indent: int) -> Tuple[str, int]:
    """Join a run: | block's lines deeper than indent; return it and the next index."""
    block = []
    while index < len(lines):
        line = lines[index]
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        block.append(line.strip())
        index += 1
    return " ".join(part for part in block if part), index


def _workflow_run_steps(path: str, workflow: str) -> List[Tuple[str, str]]:
    """Each non-empty run step of a workflow as (where it comes from, command)."""
    lines = workflow.splitlines()
    found: List[Tuple[str, str]] = []
    index = 0
    step_name = ""
    run_number = 0
    while index < len(lines):
        line = lines[index]
        if re.match(r"^\s*-\s+", line):
            step_name = ""
        named = re.match(r"^\s*-\s*name:\s*(.+?)\s*$", line)
        if named:
            step_name = named.group(1).strip("\"'")
        index += 1
        match = re.match(r"^\s*(?:-\s*)?run:\s*(.*)$", line)
        if not match:
            continue
        run_number += 1
        command = match.group(1).strip()
        if command in ("|", ">", "|-", ">-"):
            command, index = _block_scalar(lines, index, len(line) - len(line.lstrip()))
        if command:
            found.append(("%s, step %s" % (path, step_name or str(run_number)), command))
    return found


def _health_diagnose(env: Dict[str, str]) -> Dict[str, str]:
    commands, problem = health.diagnose_commands(env.get("LOOM_HEALTH_DIAGNOSE_CMDS", ""))
    if problem:
        _say("ignoring LOOM_HEALTH_DIAGNOSE_CMDS: %s" % problem)
    return commands


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


def _hours(raw: str, default: float) -> float:
    """A positive number of hours, else default."""
    try:
        value = float(raw.strip())
    except ValueError:
        return default
    return value if value > 0 else default


def _seconds(raw: str, default: int) -> int:
    """A whole number of seconds of at least 60, else default."""
    raw = raw.strip()
    return int(raw) if raw.isdigit() and int(raw) >= 60 else default


def _pass_deadline(raw: str) -> int:
    """LOOM_PASS_DEADLINE: 0 turns the deadline off; a whole number of
    seconds of at least 60 is used; anything else is PASS_DEADLINE."""
    raw = raw.strip()
    if raw == "0":
        return 0
    if raw:
        value = _seconds(raw, 0)
        if value:
            return value
        _say("ignoring LOOM_PASS_DEADLINE %r: not a whole number of seconds, at least 60"
             % raw)
    return PASS_DEADLINE


def _deadline_stream() -> Any:
    """The stream faulthandler writes to: the station log (stderr, as launchd
    runs the station), or the process's own stderr when sys.stderr has no
    file descriptor (a test capturing it)."""
    try:
        sys.stderr.fileno()
        return sys.stderr
    except (AttributeError, OSError, ValueError):
        return sys.__stderr__


def _arm_pass_deadline(state: str, seconds: int, now: float) -> Optional[str]:
    """Say whether the last pass overran, then arm this pass's deadline and
    write its marker. Returns the marker's path, or None when off."""
    path = os.path.join(state, "station", DEADLINE_MARKER_FILE)
    last = _read_json(path, None, strict=False)
    if isinstance(last, dict):
        started = last.get("started")
        limit = last.get("deadline")
        if isinstance(started, (int, float)) and isinstance(limit, int):
            ran = int(now - started)
            if ran >= limit:
                _say("the last pass (pid %s, started %s) overran its %ds deadline: its stack is "
                     "in this log above" % (last.get("pid"), time.strftime(
                         "%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)), limit))
            else:
                _say("the last pass (pid %s) ended %ds into its %ds deadline without a clean "
                     "finish (killed or crashed)" % (last.get("pid"), ran, limit))
    if seconds <= 0:
        _discard(path)
        return None
    _write_json(path, {"pid": os.getpid(), "started": now, "deadline": seconds})
    faulthandler.dump_traceback_later(seconds, exit=True, file=_deadline_stream())
    return path


def _discard(path: str) -> None:
    """Remove a file that may already be gone."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _try_arm_pass_deadline(state: str, seconds: int, now: float) -> Optional[str]:
    """_arm_pass_deadline, but a failure to arm is logged and the pass runs
    unarmed rather than dying before it starts: the deadline is a backstop,
    and losing it must not stop the station."""
    try:
        return _arm_pass_deadline(state, seconds, now)
    except Exception as err:  # noqa: BLE001 - any arm failure must not stop the pass
        _say("could not arm the pass deadline (%s: %s); this pass runs without one"
             % (type(err).__name__, err))
        try:
            faulthandler.cancel_dump_traceback_later()
            _discard(os.path.join(state, "station", DEADLINE_MARKER_FILE))
        except OSError as cleanup:
            _say("could not clear the pass deadline marker: %s" % cleanup)
        return None


def _disarm_pass_deadline(path: Optional[str]) -> None:
    """Cancel this pass's deadline and remove its marker."""
    if path is None:
        return
    faulthandler.cancel_dump_traceback_later()
    _discard(path)


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


def _muse_rules() -> Tuple[str, str]:
    """(commit, Rules section) of docs/MUSE.md in this checkout, the one
    lanes/mac/station.sh deploys: the commit is the last one that changed the
    file. ("", "") when git or the file cannot say."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        out = subprocess.run(["git", "-C", root, "log", "-1", "--format=%H", "--", "docs/MUSE.md"],
                             capture_output=True, text=True, timeout=30)
        with open(os.path.join(root, "docs", "MUSE.md"), encoding="utf-8") as fh:
            doc = fh.read()
    except (OSError, subprocess.SubprocessError) as err:
        _say("could not read docs/MUSE.md for the rules digest (%s)" % type(err).__name__)
        return "", ""
    commit = out.stdout.strip()
    if out.returncode != 0 or not COMMIT_RE.fullmatch(commit):
        _say("could not find the commit of docs/MUSE.md for the rules digest (git exit %d)"
             % out.returncode)
        return "", ""
    return commit, brain_memory.rules_section(doc)


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
    # The UTC time leads the line, so evidence that quotes a station log line
    # can name when it was seen (loom#1307).
    print("[%s] station: %s" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), msg),
          file=sys.stderr)


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
    """The provider lane of a slot (anthropic-2 -> anthropic, free-kilo ->
    free), as the runner maps it."""
    if re.fullmatch(r"free-[a-z]+", lane):
        return "free"
    return re.sub(r"-[0-9]+$", "", lane)


# Failure classes that did no work on the ticket (loom#572).
NOT_WORK = ("factory", "transient", "limit", "auth")


def failure_class(reason: str) -> str:
    m = CLASS_RE.match(reason)
    return m.group(1) if m else "work"


def ending_class(reason: str) -> str:
    """failure_class, except that a fix round whose branch moved is
    [transient]: the runner parks it under the default [work] class, but the
    model did nothing wrong (_branch_moved)."""
    return "transient" if CLASS_RE.sub("", reason).startswith(BRANCH_MOVED) else failure_class(reason)


def fix_pr_merged(fix_pr: str, reason: str) -> bool:
    """Whether a fix brief was parked unrun because its own pull request,
    fix_pr, merged before the lane ran it (PR_GONE_RE)."""
    m = PR_GONE_RE.match(CLASS_RE.sub("", reason))
    return bool(m) and m.group(1) == fix_pr and m.group(2) == "MERGED"


def lane_takes_model(lane: str, model: str) -> bool:
    """Whether the lane's runner accepts the model (lanes/run_lane.sh)."""
    lane = provider(lane)
    if lane == "codex":
        return not any(fnmatch.fnmatchcase(model, p) for p in CLAUDE_MODELS)
    return any(fnmatch.fnmatchcase(model, p) for p in LANE_MODELS.get(lane, []))


def brief_model(lane: str, headers: Dict[str, str]) -> str:
    """The model a brief for this lane runs when the runner must keep it: the
    ticket's Model: line, else a hard ticket's HARD_MODELS model (as
    tickets.render_brief writes it); "" when the lane picks its own."""
    if headers.get("Model"):
        return headers["Model"]
    if headers.get("Difficulty") == "hard":
        return factory.HARD_MODELS.get(lane, "")
    return ""


def model_lanes(model: str) -> List[str]:
    """Compatible lanes, with the model ladder's lanes first when applicable."""
    rung = next((lanes for lanes, rung_model in factory.LADDER if rung_model == model), ())
    return [lane for lane in dict.fromkeys([*rung, *LANES])
            if lane not in REMOTE_LANES and lane_takes_model(lane, model)]


def beat_fresh(at: float, now: float, window: float = HEARTBEAT_MAX_AGE) -> bool:
    """Whether a heartbeat stamped `at` is at most `window` seconds older than
    the pass time `now` and at most a minute ahead of the clock. A pass reads
    beats long after it began, so a beat written since is fresh: measured
    against `now` alone, every lane that beat in a pass's first minutes read
    as down and the station gave no queued ticket a lane (loom#717)."""
    return now - at <= window and at - max(now, time.time()) <= 60


def remote_beat_fresh(lane: str, at: Optional[float], now: float) -> bool:
    """Whether a remote lane's last heartbeat still counts as up: within its
    configured beat_max (labels.REMOTE_LANES), else HEARTBEAT_MAX_AGE."""
    window = REMOTE_LANES.get(lane, {}).get("beat_max", HEARTBEAT_MAX_AGE)
    return at is not None and beat_fresh(at, now, window)


def hold_expired(beat: Dict[str, Any], now: float) -> bool:
    """Whether a lane's limit hold has run out: the reset it names is in the
    past. A beat with no reset has no known end, so its hold stands. The Usage
    view reads the same heartbeat the same way (bus/usage.py _limit_hold)."""
    until = beat.get("retry_at")
    if isinstance(until, bool) or not isinstance(until, (int, float)):
        return False
    return until <= now


def needs_public_repo(lane: str, model: str) -> bool:
    """Free lanes, and lanes whose provider trains on prompts, take public
    repos only. A slot named in factory.PUBLIC_ONLY counts before it is mapped
    to its provider: free-nvidia, free-kilo and free-groq are free provider
    slots that may train (infra#5617), while free-ollama and free-gemma may
    take private repos."""
    if lane in factory.PUBLIC_ONLY:
        return True
    # A cluster runner's inputs and logs live in this public repo's Actions
    # (bus/runners.py).
    if lane in runners.runner_slots():
        return True
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
    the full `Refs OWNER/NAME#N` form or the same-repo `Refs #N` form; the
    line may carry trailing words after the number."""
    return [int(n) for n in re.findall(r"^Refs (?:%s)?#([0-9]+)\b" % re.escape(repo),
                                       body, re.M)]


def _linked_pr(issue: Dict[str, Any], repo: str, pr: Dict[str, Any]) -> bool:
    number = int(issue.get("number", 0))
    return bool(number in _refs_numbers(repo, str(pr.get("body") or ""))
                or re.match(r"loom/%d(?:-|$)" % number,
                            str((pr.get("head") or {}).get("ref") or "")))


def _covers_pr(issue: Dict[str, Any], repo: str, pr: Dict[str, Any]) -> bool:
    """A pull request linked to the ticket, or one whose Refs line names the
    issue the ticket was rewritten from (its "Rewritten from issue" line)."""
    if _linked_pr(issue, repo, pr):
        return True
    source = tickets.rewritten_source(issue.get("body"))
    return source is not None and source in _refs_numbers(repo, str(pr.get("body") or ""))


def adoption_body(pr: int) -> str:
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
        ADOPTS_MARKER % pr, ""])


def adoption_comment(repo: str, ticket: int) -> str:
    """The marker the station leaves on a pull request it adopted."""
    return ("%s\nLoom adopted this pull request: no ticket linked it, so ticket #%d now owns "
            "it. The lane that takes the ticket lands this change on a `loom/` branch, or "
            "closes this pull request as superseded with the reason."
            % (ADOPTED_MARKER % (repo, ticket), ticket))


# A ready mark: a status event naming the head whose checks and review passed.
# Plain text, because a status event is shown to readers as it is written.
READY_TEXT = ("Ready: checks and review passed on head %s; the factory lands the pull "
              "request in its turn.")
READY_RE = re.compile(r"Ready: checks and review passed on head ([0-9a-f]{40})\b")
# A ticket marked ready before the status event kept a ready-head comment of
# its own; it is still read.
OLD_READY_RE = re.compile(r"<!-- loom:ready-head ([0-9a-f]+) -->")
# The station's own pre-check of a lane pull request's diff (bus/weakened.py):
# a head that weakens its tests is sent back under this name, as if a check
# by that name had failed with the weakened lines as its output, and the
# ready mark above is withheld.
WEAKENED_CHECK = "weakened-test pre-check"


def _unindexed_entries(tree: List[Any]) -> List[Dict[str, Any]]:
    """The blobs of a git tree listing the code search index skips
    (UNINDEXED_BYTES and over), leaving out files no log line is printed from."""
    return [entry for entry in tree if isinstance(entry, dict)
            and entry.get("type") == "blob" and isinstance(entry.get("size"), int)
            and entry["size"] >= UNINDEXED_BYTES
            and not str(entry.get("path") or "").lower().endswith(UNREAD_SUFFIXES)]


def parse_roll_signals(text: str) -> List[Tuple[str, str, str]]:
    """The roll records a ROLL_SIGNAL_FILE declares: (repo, path, key), one
    a line, `#` comments. Raises on a malformed line, so a typo is never read
    as 'nothing rolls this repo'."""
    found = []
    for raw in text.splitlines():
        words = raw.split("#", 1)[0].split()
        if not words:
            continue
        if len(words) != 3:
            raise ValueError("%s: a line is <OWNER/NAME> <path> <key>" % ROLL_SIGNAL_FILE)
        repo, path, key = words
        if (not tickets.REPO_RE.fullmatch(repo) or path.startswith("/")
                or any(part in ("", ".", "..") for part in path.split("/"))
                or not ROLL_KEY_RE.fullmatch(key)):
            raise ValueError("%s: %r is not <OWNER/NAME> <path> <key>"
                             % (ROLL_SIGNAL_FILE, raw.strip()))
        found.append((repo, path, key))
    return found


def rolled_commit(record: str, key: str) -> str:
    """The full commit a roll record's `<key>=<sha>` or `<key>: <sha>` line
    names (quotes allowed); "" when it names none, or two different ones."""
    pattern = re.compile(r"^\s*(?:export\s+)?%s\s*[=:]\s*[\"']?([0-9a-f]{40})[\"']?\s*(?:#.*)?$"
                         % re.escape(key), re.M)
    shas = set(pattern.findall(record))
    return shas.pop() if len(shas) == 1 else ""


class Station:
    def __init__(self, gh: GitHub, settings: Settings,
                 now: Optional[float] = None,
                 read_secret: Optional[SecretReader] = None,
                 post: Optional[Poster] = None,
                 discord: Optional[DiscordCall] = None,
                 upload: Optional[Uploader] = None,
                 ancestor: Optional[Callable[[str, str], bool]] = None,
                 head: Optional[Callable[[], str]] = None,
                 fetch: Optional[Fetcher] = None,
                 urgency_client: Optional[urgency.Client] = None) -> None:
        self.gh = gh
        # The urgency model's client; None reads the key from SSM when a score
        # is first wanted (Station._urgency_client).
        self._urgency_injected = urgency_client
        self._digest_ranked = False  # act mode: the digest keeps the score order
        # Status comments by the station's trusted writers read as events.
        writers = getattr(gh, "status_authors", None)
        if isinstance(writers, set):
            writers |= {a.lower() for a in settings.authors}
        # A fix-rounds comment reads as its rounds only while it is exactly
        # as this Loom last wrote it (bus/fixrounds.py ledger, loom#1640).
        if hasattr(gh, "fix_ledger"):
            gh.fix_ledger = fixrounds.ledger_path(settings.home)
        self.s = settings
        self._live_budget = LIVE_CHECKS_PER_PASS
        self._proof_budget = LIVE_PROOFS_PER_PASS
        # Rewrite agent jobs this pass started, by issue (_start_rewrite).
        self._rewrite_procs: Dict[str, Any] = {}
        self.read_secret = read_secret or read_ssm
        self.post = post or post_webhook
        self.upload = upload or post_transcript
        self.fetch = fetch or brain_memory.fetch_handoff
        self.ancestor = ancestor or _is_ancestor
        self.head = head or _checkout_head
        # Where Claude Code and codex keep their sessions (the claude lane's
        # and the brain's transcripts, and codex rollouts).
        self.claude_projects = Path.home() / ".claude" / "projects"
        self.codex_sessions = Path.home() / ".codex" / "sessions"
        self.discord = discord or discord_call
        self._goals: Optional[List[Tuple[str, Dict[str, Any]]]] = None
        self.now = time.time() if now is None else now
        self._pass_started = time.monotonic()
        self.state = os.path.join(settings.home, "state", "station")
        self._fabric_asks = 0  # Fabric questions this pass (FABRIC_ASKS)
        self.event_log = os.path.join(settings.home, "logs", "events.jsonl")
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
        # Free bytes on the loom home's disk, read once a pass (_disk_free).
        self._free: Optional[int] = None
        self._free_read = False
        # repo -> open ticket numbers whose lane trees may still serve a fix
        # round, from this pass's reconcile read (prune_trees).
        self._open_work: Dict[str, Set[int]] = {}
        # Log data sections the verify gate already read this pass, by ticket.
        self._evidence_text: Dict[str, str] = {}
        # This pass's factory-repair tickets over the unproven-work limit, per
        # (repo, goal): running, at a pull request, or let through (loom#767).
        self._repair_flying: Dict[Tuple[str, int], List[int]] = {}
        self._parents: Dict[Tuple[str, int], Dict[str, Any]] = {}
        # This pass's closed pull requests and default branches, per repo:
        # the hand-revert scan reads the same pages as the ship checks.
        self._closed_cache: Dict[str, List[Dict[str, Any]]] = {}
        # The claimed settle's re-review requests, by "repo#pull request", as
        # saved in claimed.json, and how many it has written this run.
        self._claimed_rereviews: Dict[str, Any] = {}
        self._claimed_writes = 0
        self._branch_cache: Dict[str, str] = {}
        self._stale_asks_withdrawn = 0
        # Per pass: each path-scoped repo's workflow filters, and its deploy runs.
        self._path_filters: Dict[str, Dict[str, List[str]]] = {}
        self._manual_listed: Dict[str, List[str]] = {}
        self._path_runs: Dict[Tuple[str, str], Optional[Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]]] = {}
        # Repos whose claim:<login> label this pass has made sure of.
        self._claim_labels: set = set()
        self._issue_stamps: Dict[Tuple[str, int], str] = {}
        saved_reads = _read_json(os.path.join(self.state, "ticket_reads.json"), {}, strict=False)
        self._ticket_reads: Dict[str, Any] = saved_reads if isinstance(saved_reads, dict) else {}
        if hasattr(self.gh, "target") and self.gh.target is None:
            self.gh.target = PASS_REQUEST_TARGET

    # -- paths ---------------------------------------------------------
    def inbox(self, lane: str) -> str:
        return os.path.join(self.s.home, "briefs", lane)

    def _repo_ok(self, repo: str) -> bool:
        return repo in self.s.repos and bool(tickets.REPO_RE.fullmatch(repo))

    # -- reading GitHub ------------------------------------------------
    def _remember_issues(self, repo: str, items: Any) -> None:
        if not isinstance(items, list):
            return
        for issue in items:
            if not isinstance(issue, dict) or not isinstance(issue.get("updated_at"), str):
                continue
            try:
                number = int(issue["number"])
            except (KeyError, TypeError, ValueError):
                continue
            self._issue_stamps[(repo, number)] = issue["updated_at"]

    def _paginate(self, path: str) -> List[Any]:
        issue_list = re.match(r"^/repos/([^/]+/[^/]+)/issues\?", path)
        if issue_list:
            items = self.gh.paginate(path)
            self._remember_issues(issue_list.group(1), items)
            return items
        detail = re.match(r"^/repos/([^/]+/[^/]+)/issues/(\d+)/(comments|events)(?:\?.*)?$", path)
        if detail is None or ("?" in path and "since=" in path):
            return self.gh.paginate(path)
        repo, number_text, kind = detail.groups()
        number = int(number_text)
        stamp = self._issue_stamps.get((repo, number))
        if (not stamp or not hasattr(self.gh, "issue_writes")
                or self.gh.comment_edits or (repo, number) in self.gh.issue_writes):
            return self.gh.paginate(path)
        key = "%s#%d/%s" % (repo, number, kind)
        kept = self._ticket_reads.get(key)
        if isinstance(kept, dict) and kept.get("stamp") == stamp and isinstance(kept.get("data"), list):
            kept["seen"] = self.now
            return kept["data"]
        carried_before = dict(getattr(self.gh, "carried_over", {}))
        items = self.gh.paginate(path)
        carried_after = getattr(self.gh, "carried_over", {})
        if carried_before == carried_after:
            self._ticket_reads[key] = {"stamp": stamp, "data": items, "seen": self.now}
        return items

    def _issue(self, repo: str, number: int) -> Dict[str, Any]:
        data = self.gh.rest("GET", "/repos/%s/issues/%d" % (repo, number)).data
        if not isinstance(data, dict):
            raise GitHubError("no issue %s#%d" % (repo, number))
        self._remember_issues(repo, [data])
        data.setdefault("repository_url", "%s/repos/%s" % (self.gh.api, repo))
        return data

    def _editor(self, repo: str, number: int) -> Optional[str]:
        owner, name = repo.split("/")
        data = self.gh.graphql(
            "query($owner:String!,$name:String!,$number:Int!){"
            "repository(owner:$owner,name:$name){issue(number:$number){"
            "editor{__typename login} lastEditedAt}}}",
            {"owner": owner, "name": name, "number": number})
        issue = ((data or {}).get("repository") or {}).get("issue")
        if issue is None:
            raise GitHubError("graphql returned no issue %s#%d" % (repo, number))
        editor = issue.get("editor")
        if editor and editor.get("login"):
            login = str(editor["login"])
            # GraphQL names a GitHub App "loom-app"; REST, and so
            # LOOM_BRIEF_AUTHORS, name it "loom-app[bot]".
            if editor.get("__typename") == "Bot" and not login.endswith("[bot]"):
                login += "[bot]"
            return login
        # Edited, but GitHub names no editor (a deleted account): nobody on
        # the list can vouch for this body.
        return "(deleted account)" if issue.get("lastEditedAt") else None

    def _check_trust(self, repo: str, issue: Dict[str, Any]) -> Tuple[bool, str]:
        number = int(issue["number"])
        events = self._paginate("/repos/%s/issues/%d/events" % (repo, number))
        if self._target_reached():
            return False, "target reached"
        editor = self._editor(repo, number)
        return tickets.trusted(issue, events, editor, self.s.authors, self.s.owners,
                               self.s.briefers)

    def _poll(self, repo: str) -> List[Dict[str, Any]]:
        # A conditional read (bus/github.py keeps the last answer): an
        # unchanged list is a 304, which the budget does not count.
        resp = self.gh.rest(
            "GET", "/repos/%s/issues?labels=loom:briefed&state=open&per_page=100" % repo)
        items = resp.data if isinstance(resp.data, list) else []
        self._remember_issues(repo, items)
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
                except RateLimited:
                    raise
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

    def _event(self, kind: str, reason: str, item: str = "", lane: str = "",
               model: str = "") -> None:
        """Append one redacted decision event and rotate its log by UTC day.
        The log is a record of decisions, not a decision: a disk error is
        logged and the event dropped, so it cannot fail the step that made
        the decision (loom#763)."""
        try:
            self._write_event(kind, reason, item, lane, model)
        except OSError as err:
            _say("could not write %s to the event log: %s" % (kind, err))

    def _write_event(self, kind: str, reason: str, item: str, lane: str,
                     model: str) -> None:
        directory = os.path.dirname(self.event_log)
        os.makedirs(directory, exist_ok=True)
        day = time.strftime("%Y-%m-%d", time.gmtime(self.now))
        lock_path = os.path.join(directory, "events.lock")
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            if os.path.exists(self.event_log):
                mtime = os.path.getmtime(self.event_log)
                old_day = time.strftime("%Y-%m-%d", time.gmtime(mtime))
                if old_day != day:
                    os.replace(self.event_log, os.path.join(directory, "events.%s.jsonl" % old_day))
            # Retain today's file and six previous daily archives.
            archives = sorted((p for p in Path(directory).glob("events.*.jsonl")
                               if p.name[7:17].count("-") == 2), reverse=True)
            for stale in archives[6:]:
                stale.unlink()
            digest = lambda value: hashlib.sha256(str(value).encode("utf-8")).hexdigest()
            record = {"time": invariants.utc(self.now), "at": self.now, "kind": kind,
                      "reason": digest(reason), "item": digest(item),
                      "lane": digest(lane), "model": digest(model)}
            with open(self.event_log, "a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, sort_keys=True) + "\n")
        finally:
            os.close(lock_fd)

    def _read_events(self) -> List[invariants.Event]:
        directory = os.path.dirname(self.event_log)
        rows = []
        os.makedirs(directory, exist_ok=True)
        lock_fd = os.open(os.path.join(directory, "events.lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_SH)
            paths = list(Path(directory).glob("events.*.jsonl")) + [Path(self.event_log)]
            for path in paths:
                try:
                    with path.open(encoding="utf-8") as stream:
                        for line in stream:
                            try:
                                row = json.loads(line)
                                if not isinstance(row, dict):
                                    continue
                                raw_at = row.get("at")
                                if isinstance(raw_at, bool) or not isinstance(raw_at, (int, float)):
                                    continue
                                at = float(raw_at)
                                kind, reason = row.get("kind"), row.get("reason")
                                if (not math.isfinite(at) or kind not in
                                        invariants.FAILURE_KINDS + ("rewrite_succeeded",)
                                        or not isinstance(reason, str)
                                        or not re.fullmatch(r"[0-9a-f]{64}", reason)):
                                    continue
                                if self.now - invariants.EVENT_WINDOW <= at <= self.now:
                                    rows.append(invariants.Event(
                                        at, kind, reason,
                                        item=row.get("item", ""),
                                        lane=row.get("lane", ""),
                                        model=row.get("model", ""),
                                    ))
                            except (ValueError, TypeError):
                                continue
                except FileNotFoundError:
                    continue
        finally:
            os.close(lock_fd)
        return rows

    def _refuse(self, repo: str, issue: Dict[str, Any], reason: str) -> None:
        """Log a refusal once per issue and body, and remember it. A refusal
        for an untrusted labeller is also counted toward its invariant."""
        self._note_labeller(reason)
        path = os.path.join(self.state, "untrusted.json")
        seen = _read_json(path, {})
        if not isinstance(seen, dict):
            seen = {}
        key = "%s#%s" % (repo, issue["number"])
        sha = _sha12(issue.get("body"))
        old = seen.get(key) or {}
        if old.get("sha") != sha:
            _say("not claiming %s: %s" % (key, reason))
            self._event("not_claiming", reason, key)
        seen[key] = {"sha": sha, "updated_at": issue.get("updated_at")}
        _write_json(path, seen)

    def _note_labeller(self, reason: str) -> None:
        """Count a refusal for an untrusted labeller against its login, with
        the time, in LABELER_FILE (loom#660)."""
        login = tickets.refused_labeller(reason)
        if login is None:
            return
        path = os.path.join(self.state, LABELER_FILE)
        seen = _read_json(path, {})
        seen = seen if isinstance(seen, dict) else {}
        seen[login] = self._recent(seen.get(login)) + [self.now]
        _write_json(path, seen)

    def _recent(self, times: Any) -> List[float]:
        """The refusal times in the last invariants.LABELER_WINDOW seconds."""
        return [t for t in times if self._num(t) and 0 <= self.now - t <= invariants.LABELER_WINDOW] \
            if isinstance(times, list) else []

    def _labeler_refusals(self) -> Dict[str, int]:
        """Untrusted labeller login -> its refusals in the last hour."""
        seen = _read_json(os.path.join(self.state, LABELER_FILE), {})
        seen = seen if isinstance(seen, dict) else {}
        counts = {login: len(self._recent(times)) for login, times in seen.items()}
        return {login: n for login, n in counts.items() if n}

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
        LOOM_BRIEF_AUTHORS: another operator's Loom owns it. A trusted
        briefer's own loom:ticket is a hand-off, not theirs."""
        owner = tickets.owned_elsewhere(issue, self.s.authors, self.s.briefers)
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

    def _fix_round_for(self, repo: str, ticket: Dict[str, Any], pr: int) -> bool:
        """Whether the ticket's newest trusted loom:fix comment, after its last
        claim, sends a lane to fix pull request `pr`. An unreadable or unusable
        fix is no fix round."""
        try:
            comments = self._paginate("/repos/%s/issues/%d/comments" % (repo, int(ticket["number"])))
            found = self._fix_for(repo, comments)
        except RateLimited:
            raise
        except (GitHubError, ValueError):
            return False
        return found is not None and found[0].pr == pr

    def _fix_for(self, repo: str, comments: List[Any]) -> Optional[Tuple[tickets.Fix, Any]]:
        """Return the newest trusted fix after the last claim, regardless of
        later non-fix comments. Raises ValueError for an unusable fix."""
        allowed = {a.lower() for a in self.s.authors}
        last_claim, newest_fix = -1, None
        for i, c in enumerate(comments):
            if not isinstance(c, dict):
                continue
            body = str(c.get("body") or "")
            trusted = tickets._login(c.get("user")) in allowed
            # A stranger's claim marker must not hide a trusted fix.
            if trusted and CLAIM_RE.fullmatch(body.strip()):
                last_claim = i
            if trusted and body.startswith(tickets.FIX_MARKER):
                newest_fix = (i, c, body)
        if newest_fix is None or newest_fix[0] < last_claim:
            return None
        # GitHub keeps no editor for comments: one changed after it was
        # posted may carry someone else's words.
        if newest_fix[1].get("updated_at") != newest_fix[1].get("created_at"):
            raise ValueError("the loom:fix comment was edited after it was posted")
        return tickets.parse_fix(repo, newest_fix[2]), newest_fix[1].get("id")

    def _claim_time(self, comments: List[Any], fix_comment_id: Any) -> float:
        """When a claim is stamped: the pass time, or one second after the
        fix comment it claims when that was posted later. The factory posts a
        fix in the same pass, after self.now, and a claim sorted before its
        fix left the fix unclaimed, so the next pass sent the same round
        again (39 duplicate fix rounds, 2026-10-21)."""
        # The rounds of one fix-rounds comment share its id: the newest counts.
        times = [self._epoch(str(c.get("created_at") or ""))
                 for c in (comments if fix_comment_id is not None else ())
                 if isinstance(c, dict) and c.get("id") == fix_comment_id]
        posted = max((t for t in times if t is not None), default=None)
        if posted is not None and posted >= self.now:
            return posted + 1
        return self.now

    def _claim(self, repo: str, lane: str, issue: Dict[str, Any]) -> bool:
        """Steps 2 and 3 for an issue already re-read and trusted. False when
        its fix comment cannot be used (refused, as an untrusted ticket is),
        or when its newest claim's brief is still waiting or running in a
        lane inbox (left for the next pass)."""
        number = int(issue["number"])
        comments = self._claim_comments(repo, issue, number)
        if comments is None:
            return False
        try:
            fix_source = self._fix_for(repo, comments)
        except ValueError as err:
            self._refuse(repo, issue, str(err))
            return False
        fix, fix_comment_id = fix_source if fix_source else (None, None)
        if self._held_before_claim(repo, issue, fix, comments):
            return False

        earlier = sum(1 for c in comments
                      if isinstance(c, dict) and CLAIM_RE.fullmatch(str(c.get("body") or "").strip()))
        if earlier and self._newest_brief_busy(repo, number, earlier):
            return False
        attempt = earlier + 1
        name = tickets.ticket_name(repo, number, attempt)
        has_contract, checks = self._validation_checks(repo)
        text = tickets.render_brief(repo, issue, lane, attempt, fix, checks, has_contract,
                                    None if fix else self._timedout_branch(comments),
                                    notes=tickets.operator_notes(
                                        comments, list(self.s.authors) + list(self.s.briefers)
                                        + list(self.s.owners)),
                                    linked=self._linked_pulls(repo, number))
        claiming = self._write_brief(lane, name, text)
        # No assignment: the claim:<login> label below marks the claim for
        # another operator's Loom (tickets.owned_elsewhere reads both), and an
        # assignee is emailed for every comment on the ticket (2026-10-18).
        self.gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number),
                     {"labels": self._claimed_labels(repo, issue)})
        # The claim marker goes on the ticket's one status comment, edited in
        # place: a new comment emailed the operator on every claim (loom#866).
        status.add(self.gh, repo, number, "Claimed for lane %s (body sha256 %s)"
                   % (lane, _sha12(issue.get("body"))),
                   self._claim_time(comments, fix_comment_id), self.s.authors)
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

    def _claim_comments(self, repo: str, issue: Dict[str, Any],
                        number: int) -> Optional[List[Any]]:
        """Read claim comments after verification, stopping at the pass target."""
        if self._verify_before_build(repo, issue):
            return None
        comments = self._paginate("/repos/%s/issues/%d/comments" % (repo, number))
        return None if self._target_reached() else comments

    def _linked_pulls(self, repo: str, number: int) -> List[str]:
        """The pull requests in any repo that reference the ticket, from the
        first page of its timeline (tickets.linked_pulls). _verify_before_build
        sees only this repo's merges; a merge elsewhere reaches the lane here."""
        return tickets.linked_pulls(self.gh.rest(
            "GET", "/repos/%s/issues/%d/timeline?per_page=100" % (repo, number)).data)

    def _timedout_branch(self, comments: List[Any]) -> Optional[str]:
        """The side branch a timed-out attempt kept its edits on, as the
        factory's own status events name it; None when none did."""
        allowed = {a.lower() for a in self.s.authors}
        # The imported status module expands the factory's event comments.
        expanded = status.expand(comments, self.s.authors)
        if not isinstance(expanded, list):
            _say("could not find timed-out branch: expanded issue comments are not a list")
            return None
        return tickets.timedout_branch(
            [str(c.get("body") or "") for c in expanded
             if isinstance(c, dict) and tickets._login(c.get("user")).lower() in allowed])

    def _fix_refusal(self, fix: Optional[tickets.Fix]) -> Optional[str]:
        """Why a fix brief cannot go out yet, or None when it can (no fix, or
        every finding parses and has a disposition)."""
        if fix is None:
            return None
        # Only parsing sits in the try: parse_findings raises ValueError
        # solely through Finding() (its regexes admit only known
        # dispositions and numeric lines, so in practice a malformed ID),
        # and err names the cause. The caller refuses outside any try, so a
        # ValueError from refusing cannot be reported as a bad finding.
        try:
            findings = fix.findings_list
        except ValueError as err:
            return "invalid pre-roll finding: %s" % err
        undispositioned = [f.id for f in findings if not f.disposition]
        if undispositioned:
            return "pre-roll findings without dispositions: %s" % ",".join(undispositioned)
        return None

    def _newest_brief_busy(self, repo: str, number: int, earlier: int) -> bool:
        """Whether the brief of the newest of `earlier` claims still waits or
        runs in a live lane's inbox. Two lanes on one ticket push to one
        branch, and the later push is refused: the newest claim's run must
        end first. A dead lane's brief is returned, so it does not count."""
        newest = tickets.ticket_name(repo, number, earlier)
        busy = [l for l in LANES
                if os.path.exists(os.path.join(self.inbox(l), newest + ".md"))
                and not self._return_dead_brief(l, newest)]
        if busy:
            _say("not claiming %s#%d: its brief %s is still in lane %s's inbox; next pass"
                 % (repo, number, newest, busy[0]))
        return bool(busy)

    def _claimed_labels(self, repo: str, issue: Dict[str, Any]) -> List[str]:
        """The issue's labels once claimed: briefed and wait labels dropped,
        the running label and this operator's claim label added."""
        labels = [n for n in _label_names(issue) if n != BRIEFED and not n.startswith(WAIT)]
        if RUNNING not in labels:
            labels.append(RUNNING)
        mark = self._claim_label(repo)
        if mark and mark not in [n.lower() for n in labels]:
            labels.append(mark)
        return labels

    def _drop_claim(self, names: Iterable[str]) -> List[str]:
        """`names` without this Loom's own claim:<login> label. The claim
        marks a run in progress: a ticket that leaves loom:running, or whose
        run's pull request merges, holds no claim (loom#1393). A claim held
        by another login is left."""
        return stale_claims.without_own_claim(names, self.s.authors)

    def _return_dead_brief(self, lane: str, name: str) -> bool:
        """Move a brief out of a dead lane's inbox into the inbox's returned/
        (which nothing reads), so the ticket can be claimed on a live lane.
        True when it was moved. A brief stays in the inbox while its run goes
        on, so "dead" needs every sign that nothing runs there: no runner lock
        with a live pid (lanes/run_lane.sh holds run/<lane>.lock for the whole
        run), a heartbeat whose own time is DEAD_BRIEF_AGE old or missing (the
        runner beats every tick, available or not: a limit or the spend floor
        is not death), and a brief that has waited DEAD_BRIEF_AGE. A ticket
        waited two days behind a brief in the inbox of a lane whose key had
        expired (2026-10-18)."""
        if self._runner_alive(lane):
            return False
        beat = _read_json(os.path.join(self.s.home, "state", "usage", lane + ".json"),
                          None, strict=False)
        at = beat.get("at") if isinstance(beat, dict) else None
        if isinstance(at, (int, float)) and not isinstance(at, bool) \
                and self.now - at < DEAD_BRIEF_AGE:
            return False
        path = os.path.join(self.inbox(lane), name + ".md")
        try:
            if self.now - os.path.getmtime(path) < DEAD_BRIEF_AGE:
                return False
            returned = os.path.join(self.inbox(lane), "returned")
            os.makedirs(returned, exist_ok=True)
            os.replace(path, os.path.join(returned, name + ".md"))
        except OSError:
            return False
        _say("returned brief %s from lane %s: no runner and no heartbeat for an hour"
             % (name, lane))
        return True

    def _runner_alive(self, lane: str) -> bool:
        """Whether lane's runner holds its lock with a live pid. A name that
        is not one of LANES counts as alive, so it never builds a path and its
        brief is never moved."""
        if lane not in LANES:
            return True
        lock = os.path.join(self.s.home, "run", lane + ".lock")
        try:
            with open(os.path.join(lock, "pid")) as fh:
                pid = int(fh.read().strip())
        except (OSError, ValueError):
            # A lock with no readable pid may be a runner starting up.
            return os.path.isdir(lock)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            pass  # alive, owned by another user (the lane user)
        return True

    def _keepalive_age(self, lane: str) -> Optional[float]:
        """Seconds since lane's runner last stamped state/usage/<lane>.alive
        (a unix time, written every minute while the runner lives); None when
        there is no readable stamp. A runner writes its heartbeat only between
        runs, so a lane deep in a run is told alive by this stamp (loom#1281)."""
        if lane not in LANES:
            return None
        path = os.path.join(self.s.home, "state", "usage", lane + ".alive")
        try:
            with open(path) as fh:
                at = float(fh.read().strip())
        except (OSError, ValueError):
            return None
        if at - max(self.now, time.time()) > 60:
            return None  # from the future: not a stamp this runner wrote
        return max(0.0, self.now - at)

    def _keepalive_fresh(self, lane: str) -> bool:
        age = self._keepalive_age(lane)
        return age is not None and age <= ALIVE_MAX_AGE

    def _runner_state(self, lane: str) -> str:
        """What the station sees of lane's runner, for the log line that
        names why a lane was read as silent (loom#1281)."""
        if lane not in LANES:
            return "not a lane"
        lock = os.path.join(self.s.home, "run", lane + ".lock")
        if not os.path.isdir(lock):
            lock_state = "no lock"
        else:
            try:
                with open(os.path.join(lock, "pid")) as fh:
                    pid = int(fh.read().strip())
            except (OSError, ValueError):
                lock_state = "lock without a readable pid"
            else:
                lock_state = ("lock pid %d dead" % pid if not self._runner_alive(lane)
                              else "lock pid %d alive" % pid)
        age = self._keepalive_age(lane)
        return "%s, %s" % (lock_state, "no keepalive stamp" if age is None
                           else "keepalive stamp %d seconds old" % age)

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
            except RateLimited:
                raise
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
            if workflow is not None and _has_pull_request_trigger(workflow):
                found.extend(_workflow_run_steps(path, workflow))
        return False, found

    def file_validation_contracts(self) -> None:
        """Ask each goal home for a validation contract while its repo lacks one."""
        title = "Add the repository validation contract"
        body = ("## Why\nLanes need the repository's checks before opening pull requests.\n\n"
                "## What\nAdd `.loom/validation.conf` with the lint, format, type and test "
                "commands that the pull-request workflows run, in that order. For every "
                "command, record the workflow file and step it comes from. The contract "
                "must pass on the default branch in this ticket's pull request. List only "
                "commands that run on a lane machine with no secrets, simulators or unset "
                "variables, in the forms docs/LANES.md documents (key: value lines, "
                "`<kind> | <command>` lines, or `[kind]` and `[[kind]]` tables with a "
                "`command` key). A lane change to the contract is refused before push when "
                "the runner cannot read it or its check fails on the lane's own tree.\n\n"
                "## Acceptance criteria\n- [ ] The contract lists the repository's lint, "
                "format, type and test commands in order, with workflow file and step.\n"
                "- [ ] Every listed command matches a run step in a pull-request workflow.\n"
                "- [ ] The ticket pull request passes the contract on the default branch.\n\n"
                "## Size\nS\n\n## Out of scope\nWriting the contract by the station.\n")
        for repo, goal in self._goal_homes():
            if not naming.is_backlog_home(goal):
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
            brain.brief_intake(self.gh, repo, int(goal["number"]), title, body,
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
                except RateLimited:
                    raise
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
        return beat_fresh(at, self.now)

    def _remote_heartbeat(self, lane: str) -> Optional[float]:
        """Latest heartbeat posted by a remote worker on the lane status issue."""
        saved = _read_json(os.path.join(self.state, "remote-status.json"), {}, strict=False)
        cached = saved.get(lane) if isinstance(saved, dict) else None
        if (isinstance(cached, dict) and isinstance(cached.get("checked_at"), (int, float))
                and 0 <= self.now - cached["checked_at"] <= HEARTBEAT_MAX_AGE):
            value = cached.get("heartbeat")
            return value if isinstance(value, (int, float)) else None
        heartbeat = self._read_remote_heartbeat(lane)
        counts, repos = (0, 0)
        try:
            if heartbeat is not None:
                counts, repos = self._read_remote_claim_count(lane)
        finally:
            # Always written once the heartbeat was read: a failed count
            # read must not leave every later caller in the pass reading
            # again and raising again.
            if not isinstance(saved, dict):
                saved = {}
            saved[lane] = {"checked_at": self.now, "heartbeat": heartbeat,
                           "claims": counts, "repos": repos}
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
        # A trusted heartbeat only: a stranger's copy must not mark a lane alive.
        for comment in brain.trusted_comments(self._paginate(
                "/repos/%s/issues/%d/comments" % (repo, int(issues[0]["number"]))),
                self.s.authors):
            match = pattern.fullmatch(str(comment.get("body") or "").strip())
            if match and match.group(1) == lane:
                try:
                    at = float(match.group(2))
                except ValueError:
                    continue
                latest = at if latest is None else max(latest, at)
        return latest

    def limited_lanes(self) -> List[str]:
        """Lanes whose fresh heartbeat says unavailable at a limit."""
        return list(self._limit_reasons())

    def limit_until(self) -> Dict[str, float]:
        """Lane -> epoch its limit lifts (its heartbeat's retry_at), for each
        lane limited_lanes names whose heartbeat gives one."""
        out: Dict[str, float] = {}
        for lane in self._limit_reasons():
            beat = _read_json(os.path.join(self.s.home, "state", "usage", lane + ".json"),
                              None, strict=False)
            until = beat.get("retry_at") if isinstance(beat, dict) else None
            if isinstance(until, (int, float)) and not isinstance(until, bool):
                out[lane] = float(until)
        return out

    def down_lanes(self) -> List[str]:
        """Lanes whose fresh heartbeat says unavailable for a reason other
        than a limit (an expired key, a lane stopped on purpose). A stale or
        missing heartbeat is not counted: a lane deep in a long run can go
        quiet without being down."""
        out: List[str] = []
        for lane in LANES:
            beat = _read_json(os.path.join(self.s.home, "state", "usage", lane + ".json"),
                              None, strict=False)
            if not isinstance(beat, dict) or beat.get("available") is not False:
                continue
            at = beat.get("at")
            if isinstance(at, bool) or not isinstance(at, (int, float)):
                continue
            if beat_fresh(at, self.now) and not str(beat.get("reason") or "").startswith(LIMIT):
                out.append(lane)
        return out

    def _limit_reasons(self) -> Dict[str, str]:
        """Lane -> the reason of its fresh heartbeat that says unavailable
        at a limit. A hold whose reset has passed is no hold: the lane is
        free again, as the Usage view reads the same heartbeat."""
        out: Dict[str, str] = {}
        for lane in LANES:
            beat = _read_json(os.path.join(self.s.home, "state", "usage", lane + ".json"),
                              None, strict=False)
            if not isinstance(beat, dict) or beat.get("available") is not False:
                continue
            at = beat.get("at")
            if isinstance(at, bool) or not isinstance(at, (int, float)):
                continue
            reason = str(beat.get("reason") or "")
            if beat_fresh(at, self.now) and reason.startswith(LIMIT) \
                    and not hold_expired(beat, self.now):
                out[lane] = reason
        return out

    def _no_topup_lanes(self) -> Tuple[str, ...]:
        """The lanes held out of credit on a LOOM_NO_TOPUP provider: they
        wait for the credit to renew, as the operator chose."""
        if not self.s.no_topup:
            return ()
        return tuple(lane for lane, reason in self._limit_reasons().items()
                     if provider(lane) in self.s.no_topup and usage.OUT_OF_CREDIT in reason)

    def model_limits(self) -> Dict[str, Dict[str, float]]:
        """Lane -> {model: epoch its limit lifts}, from each fresh heartbeat's
        limited_models (lanes/run_lane.sh write_beat). A lane with a model
        ladder stays available while one model is limited, and a brief
        pinned to that model is parked at once, so these are the claims that
        cannot run there before the reset."""
        out: Dict[str, Dict[str, float]] = {}
        for lane in LANES:
            if lane in REMOTE_LANES:
                continue
            beat = _read_json(os.path.join(self.s.home, "state", "usage", lane + ".json"),
                              None, strict=False)
            if not isinstance(beat, dict) or not isinstance(beat.get("limited_models"), dict):
                continue
            at = beat.get("at")
            if isinstance(at, bool) or not isinstance(at, (int, float)) or not beat_fresh(at, self.now):
                continue
            held = {str(model): float(until) for model, until in beat["limited_models"].items()
                    if isinstance(until, (int, float)) and not isinstance(until, bool)
                    and until > self.now}
            if held:
                out[lane] = held
        return out

    def model_limited(self, lane: str, model: str) -> Optional[float]:
        """When the lane's limit on this model lifts, or None when it has none."""
        return self.model_limits().get(lane, {}).get(model) if model else None

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
        self._repair_flying = {}
        # A runaway held because one dead lane kept failing it goes back to a
        # free lane of its tier on its own (loom#565), before claims.
        self._release_dead_lane_runaways(free)
        # A starved shipped ticket takes one free lane ahead of build work.
        free = self._reserve_verify(free)
        free = self._not_demoted(free)
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
                found = self._paginate("/repos/%s/issues?labels=%s&state=open"
                                         % (repo, RUNAWAY))
            except RateLimited:
                raise
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
            except RateLimited:
                raise
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
        comments = [c for c in self._paginate("/repos/%s/issues/%d/comments" % (repo, number))
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
                          "stays." % (marker, lane, inv_number), notify.RUNAWAY)
            _say("%s#%d stays held: its dead lane %s has no free tier lane"
                 % (repo, number, lane))
            return
        names = self._drop_claim(n for n in names
                                 if n != RUNAWAY and n != RUNNING and not n.startswith("lane:"))
        if BRIEFED not in names:
            names.append(BRIEFED)
        names.append("lane:" + target)
        self.gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number), {"labels": names})
        issue["labels"] = [{"name": n} for n in names]
        self._comment(repo, number,
                      "%s\nRunaway hold released: every attempt since the last brief ran on "
                      "lane %s, which has an open runs-and-none-done invariant (#%d: %s). "
                      "This is not a failure: the ticket is briefed to lane %s, in its "
                      "difficulty tier." % (marker, lane, inv_number, inv_title, target),
                      notify.RUNAWAY)
        used.add(target)
        _say("released runaway %s#%d from dead lane %s to lane %s" % (repo, number, lane, target))

    def _parent(self, repo: str, number: int) -> Dict[str, Any]:
        """An issue a ticket names as its parent, read once a pass; empty
        when it cannot be read."""
        key = (repo, number)
        if key not in self._parents:
            try:
                data = self.gh.rest("GET", "/repos/%s/issues/%d" % key).data
            except RateLimited:
                raise
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
        never gated: the limit holds back new work, not repairs. Factory-repair
        tickets pass under their own in-flight cap (_repair_gate)."""
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
        if brain.is_repair(repo, issue):
            return self._repair_gate(repo, issue, goal, held, limit)
        return ("goal #%d has %d tickets shipped without a proof "
                "(limit %d); the goal verifies them before the station starts more"
                % (goal, held, limit))

    def _repair_gate(self, repo: str, issue: Dict[str, Any], goal: int, held: int,
                     limit: int) -> Optional[str]:
        """A factory-repair ticket over the unproven-work limit (loom#767): it
        goes while fewer than brain.REPAIR_IN_FLIGHT other repair tickets of
        the goal are running or at a pull request. One read of the goal's
        tickets a pass; each ticket let through counts against the rest."""
        key = (repo, goal)
        if key not in self._repair_flying:
            try:
                self._repair_flying[key] = brain.goal_repairs_flying(self.gh, repo, goal)
            except ValueError as err:
                return "its goal #%d cannot be read: %s" % (goal, err)
        number = int(issue["number"])
        others = [n for n in self._repair_flying[key] if n != number]
        if len(others) < brain.REPAIR_IN_FLIGHT:
            if number not in self._repair_flying[key]:
                self._repair_flying[key].append(number)
            return None
        return ("goal #%d has %d tickets shipped without a proof (limit %d), and %d "
                "factory-repair tickets are in flight already (at most %d pass the limit)"
                % (goal, held, limit, len(others), brain.REPAIR_IN_FLIGHT))

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
                    self._set_wait(repo, issue, "gate")
                    _say("not claiming %s#%s for lane %s: %s"
                         % (repo, issue.get("number"), lane, reason))
                    self._event("not_claiming", reason,
                                "%s#%s" % (repo, issue.get("number")), lane)
        return gated

    def _gate_held(self) -> List[str]:
        """The tickets the unproven-work gate refused this pass, "owner/repo#n":
        the factory neither counts them as lane load nor moves them."""
        return ["%s#%d" % (hold["repo"], int(number))
                for hold in self._gate_holds.values() for number in hold["tickets"]]

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
            except RateLimited:
                raise
            except TargetReached as err:
                # Claims made earlier this pass stay made; the rest wait.
                _say("%s#%s waits for the next pass: %s" % (repo, issue["number"], err))
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
            except RateLimited:
                raise
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
        # The model the ticket runs, as tickets.parse reads it: a retired pin
        # runs its stand-in, which its labelled lane may well take.
        if not model or lane_takes_model(lane, tickets.allowed_model(model.group(1))):
            return
        if free:
            by_lane[free[0]].append((repo, issue))
            return
        try:
            self._try_ticket(repo, lane, issue, free)
        except RateLimited:
            raise
        except TargetReached as err:
            _say("%s#%s waits for the next pass: %s" % (repo, issue["number"], err))
        except (GitHubError, OSError) as err:
            _say("could not queue %s#%s: %s" % (repo, issue["number"], err))

    # -- verify runs ------------------------------------------------------
    def _tickets_open(self, repo: str) -> List[Dict[str, Any]]:
        """The repo's open tickets filed by a briefing login."""
        found = self._paginate("/repos/%s/issues?labels=loom:ticket&state=open" % repo)
        return [i for i in found if isinstance(i, dict) and "pull_request" not in i
                and tickets.brief_author_trusted(i, self.s.authors, self.s.briefers)
                and not self._owned_elsewhere(repo, i)]

    def _queued_lanes(self, repo: str, issues: List[Dict[str, Any]],
                      counts: Dict[Tuple[str, int], int]) -> set:
        """The lanes a queued ticket of the repo may be given: its model's
        lanes, else its difficulty tier's. A ticket the unproven-work gate
        refuses wants no lane: holding its lanes from verify runs too left
        the goal's shipped work stuck over the limit (2026-10-18)."""
        limit = brain.unverified_limit()
        lanes: set = set()
        for issue in issues:
            if "loom:queued" not in _label_names(issue):
                continue
            if self._unproven_gate(repo, issue, limit, counts) is not None:
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
        comments = self._paginate("/repos/%s/issues/%d/comments" % (repo, number))
        return audit.proven(comments, self.s.authors)

    def _shipped_stamp(self, repo: str, issue: Dict[str, Any]) -> float:
        """When loom:shipped was last applied; the issue's update time if the
        events do not say."""
        events = self._paginate("/repos/%s/issues/%d/events" % (repo, int(issue["number"])))
        stamps = [metrics.stamp(e.get("created_at")) for e in events
                  if isinstance(e, dict) and e.get("event") == "labeled"
                  and (e.get("label") or {}).get("name") == "loom:shipped"]
        return max(stamps) if stamps else metrics.stamp(issue.get("updated_at"))

    def _verify_repos(self) -> List[str]:
        return [repo for repo in self.s.repos if tickets.REPO_RE.fullmatch(repo)]

    def _verify_pool(self, attempts: Dict[str, Any], queued_holds: bool = True
                     ) -> Tuple[List[Tuple[str, Dict[str, Any]]], set]:
        """(the tickets due a verify run, oldest ship first; the lanes queued
        build work could take, or a failed audit holds). The reserved lane
        (_reserve_verify) passes queued_holds=False: queued build work is the
        very thing it must not wait behind."""
        held = set(self._audit_holds())
        shipped: List[Tuple[str, Dict[str, Any]]] = []
        counts: Dict[Tuple[str, int], int] = {}
        for repo in self._verify_repos():
            issues = self._tickets_open(repo)
            if queued_holds:
                held |= self._queued_lanes(repo, issues, counts)
            shipped += [(repo, i) for i in issues]
        unsearchable = sum(1 for _, issue in shipped
                           if "loom:shipped" in _label_names(issue)
                           and live_needles(issue.get("body")) == [])
        if unsearchable:
            _say("skipped %d shipped tickets whose Live criterion names no exact string" %
                 unsearchable)
        refused = self._verify_refused_ledger()
        # The cheap filters first: the events and comments of a ticket are
        # read only when its cooldown is over.
        found = []
        reads: Dict[str, Any] = {"searches": UNMEETABLE_SEARCHES, "big": {}}
        for repo, issue in verify_due(shipped, attempts, self.now, refused):
            if self._unmeetable_live(repo, issue, reads):
                continue
            issue["shipped_at"] = self._shipped_stamp(repo, issue)
            issue["has_proof"] = self._proven(repo, int(issue["number"]))
            issue["health_proof"] = health.proof_of(self._paginate(
                "/repos/%s/issues/%d/comments" % (repo, int(issue["number"]))), self.s.authors)
            found.append((repo, issue))
        return verify_due(found, attempts, self.now, refused), held

    def _unmeetable_live(self, repo: str, issue: Dict[str, Any], reads: Dict[str, Any]) -> bool:
        """True when the ticket's Live box can never be met as written
        (bus/unmeetable.py): it waits on a closed issue or an unmerged closed
        pull request, or on a log string no file of the repo contains. The
        first time a box text is flagged one restate ask is posted; until the
        text changes the ticket is not handed to a verify lane again (loom#2028:
        two such tickets went through lane after lane and came back unmet).
        `reads` holds this pass's source lookups (_source_has)."""
        live = brain.live_criterion(issue.get("body"), limit=None)
        if live == "(no Live criterion on the ticket)":
            return False
        number = int(issue["number"])
        ticket = "%s#%d" % (repo, number)
        box = unmeetable.key(live)
        path = os.path.join(self.state, UNMEETABLE_FILE)
        state = _read_json(path, {}, strict=False)
        state = state if isinstance(state, dict) else {}
        known = self._unmeetable_known(state.get(ticket), box)
        if known is not None:
            return known
        unknown: List[bool] = []
        found = unmeetable.reasons(live, repo, number, self._issue_state,
                                   lambda term: self._source_has(repo, term, reads, unknown))
        if not found:
            if not unknown:
                state[ticket] = {"key": box, "reasons": [], "at": self.now}
                _write_json(path, state)
            return False
        if self._ask_restate(repo, number, live, found):
            state[ticket] = {"key": box, "reasons": found, "at": self.now}
            _write_json(path, state)
            _say("not verifying %s: its Live box cannot be met as written (%s)"
                 % (ticket, "; ".join(found)))
        return True

    def _unmeetable_known(self, seen: Any, box: str) -> Optional[bool]:
        """The recorded answer for this box text: True when it was flagged,
        False when it was clean within UNMEETABLE_RECHECK, else None."""
        if not isinstance(seen, dict) or seen.get("key") != box:
            return None
        if seen.get("reasons"):
            return True
        at = seen.get("at")
        if isinstance(at, (int, float)) and 0 <= self.now - at < UNMEETABLE_RECHECK:
            return False
        return None

    def _issue_state(self, repo: str, number: int) -> Optional[Tuple[str, bool]]:
        """(state, merged) of an issue or pull request, None when unreadable."""
        try:
            data = self.gh.rest("GET", "/repos/%s/issues/%d" % (repo, number)).data
        except RateLimited:
            raise
        except GitHubError:
            return None
        if not isinstance(data, dict):
            return None
        pull = data.get("pull_request")
        return str(data.get("state") or ""), bool(isinstance(pull, dict) and pull.get("merged_at"))

    def _source_has(self, repo: str, term: unmeetable.Term, reads: Dict[str, Any],
                    unknown: List[bool]) -> Optional[bool]:
        """Whether the repo's source holds `term`; None when unknown
        (`unknown` is marked). A code search that finds the term's fixed part
        is enough to say yes. One that finds nothing is not proof (loom#2050):
        the index skips files over 384 KB, so those are read directly and
        searched line by line with the term's wildcards; only when that read
        covers them all is the answer no."""
        found = self._code_search_hits(repo, term.literal, reads)
        if found is None:
            unknown.append(True)
            return None
        if found:
            return True
        big = self._unindexed_sources(repo, reads)
        if big is None:
            unknown.append(True)
            return None
        return unmeetable.holds(term, big)

    def _code_search_hits(self, repo: str, text: str, reads: Dict[str, Any]) -> Optional[int]:
        """How many files a code search of the repo finds `text` in; None
        when the search was not run, failed or was incomplete."""
        if reads["searches"] <= 0:
            return None
        reads["searches"] -= 1
        try:
            data = self.gh.rest("GET", "/search/code?q=%s+repo:%s" % (
                quote('"%s"' % text, safe=""), repo)).data
        except GitHubError:
            # The search budget is apart from the core one: a refused search
            # proves nothing and ends this pass's searches.
            reads["searches"] = 0
            return None
        if not isinstance(data, dict) or data.get("incomplete_results") \
                or not isinstance(data.get("total_count"), int):
            return None
        return data["total_count"]

    def _unindexed_sources(self, repo: str, reads: Dict[str, Any]) -> Optional[List[str]]:
        """The text of every file on the repo's default branch the code
        search index skips (UNINDEXED_BYTES and over), read once a pass;
        None when the tree or a file cannot be read, the tree listing is
        truncated, or there are more such files than UNMEETABLE_BIG_READS."""
        cache = reads["big"]
        if repo in cache:
            return cache[repo]
        cache[repo] = None
        try:
            data = self.gh.rest("GET", "/repos/%s/git/trees/HEAD?recursive=1" % repo).data
        except RateLimited:
            raise
        except GitHubError:
            return None
        if not isinstance(data, dict) or data.get("truncated") \
                or not isinstance(data.get("tree"), list):
            return None
        big = _unindexed_entries(data["tree"])
        if len(big) > UNMEETABLE_BIG_READS:
            return None
        texts = []
        for entry in big:
            text = self._blob_text(repo, str(entry.get("sha") or ""))
            if text is None:
                return None
            texts.append(text)
        cache[repo] = texts
        return texts

    def _blob_text(self, repo: str, sha: str) -> Optional[str]:
        """A file's text read by its blob SHA; None when it cannot be read."""
        try:
            blob = self.gh.rest("GET", "/repos/%s/git/blobs/%s" % (repo, sha)).data
        except RateLimited:
            raise
        except GitHubError:
            return None
        if not isinstance(blob, dict) or blob.get("encoding") != "base64" \
                or not isinstance(blob.get("content"), str):
            return None
        try:
            return base64.b64decode(blob["content"]).decode("utf-8", errors="replace")
        except ValueError:
            return None

    def _ask_restate(self, repo: str, number: int, live: str, found: List[str]) -> bool:
        """Post the one restate ask for this box text unless a comment
        already carries it; False when GitHub refused (tried next pass)."""
        try:
            comments = self._paginate("/repos/%s/issues/%d/comments" % (repo, number))
            if not unmeetable.asked(list(comments), live):
                notify.post(self.gh, repo, number, unmeetable.ask(
                    live, [self._note_text(r) for r in found]), notify.HELD)
        except RateLimited:
            raise
        except GitHubError as err:
            _say("could not ask to restate the Live box of %s#%d: %s" % (repo, number, err))
            return False
        return True

    def _audit_holds(self) -> Dict[str, float]:
        """The lanes a failed audit of their proof holds from verify runs,
        with when each hold ends (bus/audit.py)."""
        holds = audit.held(self.gh, self._verify_repos(), self.s.authors, self.now)
        for lane, until in sorted(holds.items()):
            _say("lane %s takes no verify run until %s: a proof it made failed its audit"
                 % (lane, time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(until))))
        return holds

    def _reserve_verify(self, free: List[str]) -> List[str]:
        """Take one free lane for the oldest shipped ticket that has waited
        VERIFY_RESERVE_AFTER since its ship, ahead of build work (loom#964).
        Idle lanes still verify through _hand_verifies; this gives verify an
        owner when no lane is ever idle. At most one lane per
        VERIFY_RESERVE_EVERY, so build work is delayed, never starved. Returns
        the free lanes that still have an empty inbox."""
        if not free:
            return free
        path = os.path.join(self.state, VERIFY_RESERVE_FILE)
        last = _read_json(path, {}, strict=False)
        checked = last.get("at") if isinstance(last, dict) else None
        if isinstance(checked, (int, float)) and 0 <= self.now - float(checked) < VERIFY_RESERVE_EVERY:
            return free
        _write_json(path, {"at": self.now})
        self._hand_verifies(free, starved_for=VERIFY_RESERVE_AFTER, limit=1)
        return [lane for lane in free if self._inbox_empty(lane)]

    def _hand_verifies(self, idle: List[str], starved_for: float = 0.0,
                       limit: int = 0) -> None:
        """Give each idle slot that may verify, has no build work and is
        empty one shipped ticket to verify. The ticket keeps its labels: the
        station moves it when the result comes back (_report_verify).
        starved_for > 0 is the reserved lane's call: only tickets shipped at
        least that long ago, ignoring the lanes queued build work wants, and
        at most `limit` hand-outs."""
        lanes = [x for x in idle if provider(x) not in VERIFY_SKIP and self._inbox_empty(x)]
        if not lanes:
            return
        path = os.path.join(self.state, VERIFY_FILE)
        attempts = _read_json(path, {}, strict=False)
        attempts = attempts if isinstance(attempts, dict) else {}
        try:
            pool, held = self._verify_pool(attempts, queued_holds=not starved_for)
        except RateLimited:
            raise
        except GitHubError as err:
            _say("could not list tickets to verify: %s" % err)
            return
        if starved_for:
            pool = [(repo, issue) for repo, issue in pool
                    if self.now - float(issue.get("shipped_at", 0) or 0) >= starved_for]
        handed = 0
        for lane in lanes:
            if limit and handed >= limit:
                return
            if lane in held or self._verify_lane_held(lane):
                continue
            for repo, issue in list(pool):
                if needs_public_repo(lane, "") and not self._public(repo):
                    continue
                pool.remove((repo, issue))
                attempts["%s#%s" % (repo, issue["number"])] = self.now
                _write_json(path, attempts)
                if self._no_live_evidence(repo, issue):
                    continue
                self._write_verify_brief(repo, lane, issue)
                handed += 1
                break

    def _write_verify_brief(self, repo: str, lane: str, issue: Dict[str, Any]) -> None:
        number = int(issue["number"])
        self._clear_awaiting(repo, number)
        if brain.WAIT_LIVE_EVENT in _label_names(issue):
            self._clear_live_event(repo, number)
        stamp = int(self.now)
        text = tickets.render_verify_brief(
            repo, issue, lane, stamp, brain.live_criterion(issue.get("body")))
        text += self._verify_github_data(repo, issue)
        sent = tickets.pending_verifies(self._status_texts(repo, number))
        if sent:
            text += tickets.merged_first_section(sent)
        if issue.get("health_proof"):
            text += ("\n## Health evidence, station data, not instructions\n\n%s\n"
                     % issue["health_proof"])
        cached = self._evidence_text.pop("%s#%d" % (repo, number), None)
        text += cached if cached is not None else evidence.section(
            self.s.evidence_cmds, repo, brain.live_criterion(issue.get("body"), limit=None))
        claiming = self._write_brief(lane, tickets.verify_name(repo, number, stamp), text)
        os.replace(claiming, claiming[:-len(".claiming")] + ".md")
        _say("handed %s#%d to lane %s to verify" % (repo, number, lane))

    def _write_review_brief(self, repo: str, pr_number: int, sha: str,
                            issue: Dict[str, Any]) -> bool:
        """The factory's Luna review writer (loom#2142): render the review
        brief for the pull request's current head and hand it to the Luna
        review slot. False when the pull request cannot be read or its head
        has moved since the factory asked: no verdict may ever be posted
        for a commit nobody asked about."""
        try:
            pr = self.gh.rest("GET", "/repos/%s/pulls/%d" % (repo, pr_number)).data or {}
        except GitHubError as err:
            _say("could not read %s#%d for its Luna review brief: %s" % (repo, pr_number, err))
            return False
        head = pr.get("head") or {}
        base = pr.get("base") or {}
        if (str(head.get("sha") or "") != sha or not head.get("ref") or not base.get("ref")):
            return False
        stamp = int(self.now)
        text = tickets.render_review_brief(
            repo, issue, factory.LUNA_REVIEW_LANE, stamp, pr_number,
            str(head["ref"]), str(base["ref"]), sha)
        claiming = self._write_brief(
            factory.LUNA_REVIEW_LANE,
            tickets.review_name(repo, int(issue["number"]), stamp), text)
        os.replace(claiming, claiming[:-len(".claiming")] + ".md")
        _say("handed %s#%d head %s to lane %s to review"
             % (repo, pr_number, sha[:12], factory.LUNA_REVIEW_LANE))
        return True

    def _verify_github_data(self, repo: str, issue: Dict[str, Any]) -> str:
        """The verify brief's GitHub section: the lane has no network, so the
        station reads the ticket's merged pull requests, linked the way
        reconcile links them, with their checks, files and added tests. A
        GitHub failure is one line in the section, never a lost brief."""
        branch, pulls, error = self._merged_pull_facts(repo, issue)
        if error:
            _say("could not read GitHub data for the verify brief of %s#%s: %s"
                 % (repo, issue.get("number"), error))
        return verify_data.section(branch, str(issue.get("body") or ""), pulls, error)

    def _merged_pull_facts(self, repo: str, issue: Dict[str, Any], files: bool = True
                           ) -> Tuple[str, List[Dict[str, Any]], str]:
        """(default branch, merged pull requests, error) for a ticket: its
        pull requests linked the way reconcile links them, newest first, the
        newest verify_data.MAX_PULLS read in full (check runs on the merge
        commit, whether the branch carries it and, when files is set, the
        changed files). After a GitHub failure only fully read pull requests
        are returned, with the failure as "status N" or the error's name;
        the verify brief and the done close both use these reads."""
        branch = ""
        read: List[Dict[str, Any]] = []
        try:
            branch = self._default_branch(repo)
            linked = sorted((dict(pr) for pr in self._closed_pulls(repo)
                             if _covers_pr(issue, repo, pr)),
                            key=lambda pr: str(pr.get("merged_at") or ""), reverse=True)
            # Only the pull requests the section shows are read in full.
            for pr in linked[:verify_data.MAX_PULLS]:
                sha = str(pr["merge_commit_sha"])
                runs = self.gh.rest("GET", "/repos/%s/commits/%s/check-runs?per_page=100"
                                    % (repo, sha)).data
                pr["check_runs"] = (runs.get("check_runs") if isinstance(runs, dict) else []) or []
                pr["on_branch"] = self._carries(repo, sha, quote(branch, safe="/")) \
                    if branch else None
                if files:
                    pr["files"] = self.gh.rest("GET", "/repos/%s/pulls/%d/files?per_page=100"
                                               % (repo, int(pr["number"]))).data or []
                read.append(pr)
            return branch, read + linked[verify_data.MAX_PULLS:], ""
        except RateLimited:
            raise
        except (GitHubError, OSError, ValueError) as err:
            return branch, read, ("status %d" % err.status
                                  if isinstance(err, GitHubError) and err.status
                                  else type(err).__name__)

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

    def _no_live_evidence(self, repo: str, issue: Dict[str, Any]) -> bool:
        """True when the station's own log command finds no line naming any of
        the ticket's live-criterion strings: the event has not happened, so a
        verify run can only fail. The ticket is marked awaiting evidence (no
        comment) and checked again after its cooldown. mod-overseer#922 spent a
        run and posted a comment every 6 hours for days waiting on an event that had not happened."""
        live = brain.live_criterion(issue.get("body"), limit=None)
        text, seen, reported = evidence.gather_window(self.s.evidence_cmds, repo, live)
        number = int(issue["number"])
        names = _label_names(issue)
        absence = evidence.is_absence(live)
        if absence and seen is None and self.s.evidence_cmds.get(repo):
            # A command that errored or timed out proved nothing, either way.
            _say("not verifying %s#%d yet: the log command gave no answer for its absence "
                 "criterion" % (repo, number))
            self._await_absence(repo, number, names)
            return True
        if absence and seen is False:
            window = reported if reported is not None else self.s.evidence_window_hours
            span = evidence.required_span(live)
            if span is not None and window < span:
                _say("not verifying %s#%d yet: the log covers %s of the %s hours its absence "
                     "criterion needs; the missing window is %s hours"
                     % (repo, number, evidence.hours_text(window), evidence.hours_text(span),
                        evidence.hours_text(span - window)))
                self._await_absence(repo, number, names)
                return True
            # Nothing found over the window is the evidence: the brief says so.
            text += evidence.absence_section(evidence.needles(live), window, span)
            self._evidence_text["%s#%d" % (repo, number)] = text
            return False
        # The brief quotes the same lines, so the command runs once.
        self._evidence_text["%s#%d" % (repo, number)] = text
        if seen is not False:
            return False
        if brain.WAIT_LIVE_EVENT in names:
            _say("not verifying %s#%d: held for a live event no log line shows yet" % (repo, number))
            return True
        shipped = float(issue.get("shipped_at") or 0)
        if shipped and self.now - shipped >= VERIFY_WAIT_DAYS * 86400:
            self._hold_for_live_event(repo, number, live)
            return True
        if brain.AWAITING_EVIDENCE not in names:
            self._await_evidence(repo, number,
                                 "No log line names the live criterion's strings yet; the "
                                 "station checks again in %d hours." % (VERIFY_INTERVAL // 3600))
        _say("not verifying %s#%d yet: no log line names its live criterion" % (repo, number))
        return True

    def _await_absence(self, repo: str, number: int, names: List[str]) -> None:
        """Label an absence criterion the log cannot yet prove, once and with
        no comment. It is never held for a live event: an event cannot make
        an absence provable, a longer log window or a working command can."""
        if brain.AWAITING_EVIDENCE not in names:
            self._await_evidence(repo, number,
                                 "The log command gave no answer, or its log does not cover "
                                 "the span the absence criterion needs; the station checks "
                                 "again in %d hours." % (VERIFY_INTERVAL // 3600))

    def _hold_for_live_event(self, repo: str, number: int, live: str) -> None:
        """Move a shipped ticket whose Live box has seen no matching event for
        VERIFY_WAIT_DAYS days to wait:live-event, with one note naming the
        event it waits for. It stops counting as open work; the station checks
        the log for the event on every verify cooldown and verifies the ticket
        once a line shows (the hand-out drops the label, _clear_live_event)."""
        try:
            self._add_label(repo, number, brain.WAIT_LIVE_EVENT)
            self.gh.rest("DELETE", "/repos/%s/issues/%d/labels/%s"
                         % (repo, number, quote(brain.AWAITING_EVIDENCE, safe="")), ok=(404,))
            notify.post(self.gh, repo, number,
                "%s\nNo log line has named this ticket's Live box in %d days since it shipped, "
                "so it is held apart from open work (%s). It waits for this event: %s\n\n"
                "The station keeps checking the log every %d hours and runs the verify again "
                "as soon as a matching line appears; the ticket then closes on proof as usual."
                % (LIVE_EVENT_MARKER, VERIFY_WAIT_DAYS, brain.WAIT_LIVE_EVENT,
                   self._note_text(live), VERIFY_INTERVAL // 3600), notify.HELD)
        except RateLimited:
            raise
        except GitHubError as err:
            _say("could not hold %s#%d for a live event: %s" % (repo, number, err))
            return
        _say("held %s#%d for a live event: no log line names its live criterion after %d days"
             % (repo, number, VERIFY_WAIT_DAYS))

    def _clear_live_event(self, repo: str, number: int) -> None:
        """Drop wait:live-event when the event showed and the ticket is handed
        out to be verified, so a failed run re-earns the hold."""
        try:
            self.gh.rest("DELETE", "/repos/%s/issues/%d/labels/%s"
                         % (repo, number, quote(brain.WAIT_LIVE_EVENT, safe="")), ok=(404,))
        except RateLimited:
            raise
        except GitHubError as err:
            _say("could not clear the live-event hold on %s#%d: %s" % (repo, number, err))

    def _verify_note(self, repo: str, number: int, lane: str, why: str) -> None:
        # A log line, not a comment: each failed verify run emailed the
        # operator, every 6 hours per shipped ticket (2026-10-18).
        _say("verify of %s#%d on lane %s left it shipped: %s; tried again after %d hours"
             % (repo, number, lane, why, VERIFY_INTERVAL // 3600))

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
        except RateLimited:
            raise
        except GitHubError as err:
            _say("could not label %s#%d as awaiting evidence: %s" % (repo, number, err))

    def _clear_awaiting(self, repo: str, number: int) -> None:
        """Drop the awaiting-evidence label when the ticket is handed out to be
        verified again, so the new run decides afresh."""
        try:
            self.gh.rest("DELETE", "/repos/%s/issues/%d/labels/%s"
                         % (repo, number, quote(brain.AWAITING_EVIDENCE, safe="")), ok=(404,))
        except RateLimited:
            raise
        except GitHubError as err:
            _say("could not clear the awaiting label on %s#%d: %s" % (repo, number, err))

    def _read_result(self, lane: str, name: str, filename: str) -> Tuple[Optional[Dict[str, Any]], str]:
        """(the lane's result file, "") or (None, why it cannot be used): the
        shared reader behind _read_verify and _read_review."""
        path = os.path.join(self.s.home, "logs", lane, name + "." + filename)
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return None, "The run wrote no %s." % filename
        except ValueError:
            return None, "Its %s is not valid JSON." % filename
        if not isinstance(data, dict):
            return None, "Its %s is not a JSON object." % filename
        return data, ""

    def _read_verify(self, lane: str, name: str) -> Tuple[Optional[Dict[str, Any]], str]:
        """(the lane's verify.json, "") or (None, why it cannot be used)."""
        return self._read_result(lane, name, "verify.json")

    def _review_model(self, lane: str, name: str) -> str:
        """The model the review run used, as the runner recorded it next to
        review.json; "" when it recorded none."""
        path = os.path.join(self.s.home, "logs", lane, name + ".review.model")
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read().strip()
        except FileNotFoundError:
            return ""

    def _read_review(self, lane: str, name: str) -> Tuple[Optional[Dict[str, Any]], str]:
        """(the lane's review.json, "") or (None, why it cannot be used)."""
        return self._read_result(lane, name, "review.json")

    def _report_verify(self, lane: str, kind: str, repo: str, number: int, name: str) -> bool:
        """Apply a finished verify run. A failed run changes nothing: the
        ticket is due again once its cooldown is over."""
        if kind == "failed":
            klass = failure_class(self._failure_reason(lane, name))
            if klass in ("transient", "limit"):
                # The run never looked at the ticket: it costs the ticket no
                # cooldown, and a lane failing this way twice sits out an hour
                # (loom#714).
                self._forget_verify_attempt(repo, number)
                held = klass == "transient" and self._verify_strike(lane)
                _say("verify run %s failed [%s]; %s#%d is due again now%s"
                     % (name, klass, repo, number,
                        "; lane %s takes no verify run for an hour after %d transient "
                        "verify failures in a row" % (lane, VERIFY_STRIKES) if held else ""))
                return True
            self._verify_clear(lane)
            # Status, not a fault: the ticket waits its cooldown. The line
            # names the class and the reason the runner recorded, which it
            # once left out, so a repeat could not be diagnosed (loom#463);
            # the log watcher reads it as status (watch.VERIFY_RUN_RE).
            reason = self._failure_reason(lane, name)
            _say("verify run %s failed [%s]: %s; %s#%d stays shipped and is tried again "
                 "after %d hours" % (name, klass, CLASS_RE.sub("", reason), repo, number,
                                     VERIFY_INTERVAL // 3600))
            return True
        self._verify_clear(lane)
        result, why = self._read_verify(lane, name)
        if result is None:
            self._verify_note(repo, number, lane, why)
        elif set(result) == {"missing"}:
            self._verify_note(repo, number, lane, "Missing evidence: %s" % self._note_text(
                result["missing"]))
            self._await_evidence(repo, number, result["missing"])
        elif set(result) == {"unmet"}:
            self._refute_merged(lane, repo, number, result["unmet"])
        else:
            self._apply_proof(lane, repo, number, result, self._verify_model(lane, name))
        return True

    @staticmethod
    def _review_findings(value: Any) -> Optional[List[Dict[str, Any]]]:
        """A review.json finding list, cleaned, or None when its shape is
        not a list of findings: each entry keeps its finding text (whitespace
        collapsed, capped), an optional file and an optional line."""
        if not isinstance(value, list):
            return None
        out: List[Dict[str, Any]] = []
        for entry in value[:REVIEW_FINDINGS_MAX]:
            if not isinstance(entry, dict) or not isinstance(entry.get("finding"), str):
                return None
            finding = " ".join(entry["finding"].split())[:REVIEW_FINDING_CHARS]
            if not finding:
                return None
            item: Dict[str, Any] = {"finding": finding}
            if isinstance(entry.get("file"), str) and entry["file"]:
                item["file"] = entry["file"]
            if isinstance(entry.get("line"), int) and entry["line"] > 0:
                item["line"] = entry["line"]
            out.append(item)
        return out

    @staticmethod
    def _finding_line(finding: Dict[str, Any]) -> str:
        where = ""
        if "file" in finding:
            where = "`%s`%s: " % (finding["file"],
                                  " (line %d)" % finding["line"] if "line" in finding else "")
        return where + finding["finding"]

    def _report_review(self, lane: str, kind: str, repo: str, number: int,
                       name: str, text: str) -> bool:
        """Apply a finished Luna review run (loom#2142): post the lane's
        review.json on the pull request as a verdict in the Elder's markings
        shape, with its blocking findings as review threads when they name a
        file and a line, so the factory's gate and fix rounds read them
        exactly like the review bot's own. A failed run, an unreadable
        result or a result in the wrong shape posts nothing: the factory
        dispatches again after its retry window. A result also posts
        nothing when it did not come from the review slot's own Luna run:
        the gate trusts the posted verdict, so no other lane or model may
        speak it."""
        if kind == "failed":
            _say("Luna review run %s on lane %s failed; %s#%d keeps waiting for "
                 "a verdict" % (name, lane, repo, number))
            return True
        if lane != factory.LUNA_REVIEW_LANE:
            _say("Luna review run %s reported on lane %s, not the review slot "
                 "%s; its verdict is not posted" % (name, lane, factory.LUNA_REVIEW_LANE))
            return True
        model = self._review_model(lane, name)
        if model != factory.LUNA_MODEL:
            _say("Luna review run %s recorded model %s, not %s; its verdict is "
                 "not posted" % (name, model or "none", factory.LUNA_MODEL))
            return True
        result, why = self._read_review(lane, name)
        if result is None:
            _say("Luna review run %s left no usable verdict: %s" % (name, why))
            return True
        blocking = self._review_findings(result.get("blocking"))
        notes = self._review_findings(result.get("notes"))
        summary = result.get("summary")
        pr_field = self._field(text, "Review-PR")
        sha = self._field(text, "Head-Sha")
        if (blocking is None or notes is None or not isinstance(summary, str)
                or not pr_field.isdigit() or not sha):
            _say("Luna review run %s wrote a verdict in the wrong shape; "
                 "nothing posted" % name)
            return True
        pr_number = int(pr_field)
        # Each anchored blocking finding becomes a review thread of its own
        # (one pulls/comments post per finding), so the factory's fix rounds
        # and rebuttal reads meet them exactly like the review bot's threads.
        for finding in [f for f in blocking if "file" in f and "line" in f]:
            try:
                self.gh.rest("POST", "/repos/%s/pulls/%d/comments" % (repo, pr_number), {
                    "commit_id": sha, "path": finding["file"],
                    "line": finding["line"], "side": "RIGHT",
                    "body": finding["finding"]})
            except GitHubError as err:
                _say("could not post a Luna review thread on %s#%d (%s); the "
                     "finding still lands in the verdict comment" % (repo, pr_number, err))
        lines = [factory.LUNA_REVIEW_MARKER % sha,
                 "Elder markings - %d blocking, %d total"
                 % (len(blocking), len(blocking) + len(notes)),
                 "", " ".join(summary.split())[:1000] or "The lane gave no summary.",
                 "",
                 "This verdict comes from a Codex Luna review (lane %s, gpt-6-luna "
                 "through `codex exec`) while the Elder review route is degraded. "
                 "The merge gate reads it for this head in the degraded check's "
                 "place." % lane]
        if blocking:
            lines += ["", "Blocking findings:"] + \
                     ["- " + self._finding_line(f) for f in blocking]
        if notes:
            lines += ["", "Notes:"] + ["- " + self._finding_line(f) for f in notes]
        try:
            notify.post(self.gh, repo, pr_number, "\n".join(lines), notify.RECORD)
        except GitHubError as err:
            _say("could not post the Luna verdict on %s#%d (%s)" % (repo, pr_number, err))
            return True
        _say("posted the Luna verdict on %s#%d: %d blocking, %d total"
             % (repo, pr_number, len(blocking), len(blocking) + len(notes)))
        return True

    def _refute_merged(self, lane: str, repo: str, number: int, why: Any) -> None:
        """A verify run's answer that the merged change does not meet the
        criteria (loom#1385). It counts only for a ticket the station sent to
        verification before any build (_verify_before_build): the ticket goes
        back to loom:queued for a build, with the reason as a status event
        naming the pull request, and that pull request is not sent to a verify
        run again (tickets.judged_pulls). Any other ticket stays shipped."""
        issue = self._issue(repo, number)
        names = _label_names(issue)
        if issue.get("state") != "open" or "loom:shipped" not in names or "loom:verified" in names:
            _say("%s#%d is no longer shipped; its verify result is dropped" % (repo, number))
            return
        sent = tickets.pending_verifies(self._status_texts(repo, number))
        if not sent:
            self._verify_note(repo, number, lane, "It said the merged change does not meet the "
                              "criteria, but no merged pull request was sent to it before a build")
            return
        if not isinstance(why, str) or len(why.strip()) < MISSING_MIN:
            self._verify_note(repo, number, lane, "Its unmet answer does not say what is unmet")
            return
        states = {"loom:" + state for state in brain.STATES}
        labels = [n for n in names if n not in states and not n.startswith(WAIT)
                  and n != brain.AWAITING_EVIDENCE]
        labels.append("loom:queued")
        self.gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number), {"labels": labels})
        for pull in sent:
            self._routine(repo, number, tickets.REFUTED_NOTE % (pull, self._note_text(why)))
        self._forget_verify_attempt(repo, number)
        _say("%s#%d: the merged change does not meet its criteria; queued for a build (lane %s)"
             % (repo, number, lane))

    def _verify_refused_ledger(self) -> Dict[str, Any]:
        # One station pass runs at a time (run() holds station.lock) and applies
        # verify results one after another, and _write_json replaces the file
        # whole, so the ledger is never half written or updated by two writers.
        data = _read_json(os.path.join(self.state, VERIFY_REFUSED_FILE), {}, strict=False)
        return data if isinstance(data, dict) else {}

    def _forget_refusals(self, repo: str, number: int) -> None:
        data = self._verify_refused_ledger()
        if data.pop("%s#%d" % (repo, number), None) is not None:
            _write_json(os.path.join(self.state, VERIFY_REFUSED_FILE), data)

    def _proof_refused(self, repo: str, number: int, lane: str, names: List[str],
                       why: str) -> None:
        """Log a refused proof and count it. At VERIFY_REFUSAL_CAP the ticket
        leaves the verify queue (verify_due) and goes to the brain once: one
        comment lists the refusals and the ticket gains needs-brain."""
        self._verify_note(repo, number, lane, why)
        data = self._verify_refused_ledger()
        key = "%s#%d" % (repo, number)
        reasons = data.get(key)
        reasons = (reasons if isinstance(reasons, list) else []) + [why]
        data[key] = reasons
        _write_json(os.path.join(self.state, VERIFY_REFUSED_FILE), data)
        if len(reasons) != VERIFY_REFUSAL_CAP:
            return
        if "needs-brain" not in names:
            notify.post(self.gh, repo, number,
                "<!-- loom:verify-refused v1 -->\n"
                "The station stops handing this ticket out to verify: %d proofs were "
                "refused. The brain reads the ticket and its live criterion, then proves "
                "it or rewrites it.\n\n%s\n" % (
                    len(reasons), "\n".join("- " + self._note_text(x) for x in reasons)),
                notify.PROOF_REFUSED)
            self._add_label(repo, number, "needs-brain")
        _say("%s#%d left the verify queue after %d refused proofs; left for the brain"
             % (repo, number, len(reasons)))

    def _forget_verify_attempt(self, repo: str, number: int) -> None:
        path = os.path.join(self.state, VERIFY_FILE)
        attempts = _read_json(path, {}, strict=False)
        if isinstance(attempts, dict) and attempts.pop("%s#%d" % (repo, number), None) is not None:
            _write_json(path, attempts)

    def _verify_lanes(self) -> Dict[str, Any]:
        data = _read_json(os.path.join(self.state, VERIFY_LANES_FILE), {}, strict=False)
        return data if isinstance(data, dict) else {}

    def _verify_strike(self, lane: str) -> bool:
        """Count one transient verify failure for the lane; True when it now
        sits out VERIFY_LANE_HOLD seconds."""
        data = self._verify_lanes()
        entry = data.get(lane) if isinstance(data.get(lane), dict) else {}
        strikes = int(entry.get("strikes", 0) or 0) + 1
        held = strikes >= VERIFY_STRIKES
        data[lane] = {"strikes": 0 if held else strikes,
                      "until": self.now + VERIFY_LANE_HOLD if held else entry.get("until", 0)}
        _write_json(os.path.join(self.state, VERIFY_LANES_FILE), data)
        return held

    def _verify_clear(self, lane: str) -> None:
        """A verify run that reached the ticket ends the lane's strike run."""
        data = self._verify_lanes()
        entry = data.get(lane)
        if isinstance(entry, dict) and entry.get("strikes"):
            entry["strikes"] = 0
            _write_json(os.path.join(self.state, VERIFY_LANES_FILE), data)

    def _verify_lane_held(self, lane: str) -> bool:
        entry = self._verify_lanes().get(lane)
        until = float(entry.get("until", 0) or 0) if isinstance(entry, dict) else 0.0
        if until > self.now:
            _say("lane %s takes no verify run until %s: %d transient verify failures in a row"
                 % (lane, time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(until)), VERIFY_STRIKES))
            return True
        return False

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

    def _proof_mismatch(self, repo: str, issue: Dict[str, Any], record: Dict[str, Any]) -> str:
        """Why a lane's proof does not prove the ticket's merged change, or ""
        when it does: its revision must contain the merge commit of each linked
        merged pull request (the ones the verify brief shows), and its observed
        window must not start before the newest of them merged. A stale build,
        or evidence read before the fix landed, proves nothing about the fix.
        A ticket with no linked merged pull request is left to the close rule,
        and a pull request merged into another branch is not counted."""
        linked = self._merged_linked(repo, issue)[:verify_data.MAX_PULLS]
        if not linked:
            return ""
        revision = str(record["revision"]).strip()
        for pr in linked:
            sha = str(pr["merge_commit_sha"])
            if not self._carries(repo, sha, revision):
                return "revision %s does not contain the merge commit %s of PR #%d" % (
                    revision[:12], sha[:12], int(pr["number"]))
        newest = linked[0]
        start = str(record["observed"]["start"])
        merged, began = self._epoch(str(newest["merged_at"])), self._epoch(start)
        if merged is not None and began is not None and began < merged:
            return "the observed window starts %s before PR #%d merged at %s" % (
                start, int(newest["number"]), newest["merged_at"])
        return ""

    def _merge_refusal(self, repo: str, issue: Dict[str, Any], record: Dict[str, Any]) -> str:
        """The verify note for a proof that does not prove the merged change,
        or "" when it does. A GitHub error other than a rate limit leaves the
        ticket shipped with a note, never verified on an unchecked revision."""
        try:
            mismatch = self._proof_mismatch(repo, issue, record)
        except RateLimited:
            raise
        except GitHubError as err:
            return ("could not check the proof's revision against its merged pull requests: "
                    "%s" % self._note_text(err))
        return "Its proof was refused: %s." % mismatch if mismatch else ""

    def _proof_facts(self, repo: str, issue: Dict[str, Any], record: Dict[str, Any]
                     ) -> Tuple[str, List[str]]:
        """(why the lane's evidence contradicts the merged pull requests'
        checks, or "", the facts the proof comment carries). Facts the station
        could not read cannot back a close: it tries again after the cooldown
        rather than sign the lane's prose alone."""
        branch, pulls, error = self._merged_pull_facts(repo, issue, files=False)
        if error:
            return "the station could not read the merged pull requests (%s)" % error, []
        return (proof.contradicts(record, verify_data.failing_checks(pulls)),
                verify_data.proof_facts(branch, pulls))

    def _apply_proof(self, lane: str, repo: str, number: int, record: Dict[str, Any],
                     model: str) -> None:
        """Check the record with bus/proof.py, tick the criteria and move the
        ticket to verified with it, marked verified_by "<lane>/<model>"; a
        refused record leaves the ticket shipped with one note. The station
        reads the merged pull requests' facts itself: evidence that says every
        check passed when one did not is refused, and the proof comment
        carries those facts, so the close does not rest on the lane's prose."""
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
            self._proof_refused(repo, number, lane, names, "Its proof was refused: %s." % self._note_text(err))
            return
        if _text.CREDENTIAL_RE.search(proof.render(record, ticked)):
            self._proof_refused(repo, number, lane, names, "Its proof holds text shaped like a credential.")
            return
        unproven = criteria_gate.unverified(body, self._live_passed(repo, number, body))
        if unproven:
            # A verify lane runs with no network: its word that a live check
            # passed, or that the operator acted, is not evidence (2026-10-22
            # audit). The station's own check, or the operator, decides.
            self._proof_refused(repo, number, lane, names, (
                "Its proof was refused: a verify lane may not pass a live or operator "
                "criterion without a passing station check: %s." % self._note_text(
                    "; ".join(_clip(" ".join(t.split()), 120) for _, t in unproven[:3]))))
            return
        unseen = unseen_live_evidence(record)
        if unseen:
            self._proof_refused(repo, number, lane, names, "Its proof was refused: %s." % unseen)
            return
        refusal = self._merge_refusal(repo, issue, record)
        if refusal:
            self._proof_refused(repo, number, lane, names, refusal)
            return
        contradiction, facts = self._proof_facts(repo, issue, record)
        if contradiction:
            self._proof_refused(repo, number, lane, names, "Its proof was refused: %s." % self._note_text(
                contradiction))
            return
        self.gh.rest("PATCH", "/repos/%s/issues/%d" % (repo, number), {"body": ticked})
        try:
            source = brain.move(self.gh, repo, number, "verified", record=record,
                                verified_by="%s/%s" % (lane, model), facts=facts)
        except ValueError as err:
            self.gh.rest("PATCH", "/repos/%s/issues/%d" % (repo, number), {"body": body})
            self._proof_refused(repo, number, lane, names, "Its proof was refused: %s." % self._note_text(err))
            return
        self._forget_refusals(repo, number)
        _say("verified %s#%d from the proof lane %s gathered" % (repo, number, lane))
        if source:
            _say("%s#%d: %s" % (repo, number, source))
        try:
            source = brain.close(self.gh, repo, number, reason="completed",
                                 env={"LOOM_BRIEF_AUTHORS": " ".join(self.s.authors)})
            if source:
                _say("%s#%d: %s" % (repo, number, source))
        except RateLimited:
            raise
        except (ValueError, GitHubError) as err:
            _say("verified but not closed %s#%d: %s" % (repo, number, self._note_text(err)))

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
            except RateLimited:
                raise
            except GitHubError as err:
                _say("could not read %s: %s" % (key, err))
                self._depends[key] = None
        return self._depends[key]

    def _release_hold(self, repo: str, issue: Dict[str, Any],
                      released: List[tickets.After]) -> None:
        """Say on the ticket that an After: line no longer holds it, once per
        ticket that was closed as not planned."""
        number = int(issue["number"])
        said = {m.group(1) for c in self._paginate(
            "/repos/%s/issues/%d/comments" % (repo, number)) if isinstance(c, dict)
            for m in [AFTER_RELEASE_RE.match(str(c.get("body") or ""))] if m}
        for dep in released:
            if str(dep.number) in said:
                continue
            notify.post(self.gh, repo, number,
                        "<!-- loom:after-release %d -->\nNot held by After: any more: "
                        "%s was closed as not planned, so this ticket no longer waits "
                        "for it." % (dep.number, dep.short), notify.RECORD)
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
        if self._target_reached():
            return False
        if not ok:
            self._refuse(repo, issue, reason)
            return False
        if self._goal_elsewhere(repo, issue):
            return False
        waiting, released = tickets.after_holds(repo, headers, self._dependency)
        if released and not waiting:
            self._release_hold(repo, issue, released)
        if waiting:
            self._set_wait(repo, issue, "after")
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
        # The claiming lane must take the model too: a ticket handed to a free
        # lane as a mismatch was claimed there although that lane's runner
        # refuses the model, and the refusal cost a rung (loom#1470).
        if model and not (lane_takes_model(lane, model) and
                          (not labelled_lane or lane_takes_model(labelled_lane, model))):
            lane = self._route_model_claim(repo, fresh, labelled_lane or lane, model, free)
            if lane is None:
                return False
        if self._model_waits(repo, issue, lane, headers):
            return False
        if needs_public_repo(lane, model) and not self._public(repo):
            # A lane that became public-only after the ticket was labeled for
            # it (a slot moved to cluster runners): the dispatch would park
            # the brief and only then retry it (loom#1917). Back to the queue,
            # where the factory gives it a lane that takes private repos.
            names = [n for n in _label_names(fresh) if not n.startswith("lane:")
                     and n not in (BRIEFED, "loom:queued")] + ["loom:queued"]
            self.gh.rest("PUT", "/repos/%s/issues/%s/labels" % (repo, fresh["number"]),
                         {"labels": names})
            _say("%s#%s: lane %s takes public repos only; back to the queue"
                 % (repo, fresh["number"], lane))
            return False
        return self._claim(repo, lane, fresh)

    def _status_texts(self, repo: str, number: int) -> List[str]:
        """The texts of the ticket's comments and status events written by a
        briefing login (the client expands status comments, one item per event)."""
        allowed = {a.lower() for a in self.s.authors}
        return [str(c.get("body") or "") for c in self._paginate(
            "/repos/%s/issues/%d/comments" % (repo, number))
            if isinstance(c, dict) and tickets._login(c.get("user")) in allowed]

    def _verify_before_build(self, repo: str, issue: Dict[str, Any]) -> bool:
        """Send a ticket to a verify run instead of a build lane when a pull
        request that merged into the default branch already covers it (loom#1385).

        Covers means linked the way reconcile links (a Refs line or a
        loom/<number> branch) or a Refs line naming the issue the ticket was
        rewritten from, and the merge may predate the ticket: a rewrite of an
        issue whose work had merged two days earlier burned three lanes for
        nothing to change. The ticket moves to loom:merged, as after a build
        run's no-change answer (_settle_no_change), so the ship rules and a
        verify run judge it against its criteria. True when it moved.

        Left to build: a regression or revert ticket, a ticket whose Live
        criterion names nothing a verify run can search for (it would never be
        verified), a ticket with a fix round, and any pull request the station
        already judged for it: one it sent back for unbuilt criteria, or one
        a verify run found wanting (tickets.judged_pulls)."""
        names = _label_names(issue)
        if set(names) & {REGRESSION, factory.REVERT_LABEL} or live_needles(issue.get("body")) == []:
            return False
        number = int(issue["number"])
        merged = self._merged_linked(repo, issue) or self._merged_cited(repo, issue)
        if not merged:
            return False
        texts = self._status_texts(repo, number)
        if any(t.startswith(tickets.FIX_MARKER) for t in texts):
            return False
        judged = set(tickets.judged_pulls(texts))
        fresh = [pr for pr in merged if isinstance(pr.get("number"), int)
                 and pr["number"] not in judged]
        if not fresh:
            return False
        pr = fresh[0]
        states = {"loom:" + state for state in brain.STATES}
        labels = [n for n in names if n not in states and not n.startswith(WAIT)]
        labels.append("loom:merged")
        self.gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number), {"labels": labels})
        issue["labels"] = [{"name": n} for n in labels]
        self._routine(repo, number, tickets.MERGED_FIRST_NOTE % (
            int(pr["number"]), pr.get("merged_at"), str(pr["merge_commit_sha"])[:12]))
        _say("%s#%d: merged pull request #%d may already do its work; verifying before any build"
             % (repo, number, int(pr["number"])))
        return True

    def _held_before_claim(self, repo: str, issue: Dict[str, Any], fix: Any,
                           comments: List[Any]) -> bool:
        """A fix round its pull request blocks, or a new build the brief
        check holds (a fix round's brief is the ticket's own, so it is not
        checked again)."""
        return (self._fix_blocks(repo, issue, fix)
                or (fix is None and self._brief_holds(repo, issue, comments)))

    def _brief_holds(self, repo: str, issue: Dict[str, Any], comments: List[Any]) -> bool:
        """Whether the ticket goes to no build lane (bus/brief_check.py): a
        health ticket whose cause is missing or names only the measurement
        goes back to the queue with needs-brain; a stale or already-shipped
        one goes back to the queue with loom:human and one ask. A read the
        check needs that fails holds the ticket for the next pass. A
        regression or revert ticket is checked for its cause only: its merged
        pull request is why it exists."""
        number = int(issue["number"])
        names = _label_names(issue)
        body = str(issue.get("body") or "")
        found = brief_check.health_verdict(body)
        if found is None and not set(names) & {REGRESSION, factory.REVERT_LABEL}:
            try:
                found = brief_check.check(self.gh, repo, issue,
                                          [c for c in comments if isinstance(c, dict)],
                                          self.s.authors, self._default_branch(repo))
            except brief_check.Unread as err:
                _say("%s#%d waits: the stale-ticket check could not read (%s)"
                     % (repo, number, err))
                return True
        if found is None:
            return False
        try:
            brain.move(self.gh, repo, number, "queued")
            if found.kind == brief_check.HEALTH:
                # Added, not put: the label list read before the move is stale.
                self.gh.rest("POST", "/repos/%s/issues/%d/labels" % (repo, number),
                             {"labels": ["needs-brain"]})
                self._routine(repo, number, brief_check.HEALTH_NOTE % (
                    found.reason, "A lane gets it once its body names the cause on a "
                    "`Cause:` line: why the affected are affected, not what the health read "
                    "counts."))
            elif found.kind == brief_check.CRITERIA:
                # Added, not put: the label list read before the move is stale.
                self.gh.rest("POST", "/repos/%s/issues/%d/labels" % (repo, number),
                             {"labels": ["needs-brain"]})
                self._routine(repo, number, brief_check.HEALTH_NOTE % (
                    found.reason, "A lane gets it once its body names each acceptance "
                    "criterion as a checkbox under `## Acceptance criteria`, stated as "
                    "an observable result: the brief restates each one as the check "
                    "that proves it."))
            else:
                brain.human(self.gh, repo, number, brief_check.ask(found))
        except ValueError as err:
            _say("%s#%d gets no lane (%s), but holding it failed: %s"
                 % (repo, number, found.reason, err))
            return True
        _say("%s#%d gets no lane: %s" % (repo, number, found.reason))
        self._event("not_claiming", found.reason, "%s#%d" % (repo, number))
        return True

    def _model_waits(self, repo: str, issue: Dict[str, Any], lane: str,
                     headers: Dict[str, str]) -> bool:
        """The lane is up, but not for this ticket's model until its limit
        lifts: a claim now is parked at once, and was re-claimed every pass
        (24 claims in 90 minutes). It waits briefed; the factory moves it to
        a lane that serves it, or it waits for the reset."""
        model = brief_model(lane, headers)
        until = self.model_limited(lane, model)
        if until is None:
            return False
        _say("%s#%s waits: model %s is limited on lane %s until %s"
             % (repo, issue["number"], model, lane, self._utc(until)))
        self._set_wait(repo, issue, "model-limit")
        return True

    def _route_model_claim(self, repo: str, issue: Dict[str, Any], lane: str,
                           model: str, free: List[str]) -> Optional[str]:
        target = next((candidate for candidate in model_lanes(model)
                       if candidate in free and self._inbox_empty(candidate)
                       and (not needs_public_repo(candidate, model) or self._public(repo))), None)
        number = int(issue["number"])
        names = [name for name in _label_names(issue) if not name.startswith("lane:")]
        already_waiting = WAIT + "lane" in names
        if target is None:
            names = [name for name in names if name != BRIEFED and not name.startswith(WAIT)] \
                + ["loom:queued", WAIT + "lane"]
            self.gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number),
                         {"labels": names})
            # A log line, not a comment: each comment emails the operator
            # (2026-10-18), and the ticket's labels already say it waits.
            _say("%s#%d waits: no free lane takes model %s" % (repo, number, model))
            if not already_waiting:
                self._event("model_wait", "no free lane takes model %s" % model,
                            "%s#%d" % (repo, number), lane, model)
            return None
        names.append("lane:" + target)
        self.gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number),
                     {"labels": names})
        issue["labels"] = [{"name": name} for name in names]
        _say("moved %s#%d from lane %s to lane %s for model %s"
             % (repo, number, lane, target, model))
        if already_waiting:
            self._event("model_wait_succeeded", "found free lane %s for model %s" % (target, model),
                        "%s#%d" % (repo, number), lane, model)
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
            except RateLimited:
                raise
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

    def _reconcile_pr_ticket(self, repo: str, pr: dict, tickets_by_number: dict,
                             landed: Sequence[dict] = ()) -> None:
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
            events = self._paginate("/repos/%s/issues/%d/events" % (repo, number))
            if tickets.merged_before_regression_requeue(ticket, pr.get("merged_at"), events):
                return
        left: List[str] = []
        merged = target == "loom:merged"
        if merged:
            left = self._unbuilt_criteria(ticket, [pr, *landed])
            if left:
                events = self._paginate("/repos/%s/issues/%d/events" % (repo, number))
                if (tickets.merged_before_requeue(pr.get("merged_at"), events)
                        or "loom:queued" in labels):
                    return
                target = "loom:queued"
        # ready and answered are states too: a ticket moved on from loom:pr kept
        # loom:ready and read as ready everywhere (loom#1393).
        state_names = ("queued", "briefed", "running", "pr", "ready", "answered", "failed",
                       "merged", "shipped", "verified", "closed-unmerged")
        states = {"loom:" + state for state in state_names}
        kept = self._drop_claim(name for name in labels if name not in states)
        # A merged pull request lands the lane's run, so a runaway hold the run
        # caused is cleared even when the ticket is requeued to build criteria
        # the pull request left unbuilt; otherwise a runaway ticket can never
        # be claimed again (bus/station.py, test_a_runaway_ticket_is_not_claimed).
        cleared = merged and RUNAWAY in kept
        if cleared:
            kept = [name for name in kept if name != RUNAWAY]
        kept.append(target)
        self.gh.rest("PUT", "/repos/%s/issues/%s/labels" % (repo, ticket["number"]),
                     {"labels": kept})
        ticket["labels"] = [{"name": name} for name in kept]
        if cleared:
            self._routine(repo, number,
                          "Pull request #%d merged, so this ticket's loom:runaway hold "
                          "is cleared." % int(pr["number"]))
        if left:
            # Routine: the ticket goes back for the rest, and a comment would
            # email the operator on each such merge.
            self._routine(repo, number, tickets.UNBUILT_NOTE
                          % (int(pr["number"]), "; ".join(left)))
            _say("%s#%d: merged pull request #%d left %d criteria unbuilt, requeued"
                 % (repo, number, int(pr["number"]), len(left)))

    @staticmethod
    def _unbuilt_criteria(ticket: dict, merged: Sequence[dict]) -> List[str]:
        """The ticket's code criteria that no merged pull request for it ticks
        built: a box marked "not built: <reason>" is still unbuilt (loom#2026)."""
        return factory.unbuilt_criteria(str(ticket.get("body") or ""),
                                        [str(pr.get("body") or "") for pr in merged])

    def _reconcile_adoptions(self, repo: str, open_tickets: List[Dict[str, Any]]) -> None:
        """Move each open adoption ticket to loom:merged once the pull
        request its title and body name has merged, freeing its lane. A
        target still open, or closed unmerged, leaves the ticket: its job is
        then to supersede that pull request (loom#1020)."""
        # loom:failed too: a lane run on the ticket can fail while its target
        # merges, and the factory then re-briefed landed work (loom#1717).
        waiting = ("loom:queued", "loom:briefed", "loom:running", "loom:answered",
                   "loom:failed")
        for issue in open_tickets:
            if (not isinstance(issue, dict) or "pull_request" in issue
                    or not set(_label_names(issue)).intersection(waiting)):
                continue
            number = self._adopted_pull_number(issue)
            if number is None:
                continue
            pr = self.gh.rest("GET", "/repos/%s/pulls/%d" % (repo, number)).data
            if isinstance(pr, dict) and self._terminal_label(pr) == "loom:merged":
                self._move_adoption_to_merged(repo, issue, pr)

    def _move_adoption_to_merged(self, repo: str, ticket: Dict[str, Any],
                                 pr: Dict[str, Any]) -> None:
        """Move an adoption ticket to loom:merged with a note naming the
        merged target, its merge commit and time, and drop the lane label."""
        number = int(ticket["number"])
        states = {"loom:" + state for state in brain.STATES + tickets.TICKET_STATES}
        kept = self._drop_claim(name for name in _label_names(ticket)
                                if name not in states and not name.startswith(("lane:", "wait:")))
        cleared = RUNAWAY in kept
        if cleared:
            kept = [name for name in kept if name != RUNAWAY]
        kept.append("loom:merged")
        self.gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number),
                     {"labels": kept})
        ticket["labels"] = [{"name": name} for name in kept]
        note = ("Pull request #%d merged at %s (merge commit %s), so this Land or supersede "
                "ticket moved to loom:merged." % (
                    int(pr["number"]), str(pr.get("merged_at") or "").replace("T", " "),
                    str(pr.get("merge_commit_sha") or "")[:12]))
        self._comment(repo, number, note, notify.MERGED)
        if cleared:
            self._routine(repo, number,
                          "Pull request #%d merged, so this ticket's loom:runaway hold "
                          "is cleared." % int(pr["number"]))
        _say("moved %s#%d to loom:merged: land-or-supersede target #%d merged"
             % (repo, number, int(pr["number"])))

    def _reconcile_repo(self, repo: str) -> None:
        tickets_by_number = {int(i["number"]): i for i in self._pr_tickets(repo)
                             if "loom:ticket" in _label_names(i)}
        open_tickets = self._paginate("/repos/%s/issues?labels=loom:ticket&state=open" % repo)
        self._open_work[repo] = {
            int(i["number"]) for i in open_tickets
            if isinstance(i, dict) and "pull_request" not in i and "number" in i
            and "loom:ticket" in _label_names(i)
            and not TREE_DONE_STATES.intersection(_label_names(i))}
        self._reconcile_adoptions(repo, open_tickets)
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
            if head_repo != repo:
                continue
            if author not in trusted_authors:
                # A second contributor's pull request links only the
                # trusted ticket that contributor owns (loom#688).
                self._link_contributor_pr(repo, pr, author, tickets_by_number)
                continue
            number = self._pr_ticket_number(repo, pr)
            if number is None:
                continue
            ticket = tickets_by_number.get(number)
            if ticket is None:
                try:
                    ticket = self._issue(repo, number)
                except RateLimited:
                    raise
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
            if "loom:briefed" in labels and self._fix_round_for(repo, ticket, int(pr["number"])):
                # A brain fix round moved it back to briefed for a lane to fix
                # this very pull request: not a new link. Relabelling it pr
                # undid the round every pass, and the brain re-briefed it
                # (zippie#269, 2026-10-18: a flip and a comment every 3-6 min).
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
            _say("%s#%d: linked pull request #%d is open" % (repo, number, int(pr["number"])))
        # A ticket still at queued or briefed whose pull request merged:
        # a brain fix round sent it back to briefed and the pull request
        # merged before a lane took the fix, or the pull request opened and
        # merged between two passes (loom#784).
        unlinked = self._unlinked_tickets(repo, tickets_by_number, open_tickets)
        if not tickets_by_number and not unlinked:
            return
        waiting = {n: t for n, t in tickets_by_number.items()
                   if not any(_linked_pr(t, repo, p) for p in open_prs)}
        closed = self.gh.rest("GET", "/repos/%s/pulls?state=closed&sort=updated"
                              "&direction=desc&per_page=100" % repo).data
        for pr in closed if isinstance(closed, list) else []:
            if isinstance(pr, dict) and pr.get("state") == "closed":
                merged = self._terminal_label(pr) == "loom:merged"
                pool = tickets_by_number if merged else waiting
                landed = [other for other in closed if other is not pr and isinstance(other, dict)
                          and self._terminal_label(other) == "loom:merged"
                          and self._pr_ticket_number(repo, other) == self._pr_ticket_number(repo, pr)
                          ] if merged else []
                self._reconcile_pr_ticket(repo, pr, pool, landed)
                if merged:
                    self._deliver_unlinked(repo, pr, unlinked)
        # Also check contributor-linked tickets for merging
        for pr in closed if isinstance(closed, list) else []:
            if isinstance(pr, dict) and pr.get("state") == "closed":
                merged = self._terminal_label(pr) == "loom:merged"
                if not merged:
                    continue
                # Find tickets linked by this contributor PR
                author = tickets._login(pr.get("user"))
                if author in trusted_authors:
                    continue
                # Look for tickets this contributor owns and linked by this PR
                number = self._pr_ticket_number(repo, pr)
                if number is not None:
                    ticket = tickets_by_number.get(number)
                    if ticket is not None and self._contributor_ticket(repo, pr, author) is ticket:
                        # This is a contributor-linked ticket
                        pool = tickets_by_number if merged else waiting
                        self._reconcile_pr_ticket(repo, pr, pool, landed)
                        # Check if it moved to merged
                        if "loom:merged" in _label_names(ticket):
                            _say("%s#%d: contributor-linked pull request #%d moved to loom:merged" % (repo, number, int(pr["number"])))
                    ticket = tickets_by_number.get(number)
                    if ticket is not None and self._contributor_ticket(repo, pr, author) is ticket:
                        # This is a contributor-linked ticket
                        pool = tickets_by_number if merged else waiting
                        self._reconcile_pr_ticket(repo, pr, pool, landed)
                        # Check if it moved to merged
                        if "loom:merged" in _label_names(ticket):
                            _say("%s#%d: contributor-linked pull request #%d moved to loom:merged" % (repo, number, int(pr["number"])))

    def _unlinked_tickets(self, repo: str, linked: Dict[int, Dict[str, Any]],
                          found: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
        """The repo's open tickets at loom:queued or loom:briefed that a
        briefing login filed, by number. The open-tickets list is the verify
        runs' own read, so a pass that reads both pays for it once; with no
        such ticket and no loom:pr ticket, no closed pull request is read."""
        out: Dict[int, Dict[str, Any]] = {}
        for issue in found:
            if (isinstance(issue, dict) and "pull_request" not in issue
                    and int(issue.get("number") or 0) not in linked
                    and "loom:ticket" in _label_names(issue)
                    and set(_label_names(issue)).intersection(("loom:queued", "loom:briefed"))
                    and tickets.brief_author_trusted(issue, self.s.authors, self.s.briefers)):
                # The trust check reads the repo owner from repository_url.
                issue.setdefault("repository_url", "%s/repos/%s" % (self.gh.api, repo))
                out[int(issue["number"])] = issue
        return out

    def _deliver_trusted_author(self, repo: str, ticket: Dict[str, Any],
                                    pr: Dict[str, Any]) -> bool:
        """Whether the pull request author may deliver the ticket: a trusted
        author of a trusted ticket, or the contributor who owns it and whose
        ticket a trusted login filed."""
        author = tickets._login(pr.get("user"))
        authors = {name.lower() for name in self.s.authors}
        if author in authors:
            return self._check_trust(repo, ticket)[0]
        owners = set(tickets.assignees(ticket)) | set(tickets.claimed_by(ticket))
        return bool(author and author in owners
                    and tickets._login(ticket.get("user")) in authors)

    def _deliver_unlinked(self, repo: str, pr: Dict[str, Any],
                          unlinked: Dict[int, Dict[str, Any]]) -> None:
        """Move a queued or briefed ticket to loom:merged when this merged
        pull request delivers it: a same-repo branch, linked to the ticket,
        by a trusted author of a trusted ticket or by the contributor who
        owns it, and merged after the ticket last went to queued or briefed."""
        number = self._pr_ticket_number(repo, pr)
        ticket = unlinked.get(number) if number is not None else None
        if ticket is None or not _linked_pr(ticket, repo, pr):
            return
        head_repo = str(((pr.get("head") or {}).get("repo") or {}).get("full_name") or "")
        if head_repo != repo:
            return
        if not self._deliver_trusted_author(repo, ticket, pr):
            return
        events = self._paginate("/repos/%s/issues/%d/events" % (repo, number))
        if tickets.merged_before_requeue(pr.get("merged_at"), events):
            return
        labels = _label_names(ticket)
        was = "loom:briefed" if "loom:briefed" in labels else "loom:queued"
        self._reconcile_pr_ticket(repo, pr, {number: ticket})
        if "loom:merged" in _label_names(ticket):
            del unlinked[number]
            self._comment(repo, number, "Pull request #%d merged while this ticket was at %s, "
                          "so it moved to loom:merged." % (int(pr["number"]), was),
                          notify.MERGED)

    def _contributor_ticket(self, repo: str, pr: Dict[str, Any],
                            author: str) -> Optional[Dict[str, Any]]:
        """The open loom:ticket a pull request names when its author owns it
        (assigned, or labelled claim:<author>) and a trusted login filed it;
        None otherwise. The ticket is read only for such a pull request."""
        if not author or author in {a.lower() for a in self.s.authors}:
            return None
        number = self._pr_ticket_number(repo, pr)
        if number is None:
            return None
        try:
            ticket = self._issue(repo, number)
        except RateLimited:
            raise
        except GitHubError as err:
            if err.status in (404, 410):
                return None
            raise
        owners = set(tickets.assignees(ticket)) | set(tickets.claimed_by(ticket))
        if ("pull_request" in ticket or ticket.get("state") != "open"
                or "loom:ticket" not in _label_names(ticket) or author not in owners
                or tickets._login(ticket.get("user")) not in {a.lower() for a in self.s.authors}
                or not _linked_pr(ticket, repo, pr)):
            return None
        return ticket

    def _link_contributor_pr(self, repo: str, pr: Dict[str, Any], author: str,
                             found: Dict[int, Dict[str, Any]]) -> None:
        """An open pull request by the contributor who owns its ticket moves
        a queued ticket to loom:pr; it is never briefed or claimed. From
        loom:pr, the closed pull request pass moves it to loom:merged when it
        merges, as for a lane's pull request, and shipping follows."""
        ticket = self._contributor_ticket(repo, pr, author)
        if ticket is None or "loom:queued" not in _label_names(ticket):
            return
        number = int(ticket["number"])
        kept = [n for n in _label_names(ticket) if n != "loom:queued"] + ["loom:pr"]
        self.gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number), {"labels": kept})
        ticket["labels"] = [{"name": n} for n in kept]
        found[number] = ticket
        _say("%s#%d: linked pull request #%d is open" % (repo, number, int(pr["number"])))

    def _run_steps(self) -> None:
        """The pass's main step loop, given PASS_STEP_BUDGET seconds of its
        own: counted from the pass's start, the steps before it (reconcile,
        hand back, claim, assign, about six minutes on 2026-10-24) left one
        loop step a pass and a full turn of the loop took hours."""
        loop_started = time.monotonic()
        # Below the budget floor these wait for the reset; trees are local.
        steps = (self.roll_source_pins, self.ship_deployed, self.ship_pins, self.ship_rolls,
                     self.ship_merged,
                     self.ship_workflows, self.ship_path_workflows, self.unwind_reverts,
                     self.adopt_untracked_pulls, self.publish_lanes, self.forward_asks, self.post_overdue_standups,
                     self.forward_reports, self.triage_backlogs, self.rewrite_triaged,
                     self.close_done_tickets, self.settle_orphan_sources,
                     self.settle_claimed_tickets, self.release_stale_claims,
                     self.clean_labels,
                     self.queue_stateless_tickets,
                     self.restate_shipped_live,
                     self.health_intake, self.intake_friction,
                     # The packet after the tick's row, so it renders that tick.
                     self.watch_brain, self.upload_transcripts, self.post_brain_memory,
                     self.publish_handoff, self.relay_operator,
                     self.watch_default_branches, self.watch_deploys, self.score_lanes,
                     self.refresh_bench, self.run_factory, self.lift_brain_holds,
                     # The move first, so the invariants read a goal whose
                     # tickets sit under an epic.
                     self.move_outside_tickets, self.check_invariants,
                     self.release_factory_held)
        rotation_path = os.path.join(self.state, STEP_ROTATION_FILE)
        rotation = _read_json(rotation_path, {}, strict=False)
        start = rotation.get("next") if isinstance(rotation, dict) else 0
        start = start if isinstance(start, int) and 0 <= start < len(steps) else 0
        stopped_at = None
        for offset in range(len(steps)):
            if self._deferred():
                stopped_at = (start + offset) % len(steps)
                break
            # Past the budget the rest of the loop waits for the next pass,
            # which starts here: claims, fix rounds and deploys come round
            # every few minutes instead of after a 40-minute pass, and every
            # step still runs in turn, in the same order.
            if offset and time.monotonic() - loop_started >= PASS_STEP_BUDGET:
                stopped_at = (start + offset) % len(steps)
                _say("pass budget reached after %ds of steps: %d step(s) wait for the next pass, from %s"
                     % (time.monotonic() - loop_started, len(steps) - offset,
                        steps[stopped_at].__name__))
                break
            step = steps[(start + offset) % len(steps)]
            try:
                if not self._timed(step):
                    # The carried-over step starts the next pass's loop.
                    stopped_at = (start + offset) % len(steps)
                    break
            except RateLimited:
                raise
            except (GitHubError, OSError, ValueError) as err:
                _say("%s failed: %s" % (step.__name__, err))
        try:
            _write_json(rotation_path, {"next": stopped_at if stopped_at is not None else 0})
        except OSError as err:
            _say("could not record the step rotation: %s" % err)

    def _timed(self, step: Any) -> bool:
        """Run one pass step, logging it when it takes SLOW_STEP_SECONDS or
        more, with the requests it made (as the step loop does). False when
        a read the step needed was carried over at the pass request target:
        the line names the step, and the rest of it waits for the next pass."""
        started, asked = time.monotonic(), self.gh.requests
        try:
            step()
        except TargetReached as err:
            _say("step %s carried over to the next pass: %s" % (step.__name__, err))
            return False
        finally:
            took = time.monotonic() - started
            if took >= SLOW_STEP_SECONDS:
                _say("step %s took %ds and %d requests"
                     % (step.__name__, took, self.gh.requests - asked))
        return True

    def reconcile_pull_requests(self) -> None:
        """Repair ticket state from the PR's authoritative terminal state.

        A GitHub failure on one repo is logged and the pass moves on; the
        next pass retries, since the repair is idempotent."""
        for repo in self.scan_repos():
            try:
                # Both the issues and pulls fetches are covered by this repo guard.
                self._reconcile_repo(repo)
            except RateLimited:
                raise
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
            except RateLimited:
                raise
            except (GitHubError, OSError) as err:
                _say("could not adopt pull requests for %s: %s" % (repo, err))
            ledger[repo] = rec
        _write_json(path, {r: v for r, v in ledger.items() if r in self.s.repos})

    def _adopt_repo(self, repo: str, rec: Dict[str, Any]) -> None:
        pulls = self._adoptable_pulls(repo)
        # The open tickets are read only when a pull request may need one.
        open_tickets: List[Dict[str, Any]] = []
        if pulls:
            issues = self._paginate("/repos/%s/issues?labels=loom:ticket&state=open"
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
        # The open list was read once for the whole pass; a pull request
        # merged or closed since then needs no ticket. A failed read raises
        # GitHubError, which the caller logs, and the next pass tries again.
        fresh = self.gh.rest("GET", "/repos/%s/pulls/%d" % (repo, number)).data
        if not isinstance(fresh, dict) or fresh.get("state") != "open" or fresh.get("merged") \
                or fresh.get("merged_at"):
            rec["adopted"] = sorted(set(rec["adopted"]) | {number})
            _say("adoption %s#%d: closed or merged since the pass began, not adopted"
                 % (repo, number))
            return
        goal = self._adoption_goal(repo)
        if goal is None:
            self._adoption_refused(repo, rec, "no goal home to file adoption tickets on")
            return
        try:
            url = brain.brief_intake(self.gh, repo, goal, self._adoption_title(repo, pr),
                              adoption_body(number), difficulty="medium")
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
        self._comment(repo, number, adoption_comment(repo, int(ticket)), notify.RECORD)
        _say("adopted %s#%d as ticket %s#%s" % (repo, number, repo, ticket))

    def _adoptable_pulls(self, repo: str) -> List[Dict[str, Any]]:
        """The repo's open pull requests that are not drafts, come from a
        trusted author on a branch of the repo itself and are at least
        ADOPT_GRACE old, oldest first. One whose created_at is missing or
        unreadable counts as old enough, so it is never left unadopted.
        Whether an open ticket links one (a Refs line or a loom/N branch) is
        checked by the caller."""
        authors = {a.lower() for a in self.s.authors}
        found = [pr for pr in self._open_pulls(repo)
                 if isinstance(pr, dict) and pr.get("state", "open") == "open"
                 and not pr.get("draft") and tickets._login(pr.get("user")) in authors
                 and brain._same_repo(repo, pr, {}) and not self._owned_elsewhere(repo, pr)
                 and self.now - (self._epoch(str(pr.get("created_at") or "")) or 0)
                 >= ADOPT_GRACE]
        return sorted(found, key=lambda pr: (str(pr.get("created_at") or ""),
                                             int(pr.get("number") or 0)))

    def _marked_adopted(self, repo: str, number: int) -> bool:
        """A trusted author marked the pull request adopted."""
        authors = {a.lower() for a in self.s.authors}
        comments = self._paginate("/repos/%s/issues/%d/comments" % (repo, number))
        return any(isinstance(c, dict) and tickets._login(c.get("user")) in authors
                   and ADOPTED_RE.search(str(c.get("body") or "")) for c in comments)

    def _adoption_goal(self, repo: str) -> Optional[int]:
        """The goal home the repo's adoption tickets are filed under: its
        oldest open goal home that is not a backlog, else its backlog home;
        None when the repo has none."""
        homes = sorted((naming.is_backlog_home(i), int(i["number"]))
                       for r, i in self._goal_homes() if r == repo)
        return homes[0][1] if homes else None

    @staticmethod
    def _adoption_title(repo: str, pr: Dict[str, Any]) -> str:
        """"Land or supersede #N: <title>" clipped to naming.TITLE_MAX, or
        without the title when it is text a brief refuses (a closing keyword,
        another repo's issue)."""
        bare = ADOPT_TITLE % int(pr["number"])
        title = " ".join(str(pr.get("title") or "").split())
        if not title:
            return bare
        full = naming.generated(bare, title)
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
        """Every merged pull request of each wanted ticket, a revert not
        counting as one of the ticket's own."""
        groups: Dict[int, List[Dict[str, Any]]] = {}
        for pr in self._closed_pulls(repo):
            number = self._pr_ticket_number(repo, pr)
            if number in wanted and not revert.looks_like_revert(pr):
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
            except RateLimited:
                raise
            except (GitHubError, OSError, ValueError) as err:
                _say("unwind_reverts %s failed: %s" % (repo, err))

    def _unwind_repo(self, repo: str, allowed: set) -> None:
        landed: Dict[int, Dict[str, Any]] = {}
        for label in ("loom:merged", "loom:shipped"):
            data = self._paginate("/repos/%s/issues?labels=%s&state=open&per_page=100"
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
        comments = self._paginate("/repos/%s/issues/%d/comments" % (repo, number))
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
                      seen: Optional[List[Dict[str, Any]]] = None,
                      beyond: Optional[set] = None) -> Dict[int, Dict[str, Any]]:
        """Each wanted ticket's latest merged pull request, found by its Refs
        line or loom branch in the most recently updated closed pull requests
        (the URL reconciliation reads), stopping once every one is found, then
        for up to SHIP_PER_PASS tickets still missing, the one an adoption
        ticket names or one past the window (_ticket_pull_beyond_window): every
        ship rule reads this, and only the deploy rule looked those up, so 33
        adoption tickets in other repos never shipped (2026-10-20).
        Every merged pull request read is added to seen when it is given.
        With beyond given, only those tickets are looked up past the window:
        an open triage or queued issue mostly has no merged pull request, and
        looking each one up every pass would spend the API budget."""
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
        # A revert is never the pull request that ships a ticket, and a
        # ticket whose newest merged pull request is one stays merged
        # (a ticket shipped through its own revert, 2026-10-21).
        found = {n: pr for n, pr in found.items() if not revert.looks_like_revert(pr)}
        missing = wanted - set(found)
        self._add_beyond_window(repo, missing if beyond is None else missing & beyond,
                                found, seen)
        return found

    def _add_beyond_window(self, repo: str, missing: set, found: Dict[int, Dict[str, Any]],
                           seen: Optional[List[Dict[str, Any]]]) -> None:
        """Add the merged pull request of up to SHIP_PER_PASS missing tickets
        found past the closed pull window (_ticket_pull_beyond_window)."""
        for number in sorted(missing)[:SHIP_PER_PASS]:
            pr = self._ticket_pull_beyond_window(repo, number)
            if pr is not None and not revert.looks_like_revert(pr):
                found[number] = pr
                if seen is not None:
                    seen.append(pr)

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
        early = self._early_tickets(repo)
        if not merged and not early:
            return
        latest = self._merged_pulls(repo, merged | set(early), beyond=merged)
        groups = self._ticket_pulls(repo, set(latest))
        hands = self._hand_reverts(repo, self._closed_pulls(repo))
        carried = sorted((str(pr["merged_at"]), number, pr)
                         for number, pr in latest.items()
                         if self.ancestor(str(pr["merge_commit_sha"]), revision)
                         and not self._all_reverted(groups.get(number) or [pr], hands,
                                                    revision, self.ancestor))
        shipped = 0
        for _, number, pr in carried:
            if shipped >= SHIP_PER_PASS:
                break
            if number in early and not self._early_ships(repo, early[number], pr):
                continue
            how = "the station deployed revision %s at %s" % (revision[:12], self._utc(at))
            if self._ship_one(repo, number, pr, how, "deployed %s" % revision[:12]):
                shipped += 1
                if number in early:
                    self._drop_triage(repo, number, early[number])

    def _early_tickets(self, repo: str) -> Dict[int, Dict[str, Any]]:
        """The repo's open issues at a state before a pull request
        (EARLY_SHIP_STATES), by number: a pull request that names one can
        merge and deploy while the issue waits there, and only loom:pr
        tickets are reconciled from their pull request (loom#2025)."""
        found: Dict[int, Dict[str, Any]] = {}
        for label in EARLY_SHIP_STATES:
            for issue in self._paginate("/repos/%s/issues?labels=%s&state=open&per_page=100"
                                        % (repo, label)):
                if (isinstance(issue, dict) and "pull_request" not in issue
                        and isinstance(issue.get("number"), int)
                        and label in _label_names(issue)
                        and not SETTLE_LANDED_SET.intersection(_label_names(issue))):
                    found[int(issue["number"])] = issue
        return found

    def _early_ships(self, repo: str, issue: Dict[str, Any], pr: Dict[str, Any]) -> bool:
        """Whether a triage or queued issue ships with this deployed pull
        request: not another operator's, not a regression re-opened after it,
        and not sent back for work after it merged (a merge that left
        criteria unbuilt requeues the ticket, tickets.merged_before_requeue)."""
        names = _label_names(issue)
        if "loom:regression" in names or brain.owned_elsewhere(issue) is not None:
            return False
        events = self._paginate("/repos/%s/issues/%d/events" % (repo, int(issue["number"])))
        return not tickets.merged_before_requeue(pr.get("merged_at"), events)

    def _drop_triage(self, repo: str, number: int, issue: Dict[str, Any]) -> None:
        """Remove loom:triage from an issue that shipped: a move keeps it, and
        the rewrite step would then turn shipped work into a new ticket."""
        if "loom:triage" not in _label_names(issue):
            return
        try:
            self.gh.rest("DELETE", "/repos/%s/issues/%d/labels/%s"
                         % (repo, number, quote("loom:triage", safe="")), ok=(404,))
        except RateLimited:
            raise
        except GitHubError as err:
            _say("could not drop loom:triage from %s#%d: %s" % (repo, number, err))

    def _ticket_pull_beyond_window(self, repo: str, number: int) -> Optional[Dict[str, Any]]:
        """The merged pull request of a merged ticket that _merged_pulls did
        not find: the one a station-filed adoption ticket names
        (_adopted_pull_number), else one merged pull request that
        cross-references the ticket (_xref_pull_numbers) whose Refs line or
        loom branch names it. Neither path was read, so five adoption tickets
        and three tickets merged a day before never shipped while the station
        deployed their merges (2026-10-19). A ticket with no match is not
        looked up again for SHIP_LOOKUP_RETRY. None when there is none."""
        key = "%s#%d" % (repo, number)
        if self._lookup_recent(key):
            return None
        found: List[Dict[str, Any]] = []
        try:
            issue = self.gh.rest("GET", "/repos/%s/issues/%d" % (repo, number)).data
            adopted = self._adopted_pull_number(issue if isinstance(issue, dict) else {})
            numbers = [adopted] if adopted else self._xref_pull_numbers(repo, number)
            for pr_number in numbers:
                pr = self.gh.rest("GET", "/repos/%s/pulls/%d" % (repo, pr_number)).data
                if (isinstance(pr, dict) and pr.get("merged_at")
                        and COMMIT_RE.fullmatch(str(pr.get("merge_commit_sha") or ""))
                        and (adopted or self._pr_ticket_number(repo, pr) == number)):
                    found.append(pr)
            found = found or self._source_pulls(repo, issue)
        except RateLimited:
            raise
        except (GitHubError, ValueError, TypeError) as err:
            _say("could not find the merged pull request of %s: %s" % (key, err))
        if not found:
            self._lookup_failed(key)
            return None
        return max(found, key=lambda pr: str(pr["merged_at"]))

    def _source_pulls(self, repo: str, issue: Any) -> List[Dict[str, Any]]:
        """The merged pull requests of the issue a ticket was rewritten from
        (its "Rewritten from issue" line): a ticket rewritten from an issue
        ships with that issue's merged pull request (loom#1385)."""
        if not isinstance(issue, dict) or not tickets.rewritten_source(issue.get("body")):
            return []
        return [dict(pr) for pr in self._closed_pulls(repo)
                if _covers_pr(issue, repo, pr) and not _linked_pr(issue, repo, pr)]

    def _adopted_pull_number(self, issue: Dict[str, Any]) -> Optional[int]:
        """The pull request an adoption ticket names, when a trusted login
        filed it and its title and body (adoption_body) name the same #N: a
        title alone can be edited by anyone who can triage (security review)."""
        title = re.match(r"Land or supersede #([0-9]+)\b", str(issue.get("title") or ""))
        if (title is None
                or not tickets.brief_author_trusted(issue, self.s.authors, self.s.briefers)
                or "Pull request #%s is open in this repo" % title.group(1)
                not in str(issue.get("body") or "")):
            return None
        return int(title.group(1))

    def _xref_pull_numbers(self, repo: str, number: int) -> List[int]:
        """The newest SHIP_XREF_MAX merged pull requests of the repo that
        cross-reference the ticket, from the first page of its timeline."""
        events = self.gh.rest("GET", "/repos/%s/issues/%d/timeline?per_page=100"
                              % (repo, number)).data
        numbers = []
        for event in events if isinstance(events, list) else []:
            ref = ((event or {}).get("source") or {}).get("issue") or {}
            if (event.get("event") == "cross-referenced"
                    and (ref.get("pull_request") or {}).get("merged_at")
                    and str((ref.get("repository") or {}).get("full_name") or "") == repo):
                numbers.append(int(ref["number"]))
        return numbers[-SHIP_XREF_MAX:]

    def _lookup_recent(self, key: str) -> bool:
        tried = _read_json(os.path.join(self.state, SHIP_LOOKUPS_FILE), {}, strict=False)
        last = tried.get(key) if isinstance(tried, dict) else None
        return isinstance(last, (int, float)) and 0 <= self.now - last < SHIP_LOOKUP_RETRY

    def _lookup_failed(self, key: str) -> None:
        path = os.path.join(self.state, SHIP_LOOKUPS_FILE)
        tried = _read_json(path, {}, strict=False)
        tried = tried if isinstance(tried, dict) else {}
        tried[key] = self.now
        _write_json(path, {k: v for k, v in tried.items()
                           if isinstance(v, (int, float)) and 0 <= self.now - v < SHIP_LOOKUP_RETRY})

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
        key = "%s#%d" % (repo, number)
        try:
            brain.move(self.gh, repo, number, "shipped", note=note)
        except RateLimited:
            raise
        except (GitHubError, ValueError) as err:
            _say("could not ship %s#%d: %s" % (repo, number, err))
            self._deployed_seen(key, True)
            return False
        self._deployed_seen(key, False)
        _say("shipped %s#%d: %s carries #%d" % (repo, number, what, int(pr["number"])))
        return True

    def _deployed_seen(self, key: str, seen: bool) -> None:
        """Keep when a ship rule first found the ticket deployed
        (DEPLOYED_SEEN_FILE) until it moves; the merged-not-shipped
        invariant reads what is left (loom#2025)."""
        path = os.path.join(self.state, DEPLOYED_SEEN_FILE)
        saved = _read_json(path, {}, strict=False)
        ledger = {k: v for k, v in (saved.items() if isinstance(saved, dict) else ())
                  if isinstance(k, str) and self._num(v)}
        if seen == (key in ledger):
            return
        if seen:
            ledger[key] = self.now
        else:
            ledger.pop(key, None)
        _write_json(path, ledger)

    def _invariant_unshipped(self) -> List[Tuple[invariants.Ref, float]]:
        """Each ticket a ship rule found deployed that is still an open
        loom:merged ticket, with when it was first found deployed. A ticket
        that moved, closed or left loom:merged by other hands is dropped."""
        path = os.path.join(self.state, DEPLOYED_SEEN_FILE)
        saved = _read_json(path, {}, strict=False)
        ledger = {k: v for k, v in (saved.items() if isinstance(saved, dict) else ())
                  if isinstance(k, str) and self._num(v)}
        by_repo: Dict[str, List[Tuple[int, float]]] = {}
        for key, at in ledger.items():
            repo, _, number = key.rpartition("#")
            if tickets.REPO_RE.fullmatch(repo) and number.isdigit():
                by_repo.setdefault(repo, []).append((int(number), float(at)))
        kept: Dict[str, float] = {}
        found: List[Tuple[invariants.Ref, float]] = []
        for repo, items in sorted(by_repo.items()):
            merged = self._merged_tickets(repo)
            for number, at in sorted(items):
                if number in merged:
                    kept["%s#%d" % (repo, number)] = at
                    found.append((self._invariant_ref(repo, number), at))
        if kept != ledger:
            _write_json(path, kept)
        return found

    # -- shipping on merge or on a deploy workflow --------------------------
    def ship_merged(self) -> None:
        """Move the open merged tickets of each LOOM_SHIP_ON_MERGE repo to
        loom:shipped: nothing deploys those repos, so the merge is the ship.
        A pull request a later merged pull request reverted is skipped, as
        the pin step does."""
        for repo in [r for r in self.s.ship_on_merge if tickets.REPO_RE.fullmatch(r)]:
            try:
                merged = self._merged_tickets(repo)
                if not merged:
                    continue
                seen: List[Dict[str, Any]] = []
                latest = self._merged_pulls(repo, merged, seen)
            except GitHubError as err:
                _say("could not ship merged tickets of %s: %s" % (repo, err))
                continue
            branch = self._default_branch(repo)
            ordered = sorted((str(pr["merged_at"]), number, pr) for number, pr in latest.items())
            for _, number, pr in ordered[:SHIP_PER_PASS]:
                if branch and self._reverted(repo, pr, seen, branch):
                    _say("%s#%d stays merged: pull request #%d was reverted" % (
                        repo, number, int(pr["number"])))
                    continue
                self._ship_one(repo, number, pr,
                               "%s ships on merge (LOOM_SHIP_ON_MERGE); pull request #%d "
                               "merged at %s" % (repo, int(pr["number"]),
                                                 str(pr["merged_at"]).replace("T", " ")),
                               "merge")

    def ship_workflows(self) -> None:
        """Move the open merged tickets of each LOOM_SHIP_WORKFLOWS repo whose
        pull request's merge commit a successful run of the repo's deploy
        workflow ran on, or the newest successful run on the default branch
        carries, to loom:shipped."""
        for repo, workflow in sorted(self.s.ship_workflows.items()):
            if not tickets.REPO_RE.fullmatch(repo):
                continue
            try:
                self._ship_workflow(repo, workflow)
            except GitHubError as err:
                _say("could not ship %s on workflow %s: %s" % (repo, workflow, err))

    def _ship_workflow(self, repo: str, workflow: str) -> None:
        merged = self._merged_tickets(repo)
        branch = self._default_branch(repo) if merged else ""
        runs = self._deploy_runs(repo, workflow, branch) if branch else []
        if not runs:
            return
        by_sha = {str(r["head_sha"]): r for r in runs}
        newest = max(runs, key=lambda r: str(r.get("created_at") or ""))
        seen: List[Dict[str, Any]] = []
        latest = self._merged_pulls(repo, merged, seen)
        shipped = 0
        for _, number, pr in sorted((str(pr["merged_at"]), n, pr) for n, pr in latest.items()):
            if shipped >= SHIP_PER_PASS:
                return
            run = self._covering_run(repo, pr, by_sha, newest)
            if run is None or self._reverted(repo, pr, seen, str(run["head_sha"])):
                continue
            if self._ship_one(
                    repo, number, pr,
                    "workflow %s succeeded on %s revision %s (run %s)" % (
                        workflow, repo, str(run["head_sha"])[:12], run.get("id")),
                    "workflow %s" % workflow):
                shipped += 1

    def ship_path_workflows(self) -> None:
        """Move the open merged tickets of each LOOM_SHIP_PATH_WORKFLOWS repo
        to loom:shipped once every deploy workflow its pull request's files
        trigger (by the workflow's on.push.paths filter) has a successful push
        run carrying the merge commit; a pull request that changes no deployed
        path ships on merge."""
        for repo in sorted(r for r in self.s.ship_path_workflows if tickets.REPO_RE.fullmatch(r)):
            try:
                self._ship_by_paths(repo)
            except RateLimited:
                raise
            except GitHubError as err:
                _say("could not ship %s by deploy paths: %s" % (repo, err))

    def _ship_by_paths(self, repo: str) -> None:
        merged = self._merged_tickets(repo)
        branch = self._default_branch(repo) if merged else ""
        if not branch:
            return
        filters = self._workflow_filters(repo, branch)
        if filters is None:
            _say("could not ship %s by deploy paths: its workflow files could not all be read" % repo)
            return
        seen: List[Dict[str, Any]] = []
        latest = self._merged_pulls(repo, merged, seen)
        cache_path = os.path.join(self.state, SHIP_PATH_FILES)
        saved = _read_json(cache_path, {}, strict=False)
        known = saved if isinstance(saved, dict) else {}
        kept: Dict[str, Any] = {}
        manual_path = os.path.join(self.state, SHIP_PATH_MANUAL)
        noted = _read_json(manual_path, {}, strict=False)
        told = noted if isinstance(noted, dict) else {}
        listed = self._manual_listed_paths(repo)
        shipped = 0
        try:
            for _, number, pr in sorted((str(pr["merged_at"]), n, pr) for n, pr in latest.items()):
                if shipped >= SHIP_PER_PASS:
                    break
                key = "%s#%d" % (repo, int(pr["number"]))
                files = known.get(key)
                if not isinstance(files, list):
                    files = self._pull_paths(repo, int(pr["number"]))
                    if files is None:
                        continue
                kept[key] = files
                by_hand = ship_paths.manual_files(files, filters, listed)
                if by_hand:
                    self._note_by_hand(repo, number, pr, by_hand, told)
                    continue
                needed = sorted(w for w, pats in filters.items()
                                if any(ship_paths.matches(pats, f) for f in files))
                how, revision = self._path_cover(repo, pr, branch, needed)
                if not how or self._reverted(repo, pr, seen, revision):
                    continue
                if self._ship_one(repo, number, pr, how, "deploy paths"):
                    shipped += 1
        finally:
            _write_json(cache_path, {**{k: v for k, v in known.items() if k.split("#")[0] != repo}, **kept})
            _write_json(manual_path, told)

    def _manual_listed_paths(self, repo: str) -> List[str]:
        """The path patterns the repo's .loom/manual-paths lists as applied by
        hand, read once a pass; [] when the repo has no such file."""
        if repo not in self._manual_listed:
            text = self._contents_text(repo, ship_paths.MANUAL_PATHS_FILE)
            self._manual_listed[repo] = ship_paths.listed_manual(text) if text else []
        return self._manual_listed[repo]

    def _note_by_hand(self, repo: str, number: int, pr: Dict[str, Any],
                      files: List[str], told: Dict[str, Any]) -> None:
        """Leave a ticket at loom:merged because its pull request changed
        paths that are applied by hand, and name the manual step once on the
        ticket (kept in ship_path_manual.json, so a pass never repeats it)."""
        key = "%s#%d" % (repo, number)
        if told.get(key) == int(pr["number"]):
            return
        shown = ", ".join("`%s`" % f for f in files[:5]) + (
            " and %d more" % (len(files) - 5) if len(files) > 5 else "")
        _say("%s stays merged: pull request #%d changed paths applied by hand: %s" % (
            key, int(pr["number"]), ", ".join(files[:5])))
        body = ("Not shipped on merge: pull request #%d changed %s, which merging does not "
                "deploy (applied by hand: a Helm values file no deploy workflow covers, or a "
                "path listed in `%s`). The ticket stays at loom:merged until the manual step "
                "(for a chart, the helm upgrade) is run and the brain or an operator moves it "
                "to loom:shipped." % (int(pr["number"]), shown, ship_paths.MANUAL_PATHS_FILE))
        try:
            notify.post(self.gh, repo, number, body, notify.HELD)
        except RateLimited:
            raise
        except GitHubError as err:
            _say("could not note the manual step on %s: %s" % (key, err))
            return
        told[key] = int(pr["number"])

    def _workflow_filters(self, repo: str, branch: str) -> Optional[Dict[str, List[str]]]:
        """Workflow file name to its on.push.paths filter, for the repo's
        workflows on its default branch that have one, read once a pass.
        None when the listing or any workflow file cannot be read: with a
        filter missing, a pull request could look as if it touched no
        deployed path, so nothing in the repo ships this pass."""
        if repo not in self._path_filters:
            self._path_filters[repo] = ship_paths.read_filters(self.gh, repo, branch)
        return self._path_filters[repo]

    def _pull_paths(self, repo: str, number: int) -> Optional[List[str]]:
        """Every path a pull request changed, a renamed file's old path too;
        None when the list may be cut off (the files API stops at 3000), so a
        pull request is never shipped on a partial list."""
        files = [f for f in self._paginate("/repos/%s/pulls/%d/files" % (repo, number))
                 if isinstance(f, dict)]
        if len(files) >= PULL_FILES_MAX:
            _say("not shipping %s#%d by deploy paths: it changed %d+ files" % (
                repo, number, PULL_FILES_MAX))
            return None
        return sorted({str(f.get(k) or "") for f in files
                       for k in ("filename", "previous_filename") if f.get(k)})

    def _path_cover(self, repo: str, pr: Dict[str, Any], branch: str,
                    needed: List[str]) -> Tuple[str, str]:
        """(how it shipped, the revision to check reverts against), or ("", "")
        while a workflow the pull request's files trigger has not deployed it."""
        if not needed:
            return ("%s ships on merge (LOOM_SHIP_PATH_WORKFLOWS): pull request #%d changed no "
                    "deployed path" % (repo, int(pr["number"])), branch)
        covers = []
        for workflow in needed:
            if (repo, workflow) not in self._path_runs:
                runs = self._deploy_runs(repo, workflow, branch)
                self._path_runs[(repo, workflow)] = (
                    ({str(r["head_sha"]): r for r in runs},
                     max(runs, key=lambda r: str(r.get("created_at") or ""))) if runs else None)
            known = self._path_runs[(repo, workflow)]
            run = self._covering_run(repo, pr, known[0], known[1]) if known else None
            if run is None:
                return "", ""
            covers.append((workflow, run))
        workflow, run = max(covers, key=lambda c: str(c[1].get("created_at") or ""))
        return ("workflows %s succeeded on %s revisions (latest run %s)" % (
            ", ".join(w for w, _ in covers), repo, run.get("id")), str(run["head_sha"]))

    def _deploy_runs(self, repo: str, workflow: str, branch: str) -> List[Dict[str, Any]]:
        """The workflow's recent successful push runs on the repo's own
        default branch, read once (SHIP_RUNS_URL) and checked again here."""
        data = self.gh.rest("GET", SHIP_RUNS_URL % (repo, workflow, quote(branch, safe=""))).data
        listed = data.get("workflow_runs") if isinstance(data, dict) else None
        return [r for r in (listed if isinstance(listed, list) else [])
                if isinstance(r, dict) and self._deploy_run(repo, branch, r)]

    def _covering_run(self, repo: str, pr: Dict[str, Any], by_sha: Dict[str, Dict[str, Any]],
                      newest: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """The deploy run that covers the pull request's merge commit: one
        that ran on it, else the newest run when it started after the merge
        and its revision carries the commit; None when no run does."""
        sha = str(pr["merge_commit_sha"])
        if sha in by_sha:
            return by_sha[sha]
        if str(newest.get("created_at") or "") < str(pr["merged_at"]):
            return None
        return newest if self._carries(repo, sha, str(newest["head_sha"])) else None

    @staticmethod
    def _deploy_run(repo: str, branch: str, run: Dict[str, Any]) -> bool:
        """A successful push run on the repo's own default branch: the
        filters asked of GitHub, checked again on what came back."""
        head = run.get("head_repository") if isinstance(run.get("head_repository"), dict) else {}
        return (run.get("status") == "completed" and run.get("conclusion") == "success"
                and run.get("event") == "push" and run.get("head_branch") == branch
                and str(head.get("full_name") or "").lower() == repo.lower()
                and bool(COMMIT_RE.fullmatch(str(run.get("head_sha") or ""))))

    # -- shipping through a downstream roll -------------------------------
    def ship_rolls(self) -> None:
        """Move the open merged tickets of each watched repo that declares a
        roll record (ROLL_SIGNAL_FILE) to loom:shipped once the source commit
        the record names carries their pull request's merge commit, unless a
        later revert the commit also carries undid it (loom#2025)."""
        path = os.path.join(self.state, ROLL_SIGNALS_FILE)
        saved = _read_json(path, {}, strict=False)
        known = saved if isinstance(saved, dict) else {}
        kept: Dict[str, Any] = {}
        try:
            for repo in [r for r in self.s.repos if tickets.REPO_RE.fullmatch(r)]:
                try:
                    text = self._roll_signal(repo, known.get(repo), kept)
                    merged = self._merged_tickets(repo) if text else set()
                    if not merged:
                        continue
                    for deploy, where, key in parse_roll_signals(text or ""):
                        self._ship_roll(repo, merged, deploy, where, key)
                except RateLimited:
                    raise
                except (GitHubError, ValueError) as err:
                    _say("could not ship %s by its roll record: %s" % (repo, err))
        finally:
            _write_json(path, kept)

    def _roll_signal(self, repo: str, cached: Any, kept: Dict[str, Any]) -> Optional[str]:
        """The repo's ROLL_SIGNAL_FILE text, None when it has none: read
        again only ROLL_SIGNAL_EVERY after the last read."""
        if (isinstance(cached, dict) and self._num(cached.get("at"))
                and 0 <= self.now - cached["at"] < ROLL_SIGNAL_EVERY
                and isinstance(cached.get("text"), (str, type(None)))):
            kept[repo] = cached
            return cached["text"]
        text = self._contents_text(repo, ROLL_SIGNAL_FILE)
        kept[repo] = {"text": text, "at": self.now}
        return text

    def _ship_roll(self, repo: str, merged: set, deploy: str, path: str, key: str) -> None:
        record = self._contents_text(deploy, path)
        revision = rolled_commit(record or "", key)
        if not revision:
            _say("%s: the roll record names no %s commit" % (repo, key))
            return
        seen: List[Dict[str, Any]] = []
        latest = self._merged_pulls(repo, merged, seen)
        carried = sorted((str(pr["merged_at"]), number, pr) for number, pr in latest.items()
                         if self._carries(repo, str(pr["merge_commit_sha"]), revision)
                         and not self._reverted(repo, pr, seen, revision))
        for name in (deploy, repo):
            if name not in self._publics:
                self._publics[name] = self._public(name)
        # A ship note on a public ticket never names a private repo.
        where = deploy if (self._publics[deploy] or not self._publics[repo]) else "a private repo"
        how = "a roll record in %s names %s revision %s" % (where, repo, revision[:12])
        for _, number, pr in carried[:SHIP_PER_PASS]:
            self._ship_one(repo, number, pr, how, "roll %s" % revision[:12])

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
                    except RateLimited:
                        raise
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
        files = self._paginate("/repos/%s/pulls/%d/files" % (deploy, int(pin["number"])))
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
        a hand-made revert of it (its body says "Reverts <repo>#<n>", its
        title is 'Revert #<n>' or 'Revert "<title>"', or its body runs git
        revert on the pull request's merge commit, full or short), the
        forms GitHub's revert button and the revert pass write."""
        for other in seen:
            if other is pr or str(other.get("merged_at")) <= str(pr["merged_at"]):
                continue
            if revert.undoes(other, pr) \
                    and self._carries(repo, str(other["merge_commit_sha"]), revision):
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
        except RateLimited:
            raise
        except GitHubError as err:
            _say("factory release: could not read the closed incidents: %s" % err)
            return
        for key, info in list(held.items()):
            self._release_held(key, info, closed_incidents)

    def _closed_incidents(self) -> Dict[str, Dict[str, Any]]:
        """Closed factory incidents the runner filed, by "owner/repo#n".

        Incidents are found by the body marker, not the loom:factory label:
        the label may not exist in a repo (loom#918), so filtering on it
        loses every unlabelled incident. Closed issues are listed without the
        label and then matched on the marker."""
        closed: Dict[str, Dict[str, Any]] = {}
        for incident in self._paginate(
                "/repos/%s/issues?state=closed&per_page=100" % FACTORY_REPO):
            if isinstance(incident, dict) and INCIDENT_MARKER in str(incident.get("body") or ""):
                closed["%s#%d" % (FACTORY_REPO, incident.get("number"))] = incident
        return closed

    def _release_held(self, key: str, info: Any,
                      closed_incidents: Dict[str, Dict[str, Any]]) -> None:
        """Re-queue one held ticket: a closed and named incident, or a hold
        with no incident (the filing failed) that has outlived
        HELD_NO_INCIDENT_FOR. Forget it once it is no longer running."""
        repo, _, num = key.rpartition("#")
        number = int(num) if num.isdigit() else 0
        ref = info.get("incident") if isinstance(info, dict) else None
        raw_at = (info or {}).get("at")
        at = float(raw_at) if raw_at is not None else None
        try:
            issue = self.gh.rest("GET", "/repos/%s/issues/%d" % (repo, number)).data
            comments = self._paginate(
                "/repos/%s/issues/%d/comments?per_page=100" % (repo, number))
        except RateLimited:
            raise
        except GitHubError as err:
            _say("factory release: could not read %s#%d: %s" % (repo, number, err))
            return
        if not isinstance(issue, dict) or RUNNING not in _label_names(issue):
            self._forget_held(key)
            return
        if ref is None:
            # The incident filing failed: nothing names an incident that can
            # close this hold, so release it once it has outlived
            # HELD_NO_INCIDENT_FOR instead of holding it forever.
            green = False
            if at is not None and self.now - at < HELD_NO_INCIDENT_FOR:
                try:
                    green = self._green_default_after_hold(repo, at)
                except RateLimited:
                    raise
                except GitHubError as err:
                    _say("factory release: could not check default-branch CI for %s#%d: %s"
                         % (repo, number, err))
            if at is None or (self.now - at < HELD_NO_INCIDENT_FOR and not green):
                return
            cleared = RUNAWAY in _label_names(issue)
            reason = ("a newer green default-branch run passed" if green else
                      "the hold reached %d hours" % (HELD_NO_INCIDENT_FOR // 3600))
            self._release_one(key, repo, number, issue, cleared,
                              "held: incident filing failed; no incident was "
                              "filed, so the ticket is back on its lane because " + reason)
            _say("factory release: %s#%d re-queued: incident filing failed"
                 % (repo, number))
            return
        incident = closed_incidents.get(ref) if ref else None
        if incident is None or not any(isinstance(c, dict) and ref in str(c.get("body") or "")
                                       for c in comments):
            return
        if self._claimed_since(comments, incident):
            return
        cleared = RUNAWAY in _label_names(issue)
        self._release_one(key, repo, number, issue, cleared,
                          "Factory incident %s is closed: the ticket is back in the queue "
                          "on its lane%s." % (ref, ", and its loom:runaway hold is cleared"
                                              if cleared else ""))
        _say("factory release: %s#%d re-queued after incident %s closed" % (repo, number, ref))

    def _green_default_after_hold(self, repo: str, held_at: float) -> bool:
        """Whether a newer commit on the default branch has all checks green."""
        branch = self._default_branch(repo)
        if not branch:
            return False
        commit = self.gh.rest("GET", "/repos/%s/commits/%s" % (repo, quote(branch, safe=""))).data
        if not isinstance(commit, dict):
            return False
        sha = str(commit.get("sha") or "")
        committed = self._epoch(str(((commit.get("commit") or {}).get("committer") or {}).get("date") or ""))
        if not sha or committed is None or committed <= held_at:
            return False
        return self._default_commit_checks_green(repo, sha)

    def _default_commit_checks_green(self, repo: str, sha: str) -> bool:
        """Whether all check runs and the combined status for a commit passed."""
        checks = self.gh.rest("GET", "/repos/%s/commits/%s/check-runs?per_page=100"
                              % (repo, sha)).data or {}
        runs = checks.get("check_runs") if isinstance(checks, dict) else None
        if not isinstance(runs, list) or not runs or any(
                not isinstance(run, dict) or run.get("status") != "completed"
                or run.get("conclusion") != "success" for run in runs):
            return False
        status_data = self.gh.rest("GET", "/repos/%s/commits/%s/status" % (repo, sha)).data or {}
        return isinstance(status_data, dict) and status_data.get("state") == "success"

    def _release_one(self, key: str, repo: str, number: int, issue: Dict[str, Any],
                     cleared: bool, text: str) -> None:
        """Move a held running ticket to loom:queued, clear its runaway hold
        when the release cleared one, post the status event, and forget it."""
        self._relabel(repo, number, "loom:queued")
        if cleared:
            kept = [n for n in _label_names(self._issue(repo, number)) if n != RUNAWAY]
            self.gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number),
                         {"labels": kept})
        self._routine(repo, number, text)
        self._forget_held(key)

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
        self._remember_issues(repo, issues)
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

    def _elder_markings_with_accept(self, summary: str, sha: str) -> List[Tuple[str, List[str], List[str]]]:
        """Parse Elder review markings and their accept-removal lines.
        Returns list of (marking_kind, paths, accept_removal_lines) tuples.
        Each accept_removal_line is the exact '/grug accept-removal PATH SHA' line.
        If not found in the summary, builds it from the path and the given SHA."""
        text = str(summary or "")
        markings = []
        for m in _ELDER_MARKING_RE.finditer(text):
            kind = m.group(1)
            rest = m.group(2)
            paths = [p.strip("` ") for p in re.findall(r"`([^`]+)`", rest)]
            # Find accept-removal lines for these paths in the full text
            accept_lines = []
            for path in paths:
                found = False
                for am in _ACCEPT_REMOVAL_RE.finditer(text):
                    if am.group(1) == path:
                        accept_lines.append(f"/grug accept-removal {am.group(1)} {am.group(2)}")
                        found = True
                        break
                if not found:
                    accept_lines.append(f"/grug accept-removal {path} {sha}")
            markings.append((kind, paths, accept_lines))
        return markings

    def _is_removed_guarded_path_named(self, marking: Tuple[str, List[str]], ticket_body: str) -> bool:
        """True when a removed-guarded-path marking only names paths the ticket names."""
        kind, paths = marking
        if kind != "removed-guarded-path":
            return False
        return all(operator_hold._named(path, ticket_body) for path in paths)

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
        comments = self._paginate("/repos/%s/issues/%d/comments" % (repo, number))
        names = _label_names(issue)
        ready = self._ready_head(comments)
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
        # A degraded review result is the review bot's "not a verdict": a lane
        # cannot fix it, so it stays at loom:pr for the factory's re-review
        # (factory._review_again) instead of spending a fix round.
        kept = [(name, tail) for name, tail in failed
                if not (name == self.s.review_check and factory.degraded_review(tail, tail))]
        degraded = len(kept) != len(failed)
        failed = kept
        # Check for the special case: only the review check failed, and all
        # its blocking Elder markings are "removed-guarded-path" on paths the
        # ticket names for removal. In this case, ask the maintainer for
        # /grug accept-removal approval instead of a fix round.
        if (len(failed) == 1 and failed[0][0] == self.s.review_check):
            review_summary = failed[0][1]
            markings = self._elder_markings_with_accept(review_summary, sha)
            ticket_body = str(issue.get("body") or "")
            blocking_markings = [m for m in markings if m[0] != "reviewability"]
            if (blocking_markings and
                    all(self._is_removed_guarded_path_named((m[0], m[1]), ticket_body) for m in blocking_markings)):
                # Only removed-guarded-path on named paths: no fix round, no runaway,
                # ask maintainer for /grug accept-removal with exact lines
                accept_lines = []
                for m in blocking_markings:
                    accept_lines.extend(m[2])
                accept_block = "\n".join(accept_lines)
                paths = ", ".join(p for m in blocking_markings for p in m[1])
                ask = operator_hold.ask(number, [
                    f"it deletes or mostly replaces {paths}, which its ticket names for removal"
                ])
                ask += f"\n\n{accept_block}"
                brain.human(self.gh, repo, number, ask)
                _say("%s#%d: only removed-guarded-path on named paths; moved to loom:human for accept-removal"
                     % (repo, number))
                return
        # An infra timeout (bus/flake.py) is no lane's to fix: it is re-run
        # once before any round, whatever else fails, and never reads as a
        # stuck failure after (loom#1962). A cancelled job, a job that failed
        # before any repo code ran, and pytest reporting only timeout or
        # connection errors (no test failures) are the same shape. The station
        # re-runs each head commit's failed jobs once, records it on the
        # ticket's status comment and waits: a second red on the same head
        # counts as a fix round as usual.
        flaky = [name for name, tail in failed if name not in (
            self.s.review_check, self.s.plan_check) and flake.infra_flake(tail)]
        if flaky and self._rerun_flaky(repo, number, pr_number, sha, flaky, comments):
            self._remove_ready_if_present(repo, number, names)
            return
        self._note_first_push(repo, pr_number, comments, failed)
        threads = self._bot_threads(repo, pr_number)
        # A conflicting pull request is not ready, however green its checks
        # (loom#1963): status read READY for pull requests GitHub reported as
        # DIRTY after hand merges moved main. Its conflict brief is the
        # factory's (Factory._send_back), so no round is sent from here.
        conflict = detail.get("mergeable") is False
        # A diff that weakens its own tests is bounced before the review
        # round (bus/weakened.py, loom#2162): the miss is knowable from the
        # diff alone, so the lane hears it at once, naming the lines,
        # instead of after a full Elder round-trip. It rides the fix-round
        # machinery as a failed check of the station's own.
        weakened_lines = self._weakened_tests(repo, pr_number)
        if weakened_lines:
            self._remove_ready_if_present(repo, number, names)
            self._post_fix_or_escalate(repo, number, pr_number, lane, detail, sha,
                                       comments, names,
                                       [(WEAKENED_CHECK, "\n".join(weakened_lines))],
                                       [], rounds, flaky)
            return
        if not failed and not threads and not degraded and not conflict:
            # A lane pull request is ready only on its goal home's trunk (or
            # the repo's default branch when the goal home names no trunk).
            base_ref = str((detail.get("base") or {}).get("ref") or "")
            if base_ref:
                trunk = self._trunk_for(repo, issue)
                if trunk and base_ref != trunk:
                    self._remove_ready_if_present(repo, number, names)
                    self._routine(repo, number, "base %s is not the trunk %s"
                                  % (base_ref, trunk))
                    return
            # The repo's own CI must have run green on the head, but only in a
            # repo that has a pull-request-triggered workflow. When none ran,
            # the status says so and the pull request stays not ready.
            if self._has_pull_request_workflow(repo) \
                    and not self._repo_ci_green_on_head(repo, sha):
                self._remove_ready_if_present(repo, number, names)
                self._routine(repo, number, "repo CI did not run on head %s" % sha)
                return
            self._mark_ready(repo, number, sha, names, ready)
            return
        self._remove_ready_if_present(repo, number, names)
        if self._no_round(failed, threads):
            return
        self._post_fix_or_escalate(repo, number, pr_number, lane, detail, sha,
                                   comments, names, failed, threads, rounds, flaky)

    def _rerun_flaky(self, repo: str, number: int, pr_number: int, sha: str,
                     flaky: List[str], comments: List[Any]) -> bool:
        """Re-run the workflow runs of the infra-failed checks on this head
        once (flake.rerun_once), record it on the ticket's status comment and
        wait. Returns True when a re-run was started and the ticket should
        wait for it; False when it was already re-run on this head, no re-run
        is due, or GitHub refused the re-run: the fix round then goes as it
        did before this re-run existed (a second red on this head counts as
        usual)."""
        if self._already_re_ran(repo, sha):
            return False
        try:
            runs = brain.newest_check_runs((self.gh.rest(
                "GET", "/repos/%s/commits/%s/check-runs?per_page=100" % (repo, sha)).data
                or {}).get("check_runs") or [])
            done = flake.rerun_once(self.gh, repo, runs, flaky)
            if not done:
                return False
        except GitHubError as err:
            self._cannot_re_run(repo, err)
            return False
        self._record_re_run(repo, number, sha, flaky, comments)
        _say("%s#%d: re-ran %s on %s#%d once before a fix round: an infra failure"
             % (repo, number, ", ".join(sorted(flaky)), repo, pr_number))
        return True

    def _already_re_ran(self, repo: str, sha: str) -> bool:
        """Whether this Loom already re-ran the failed jobs of `sha` on this
        repo once (state/ci_reruns.json), so a second red on the same head
        counts as a fix round instead of another re-run."""
        record = _read_json(os.path.join(self.state, "ci_reruns.json"), {}, strict=False)
        return "%s#%s" % (repo, sha) in record

    def _record_re_run(self, repo: str, number: int, sha: str, flaky: List[str],
                       comments: List[Any]) -> None:
        """Remember the re-run (state/ci_reruns.json) and note it on the
        ticket's status comment so the next pass reads it back and waits."""
        record = _read_json(os.path.join(self.state, "ci_reruns.json"), {}, strict=False)
        record["%s#%s" % (repo, sha)] = self.now
        _write_json(os.path.join(self.state, "ci_reruns.json"), record)
        status.add(self.gh, repo, number,
                   "Re-ran %s once before a fix round (infra failure, head %s): "
                   "waiting on the re-run." % (", ".join(sorted(flaky)), sha[:12]),
                   self.now, self.s.authors)

    def _cannot_re_run(self, repo: str, err: GitHubError) -> None:
        """Say once per repo that the Loom App cannot re-run jobs because it
        lacks the actions: write permission, so the operator knows to install
        it (docs/LANES.md). Other refusals keep the earlier behaviour."""
        said = _read_json(os.path.join(self.state, "ci_rerun_refused.json"), {}, strict=False)
        if repo in said:
            return
        said[repo] = self.now
        _write_json(os.path.join(self.state, "ci_rerun_refused.json"), said)
        if getattr(err, "status", 0) in (401, 403):
            _say("%s: cannot re-run infra-failed jobs: the Loom App has no actions: "
                 "write permission (%s); a fix round goes as usual" % (repo, err))
        else:
            _say("%s: could not re-run infra-failed jobs (%s); a fix round goes as usual"
                 % (repo, err))

    def _no_round(self, failed: List[Tuple[str, str]], threads: List[Any]) -> bool:
        """Whether failed checks need no lane round because they read PR text."""
        if threads:
            return False
        if all(name == self.s.plan_check for name, _ in failed):
            return True
        return bool(failed) and all(
            factory.LEAK_CHECK_RE.search(name)
            and factory.body_leak_hits(tail.splitlines()) is not None
            for name, tail in failed)

    def _note_first_push(self, repo: str, pr_number: int, comments: List[Any],
                         failed: List[Tuple[str, str]]) -> None:
        """Record once whether a lane pull request was red in CI on its first
        push: its checks finished and no fix round was sent yet. The review
        bot's check is not CI. bus.brain status shows the rate per repo
        (bus/ci_gap.py)."""
        authors = {author.lower() for author in self.s.authors}
        if self._fix_round_count(comments, authors):
            return
        red = any(name != self.s.review_check for name, _ in failed)
        try:
            ci_gap.note_first_push(self.state, repo, pr_number, red, self.now)
        except OSError as err:
            _say("could not record the first push of %s#%d: %s" % (repo, pr_number, err))

    def _validation_gaps(self, repo: str, pr_number: int, detail: Dict[str, Any], sha: str,
                         failed: List[Tuple[str, str]]) -> str:
        """File a validation gap for each failing CI job whose command the
        repo's contract does not run (bus/ci_gap.py), and return the fix
        round's notes: run those commands before pushing. "" when there is
        no contract, no gap, or the reads fail (the fix round goes as before)."""
        names = {name for name, _ in failed if name != self.s.review_check}
        if not names:
            return ""
        base = str((detail.get("base") or {}).get("ref") or "")
        if not base:
            return ""
        try:
            fields = ci_gap.contract_fields(self.gh, repo, base)
            if fields is None:
                return ""
            runs = brain.newest_check_runs((self.gh.rest(
                "GET", "/repos/%s/commits/%s/check-runs?filter=all&per_page=100" % (repo, sha)).data
                or {}).get("check_runs") or [])
            failing = [run for run in runs if isinstance(run, dict)
                       and run.get("conclusion") in brain.FAILED_CONCLUSIONS
                       and str(run.get("name") or "") in names]
            gaps = ci_gap.find_gaps(self.gh, repo, pr_number, sha, fields, failing,
                                    lambda: self._pull_paths(repo, pr_number) or [], _say)
            filed = ci_gap.file_gaps(self.gh, self.state, gaps, self.now, _say)
        except RateLimited:
            raise
        except (GitHubError, ValueError, OSError, KeyError) as err:
            _say("validation gap: could not judge %s#%d: %s" % (repo, pr_number, err))
            return ""
        return ci_gap.fix_notes(filed)

    def _weakened_tests(self, repo: str, pr_number: int) -> List[str]:
        """The weakened-test lines in a lane pull request's diff
        (bus/weakened.py), read from the files GitHub lists for it, page
        by page while a page comes back full. A read that fails names
        none: the evaluation goes as it did before the pre-check, and the
        review remains the backstop."""
        try:
            entries = self.gh.rest("GET", "/repos/%s/pulls/%d/files?per_page=100"
                                   % (repo, pr_number)).data
            page = 2
            while isinstance(entries, list) and len(entries) % 100 == 0 and entries \
                    and page <= 10:
                more = self.gh.rest("GET", "/repos/%s/pulls/%d/files?per_page=100&page=%d"
                                    % (repo, pr_number, page)).data
                if not isinstance(more, list) or not more:
                    break
                entries = entries + more
                page += 1
        except (GitHubError, ValueError, TypeError, AssertionError) as err:
            _say("weakened-test pre-check: could not read %s#%d files (%s)"
                 % (repo, pr_number, err))
            return []
        if not isinstance(entries, list):
            return []
        return weakened.find([e for e in entries if isinstance(e, dict)])

    def _ready_head(self, comments: List[Any]) -> Optional[str]:
        """The head the newest ready mark of ours names. A mark is an event in
        the status comment (raw, or expanded by the GitHub client); a ticket
        marked before that keeps its own ready-head comment, read the same.
        Ours means the logins status.add writes as: the trusted authors and
        this client's own App login."""
        authors = {author.lower() for author in self.s.authors}
        own = getattr(self.gh, "_status_logins", None)
        if callable(own):
            authors |= {str(login).lower() for login in own()}
        marks: List[Tuple[str, str]] = []
        for comment in comments:
            if not isinstance(comment, dict) or tickets._login(comment.get("user")) not in authors:
                continue
            body = str(comment.get("body") or "")
            if status.is_status(body):
                events = status.parse(body)
            else:
                events = [(str(comment.get("created_at") or ""), body)]
            for at, text in events:
                match = READY_RE.match(text) or OLD_READY_RE.search(text)
                if match:
                    marks.append((at, match.group(1)))
        return max(marks, key=lambda m: m[0])[1] if marks else None

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
                           ready: Optional[str], sha: str) -> None:
        if "loom:ready" in names and ready != sha:
            self._remove_label(repo, number, "loom:ready")
            names.remove("loom:ready")

    def _checks_finished(self, repo: str, sha: str) -> bool:
        runs = (self.gh.rest("GET", "/repos/%s/commits/%s/check-runs?per_page=100" % (repo, sha)).data or {}).get("check_runs") or []
        statuses = (self.gh.rest("GET", "/repos/%s/commits/%s/status" % (repo, sha)).data or {}).get("statuses") or []
        return not any(run.get("status") != "completed" for run in runs) and not any(
            status.get("state") == "pending" for status in statuses)

    def _trunk_for(self, repo: str, issue: Dict[str, Any]) -> str:
        """The goal home's Trunk branch, else the repo's default branch, else
        "". An unreadable goal home or repo reads as no trunk, so a pull
        request whose base cannot be compared is not blocked here."""
        trunk = ""
        try:
            goal = brain._goal_home_for_ticket(self.gh, repo, issue)
            if isinstance(goal, dict):
                trunk = str(brain.goal_trunk(goal) or "")
        except (GitHubError, ValueError, TypeError):
            trunk = ""
        if not trunk:
            try:
                trunk = self._default_branch(repo)
            except (GitHubError, ValueError, TypeError):
                trunk = ""
        return trunk

    def _has_pull_request_workflow(self, repo: str) -> bool:
        """Whether the repo has a workflow that runs on pull_request: only then
        does the repo's own CI need to have run on a lane pull request's head
        for it to be ready. An unreadable workflow list reads as none."""
        try:
            listing = self.gh.rest("GET", "/repos/%s/contents/.github/workflows" % repo).data
        except (GitHubError, ValueError, TypeError, AssertionError):
            return False
        if not isinstance(listing, list):
            return False
        for item in listing:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "")
            if not path.endswith((".yml", ".yaml")):
                continue
            try:
                workflow = self._contents_text(repo, path)
            except (GitHubError, ValueError, TypeError, AssertionError):
                continue
            if workflow is not None and _has_pull_request_trigger(workflow):
                return True
        return False

    def _repo_ci_green_on_head(self, repo: str, sha: str) -> bool:
        """Whether at least one check from the repo's own workflows - not the
        review bot and not code scanning - concluded success on the head
        commit."""
        try:
            data = self.gh.rest("GET", "/repos/%s/commits/%s/check-runs?per_page=100"
                                % (repo, sha)).data or {}
        except (GitHubError, ValueError, TypeError, AssertionError):
            return False
        runs = data.get("check_runs") or []
        for run in runs:
            if not isinstance(run, dict):
                continue
            if str(run.get("conclusion") or "") != "success":
                continue
            name = str(run.get("name") or "")
            if not name or name == self.s.review_check:
                continue
            if _is_code_scanning_check(name, run):
                continue
            return True
        return False

    def _bot_threads(self, repo: str, pr_number: int) -> List[Dict[str, Any]]:
        threads = brain._open_threads(self.gh, repo, pr_number, bot=self.s.review_bot,
                                      authors=self.s.authors)
        return [thread for thread in threads if self._thread_starts_with_bot(thread)]

    def _thread_starts_with_bot(self, thread: Dict[str, Any]) -> bool:
        """Whether the review bot opened the thread. GitHub's GraphQL API names
        a bot without the "[bot]" suffix the REST API adds; matching only
        "grug-tribe[bot]" dropped every review-bot thread from fix rounds from
        2026-10-13 to 2026-10-24."""
        comments = (thread.get("comments") or {}).get("nodes") or []
        if not comments:
            return False
        # rest_login adds "[bot]" only for a Bot author, so a person who takes
        # the bot's bare name as a user login is never read as the review bot.
        login = rest_login(comments[0].get("author")).lower()
        bot = self.s.review_bot.strip().lower()
        return login == (bot if bot.endswith("[bot]") else bot + "[bot]")

    def _remove_ready_if_present(self, repo: str, number: int, names: List[str]) -> None:
        if "loom:ready" in names:
            self._remove_label(repo, number, "loom:ready")

    def _mark_ready(self, repo: str, number: int, sha: str, names: List[str],
                    ready: Optional[str]) -> None:
        if "loom:ready" not in names:
            self._add_label(repo, number, "loom:ready")
        # Routine, so an event in the status comment rather than a comment of
        # its own: a bare marker comment emailed a blank message (2026-10-20).
        if ready != sha:
            self._routine(repo, number, READY_TEXT % sha)

    @staticmethod
    def _fix_round_count(comments: List[Any], authors: set) -> int:
        """The fix rounds sent: each trusted loom:fix comment with a head
        marker is one round, whether or not the round moved the head. Counting
        distinct heads let a round that failed [transient] and pushed nothing
        repeat without end (grug#1315: 15 rounds, 9 on one head, 2026-10-24)."""
        return sum(1 for comment in comments
                   if isinstance(comment, dict)
                   and tickets._login(comment.get("user")) in authors
                   and Station._fix_head(str(comment.get("body") or "")))

    @staticmethod
    def _fix_head(body: str) -> Optional[str]:
        if not body.startswith(tickets.FIX_MARKER):
            return None
        match = re.search(r"<!-- loom:fix-head ([0-9a-f]+) -->", body)
        return match.group(1) if match else None

    def _post_fix_or_escalate(self, repo: str, number: int, pr_number: int, lane: str,
                              detail: Dict[str, Any], sha: str, comments: List[Any],
                              names: List[str], failed: List[Any], threads: List[Any],
                              rounds: int, flaky: Sequence[str] = ()) -> None:
        """Send the next fix round, unless the last one moved nothing. Each
        round went to the same lane until the cap, with no look at whether
        the last one fixed anything (2026-10-26: four rounds on a local model
        left the same checks failing and the same finding open). A round whose
        failures persist by signature (bus/converge.py) climbs the model
        ladder at once; with no rung left, or with the round cap reached on
        its rung, the rounds stop (_stop_fix_rounds). A check in `flaky`
        failed on the infrastructure (bus/flake.py) and is never read as a
        failure the last round left."""
        authors = {author.lower() for author in self.s.authors}
        failing = [name for name, _ in failed]
        persisted = converge.persistent(
            converge.last_fix_body(comments, authors, (repo, pr_number)),
            failing, threads, ignore=(self.s.review_check, self.s.plan_check) + tuple(flaky))
        if persisted:
            lane = self._climb_stuck(repo, number, pr_number, detail, comments, persisted)
            if lane is None:
                return
            names = _label_names(self._issue(repo, number))
        elif self._rung_round_count(comments, authors) >= rounds:
            self._stop_fix_rounds(repo, number, pr_number,
                                  "the fix round cap (%d) is reached on its rung" % rounds,
                                  converge.describe(failing, threads, (self.s.review_check,
                                                                       self.s.plan_check)))
            return
        self._post_fix_comment(repo, number, pr_number, detail, sha, failed, threads, comments)
        self._move_fix_to_lane(repo, number, lane, names)

    @staticmethod
    def _rung_round_count(comments: List[Any], authors: set) -> int:
        """The fix rounds sent since the ticket last moved on the model ladder
        (its newest rung marker): a climb gives the stronger model rounds of
        its own. Every round counts when it never climbed."""
        since = factory.last_climb(comments)
        return Station._fix_round_count(
            [c for c in comments if not since
             or (isinstance(c, dict) and factory.ts_of(c.get("created_at")) > since)], authors)

    def _climb_stuck(self, repo: str, number: int, pr_number: int, detail: Dict[str, Any],
                     comments: List[Any], persisted: List[str]) -> Optional[str]:
        """Move a ticket whose last fix round left the same failures up the
        model ladder (factory.Factory._climb, which keeps the operator's
        model and private-repo rules). The new lane, or None when it waits for
        a lane of its next rung or its rounds stopped with no rung left."""
        runner = self._factory()
        issue = self._issue(repo, number)
        found = converge.summary(persisted)
        footer = runner._footer(repo, detail)
        lane = runner._climb(repo, issue, comments, "the same failures came back after a fix "
                             "round: %s" % found, head="Stuck fix round", footer=footer)
        if lane is not None:
            _say("%s#%d: its fix round left the same failures (%s); next round on lane %s"
                 % (repo, number, found, lane))
            return lane
        if factory.RUNAWAY in _label_names(issue):
            return None
        if runner._rungs_left(repo, issue, comments, footer):
            _say("%s#%d: its fix round left the same failures (%s); waits for a lane of its "
                 "next rung" % (repo, number, found))
            return None
        self._stop_fix_rounds(repo, number, pr_number,
                              "the same failures came back and no rung of the model ladder "
                              "is left above it", persisted)
        return None

    def _stop_fix_rounds(self, repo: str, number: int, pr_number: int, why: str,
                         failures: List[str]) -> None:
        """No more fix rounds: the ticket goes to loom:failed with one note
        naming what still fails, and into the hand-off pool
        (labels.HANDOFF). Another round would repeat the last one."""
        note = FIX_STOP_TEXT % (pr_number, why, converge.summary(failures) or "see the last "
                                "fix round", labels.HANDOFF)
        try:
            brain._check_written_text(note)
        except ValueError as err:
            # The failure summary quotes check names and review text; when
            # that text fails the written-text check, keep the stop and say
            # so in the log instead of posting it.
            _say("%s#%d: stop note's failure summary refused (%s); using the plain note"
                 % (repo, number, err))
            note = FIX_STOP_TEXT % (pr_number, why, "the failures the last fix round names",
                                    labels.HANDOFF)
        brain.move(self.gh, repo, number, "failed", note=note)
        self._add_label(repo, number, labels.HANDOFF)
        _say("%s#%d: fix rounds stopped (%s); left failed for the hand-off pool"
             % (repo, number, why))

    def _post_fix_comment(self, repo: str, number: int, pr_number: int,
                          detail: Dict[str, Any], sha: str, failed: List[Any],
                          threads: List[Any], comments: Optional[List[Any]] = None) -> None:
        branch = str((detail.get("head") or {}).get("ref") or "")
        notes = self._validation_gaps(repo, pr_number, detail, sha, failed)
        body = brain._fix_body(repo, pr_number, branch, notes, failed, threads)
        marker = "\n\n<!-- loom:fix-head %s -->\n" % sha
        if len(body) > MAX_FIX_BODY:  # the API rejects comments over 65536 characters
            body = body[:MAX_FIX_BODY].rstrip() + "\n\n(findings truncated)"
        body = body.rstrip() + marker
        # A later round on the same pull request edits that one comment, so it
        # sends no email (loom#1640).
        fixrounds.post_round(self.gh, repo, number, body, self.s.authors,
                             fixrounds.ledger_path(self.s.home), comments)

    def _move_fix_to_lane(self, repo: str, number: int, lane: str,
                          names: List[str]) -> None:
        labels = self._drop_claim(name for name in names if name not in (
            "loom:pr", "loom:ready", "needs-brain", "loom:running", "loom:briefed")
                                  and not name.startswith("lane:"))
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
        if self._field(text, "Deliverable") == "review":
            return self._report_review(lane, kind, repo, number, name, text)
        self._revert_decision_ticks(repo, number, lane, text)
        if kind == "done":
            if self._field(text, "Deliverable") == "answer":
                _say("reported %s#%d answer posted by lane %s" % (repo, number, lane))
                return True
            if not self._field(text, "Fix-PR") and self._answered(lane, name):
                prs = {int(n) for n in NAMED_PR_RE.findall(text)}
                self._post_thread_answers(repo, prs, lane, name, text, None)
                self._settle_no_change(repo, number, lane, name, text)
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
            if self._field(text, "Fix-PR"):
                # A fix round's push is routine (the pull request already
                # exists and the lane comments on it): three of them emailed
                # one ticket's author on 2026-10-20.
                # Only a run that pushed a commit says so: a round that answered
                # threads or repaired text, or whose check was stale, pushed
                # nothing (zippie-style loop, 2026-10-19).
                if self._pushed(lane, name):
                    self._routine(repo, number, "Lane %s pushed fixes to a pull request: %s"
                                  % (lane, url))
                else:
                    self._routine(repo, number, "Lane %s made no code change for a fix to a "
                                  "pull request: %s" % (lane, url))
            else:
                # The pull request's "Refs #N" already cross-references the
                # ticket, which sends no email, and loom:pr is set above: a new
                # comment said it again, 55 a day (loom#1390).
                self._routine(repo, number, "Lane %s opened a pull request: %s" % (lane, url))
            _say("reported %s#%d done: %s" % (repo, number, url))
            return True
        if not self._report_failure(repo, number, lane, text, name):
            return False
        self._never_left_running(repo, number, lane)
        return True

    def _never_left_running(self, repo: str, number: int, lane: str) -> None:
        """A reported failure ends the run, so a ticket still loom:running on
        that lane goes to loom:failed, where the factory acts on it. A path
        that reported and forgot to move it left the ticket running with no
        brief: a runaway hold did, and four tickets sat 8 to 48 hours
        (2026-10-20). A factory hold (wait:incident) stays running by design."""
        names = _label_names(self._issue(repo, number))
        if (RUNNING not in names or "lane:" + lane not in names
                or WAIT + "incident" in names):
            return
        self._relabel(repo, number, "loom:failed")
        self._routine(repo, number, "Lane %s's run ended and nothing moved the ticket, so it "
                                    "is loom:failed for the factory to act on." % lane)
        _say("moved %s#%d to loom:failed: its reported run left it running" % (repo, number))

    def _report_failure(self, repo: str, number: int, lane: str, text: str, name: str) -> bool:
        """Report a failed brief, keeping fix rounds on their existing pull request."""
        reason = self._failure_reason(lane, name)
        fix_pr = self._field(text, "Fix-PR")
        klass, bare = failure_class(reason), CLASS_RE.sub("", reason)
        if fix_pr_merged(fix_pr, reason):
            self._fix_pr_merged(repo, number, lane, int(fix_pr))
            return True
        if klass == "transient" and self._timed_out_again(repo, number, reason):
            # One timeout is a slow provider; the run before it also timing
            # out is the ticket (too large, a hung test): counted as work, it
            # climbs the ladder and a third flags it (loom#1419).
            reason = "[work] %s; timed out again on this ticket, so it counts as a failure" % bare
            klass, bare = "work", CLASS_RE.sub("", reason)
        if self._flag_repeated_failure(repo, number, lane, reason):
            # The run is over, so the held ticket waits failed, as any work
            # failure does. Left loom:running on a lane with no brief, it read
            # as a live claim: the brain lifted the hold "until the live claim
            # resolves", and the ticket sat 8 hours with nothing running
            # (2026-10-20).
            self._relabel(repo, number, "loom:failed")
            return True
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
        if (klass == "factory" and lane in runners.runner_slots()
                and CHECK_CANNOT_RUN_RE.search(bare)):
            # Held behind the incident, the ticket went back to the same
            # runner, which can never clear it: another lane runs the check.
            note = self._retry(repo, number, lane, "transient", reason)
            if note:
                self._requeue_transient(repo, number, lane, reason, note)
            return True
        if klass == "factory":
            # The validation environment the factory broke, not the ticket's
            # work: the ticket stays running on its lane and keeps its rung.
            # release_factory_held re-queues it when the incident closes, and
            # releases a hold with no incident after HELD_NO_INCIDENT_FOR.
            match = HELD_FOR_RE.search(bare)
            incident_ref = "%s#%s" % (match.group(1), match.group(2)) if match else None
            self._note_held(repo, number, incident_ref)
            self._set_wait(repo, self._issue(repo, number), "incident")
            if incident_ref is None:
                self._routine(repo, number,
                              "Lane %s held: incident filing failed. %s. The ticket keeps its rung and "
                              "returns to the queue "
                              "after %d hours." % (lane, bare,
                                                   HELD_NO_INCIDENT_FOR // 3600))
            else:
                self._routine(repo, number,
                              "Lane %s held: %s. The ticket keeps its rung and returns to the "
                              "queue when the factory incident closes." % (lane, bare))
            _say("held %s#%d for the factory: %s" % (repo, number, reason))
            return True
        if klass == "auth":
            # The provider refused the key: no attempt of any lane can renew
            # it, so the ticket costs nothing and goes to a lane that still has
            # a working one (loom#744).
            self._unauthenticated(repo, number, fix_pr.isdigit(), lane, bare)
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
            self._routine(repo, number, "Lane %s failed: %s. This was a fix round on pull "
                          "request #%s; the ticket is back at %s and the pull request "
                          "is unchanged." % (lane, reason, fix_pr, state))
            _say("reported %s#%d fix failed, back to %s: %s" % (repo, number, state, reason))
            return True
        note = ""
        if klass in RETRY_CLASSES:
            note = self._retry(repo, number, lane, klass, reason)
            if not note:
                return True
            if klass == "limit":
                # A limit is not a failure even with no other lane to retry
                # on: loom:failed made the factory climb the ladder for it (a
                # provider limit put a ticket on rung 4 on 2026-10-18). It
                # waits briefed for a lane that serves it (loom#572, #271).
                self._limited(repo, number, False, lane, reason)
                return True
            # A transient ending did no work either: loom:failed made the
            # factory climb the ladder for it (a token-mint outage put six
            # tickets a rung up in 35 minutes, 2026-10-20; loom#1395).
            self._requeue_transient(repo, number, lane, reason, note)
            return True
        self._relabel(repo, number, "loom:failed")
        self._routine(repo, number, "Lane %s failed: %s%s" % (lane, reason, note))
        _say("reported %s#%d failed: %s" % (repo, number, reason))
        return True

    def _routine(self, repo: str, number: int, text: str) -> None:
        """A lane failure, factory hold or stop the factory acts on by itself,
        a fix round's push or a ready mark, as an event in the ticket's status
        comment: an edit sends no email.
        Each one was a new comment, an email apiece (an infra ticket,
        2026-10-19). The GitHub client expands status events, so the attempt
        table still reads each "Lane X failed:", "held:" and "stopped:" line
        as the ending of the claim before it (factory.attempts)."""
        status.add(self.gh, repo, number, text, self.now, self.s.authors)

    def _timed_out_again(self, repo: str, number: int, reason: str) -> bool:
        """Whether `reason`, the newest run's timeout, follows enough timeouts
        in a row on the ticket (factory.TIMEOUT_STRIKES) to count as a failure."""
        if not factory.is_timeout(reason):
            return False
        authors = {author.lower() for author in self.s.authors}
        comments = [c for c in self._paginate("/repos/%s/issues/%d/comments" % (repo, number))
                    if isinstance(c, dict) and tickets._login(c.get("user")).lower() in authors]
        # The newest claim is the run being reported: the ones before it ended.
        earlier = factory.attempts(comments, reason)[:-1]
        return factory.timeouts_in_a_row(earlier) + 1 >= factory.TIMEOUT_STRIKES

    def _flag_repeated_failure(self, repo: str, number: int, lane: str, reason: str) -> bool:
        """Hold a ticket after the same consecutive failure or three attempts in
        six hours, and record its attempt table."""
        if ending_class(reason) in NOT_WORK:
            # An incident is the factory's problem repeating, a transient or
            # limit exit is the lane's, and an expired key is the operator's:
            # none did the ticket's work, so none may flag it or cost it a rung
            # (loom#572, loom#744). Earlier claims that ended so are left out
            # by the ending each recorded: a failure, a retry, a hold or a
            # stop (factory.attempts).
            return False
        issue = self._issue(repo, number)
        if tickets.ticket_state(issue) in tickets.MERGED_STATES:
            # The ticket's work landed: whatever failed after that, the ticket
            # is not a runaway, and a hold would only cost the brain a read
            # each tick (loom#1178).
            return False
        authors = {author.lower() for author in self.s.authors}
        comments = [c for c in self._paginate("/repos/%s/issues/%d/comments" % (repo, number))
                    if isinstance(c, dict) and tickets._login(c.get("user")).lower() in authors]
        attempts = factory.attempts(comments, reason)
        def is_work(reason: str) -> bool:
            return not reason or failure_class(reason) not in NOT_WORK
        work = [a for a in attempts if is_work(a[3])]
        failures = [a[3] for a in work[:-1] if a[3]]
        # Timeouts name their kept branch, which differs each run: one reason.
        def same(a: str, b: str) -> bool:
            return a == b or (factory.is_timeout(a) and factory.is_timeout(b))
        reason_repeat = bool(failures) and same(failures[-1], reason)
        recent = [a for a in work if a[0] >= self.now - 6 * 3600]
        if not reason_repeat and len(recent) < 3:
            return False
        self.gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number),
                     {"labels": sorted(set(_label_names(issue)) | {RUNAWAY})})
        trigger = "repeated failure reason" if reason_repeat else "three attempts within six hours"
        body = factory.runaway_comment(
            trigger, attempts, factory.issue_model(issue), factory.attempt_models(issue, comments),
            counted=is_work)
        skipped = len(attempts) - len(work)
        if skipped:
            body += ("\n\nNot counted as work attempts: %d that ended [transient], [limit], "
                     "[auth] or [factory]." % skipped)
        self._comment(repo, number, body, notify.RUNAWAY)
        return True

    def _retry(self, repo: str, number: int, lane: str, klass: str, reason: str) -> str:
        """Brief a running ticket to another lane after a limit or transient
        failure. Its next claim is a new attempt, so a fresh branch. Returns ""
        when it moved, else why not."""
        issue = self._issue(repo, number)
        names = _label_names(issue)
        if RUNNING not in names:
            return "the ticket is no longer running"
        comments = self._paginate("/repos/%s/issues/%d/comments" % (repo, number))
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
            status.add(self.gh, repo, number, "Automatic retry %d of %d (%s): moved from lane %s "
                       "to lane %s. Lane %s failed: %s"
                       % (used + 1, self.s.retry_max, klass, lane, target, lane, reason),
                       self.now, self.s.authors)
        labels = self._drop_claim(n for n in names if n != RUNNING and not n.startswith("lane:"))
        labels += [BRIEFED, "lane:" + target]
        self.gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number), {"labels": labels})
        _say("retrying %s#%d on lane %s after a %s failure on lane %s"
             % (repo, number, target, klass, lane))
        return ""

    def _retry_lane(self, repo: str, issue: Dict[str, Any], failed: str, klass: str,
                    skip: set) -> Optional[str]:
        """The next eligible lane: RETRY_ORDER, then free lanes for an easy
        ticket, never a slot of the failed lane's provider. Its slots share the
        model, the key and often the box, so a sibling repeats the failure (a
        Spark that timed out, an expired opencode-go key; loom#747). Only available lanes whose runner takes the
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
        first: Optional[List[Dict[str, Any]]] = None
        busy = None
        for cand in dict.fromkeys(order):
            if cand in skip or not self._retry_eligible(repo, cand, failed, klass, model, public):
                continue
            if self.model_limited(cand, brief_model(cand, headers)) is not None:
                continue
            if first is None:
                first = self._first_waiting()
            if self._first_waits_for(first, cand):
                # A regression, revert or urgent ticket queued for this lane
                # never got it while retries kept taking it: one waited 2026-10-19
                # to 2026-10-21 and held its goal's regression slot (loom#1292).
                continue
            if self._inbox_empty(cand):
                return cand
            busy = busy or cand
        return busy

    def _first_waiting(self) -> List[Dict[str, Any]]:
        """Open tickets of the station's repos at loom:queued or loom:briefed
        with a FIRST_LABELS label: one list read per repo, made once per retry
        and only when a candidate lane is otherwise eligible."""
        found: List[Dict[str, Any]] = []
        for repo in self._verify_repos():
            for issue in self._tickets_open(repo):
                names = set(_label_names(issue))
                if names & set(tickets.FIRST_LABELS) and names & {"loom:queued", BRIEFED}:
                    found.append(issue)
        return found

    @staticmethod
    def _first_waits_for(first: List[Dict[str, Any]], lane: str) -> bool:
        """Whether a ticket in first is labelled for lane or pinned (Model:
        header) to a model lane serves."""
        for issue in first:
            if "lane:" + lane in _label_names(issue):
                return True
            model = re.search(r"^Model:\s*(\S+)", str(issue.get("body") or ""), re.M)
            if model and lane_takes_model(lane, model.group(1)):
                return True
        return False

    def _retry_order(self, failed: str, klass: str, difficulty: Optional[str]) -> List[str]:
        order = list(RETRY_ORDER)
        if difficulty == "easy":
            order += FREE_LANES
        return order

    def _retry_eligible(self, repo: str, cand: str, failed: str, klass: str, model: str,
                        public: Dict[str, bool]) -> bool:
        """Whether lane cand can take a retry: a known, available lane, not
        the failed lane's provider, whose runner takes the model and, for a
        public-only lane, a public repo. public caches the repo lookup across
        candidates."""
        if cand not in LANES or not self._heartbeat_ok(cand):
            return False
        if provider(cand) == provider(failed):
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
        except RateLimited:
            raise
        except GitHubError as err:
            _say("could not read whether %s is public: %s" % (repo, err))
            return False
        return isinstance(data, dict) and data.get("private") is False

    def _limited(self, repo: str, number: int, fix: bool, lane: str, reason: str) -> None:
        """A run the lane's provider refused (a limit, not a failure). The
        ticket goes back to briefed, where the factory moves it to the next
        lane; a fix brief's goes back to loom:pr, where the factory sends its
        pull request back again."""
        self._relabel(repo, number, "loom:pr" if fix else BRIEFED,
                      wait="" if fix else "model-limit")
        # No new comment: a limit is not news, and one comment a bounce
        # emailed the operator dozens of times an hour (2026-10-18). A status
        # event is an edit, and it ends the claim as [limit], so the runaway
        # detector does not count it as work.
        self._stopped(repo, number, lane, "limit", reason)
        _say("reported %s#%d limited on lane %s: %s" % (repo, number, lane, reason))

    def _requeue_transient(self, repo: str, number: int, lane: str, reason: str,
                           why_not: str) -> None:
        """A transient ending with no retry left (every automatic retry used,
        or no other lane to take it). The lane did no work on the ticket, so it
        is no failure and costs no rung: it goes back to loom:queued with
        wait:lane, its Model: line and rung markers untouched, and the factory
        gives it a lane again. A status event, not a comment, ends the claim as
        [transient] and names why it was not retried (loom#1395)."""
        names = _label_names(self._issue(repo, number))
        if RUNNING not in names:
            return
        labels = self._drop_claim(n for n in names if n not in (RUNNING, BRIEFED)
                                  and not n.startswith(("lane:", WAIT))
                                  ) + ["loom:queued", WAIT + "lane"]
        self.gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number), {"labels": labels})
        self._stopped(repo, number, lane, "transient",
                      "%s. Not retried: %s; the ticket is back in the queue on its rung."
                      % (CLASS_RE.sub("", reason), why_not))
        _say("requeued %s#%d after a transient ending on lane %s, not retried: %s"
             % (repo, number, lane, why_not))

    def _unauthenticated(self, repo: str, number: int, fix: bool, lane: str,
                         reason: str) -> None:
        """A run the provider refused the key for (not a failure, and no
        attempt spent). The lane runner has taken every lane that shares the key
        offline and opened the operator an ask naming the parameter, so the
        ticket goes back to briefed, where the factory moves it to a lane whose
        key works; a fix brief's goes back to loom:pr."""
        self._relabel(repo, number, "loom:pr" if fix else BRIEFED)
        # No new comment: the runner already opened the operator one ask for
        # the key. The status event ends the claim as [auth], not as work.
        self._stopped(repo, number, lane, "auth", reason)
        _say("reported %s#%d unauthenticated on lane %s: %s" % (repo, number, lane, reason))

    def _branch_moved(self, repo: str, number: int, pr: int, lane: str, reason: str) -> None:
        """A fix brief whose push was refused because the pull request's
        branch moved during the run. The model did nothing wrong, so no rung
        of the ladder is spent: the ticket returns to loom:pr, where the
        factory judges the new head and sends it back if it still needs a fix."""
        self._relabel(repo, number, "loom:pr")
        # No new comment: nothing failed, and the new head's review says the
        # rest. The status event ends the claim as [transient], not as work.
        self._stopped(repo, number, lane, "transient", reason)
        _say("reported %s#%d: branch moved on lane %s: %s" % (repo, number, lane, reason))

    def _stopped(self, repo: str, number: int, lane: str, klass: str, reason: str) -> None:
        """Record a claim that ended without doing the ticket's work as one
        routine "Lane X stopped: [class] ..." event (factory.STOPPED_RE).
        Never "Lane X failed:": the dashboard and the ladder count those as
        failures."""
        self._routine(repo, number,
                      "Lane %s stopped: [%s] %s" % (lane, klass, CLASS_RE.sub("", reason)))

    def _fix_blocks(self, repo: str, issue: Dict[str, Any],
                    fix: Optional[tickets.Fix]) -> bool:
        """Whether the ticket's fix round must not be briefed: its findings are
        refused (the ticket is refused, as an untrusted one is), or its pull
        request already merged."""
        refusal = self._fix_refusal(fix)
        if refusal:
            self._refuse(repo, issue, refusal)
            return True
        return self._fix_already_merged(repo, issue, fix)

    def _fix_already_merged(self, repo: str, issue: Dict[str, Any],
                            fix: Optional[tickets.Fix]) -> bool:
        """Whether the fix round's pull request merged after its loom:fix
        comment, so the round would only fix a closed pull request: three such
        rounds ran in two hours, one on a paid lane (2026-10-22). The ticket
        goes to loom:pr, where reconcile moves it on to loom:merged; one
        already merged or later keeps its labels."""
        if fix is None or not self._pull_merged(repo, fix.pr):
            return False
        number = int(issue["number"])
        if tickets.ticket_state(issue) not in tickets.MERGED_STATES:
            states = {"loom:" + state for state in brain.STATES}
            labels = [n for n in _label_names(issue)
                      if n not in states and not n.startswith(WAIT)] + ["loom:pr"]
            self.gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number), {"labels": labels})
            issue["labels"] = [{"name": n} for n in labels]
        _say("%s#%d: pull request #%d merged before its fix round; no fix brief"
             % (repo, number, fix.pr))
        return True

    def _pull_merged(self, repo: str, pr: int) -> bool:
        """Whether pull request `pr` has merged; a read that fails says no,
        so the round is briefed as before and the runner's own check stands."""
        try:
            data = self.gh.rest("GET", "/repos/%s/pulls/%d" % (repo, pr)).data
        except GitHubError:
            return False
        return isinstance(data, dict) and bool(data.get("merged_at"))

    def _fix_pr_merged(self, repo: str, number: int, lane: str, pr: int) -> None:
        """A fix brief the runner parked unrun because its pull request merged
        first. Nothing ran and the work landed, so no rung is spent and no
        hold is set. A running ticket goes back to loom:pr, where
        reconcile_pull_requests moves it on to loom:merged; one already merged
        or later keeps its labels."""
        if tickets.ticket_state(self._issue(repo, number)) not in tickets.MERGED_STATES:
            self._relabel(repo, number, "loom:pr")
        # A stop, never "failed:": the claim did no work, so it is not an
        # attempt toward a runaway (factory.attempts).
        self._stopped(repo, number, lane, "transient",
                      "pull request #%d merged before lane %s ran its fix round; nothing "
                      "ran, which is not a failure" % (pr, lane))
        _say("reported %s#%d: pull request #%d merged before lane %s's fix round"
             % (repo, number, pr, lane))

    def _post_answers(self, repo: str, pr: int, lane: str, name: str, brief: str) -> None:
        """Post each answer a fix round's lane wrote instead of a fix. Only a
        thread the brief asked about, on its pull request, gets one."""
        asked = set(factory.FIX_THREAD_RE.findall(brief))
        self._post_thread_answers(repo, {pr}, lane, name, brief, asked)

    def _pushed(self, lane: str, name: str) -> bool:
        """Did the runner push a commit for this run? Read from the runner's
        marker file (lanes/run_lane.sh), never from the log."""
        return os.path.isfile(os.path.join(self.s.home, "logs", lane, name + ".pushed"))

    def _answered(self, lane: str, name: str) -> bool:
        """Did the runner report this run answered: it committed nothing
        because its agent found the work already done or no longer applying
        (lanes/run_lane.sh, no_change_reason)? Read from the runner's marker
        file, never from the log, which carries the agent's own output."""
        return os.path.isfile(os.path.join(self.s.home, "logs", lane, name + ".answered"))

    def _no_change_said(self, lane: str, name: str) -> str:
        """The answer the runner wrote into the .answered marker: the lane's
        reason, quoted in full. It is lane output, so it passes the text
        checks again here, and a marker with nothing usable gives a plain
        sentence instead."""
        plain = ("Lane %s made no change: the agent found this work already done or no "
                 "longer applying." % lane)
        try:
            with open(os.path.join(self.s.home, "logs", lane, name + ".answered"),
                      encoding="utf-8", errors="replace") as fh:
                said = fh.read(32768).strip()
        except OSError:
            return plain
        if not said:
            return plain
        try:
            _text.check(said)
        except ValueError:
            return plain[:-1] + "; its reason did not pass the text checks and is in the lane log."
        return said

    def _revert_decision_ticks(self, repo: str, number: int, lane: str, brief: str) -> None:
        """Untick each decision box (bus/criteria_gate.py) that was unticked
        in the brief and is ticked on the ticket now that the lane reported,
        unless an operator made the last body edit: only an operator decides.
        One incident note on the ticket names the boxes. On 2026-10-21 a
        lane's no-change report ticked a box asking for an operator decision,
        and every later step read it as decided (loom#2029)."""
        issue = self._issue(repo, number)
        body = str(issue.get("body") or "")
        ticked = criteria_gate.decision_ticks(brief, body)
        if not ticked:
            return
        editor = (self._editor(repo, number) or "").lower()
        ops = answer.operators(trusted=list(self.s.authors) + list(self.s.briefers))
        if editor and not editor.endswith("[bot]") and editor in ops:
            return
        fixed = criteria_gate.untick(body, ticked)
        self.gh.rest("PATCH", "/repos/%s/issues/%d" % (repo, number), {"body": fixed})
        issue["body"] = fixed
        notify.post(self.gh, repo, number, "%s\nIncident: a decision box was ticked while lane "
                    "%s ran on this ticket, and only an operator ticks one. Loom unticked it: "
                    "%s. Tick it yourself once you have decided."
                    % (DECISION_REVERT_MARKER, lane,
                       "; ".join(" ".join(t.split())[:criteria_gate.ASK_WIDTH] for t in ticked)),
                    notify.FAILURE)
        _say("%s#%d: unticked %d decision box(es) ticked during lane %s's run"
             % (repo, number, len(ticked), lane))

    def _settle_no_change(self, repo: str, number: int, lane: str, name: str,
                          brief: str = "") -> None:
        """Record a build run's no-change answer and settle its ticket from it.

        The answer is an event in the ticket's status comment, quoting the
        lane in full: the runner's new comment emailed the ticket's author for
        what is a routine outcome (a watched repo's ticket, 2026-10-21). When a
        pull request linked to the ticket has merged into the default branch,
        the work the lane found done is that pull request's: the ticket moves
        to loom:merged, so the ship rules move it to loom:shipped and a verify
        run checks its acceptance criteria against live evidence, closing it
        with a proof or marking what evidence is missing. Without one, it
        waits at loom:answered for the brain, as before. So does a regression
        or revert ticket: the pull request linked to it is the change that
        regressed or must be undone, not this ticket's work. A ticket no
        longer running (moved while the station was away) keeps its labels."""
        said = self._no_change_said(lane, name)
        issue = self._issue(repo, number)
        names = _label_names(issue)
        if self._unproven_no_change(repo, number, issue, said):
            return
        undoes = bool(set(names) & {REGRESSION, factory.REVERT_LABEL})
        merged = self._merged_linked(repo, issue) if (
            issue.get("state") == "open" and RUNNING in names and not undoes) else []
        decide = criteria_gate.open_decisions(str(issue.get("body") or ""))
        if merged and decide:
            # The merged work does not settle a decision only an operator
            # makes: the ticket waits on a person, not on ship and verify.
            self._relabel(repo, number, "loom:answered")
            self._routine(repo, number, "%s\n\nPull request #%d already carries this "
                          "ticket's work, but the ticket has a decision box only an "
                          "operator ticks, so it waits on a person, not loom:merged."
                          % (said, int(merged[0]["number"])))
            if HUMAN not in _label_names(self._issue(repo, number)):
                brain.human(self.gh, repo, number, DECISION_ASK % "; ".join(
                    " ".join(t.split())[:criteria_gate.ASK_WIDTH] for t in decide))
            return
        if merged:
            pr = merged[0]
            states = {"loom:" + s for s in brain.STATES}
            labels = [n for n in names if n not in states and not n.startswith(WAIT)]
            labels.append("loom:merged")
            self.gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number), {"labels": labels})
            note = ("Pull request #%d (merged %s, merge commit %s) already carries this "
                    "ticket's work, so the ticket moved to loom:merged: it ships and is verified "
                    "against its acceptance criteria like any merged ticket."
                    % (int(pr["number"]), pr.get("merged_at"), str(pr["merge_commit_sha"])[:12]))
        else:
            self._relabel(repo, number, "loom:answered")
            note = ("%s, so it waits at loom:answered for the brain to verify it or brief it "
                    "again." % ("This is a regression or revert ticket" if undoes else
                                "No merged pull request references this ticket"))
        self._routine(repo, number, "%s\n\n%s" % (said, note))

    def _unproven_no_change(self, repo: str, number: int, issue: Dict[str, Any],
                            said: str) -> bool:
        """Send a running ticket back to loom:queued, naming each box, when a
        no-change answer leaves a code acceptance box unproven (neither the
        ticket nor the answer's checklist ticks it). On 2026-10-17 a lane's
        no-op answer settled a ticket whose third box still failed, and it
        went on to loom:merged (loom#2032). True when it requeued."""
        if issue.get("state") != "open" or RUNNING not in _label_names(issue):
            return False
        left = factory.unproven_criteria(str(issue.get("body") or ""), said)
        if not left:
            return False
        self._relabel(repo, number, "loom:queued")
        self._routine(repo, number, "%s\n\n%s" % (said, NO_CHANGE_UNPROVEN_NOTE % "; ".join(
            " ".join(text.split())[:criteria_gate.ASK_WIDTH] for text in left)))
        _say("%s#%d: a no-change answer left %d box(es) unproven, requeued"
             % (repo, number, len(left)))
        return True

    def _merged_cited(self, repo: str, issue: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Merged pull requests the Fabric workspace cites as already doing
        the ticket's work, when nothing links one (loom#1437): work often
        merges under another ticket or none. Each citation must name a pull
        request of this repo, merged into the default branch, whose title is
        the cited card's; the verify run then judges the ticket against its
        criteria. Asked once per ticket body, at most FABRIC_ASKS a pass;
        Fabric unset, down or silent leaves the ticket to build."""
        if not self._fabric_may_ask(repo, issue):
            return []
        body = str(issue.get("body") or "")
        criteria = "\n".join("- %s" % text for _, text in proof.criteria(body)) or body[:1500]
        answer = fabric.ask(FABRIC_QUESTION % (repo, issue.get("title") or "", criteria),
                            self.s.fabric_key_ssm, self.s.region)
        found = self._checked_citations(repo, fabric.cited_pulls(answer)[:FABRIC_CITES])
        if found:
            _say("%s#%d: Fabric cites merged pull request #%d for its work"
                 % (repo, int(issue["number"]), int(found[0]["number"])))
        return found

    def _fabric_may_ask(self, repo: str, issue: Dict[str, Any]) -> bool:
        """Whether this pass may ask Fabric about the ticket: a key is set, the
        pass has a question left, and the ticket's body was not asked about
        before. A True answer records the ask."""
        if not self.s.fabric_key_ssm or self._fabric_asks >= FABRIC_ASKS:
            return False
        path = os.path.join(self.state, FABRIC_ASKED_FILE)
        asked = _read_json(path, {}, strict=False)
        if not isinstance(asked, dict):
            asked = {}
        key = "%s#%d" % (repo, int(issue["number"]))
        body = status._SECTION_RE.sub("", str(issue.get("body") or ""))
        stamp = hashlib.sha256(body.strip().encode("utf-8")).hexdigest()[:16]
        if asked.get(key) == stamp:
            return False
        today = datetime.fromtimestamp(self.now, timezone.utc).strftime("%Y-%m-%d")
        day = asked.get("_day") if isinstance(asked.get("_day"), dict) else {}
        count = int(day.get("count") or 0) if day.get("date") == today else 0
        if count >= FABRIC_ASKS_PER_DAY:
            return False
        self._fabric_asks += 1
        asked[key] = stamp
        asked["_day"] = {"date": today, "count": count + 1}
        _write_json(path, asked)
        return True

    def _checked_citations(self, repo: str,
                           cited: List[Tuple[int, str]]) -> List[Dict[str, Any]]:
        """The cited pull requests that are of this repo, merged into the
        default branch, and titled as their cited cards; one read each."""
        branch = self._default_branch(repo)
        found = []
        for number, title in cited:
            try:
                pr = self.gh.rest("GET", "/repos/%s/pulls/%d" % (repo, number)).data
            except GitHubError:
                continue
            if (isinstance(pr, dict) and pr.get("merged_at") and pr.get("merge_commit_sha")
                    and str((pr.get("base") or {}).get("ref") or "") == branch
                    and fabric.same_title(title, pr.get("title") or "")):
                found.append(pr)
        return found

    def _merged_linked(self, repo: str, issue: Dict[str, Any]) -> List[Dict[str, Any]]:
        """The ticket's pull requests merged into the default branch, linked
        the way reconcile links them (a Refs line or a loom/<number> branch),
        newest merge first, from the closed pull requests cached for the pass."""
        branch = self._default_branch(repo)
        return sorted((pr for pr in self._closed_pulls(repo) if _covers_pr(issue, repo, pr)
                       and str((pr.get("base") or {}).get("ref") or branch) == branch),
                      key=lambda pr: str(pr.get("merged_at") or ""), reverse=True)

    def _post_thread_answers(self, repo: str, prs: Set[int], lane: str, name: str,
                             brief: str, asked: Optional[Set[str]]) -> None:
        """Post each answer the lane wrote as the reply on its thread: an open
        thread on one of prs and, unless asked is None, one the brief asked
        about. It is resolved when the round ran on a ladder model, the
        thread is the review bot's own medium or low finding, and the answer
        holds on the head (bus/rebuttal.py); otherwise it stays open, and the
        factory reads it as a finding that survived."""
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
                except RateLimited:
                    raise
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
        body = "Lane %s %s\n\n%s" % (
            lane, factory.ANSWER_REPLY, "\n".join("> " + line for line in said.splitlines()))
        try:
            _text.check(body)
        except ValueError as err:
            _say("answer to %s on %s#%d not posted: %s" % (thread_id, repo, pr, err))
            return
        self.gh.graphql(REPLY_MUTATION, {"id": thread_id, "body": body})
        root = ((node.get("comments") or {}).get("nodes") or [{}])[0] or {}
        settled = (strong and _bot_minor(root, self.s.review_bot)
                   and self._answer_holds(repo, pr, lane, thread_id, node, said))
        if settled:
            try:
                self.gh.graphql(RESOLVE_MUTATION, {"id": thread_id})
            except RateLimited:
                raise
            except GitHubError as err:
                # The reply is posted; an open thread is read by the factory
                # as a finding that survived, which is the safe side.
                settled = False
                _say("could not resolve %s on %s#%d: %s" % (thread_id, repo, pr, err))
        _say("posted lane %s's answer on %s#%d thread %s%s" % (
            lane, repo, pr, thread_id, " and resolved it" if settled else ""))

    def _answer_holds(self, repo: str, pr: int, lane: str, thread_id: str,
                      node: Dict[str, Any], said: str) -> bool:
        """The answer settles its thread only when what it cites holds on
        the head (bus/rebuttal.py, loom#2081): a false "it has a caller"
        answer once merged dead code."""
        root = ((node.get("comments") or {}).get("nodes") or [{}])[0] or {}
        head = str((node.get("pullRequest") or {}).get("headRefOid") or "")
        why = rebuttal.check_on_head(self.gh, repo, head, said,
                                     str(node.get("path") or ""), str(root.get("body") or ""))
        if why:
            _say("left %s on %s#%d open: lane %s's answer does not hold on the head: %s"
                 % (thread_id, repo, pr, lane, why))
        return not why

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
        self._comment(repo, pr, body, notify.NOTE)
        self._comment(repo, pr, factory.review_again(), notify.REVIEW_REQUEST)
        self._relabel(repo, number, "loom:pr")
        self._comment(repo, number, "Lane %s made no change for the fix to #%d and said why "
                      "on the pull request; the review bot was asked to review it again."
                      % (lane, pr), notify.NOTE)
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

    def _relabel(self, repo: str, number: int, new: str, wait: str = "") -> None:
        """Move a running ticket to `new`, with wait:<wait> as its only wait
        label when given, in one labels write."""
        names = _label_names(self._issue(repo, number))
        if RUNNING not in names:
            return
        labels = self._drop_claim(n for n in names if n != RUNNING and not n.startswith(WAIT))
        if new not in labels:
            labels.append(new)
        if wait:
            labels.append(WAIT + wait)
        self.gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number),
                     {"labels": labels})

    def _set_wait(self, repo: str, issue: Dict[str, Any], reason: str) -> None:
        """Make wait:<reason> the ticket's only wait label, from the labels
        already read; no write when it already is (loom labels, not comments:
        a comment emails the operator, a label does not)."""
        names = _label_names(issue)
        want = [n for n in names if not n.startswith(WAIT)] + [WAIT + reason]
        if set(want) == set(names):
            return
        self.gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, int(issue["number"])),
                     {"labels": want})
        issue["labels"] = [{"name": n} for n in want]

    def _comment(self, repo: str, number: int, body: str, kind: str) -> None:
        """A new comment: an outcome or record of one of notify.KINDS. A routine
        event is `_routine`, an edit of the status comment."""
        notify.post(self.gh, repo, number, body, kind)

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
        disk = self._disk_status_line()
        body = "\n".join(
            [LANES_MARKER, "Updated " + self._utc(self.now), ""]
            + ([disk, ""] if disk else [])
            + ["| lane | available | heartbeat (UTC) |", "|---|---|---|"]
            + ["| %s | %s | %s |" % row for row in rows]
            + ["", "Remote claims: muse %d/%d" % (
                self._remote_claim_count("muse"), REMOTE_LANES["muse"]["slots"])]
            + self._scorecard_lines()
            + (["", summary.line()] if summary is not None else []))
        return body

    def _scorecard_lines(self) -> List[str]:
        """The lane scorecard's table, for the lane status comment the
        dashboard's Lanes page reads."""
        return scorecard.Scorecard(self.state, self.now).markdown()

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

    def _read_remote_claim_count(self, lane: str) -> Tuple[int, int]:
        """(claimed tickets, repos read) across the station's repo list.

        A repo GitHub reports as unreadable (moved, gone, or not granted to
        the token) is skipped with one log line, so one unreadable repo no
        longer stops the whole pass. Any other GitHub failure still raises.
        """
        count = 0
        readable = 0
        for repo in self.s.repos:
            try:
                found = self.gh.rest("GET", "/repos/%s/issues?state=open&labels=loom:running,lane:%s"
                                     % (repo, lane)).data
            except GitHubError as err:
                why = unreadable(err)
                if why is None:
                    raise
                _say("remote claims: skipped %s: %s" % (repo, why))
                continue
            readable += 1
            if isinstance(found, list):
                count += sum(1 for item in found if isinstance(item, dict)
                             and "pull_request" not in item)
        return count, readable

    def _upsert_lanes_comment(self, repo: str, number: int, body: str) -> Optional[int]:
        for c in self._paginate("/repos/%s/issues/%d/comments" % (repo, number)):
            if isinstance(c, dict) and str(c.get("body") or "").startswith(LANES_MARKER):
                self.gh.rest("PATCH", "/repos/%s/issues/comments/%d" % (repo, int(c["id"])),
                             {"body": body})
                return int(c["id"])
        made = notify.post(self.gh, repo, number, body, notify.RECORD).data
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
        # A changed scorecard (bus/scorecard.py) republishes too.
        values["scorecard"] = hashlib.sha256("\n".join(
            self._scorecard_lines()).encode("utf-8")).hexdigest()[:16]
        # Claims paused or resumed for disk republishes (loom#1808).
        values["disk"] = "paused" if self._disk_status_line() else "ok"
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
        events = self._paginate("/repos/%s/issues/%d/events" % (repo, int(issue["number"])))
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
                items = self._paginate(
                    "/repos/%s/issues?labels=%s&state=open&per_page=100" % (repo, HUMAN))
            except RateLimited:
                raise
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
        comments = self._paginate("/repos/%s/issues/%d/comments?per_page=100"
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
                except RateLimited:
                    raise
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
            except RateLimited:
                raise
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
            except RateLimited:
                raise
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
        # Asks ranked by their urgency score (act mode) keep that order.
        ranked = self._digest_ranked
        new = [a for a in (asks if ranked else reversed(asks))
               if prev.get(a[0]) != covered[a[0]]]
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
        epic_goals: Optional[Dict[int, int]] = None
        groups: Dict[Optional[int], List[Dict[str, Any]]] = {}
        for issue in issues:
            refs = [int(n) for n in ASK_GOAL_REF_RE.findall(str(issue.get("body") or ""))]
            goal = next((n for n in refs if n in goals), None)
            if goal is None and refs:
                # An ask may name an epic: it goes under that epic's goal.
                if epic_goals is None:
                    epic_goals = self._epic_goals(repo)
                goal = next((epic_goals[n] for n in refs
                             if epic_goals.get(n) in goals), None)
            groups.setdefault(goal, []).append(issue)
        out = [DIGEST_MARKER, "Asks waiting for the operator on %s: %d in this repo."
               % (today, len(issues))]
        for goal in sorted(groups, key=lambda g: (g is None, g or 0)):
            if goal is None:
                head = "Not filed under a goal"
            else:
                # The heading names the goal, so its title drops a kind word.
                head = "%s (#%d)" % (naming.strip_kind(goals[goal].get("title")), goal)
            out += ["", "## " + head.replace("@", "@\u200b"), ""]
            out += ["- #%d %s" % (int(i["number"]),
                                  " ".join(str(i.get("title") or "").split()).replace("@", "@\u200b"))
                    for i in groups[goal]]
        return "\n".join(out)

    def _epic_goals(self, repo: str) -> Dict[int, int]:
        """epic number -> the goal its Part of line or sub-issue parent names,
        for the repo's epics (one conditional list read)."""
        found: Dict[int, int] = {}
        for item in self._paginate("/repos/%s/issues?labels=epic&state=all&per_page=100" % repo):
            if isinstance(item, dict) and "pull_request" not in item \
                    and str(item.get("number") or "").isdigit():
                goal = brain._issue_goal(item)
                if goal is not None:
                    found[int(item["number"])] = goal
        return found

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
        found = self._paginate("/repos/%s/issues?labels=%s&state=open&per_page=100"
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
                made = notify.post(self.gh, repo, number, page["body"], notify.DIGEST).data
            except RateLimited:
                raise
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
        except RateLimited:
            raise
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
        routes = self._route_asks(keyed)
        urgent_keys = {k for k, _, i in keyed
                       if routes[k].route == urgency.URGENT and k not in ledger[2]}
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
            if self.s.urgency.mode == urgency.ACT:
                # Act: only urgent and digest routes go in, highest score first.
                keyed = [a for a in keyed
                         if routes[a[0]].route in (urgency.URGENT, urgency.DIGEST)]
                keyed.sort(key=lambda a: -(routes[a[0]].score or 0.0))
                self._digest_ranked = True
            self._send_digest(url, keyed, ledger, today)

    def _urgency_client(self) -> Optional[urgency.Client]:
        if self._urgency_injected is not None:
            return self._urgency_injected
        if not self.s.urgency.key_ssm:
            return None
        key = self.read_secret(self.s.urgency.key_ssm, self.s.region)
        return urgency.http_client(key, self.s.urgency.model) if key else None

    def _label_urgent(self, key: str, repo: str, issue: Dict[str, Any]) -> None:
        """Add loom:urgent beside the ask's labels (act mode); a failed write
        is said and the ask is still forwarded this pass."""
        try:
            self.gh.rest("POST", "/repos/%s/issues/%d/labels" % (repo, int(issue["number"])),
                         {"labels": [URGENT]})
        except RateLimited:
            raise
        except GitHubError as err:
            _say("could not label %s %s: %s" % (key, URGENT, err))

    def _route_asks(self, keyed: List[Tuple[str, str, Dict[str, Any]]]
                    ) -> Dict[str, urgency.Decision]:
        """key -> where each ask goes this pass (bus/urgency.py). Off, or
        shadow, routes by the label rules; act may add loom:urgent."""
        cfg = self.s.urgency
        scorer = urgency.Scorer(cfg, os.path.join(self.state, "urgency.json"),
                                os.path.join(self.state, "urgency.jsonl"),
                                client=self._urgency_client, now=self.now, say=_say)
        titles = {k: " ".join(str(i.get("title") or "").split()) for k, _, i in keyed}
        out: Dict[str, urgency.Decision] = {}
        for key, repo, issue in keyed:
            labels = _label_names(issue)
            if cfg.mode == urgency.OFF:
                out[key] = urgency.Decision(urgency.today_route(labels), None, None, False)
                continue
            state = urgency.ask_state(repo, issue, labels, self.now,
                                      [t for k, t in titles.items() if k != key])
            decision = scorer.decide(key, state, labels)
            if decision.route == urgency.URGENT and URGENT not in labels:
                self._label_urgent(key, repo, issue)
            out[key] = decision
        if cfg.mode != urgency.OFF:
            scorer.prune(set(out))
            try:
                scorer.save()
            except OSError as err:
                _say("could not save the urgency ledger (%s)" % type(err).__name__)
        return out

    # -- goal reports -----------------------------------------------------
    def _goal_issues(self, repo: str) -> List[Dict[str, Any]]:
        data = self._paginate(
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
            for comment in self._paginate(path):
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
            if author in allowed and self._quiet_report(repo, issue, comment):
                _say("report %s#%s not pushed: the goal was quiet since its last report"
                     % (repo, issue.get("number")))
            elif author in allowed:
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

    def _quiet_report(self, repo: str, issue: Dict[str, Any], comment: Dict[str, Any]) -> bool:
        """Whether the goal was quiet since its report before this one
        (bus/quiet.py): such a report is never pushed to the operator's
        phone, as no report is owed for it (loom#1534). A read that fails
        pushes it: a report is never lost to a failed read."""
        number, ident = int(issue["number"]), self._report_id(comment)
        try:
            earlier = [c for c in self._paginate("/repos/%s/issues/%d/comments?per_page=100"
                                                 % (repo, number))
                       if isinstance(c, dict) and self._report_id(c) < ident]
            children = brain.goal_children(self.gh, repo, number)
            named = self._open_breaches(repo, number)
        except RateLimited:
            raise
        except (GitHubError, OSError, ValueError) as err:
            _say("report %s#%d: could not read whether the goal was quiet, so it is pushed: %s"
                 % (repo, number, err))
            return False
        now = brain.datetime.fromtimestamp(self.now, brain.timezone.utc)
        return quiet_rule.quiet(earlier, children, named, now,
                                [a.lower() for a in self.s.authors]) is not None

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
            issues = [i for i in self._goal_issues(repo) if not naming.is_backlog_home(i)]
            latest: Dict[int, Tuple[Dict[str, Any], Dict[str, Any]]] = {}
            for ident, issue, comment in self._report_comments(repo, issues, since):
                author = str((comment.get("user") or {}).get("login") or "").lower()
                if author in allowed and ident > after_id:
                    latest[int(issue["number"])] = (issue, comment)
                    top = max(top, ident)
            quiet += len(issues) - len(latest)
            for issue, comment in latest.values():
                if self._quiet_report(repo, issue, comment):
                    quiet += 1
                    continue
                body = self._report_body(comment)
                first = next((x.lstrip("#").strip() for x in body.splitlines()
                              if x.lstrip("#").strip()), "")
                if not first or first.lower().startswith("nothing moved") \
                        or IN_FLIGHT_ONLY_RE.match(first):
                    # Work only in flight is not news: 18 such lines made
                    # every digest list every goal (2026-10-18).
                    quiet += 1
                    continue
                moving.append("- **%s** %s <%s>" % (
                    self._short_name(issue), first[:SHORT_LINE_MAX],
                    self._report_link(issue, comment)))
        return moving, quiet, top

    def _digest_backlog_lines(self, since: float) -> List[str]:
        """One line per backlog goal home: they post no report on GitHub
        (2026-10-25), so this is where their status shows, from ticket data.
        One that cannot be read says so rather than reading as empty."""
        lines = []
        for repo, goal in self._readable_goal_homes():
            if not naming.is_backlog_home(goal):
                continue
            try:
                loose, tree = brain.goal_tree(self.gh, repo, int(goal["number"]))
            except RateLimited:
                raise
            except GitHubError as err:
                lines.append("- **%s** could not be read (%s)" % (self._short_name(goal),
                                                                  type(err).__name__))
                continue
            summary = self._standup_summary(
                self._standup_ticket_groups(brain._flatten(loose, tree), since))
            lines.append("- **%s** %s" % (self._short_name(goal), summary or "nothing moved"))
        return lines

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

    def _digest_shadow_lines(self, since: float) -> List[str]:
        """What shadow mode held since the last digest (bus/shadow.py): at
        most two lines, the counts and the command that lists each with its
        link. An unreadable ledger says so rather than reading as empty."""
        path = shadow.ledger_path(dict(os.environ, LOOM_HOME=self.s.home))
        try:
            return shadow.digest_lines(since, self.now, path)
        except OSError as err:
            return ["Shadow ledger %s could not be read (%s)." % (path, type(err).__name__)]

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
        backlogs = self._digest_backlog_lines(start)
        if backlogs:
            lines.append("Backlogs, %d:" % len(backlogs))
            lines += backlogs
        benched = bench.digest_lines(self._bench_list(), start, self.now)
        if benched:
            lines.append("Benched and returned models (%d):" % len(benched))
            lines += benched
        factory_line = self._digest_factory_line()
        if factory_line:
            lines.append(factory_line)
        lines += self._digest_shadow_lines(start)
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
        except RateLimited:
            raise
        except (GitHubError, OSError) as err:
            _say("could not list goal homes for triage: %s" % err)
            return None
        goals = {}
        keep = {"_scanned"}
        fallback = []
        repos_with_backlog = set()
        for repo, goal in homes:
            if not naming.is_backlog_home(goal):
                continue
            repos_with_backlog.add(repo)
            number = int(goal["number"])
            key = "%s#%d" % (repo, number)
            keep.add(key)
            goals[key] = (repo, number, state.get(key))
        for repo, goal in homes:
            if repo in repos_with_backlog or naming.is_backlog_home(goal):
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

        A backlog goal is the goal home onboard opens: labelled loom:backlog,
        or titled "Backlog: ..." when opened before that label existed
        (docs/NAMING.md). The last run time per goal is kept in triage.json so a
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
                    queued = self._paginate(
                        "/repos/%s/issues?labels=loom:queued&state=open&per_page=100" % repo)
                    if len(queued if isinstance(queued, list) else []) >= 3:
                        continue
                    lines = brain.triage(self.gh, repo, number, brain.TRIAGE_LIMIT)
                    counts = (sum(" queued under #" in line for line in lines),
                              sum(" loom:triage" in line for line in lines),
                              sum(" held:" in line for line in lines))
                    _say("triage %s: %d queued, %d triage, %d held (goal home)" %
                         (key, *counts))
                else:
                    self._run_triage(key, repo, number)
            except RateLimited:
                raise
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
                      budget: int, cmds: Dict[str, str],
                      jobs: Optional[Dict[str, Any]] = None) -> int:
        """Start oldest-first the rewrites the goal's hourly allowance and the
        pass allow, each as a background agent job kept in `jobs`
        (_start_rewrite); returns how many started."""
        jobs = {} if jobs is None else jobs
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
            self._event("rewrite_skipped", "no agent command for this repo", key)
            return 0
        # Intake refuses a brief at the goal's in-flight cap, so a model run
        # then writes nothing (17 of 72 rewrites on 2026-10-21).
        try:
            flying = brain.goal_flying(self.gh, repo, goal)
        except ValueError:
            flying = []  # not an open goal: intake says why per issue
        if len(flying) >= brain.goal_cap():
            _say(self._at_cap(key, repo, rec, flying, brain.goal_cap()))
            return 0
        rec.pop("cap", None)
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
                self._start_rewrite(key, repo, goal, issue, cmd, jobs)
            except rewrite.CommandFailure as err:
                tried.pop(str(number), None)
                self._rewrite_failed(repo, number, err)
            except RateLimited:
                raise
            except (rewrite.Invalid, ValueError, GitHubError, OSError) as err:
                self._rewrite_failed(repo, number, err)
        return done

    def _rewrite_failed(self, repo: str, number: int, err: Exception) -> None:
        _say("rewrite %s#%d wrote nothing: %s" % (repo, number, err))
        self._event("rewrite_failed", str(err), "%s#%d" % (repo, number))

    def _rewrite_folder(self, repo: str, number: int) -> str:
        return os.path.join(self.state, REWRITE_JOBS_DIR,
                            "%s-%d" % (repo.replace("/", "-"), number))

    def _start_rewrite(self, key: str, repo: str, goal: int, issue: Dict[str, Any],
                       cmd: str, jobs: Dict[str, Any]) -> None:
        """Start the agent command for one issue in the background and record
        the job; a later collect writes its answer (_collect_rewrites). The
        model never holds the pass: five rewrites of up to REWRITE_TIMEOUT
        each ran inline, and the step took 305 s of one pass (loom#1663)."""
        number = int(issue["number"])
        folder = self._rewrite_folder(repo, number)
        prompt = rewrite.prompt_for(self.gh, repo, issue)
        proc = rewrite.start_job(cmd, prompt, REWRITE_TIMEOUT, folder)
        self._rewrite_procs["%s#%d" % (repo, number)] = proc
        jobs["%s#%d" % (repo, number)] = {"goal_key": key, "repo": repo, "goal": goal,
                                          "number": number, "started": self.now}

    def _collect_rewrites(self, state: Dict[str, Any], wait: float = 0) -> None:
        """Write the answer of every rewrite job that has ended, waiting up
        to `wait` seconds for the ones this pass started. A job with no
        result past REWRITE_TIMEOUT and a grace is dropped as failed."""
        jobs = state.get(REWRITE_JOBS_KEY)
        if not isinstance(jobs, dict):
            jobs = state[REWRITE_JOBS_KEY] = {}
        deadline = time.monotonic() + wait
        while True:
            for job_key in list(jobs):
                self._collect_rewrite(state, jobs, job_key)
            running = [k for k in jobs if k in self._rewrite_procs]
            if not running or time.monotonic() >= deadline:
                break
            time.sleep(REWRITE_POLL)
        for job_key in running if wait else []:
            if job_key in jobs:
                _say("rewrite %s: the agent still runs; a later pass writes its answer"
                     % job_key)

    def _collect_rewrite(self, state: Dict[str, Any], jobs: Dict[str, Any],
                         job_key: str) -> None:
        job = jobs[job_key]
        if not isinstance(job, dict) or not isinstance(job.get("number"), int):
            del jobs[job_key]
            return
        repo, number = str(job.get("repo")), job["number"]
        proc = self._rewrite_procs.get(job_key)
        if proc is not None and proc.poll() is not None:
            del self._rewrite_procs[job_key]
        folder = self._rewrite_folder(repo, number)
        result = rewrite.job_result(folder)
        started = job.get("started")
        if result is None:
            if (job_key in self._rewrite_procs or not isinstance(started, (int, float))
                    or self.now - started < REWRITE_TIMEOUT + REWRITE_JOB_GRACE):
                return
            result = {"error": "no result after %ds" % (self.now - started)}
        try:
            output = rewrite.answer_of(result, REWRITE_TIMEOUT)
            issue = self._issue(repo, number)
            if issue.get("state") != "open" or "loom:triage" not in _label_names(issue):
                raise rewrite.Invalid("the issue is no longer open in loom:triage")
            action = rewrite.apply_answer(self.gh, repo, int(job.get("goal") or 0), number,
                                          output)
        except RateLimited:
            raise  # the job and its answer wait for the next pass
        except rewrite.CommandFailure as err:
            rec = state.get(str(job.get("goal_key")))
            if isinstance(rec, dict) and isinstance(rec.get("tried"), dict):
                rec["tried"].pop(str(number), None)
            self._rewrite_failed(repo, number, err)
            action = ""
        except (rewrite.Invalid, ValueError, GitHubError, OSError) as err:
            self._rewrite_failed(repo, number, err)
            action = ""
        del jobs[job_key]
        self._rewrite_procs.pop(job_key, None)
        shutil.rmtree(folder, ignore_errors=True)
        if not action:
            return
        _say("rewrite %s#%d: %s" % (repo, number, action))
        self._event("rewrite_succeeded", action, "%s#%d" % (repo, number))

    def _at_cap(self, key: str, repo: str, rec: Dict[str, Any],
                flying: List[Dict[str, Any]], cap: int) -> str:
        """Keep in the goal's rewrite record since when it has been at its
        in-flight cap and how many of its in-flight tickets wait with no lane
        that takes them (ticket-waits.json no_lane_since), and return the skip
        line: past GOAL_CAP_STUCK it says the goal is stuck (loom#1831)."""
        waiting = self._no_lane_since()
        blocked = sum(1 for issue in flying if "%s#%s" % (repo, issue.get("number")) in waiting)
        old = rec.get("cap") if isinstance(rec.get("cap"), dict) else {}
        since = old["since"] if self._num(old.get("since")) else self.now
        rec["cap"] = {"since": since, "seen": self.now, "in_flight": len(flying), "cap": cap,
                      "no_lane": blocked}
        counts = "%d tickets in flight (cap %d), %d blocked on no lane" % (len(flying), cap, blocked)
        if self.now - since > GOAL_CAP_STUCK:
            return "rewrite %s: goal stuck at its cap for %.0f hours: %s; skipped" % (
                key, (self.now - since) / 3600, counts)
        return "rewrite %s: goal has %s; skipped" % (key, counts)

    def _stuck_goals(self, state: Dict[str, Any]) -> List[Dict[str, Any]]:
        """The goals the rewrite step found at their cap for over
        GOAL_CAP_STUCK and saw so within GOAL_CAP_SEEN, from rewrite.json."""
        out = []
        for key, rec in sorted(state.items()):
            cap = rec.get("cap") if isinstance(rec, dict) else None
            if not (isinstance(cap, dict) and self._num(cap.get("since"))
                    and self._num(cap.get("seen")) and self.now - cap["seen"] <= GOAL_CAP_SEEN
                    and self.now - cap["since"] > GOAL_CAP_STUCK):
                continue
            out.append({"goal": key, "since": cap["since"],
                        **{field: int(cap.get(field) or 0) for field in ("in_flight", "cap", "no_lane")}})
        return out

    def post_stuck_goals(self, state: Dict[str, Any]) -> None:
        """Send the dashboard (POST GOAL_CAPS_PATH) the goals stuck at their
        cap, for its Needs-you page: when the list changes, else at most every
        GOAL_CAPS_EVERY seconds. A failed send is tried again next pass."""
        if not (self.s.transcript_url and self.s.transcript_token_ssm):
            return
        goals = self._stuck_goals(state)
        path = os.path.join(self.state, "goal-caps.json")
        posted = _read_json(path, {}, strict=False)
        posted = posted if isinstance(posted, dict) else {}
        shown = sorted(g["goal"] for g in goals)
        if (posted.get("goals") == shown and self._num(posted.get("at"))
                and 0 <= self.now - posted["at"] < GOAL_CAPS_EVERY):
            return
        token = self._transcript_token()
        if token is None:
            return
        try:
            self.upload(brain_memory.dashboard_url(self.s.transcript_url, GOAL_CAPS_PATH), token,
                        {"goals": goals, "at": self.now})
        # Deliberately broad, as in upload_usage_limits: only the type is logged.
        except Exception as err:  # noqa: BLE001
            _say("could not send the goals stuck at their cap (%s); next pass" % type(err).__name__)
            return
        _write_json(path, {"goals": shown, "at": self.now})

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
        # The agent runs as a background job: the answers of the jobs that
        # ended since the last pass are written first, and this pass's jobs
        # get REWRITE_WAIT seconds before they are left to a later pass.
        try:
            self._collect_rewrites(state)
        finally:
            _write_json(path, state)
        jobs = state[REWRITE_JOBS_KEY]
        goals = self._rewrite_goals()
        recent = [t for t in state.get(REWRITE_STATION_KEY) or []
                  if isinstance(t, (int, float)) and 0 <= self.now - t < 3600]
        for stale in [k for k in state
                      if k not in (REWRITE_STATION_KEY, "_live_tried", REWRITE_JOBS_KEY)
                      and k not in {g[0] for g in goals}]:
            del state[stale]
        state[REWRITE_STATION_KEY] = recent
        budget = min(REWRITE_PER_PASS, REWRITE_STATION_PER_HOUR - len(recent))
        cmds: Dict[str, str] = {}
        for key, repo, goal in goals if budget > 0 else []:
            rec = state.get(key)
            if not isinstance(rec, dict):
                rec = state[key] = {}
            used = 0
            try:
                used = self._rewrite_goal(key, repo, goal, rec, budget, cmds, jobs)
            except RateLimited:
                raise
            except (GitHubError, OSError) as err:
                _say("rewrite %s failed: %s" % (key, err))
            budget -= used
            recent.extend([self.now] * used)
            _write_json(path, state)
            if budget <= 0 or self._deferred():
                break
        if self._rewrite_procs:
            try:
                self._collect_rewrites(state, REWRITE_WAIT)
            finally:
                _write_json(path, state)
        self.post_stuck_goals(state)

    def close_done_tickets(self) -> None:
        """Close each done ticket, by the operator's rule (2026-10-19): a
        shipped one whose Live line names no string a log search can find.
        The status text states each linked pull request's merge and the
        conclusion of every check run on its merge commit, as read now: the
        merge gate lets a review check conclude neutral, so a fixed "merged
        on green checks" was wrong on most closes (2026-10-20). A Live line
        that names a time window that ended before the evidence source starts
        also closes, with both times mentioned in the close comment. A Live
        line that names one that is still open or covered by the evidence
        source keeps the ticket open for a verify run, and a verified
        ticket closes through brain.close on the verify path, which checks
        its criteria. Done tickets never closed: 199 sat shipped and the
        backlog grew all week though the work was finished. Since 2026-10-22
        no done ticket closes while a live or operator criterion has no
        evidence (_evidence_holds, bus/criteria_gate.py). A shipped ticket
        whose live criteria name a read-only proof is proved first, and closed
        on it when every criterion has evidence (_prove_live, #1608)."""
        if self._deferred():
            return
        left = CLOSE_DONE_PER_PASS
        self._live_budget = LIVE_CHECKS_PER_PASS
        self._proof_budget = LIVE_PROOFS_PER_PASS
        log_root = os.path.join(self.s.home, "logs")
        for repo in self._verify_repos():
            for issue in self._tickets_open(repo):
                if left <= 0:
                    return
                if self._prove_live(repo, issue):
                    left -= 1
                    continue
                why = self._done_reason(issue, self._earliest_record(repo, issue, log_root))
                if not why or self._evidence_holds(repo, issue):
                    continue
                if self._close_done_ticket(repo, issue, why):
                    left -= 1
                    _say("closed done %s#%d: %s" % (repo, int(issue["number"]), why))

    def _earliest_record(self, repo: str, issue: Dict[str, Any],
                         log_root: str) -> Optional[datetime]:
        """The evidence source's earliest record, read only for a ticket
        whose Live line names a time window; None otherwise."""
        if not _live_time_window(brain.live_criterion(issue.get("body"), limit=None)):
            return None
        return _earliest_evidence_record(self.s.evidence_cmds, repo, log_root)

    def _live_checks(self) -> Dict[str, Any]:
        found = _read_json(os.path.join(self.state, LIVE_CHECKS_FILE), {}, strict=False)
        return found if isinstance(found, dict) else {}

    def _live_passed(self, repo: str, number: int, body: str = "",
                     logs: bool = True) -> List[str]:
        """The keys of the ticket's criteria the station has evidence for:
        its live checks that passed, and, unless `logs` is off, the Live line
        when it names log strings (a verify run is dispatched only once the
        station's own log command found them, _no_live_evidence). The live
        proof turns it off: it reads the log itself."""
        mine = self._live_checks().get("%s#%d" % (repo, number))
        keys = [k for k, v in (mine.items() if isinstance(mine, dict) else [])
                if isinstance(v, dict) and v.get("result") == "pass"]
        if logs and live_needles(body):
            keys.append(criteria_gate.key(brain.live_criterion(body, limit=None)))
        return keys

    def _run_live_checks(self, repo: str, number: int,
                         gaps: Sequence[Tuple[str, str]]) -> Dict[str, str]:
        """Run the runnable checks of the ticket's unverified live criteria
        that are due, record each result in the station's state and the
        ticket's status, and return what each failed one saw, by key."""
        state = self._live_checks()
        mine = state.setdefault("%s#%d" % (repo, number), {})
        failed: Dict[str, str] = {}
        for found, text in gaps:
            check = criteria_gate.check_of(text) if found == criteria_gate.LIVE else None
            k = criteria_gate.key(text)
            last = mine.get(k) if isinstance(mine.get(k), dict) else {}
            if check is None:
                continue
            due = not isinstance(last.get("at"), (int, float)) \
                or self.now - last["at"] >= LIVE_CHECK_RETRY
            if due and self._live_budget > 0:
                self._live_budget -= 1
                passed, seen = criteria_gate.run(check, self.gh)
                last = mine[k] = {"result": "pass" if passed else "fail", "at": self.now,
                                  "seen": seen}
                _write_json(os.path.join(self.state, LIVE_CHECKS_FILE), state)
                self._routine(repo, number, "Live check %s: `%s` (%s) for: %s" % (
                    last["result"], check.target, seen, _clip(" ".join(text.split()), 160)))
            if last.get("result") == "fail":
                failed[k] = "its check failed: %s" % last.get("seen")
        return failed

    def _proof_sources(self, repo: str) -> live_proof.Sources:
        """Where the live proof reads: the repo's configured evidence command
        as its log, run once per string with LOOM_EVIDENCE_NEEDLE and only its
        lines since the deploy kept (evidence.lines_since), and the station's
        GitHub client, and, for a repo the operator opted in, the repo's
        live-evidence adapter for `log:<source>` and SQL steps
        (bus/live_adapter.py, loom#1770). With no adapter a SELECT step reads
        as no evidence."""
        command = self.s.evidence_cmds.get(repo)
        log = None
        if command:
            def log(literal: str, since: float) -> Optional[List[str]]:
                return evidence.lines_since(command, literal, since, self.now,
                                            self.s.evidence_window_hours)
        runner = self._live_adapter(repo)
        if runner is None:
            return live_proof.Sources(log=log, gh=self.gh)
        return live_proof.Sources(
            log=log, gh=self.gh,
            source_log=runner.log if runner.adapter.log is not None else None,
            sql=runner.query if runner.adapter.sql is not None else None)

    def _live_adapter(self, repo: str) -> Optional[live_adapter.Runner]:
        """The repo's live-evidence adapter, read once a pass from its default
        branch, for a repo in LOOM_LIVE_EVIDENCE_REPOS; None otherwise or when
        its manifest does not parse (said once a pass)."""
        if repo not in self.s.live_evidence_repos:
            return None
        cache = self.__dict__.setdefault("_live_adapters", {})
        if repo not in cache:
            def read(path: str) -> Optional[str]:
                return self._contents_text(repo, path)
            adapter, why = live_adapter.parse(read(live_adapter.MANIFEST), read)
            if adapter is None:
                _say("no live-evidence adapter for %s: %s" % (repo, why))
            cache[repo] = None if adapter is None else live_adapter.Runner(
                adapter, extra=self.s.live_evidence_env)
        return cache[repo]

    @staticmethod
    def _station_step(text: str, sources: live_proof.Sources) -> bool:
        """Whether a live criterion names a proof the station runs itself with
        these sources: log strings and a window read by the station's log
        command, or a step the repo's live-evidence adapter answers (a
        `log:<source>` or SQL step). A Live line with log strings and no
        window is proved by a verify run once the log names them
        (_no_live_evidence), and a check with no log by _evidence_holds."""
        step, _ = live_proof.step_of(text)
        if step is None:
            return False
        if step.kind == live_proof.LOG and step.source:
            return sources.source_log is not None
        if step.kind == live_proof.LOG:
            return sources.log is not None and step.hours > 0
        if step.kind == live_proof.QUERY:
            return sources.sql is not None
        return False

    def _prove_live(self, repo: str, issue: Dict[str, Any]) -> bool:
        """Run the read-only proof a shipped ticket's live criteria name, once
        its window has passed after the ship (bus/live_proof.py, #1608). A
        proven ticket is moved to verified with a station proof record and
        closed quoting the evidence; any other gets one status line naming the
        missing criterion and the next check time, edited into its status
        comment, and is not proved again until then. True when the ticket was
        moved, so the done close leaves it alone this pass. 62 tickets of one
        backlog sat shipped or verified and an overnight sweep found about
        half built, needing only their live proof read."""
        sources = self._proof_sources(repo)
        if not self._provable(issue, sources):
            return False
        number = int(issue["number"])
        body = str(issue.get("body") or "")
        key = "%s#%d" % (repo, number)
        path = os.path.join(self.state, LIVE_PROOF_FILE)
        state = _read_json(path, {}, strict=False)
        state = state if isinstance(state, dict) else {}
        last = state.get(key) if isinstance(state.get(key), dict) else {}
        contract = proof.contract(body)
        if not self._proof_due(last, contract) or self._proof_budget <= 0:
            return False
        self._proof_budget -= 1
        deployed_at = self._shipped_stamp(repo, issue)
        outcome = live_proof.prove(body, deployed_at, sources, now=self.now,
                                   passed=self._live_passed(repo, number, body, logs=False))
        moved = False
        if outcome.proven:
            moved, why = self._close_proven(repo, issue, outcome, deployed_at)
            # A record the station could not make is tried again; so is a
            # close shadow mode held, once the repo leaves shadow.
            outcome = outcome._replace(next_at=self.now + live_proof.RETRY)
            if why:
                outcome = outcome._replace(proven=False, missing=[("its station proof", why)])
        state.pop(key, None)
        if not moved:
            state[key] = {"contract": contract, "next_at": outcome.next_at,
                          "said": self._say_unproven(repo, number, outcome, last.get("said"))}
        _write_json(path, state)
        return moved

    def _provable(self, issue: Dict[str, Any], sources: live_proof.Sources) -> bool:
        """Whether the station proves this ticket itself: open, shipped, not
        verified or held, with a live criterion that names a proof its
        sources can read (_station_step)."""
        labels = set(_label_names(issue))
        if (issue.get("state") != "open" or "loom:shipped" not in labels
                or "loom:verified" in labels or labels & set(CLOSE_DONE_HOLDS)):
            return False
        return any(k == criteria_gate.LIVE and self._station_step(t, sources)
                   for k, t in criteria_gate.needing_evidence(str(issue.get("body") or "")))

    def _proof_due(self, last: Dict[str, Any], contract: str) -> bool:
        """A ticket is proved again at its next check, or once its criteria
        change when nothing Loom runs can change the answer (no next check)."""
        if last.get("contract") != contract:
            return True
        due = last.get("next_at")
        return isinstance(due, (int, float)) and self.now >= due

    def _say_unproven(self, repo: str, number: int, outcome: live_proof.Outcome,
                      said: Any) -> Any:
        """Edit the status line of an unproven ticket into its status comment
        when it differs from the one last said; the line now said."""
        line = live_proof.status_line(outcome)
        if not line or line == said:
            return said
        self._routine(repo, number, line)
        _say("live proof of %s#%d not met: %s" % (repo, number, line))
        return line

    def _close_proven(self, repo: str, issue: Dict[str, Any], outcome: live_proof.Outcome,
                      deployed_at: float) -> Tuple[bool, str]:
        """Move a ticket whose every criterion has evidence to verified, with a
        station proof record, and close it quoting the evidence. (whether it
        was moved, why not when it was not). Shadow mode records the close it
        would make and sends nothing."""
        number = int(issue["number"])
        record, facts, why = self._station_proof(repo, issue, outcome, deployed_at)
        if record is None:
            return False, why
        if shadow.active(repo):
            # Before the tick, the proof comment and the verified label.
            held = shadow.record_held(shadow.CLOSE, repo, number, "issue",
                                      source="station live proof",
                                      reason="every criterion has evidence, read live",
                                      gates=["shipped", "live proof window passed",
                                             "every criterion has evidence"])
            if held["new"]:
                _say("shadow mode: would verify and close %s#%d on its live proof" % (repo, number))
            return False, ""
        body = str(issue.get("body") or "")
        self.gh.rest("PATCH", "/repos/%s/issues/%d" % (repo, number), {"body": proof.tick(body)})
        try:
            source = brain.move(self.gh, repo, number, "verified", record=record,
                                verified_by="station", facts=facts)
        except ValueError as err:
            self.gh.rest("PATCH", "/repos/%s/issues/%d" % (repo, number), {"body": body})
            return False, self._note_text(err)
        _say("verified %s#%d on its live proof" % (repo, number))
        if source:
            _say("%s#%d: %s" % (repo, number, source))
        try:
            source = brain.close(self.gh, repo, number, reason="completed",
                                 note=live_proof.close_note(outcome),
                                 env={"LOOM_BRIEF_AUTHORS": " ".join(self.s.authors)})
            if source:
                _say("%s#%d: %s" % (repo, number, source))
        except RateLimited:
            raise
        except (ValueError, GitHubError) as err:
            _say("verified but not closed %s#%d: %s" % (repo, number, self._note_text(err)))
        return True, ""

    def _station_proof(self, repo: str, issue: Dict[str, Any], outcome: live_proof.Outcome,
                       deployed_at: float) -> Tuple[Optional[Dict[str, Any]], List[str], str]:
        """(the station's proof record for a proven ticket, the merged pull
        requests' facts, why there is none): the newest merged pull request's
        commit as the revision, the deploy to now as the observed window, and
        each criterion's evidence: the live proof's, a passing live check's,
        or, for a code criterion, the merged change."""
        branch, pulls, error = self._merged_pull_facts(repo, issue, files=False)
        if error:
            return None, [], "the station could not read the merged pull requests (%s)" % error
        merged = [p for p in pulls if p.get("merge_commit_sha")]
        if not merged:
            return None, [], "no merged pull request is linked to the ticket"
        newest = merged[0]
        number = int(issue["number"])
        checks = self._live_checks().get("%s#%d" % (repo, number))
        checks = checks if isinstance(checks, dict) else {}
        change = "met by the merged change: pull request #%d, merge commit %s" % (
            int(newest["number"]), newest["merge_commit_sha"])
        results = []
        texts = [text for _, text in proof.criteria(str(issue.get("body") or ""))]
        for text in texts:
            seen = outcome.evidence.get(text)
            if seen is None and criteria_gate.kind(text) == criteria_gate.LIVE:
                check = checks.get(criteria_gate.key(text))
                seen = "a passing station live check: %s" % (
                    check.get("seen") if isinstance(check, dict) else "recorded")
            results.append({"criterion": text, "result": "pass", "evidence": seen or change})
        live = sum(1 for t in texts if criteria_gate.kind(t) != criteria_gate.CODE)
        record = {
            "kind": "runtime", "revision": str(newest["merge_commit_sha"]),
            "environment": "the live deployment the station's evidence command reads",
            "artifact": "pull request #%d" % int(newest["number"]),
            "observed": {"start": audit._iso(min(deployed_at, self.now)),
                         "end": audit._iso(self.now)},
            "sources": ["pull request #%d" % int(newest["number"]),
                        "the station's read-only live proof (bus/live_proof.py)"],
            "criteria": results,
            "measure": {"name": "live criteria with station evidence",
                        "baseline": "0 of %d" % live, "target": "%d of %d" % (live, live),
                        "after": "%d of %d" % (live, live)},
        }
        return record, verify_data.proof_facts(branch, pulls), ""

    def _live_ask_room(self) -> bool:
        """Whether another unverified-criteria ask fits this hour; takes it."""
        state = self._live_checks()
        asks = [t for t in state.get("_asks") or []
                if isinstance(t, (int, float)) and 0 <= self.now - t < 3600]
        if len(asks) >= LIVE_ASKS_PER_HOUR:
            return False
        state["_asks"] = asks + [self.now]
        _write_json(os.path.join(self.state, LIVE_CHECKS_FILE), state)
        return True

    def _evidence_holds(self, repo: str, issue: Dict[str, Any]) -> bool:
        """Whether a done ticket stays open because a live or operator
        criterion has no evidence (bus/criteria_gate.py): a live one with a
        check Loom can run gets the check; one with none, a failed check, or
        an operator action sends the ticket to loom:human with one ask, once.
        2026-10-22 audit: at least 12 tickets of the private deployment repo
        closed shipped on green CI with live criteria never checked."""
        number = int(issue["number"])
        body = str(issue.get("body") or "")
        gaps = criteria_gate.unverified(body, self._live_passed(repo, number, body))
        if not gaps:
            return False
        failed = self._run_live_checks(repo, number, gaps)
        gaps = criteria_gate.unverified(body, self._live_passed(repo, number, body))
        if not gaps:
            return False
        waiting = [t for k, t in gaps if k == criteria_gate.LIVE
                   and criteria_gate.check_of(t) is not None
                   and criteria_gate.key(t) not in failed]
        if waiting or "loom:human" in _label_names(issue) or not self._live_ask_room():
            return True
        try:
            brain.human(self.gh, repo, number, criteria_gate.ask(gaps, failed))
        except RateLimited:
            raise
        except (ValueError, GitHubError) as err:
            _say("could not ask about the unverified criteria of %s#%d: %s" % (repo, number, err))
            return True
        _say("held done %s#%d for the operator: %d criteria have no evidence"
             % (repo, number, len(gaps)))
        return True

    def _close_done_ticket(self, repo: str, issue: Dict[str, Any],
                           reason: str) -> bool:
        """Close one done ticket with `reason`; True when the close writes
        landed, False when GitHub refused and the run moves on."""
        number = int(issue["number"])
        if shadow.active(repo):
            # Before the status note and the force-close label.
            held = shadow.record_held(shadow.CLOSE, repo, number, "issue", source="station done close",
                                 reason=reason, gates=["shipped", "every criterion has evidence"])
            if held["new"]:
                _say("shadow mode: would close done %s#%d (%s)" % (repo, number, reason))
            return False
        branch, pulls, error = self._merged_pull_facts(repo, issue, files=False)
        if error:
            _say("could not read the checks of done %s#%d: %s" % (repo, number, error))
        try:
            self._routine(repo, number, "Closed: %s. %s Not verified live; its criteria "
                          "were not ticked one by one."
                          % (reason, verify_data.close_facts(branch, pulls, error)))
            # force-close is the close-completeness guard's escape hatch:
            # this close is by rule, not a claim that each box was seen to
            # hold. Without it the guard reopened 46 of 175 such closes,
            # each with an email (2026-10-19).
            self.gh.rest("POST", "/repos/%s/issues/%d/labels" % (repo, number),
                         {"labels": [CLOSE_DONE_LABEL]})
            self.gh.rest("PATCH", "/repos/%s/issues/%d" % (repo, number),
                         {"state": "closed", "state_reason": "completed"})
            kept = brain.closing_labels(_label_names(issue), self.s.authors)
            self.gh.rest("PUT", "/repos/%s/issues/%d/labels" % (repo, number),
                         {"labels": kept if CLOSE_DONE_LABEL in kept
                          else kept + [CLOSE_DONE_LABEL]})
        except RateLimited:
            raise
        except GitHubError as err:
            _say("could not close done %s#%d: %s" % (repo, number, err))
            return False
        return True

    def settle_orphan_sources(self) -> None:
        """Settle each open loom:rewritten issue whose ticket is closed. A
        source was settled only when its ticket was verified or closed
        through brain.close, so a ticket the done close or a person closed
        left its source open: 87 such sources counted as open backlog on
        2026-10-20. A ticket closed as completed closes its source; one
        closed as not planned leaves it open with a comment."""
        path = os.path.join(self.state, SOURCES_FILE)
        state = _read_json(path, {}, strict=False)
        state = state if isinstance(state, dict) else {}
        last = state.get("_at")
        if isinstance(last, (int, float)) and 0 <= self.now - last < SETTLE_EVERY:
            return
        pairs = state.get("pairs") if isinstance(state.get("pairs"), dict) else {}
        left = SETTLE_PER_RUN
        # The run is stamped however it ends: at the cap, deferred, or past a
        # repo GitHub refused. Stamped only on a full run, it ran every pass,
        # so the cap held per pass and 91 sources settled in minutes, and a
        # repo the App could not read stopped every run (2026-10-20).
        try:
            for repo in self._verify_repos():
                if left <= 0 or self._deferred():
                    break
                try:
                    left -= self._settle_repo_sources(repo, pairs, left)
                except RateLimited:
                    raise
                except GitHubError as err:
                    _say("could not settle the sources of %s: %s" % (repo, err))
        finally:
            state["_at"] = self.now
            state["pairs"] = pairs
            _write_json(path, state)

    def _settle_repo_sources(self, repo: str, pairs: Dict[str, Any], left: int) -> int:
        """Settle up to `left` of the repo's orphaned sources; how many were."""
        sources = [i for i in self._paginate(
            "/repos/%s/issues?labels=loom:rewritten&state=open" % repo)
            if isinstance(i, dict) and "pull_request" not in i]
        if not sources:
            return 0
        listed = self._paginate("/repos/%s/issues?labels=loom:ticket&state=open" % repo)
        still_open = {int(i["number"]) for i in listed if isinstance(i, dict)}
        done = 0
        for source in sources:
            if done >= left:
                break
            key = "%s#%d" % (repo, int(source["number"]))
            if key not in pairs:
                pairs[key] = self._rewritten_as(repo, int(source["number"]))
            ticket = pairs[key]
            if isinstance(ticket, int) and ticket and ticket not in still_open:
                done += self._settle_source_of(repo, key, ticket, pairs)
        return done

    def _settle_source_of(self, repo: str, key: str, ticket: int,
                          pairs: Dict[str, Any]) -> int:
        """Settle the source of a ticket not listed open: 1 when something
        was said or done, else 0. A source with nothing left to do is
        recorded as 0 in `pairs` and not read again."""
        issue = self.gh.rest("GET", "/repos/%s/issues/%d" % (repo, ticket)).data or {}
        if issue.get("state") != "closed":
            return 0
        labels = set(_label_names(issue))
        completed = issue.get("state_reason") == "completed"
        # Closed as completed settles the source only when the ticket's work
        # landed, the close rule's bar; any other completed close is left for
        # a person.
        if ("loom:ticket" not in labels
                or not tickets.brief_author_trusted(issue, self.s.authors, self.s.briefers)
                or (completed and not labels & set(SETTLE_LANDED))):
            pairs[key] = 0
            return 0
        said = brain.settle_source(self.gh, repo, ticket, issue.get("body"),
                                   "completed" if completed else "not_planned",
                                   unproven=completed, authors=self.s.authors)
        if said is None or not completed:
            pairs[key] = 0  # nothing more to do for this source
        if said is None:
            return 0
        _say("settled the source of %s#%d: %s" % (repo, ticket, said))
        return 1

    def settle_claimed_tickets(self) -> None:
        """Settle operator-claimed issues whose pull requests merged
        (bus/claimed.py): an issue with a claim:<login> label and no loom:
        label had nothing to close it once its Refs pull request merged, and
        about 60 stayed open in one day (2026-10-20). At most once an hour,
        at most settle_claimed_max issues a run, each write WRITE_GAP seconds
        after the last; "dry" logs each decision and writes nothing."""
        if self.s.settle_claimed not in ("on", "dry"):
            return
        path = os.path.join(self.state, claimed.STATE_FILE)
        state = _read_json(path, {}, strict=False)
        state = state if isinstance(state, dict) else {}
        last = state.get("_at")
        if isinstance(last, (int, float)) and 0 <= self.now - last < claimed.EVERY:
            return
        left = self.s.settle_claimed_max
        pacer = claimed.Pacer()
        self._claimed_writes = 0
        if not isinstance(state.get("rereview"), dict):
            state["rereview"] = {}
        self._claimed_rereviews = state["rereview"]
        # Stamped however the run ends, as settle_orphan_sources is.
        try:
            for repo in self._verify_repos():
                if left <= 0 or self._deferred():
                    break
                try:
                    left -= self._settle_claimed_repo(repo, left, pacer)
                except RateLimited:
                    raise
                except GitHubError as err:
                    _say("could not settle the claimed issues of %s: %s" % (repo, err))
        finally:
            state["_at"] = self.now
            _write_json(path, state)

    def queue_stateless_tickets(self) -> None:
        """Queue each open loom:ticket issue that carries no state label and
        nobody holds (loom#1653): triage reads only issues that are not tickets
        yet and the factory routes only loom:queued ones, so a ticket filed with
        loom:ticket alone waited forever (33 of them on 2026-10-24). A claim,
        an operator ask, a wait, or any state or hold label leaves it alone. At
        most STATELESS_PER_RUN a run, at most once an hour, each a routine
        status event and no comment."""
        path = os.path.join(self.state, STATELESS_FILE)
        state = _read_json(path, {}, strict=False)
        state = state if isinstance(state, dict) else {}
        last = state.get("_at")
        if isinstance(last, (int, float)) and 0 <= self.now - last < STATELESS_EVERY:
            return
        left = STATELESS_PER_RUN
        try:
            for repo in self._verify_repos():
                if left <= 0 or self._deferred():
                    break
                try:
                    found = self._paginate("/repos/%s/issues?labels=loom:ticket&state=open&per_page=100" % repo)
                except GitHubError as err:
                    _say("could not read the tickets of %s: %s" % (repo, err))
                    continue
                for issue in found:
                    if left <= 0:
                        break
                    if not isinstance(issue, dict) or "pull_request" in issue:
                        continue
                    names = _label_names(issue)
                    if any(_holds_ticket(name) for name in names):
                        continue
                    number = int(issue["number"])
                    self._add_label(repo, number, "loom:queued")
                    self._routine(repo, number, STATELESS_TEXT)
                    _say("queued %s#%d: a ticket with no state label is never routed" % (repo, number))
                    left -= 1
        finally:
            state["_at"] = self.now
            _write_json(path, state)

    def release_stale_claims(self) -> None:
        """Take back another operator's claims their claimer left alone for
        claim_idle_hours (bus/stale_claims.py): remove the claim label and
        unassign them, at most stale_claims.PER_RUN issues a run, at most once
        an hour. A claim nobody worked held its issue forever (2026-10-21)."""
        if self.s.claim_idle_hours <= 0:
            return
        path = os.path.join(self.state, stale_claims.STATE_FILE)
        state = _read_json(path, {}, strict=False)
        state = state if isinstance(state, dict) else {}
        last = state.get("_at")
        if isinstance(last, (int, float)) and 0 <= self.now - last < stale_claims.EVERY:
            return
        left = stale_claims.PER_RUN
        try:
            for repo in self._verify_repos():
                if left <= 0 or self._deferred():
                    break
                try:
                    left -= self._release_stale_claims_repo(repo, left)
                    left -= self._lapse_own_claims_repo(repo, left)
                except RateLimited:
                    raise
                except GitHubError as err:
                    _say("could not read the claims of %s: %s" % (repo, err))
        finally:
            state["_at"] = self.now
            _write_json(path, state)

    def clean_labels(self) -> None:
        """One label pass (bus/label_pass.py, loom#2024): remove labels that
        contradict each other or describe a step that is over, on at most
        label_pass.PER_RUN issues a run, at most once every label_pass.EVERY
        seconds. A body Part of #N that disagrees with the sub-issue parent is
        one health note in the state file, said once. In shadow each removal
        is a ledger line and is not sent."""
        path = os.path.join(self.state, label_pass.STATE_FILE)
        state = _read_json(path, {}, strict=False)
        state = state if isinstance(state, dict) else {}
        last = state.get("_at")
        if isinstance(last, (int, float)) and 0 <= self.now - last < label_pass.EVERY:
            return
        notes = state.get("notes") if isinstance(state.get("notes"), dict) else {}
        left = label_pass.PER_RUN
        try:
            for repo in self._verify_repos():
                if left <= 0 or self._deferred():
                    break
                try:
                    left -= self._clean_labels_repo(repo, left, notes)
                except RateLimited:
                    raise
                except GitHubError as err:
                    _say("could not clean the labels of %s: %s" % (repo, err))
        finally:
            state["_at"] = self.now
            state["notes"] = notes
            _write_json(path, state)

    def _clean_labels_repo(self, repo: str, left: int, notes: Dict[str, Any]) -> int:
        """Clean up to `left` issues of the repo: its open issues, and its
        closed ones still at loom:triage; how many were changed."""
        issues = self._paginate("/repos/%s/issues?state=open&per_page=100" % repo)
        closed = self.gh.rest("GET", "/repos/%s/issues?labels=%s&state=closed&per_page=100"
                              % (repo, quote(label_pass.TRIAGE, safe=":"))).data
        issues = list(issues or []) + list(closed if isinstance(closed, list) else [])
        station_logins = list(self.s.authors) + sorted(self._own_apps())
        seen = set()
        changed = 0
        for issue in issues:
            if not isinstance(issue, dict) or "pull_request" in issue:
                continue
            number = int(issue["number"])
            if (number in seen
                    or tickets.owned_elsewhere(issue, self.s.authors, self.s.briefers)):
                continue
            seen.add(number)
            events = (self._paginate("/repos/%s/issues/%d/events?per_page=100" % (repo, number))
                      if label_pass.needs_events(issue) else None)
            found = label_pass.plan(issue, events, station_logins)
            key = "%s#%d" % (repo, number)
            if found.note and notes.get(key) != found.note:
                _say("health note %s: %s" % (key, found.note))
            if found.note:
                notes[key] = found.note
            else:
                notes.pop(key, None)
            if not found.remove or changed >= left:
                continue
            for removal in found.remove:
                self._unlabel(repo, number, removal)
            changed += 1
        return changed

    def _unlabel(self, repo: str, number: int, removal: "label_pass.Removal") -> None:
        """Remove one label, or in shadow record that it would be removed."""
        why = "remove %s: %s" % (removal.label, removal.why)
        if shadow.active(repo):
            shadow.record_held(shadow.UNLABEL, repo, number, "issue", reason=why,
                               label=removal.label, source="station clean_labels")
            return
        self.gh.rest("DELETE", "/repos/%s/issues/%d/labels/%s"
                     % (repo, number, quote(removal.label, safe="")), ok=(404,))
        _say("%s#%d: %s" % (repo, number, why))

    def _foreign_claimed(self, repo: str) -> Dict[int, Dict[str, Any]]:
        """The repo's open issues another operator claims by label, or that
        are assigned to a trusted briefer (another operator's login)."""
        ours = {a.lower() for a in self.s.authors}
        found: Dict[int, Dict[str, Any]] = {}
        names = [str(label.get("name") or "") for label in self.gh.paginate(
            "/repos/%s/labels" % repo) if isinstance(label, dict)]
        queries = ["labels=%s" % quote(name, safe=":") for name in names
                   if name.lower().startswith(stale_claims.CLAIM_PREFIX)
                   and name[len(stale_claims.CLAIM_PREFIX):].strip().lower() not in ours]
        queries += ["assignee=%s" % quote(login) for login in self.s.briefers
                    if login.lower() not in ours]
        for query in queries:
            for issue in self._paginate("/repos/%s/issues?%s&state=open" % (repo, query)):
                if isinstance(issue, dict) and "pull_request" not in issue:
                    found[int(issue["number"])] = issue
        return found

    def _release_stale_claims_repo(self, repo: str, left: int) -> int:
        """Release up to `left` of the repo's idle foreign claims; how many."""
        idle = self.s.claim_idle_hours * 3600
        released = 0
        for number, issue in sorted(self._foreign_claimed(repo).items()):
            if released >= left:
                break
            owners = stale_claims.claimers(issue, self.s.authors, self.s.briefers)
            if not owners:
                continue
            timeline = self._paginate("/repos/%s/issues/%d/timeline?per_page=100" % (repo, number))
            due = {login: label for login, label in owners.items()
                   if stale_claims.stale(issue, timeline, login, label, self.now, idle)}
            if not due:
                continue
            for login, label in due.items():
                if label:
                    self.gh.rest("DELETE", "/repos/%s/issues/%d/labels/%s"
                                 % (repo, number, quote(label, safe="")), ok=(404,))
                if any(stale_claims._login(p) == login for p in issue.get("assignees") or []):
                    self.gh.rest("DELETE", "/repos/%s/issues/%d/assignees" % (repo, number),
                                 {"assignees": [login]})
            _say("released %s#%d from %s: no act of theirs on it in %d hours"
                 % (repo, number, ", ".join(sorted(due)), self.s.claim_idle_hours))
            released += 1
        return released

    def _lapse_own_claims_repo(self, repo: str, left: int) -> int:
        """Drop this Loom's own claim from up to `left` of the repo's open
        tickets no lane works (stale_claims.lapsed): the claim label, this
        operator's assignment, and a status note saying why (loom#1217)."""
        if left <= 0:
            return 0
        ours = {a.lower() for a in self.s.authors}
        found = self._own_claimed(repo, ours)
        if not found:
            return 0
        idle = self.s.claim_idle_hours * 3600
        pulls = self._open_pull_updates(repo)
        apps = self._own_apps()
        lapsed = 0
        for number, issue in sorted(found.items()):
            if lapsed >= left:
                break
            timeline = self._paginate("/repos/%s/issues/%d/timeline?per_page=100" % (repo, number))
            due = {login: label for login, label in stale_claims.own_claims(issue, ours).items()
                   if stale_claims.lapsed(issue, timeline, pulls.get(number, []), label,
                                          self.now, idle, apps)}
            if due:
                self._drop_own_claim(repo, issue, due)
                lapsed += 1
        return lapsed

    def _own_apps(self) -> Set[str]:
        """This Loom's own App logins: the trusted authors that are apps
        ("[bot]"), and the login the GitHub client writes as. Their timeline
        events are the station's own relabels, not work on the ticket."""
        apps = {a.lower() for a in self.s.authors if a.lower().endswith("[bot]")}
        login = getattr(self.gh, "app_login", None)
        try:
            named = login() if callable(login) else None
        except GitHubError:
            named = None
        if isinstance(named, str) and named:
            apps.add(named.lower())
        return apps

    def _own_claimed(self, repo: str, ours: set) -> Dict[int, Dict[str, Any]]:
        """The repo's open Loom-managed issues with no run in progress that
        carry a claim label naming one of `ours`."""
        names = [str(label.get("name") or "") for label in self.gh.paginate(
            "/repos/%s/labels" % repo) if isinstance(label, dict)]
        queries = ["labels=%s" % quote(name, safe=":") for name in names
                   if name.lower().startswith(stale_claims.CLAIM_PREFIX)
                   and name[len(stale_claims.CLAIM_PREFIX):].strip().lower() in ours]
        found: Dict[int, Dict[str, Any]] = {}
        for query in queries:
            for issue in self._paginate("/repos/%s/issues?%s&state=open" % (repo, query)):
                if isinstance(issue, dict) and "pull_request" not in issue \
                        and stale_claims.loom_managed(issue) and not stale_claims.running(issue):
                    found[int(issue["number"])] = issue
        return found

    def _open_pull_updates(self, repo: str) -> Dict[int, List[Any]]:
        """{ticket number: updated_at of each open pull request whose body
        names it with a Refs line}."""
        pulls: Dict[int, List[Any]] = {}
        for pr in self.gh.paginate("/repos/%s/pulls?state=open" % repo):
            if isinstance(pr, dict):
                for n in _refs_numbers(repo, str(pr.get("body") or "")):
                    pulls.setdefault(n, []).append(pr.get("updated_at"))
        return pulls

    def _drop_own_claim(self, repo: str, issue: Dict[str, Any], due: Dict[str, str]) -> None:
        """Remove the claim labels (and the matching assignments) in `due`
        ({login: label}) from the issue and say why in its status."""
        number = int(issue["number"])
        for login, label in due.items():
            self.gh.rest("DELETE", "/repos/%s/issues/%d/labels/%s"
                         % (repo, number, quote(label, safe="")), ok=(404,))
            if any(stale_claims._login(p) == login for p in issue.get("assignees") or []):
                self.gh.rest("DELETE", "/repos/%s/issues/%d/assignees" % (repo, number),
                             {"assignees": [login]})
        hours = self.s.claim_idle_hours
        status.add(self.gh, repo, number,
                   "Released the claim label %s: no lane run, pull request update or "
                   "ticket activity for %d hours, so another operator's Loom may take "
                   "this ticket" % (", ".join(sorted(due.values())), hours),
                   self.now, self.s.authors)
        _say("lapsed the claim on %s#%d: this Loom has not worked it in %d hours"
             % (repo, number, hours))

    def _settle_claimed_repo(self, repo: str, left: int, pacer: "claimed.Pacer") -> int:
        """Settle up to `left` of the repo's claimed issues; how many were."""
        found = self._claimed_issues(repo)
        if not found:
            return 0
        still_open = {n for pr in self.gh.paginate("/repos/%s/pulls?state=open" % repo)
                      if isinstance(pr, dict)
                      for n in _refs_numbers(repo, str(pr.get("body") or ""))}
        done = 0
        for number in sorted(set(found) - still_open):
            if done >= left:
                break
            done += self._try_settle_claimed(repo, found[number], pacer)
        return done

    def _claimed_issues(self, repo: str) -> Dict[int, Dict[str, Any]]:
        """The repo's open claim:<login> issues claimed.skip_reason allows."""
        claims = [str(label.get("name") or "") for label in self.gh.paginate(
            "/repos/%s/labels" % repo) if isinstance(label, dict)
            and str(label.get("name") or "").lower().startswith(claimed.CLAIM_PREFIX)]
        found: Dict[int, Dict[str, Any]] = {}
        for name in claims:
            for issue in self._paginate("/repos/%s/issues?labels=%s&state=open"
                                        % (repo, quote(name, safe=":"))):
                if isinstance(issue, dict) and not claimed.skip_reason(issue):
                    found[int(issue["number"])] = issue
        return found

    def _try_settle_claimed(self, repo: str, issue: Dict[str, Any],
                            pacer: "claimed.Pacer") -> int:
        """Settle one candidate when its merged pull requests prove it: 1 when
        it was settled. One issue GitHub or the text checks refuse is logged
        and skipped; it does not stop the others."""
        number = int(issue["number"])
        pulls = [pr for pr in self._closed_pulls(repo)
                 if number in _refs_numbers(repo, str(pr.get("body") or ""))]
        if not pulls or len(pulls) > claimed.MAX_PULLS:
            return 0
        branch = self._default_branch(repo)
        try:
            checked = self._claimed_pulls(repo, number, branch, pulls, pacer)
            if checked is None:
                return 0
            return self._settle_claimed(repo, issue, branch, checked, pacer)
        except RateLimited:
            raise
        except (GitHubError, ValueError) as err:
            _say("settle-claimed: could not settle %s#%d: %s" % (repo, number, err))
            return 0

    def _claimed_pulls(self, repo: str, number: int, branch: str,
                       pulls: List[Dict[str, Any]],
                       pacer: "claimed.Pacer") -> Optional[List[Dict[str, Any]]]:
        """The issue's merged pull requests with their head's newest check
        runs, or None when one does not prove it (said once, in the log). A
        review check that merged neutral earns a re-review (loom#1221)."""
        checked = []
        for pr in pulls:
            head = str((pr.get("head") or {}).get("sha") or "")
            runs: List[Dict[str, Any]] = []
            if COMMIT_RE.fullmatch(head):
                data = self.gh.rest("GET", "/repos/%s/commits/%s/check-runs?per_page=100"
                                    % (repo, head)).data
                runs = verify_data.newest_runs(
                    (data.get("check_runs") if isinstance(data, dict) else None) or [])
            why = claimed.pull_gate(pr, runs, self.s.review_check, branch)
            if why:
                _say("settle-claimed: %s#%d stays open: PR #%d %s"
                     % (repo, number, int(pr.get("number") or 0), why))
                case = claimed.rereview_case(pr, runs, self.s.review_check, branch)
                if case:
                    self._claimed_rereview(repo, number, pr, case, runs, pacer)
                return None
            checked.append(dict(pr, runs=runs))
        return checked

    def _claimed_rereview(self, repo: str, number: int, pr: Dict[str, Any], case: str,
                          runs: List[Dict[str, Any]], pacer: "claimed.Pacer") -> None:
        """Ask the review bot to review a pull request that merged with a
        neutral review check again, once ever, then act on its next result: a
        failure files the findings as a follow-up issue, once. A success
        needs nothing here; the gate passes it. The request is recorded in
        claimed.json and by a marker on the pull request, and at most
        REREVIEW_PER_RUN requests and issues are written a run."""
        key = "%s#%d" % (repo, int(pr["number"]))
        held = self._claimed_rereviews.get(key)
        run = claimed.review_run(runs, self.s.review_check)
        stamp = verify_data._run_time(run)
        if case == "neutral" and not isinstance(held, dict):
            self._ask_rereview(repo, pr, key, stamp, pacer)
        elif case == "failure" and isinstance(held, dict) and not held.get("followup") \
                and held.get("run") != stamp:
            self._file_rereview_findings(repo, number, pr, held, run, pacer)

    def _ask_rereview(self, repo: str, pr: Dict[str, Any], key: str, stamp: str,
                      pacer: "claimed.Pacer") -> None:
        pull = int(pr["number"])
        if self._claimed_writes >= claimed.REREVIEW_PER_RUN:
            return
        act = self.s.settle_claimed == "on"
        self._claimed_writes += 1
        if not act:
            _say("settle-claimed: would ask the review bot to review %s#%d again" % (repo, pull))
            return
        url = "/repos/%s/issues/%d/comments" % (repo, pull)
        # Anyone may comment on a public pull request: only a marker by one of
        # ours (the trusted authors and this client's App login) counts.
        ours = {author.lower() for author in self.s.authors}
        own = getattr(self.gh, "_status_logins", None)
        if callable(own):
            ours |= {str(login).lower() for login in own()}
        if not any(str(c.get("body") or "").startswith(claimed.REREVIEW_MARKER)
                   and tickets._login(c.get("user")) in ours
                   for c in self._paginate(url) if isinstance(c, dict)):
            pacer.wait()
            notify.post(self.gh, repo, pull, "%s\n%s" % (
                claimed.REREVIEW_MARKER, factory.review_again()), notify.REVIEW_REQUEST)
        self._claimed_rereviews[key] = {"at": self.now, "run": stamp}
        _say("settle-claimed: asked the review bot to review %s#%d again, merged with a "
             "neutral review" % (repo, pull))

    def _file_rereview_findings(self, repo: str, number: int, pr: Dict[str, Any],
                                held: Dict[str, Any], run: Dict[str, Any],
                                pacer: "claimed.Pacer") -> None:
        pull = int(pr["number"])
        if self._claimed_writes >= claimed.REREVIEW_PER_RUN:
            return
        title = claimed.followup_title(pull)
        body = claimed.followup_body(number, pr, run)
        brain._check_written_text(title + "\n" + body)
        self._claimed_writes += 1
        if self.s.settle_claimed != "on":
            _say("settle-claimed: would file the failed re-review of %s#%d as a follow-up"
                 % (repo, pull))
            return
        pacer.wait()
        made = self.gh.rest("POST", "/repos/%s/issues" % repo, {
            "title": title, "body": body, "labels": [factory.DEFER_LABEL]}).data
        filed = int(made.get("number") or 0) if isinstance(made, dict) else 0
        if not filed:
            raise GitHubError("no issue was created in %s" % repo)
        held["followup"] = filed
        pacer.wait()
        notify.post(self.gh, repo, number,
                    "%s\nThe review check failed on pull request #%d when the station asked "
                    "the review bot to review it again. The findings are filed as #%d; this "
                    "issue stays open until they are settled." % (
                        claimed.REREVIEW_MARKER, pull, filed), notify.FAILURE)
        _say("settle-claimed: filed %s#%d, the failed re-review of PR #%d for #%d"
             % (repo, filed, pull, number))

    def _claimed_tests(self, repo: str, branch: str, pr: Dict[str, Any]) -> List[str]:
        """The test functions the pull request added that `branch` still
        defines, read from the branch itself."""
        files = self.gh.rest("GET", "/repos/%s/pulls/%d/files?per_page=100"
                             % (repo, int(pr["number"]))).data
        tests = claimed.added_tests(files if isinstance(files, list) else [])
        sources: Dict[str, str] = {}
        kept = []
        for name, test in tests:
            if name not in sources:
                if len(sources) >= claimed.MAX_TEST_FILES:
                    break
                sources[name] = claimed.decoded(self.gh.rest(
                    "GET", "/repos/%s/contents/%s?ref=%s"
                    % (repo, quote(name), quote(branch, safe="")), ok=(404,)).data)
            if claimed.defines(sources[name], test):
                kept.append(test)
        return kept

    def _settle_claimed(self, repo: str, issue: Dict[str, Any], branch: str,
                        pulls: List[Dict[str, Any]], pacer: "claimed.Pacer") -> int:
        """Close one proven claimed issue, or label it when a box is live: 1
        when it was settled (or, in a dry run, would be)."""
        number = int(issue["number"])
        body = str(issue.get("body") or "")
        act = self.s.settle_claimed == "on"
        live = claimed.live_boxes(body)
        if live:
            if act:
                pacer.wait()
                self.gh.rest("POST", "/repos/%s/issues/%d/labels" % (repo, number),
                             {"labels": [claimed.LIVE_LABEL]})
            _say("settle-claimed: %s %s#%d %s, a box asks for a live check: %s"
                 % ("labelled" if act else "would label", repo, number, claimed.LIVE_LABEL,
                    _clip(live[0], 120)))
            return 1
        for pr in pulls:
            pr["tests"] = self._claimed_tests(repo, branch, pr)
        comment = claimed.evidence_comment(pulls, branch, self.s.review_check)
        brain._check_written_text(comment)
        shown = ", ".join("#%d" % int(pr.get("number") or 0) for pr in pulls)
        if not act:
            _say("settle-claimed: would close %s#%d, merged as PR %s" % (repo, number, shown))
            return 1
        if shadow.active(repo):
            # Before the evidence comment: a held close must not say it closed.
            held = shadow.record_held(shadow.CLOSE, repo, number, "issue",
                                 source="station settle-claimed",
                                 reason="claimed issue merged as PR %s" % shown,
                                 gates=["every box proven by the merged pull requests"])
            if held["new"]:
                _say("settle-claimed: shadow mode, would close %s#%d, merged as PR %s"
                     % (repo, number, shown))
            return 1
        comments = self._paginate("/repos/%s/issues/%d/comments" % (repo, number))
        if not any(str(c.get("body") or "").startswith(claimed.MARKER)
                   for c in comments if isinstance(c, dict)):
            pacer.wait()
            notify.post(self.gh, repo, number, comment, notify.CLOSED)
        ticked = proof.tick(body)
        if ticked != body:
            pacer.wait()
            self.gh.rest("PATCH", "/repos/%s/issues/%d" % (repo, number), {"body": ticked})
        pacer.wait()
        self.gh.rest("PATCH", "/repos/%s/issues/%d" % (repo, number),
                     {"state": "closed", "state_reason": "completed"})
        _say("settle-claimed: closed %s#%d, merged as PR %s" % (repo, number, shown))
        return 1

    def _rewritten_as(self, repo: str, number: int) -> int:
        """The ticket the newest rewrite comment by a briefing login names, 0
        for none: anyone may comment on a public issue."""
        authors = {author.lower() for author in self.s.authors}
        found = 0
        for comment in self._paginate("/repos/%s/issues/%d/comments" % (repo, number)):
            if not isinstance(comment, dict) or tickets._login(comment.get("user")) not in authors:
                continue
            body = str(comment.get("body") or "")
            match = REWRITTEN_AS_RE.search(body)
            if brain.TRIAGE_MARKER in body and match:
                found = int(match.group(1))
        return found

    @staticmethod
    def _done_reason(issue: Dict[str, Any], earliest: Optional[datetime] = None) -> str:
        """Why the ticket is done and may close, or "" to leave it open.

        A Live line with a time window closes only when `earliest`, the
        evidence source's first record, falls after the window's end; a window
        still open, or one the source covers, stays open for a verify run."""
        labels = set(_label_names(issue))
        if issue.get("state") != "open" or labels & set(CLOSE_DONE_HOLDS):
            return ""
        if "loom:shipped" in labels and "loom:verified" not in labels:
            live = brain.live_criterion(issue.get("body"), limit=None)
            time_window = _live_time_window(live)
            if time_window:
                window_end = time_window[1]
                if window_end is not None and earliest is not None and earliest > window_end:
                    return ("shipped, its Live window ended %s but evidence started %s" % (
                        window_end.strftime("%Y-%m-%d"), earliest.strftime("%Y-%m-%d")))
                return ""
            if _unproven_host_checks(issue.get("body")):
                # 2026-10-22: a ticket whose criteria were all host checks
                # (a version on a host, a closed port) closed as shipped on
                # a merge that could not meet them. They wait for evidence.
                return ""
            if not live_needles(issue.get("body")):
                return "shipped, and its Live line names nothing a log search can find"
        return ""

    @staticmethod
    def _live_needs_restating(issue: Dict[str, Any]) -> bool:
        """A shipped ticket whose Live line names no exact string to search."""
        live = brain.live_criterion(issue.get("body"), limit=None)
        return ("loom:shipped" in _label_names(issue)
                and live != "(no Live criterion on the ticket)"
                and not live_needles(issue.get("body")))

    def _restate_one(self, repo: str, key: str, issue: Dict[str, Any], cmd: str) -> None:
        """Ask the rewrite command for a new Live line and write it."""
        prompt = ("Rewrite the shipped ticket's Live criterion. Return one JSON object "
                  'with "action":"restate" and a "line" string: one complete Live: '
                  "line naming an exact string the merged pull request makes the live "
                  "system print or serve. "
                  "Do not include markdown or explanation.\n\nTitle: %s\nBody:\n%s" %
                  (issue.get("title", ""), issue.get("body", "")))
        try:
            answer = rewrite.parse_answer(rewrite.run_agent(cmd, prompt, REWRITE_TIMEOUT))
            line = answer.get("line")
            if not isinstance(line, str):
                raise rewrite.Invalid("the answer has no Live line")
            brain.restate_live(self.gh, repo, int(issue["number"]), line,
                               env={"LOOM_BRIEF_AUTHORS": ",".join(self.s.authors),
                                    "LOOM_BRIEFERS": ",".join(self.s.briefers)})
        except RateLimited:
            raise
        except (rewrite.Invalid, ValueError, GitHubError, OSError) as err:
            _say("could not restate the Live criterion of %s: %s" % (key, err))
            return
        _say("restated the Live criterion of %s" % key)

    def _restate_state(self, path: str) -> Tuple[Dict[str, Any], List[float], Dict[str, Any]]:
        """The rewrite state file, the restate runs of the last hour, and the
        per-ticket last-try times."""
        state = _read_json(path, {}, strict=False)
        if not isinstance(state, dict):
            state = {}
        recent = [t for t in state.get(RESTATE_KEY) or []
                  if isinstance(t, (int, float)) and 0 <= self.now - t < 3600]
        state[RESTATE_KEY] = recent
        tried = state.get("_live_tried")
        if not isinstance(tried, dict):
            tried = state["_live_tried"] = {}
        return state, recent, tried

    def restate_shipped_live(self) -> None:
        """Restate shipped Live lines with no searchable exact string, using
        the rewrite command and its own hourly allowance (RESTATE_PER_HOUR)."""
        if not (self.s.rewrite_cmd or self.s.rewrite_private_cmd):
            return
        path = os.path.join(self.state, "rewrite.json")
        state, recent, tried = self._restate_state(path)
        budget = min(REWRITE_PER_PASS, RESTATE_PER_HOUR - len(recent))
        if budget <= 0:
            return
        cmds: Dict[str, str] = {}
        for repo in self._verify_repos():
            for issue in self._tickets_open(repo):
                if budget <= 0 or self._deferred():
                    break
                if not self._live_needs_restating(issue):
                    continue
                key = "%s#%s" % (repo, issue["number"])
                last = tried.get(key)
                if isinstance(last, (int, float)) and 0 <= self.now - last < REWRITE_RETRY:
                    continue
                cmd = self._rewrite_command(repo, cmds)
                if not cmd:
                    continue
                tried[key] = self.now
                recent.append(self.now)
                budget -= 1
                self._restate_one(repo, key, issue, cmd)
            _write_json(path, state)

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
        sources, problems = health.parse_sources(self.s.health_urls, self.s.health_diagnose)
        for problem in problems:
            _say("ignoring a LOOM_HEALTH_URLS entry: %s" % problem)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.now))
        health.Intake(self.gh, state, self.now, stamp, _say).run(sources)
        _write_json(path, state)

    # -- friction intake ---------------------------------------------------
    def intake_friction(self) -> None:
        """File a ticket for a slowdown the tick journals on the invariant
        goal home keep naming, at most every friction_intake.EVERY seconds
        (bus/friction_intake.py, LOOM_FRICTION_INTAKE)."""
        if not self.s.friction_mode:
            return
        path = friction_intake.ledger_path(self.s.home)
        state = _read_json(path, {}, strict=False)
        if not isinstance(state, dict):
            state = {}
        last = state.get("last")
        if isinstance(last, (int, float)) and 0 <= self.now - last < friction_intake.EVERY:
            return
        goal = self._invariant_goal()
        if goal is None:
            return
        repo, number = goal
        # Whole hours, so the same URL serves an hour of passes.
        since = int((self.now - friction_intake.WINDOW) // 3600 * 3600)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since))
        comments = self._paginate("/repos/%s/issues/%d/comments?since=%s&per_page=100"
                                  % (repo, number, stamp))
        journals = friction_intake.journals_from(comments, self.s.authors, self.now)
        issues = (self._paginate("/repos/%s/issues?state=open&per_page=100" % repo)
                  + self._paginate("/repos/%s/issues?state=closed&since=%s&per_page=100"
                                   % (repo, stamp)))
        intake = friction_intake.Intake(self.gh, state, self.now, _say,
                                        write=self.s.friction_mode == "on")
        try:
            intake.run(repo, number, journals, issues)
        finally:
            _write_json(path, state)

    # -- log watch -------------------------------------------------------
    def watch_logs(self) -> None:
        """Read each component's new log lines and file one loom:triage issue
        in the lanes repo per new repeated error (bus/watch.py). The logs are
        read every pass; below the budget floor filing waits for the reset."""
        repo = self.s.status_repo
        if not repo or not tickets.REPO_RE.fullmatch(repo):
            return
        # A denied path an agent reached for may sit in another goal repo's
        # checkout; those names stay out of the public lanes repo. The lanes
        # repo's own name is public already.
        own = repo.rsplit("/", 1)[-1]
        private = sorted({r.rsplit("/", 1)[-1] for r in self.s.repos
                          if isinstance(r, str) and "/" in r} - {own})
        watch.Watcher(self.gh, repo, self.s.watched, os.path.join(self.state, "watch.json"),
                      self.now, _say, write=not self._deferred(), private=private).run()

    # -- tickets under no epic ---------------------------------------------
    def move_outside_tickets(self) -> None:
        """Move each goal home's own open tickets that sit under no epic into
        an open intake epic of that goal, at most every OUTSIDE_EPIC_EVERY
        seconds (decision 0003). One log line per goal."""
        path = os.path.join(self.state, "outside-epic.json")
        state = _read_json(path, {}, strict=False)
        if not isinstance(state, dict):
            state = {}
        checked = state.get("checked")
        if self._num(checked) and 0 <= self.now - checked < OUTSIDE_EPIC_EVERY:
            return
        try:
            homes = self._goal_homes()
        except RateLimited:
            raise
        except (GitHubError, OSError) as err:
            _say("outside-epic: could not read the goal homes: %s" % err)
            return
        for repo, goal in homes:
            number = int(goal["number"])
            try:
                moved, left = brain.move_outside_tickets(self.gh, repo, number)
            except RateLimited:
                raise
            except (GitHubError, OSError, ValueError) as err:
                _say("outside-epic: goal #%d in %s failed: %s" % (number, repo, err))
                continue
            _say("outside-epic: moved %d, left %d for goal #%d in %s"
                 % (moved, left, number, repo))
        state["checked"] = self.now
        _write_json(path, state)

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
            subjects = state.get("subjects")
            if not isinstance(subjects, dict):
                subjects = state["subjects"] = {}
            invariants.Filer(self.gh, goal[0], goal[1], issues, self.now, _say,
                             write=not self._deferred(), authors=self.s.authors,
                             subjects=subjects,
                             settled=lambda inv, subject: self._invariant_settled(
                                 facts, inv, subject)).run(breaches)
        invariants.save(path, state)

    def _invariant_settled(self, facts: invariants.Facts, invariant: str,
                           subject: str) -> Optional[str]:
        """Why a gone breach's subject is settled for good, in words a public
        issue may carry, or None (loom#2022): a lane pull request merged or
        closed unmerged, an epic back at its ticket cap."""
        ref = re.fullmatch(r"([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)#([1-9][0-9]*)", subject)
        if ref is None:
            return None
        if invariant == "lane-pr-stalled":
            return self._lane_pull_settled(ref.group(1), int(ref.group(2)))
        if invariant == "epic-over-cap":
            return self._epic_settled(facts, subject)
        return None

    def _lane_pull_settled(self, repo: str, number: int) -> Optional[str]:
        """How a lane pull request left the open set, or None while it is open."""
        if any(isinstance(p, dict) and p.get("number") == number
               for p in self._pulls.get(repo, [])):
            return None
        pr = self.gh.rest("GET", "/repos/%s/pulls/%d" % (repo, number)).data
        if not isinstance(pr, dict) or pr.get("state") != "closed":
            return None
        label = self._invariant_ref(repo, number, "pull request").label
        if pr.get("merged_at"):
            return "lane %s merged at %s" % (label, pr["merged_at"])
        return "lane %s was closed unmerged at %s" % (
            label, pr.get("closed_at") or "an unknown time")

    @staticmethod
    def _epic_settled(facts: invariants.Facts, subject: str) -> Optional[str]:
        """An epic this pass read closed, or at or under its ticket cap, or
        None (loom#2140: a closed epic keeps its closed tickets)."""
        for shape in facts.shapes or []:
            for epic in shape.epics:
                if epic.epic.key == subject and not epic.open:
                    return "epic %s is closed" % epic.epic.label
                if epic.epic.key == subject and epic.tickets <= invariants.EPIC_TICKETS:
                    return "epic %s holds %d tickets, within the cap of %d" % (
                        epic.epic.label, epic.tickets, invariants.EPIC_TICKETS)
        return None

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
                 if not naming.is_backlog_home(i)]
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

    def _goal_shape(self, repo: str, goal: Dict[str, Any]) -> invariants.GoalShape:
        """The goal home's epics and the open tickets under none, from one
        walk (brain.goal_tree)."""
        number = int(goal["number"])
        loose, tree = brain.goal_tree(self.gh, repo, number)
        ref = lambda issue: self._invariant_ref(repo, int(issue["number"]))  # noqa: E731
        outside = tuple(ref(i) for i in loose if i.get("state") == "open"
                        and "loom:ticket" in _label_names(i))
        # An open epic's tickets are its sub-issues and every issue whose Part
        # of line names it, closed ones too, each once (loom#2023). The
        # standing repairs epic has no ticket cap, so it costs no search.
        linked = brain.part_of_linked(self.gh, repo, [
            int(epic["number"]) for epic, _ in tree
            if epic.get("state", "open") == "open" and not brain.standing_epic(epic)])
        epics = []
        for epic, found in tree:
            union = {int(t.get("number") or 0): t for t in found}
            for t in linked.get(int(epic["number"]), []):
                union.setdefault(int(t.get("number") or 0), t)
            union.pop(0, None)
            epics.append(invariants.EpicShape(
                ref(epic), epic.get("state", "open") == "open", len(union),
                tuple(ref(t) for t in union.values() if t.get("state") != "closed"),
                brain.standing_epic(epic), brain.sub_issue_count(self.gh, repo, epic, found),
                title=str(epic.get("title") or "")))
        return invariants.GoalShape(self._invariant_ref(repo, number),
                                    brain.goal_epic_cap(goal), tuple(epics), outside)

    def _roll_goals(self, homes: List[Tuple[str, Dict[str, Any]]]) -> List[invariants.Ref]:
        """The goal homes whose body names a roll that no LOOM_ROLL_SHIP_DOCS
        entry names: the roll source-pin pass skips them (loom#528)."""
        named = {spec["goal"] for spec in self.s.roll_ship_docs}
        return [self._invariant_ref(repo, int(goal["number"])) for repo, goal in homes
                if roll_pin.ships_by_roll(str(goal.get("body") or ""))
                and "%s#%d" % (repo, int(goal["number"])) not in named]

    def _invariant_goal_facts(self, facts: invariants.Facts) -> None:
        """Goal homes with their epics and open tickets under none, and their journals and
        marker posts; a source that cannot be read stays None."""
        try:
            homes = self._goal_homes()
        except RateLimited:
            raise
        except (GitHubError, OSError) as err:
            _say("invariants: could not read the goal homes: %s" % err)
            return
        try:
            facts.shapes = [self._goal_shape(repo, g) for repo, g in homes]
        except RateLimited:
            raise
        except (GitHubError, OSError) as err:
            _say("invariants: could not read the goal tickets: %s" % err)
        facts.roll_goals = self._roll_goals(homes)
        facts.roll_docs = len(self.s.roll_ship_docs)
        try:
            facts.journals, facts.posts = self._invariant_comments(homes)
        except RateLimited:
            raise
        except (GitHubError, OSError) as err:
            _say("invariants: could not read the goal home comments: %s" % err)

    def _invariant_facts(self, state: Dict[str, Any]) -> invariants.Facts:
        """What the invariants read, each source on its own: one that cannot
        be read stays None and its invariants find nothing this pass."""
        facts = invariants.Facts(now=self.now)
        for name, read in (("events", self._read_events),
                           ("runs", self._invariant_runs),
                           ("silent_runs", self._invariant_silent),
                           ("waits", self._invariant_waits),
                           ("unshipped", self._invariant_unshipped),
                           ("beats", lambda: {lane: self._remote_heartbeat(lane)
                                              for lane in REMOTE_LANES})):
            try:
                setattr(facts, name, read())
            except RateLimited:
                raise
            except (GitHubError, OSError) as err:
                _say("invariants: could not read %s: %s" % (name, err))
        facts.held = state["held"] = self._invariant_held(state.get("held"))
        facts.no_topup = self._no_topup_lanes()
        facts.label_refusals = self._labeler_refusals()
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
        if any(self.now - since > invariants.HELD_MAX and lane not in facts.no_topup
               for lane, since in facts.held.items()):
            try:
                facts.queued = self._invariant_queued()
            except RateLimited:
                raise
            except (GitHubError, OSError) as err:
                _say("invariants: could not read the queued tickets: %s" % err)
        try:
            facts.checks = self._invariant_checks()
        except RateLimited:
            raise
        except (GitHubError, OSError) as err:
            _say("invariants: could not read the plan checks: %s" % err)
        try:
            facts.lane_pulls = self._invariant_lane_pulls()
        except RateLimited:
            raise
        except (GitHubError, OSError) as err:
            _say("invariants: could not read the lane pull requests: %s" % err)
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
            # A journal edited in place counts once per tick, at its Tick time.
            for c in handoff.expand_journals(
                    self._paginate("/repos/%s/issues/%d/comments?since=%s&per_page=100"
                                   % (repo, int(goal["number"]), stamp)), at_key="created_at"):
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
        """Each trusted, truly queued ticket and the lanes of its difficulty
        tier: loom:queued its only state label, and not another operator's.
        A ticket still marked queued beside a later state (shipped, merged)
        or claimed by another operator waits for no lane; counting those
        named shipped tickets as queued (loom#399)."""
        states = {"loom:" + name for name in brain.STATES}
        out = []
        for repo in self.s.repos:
            if not tickets.REPO_RE.fullmatch(repo):
                continue
            for issue in self._paginate("/repos/%s/issues?labels=loom:ticket&state=open" % repo):
                if (not isinstance(issue, dict) or "pull_request" in issue
                        or states.intersection(_label_names(issue)) != {"loom:queued"}
                        or not tickets.brief_author_trusted(issue, self.s.authors,
                                                            self.s.briefers)
                        or tickets.owned_elsewhere(issue, self.s.authors,
                                                   self.s.briefers) is not None):
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

    def _invariant_lane_pulls(self) -> List[invariants.LanePull]:
        """The open lane pull requests the stalled invariant reads: trusted, on
        a loom/ branch and opened over invariants.PR_STALLED ago (loom#715).
        A pull request that cannot be read is logged and skipped, so one
        unreadable repo or ticket costs that pull request and no other."""
        authors = {a.lower() for a in self.s.authors}
        out: List[invariants.LanePull] = []
        for repo in self.s.repos:
            if not tickets.REPO_RE.fullmatch(repo):
                continue
            try:
                pulls = self._open_pulls(repo)
            except RateLimited:
                raise
            except GitHubError as err:
                _say("invariants: could not read the open pull requests of %s: %s" % (repo, err))
                continue
            for pr in pulls:
                at = self._stalled_since(pr, authors)
                if at is None:
                    continue
                try:
                    out.append(self._lane_pull(repo, pr, at))
                except RateLimited:
                    raise
                except GitHubError as err:
                    _say("invariants: left out pull request %s#%s, unreadable: %s"
                         % (repo, pr.get("number"), err))
        return out

    def _stalled_since(self, pr: Dict[str, Any], authors: set) -> Optional[float]:
        """When this pull request opened, or None when it is not one the
        stalled invariant reads."""
        if not self._trusted_lane_pull(pr, authors):
            return None
        at = self._epoch(str(pr.get("created_at") or ""))
        return at if at is not None and self.now - at > invariants.PR_STALLED else None

    def _lane_pull(self, repo: str, pr: Dict[str, Any], at: float) -> invariants.LanePull:
        """One open lane pull request as the stalled invariant reads it: the
        issue that carries it and its labels, the open threads the review bot
        left on it, and whether a fix round is queued on its ticket."""
        number = int(pr["number"])
        issue = self._pr_ticket(repo, pr)
        names = set(_label_names(pr))
        if issue is not None:
            names |= set(_label_names(issue))
        drives = bool(issue is not None and issue.get("state") == "open"
                      and "loom:ticket" in _label_names(issue))
        threads = len(self._bot_threads(repo, number)) if drives else 0
        return invariants.LanePull(
            self._invariant_ref(repo, number, "pull request"), at,
            self._invariant_ref(repo, int(issue["number"])) if issue is not None else None,
            tuple(sorted(names)), drives, threads,
            self._fix_queued(repo, issue, pr, threads))

    def _pr_ticket(self, repo: str, pr: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """The issue that carries this pull request (the Refs line of its body
        or the number in its branch), or None when it names none or names two.
        A ticket GitHub cannot read raises, so a transient failure skips this
        pull request instead of reporting it as a pull request no ticket
        carries."""
        try:
            association = brain._ticket_association(repo, pr)
        except ValueError as err:
            _say("invariants: %s#%d names no one ticket: %s" % (repo, pr["number"], err))
            return None
        if association is None:
            return None
        data = self.gh.rest("GET", "/repos/%s/issues/%d" % association).data
        return data if isinstance(data, dict) else None

    def _fix_queued(self, repo: str, issue: Optional[Dict[str, Any]], pr: Dict[str, Any],
                    threads: int) -> bool:
        """Whether a fix round is queued on this ticket for the pull request's
        head: hand-back's own rule. The ticket's comments are read only when
        open threads are what a fix round would answer."""
        if issue is None or not threads:
            return False
        comments = self._paginate("/repos/%s/issues/%d/comments?per_page=100"
                                    % (repo, int(issue["number"])))
        return self._fix_pending(_label_names(issue), comments,
                                 str((pr.get("head") or {}).get("sha") or ""))

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
        last = self._finished_brain_tick()
        if last is not None:
            def brain_body() -> Dict[str, Any]:
                return transcripts.brain_body(home, last["started"], last["at"], last["outcome"],
                                              str(last.get("model") or ""), self.claude_projects)
            runs.append(("brain", float(last["started"]), brain_body))
        return runs

    def _finished_brain_tick(self) -> Optional[Dict[str, Any]]:
        """The last brain tick (state/brain.json) when it ran the agent and
        ended: outcome ok or failed, with the numeric start brain.sh records
        and its end; else None."""
        tick = self._brain()
        last = tick[0] if tick else {}
        if last.get("outcome") in ("ok", "failed") and all(
                self._num(last.get(k)) for k in ("started", "at")):
            return last
        return None

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

    # -- brain memory ------------------------------------------------------
    def post_brain_memory(self) -> None:
        """Post the last brain tick on this machine to the dashboard's
        brain_ticks once (bus/brain_memory.py), with its commitments, then
        the rules digest's sources that changed. A failed post is tried on
        the next pass, UPLOAD_TRIES times in all; the rules wait for a tick
        that posted. Between ticks, every CLOSE_EVERY seconds, it sends the
        done markers written since the last send. Uses the transcript upload
        settings and token."""
        if not (self.s.transcript_url and self.s.transcript_token_ssm):
            return
        path = os.path.join(self.state, "brain_memory.json")
        ledger = _read_json(path, {})
        if not isinstance(ledger, dict):
            raise StateError("state file %s does not hold an object" % path)
        last = self._finished_brain_tick()
        tries = self._tick_tries(ledger, last)
        closed_at = ledger.get("closed_at")
        close_due = self._num(ledger.get("since")) and not (
            self._num(closed_at) and 0 <= self.now - closed_at < CLOSE_EVERY)
        if tries is None and not close_due:
            return
        token = self._transcript_token()
        if token is None:
            return
        homes = [(repo, issue, [c for c in self.gh.paginate(
                    "/repos/%s/issues/%d/comments?per_page=100" % (repo, int(issue["number"])))
                  if isinstance(c, dict)]) for repo, issue in self._goal_homes()]
        found = handoff.journals(
            [{"body": c.get("body"), "at": c.get("created_at"), "login": tickets._login(c.get("user"))}
             for _, _, comments in homes for c in comments], self.s.authors)
        if last is not None and tries is not None:
            self._post_brain_tick(last, ledger, token, found, tries)
            if ledger["tick"]["result"] == "sent":
                self._post_brain_rules(ledger, token, homes)
        else:
            self._post_brain_closings(ledger, token, found)
        _write_json(path, ledger)

    def _tick_tries(self, ledger: Dict[str, Any], last: Optional[Dict[str, Any]]) -> Optional[int]:
        """The failed posts of the last finished tick so far, or None when
        there is none to post (none finished, or it is sent or given up)."""
        if last is None:
            return None
        rec = ledger.get("tick") if isinstance(ledger.get("tick"), dict) else {}
        tries = rec.get("result") if rec.get("started") == last["started"] else 0
        return int(tries) if self._num(tries) else None

    def _post_brain_tick(self, last: Dict[str, Any], ledger: Dict[str, Any], token: str,
                         found: List[Dict[str, Any]], tries: int) -> None:
        """Post the tick's row and record in the ledger "sent", "refused",
        "gave up", or the failed tries so far. Sent, the done markers up to
        the tick's end count as sent."""
        record = (brain.tick_step(self.gh, self.s.status_repo)
                  if tickets.REPO_RE.fullmatch(self.s.status_repo or "") else None)
        since = ledger.get("since") if self._num(ledger.get("since")) else None
        body = brain_memory.tick_body(last, record, found, brain_memory.holder_of(last), since=since)
        when = self._utc(last["started"])
        try:
            self.upload(brain_memory.dashboard_url(self.s.transcript_url, brain_memory.TICKS_PATH),
                        token, body)
            result: Any = "sent"
            ledger["since"] = max(since or 0, last["at"] + brain_memory.GRACE)
        except UploadRefused as err:
            _say("the dashboard refused the brain tick of %s (%d); given up" % (when, err.status))
            result = "refused"
        # Deliberately broad, as in upload_transcripts: only the type is logged.
        except Exception as err:  # noqa: BLE001
            result = tries + 1
            if result >= UPLOAD_TRIES:
                _say("gave up posting the brain tick of %s after %d tries (%s)"
                     % (when, UPLOAD_TRIES, type(err).__name__))
                result = "gave up"
        ledger["tick"] = {"started": last["started"], "result": result}

    def _post_brain_closings(self, ledger: Dict[str, Any], token: str,
                             found: List[Dict[str, Any]]) -> None:
        """Send the done markers written since the last send, so the
        commitments they finish close; a failed send is tried CLOSE_EVERY
        seconds later."""
        ledger["closed_at"] = self.now
        closings = brain_memory.closings(found, ledger["since"])
        if not closings:
            return
        try:
            self.upload(brain_memory.dashboard_url(self.s.transcript_url,
                                                   brain_memory.COMMITMENTS_PATH),
                        token, {"commitments": [c for _, c in closings]})
        # Deliberately broad, as above: only the type is logged.
        except Exception as err:  # noqa: BLE001
            _say("could not send the brain's done markers (%s); next in %d minutes"
                 % (type(err).__name__, CLOSE_EVERY // 60))
            return
        ledger["since"] = closings[-1][0] + 1

    def _post_brain_rules(self, ledger: Dict[str, Any], token: str,
                          homes: List[Tuple[str, Dict[str, Any], List[Dict[str, Any]]]]) -> None:
        """Load the rules whose text or source changed since they were last
        loaded: docs/MUSE.md's Rules, keyed by commit, and each goal home's;
        and retire the sources that no longer have any."""
        commit, text = _muse_rules()
        rules = [{"source": MUSE_RULES, "source_ref": commit, "text": text}] if commit and text else []
        rules += [r for r in (brain_memory.goal_rules(repo, issue, comments, self.s.authors)
                              for repo, issue, comments in homes) if r]
        loaded = ledger.get("rules") if isinstance(ledger.get("rules"), dict) else {}
        # A source that yields no rules now (a closed goal, a removed section)
        # is retired with an empty text; docs/MUSE.md only when it was read.
        current = {r["source"] for r in rules}
        rules += [{"source": source, "source_ref": "removed", "text": ""} for source in loaded
                  if source not in current and (source != MUSE_RULES or commit)]
        changed = [r for r in rules if loaded.get(r["source"]) != brain_memory.digest(r)]
        if not changed:
            return
        try:
            self.upload(brain_memory.dashboard_url(self.s.transcript_url, brain_memory.RULES_PATH),
                        token, {"rules": changed})
        # Deliberately broad, as above; the rules are loaded after the next tick.
        except Exception as err:  # noqa: BLE001
            _say("could not load the brain rules (%s); next tick" % type(err).__name__)
            return
        loaded = dict(loaded, **{r["source"]: brain_memory.digest(r) for r in changed})
        ledger["rules"] = {source: d for source, d in loaded.items()
                           if not any(r["source"] == source and not r["text"] for r in changed)}

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
        # One goal's failure must not skip the later goals' standups, nor drop
        # the stamps of the goals already posted this pass (loom#201), and one
        # unreadable repo must not skip the other repos' goals (loom#1071).
        for repo, goal in self._readable_goal_homes():
            if naming.is_backlog_home(goal):
                # A backlog goal posts no report on GitHub: its status is one
                # line in the twice-daily digest (_digest_backlog_lines).
                continue
            try:
                self._post_overdue_goal_standup(repo, goal, state, authors, now)
            except RateLimited:
                raise
            except (GitHubError, OSError, ValueError) as err:
                _say("standup %s#%s failed: %s" % (repo, goal.get("number"), err))
        _write_json(state_path, state)

    def _post_overdue_goal_standup(self, repo: str, goal: Dict[str, Any],
                                   state: Dict[str, Any], authors: List[str],
                                   now: Any) -> None:
        number = int(goal["number"])
        key = "%s#%d" % (repo, number)
        sent = state.get(key)
        if isinstance(sent, (int, float)) and not isinstance(sent, bool) \
                and sent <= self.now < self._report_due(sent) - STANDUP_LEAD:
            return
        held = state.get(key + " quiet")
        if isinstance(held, (int, float)) and not isinstance(held, bool) \
                and 0 <= self.now - held < QUIET_RECHECK:
            return
        comments = self._paginate("/repos/%s/issues/%d/comments?per_page=100" %
                                    (repo, number))
        node = {"comments": {"nodes": [
            {"body": c.get("body"), "createdAt": c.get("created_at"),
             "author": {"login": tickets._login(c.get("user"))}}
            for c in comments if isinstance(c, dict)]}}
        ledger = brain.goal_ledger(goal, node, now, authors)
        last = self._epoch(str(ledger.get("report") or ""))
        due_at = self._epoch(str(ledger.get("due") or ""))
        due = (ledger["state"] == "overdue"
               or (last is not None and due_at is not None
                   and self.now >= due_at - STANDUP_LEAD))
        if not due:
            return
        loose, tree = brain.goal_tree(self.gh, repo, number)
        children = brain._flatten(loose, tree)
        named = self._open_breaches(repo, number)
        breaches = named if (repo, number) == self._invariant_goal() else None
        hold = self._gate_hold_for(repo, number)
        body = self._overdue_standup_body(children, self._epoch(str(ledger.get("report") or "")),
                                          breaches, gate_hold=hold,
                                          finished=brain.finished_epics(tree))
        brain._check_written_text(body)
        if hold is None and self._quiet_standup(repo, comments, children, named, body,
                                                authors, now):
            state[key + " quiet"] = self.now
            return
        quiet = hold is None and body == self._overdue_standup_body(
            [], None, None if breaches is None else [])
        previous = self._last_same_report(comments, authors, body)
        if previous is not None:
            # Every new comment emails the goal's author; a report that says
            # what an earlier one said edits that one instead, which notifies
            # no one, even when a brain's report came in between.
            stamp = QUIET_STAMP if quiet else SAME_STAMP
            self.gh.rest("PATCH", "/repos/%s/issues/comments/%d" % (repo, previous),
                         {"body": body + "\n" + stamp % now.strftime("%Y-%m-%dT%H:%MZ")})
        else:
            notify.post(self.gh, repo, number, body, notify.REPORT)
        state[key] = self.now

    @staticmethod
    def _report_due(at: float) -> float:
        """The epoch of the reading time a report posted at `at` is followed by."""
        return brain.next_report_due(
            brain.datetime.fromtimestamp(at, brain.timezone.utc)).timestamp()

    def _quiet_standup(self, repo: str, comments: List[Any], children: List[Any],
                       named: List[Dict[str, Any]], body: str, authors: List[str],
                       now: Any) -> bool:
        """True when the goal is quiet since its last report (loom#1534): it
        is owed no standup. Its weekly heartbeat edits the station's last
        standup in place, which emails no one; with none to edit, nothing is
        posted."""
        still = quiet_rule.quiet(comments, children, named, now, authors)
        if still is None:
            return False
        mine = self._last_standup(comments, authors)
        if still.heartbeat_due(now) and mine is not None:
            self.gh.rest("PATCH", "/repos/%s/issues/comments/%d" % (repo, mine),
                         {"body": quiet_rule.heartbeat(body, now)})
        return True

    @staticmethod
    def _last_standup(comments: List[Any], authors: List[str]) -> Optional[int]:
        """The id of the goal's newest station standup by `authors`, else None."""
        for comment in reversed(comments):
            text = str(comment.get("body") or "") if isinstance(comment, dict) else ""
            if (isinstance(comment.get("id") if isinstance(comment, dict) else None, int)
                    and tickets._login(comment.get("user")).lower() in authors
                    and text.startswith(REPORT_MARKER) and STANDUP_FOOTER in text):
                return comment["id"]
        return None

    @staticmethod
    def _last_same_report(comments: List[Any], authors: List[str], body: str) -> Optional[int]:
        """The id of the goal's latest report by `authors` that says `body`
        (its stamp line aside), else None. Reports in between do not matter:
        the brain's reports on the same goal never match the station's."""
        stamps = tuple(stamp.split("%")[0] for stamp in (QUIET_STAMP, SAME_STAMP))
        for comment in reversed(comments):
            if not isinstance(comment, dict) or not isinstance(comment.get("id"), int) \
                    or tickets._login(comment.get("user")).lower() not in authors:
                continue
            text = str(comment.get("body") or "")
            if not text.startswith(REPORT_MARKER):
                continue
            kept = [line for line in text.splitlines() if not line.startswith(stamps)]
            if "\n".join(kept) == body:
                return comment["id"]
        return None

    def _open_breaches(self, repo: str, goal: int) -> List[Dict[str, Any]]:
        """The open loom:invariant issues filed on the goal home."""
        return [i for i in self._paginate("/repos/%s/issues?labels=%s&state=open"
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
                              gate_hold: Optional[Dict[str, Any]] = None,
                              finished: Optional[List[Dict[str, Any]]] = None) -> str:
        groups = self._standup_ticket_groups(children, since)
        rows = self._standup_rows
        summary = self._standup_summary(groups)
        first = ("%s since the last report." % summary) if summary else "Nothing moved since the last report."
        stuck = "Past the stuck age in PR: %s." % rows(groups["aged_pr"])
        if gate_hold is not None:
            stuck += (" Held by the unproven-work gate: goal #%d -- %d tickets shipped "
                      "without a proof (limit %d), %d briefed tickets waiting, "
                      "%d lanes idle." % (gate_hold["goal"], gate_hold["count"],
                                          gate_hold["limit"], len(gate_hold["tickets"]),
                                          gate_hold["idle"]))
        # Headline, at most five lines and the footer (2026-10-25, "info
        # overload"): one line per heading, as journal.REPORT_LINES_MAX holds
        # a brain's report to.
        return "\n".join([
            REPORT_MARKER, first,
            "Going well: Merged: %s. Verified: %s. Shipped: %s." %
            (rows(groups["merged"]), rows(groups["verified"]), rows(groups["sent_back"])),
            "Not going well: Failed: %s. Regressed after shipping: %s." %
            (rows(groups["failed"]), rows(groups["regressed"]))
            + (" Invariants broken: %s." % rows(breaches) if breaches is not None else ""),
            "Still stuck: " + stuck,
            "Next: In flight: %s. Queued for a lane: %s. Needs difficulty: %s." %
            (rows(groups["in_flight"]), rows(groups["queued"]), rows(groups["needs_difficulty"]))
            + (" Epics with every ticket closed (check the done-when, then close): %s."
               % rows(finished) if finished else ""),
            "Needs you: Open asks: %s." % rows(groups["asks"]), "",
            STANDUP_FOOTER])

    def _standup_summary(self, groups: Dict[str, List[Dict[str, Any]]], station: Optional["Station"] = None) -> str:
        """"3 merged, 2 in flight": the counts of a standup's groups, or ""."""
        counts = [(len(groups[k]), word) for k, word in (
            ("merged", "merged"), ("verified", "verified"), ("sent_back", "shipped"),
            ("failed", "failed"), ("regressed", "regressed"), ("aged_pr", "stuck"),
            ("in_flight", "in flight"), ("queued", "queued"), ("asks", "waiting on you"))
            if k in groups]
        st = station or self
        stale = getattr(st, "_stale_asks_withdrawn", 0)
        summary = ", ".join("%d %s" % (n, word) for n, word in counts if n)
        if stale > 0:
            word = "stale ask withdrawn" if stale == 1 else "stale asks withdrawn"
            summary = (summary + ", " if summary else "") + ("%d %s" % (stale, word))
        return summary

    def _standup_ticket_groups(self, children: List[Any], since: Optional[float] = None
                               ) -> Dict[str, List[Dict[str, Any]]]:
        """Tickets by standup heading. Merged, verified and shipped count only
        tickets updated since the last report, so a standup says what changed."""
        groups: Dict[str, List[Dict[str, Any]]] = {key: [] for key in (
            "merged", "verified", "sent_back", "failed", "regressed", "aged_pr",
            "in_flight", "queued", "needs_difficulty", "asks")}
        for issue in children:
            if isinstance(issue, dict):
                for group in self._standup_groups_of(issue, since):
                    groups[group].append(issue)
        return groups

    @staticmethod
    def _issue_label_names(issue: Dict[str, Any]) -> List[str]:
        return [str(l.get("name", "")) if isinstance(l, dict) else str(l)
                for l in issue.get("labels", [])]

    def _standup_groups_of(self, issue: Dict[str, Any], since: Optional[float]) -> List[str]:
        """The standup headings one ticket belongs under."""
        labels = set(self._issue_label_names(issue))
        updated = self._epoch(str(issue.get("updated_at") or ""))
        recent = since is None or (updated is not None and updated >= since)
        found = [group for label, group in (("loom:merged", "merged"),
                                            ("loom:verified", "verified"),
                                            ("loom:shipped", "sent_back"))
                 if label in labels and recent]
        if issue.get("state") != "open" or "loom:ticket" not in labels:
            return found
        found.extend(self._open_standup_groups(issue, labels))
        return found

    def _open_standup_groups(self, issue: Dict[str, Any], labels: Set[str]) -> List[str]:
        created = self._epoch(str(issue.get("created_at") or ""))
        aged = created is not None and self.now - created > factory.STUCK_AGE
        is_waiting, is_queued = self._standup_ask_status(issue, labels)
        found = []
        for group, hit in (("failed", "loom:failed" in labels),
                           ("regressed", "loom:regression" in labels),
                           ("aged_pr", "loom:pr" in labels and aged),
                           ("in_flight", bool(labels & {"loom:briefed", "loom:running",
                                                        "loom:pr"})),
                           ("queued", is_queued),
                           ("needs_difficulty", "loom:queued" in labels and
                            not factory.has_difficulty(issue)),
                           ("asks", is_waiting)):
            if hit:
                found.append(group)
        return found

    def _standup_ask_status(self, issue: Dict[str, Any], labels: Set[str]) -> Tuple[bool, bool]:
        has_needs = any(self.NEEDS_LABEL_PATTERN.match(l) for l in labels)
        has_ask = self._has_ask_comment(issue) if has_needs else False
        is_waiting = bool(labels & {NEEDS_OPERATOR, HUMAN}) or (has_needs and has_ask)
        is_queued = ("loom:queued" in labels) or (has_needs and not has_ask)
        return is_waiting, is_queued

    @staticmethod
    def _standup_rows(items: List[Dict[str, Any]]) -> str:
        shown = ", ".join("#%s %s" % (issue.get("number"),
            " ".join(str(issue.get("title") or "").split()))
            for issue in items[:STANDUP_ROWS_MAX]) or "none"
        more = len(items) - STANDUP_ROWS_MAX
        return shown + (" and %d more" % more if more > 0 else "")

    # -- stale ask detection -----------------------------------------------
    STALE_ASK_PATTERN = re.compile(r"stale-when:\s*commit\s+([0-9a-f]{7,40})\s+live", re.I)
    ISSUE_EXISTS_PATTERN = re.compile(r"stale-when:\s*issue\s+exists", re.I)
    NEEDS_LABEL_PATTERN = re.compile(r"^needs:([a-z0-9_.-]+)$", re.I)
    _ASK_HEADING_PATTERN = re.compile(r"^##\s*Ask\b", re.M | re.I)
    _ASK_WORD_PATTERN = re.compile(r"[*_]*(?:exact\s+)?ask\b", re.I)

    def _parse_stale_when(self, comment_body: str) -> Optional[str]:
        """Parse an ask comment for a stale-when line: 'stale-when: commit <sha> live' or
        'stale-when: issue exists'."""
        stale_when = self.STALE_ASK_PATTERN.search(comment_body)
        if stale_when:
            return stale_when.group(1).lower()
        if self.ISSUE_EXISTS_PATTERN.search(comment_body):
            return "issue exists"
        return None

    def _check_commit_live(self, repo: str, commit_sha: str) -> bool:
        """Check if a commit SHA exists in the repo's default branch git history."""
        try:
            data = self.gh.rest("GET", "/repos/%s/commits/%s" % (repo, commit_sha)).data or {}
            return bool(isinstance(data, dict) and data.get("sha"))
        except RateLimited:
            raise
        except GitHubError as err:
            _say("could not check commit %s in %s: %s" % (commit_sha, repo, err))
            return False

    def _check_issue_exists(self, repo: str, issue_number: int) -> bool:
        """Check if an issue number exists in a repo."""
        try:
            data = self.gh.rest("GET", "/repos/%s/issues/%d" % (repo, issue_number)).data or {}
            return bool(isinstance(data, dict) and data.get("number"))
        except RateLimited:
            raise
        except GitHubError as err:
            _say("could not check issue %d in %s: %s" % (issue_number, repo, err))
            return False

    def _get_issue_comments(self, repo: str, issue_number: int) -> List[Dict[str, Any]]:
        """Fetch comments for an issue, returning empty list on error."""
        try:
            items = self.gh.paginate("/repos/%s/issues/%d/comments?per_page=100" % (repo, issue_number))
            return [c for c in items if isinstance(c, dict)]
        except RateLimited:
            raise
        except GitHubError as err:
            _say("could not fetch comments for %s#%d: %s" % (repo, issue_number, err))
            return []

    def _is_ask_text(self, body: str) -> bool:
        """Check whether a comment or issue body contains ask indicators."""
        if not body:
            return False
        if body.startswith(HUMAN_MARKER):
            return True
        patterns = (self.STALE_ASK_PATTERN, self.ISSUE_EXISTS_PATTERN,
                    self._ASK_HEADING_PATTERN, self._ASK_WORD_PATTERN)
        if any(p.search(body) for p in patterns):
            return True
        return any(self.NEEDS_LABEL_PATTERN.search(w) for w in body.split())

    def _has_ask_comment(self, issue: Dict[str, Any]) -> bool:
        """Check if an issue has an ask comment."""
        repo = issue.get("repository", {}).get("full_name") or (self.s.repos[0] if self.s.repos else "")
        number = int(issue.get("number") or 0)
        comments = self._get_issue_comments(repo, number)
        if any(self._is_ask_text(str(c.get("body") or "")) for c in comments):
            return True
        return self._is_ask_text(str(issue.get("body") or ""))

    def _target_labels_after_withdrawal(self, labels: List[str], reason: str) -> List[str]:
        remaining = [name for name in labels if not self.NEEDS_LABEL_PATTERN.match(name)]
        if not any(k in remaining for k in ("awaiting-live-check", "loom:queued")):
            target = "awaiting-live-check" if reason.startswith(("commit", "roll")) else "loom:queued"
            remaining.append(target)
        return remaining

    def _withdraw_stale_ask(self, repo: str, issue: Dict[str, Any], reason: str) -> bool:
        """Withdraw a stale ask: post a note and move issue back to working state,
        removing needs:<login> label. Returns True if withdrawn, False otherwise."""
        labels = self._issue_label_names(issue)
        if not any(self.NEEDS_LABEL_PATTERN.match(lbl) for lbl in labels):
            return False

        number = int(issue.get("number") or 0)
        body = "Stale ask withdrawn: %s" % reason

        try:
            notify.post(self.gh, repo, number, body, notify.NOTE)
            remaining = self._target_labels_after_withdrawal(labels, reason)
            new_label_objs = [{"name": name} for name in remaining]
            self.gh.rest("PATCH", "/repos/%s/issues/%d" % (repo, number),
                         {"labels": new_label_objs})
            issue["labels"] = new_label_objs
            return True
        except RateLimited:
            raise
        except GitHubError as err:
            _say("failed to withdraw stale ask on %s#%d: %s" % (repo, number, err))
            return False

    def _issue_and_comment_texts(self, repo: str, issue: Dict[str, Any]) -> List[str]:
        number = int(issue.get("number") or 0)
        comments = self._get_issue_comments(repo, number)
        texts = [str(c.get("body") or "") for c in comments if isinstance(c, dict)]
        body = str(issue.get("body") or "")
        if body:
            texts.append(body)
        return texts

    def _is_commit_deployed(self, repo: str, sha: str, deployed_commits: Set[str]) -> bool:
        return (sha in deployed_commits
                or any(d.startswith(sha) or sha.startswith(d) for d in deployed_commits)
                or self._check_commit_live(repo, sha))

    def _withdraw_if_commit_live(self, repo: str, issue: Dict[str, Any],
                                 texts: List[str], deployed_commits: Set[str]) -> bool:
        for text in texts:
            m = self.STALE_ASK_PATTERN.search(text)
            if not m:
                continue
            sha = m.group(1).lower()
            if self._is_commit_deployed(repo, sha, deployed_commits):
                return self._withdraw_stale_ask(repo, issue, "commit %s is live" % sha[:12])
        return False

    def _withdraw_if_issue_exists(self, repo: str, issue: Dict[str, Any],
                                  texts: List[str], deployed_issues: Set[Tuple[str, int]]) -> bool:
        if not any(self.ISSUE_EXISTS_PATTERN.search(text) for text in texts):
            return False
        for dep_repo, dep_num in deployed_issues:
            if self._check_issue_exists(dep_repo, dep_num):
                return self._withdraw_stale_ask(repo, issue, "issue %s exists" % dep_num)
        return False

    def _process_stale_ask_issue(self, issue: Dict[str, Any],
                                 deployed_commits: Set[str],
                                 deployed_issues: Set[Tuple[str, int]]) -> bool:
        if issue.get("state") != "open":
            return False
        labels = self._issue_label_names(issue)
        if not any(self.NEEDS_LABEL_PATTERN.match(lbl) for lbl in labels):
            return False

        repo = issue.get("repository", {}).get("full_name") or (self.s.repos[0] if self.s.repos else "")
        if not self._has_ask_comment(issue):
            return self._withdraw_stale_ask(repo, issue, "no ask comment")

        texts = self._issue_and_comment_texts(repo, issue)
        return (self._withdraw_if_commit_live(repo, issue, texts, deployed_commits)
                or self._withdraw_if_issue_exists(repo, issue, texts, deployed_issues))

    def _check_and_withdraw_stale_asks(self, issues: List[Dict[str, Any]],
                                       deployed_commits: Set[str],
                                       deployed_issues: Set[Tuple[str, int]],
                                       last_check: float) -> int:
        """Check all issues with needs:<login> labels and withdraw stale ones.
        Returns the number of asks withdrawn as stale."""
        withdrawn = 0
        for issue in issues:
            if self._process_stale_ask_issue(issue, deployed_commits, deployed_issues):
                withdrawn += 1
        return withdrawn

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
            for goal in self._paginate(
                    "/repos/%s/issues?labels=loom:goal&state=open" % repo):
                if not isinstance(goal, dict) or "number" not in goal:
                    continue
                number = int(goal["number"])
                # A journal edited in place is newest at its newest Tick.
                for c in handoff.expand_journals(self._paginate(
                        "/repos/%s/issues/%d/comments?since=%s" % (repo, number, since)),
                        at_key="created_at"):
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
        except RateLimited:
            raise
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

    def _readable_goal_homes(self) -> List[Tuple[str, Dict[str, Any]]]:
        """(repo, issue) for every open loom:goal issue in the LOOM_REPOS
        repos that can be read. A repo whose goal list fails is skipped with
        one log line (loom#1071); only a complete list is kept for
        _goal_homes, which must never route a note from a partial one."""
        if self._goals is not None:
            return self._goals
        homes: List[Tuple[str, Dict[str, Any]]] = []
        complete = True
        for repo in self.s.repos:
            if not tickets.REPO_RE.fullmatch(repo):
                continue
            try:
                homes += [(repo, issue) for issue in self._goal_issues(repo)]
            except RateLimited:
                raise
            except GitHubError as err:
                _say("standup: skipped %s: %s" % (repo, err))
                complete = False
        if complete:
            self._goals = homes
        return homes

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
        made = notify.post(self.gh, repo, number, OPERATOR_MARKER % msg_id + "\n" + note,
                           notify.RECORD).data
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
            for c in self._paginate("/repos/%s/issues/%d/comments?since=%s"
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
        runner = self._factory()
        runner.run()
        self._record_waits(runner.waits, runner.settled)
        self._record_silent(runner.silent_running, runner.silent)
        self._return_silent_briefs(runner.silent_running)

    def lift_brain_holds(self) -> None:
        """Resolve the needs-brain holds older than a day that Loom set
        (bus/factory.py brain_holds, loom#1872), at most
        brain_hold.LIFT_PER_RUN a run and a run at most every
        brain_hold.LIFT_EVERY seconds, and send the dashboard each repo's
        count. The run is stamped however it ends: stamped only on success,
        a capped or failing run would repeat every pass (2026-10-20, 91
        sources in minutes). LOOM_FACTORY=0 turns it off."""
        if not self.s.factory_on or self._deferred():
            return
        path = os.path.join(self.state, BRAIN_HOLDS_FILE)
        state = _read_json(path, {}, strict=False)
        state = state if isinstance(state, dict) else {}
        last = state.get("_at")
        if isinstance(last, (int, float)) and 0 <= self.now - last < brain_hold.LIFT_EVERY:
            return
        report: Dict[str, Dict[str, int]] = {}
        try:
            report = self._factory().brain_holds(brain_hold.LIFT_PER_RUN)
        finally:
            state["_at"] = self.now
            _write_json(path, state)
        lifted = sum(c.get("lifted", 0) for c in report.values())
        left = sum(c.get("old", 0) for c in report.values()) - lifted
        if lifted or left:
            _say("needs-brain holds past a day: resolved %d, %d left (%d a person set, %d by "
                 "an unknown setter)" % (lifted, left,
                                         sum(c.get(brain_hold.PERSON, 0) for c in report.values()),
                                         sum(c.get(brain_hold.UNKNOWN, 0) for c in report.values())))
        self._post_brain_holds(report)

    def _post_brain_holds(self, report: Dict[str, Dict[str, int]]) -> None:
        """Send the dashboard (POST BRAIN_HOLDS_PATH) each repo's holds older
        than a day that this run left; a failed send waits for the next run."""
        if not (self.s.transcript_url and self.s.transcript_token_ssm):
            return
        token = self._transcript_token()
        if token is None:
            return
        rows = brain_hold.report_rows({repo: dict(c, old=c.get("old", 0) - c.get("lifted", 0))
                                       for repo, c in report.items()})
        try:
            self.upload(brain_memory.dashboard_url(self.s.transcript_url, BRAIN_HOLDS_PATH), token,
                        {"repos": rows, "at": self.now})
        # Deliberately broad, as in post_stuck_goals: only the type is logged.
        except Exception as err:  # noqa: BLE001
            _say("could not send the needs-brain hold counts (%s); next run"
                 % type(err).__name__)

    def post_chain(self) -> None:
        """Send the dashboard (POST CHAIN_PATH) each watched repo's shadow
        mode and the last CHAIN_ROWS held and acted entries, at most every
        CHAIN_EVERY seconds. Read from bus.shadow and local files alone: no
        GitHub request. The send is stamped however it ends, and a failed
        one waits for the next run."""
        if not (self.s.transcript_url and self.s.transcript_token_ssm):
            return
        path = os.path.join(self.state, CHAIN_FILE)
        stamp = _read_json(path, {}, strict=False)
        last = stamp.get("at") if isinstance(stamp, dict) else None
        if isinstance(last, (int, float)) and 0 <= self.now - last < CHAIN_EVERY:
            return
        _write_json(path, {"at": self.now})
        token = self._transcript_token()
        if token is None:
            return
        held = [{"action": e.get("action"), "repo": e.get("repo"), "number": e.get("number"),
                 "at": e.get("time"), "source": e.get("source")}
                for e in shadow.read(shadow.ledger_path())[-CHAIN_ROWS:]]
        acted = [{key: e.get(key) for key in ("action", "repo", "number", "at", "source")}
                 for e in shadow.read_acted(CHAIN_ROWS)]
        body = {"at": self.now, "shadow_on": shadow.scope()[0],
                "repos": [{"repo": repo, "shadow": shadow.active(repo)} for repo in self.s.repos],
                "held": held, "acted": acted}
        try:
            self.upload(brain_memory.dashboard_url(self.s.transcript_url, CHAIN_PATH), token, body)
        # Deliberately broad, as in _post_brain_holds: only the type is logged.
        except Exception as err:  # noqa: BLE001
            _say("could not send the command chain snapshot (%s); next run" % type(err).__name__)

    def _silent_lanes(self) -> Dict[str, float]:
        """Local lane -> seconds since its last heartbeat, for each lane
        without a live one (_heartbeat_ok false) that has ever reported and
        whose runner does not hold its lock with a live pid. A lane deep in a
        long run writes no heartbeat until the run ends: counted silent, two
        spark runs had their tickets re-queued and lane labels removed while
        they ran, and their finished pull requests were left with no lane
        (2026-10-19). A runner also stamps <lane>.alive every minute while it
        lives, so a fresh stamp keeps a lane from being read as silent even
        when its lock cannot be read as a live runner (loom#1281)."""
        out = {}
        for lane in LANES:
            if (lane in REMOTE_LANES or self._heartbeat_ok(lane)
                    or self._runner_alive(lane) or self._keepalive_fresh(lane)):
                continue
            beat = _read_json(os.path.join(self.s.home, "state", "usage", lane + ".json"),
                              None, strict=False)
            at = beat.get("at") if isinstance(beat, dict) else None
            if self._num(at):
                out[lane] = max(0.0, self.now - at)
        return out

    def _record_silent(self, found: Dict[str, List[Tuple[str, int]]],
                       silent: Dict[str, float]) -> None:
        """Keep the lanes the factory found holding a running ticket while
        silent for the invariant check (invariants.SILENT_KEEP), which may
        come passes after the move."""
        if not found:
            return
        path = os.path.join(self.state, "silent-lanes.json")
        saved = _read_json(path, {}, strict=False)
        saved = saved if isinstance(saved, dict) else {}
        for lane, held in found.items():
            saved[lane] = {"at": self.now, "silent_for": silent[lane],
                           "tickets": ["%s#%d" % item for item in held]}
            _say("lane %s held %s while silent for %d minutes; runner seen as: %s"
                 % (lane, ", ".join("%s#%d" % item for item in held),
                    silent[lane] // 60, self._runner_state(lane)))
        _write_json(path, saved)

    def _return_silent_briefs(self, found: Dict[str, List[Tuple[str, int]]]) -> None:
        """Move the still-unpicked briefs of tickets the factory just moved
        off silent lanes out of those lanes' inboxes into returned/ (which
        nothing reads). The factory's silent-lane move relabels the ticket
        but leaves its brief behind; a revived runner would otherwise pick up
        the stale brief and work a ticket it no longer holds (loom#1206). A
        brief the runner already picked up sits in done/ or failed/, not the
        inbox, so only stale unpicked briefs move."""
        for lane, held in found.items():
            inbox = self.inbox(lane)
            try:
                files = os.listdir(inbox)
            except OSError:
                continue
            wanted = []
            for repo, number in held:
                base = tickets.ticket_name(repo, number)
                pattern = re.compile(re.escape(base) + r"(-r[0-9]+)?\.md\Z")
                wanted.extend(name for name in files if pattern.fullmatch(name))
            if not wanted:
                continue
            returned = os.path.join(inbox, "returned")
            try:
                os.makedirs(returned, exist_ok=True)
                for name in wanted:
                    os.replace(os.path.join(inbox, name),
                               os.path.join(returned, name))
            except OSError:
                continue
            _say("returned %d stale brief(s) from silent lane %s"
                 % (len(wanted), lane))

    def _invariant_silent(self) -> List[invariants.SilentRun]:
        """The lanes _record_silent kept, their tickets named for a public
        issue."""
        saved = _read_json(os.path.join(self.state, "silent-lanes.json"), {}, strict=False)
        out = []
        for lane, info in sorted(saved.items() if isinstance(saved, dict) else []):
            if not (isinstance(info, dict) and self._num(info.get("at"))
                    and self._num(info.get("silent_for"))):
                continue
            refs = []
            for key in info.get("tickets") or []:
                match = re.fullmatch(r"(.+)#([0-9]+)", str(key))
                if match:
                    refs.append(self._invariant_ref(match.group(1), int(match.group(2))))
            out.append(invariants.SilentRun(lane, info["silent_for"], tuple(refs), info["at"]))
        return out

    def assign_queued(self) -> None:
        """Give ready queued tickets a free lane after this pass's claims."""
        if self.s.factory_on:
            runner = self._factory()
            runner.assign_queued()
            self._record_waits(runner.waits, runner.settled)

    def _record_waits(self, found: Dict[str, str], settled: Iterable[str] = ()) -> None:
        """Time each ticket the factory left where it is: a reason it repeats
        keeps its start, a new reason starts again, and a ticket it has not
        named for WAIT_UNSEEN is dropped (invariants.ticket_waiting). A ticket
        waiting with no lane that takes it (factory.no_lane) also keeps
        no_lane_since, the first time it so waited, across its no-lane
        reasons; a ticket whose wait the factory settled (a lane took it, a
        reroute, the hand-off pool) is dropped (loom#1831)."""
        path = os.path.join(self.state, "ticket-waits.json")
        saved = _read_json(path, {}, strict=False)
        saved = saved if isinstance(saved, dict) else {}
        dropped = [key for key in settled if saved.pop(key, None) is not None]
        for key, reason in found.items():
            old = saved.get(key)
            if isinstance(old, dict) and old.get("reason") == reason and self._num(old.get("since")):
                old["seen"] = self.now
            else:
                saved[key] = {"reason": reason, "since": self.now, "seen": self.now}
            entry, kept_since = saved[key], old.get("no_lane_since") if isinstance(old, dict) else None
            if factory.no_lane(reason):
                entry["no_lane_since"] = kept_since if self._num(kept_since) else self.now
            else:
                entry.pop("no_lane_since", None)
        kept = {key: info for key, info in saved.items() if isinstance(info, dict)
                and self._num(info.get("seen")) and self.now - info["seen"] <= WAIT_UNSEEN}
        if kept or saved or dropped:
            _write_json(path, kept)

    def _invariant_waits(self) -> List[invariants.Wait]:
        """The ticket waits _record_waits kept, each ticket named for a
        public issue."""
        saved = _read_json(os.path.join(self.state, "ticket-waits.json"), {}, strict=False)
        out = []
        for key, info in sorted(saved.items() if isinstance(saved, dict) else []):
            match = re.fullmatch(r"(.+)#([0-9]+)", str(key))
            if not (match and isinstance(info, dict) and self._num(info.get("since"))
                    and isinstance(info.get("reason"), str)
                    and WAIT_REASON_RE.fullmatch(info["reason"])):
                continue
            out.append(invariants.Wait(self._invariant_ref(match.group(1), int(match.group(2))),
                                       info["reason"], info["since"]))
        return out

    def refresh_bench(self) -> None:
        """Read the dashboard's bench list (GET /api/bench, loom#969) with the
        transcript upload settings and token, and keep it in state/station/
        bench.json stamped with the time read. A dashboard that does not
        answer leaves the last list, which stops counting after
        bench.LIST_TTL, so an unreachable dashboard never benches a model for
        ever. A change is logged."""
        if not (self.s.transcript_url and self.s.transcript_token_ssm):
            return
        token = self._transcript_token()
        if token is None:
            return
        try:
            listed = self.fetch(brain_memory.dashboard_url(self.s.transcript_url, bench.LIST_PATH), token)
        # fetch's errors never carry the URL or the token.
        except OSError as err:
            _say("bench list: the dashboard did not answer (%s); keeping the last list" % err)
            return
        if not isinstance(listed.get("benched"), list):
            _say("bench list: the dashboard's answer holds no benched list; keeping the last list")
            return
        path = os.path.join(self.state, "bench.json")
        before = set(self.benched_pairs())
        _write_json(path, dict(listed, fetched_at=self.now))
        after = set(self.benched_pairs())
        for provider, model in sorted(after - before):
            _say("benched for everyone: %s %s" % (provider, model))
        for provider, model in sorted(before - after):
            _say("no longer benched: %s %s" % (provider, model))

    def _bench_list(self) -> Any:
        return _read_json(os.path.join(self.state, "bench.json"), None, strict=False)

    def score_lanes(self) -> None:
        """Refresh the lane scorecard once an hour and demote the lanes whose
        bad outcomes reach LOOM_LANE_DEMOTE_AT (bus/scorecard.py). Runs with
        the factory; LOOM_FACTORY=0 turns it off."""
        card = scorecard.Scorecard(self.state, self.now)
        if not self.s.factory_on or not card.due():
            return
        repos = [r for r in self.s.repos if tickets.REPO_RE.fullmatch(r)]
        try:
            failed = audit.held(self.gh, self._verify_repos(), self.s.authors, self.now)
        except GitHubError as err:
            _say("scorecard: the failed proof audits could not be read: %s" % err)
            failed = {}
        ledger = _read_json(os.path.join(self.state, deploy_watch.LEDGER), {}, strict=False)
        merges = ledger.get("merges") if isinstance(ledger, dict) else None
        card.refresh(self.gh, repos, self.s.authors, self.s.review_bot, failed,
                     merges if isinstance(merges, dict) else {})
        card.save()

    def _not_demoted(self, free: List[str]) -> List[str]:
        """The free lanes that may claim a build ticket: a demoted lane
        takes none (bus/scorecard.py)."""
        demoted = self.demoted_lanes()
        return [lane for lane in free if lane not in demoted]

    def demoted_lanes(self) -> Dict[str, str]:
        """Each lane slot the scorecard demoted, and why. A scorecard file
        that cannot be read demotes every lane: its demotions are unknown."""
        card = scorecard.Scorecard(self.state, self.now)
        if card.unreadable:
            _say("every lane takes no build ticket: %s" % card.unreadable)
            return {lane: card.unreadable for lane in LANES}
        return scorecard.demoted_slots(card.demoted(), LANES)

    def benched_pairs(self) -> List[Tuple[str, str]]:
        """The (provider, model) pairs the bench list holds benched now."""
        return bench.benched_pairs(self._bench_list(), self.now)

    def benched_lanes(self, pairs: Sequence[Tuple[str, str]]) -> List[str]:
        """The lanes whose configured model (LOOM_<LANE>_MODEL in this
        process) is benched on them: a ticket with no Model: line runs that
        model, so these lanes step aside like a lane that is down."""
        return [lane for lane in LANES if bench.is_benched(pairs, lane, brain.lane_model(lane))]

    def _factory(self) -> factory.Factory:
        repos = [r for r in self.s.repos if tickets.REPO_RE.fullmatch(r)]
        ask = scope_fit.asker(self.s.scope_url, self.s.scope_model)
        # Passed only when the check is on, so the factory's default stays off.
        extra = {"scope": ask} if ask is not None else {}
        pairs = self.benched_pairs()
        return factory.Factory(self.gh, repos, self.now, trusted=self.s.authors,
                               briefers=self.s.briefers,
                               review_check=self.s.review_check,
                               review_bot=self.s.review_bot,
                               plan_check=self.s.plan_check,
                               limited=self.limited_lanes(),
                               model_limits=self.model_limits(),
                               no_test_repos=self.s.no_test_repos,
                               available=[x for x in LANES if self._heartbeat_ok(x)],
                               held=list(self._held_tickets()) + self._gate_held(),
                               silent=self._silent_lanes(),
                               down=self.down_lanes() + self.benched_lanes(pairs),
                               demoted=self.demoted_lanes(),
                               benched=pairs,
                               limit_until=self.limit_until(),
                               known_waits=self._saved_waits(),
                               no_lane_since=self._no_lane_since(),
                               review_brief=self._write_review_brief, **extra)

    def _no_lane_since(self) -> Dict[str, float]:
        """Each ticket's first no-lane wait as _record_waits kept it."""
        saved = _read_json(os.path.join(self.state, "ticket-waits.json"), {}, strict=False)
        return {str(key): float(info["no_lane_since"]) for key, info in
                (saved.items() if isinstance(saved, dict) else [])
                if isinstance(info, dict) and self._num(info.get("no_lane_since"))}

    def _saved_waits(self) -> Dict[str, str]:
        """Each ticket's wait reason as the last pass recorded it (_record_waits)."""
        saved = _read_json(os.path.join(self.state, "ticket-waits.json"), {}, strict=False)
        return {str(key): info["reason"] for key, info in
                (saved.items() if isinstance(saved, dict) else [])
                if isinstance(info, dict) and isinstance(info.get("reason"), str)}

    def watch_default_branches(self) -> None:
        """Fix forward after a red lane merge, then revert if it stays red.
        Runs with the factory; LOOM_FACTORY=0 turns both off."""
        if not self.s.factory_on:
            return
        repos = [r for r in self.s.repos if tickets.REPO_RE.fullmatch(r)]
        revert.Reverts(self.gh, repos, self.now, trusted=self.s.authors,
                       review_check=self.s.review_check, lanes=self._factory,
                       state_dir=self.state).run()

    def watch_deploys(self) -> None:
        """Watch the deploy of each Loom auto-merge for LOOM_POST_MERGE_WATCH
        minutes and revert one whose deploy fails (bus/deploy_watch.py).
        Runs with the factory; LOOM_FACTORY=0 turns it off."""
        if not self.s.factory_on:
            return
        repos = [r for r in self.s.repos if tickets.REPO_RE.fullmatch(r)]
        deploy_watch.Watch(self.gh, repos, self.now, trusted=self.s.authors,
                           state_dir=self.state, page=self._page_operator).run()
        self._check_stale_asks(0, force=True)

    def _page_operator(self, message: str) -> Optional[bool]:
        """Send one message to the operator, with the mention: True when it
        went out, False to try again next pass, None with no webhook set."""
        if not self.s.webhook_ssm:
            return None
        url = self._webhook()
        if url is None:
            return False
        return self._send(url, message, mention=True)

    def _dated_metrics(self, path: str) -> Dict[str, Tuple[int, int]]:
        dated = {}
        allowed = {author.lower() for author in self.s.authors}
        for comment in self._paginate(path):
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
        except RateLimited:
            raise
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
                 if not naming.is_backlog_home(i)]
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
            except RateLimited:
                raise
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
        comments = [c for c in self._paginate(
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
            made = notify.post(self.gh, home[0], home[1], body, notify.RECORD)
            data = made.data if isinstance(made.data, dict) else {}
            packet = {"id": int(data["id"])} if "id" in data else None
        if packet is not None:
            packet["body"] = body
        entry["packet"] = packet
        _say("handoff packet updated on %s#%d" % home)

    def _stored_handoff(self) -> Optional[Dict[str, Any]]:
        """The dashboard's handoff packet (brain_memory.fetch_handoff), read
        with the transcript upload settings and token, when it answers with
        at least one tick; else None, logged, and the packet is built from
        the goal homes."""
        if not (self.s.transcript_url and self.s.transcript_token_ssm):
            return None
        token = self._transcript_token()
        if token is None:
            _say("handoff packet: built from the goal homes, no dashboard token")
            return None
        try:
            stored = self.fetch(brain_memory.dashboard_url(self.s.transcript_url,
                                                           brain_memory.HANDOFF_PATH), token)
        # fetch_handoff's errors never carry the URL or the token.
        except OSError as err:
            _say("handoff packet: built from the goal homes, the dashboard did not answer (%s)"
                 % err)
            return None
        rows = [stored.get(key) for key in ("ticks", "commitments", "rules")]
        if not (all(isinstance(r, list) and all(isinstance(x, dict) for x in r) for r in rows)
                and rows[0]):
            _say("handoff packet: built from the goal homes, the dashboard's packet holds no ticks")
            return None
        return stored

    def _handoff_body(self, repo: str, homes: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        """The packet text and the goal homes to keep for the next build:
        from the dashboard's brain memory when it answers (handoff.
        stored_packet), which reads no goal home and keeps the last build's,
        else from the goal homes' journals (handoff.packet)."""
        rules = self._handoff_rules(repo)
        stored = self._stored_handoff()
        if stored is not None:
            return handoff.stored_packet(stored, rules, lambda r: self._shown(r, repo)), dict(homes)
        goals, unread, kept = self._handoff_goals(repo, homes)
        return handoff.packet(goals, brain.tick_step(self.gh, repo), rules,
                              datetime.fromtimestamp(self.now, timezone.utc),
                              lambda r: self._shown(r, repo), unread), kept

    def publish_handoff(self) -> None:
        """Keep the handoff packet in one comment on the Loom goal home,
        marked HANDOFF_MARKER: posted once, then edited in place, and only
        when its text changed. It is rendered from the dashboard's brain
        memory, or built from the goal homes when the dashboard does not
        answer (_handoff_body). It is rebuilt at most every HANDOFF_EVERY
        seconds, or sooner when a brain tick just ended, so a pass does not
        read every goal home's comments."""
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
        body, kept = self._handoff_body(repo, homes)
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
        attempt makes again. A tree whose ticket is done (its pull request
        merged or closed, or the ticket closed or dropped, as this pass's
        reconcile read it) goes at once with its check copy and the runner's
        files beside it, unless a brief of that ticket is still in an inbox
        (loom#1808); so does an idle lane's run scratch from that ticket."""
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
        busy = {t for t in (self._tree_ticket(n) for n in live) if t is not None}
        self._prune_run_scratch(busy)
        self._prune_tree_root(os.path.join(self.s.home, "worktrees"), live, busy)
        if self.s.agy_workdir:
            self._prune_tree_root(self.s.agy_workdir, live, busy, tickets_only=True)

    def _prune_tree_root(self, root: str, live: Set[str], busy: Set[Tuple[str, int]],
                         tickets_only: bool = False) -> None:
        """Remove the finished trees in one lane tree folder. The folder is
        opened once, never through a symlink, and every entry is listed and
        removed relative to that open folder, so nothing resolves the path
        again. tickets_only (the agy folder, in another user's home) also
        refuses a path with a symlink in any part and removes only entries
        named for a watched repo's ticket."""
        if tickets_only and os.path.realpath(root) != os.path.abspath(root):
            _say("lane tree folder %s passes through a symlink, so nothing in it is removed"
                 % root)
            return
        try:
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return
        except OSError as err:
            _say("cannot open %s to remove finished lane trees (a symlink is never "
                 "followed): %s" % (root, err))
            return
        removed = done = 0
        try:
            for name in os.listdir(fd):
                if name in live or (tickets_only and self._tree_ticket(name) is None):
                    continue
                try:
                    st = os.lstat(name, dir_fd=fd)
                except FileNotFoundError:
                    continue
                except OSError as err:
                    _say("cannot stat lane tree %s: %s" % (name, err))
                    continue
                finished = self._tree_done(name, busy)
                if not finished and self.now - st.st_mtime < TREE_KEEP:
                    continue
                if stat.S_ISDIR(st.st_mode):
                    shutil.rmtree(name, ignore_errors=True, dir_fd=fd)
                else:
                    os.unlink(name, dir_fd=fd)
                removed += 1
                done += finished
        except OSError as err:
            _say("cannot list %s to remove finished lane trees: %s" % (root, err))
        finally:
            os.close(fd)
        if removed:
            _say("removed %d finished lane tree(s) from %s%s" % (
                removed, root, " (%d of tickets done)" % done if done else ""))

    def _tree_ticket(self, name: str) -> Optional[Tuple[str, int]]:
        """(repo, number) of a lane tree or brief name (tickets.ticket_name,
        with its -rN, -verify-N, -default-check or .summary.md tail), the
        longest matching watched repo first; None for any other name."""
        for repo in sorted(self.s.repos, key=len, reverse=True):
            prefix = repo.replace("/", ".") + "-"
            if name.startswith(prefix):
                match = re.match(r"([0-9]+)(?=$|[-.])", name[len(prefix):])
                if match:
                    return repo, int(match.group(1))
        return None

    def _tree_done(self, name: str, busy: Set[Tuple[str, int]]) -> bool:
        """The tree's ticket is in a repo this pass read and is no longer an
        open ticket that may take a fix round, and no brief of it is in an
        inbox."""
        ticket = self._tree_ticket(name)
        if ticket is None or ticket in busy:
            return False
        repo, number = ticket
        return repo in self._open_work and number not in self._open_work[repo]

    def _prune_run_scratch(self, busy: Set[Tuple[str, int]]) -> None:
        """Tend each idle lane's run scratch (run/<slot>-home, -check,
        -check-default). Idle: no runner lock with a live pid, no brief in
        its inbox, and the folder unchanged for RUN_SCRATCH_SETTLE. Its
        home's package caches and temp files (RUN_HOME_CACHES) go every pass:
        on 2026-10-25 single homes held 3.3 GB of .cache, 1.8 GB of .cargo
        and 1.3 GB of tmp. The whole scratch goes once the ticket the lane
        last ran is done (loom#1808). Anything else in a home is kept."""
        run = os.path.join(self.s.home, "run")
        removed = caches = 0
        for slot in LANES:
            if not self._lane_idle(run, slot):
                continue
            last = self._last_finished(slot)
            finished = last is not None and self._tree_done(last, busy)
            for suffix in ("-home", "-check", "-check-default"):
                path = os.path.join(run, slot + suffix)
                try:
                    st = os.lstat(path)
                except OSError:
                    continue
                if self.now - st.st_mtime < RUN_SCRATCH_SETTLE:
                    continue
                if finished:
                    _remove(path)
                    removed += 1
                elif suffix == "-home" and stat.S_ISDIR(st.st_mode):
                    caches += self._drop_home_caches(run, slot, path, st)
        if removed:
            _say("removed %d idle lane run folder(s) of tickets done" % removed)
        if caches:
            _say("removed %d cache folder(s) from idle lane homes" % caches)

    def _lane_idle(self, run: str, slot: str) -> bool:
        """No runner holds the slot and no brief waits in its inbox."""
        if self._lane_locked(run, slot):
            return False
        try:
            return not any(e.endswith(".md") for e in os.listdir(self.inbox(slot)))
        except FileNotFoundError:
            return True
        except OSError:
            return False

    def _drop_home_caches(self, run: str, slot: str, home: str, st: os.stat_result) -> int:
        """Remove RUN_HOME_CACHES from an idle lane's home, checking the lock
        again before each; the home keeps its time, so the settle check still
        reads it as idle on the next pass. The number removed."""
        dropped = 0
        for name in RUN_HOME_CACHES:
            cache = os.path.join(home, name)
            if not os.path.lexists(cache):
                continue
            if self._lane_locked(run, slot):
                break
            _remove(cache)
            dropped += 1
        if dropped:
            os.utime(home, (st.st_atime, st.st_mtime), follow_symlinks=False)
        return dropped

    @staticmethod
    def _lane_locked(run: str, slot: str) -> bool:
        """A runner holds the slot: its lock folder (lanes/run_lane.sh LOCK)
        exists and names a live pid, or no pid yet (a runner just took it)."""
        lock = os.path.join(run, slot + ".lock")
        if not os.path.lexists(lock):
            return False
        try:
            with open(os.path.join(lock, "pid"), encoding="ascii") as fh:
                pid = int(fh.read().strip())
        except (OSError, ValueError):
            return True
        if pid <= 0:
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True
        return True

    def _last_finished(self, slot: str) -> Optional[str]:
        """The name of the brief a lane slot finished last (newest in its
        done/ or failed/), None when it has none."""
        newest: Optional[Tuple[float, str]] = None
        for kind in ("done", "failed"):
            folder = os.path.join(self.inbox(slot), kind)
            try:
                entries = os.listdir(folder)
            except OSError:
                continue
            for entry in entries:
                if not entry.endswith(".md"):
                    continue
                try:
                    mtime = os.lstat(os.path.join(folder, entry)).st_mtime
                except OSError:
                    continue
                if newest is None or mtime > newest[0]:
                    newest = (mtime, entry[:-3])
        return newest[1] if newest else None

    def _disk_free(self) -> Optional[int]:
        """Free bytes on the loom home's disk, read once a pass; None when it
        cannot be read. Below twice the floor, local snapshots are thinned
        first (thin_snapshots) and the free space is read again."""
        if not self._free_read:
            self._free_read = True
            try:
                self._free = shutil.disk_usage(self.s.home).free
            except OSError as err:
                _say("cannot read the free space on the loom home's disk: %s" % err)
                self._free = None
            want = int(2 * self.s.disk_free_min_gb * GB)
            if self._free is not None and self._free < want and self._thin_due():
                ok = _thin_snapshots(self.s.home, want - self._free)
                try:
                    after = shutil.disk_usage(self.s.home).free
                except OSError:
                    after = self._free
                _say("asked macOS to thin local snapshots (%s): %s free before, %s after"
                     % ("done" if ok else "refused", _gb(self._free), _gb(after)))
                self._free = after
        return self._free

    def _thin_due(self) -> bool:
        """Thinning is on and was last asked for THIN_EVERY or more ago; the
        time is recorded now."""
        if not self.s.thin_snapshots:
            return False
        path = os.path.join(self.state, THIN_FILE)
        saved = _read_json(path, {}, strict=False)
        last = saved.get("at") if isinstance(saved, dict) else None
        if self._num(last) and self.now - float(last) < THIN_EVERY:
            return False
        _write_json(path, {"at": self.now})
        return True

    def disk_low(self) -> bool:
        """True while the loom home's disk has less than disk_free_min_gb
        free: the pass claims no new work (loom#1808). The state is kept in
        state/disk-guard.json and said in the log only when it changes, and
        the lane status shows it; a free space that cannot be read keeps the
        last state."""
        path = os.path.join(self.state, DISK_GUARD_FILE)
        saved = _read_json(path, {}, strict=False)
        was = bool(saved.get("low")) if isinstance(saved, dict) else False
        free = self._disk_free()
        floor = self.s.disk_free_min_gb * GB
        low = was if free is None else free < floor
        if free is not None and low != was:
            # Worded so the log watcher (bus/watch.py KEEP_RE) does not count it.
            if low:
                _say("claims paused: only %s free on the loom home's disk (under %s); "
                     "no new work is claimed until it has more" % (_gb(free), _gb(floor)))
            else:
                _say("claims resumed: %s free on the loom home's disk (at least %s)"
                     % (_gb(free), _gb(floor)))
            _write_json(path, {"low": low, "free": free, "floor": floor, "at": self.now})
        return low

    def _disk_status_line(self) -> Optional[str]:
        """The lane status line while claims are paused for disk, else None."""
        saved = _read_json(os.path.join(self.state, DISK_GUARD_FILE), {}, strict=False)
        if not (isinstance(saved, dict) and saved.get("low")):
            return None
        free, floor = saved.get("free"), saved.get("floor")
        if not (self._num(free) and self._num(floor)):
            return "Claims paused: the loom home's disk is low on free space"
        return "Claims paused: %s free on the loom home's disk, under %s" % (_gb(free), _gb(floor))

    def tend_disk(self) -> None:
        """Keep the loom home bounded and say how big it is (loom#643). Every
        DISK_EVERY seconds the lane logs past retention go (prune_logs),
        finished runs' session stores are compressed (compress_sessions), and
        the home's size is logged, by top-level folder. The free space below
        the floor stops claims instead (disk_low)."""
        free = self._disk_free()
        path = os.path.join(self.state, "disk.json")
        saved = _read_json(path, {}, strict=False)
        last = saved.get("at") if isinstance(saved, dict) else None
        if self._num(last) and 0 <= self.now - last < DISK_EVERY:
            return
        self.prune_logs()
        self.compress_sessions()
        sizes: Dict[str, int] = {}
        try:
            names = os.listdir(self.s.home)
        except OSError as err:
            _say("cannot list the loom home to measure it: %s" % err)
            return
        for name in names:
            try:
                key = name if stat.S_ISDIR(os.lstat(os.path.join(self.s.home, name)).st_mode) else "files"
                sizes[key] = sizes.get(key, 0) + _tree_stat(os.path.join(self.s.home, name))[1]
            except OSError:
                continue
        total = sum(sizes.values())
        parts = ", ".join("%s %s" % (k, _gb(v)) for k, v in
                          sorted(sizes.items(), key=lambda kv: (-kv[1], kv[0])))
        _say("loom home holds %s (%s); %s free on its disk"
             % (_gb(total), parts or "empty", "unknown" if free is None else _gb(free)))
        _write_json(path, {"at": self.now, "bytes": total, "free": free, "folders": sizes})

    def compress_sessions(self) -> None:
        """gzip each finished run's opencode session store (SESSION_DB_PARTS
        under logs/<slot>/<run>/) to opencode.db.gz, once no brief of that
        name is waiting or running and its transcript is not waiting for the
        dashboard (_logs_kept). bus/sessions.py reads the compressed store,
        and prune_logs removes it with the run's other logs after
        log_keep_days. The -wal and -shm files go too: the readers open the
        store immutable and never read them. Times are kept, so retention
        still counts from the run."""
        kept = self._logs_kept()
        if kept is None:
            return
        live, pending = kept
        count = before = after = 0
        for slot in LANES:
            folder = os.path.join(self.s.home, "logs", slot)
            try:
                if not stat.S_ISDIR(os.lstat(folder).st_mode):
                    continue
                names = os.listdir(folder)
            except FileNotFoundError:
                continue
            except OSError as err:
                _say("cannot list lane %s's logs to compress session stores: %s" % (slot, err))
                continue
            keep = live | {k.split("/", 1)[1] for k in pending if k.startswith(slot + "/")}
            for name in names:
                if name in keep:
                    continue
                try:
                    sizes = _gzip_session_db(os.path.join(folder, name))
                except OSError as err:
                    _say("cannot compress the session store of %s/%s: %s" % (slot, name, err))
                    continue
                if sizes is not None:
                    count += 1
                    before += sizes[0]
                    after += sizes[1]
        if count:
            _say("compressed %d finished run session store(s): %s to %s"
                 % (count, _gb(before), _gb(after)))

    def _logs_kept(self) -> Optional[Tuple[Set[str], Set[str]]]:
        """(names, slot/name keys) whose lane logs stay however old: every
        brief waiting or running in a lane inbox, and, while transcripts are
        uploaded, every finished run the ledger does not hold as settled.
        None when an inbox or the ledger cannot be read: remove nothing."""
        live: Set[str] = set()
        for lane in LANES:
            try:
                live |= {e[:-3] for e in os.listdir(self.inbox(lane)) if e.endswith(".md")}
            except FileNotFoundError:
                continue
            except OSError as err:
                _say("cannot list lane %s's inbox, so no lane log is removed: %s" % (lane, err))
                return None
        pending: Set[str] = set()
        if self.s.transcript_url and self.s.transcript_token_ssm:
            ledger = _read_json(os.path.join(self.state, "transcripts.json"), None, strict=False)
            if not isinstance(ledger, dict):
                return None
            for lane, _, _, name in self._finished_briefs():
                rec = ledger.get("%s/%s" % (lane, name))
                if rec is None or (isinstance(rec, int) and not isinstance(rec, bool)):
                    pending.add("%s/%s" % (lane, name))
        return live, pending

    def prune_logs(self) -> None:
        """Remove each entry of a lane's log folder (logs/<slot>/: a run's
        log, result and records, its archived transcripts) whose newest file
        is older than log_keep_days, unless _logs_kept keeps its ticket. The
        station's own logs in logs/ are not touched."""
        if self.s.log_keep_days <= 0:
            return
        kept = self._logs_kept()
        if kept is None:
            return
        live, pending = kept
        cutoff = self.now - self.s.log_keep_days * 24 * 3600
        removed = freed = 0
        for slot in LANES:
            folder = os.path.join(self.s.home, "logs", slot)
            try:
                if not stat.S_ISDIR(os.lstat(folder).st_mode):
                    continue
                names = os.listdir(folder)
            except FileNotFoundError:
                continue
            except OSError as err:
                _say("cannot list lane %s's logs to remove old ones: %s" % (slot, err))
                continue
            keep = live | {k.split("/", 1)[1] for k in pending if k.startswith(slot + "/")}
            for name in names:
                # A run's files are <ticket>, <ticket>.log, <ticket>.result...
                if any(name == t or name.startswith(t + ".") for t in keep):
                    continue
                path = os.path.join(folder, name)
                try:
                    newest, size = _tree_stat(path)
                    if newest >= cutoff:
                        continue
                    _remove(path)
                except OSError as err:
                    _say("cannot remove old lane log %s/%s: %s" % (slot, name, err))
                    continue
                removed += 1
                freed += size
        if removed:
            _say("removed %d lane log(s) older than %d days (%s)"
                 % (removed, self.s.log_keep_days, _gb(freed)))

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

    def _target_reached(self) -> bool:
        target = getattr(self.gh, "target", None)
        if not isinstance(target, int) or isinstance(target, bool):
            return False
        counted = getattr(self.gh, "counted", None)
        if counted is None:
            counted = getattr(self.gh, "requests", 0) - getattr(self.gh, "not_modified", 0)
        return counted >= target

    def _deferred(self) -> bool:
        """True when the budget is low or the pass request target has been
        reached: deferrable steps wait for the reset or next pass."""
        if self._target_reached():
            return True
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

    def _budget_exhausted(self) -> bool:
        """True when the last budget refusal's window is still open: the pass
        does nothing until it resets, and says so in one line (loom#666).
        The window is the client's own, or the one an earlier pass kept."""
        kept = self._budget_state().get("limited_until")
        until = self.gh.limited_until or (kept if isinstance(kept, int) else None)
        if until is None or until <= self.now:
            return False
        self.gh.limited_until = until
        _say("GitHub budget exhausted until %s: this pass waits for the reset"
             % self._utc(until))
        return True

    def end_pass(self) -> None:
        """Log this pass's requests, keep the rate limits for the next pass,
        and remove conditional-request answers nothing uses any more."""
        gh = self.gh
        kinds = ", ".join("%s %d" % (k, n) for k, n in
                          sorted(gh.kinds.items(), key=lambda kv: (-kv[1], kv[0])))
        line = "requests: %d, %d counted (%s), %d answered 304 (not counted)" % (
            gh.requests, gh.requests - gh.not_modified, kinds or "none", gh.not_modified)
        counted = gh.requests - gh.not_modified
        target = getattr(gh, "target", None) or PASS_REQUEST_TARGET
        line += "; target %d counted (%s)" % (
            target, "over" if counted > target else "met")
        carried = ", ".join("%s %d" % (k, n) for k, n in
                            sorted(getattr(gh, "carried_over", {}).items(),
                                   key=lambda kv: (-kv[1], kv[0])))
        if carried:
            line += "; carried over %s" % carried
        rates = self._rates()
        if "core" in rates:
            remaining, limit, reset = rates["core"]
            line += "; %sbudget %d of %d left, resets %s" % (
                "GitHub App " if gh.auth == "app" else "", remaining, limit, self._utc(reset))
        _say(line)
        state = self._budget_state()
        state["rates"] = {r: list(v) for r, v in rates.items()}
        if gh.limited_until is not None and gh.limited_until > self.now:
            state["limited_until"] = int(gh.limited_until)
        else:
            state.pop("limited_until", None)
        if self.budget_low() is None:
            state.pop("told", None)
        try:
            _write_json(os.path.join(self.state, "budget.json"), state)
            if getattr(gh, "comment_edits", False):
                self._ticket_reads.clear()
            for repo, number in getattr(gh, "issue_writes", ()):
                for kind in ("comments", "events"):
                    self._ticket_reads.pop("%s#%d/%s" % (repo, number, kind), None)
            self._ticket_reads = {
                key: value for key, value in self._ticket_reads.items()
                if isinstance(value, dict) and isinstance(value.get("seen"), (int, float))
                and 0 <= self.now - value["seen"] < CACHE_KEEP}
            _write_json(os.path.join(self.state, "ticket_reads.json"), self._ticket_reads)
            gh.prune_cache(CACHE_KEEP, self.now)
        except OSError as err:
            _say("could not keep the GitHub budget: %s" % err)

    def check_repo_access(self) -> bool:
        """Check registered repositories hourly and keep unreadable ones out of this pass."""
        path = os.path.join(self.state, REPO_ACCESS_FILE)
        state = _read_json(path, {}, strict=False)
        if not isinstance(state, dict):
            state = {}
        readable = []
        for repo in self.s.repos:
            record = state.get(repo)
            due = (not isinstance(record, dict) or not isinstance(record.get("checked"), (int, float))
                   or self.now - record["checked"] >= REPO_ACCESS_EVERY)
            if due:
                try:
                    self.gh.rest("GET", "/repos/%s" % repo)
                    record = {"checked": self.now, "readable": True}
                except RateLimited:
                    raise
                except GitHubError as err:
                    if err.status not in (403, 404):
                        if err.status == 401:
                            _say("repo access check could not authenticate: %s" % err)
                            self.s.repos = readable
                            _write_json(path, state)
                            return False
                        record = {"checked": self.now, "readable": True}
                        state[repo] = record
                        readable.append(repo)
                        continue
                    record = {"checked": self.now, "readable": False}
                    _say("Loom App cannot read %s; skipped until access returns" % repo)
                    self._repo_access_ask(repo, state)
                state[repo] = record
            if record.get("readable"):
                readable.append(repo)
                state.pop("_asked:" + repo, None)
        self.s.repos = readable
        _write_json(path, state)
        return True

    def _repo_access_ask(self, repo: str, state: Dict[str, Any]) -> None:
        """File one operator ask per unreadable period in the status repository."""
        key = "_asked:" + repo
        if state.get(key):
            return
        home = self.s.status_repo
        if not home:
            return
        made = self.gh.rest("POST", "/repos/%s/issues" % home, {
            "title": "Give the Loom App access to %s" % repo,
            "body": "## Ask\nGive the Loom App access to `%s` so station steps can run there." % repo,
            "labels": [NEEDS_OPERATOR]}).data
        if not isinstance(made, dict) or "number" not in made:
            raise GitHubError("no operator access ask was created in %s" % home)
        state[key] = int(made["number"])

    def _get_deployed_items(self) -> Tuple[Set[str], Set[Tuple[str, int]]]:
        """Get the set of deployed commits and issue numbers from roll and bridge ship events."""
        deployed_commits: Set[str] = set()
        deployed_issues: Set[Tuple[str, int]] = set()

        try:
            from . import deploy_watch

            ledger_path = os.path.join(self.state, deploy_watch.LEDGER)
            ledger = _read_json(ledger_path, {}, strict=False)

            if isinstance(ledger, dict):
                for entry in ledger.get("merges", {}).values():
                    if isinstance(entry, dict):
                        sha = str(entry.get("sha") or "")
                        if sha:
                            deployed_commits.add(sha.lower())
                            if len(sha) >= 7:
                                deployed_commits.add(sha[:7].lower())
                            if len(sha) >= 12:
                                deployed_commits.add(sha[:12].lower())

                        ticket = entry.get("ticket")
                        repo = str(entry.get("repo") or "")
                        if ticket and repo:
                            try:
                                deployed_issues.add((repo, int(ticket)))
                            except (ValueError, TypeError) as err:
                                _say("invalid ticket in deploy ledger: %s" % err)
        except Exception as err:
            _say("could not read deploy items: %s" % err)

        return deployed_commits, deployed_issues

    def _open_needs_issues(self) -> List[Dict[str, Any]]:
        """Find open issues carrying needs:<login> labels across configured repos."""
        found: List[Dict[str, Any]] = []
        for repo in self.s.repos:
            try:
                items = self._paginate("/repos/%s/issues?state=open&per_page=100" % repo)
                for item in items:
                    if isinstance(item, dict) and "pull_request" not in item:
                        labels = self._issue_label_names(item)
                        if any(self.NEEDS_LABEL_PATTERN.match(lbl) for lbl in labels):
                            if "repository" not in item:
                                item["repository"] = {"full_name": repo}
                            found.append(item)
            except RateLimited:
                raise
            except GitHubError as err:
                _say("could not read issues in %s: %s" % (repo, err))
        return found

    def roll_source_pins(self) -> None:
        if self.s.roll_ship_docs:
            try:
                roll_pin.run(self.gh, self.s.roll_ship_docs, self.s.authors,
                             self.s.review_check)
            except ValueError as err:
                raise GitHubError("invalid roll ship document or pin: %s" % err) from err
            except health.NotLooked as err:
                raise GitHubError("roll family gate could not be read: %s" % err) from err
            self._check_stale_asks(0, force=True)

    def _check_stale_asks(self, last_check: Optional[float] = None, force: bool = False) -> int:
        """Check and withdraw stale needs:login asks after each deploy event and daily.
        Returns the number of asks withdrawn as stale."""
        try:
            check_time = float(last_check) if isinstance(last_check, (int, float)) else 0.0
            if not force and check_time > 0 and self.now - check_time < 86400:
                return 0
            deployed_commits, deployed_issues = self._get_deployed_items()
            issues = self._open_needs_issues()
            withdrawn = self._check_and_withdraw_stale_asks(issues, deployed_commits, deployed_issues, check_time)
            self._stale_asks_withdrawn += withdrawn
            if withdrawn:
                _say("stale ask detection: withdrew %d asks" % withdrawn)
            stale_asks_path = os.path.join(self.state, "stale_asks_check.json")
            saved = _read_json(stale_asks_path, {}, strict=False)
            if not isinstance(saved, dict):
                saved = {}
            saved["at"] = self.now
            saved["withdrawn_today"] = saved.get("withdrawn_today", 0) + withdrawn
            _write_json(stale_asks_path, saved)
            return withdrawn
        except RateLimited:
            raise
        except GitHubError as err:
            _say("stale ask detection failed: %s" % err)
            return 0

    @staticmethod
    def label_digest() -> str:
        payload = json.dumps(labels.LABELS, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def sync_labels(self) -> None:
        """Sync every registered repo when the desired label set changes, and
        a repo enrolled since the last sync on its first pass: one enrolled
        later lacked loom:answered, and a lane could not set it (loom#2032).
        The ledger records the digest and the repos synced at it."""
        path = os.path.join(self.state, "labels.json")
        digest = self.label_digest()
        saved = _read_json(path, {}, strict=False)
        synced: List[str] = []
        if isinstance(saved, dict) and saved.get("digest") == digest:
            synced = [r for r in saved.get("repos") or [] if isinstance(r, str)]
        todo = [repo for repo in self.s.repos if repo not in synced]
        if not todo:
            return
        for repo in todo:
            changed = labels.ensure_labels(self.gh, repo)
            if changed:
                _say("labels changed in %s: %s" % (repo, ", ".join(changed)))
            synced.append(repo)
            _write_json(path, {"digest": digest, "repos": synced})

    def run_pass(self) -> None:
        if self._budget_exhausted():
            return
        if not self.check_repo_access():
            return
        # The local end of the pass runs however the GitHub part ends: a pass
        # that stops at its request target (TargetReached) or a rate limit
        # still prunes lane trees, tends the disk and sends its snapshots.
        # From loom#1915 until this change every such pass skipped them, and
        # no lane tree was pruned for hours on 2026-10-26.
        try:
            self.sync_labels()
            self.finish_claims()
            try:
                self._timed(self.tend_runners)
            except RateLimited:
                raise
            except (GitHubError, OSError, runners.ReportError) as err:
                _say("tend_runners failed: %s" % err)
            self.report_outcomes()
            if not self._deferred():
                try:
                    self._timed(self.reconcile_pull_requests)
                except RateLimited:
                    raise
                except (GitHubError, OSError) as err:
                    _say("pull request reconciliation failed: %s" % err)
            if not self._deferred():
                try:
                    self._timed(self.hand_back)
                except RateLimited:
                    raise
                except (GitHubError, OSError) as err:
                    _say("hand_back failed: %s" % err)
            # A disk under the floor takes no new work (loom#1808); a pass
            # still finishes half-done claims, reconciles and prunes.
            if not self.disk_low():
                self._timed(self.claim_new)
            if not self._deferred():
                try:
                    self._timed(self.assign_queued)
                except RateLimited:
                    raise
                except (GitHubError, OSError) as err:
                    _say("assign_queued failed: %s" % err)
            try:
                self.record_scans()
            except OSError as err:
                _say("could not record the repo scans: %s" % err)
            self._run_steps()
            # The day's metrics line goes out even below the budget floor: one post
            # a day, and on 2026-10-18 the floor deferred it every pass, so the
            # operator got no numbers for the day. Only a spent budget stops it.
            try:
                self.post_metrics()
            except RateLimited:
                raise
            except (GitHubError, OSError, ValueError) as err:
                _say("post_metrics failed: %s" % err)
            try:
                self.watch_logs()
            except RateLimited:
                raise
            except (GitHubError, OSError) as err:
                _say("watch_logs failed: %s" % err)
        finally:
            self._end_pass_locally()

    def _end_pass_locally(self) -> None:
        """Local housekeeping that needs no GitHub request: finished lane
        trees, old lane logs, the disk, the usage snapshot and the command
        chain snapshot."""
        for local in (self.prune_trees, self.tend_disk):
            try:
                local()
            except OSError as err:
                _say("%s failed: %s" % (local.__name__, err))
        try:
            records = usage.collect(self.s.home, now=self.now)
            usage.write(self.s.home, records)
            self.upload_usage_limits(records)
        except OSError as err:
            _say("could not write usage limits: %s" % err)
        try:
            self.post_chain()
        except OSError as err:
            _say("could not send the command chain snapshot: %s" % err)

    def tend_runners(self) -> None:
        """The slots in LOOM_RUNNER_SLOTS: dispatch their briefs to cluster
        runners and write back what each run reported (bus/runners.py)."""
        slots = runners.runner_slots(os.environ)
        if not slots:
            return
        repo = os.environ.get("LOOM_RUNNER_REPO", "").strip() or self.s.self_repo
        try:
            actor = self.gh.app_login()
        except RateLimited:
            raise
        except GitHubError as err:
            _say("could not read the station App's login for its runner slots: %s" % err)
            actor = None
        runners.Runners(self.gh, self.s.home, repo, slots, dict(os.environ), self._public, _say,
                        actor=actor, now=self.now).tend()

    def upload_usage_limits(self, records: List[Dict[str, Any]]) -> None:
        """Send the current limits snapshot using the transcript upload token."""
        if not (self.s.transcript_url and self.s.transcript_token_ssm):
            return
        url = brain_memory.dashboard_url(self.s.transcript_url, "/api/usage")
        token = self._transcript_token()
        if token is None:
            return
        for attempt in (1, 2):
            try:
                self.upload(url, token, records)
                return
            except UploadRefused as err:
                _say("the dashboard refused the usage snapshot (%d)" % err.status)
                return
            except Exception as err:  # noqa: BLE001
                if attempt == 1:
                    # A dashboard rollout or a slow first snapshot drops one
                    # upload; the second try usually lands (loom#345).
                    time.sleep(USAGE_RETRY_PAUSE)
                    continue
                # post_transcript's messages carry neither URL nor token; the
                # token is redacted anyway in case another uploader names it.
                _say("usage snapshot upload failed twice: %s: %s"
                     % (type(err).__name__, str(err).replace(token, "[token]")))


def _trust_app_login(settings: Settings, gh: GitHub) -> None:
    """Trust the Loom App's own bot login wherever LOOM_BRIEF_AUTHORS is, so a
    credential change cannot leave the station refusing its own labels
    (loom#660), and log the logins trusted. A login that cannot be read is
    logged, and the configured logins stay as they were."""
    try:
        login = gh.app_login()
    except RateLimited:
        raise
    except GitHubError as err:
        _say("could not read the GitHub App's login, so it is not trusted: %s" % err)
        login = None
    if login and login.lower() not in {a.lower() for a in settings.authors}:
        settings.authors = list(settings.authors) + [login]
    _say("trusting briefing logins: %s" % " ".join(
        tickets.briefing_logins(settings.authors, settings.briefers)))


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
        deadline = _try_arm_pass_deadline(state, settings.pass_deadline, time.time())
        try:
            return _locked_pass(settings, state, gh, hooks)
        finally:
            _disarm_pass_deadline(deadline)
    finally:
        os.close(fd)


def _locked_pass(settings: Settings, state: str, gh: Optional[GitHub],
                 hooks: Dict[str, Any]) -> int:
    """The pass run() starts once it holds the lock."""
    if gh is None:
        from .github import auth_from_env
        gh = GitHub(auth_from_env())
        gh.status_in_body = True
    _trust_app_login(settings, gh)
    if gh.cache_dir is None:
        # Every GET is conditional: a list re-read each pass costs a 304.
        gh.cache_dir = os.path.join(state, "station", "etags")
    app = getattr(gh, "app", None)
    if app is not None and getattr(app, "state_file", None) is None:
        # GitHub's ETags differ per token: keep the App token across
        # passes (0600) so last pass's ETags still answer 304 (loom#1306).
        app.state_file = os.path.join(state, "station", "app-token.json")
    st = Station(gh, settings, **hooks)
    try:
        st.run_pass()
    except (RateLimited, TargetReached) as err:
        _say("%s; the pass stopped there" % err)
    except StateError as err:
        _say("stopping: %s" % err)
        return 1
    finally:
        st.end_pass()
    return 0
