# Lane contracts

Every lane: polls `briefs/<lane>/`, works in a fresh worktree, opens a PR,
never merges. Heartbeats usage + model catalog to `state/usage/<lane>.json`.

## The codex and claude runner

`lanes/run_lane.sh <lane>` makes one pass over `briefs/<lane>/`:

- An optional `Base:` line names the branch to start from and target; the
  default is the remote's default branch.
- Every run passes an explicit model: the brief's `Model:` line, else the
  lane default (`LOOM_<LANE>_MODEL`). The PR footer names the lane and the
  model. A model outside the lane's family is refused; the `openrouter`,
  `zen` and `zai` lanes run free models only.
- A lane may set LOOM_<SLOT>_MODELS to an ordered, space-separated model
  list. Unpinned briefs use the first model not currently limited. A provider
  quota records a reset for that model; the lane remains available while
  another model is ready. A brief with a Model: line always keeps that model.
  Account-wide out-of-credit limits still hold the lane.
- Each pass writes `state/usage/<lane>.json` with `available` and `at`. A lane
  whose CLI or key is missing reports `available: false` and leaves its briefs
  alone; the dispatcher routes only to fresh, available lanes.

## Validation contract

A repo opts in with `.loom/validation.conf`, the same steps its CI runs:

```
# .loom/validation.conf
interpreter: python3.12
submodules: yes
setup: scripts/setup.sh
check: scripts/check.sh
```

`interpreter` is a command that must be on the lane machine's PATH,
`submodules: yes` checks out the pinned submodule revisions, `setup`
prepares the tree (a venv, locked dependencies, a pinned linter) and `check`
is the required check. Point the repo's CI at the same two scripts so the
lane and CI cannot drift apart. Unknown lines are refused.

- The runner reads the file from the base revision before the agent starts
  (on a fix run, from the base branch, not the pull request's branch);
  what the agent edits never changes what is checked. `setup` and `check`
  are shell commands, trusted like a CI workflow's `run:` steps, so they
  come only from the branch a pull request merges into.
- Before the agent: interpreter, submodules, `setup`, then `check` on the
  base. A missing interpreter, a failed submodule checkout or a failed setup
  parks the brief as `setup failed: ...`; the agent does not run. The base
  check is recorded but does not stop the run (a ticket may exist to fix
  it). What setup leaves untracked goes into `.git/info/exclude`, so it is
  never committed.
- The agent is told the check and how it did on the base.
- After the agent: the runner commits, then runs `check` on that commit. It
  passes only with exit 0 and no tracked file changed (a formatter that has
  to rewrite the agent's files is a failure). Only then is the branch pushed
  and the pull request opened, with a `Validation:` line naming the commit.
- A failed check parks the brief as `validation failed: ...` (exit 125 to
  127 as `validation environment unavailable: ...`), opens no pull request
  and pushes nothing to the ticket's branch or an open pull request. The
  edits go to a side branch `<branch>-unvalidated-<time>` so they can be
  recovered.
- Every step runs like an API-lane agent: an emptied environment, a scratch
  HOME (`run/<lane>-check`), under `sandbox-exec`, with network but no
  access to the runner's HOME. A machine without `sandbox-exec` fails setup
  for repos with a contract. Each step has `LOOM_CHECK_TIMEOUT` seconds
  (default 1800).
- `logs/<lane>/<ticket>.validation.jsonl` holds one line per step: phase
  (`prepare`, `setup`, `baseline`, `final`), command, exit code, tree
  revision, `ok` and detail (the interpreter's version, the submodule
  revisions).
- A repo without the file is not validated by the runner; CI still is.

## The brain tick

`lanes/mac/install.sh` also installs `com.loom.brain`, which runs
`lanes/mac/brain.sh` every `LOOM_BRAIN_INTERVAL` seconds (default 1800). A
tick takes the brain lock in `LOOM_STATUS_REPO` (a held lock ends the tick
before any model call), updates its own clone of this repo under
`$LOOM_HOME/brain`, runs `claude -p` (model `LOOM_BRAIN_MODEL`, default the
current Opus) on docs/MUSE.md with a hard stop of `LOOM_BRAIN_MINUTES`
(default 40), and releases the lock, also after a failure. It logs to
`$LOOM_HOME/logs/brain.log`. `LOOM_BRAIN=0` at install removes it. A live tick waits up to `LOOM_BRAIN_WAIT` minutes (default 25) for a lock another brain holds, because it is the only tick that ships. With
`LOOM_BRAIN_SCOPE=live` the tick does only the work that needs the cluster
(ships and rolls, checks that need cluster or database access) and leaves the
rest to Muse (docs/MUSE.md).

Each tick writes `$LOOM_HOME/state/brain.json` (how it ended) and touches
`$LOOM_HOME/state/brain-ok` when it ended well, a skip for a held lock
included. The station adds a `brain` row to the lane status. The brain also
runs on Muse, so the station reads GitHub too: while no local tick has ended
well for 2 hours it looks (at most every 10 minutes) for the newest
`<!-- loom:journal v1 -->` comment from a `LOOM_BRIEF_AUTHORS` login on the
open `loom:goal` issues in `LOOM_REPOS`. The brain is alive when either the
local tick or a journal is at most 2 hours old. Only when both are older does
the station send one urgent Discord message, naming the newest journal's time
and goal, then one more when the brain is back. A GitHub read error never
counts as stale; that pass makes no decision.

## The station

`lanes/mac/install.sh` also installs `com.loom.station`, which runs
`lanes/mac/station.sh` from this checkout every 60 seconds. That script first
deploys Loom itself: it fast-forwards this checkout (which the lanes and the
brain run from too) to its default branch once that head's `unittest` check
passed, never over local changes and never off the default branch
(`LOOM_DEPLOY=0` turns it off), and then runs `python3 -m bus station`. A
merged Loom pull request is running a minute after its CI passes on main,
and the pass after the deploy moves its ticket to `loom:shipped`
(`LOOM_SELF_REPO`, default `LOOM_STATUS_REPO`; docs/BRAIN.md, Shipping Loom
itself).
The station turns trusted
`loom:briefed` ticket issues into briefs in the lane inboxes
(`<owner>.<name>-<number>.md`, with an `Issue:` line), marks the ticket
`loom:running`, and later reports the run from `done/` or `failed/` back to the
ticket, with local paths removed from failure reasons; the runner adds
`Refs <owner/name>#<number>` to the pull request. A limit or transient
failure moves the ticket to another available lane as a new attempt, at most
`LOOM_RETRY_MAX` times (default 2); a failed fix round goes back to
`loom:pr`; a lane that hit its provider limit stays unavailable until the
provider's reset (else 60 minutes; delete its heartbeat file in
`state/usage/` to clear it sooner). It reads `LOOM_REPOS`,
`LOOM_BRIEF_AUTHORS` and `LOOM_REPO_OWNERS` from the environment of the
install (empty `LOOM_REPOS` keeps it idle) and uses `LOOM_GITHUB_TOKEN`,
`GITHUB_TOKEN` or `gh auth token`. docs/BRAIN.md has the rules.

After claims on every pass, the station gives the oldest `loom:queued`
tickets free lanes in their Difficulty tier and moves them to `loom:briefed`.
It skips future `Not before:` times and tickets without a readable Difficulty.
The latter appear in their goal's next station standup.

Lane status: every pass the station reads each lane's heartbeat (`yes` when
available and at most 600 seconds old, else `no`) and keeps one comment on the
open `loom:lanes` issue in `LOOM_STATUS_REPO` (default: this checkout's
`origin`; the issue is created when missing) current. It edits the comment
only when a yes or no changes, when an anthropic run is recorded, and at
least hourly, and never publishes the heartbeat's reason. With anthropic
spend recorded or a budget set, it ends with the spend line ("Anthropic
spend" below).

Hold: a ticket whose header has `Not before: 2026-10-13T17:00:00Z` (UTC) is
not claimed before that time, and the factory does not count it as stalled
until then; the line is left out of the brief the lane reads. A ticket whose
header has `After: #5, owner/name#7` waits for the tickets it builds on: it
is not claimed while any of them is still open without `loom:merged`,
`loom:shipped` or `loom:verified`, and the factory counts it as neither
stalled nor late (one holding it for more than 48 hours is reported to the
brain, like a stuck ticket). A named ticket closed as not planned never
merges, so it stops holding and the station says so in one comment. That line
is left out of the brief too: by the time a lane reads it, it is settled
(docs/BRAIN.md, "When a ticket may run").

Factory: at most every 5 minutes the station runs `bus/factory.py` over
`LOOM_REPOS`, with no model. A lane pull request (a `loom/` branch whose
ticket is `loom:pr`) whose checks have all finished, whose review check
(`LOOM_REVIEW_CHECK`, default `Grug - Elder`) has run on its head commit,
and that is mergeable, green and free of open threads is squash-merged
through `bus.brain merge`. One with a failing check, an open thread or a
conflict goes back to a lane as a fix brief. It stays on its lane while the
rounds make progress, and climbs the model ladder only when stuck: a review
thread survived a fix round, checks it was asked to fix still fail, or the
pull request is over 6 hours old and has not climbed in 6 hours (a review that finds new things, or a
conflict alone, never climbs). Failed tickets and briefed tickets no lane
claimed for 2 hours climb too. After a push the factory waits for the review
to run on the new head before judging it. A `<!-- loom:rung 0 -->` comment
starts a ticket's ladder again. The ladder (claude `claude-sonnet-5-5`, then codex `gpt-6-sol`, then
`claude-opus-5-5`), each climb recorded as a `<!-- loom:rung N -->` comment.
A climb takes the lowest rung the ticket has not run on since its ladder
last started; a rung whose lanes each already hold 2 briefed or running
tickets is passed over for the next untried one, and stays untried, so a
ticket gives up only once it has run on every rung. A ticket that failed on
a ladder model (its `Model:` line and lane) has tried that rung. When every
untried rung is busy, it waits on the least loaded one, and a ladder ticket
queued behind more than 2 others on its lane moves to an untried rung with a
free lane (not a failure).
A fix brief may answer a finding instead of fixing it: the agent writes the
reason to `.loom-answers/<thread id>.md`, the runner moves that folder out of
the tree (never committed; links dropped unread), and the station posts each
answer as the reply on its thread, redacted and text-checked. The station
resolves the thread only when the round ran on a ladder model and the thread
is the review bot's own medium or low finding; any other answered thread
stays open, and the factory reads it as a finding that survived.
A fix brief that ends with nothing committed (most often
every thread was already fixed and the review thread is stale) is not a
failure: the station posts the lane's explanation on the pull request, asks
the review bot to review it again (`LOOM_REVIEW_AGAIN`, default
`/grug improve`), and returns the ticket to `loom:pr`. Before a fix brief's
agent runs, the runner merges the default branch into the pull request's
branch (the agent's sandbox cannot fetch): a clean merge is pushed even when
the agent changes nothing, and a conflict is handed to the agent with the
file list, its markers in the tree; the runner completes the merge, or parks
the brief with "conflict markers left in FILES".
A lane at a limit (its heartbeat reason starts `limit:`, as the spark lane's
does while its box is hot) is not failing: a ticket briefed on it moves at
once to a free lane in its difficulty tier (docs/MUSE.md, Lane choice), with
no rung marker, or a ticket on a ladder rung to a free lane of that rung; with
none free it waits where it is for its lane to come back. A failed or stalled
ticket climbs, and past the last rung it is left `loom:failed` for the brain. Landing
keeps the default branch green: a ready pull request that is behind its base
is brought up to date first (GitHub's update-branch) and lands only after its
checks and review pass again on the new head; each repo lands at most one a
pass, `loom:urgent` and `loom:regression` tickets first, then the oldest. A pull request whose only
fault is a failed Actions job gets that job run once more before it is sent
back. A ready pull request that changes a code file (by extension: `.py`,
`.js`, `.ts`, `.tsx`, `.jsx`, `.css`, `.cpp`, `.h`, `.hpp`, `.go`, `.rs`,
`.sh`, `.lua`, `.sql`) and no test file (a `test` or `tests` folder in its
path, or `test_`, `_test.`, `.test.` or `.spec.` in its name) goes back
asking for a test that fails without the change, instead of landing; that
round climbs only if the next one still adds no test. A repo with no test
harness opts out in `LOOM_NO_TEST_REPOS` (space separated `OWNER/NAME`), and
a ticket whose body has the line `Tests: none needed (<reason>)` skips the
check. At most 12 actions per pass. `LOOM_FACTORY=0` turns it off.

Asks: the station reads open `needs-operator` issues in each `LOOM_REPOS` repo
(ETag polls) and forwards those whose author and latest `needs-operator`
labeller are in `LOOM_BRIEF_AUTHORS` to a Discord webhook whose URL it reads
from the SSM parameter named by `LOOM_DISCORD_WEBHOOK_SSM` (region
`LOOM_AWS_REGION`, default us-east-1; never printed, and with the setting
empty nothing is forwarded). A `loom:urgent` ask is posted at once, once, and
nothing the digest needs can hold it; all open asks go in one digest per day
after `LOOM_DIGEST_HOUR` (local hour, default 9). The digest lists asks new or
changed since the last digest first. When the asks do not all fit in one
message, the station posts every ask, grouped by the goal home its body names
(`#N`), as a comment on the open `loom:digest` issue in the ask's own repo
(created when missing), and the message links to each repo's comment. The
same digest includes a "Waiting on you" section for open `loom:human` issues
across `LOOM_REPOS`: the 15 newest grouped by repo, with title, link, and the
first line of the brain's ask, plus the total count. A `loom:human` issue is
never posted on its own, even if it also has `needs-operator` or
`loom:urgent`. A long daily digest may take multiple Discord messages.
The digest is fixed in `state/station/asks.json` (`pending`) before it is sent, so
a retry sends the same one; once sent, `covered` holds each ask's revision
(a hash of its title and body) and `pages` the links. Posts mention nobody
except the numeric user id in `LOOM_DISCORD_MENTION`; a failed post is retried
on the next pass.

Metrics: set `LOOM_METRICS_GOAL=owner/name#N` to name a goal home in
`LOOM_REPOS`. Once a day after `LOOM_DIGEST_HOUR`, the station measures the
last 24 hours, comments with `<!-- loom:metrics v1 -->`, and sends a short
pass count and streak line to Discord. It samples idle slots on each pass
and reads completed runs from `briefs/<lane>/done` and `failed`. The first
day's slot sample may cover less than 24 hours; missing samples fail the
spread target. Use the keys in `.env.example` as setup placeholders.
The GitHub login used by the station must be in `LOOM_BRIEF_AUTHORS`, so
its previous metrics comments can be trusted for the streak.

Reports: by default the station sends a digest, not each report:
`LOOM_REPORT_DIGEST_TIMES` (default `10:00,21:00`, local time) names when.
At each time it sends one message: every goal home whose latest report since
the last digest says something moved, as its short name, the report's first
line and a link; a count of the goal homes where nothing moved; and the
factory's daily numbers, which then no longer go out on their own. Each time
is sent once, and a failed send is tried again on the next pass. Switching
the digest on sends nothing until the next digest time. Setting
`LOOM_REPORT_DIGEST_TIMES` empty forwards each report as below.

Each report on its own: the station also reads comments on open `loom:goal`
issues in each `LOOM_REPOS` repo. On first run it records the newest report comment per repo
without sending old reports; later `<!-- loom:report v1 -->` comments from
`LOOM_BRIEF_AUTHORS` are sent to the same webhook, without a mention. A
successful send advances the per-repo ledger, while a failed send is retried
on the next pass. When the Discord bot below is set up
(`LOOM_DISCORD_CHANNEL_ID` and `LOOM_DISCORD_BOT_TOKEN_SSM`), reports go
through the bot instead, and the webhook setting is not needed for them:
each report is one short line, the goal's short name in bold and the
report's first line, with a thread on it holding the full report and the
comment link, so tapping the line expands it. The short name is a
`Short name: ...` line in the goal home body when there is one, else the goal
home title. A failed line is retried on the next pass; once the line is out,
a failed thread is only logged, so the line is never posted twice. Every
station post, through the webhook or the bot, suppresses link previews.

### Talking to the brain from Discord

The webhook only posts. To let the operator answer a report, ask a question
or give an instruction from the channel, the station also reads the channel
with a Discord bot, at most every 30 seconds:

- Each new message whose author id is `LOOM_DISCORD_MENTION` (the operator)
  becomes one comment on a goal home, starting with
  `<!-- loom:operator v1 discord=<message id> -->` and then the message text,
  followed by an `Attachments:` list with the link of each attached image or
  file, so the brain can open a screenshot (a message with only an
  attachment is noted too). Discord's attachment links expire after about a
  day.
  Messages from anyone else, bots included, are ignored. The goal is the one
  the message names: `owner/name#N` or `#N` of an open `loom:goal` issue in
  `LOOM_REPOS`, else a word of four or more letters found in one goal title
  only, else `LOOM_DISCORD_DEFAULT_GOAL` (`owner/name#N`). The station reacts
  with a checkmark and replies "Noted for the brain" with the note's link,
  naming the goal only when the message named one; a message that names no goal
  when no default is set gets a "Not noted" reply instead. The last message
  id is kept in `$LOOM_HOME/state/station/operator.json`, so each message is
  noted once; the first read only looks back 10 minutes.
- A comment on an open goal home starting with
  `<!-- loom:reply v1 discord=<message id> -->` from a `LOOM_BRIEF_AUTHORS`
  login (the brain's answer, docs/MUSE.md) is posted to the channel once, as
  a reply to that message, with the comment link.

The note is posted with the station's GitHub token, and the brain trusts
only notes from `LOOM_BRIEF_AUTHORS` logins, so that token's login must be
on the list. The note is a GitHub comment: in a public repo, what the
operator types is public. The bot token is read from SSM (region `LOOM_AWS_REGION`) and never
printed; a failed Discord call is logged by method and status only, and
retried on the next pass. With `LOOM_DISCORD_CHANNEL_ID`,
`LOOM_DISCORD_BOT_TOKEN_SSM` or `LOOM_DISCORD_MENTION` empty, none of this
runs.

One-time bot setup:

1. In the Discord developer portal, create an application and add a bot to
   it. In the bot's settings, enable the Message Content intent (without it
   the bot reads empty messages).
2. Invite the bot to the server (OAuth2 URL generator, scope `bot`) and give
   it, on the operator's channel, View Channel, Read Message History, Send
   Messages, Add Reactions, Create Public Threads and Send Messages in
   Threads.
3. Store the bot token as a SecureString parameter in SSM, in
   `LOOM_AWS_REGION`, under a name of your choice such as
   `/YOUR/PATH/discord-bot-token`.
4. Turn on Developer Mode in Discord, copy the channel id, and run
   `lanes/mac/install.sh` with `LOOM_DISCORD_CHANNEL_ID=<channel id>`,
   `LOOM_DISCORD_BOT_TOKEN_SSM=/YOUR/PATH/discord-bot-token`,
   `LOOM_DISCORD_MENTION=<the operator's user id>` and, optionally,
   `LOOM_DISCORD_DEFAULT_GOAL=owner/name#N`.

## Transcript uploads

Transcripts are read on the Loom dashboard, which stores them and serves them
over your private network (it is not part of this repo). Each station pass
uploads every run that finished since the last one: a brief in a slot's
`done/` or `failed/`, and the last brain tick. The body is
`{ticket, slot, model, outcome, started_at, finished_at, events}`, each event
`{ts, role, kind, text}` with kind `text`, `tool_use`, `tool_result` or `log`
and role `user`, `assistant`, `thinking`, `tool` or `runner`. The events are
the CLI's own session record (read by `bus/sessions.py`, the one parser the
viewer below uses too) followed by the run's last attempt in
`logs/<slot>/<ticket>.log`; a lane that keeps no transcript sends its log
alone. Every text has local paths replaced by `<path>` and GitHub, Anthropic,
OpenAI, AWS and Cloudflare key shapes and bearer tokens replaced by
`[redacted]`; each event is cut at 20,000 characters (a log keeps its tail)
and the body at 8 MB, dropping the oldest tool results first.

The station POSTs the body as JSON to `LOOM_TRANSCRIPT_URL` (https only) with
`Authorization: Bearer <token>`, the token read from the SSM parameter named
by `LOOM_TRANSCRIPT_TOKEN_SSM` (in `LOOM_AWS_REGION`) with the webhook's
reader, which gives up after 30 seconds. Set both when running
`lanes/mac/install.sh`; they live only in the station's local plist. With
either empty nothing is sent. The ledger `state/station/transcripts.json`
records each run once sent; a failed upload is tried on the next pass, and
after 5 failures the run is given up with one line in `station.log`. Errors
name only their type, never the URL or the token. The first pass after
uploads are turned on records the runs already finished without sending
them, and one pass sends at most 10 and stops at its first failure.

A brain tick is finished when `state/brain.json` has `outcome` `ok` or
`failed` and a numeric `started` (when `lanes/mac/brain.sh` started the
agent; a skipped tick has none) and `at` (when it ended). It uploads as slot
`brain` with its UTC start time as the ticket and `model` from the same
file: the Claude Code sessions in the operator's `~/.claude/projects` for
`$LOOM_HOME/brain` last written between `started` and `at`.

## Log watch

Each station pass reads the lines each component added to its log since the
last pass: by default `logs/station.log` (as `station`), `logs/brain.log`
(`brain`) and each lane's `logs/<lane>-service.log` (named after the lane).
`LOOM_WATCH` replaces that list with lines of `name=path`, a relative path
being under `$LOOM_HOME`. A line counts when it says `error`, `failed`,
`Traceback` or `refused`, or carries an HTTP 4xx or 5xx; a Python traceback
counts once, as its exception line. Each line is reduced to a signature:
times, numbers, hex ids and SHAs, long mixed ids, repos and issue numbers,
paths, addresses and mentions become placeholders, so a 403 on two repos is
one signature (the status code is kept).

A signature seen 3 or more times within 30 minutes, and not seen in the 24
hours before, files one `loom:triage` issue in `LOOM_STATUS_REPO` titled
`<component>: <signature>`, with the count and the first and last example
lines. The examples have paths, credential shapes, mail addresses, mentions,
private hosts, IP addresses and repo names replaced, and each must pass the
written-text check (`bus/text.py`) or is withheld. An open `loom:triage` issue
with the same title is counted instead of filed again. While the signature
keeps repeating its open issue gets a count comment at most once an hour; a
closed one is left alone. No model is involved: diagnosis is the brain's.

`state/station/watch.json` keeps each log's inode and offset and each
signature seen in the last 24 hours. The first scan starts at the end of each
log; a log that is later created, rotated (a new inode) or truncated is read
from its start. One pass reads at most 1 MB of each log, files or counts at
most 5, and below the GitHub budget floor only reads, filing on a later
pass. Once an hour `station.log` gets a `watch: scanned ...` line with the
logs, lines and matches seen, which is how a quiet day shows it ran; the
watcher's own lines never count as hits.

## Invariants

Each station pass runs `bus/invariants.py`, at most every 5 minutes, on what
the station already reads: the journals and marker comments trusted logins
posted on every goal home in the last day, the lane runs that ended in the
last day (`briefs/<lane>/done` and `failed`, timed by their logs), the lanes
held by a limit, queued tickets, sub-issue counts, the review bot's plan
check on open lane pull requests (`LOOM_PLAN_CHECK`, default
`Grug - Chief`) and remote heartbeats. Each invariant is one function
returning breaches with evidence:

- One brain's two newest ticks each journaled a failed or degraded step
  (`- Step <name>: failed ...`, docs/BRAIN.md Journal), or no brain journaled
  for 90 minutes once one has been seen.
- A `loom:metrics` line posted more than once for one day, or two
  `loom:report` comments within 90 minutes, on one goal home.
- A lane with 5 or more runs in 24 hours and none done.
- A lane held by a limit for over 2 hours while tickets of its tier are queued.
- A goal home at 90 or more sub-issues.
- An open lane pull request whose plan check failed more than 30 minutes ago.
- A remote lane whose newest heartbeat is older than its `beat_max`.

Each breach files one issue labelled `loom:triage` and `loom:invariant` on
the invariant goal home (`LOOM_INVARIANT_GOAL`; empty means
`LOOM_METRICS_GOAL` when it is in the self repo, else the self repo's one
goal home that is not a backlog), as a sub-issue and with a `Part of #N`
line, carrying the evidence. A hidden marker holds a hash of the invariant
and its subject: an open issue for the same breach files nothing, and a
closed one is reopened with a comment only for evidence newer than its
close. Issues and pull requests in a private repo are named in words, and
failure reasons have repos, paths and credentials replaced. One pass files
or reopens at most 5, and below the GitHub budget floor files nothing.
`python3 -m bus.brain status` lists the open ones under "Invariants broken"
for the brain's report, and the station's own standup on that goal home
names them under "Not going well". `state/station/invariants.json` keeps
the issue numbers, when each lane was first seen held, and the newest
journal seen. Adding an invariant is one function in `INVARIANTS` plus a
test.

## Transcript viewer

For reading on the lane machine itself, `lanes/viewer/serve.py` (installed
with the lanes as `com.loom.viewer`) serves
every lane's agent session as a chat at `http://localhost:7340/`: the brief,
the agent's messages, each tool call with its output, edits as diffs. It reads
Claude Code transcripts (claude and anthropic lanes), opencode's SQLite store
(openrouter, opencode-go; the 1.x `message`/`part` tables or the 2.0
`session_message` table) and codex rollouts. A slot such as `opencode-go-3`
lists under its own name and reads as its lane. It binds localhost only and
answers only localhost Host headers; the viewer itself sends nothing off
the machine (the uploads above do). API-lane agents run on the same machine, so their sandbox denies every
loopback connection; they cannot query the viewer or any other local service.

## API lanes: openrouter, opencode-go, anthropic (and zen, zai below)

- `openrouter` (free models via opencode), `opencode-go` (subscription models
  via opencode) and `anthropic` (API credits via the official Claude Code CLI).
- The provider key is read from SSM at run time (`LOOM_<LANE>_KEY_SSM`, set at
  install, never in this repo) and exported to the agent, never passed in
  argv, in an otherwise empty environment with a scratch HOME and TMPDIR.
- The agent runs under a macOS Seatbelt profile (`sandbox-exec`): no reads or
  writes anywhere under the real HOME except its worktree, its scratch home
  and the directory holding its CLI (plus read-only access to the skill
  snapshot), and no keychain access. It can edit its
  tree but cannot read credentials, push, open, or merge; the runner does
  that with the machine's own credentials. Without `sandbox-exec` an API lane
  reports itself unavailable.
- A brief whose branch already has a PR in any state moves to `done/`
  without running. Restarts cannot re-run a ticket.
- Otherwise: fresh clone, the lane's official CLI with the brief, then the
  runner (not the agent) pushes the branch and opens the PR in the format
  the Chief check requires.
- Any step that fails moves the brief to `failed/` with the reason in
  `logs/<lane>/<ticket>.log`. Failed briefs are never retried; the
  orchestrator re-briefs.
- Logs stay on the lane machine, apart from the scrubbed transcript the
  station uploads to the dashboard when that is set up (see "Transcript
  uploads"). Nothing is pushed except the ticket branch,
  or a side branch holding edits that failed the validation contract.
- API-lane session transcripts (Claude Code `.jsonl`, opencode's `opencode.db`)
  are copied to `logs/<lane>/<ticket>/` after each run, before the next run
  wipes the scratch home. Links are never followed.
- launchd starts a pass every minute and a lock keeps one pass per lane.
  An opencode 2.0 pass starts a temporary server and stops it when the run ends.
- A usage limit `opencode run` would retry silently is asked for directly.
  With `LOOM_<SLOT>_PROBE_URL` set (the installer sets OpenCode Go's chat
  endpoint for the opencode-go lanes, and `LOOM_OPENCODE_GO_PROBE_HEADER`
  `x-opencode-session`), the runner sends one `max_tokens` 1 request for the
  run's model before each run, and again after a run that timed out (exit
  124). The key goes to curl on stdin, never in argv or a log. A 429 naming
  `GoUsageLimitError` holds the lane 12 hours (heartbeat `limit: the
  provider's weekly limit ...` with a `retry_at`) and hands the brief on as
  `[limit]`; any other answer lets the run go ahead. Another provider can
  name its own endpoint in the same setting.

### One slot on opencode 2.0

opencode 2.0's installer overwrites the one binary every opencode lane runs,
so try it on one slot. Install it somewhere of its own (outside the repo, not
over the 1.x binary on PATH) and name it for that slot only:

```sh
LOOM_OPENCODE_GO_3_OPENCODE=/path/to/opencode2/bin/opencode lanes/mac/install.sh opencode-go-3
```

(the installer writes every `LOOM_*` setting into the slot's plist).

`LOOM_<SLOT>_OPENCODE` (or `LOOM_<LANE>_OPENCODE` for every slot of a lane)
names the opencode binary; without it a lane runs `opencode` from PATH. Before
each run the runner asks the binary for `--version`; when it prints
`opencode v2.` the runner starts `opencode serve` with a fresh password on a
random high loopback port inside the agent's sandbox. It allows only that
port through the sandbox's loopback deny, waits up to 15 seconds for the
server, and calls `run --server` with `--auto`. The server stops when the run
ends, including on failure or timeout. The sandbox lets the
agent read the named binary's real directory, exactly as it does the default
one; a binary that needs files outside that directory will not start there.
The transcript is archived and shown on the viewer like a 1.x one. To go
back, re-run the installer for that slot without the variable.

### Anthropic spend

The anthropic slots spend API credits, so the runner tracks what each run
costs without an Admin API key: it runs `claude -p --output-format json`,
writes the result's text to the lane log, and appends one line per run to
`$LOOM_HOME/state/usage/spend.jsonl` (time, slot, ticket, model,
`cost_usd` from the result's `total_cost_usd`, and token counts). A run that
left no result is recorded with `cost_usd` null, never zero. Settings, set at
install (the lanes and the station both get them):

| setting | meaning |
|---|---|
| `LOOM_ANTHROPIC_BUDGET_USD` | credit available, in dollars; unset shows only the spend |
| `LOOM_ANTHROPIC_BUDGET_SINCE` | when that credit was counted (`YYYY-MM-DD` is 00:00 UTC, or `YYYY-MM-DDTHH:MMZ`); runs before it do not count against it |
| `LOOM_ANTHROPIC_BUDGET_EXPIRES` | when the credit expires, same format |
| `LOOM_ANTHROPIC_FLOOR_USD` | stop the anthropic slots when the remaining credit is under this (default 5) |

The lane status comment gains one line: spent today (UTC), spent since
`LOOM_ANTHROPIC_BUDGET_SINCE`, remaining against `LOOM_ANTHROPIC_BUDGET_USD`,
the burn over the last 24 hours, and the balance projected at
`LOOM_ANTHROPIC_BUDGET_EXPIRES` at that burn. Runs of unknown cost are counted
and named, not guessed. A new run republishes the comment on the next station
pass. Under the floor (or with a budget setting that does not parse) each
anthropic slot reports itself unavailable with that reason, and the station
sends the anthropic slots nothing more.

## Skills on every lane

Every lane has the operator's skills, so a brief may name a skill ("use the
impeccable skill") on any lane.

- codex and claude run in the operator's own home and read the skill
  folders there directly.
- The sandboxed lanes cannot read that home, so the runner keeps a snapshot
  at `$LOOM_HOME/skills`: every child folder holding a `SKILL.md` in the
  folders `LOOM_SKILL_DIRS` names (colon-separated; default
  `~/.agents/skills:~/.claude/skills`; the first folder wins a name both
  hold), copied with links followed. It is refreshed at most hourly, only
  when a sandboxed lane has a brief waiting. The new copy is built beside
  the old one and swapped in only when the whole copy succeeded: a failed
  refresh keeps the last good snapshot and logs one line to
  `logs/skills.log`.
- Each API-lane run links the snapshot into its scratch home at
  `.claude/skills` (Claude Code), `.agents/skills` (the cross-tool folder)
  and `.config/opencode/skills` (opencode). The sandbox lets the agent read
  the snapshot and never write it.
- The agy lane user cannot read the snapshot either, so before each run the
  runner streams a copy to that user's `~/.gemini/config/skills`, where agy
  reads global skills, replacing the last run's copy. The sandbox keeps
  `~/.gemini/config` read-only for the agent. `--disable-slash-commands`
  stops skill expansion in the prompt only; agy still loads a skill it
  decides to use.

## Public repos only: providers that train on prompts

Some free providers may train on what they are sent. Their briefs must name a
public repo. Before cloning, the runner asks `gh api repos/<owner>/<name>
--jq .private`; a private repo parks the brief with the reason, and a failed
lookup leaves the brief for the next pass. The rule covers:

- the `zen` lane (several OpenCode Zen free models may train on prompts;
  Nemotron free models are NVIDIA trial endpoints),
- the `zai` lane (Z.ai free Flash models),
- `opencode-go` runs of a Muse Spark Contributor model (it trains Meta's
  models); other Go models keep 0 to 30 days and take any repo.

## API lanes: zen, zai

- `zen`: OpenCode Zen free models through opencode, `-m opencode/<id>-free`
  (default `opencode/space-bunny-free`). Zen serves its free models with no
  stored credential, so the lane reads no key; the agent's environment holds
  no provider key at all. opencode sends Zen's `x-opencode-session` header
  itself.
- `zai`: Z.ai's free Flash models through opencode's `zai` provider,
  `zai/glm-4.7-flash` (default) or `zai/glm-4.5-flash`. GLM-5.3 Flash and
  the FlashX models are paid and refused. The key comes from SSM
  (`LOOM_ZAI_KEY_SSM`) and reaches the agent as `ZHIPU_API_KEY`, the variable
  opencode reads.
- Both run sandboxed exactly like the other API lanes and pin opencode's
  helper model to the lane's model.

## claude/ - deep thought

- Tool: official Claude Code, owner's subscription login, on the owner's
 machine. Nothing else ever holds this credential.
- Trigger: `lanes/run_lane.sh claude` under launchd, official CLI per brief.
- Best for: root-cause investigations, gnarly C++, hard refactors.
- ToS: sanctioned client only. The harness writes briefs; the official CLI
 does the work.

## codex/ - scripted code

- Tool: `codex exec --sandbox workspace-write` on a logged-in box: no network and
 no .git writes, so the agent edits and the runner commits and pushes.
- Best for: scoped implementation, CI failure repair, `codex review` passes.
- Models per ticket: Astra (hardest), Sol (default building), Luna (volume).

## opencode/ - volume

- Tool: headless opencode runs. Providers configured in `config.json`:
 OpenRouter (free tier + paid), Poolside, NVIDIA NIM, local endpoints.
- Best for: small well-scoped tickets, failing tests first, triage, docs.
- Many parallel workers; each in its own worktree.

## sparks/ - the workshop

- Compute, not a model lane. Test farm (full gates: `python -B` suites,
 C++ builds) and local model inference (triage/review workers).
- Rule: sparks power Loom. Sparks never touch a live system.

## workersai: Cloudflare Workers AI

- Cloudflare Workers AI through opencode's `cloudflare-workers-ai` provider,
  within the account's free daily allowance (about 10,000 Neurons a day; on a
  free plan requests fail past it, on a paid plan they are billed).
- Needs a Workers AI token (`LOOM_WORKERSAI_KEY_SSM`) and the account id it
  belongs to (`LOOM_WORKERSAI_ACCOUNT_SSM`), both read from SSM at run time and
  handed to the agent in its emptied environment. opencode reads them as
  `CLOUDFLARE_API_KEY` and `CLOUDFLARE_ACCOUNT_ID`; without the key it sends
  no credentials and Cloudflare answers "Authentication error".
- Default model `@cf/qwen/qwen3.8-27b`: it calls tools, has a 262k context and
  runs on the Workers Free plan. Models without tool calls or with a 32k
  context (qwen2.5-coder-32b) fail under opencode's agent prompt, and some
  newer models (glm-5.3-flash) are paid-plan only.
- Runs `cloudflare-workers-ai/@cf/...` models only. Cloudflare does not train
  on prompts, so private repos are allowed.

## spark: a local vLLM coder, one stream at a time

- opencode against a vLLM server you run yourself, through a custom
  OpenAI-compatible provider named `spark`. `LOOM_SPARK_URL` is its base URL
  (e.g. `http://your-spark.internal:8000/v1`, reachable from the lane machine
  but not on its loopback, which the sandbox denies); it takes no key.
  Models are `spark/<served model id>`, from the brief's `Model:` line or
  `LOOM_SPARK_MODEL`.
- One slot: the server serves one stream at a time.
- Nothing leaves your own machines and nothing trains on prompts, so private
  repos are allowed.
- The server sits behind a thermal guard that refuses new connections, or
  answers 503, while the box is hot. Each pass asks `$LOOM_SPARK_URL/models`
  first, and asks again after a run that failed. A refused connection or a
  503 is a limit, not a failure: the heartbeat says unavailable, with a
  reason starting `limit:`, for 15 minutes; every brief in the inbox is
  parked with that reason; the station puts its ticket back to briefed (a
  fix brief's back to `loom:pr`), and the factory moves a ticket briefed on a
  limited lane to the next rung of the model ladder at once. Any other
  failure to answer (a timeout, a name that does not resolve) only makes the
  lane unavailable; its briefs wait.

## agy: the Google plan, as a separate macOS user

- agy (Google's Antigravity CLI) on the operator's Google plan, called
  directly with the brief in print mode and `--mode accept-edits` (never
  wrapped in another agent). Its catalog includes Gemini, Claude and GPT-OSS
  models (`gemini-*`, `claude-*`, `gpt-oss-*`); default
  `gemini-3.8-flash-high`.
- agy keeps its sign-in in the macOS keychain. A sandboxed agent that can
  reach the keychain server could ask for any of the operator's keychain
  entries, so the agent runs as a dedicated standard macOS user (default
  `loom`) instead: the runner starts it with `sudo -n -u loom`, in that user's
  emptied environment, inside the sandbox (keychain server allowed, loopback
  and everything else denied as for the other API lanes).
- Runs commands inside its sandbox. Print mode cannot prompt, so a tool no
  rule allows is denied and the run ends with no output. Before each run the
  runner writes agy's settings (`~/.gemini/antigravity-cli/settings.json`)
  as the lane user from `lanes/agy/settings.json`: `permissions.allow` lets
  through shell commands and the file tools, and `permissions.deny` blocks
  web fetching (`read_url`, `execute_url`). Of the old file, only
  `trustedWorkspaces` (set at sign-in) is kept. After the run the runner
  writes back exactly what it wrote before, and the sandbox keeps the file
  read-only during the run, so a run cannot leave a wider rule behind. The
  brief tells agy to run the repo's tests and lint and to edit only inside
  its tree, as the other lanes do.
- Commands run as the lane user, inside the same sandbox and emptied
  environment as agy itself, so they can write only where the agent can (see
  below). agy stores its token so that `/usr/bin/security` can read it
  without a prompt, and reads it that way itself, so the sandbox cannot block
  that tool, and a command could read the token. The settings deny commands
  starting with `security`, which slows that down but does not stop it. The
  runner never passes `--dangerously-skip-permissions` and passes
  `--disable-slash-commands`.
- Public repos only: whether the plan trains on prompts is not established.
- One-time setup, done by the operator:
  1. Create the standard user, hidden, and store its password in SSM.
  2. `chmod 700` the operator's home: the new user shares the `staff` group,
     so a `750` home would still let it in.
  3. A sudoers rule letting the operator's user run commands as `loom` (a
     step down in privilege, not up): `<operator> ALL=(loom) NOPASSWD: ALL`.
  4. A shared work folder under the user's home with inherited ACLs for both
     users (`LOOM_AGY_WORKDIR`).
  5. A keychain for the user that is not named `login` (a `login` keychain
     will not unlock from the command line), set as its default, no
     auto-lock, password from step 1 (`LOOM_AGY_KEYCHAIN`, `LOOM_AGY_KEY_SSM`).
  6. Install agy for the user and sign it in once, in a shell as that user.
- The keychain is unlocked during the runner's readiness check, only when
  briefs are waiting (it locks again on reboot). The password is sent on
  stdin to `security -i` running as the lane user, so it never reaches
  argv. Because `security -i` splits commands on whitespace and treats
  quotes and backslashes specially, passwords with whitespace, quotes,
  backslashes, or any characters outside letters, digits, and
  `._~+=/@%:,^-` are refused. A refused password or a failed unlock marks
  the lane unavailable and leaves its briefs waiting.
- Under the lane user's home, the sandbox allows writes only to the
  worktree, the agent's TMPDIR (`tmp`), agy's state directory
  (`~/.gemini/antigravity-cli`), `Library/Keychains`, and `Library/Caches`.
  Inside the state directory, `settings.json`, `installation_id`, `bin`,
  and `builtin` stay read-only. Everything else under the lane user's home
  stays read-only as well, including shell startup files, `~/.gemini`
  config (with the skills copied to `~/.gemini/config/skills`, see Skills
  on every lane), and MCP servers.
- Before each run, the runner clears agy's memory of earlier runs
  (`agy_forget`), deleting everything in its state directory except sign-in
  and install state. Kept entries in the state directory are `settings.json`,
  `bin`, `builtin`, `installation_id`, `jetski_state.pbtxt`, `updater`,
  `last_check.timestamp`, `log`, `cli.log`, `crashes`, and `cache`; kept
  entries under `cache` are `onboarding.json` and `default_project_id.txt`.
  Kept entries must be real files and directories: a link left in place of
  a kept entry is removed, never followed, so deletion cannot reach into
  the keychain folder or another ticket's tree.

## Adding a lane

A lane is a name in `lanes/run_lane.sh`, not a directory of its own: that one
runner polls, heartbeats, commits, pushes and opens the PR for every lane.
Adding one takes:

- In `lanes/run_lane.sh`, a CLI in `lane_ready`, and the name in the header
  comment's lane list. An API lane also joins the case that wants
  `sandbox-exec` and a key read from `LOOM_<LANE>_KEY_SSM`; a lane whose CLI
  or key is missing reports itself unavailable and takes no work.
- A branch in `run_agent` that runs the lane's CLI with the brief and the
  model. An API lane calls `api_agent <key var or -> <CLI...>`
  (`lanes/agent_sandbox.sh`, shared with the triage rewriter), which runs it
  under the sandbox in a fresh scratch home with only its own key exported,
  and must be listed in `is_api_lane` (its transcripts are then archived).
- A case in the `LANE:MODEL` model check, so a brief naming a model outside
  the lane's family is parked rather than run.
- An entry in `trains_on_prompts` if the provider may train on prompts: the
  lane then takes public repos only.
- In `lanes/mac/install.sh`, a default `LOOM_<LANE>_MODEL` (exported, so every
  PR names it) and the lane in the list installed by default; an API lane's
  `LOOM_<LANE>_KEY_SSM` is passed in the environment at install time.
- Tests in `tests/test_run_lane.py` against the fake CLIs and gh already
  there: a fake CLI for the new lane, and a pass that edits, pushes and opens
  the PR (API-lane passes need macOS `sandbox-exec`).
- A section here: what the lane is for and what it runs.

No lane may merge, ship, or hold another lane's credentials.

## Change classes and the Intent section

Every lane pull request opens with an `## Intent` section at the top of
its body, followed by the ticket sections. The section names the change
class, one line per behavior change, and the counts of files by class:

```
## Intent
Class: logic
- Fix the thing for every user
Files: 1 logic, 1 tests
```

The behavior lines come from the lane's final summary: the agent writes
one line per behavior change to `.loom/summary.md` at the top of its
tree (the runner tells it to, reads it, and never commits it). When the
lane left no summary, the runner falls back to the ticket's `What`
section, one line per non-empty line.

One class covers the whole pull request, highest wins:

- `sensitive`: any changed file matches `.loom/sensitive-paths` in the
  repo (one glob per line, `#` comments, a trailing `/` is a directory
  prefix), read from the default branch. A missing file means no
  sensitive class. Sensitive changes deserve extra scrutiny.
- `logic`: anything else behavior-carrying (code, config, scripts).
- `tests`: only test files (a `test` or `tests` folder in the path, or
  `test_`, `_test.`, `.test.` or `.spec.` in the name).
- `docs`: only `*.md`, `docs/`, `LICENSE`, `*.txt`.

The pull request carries exactly one `class:<name>` label, created like
the other labels by `bus/labels.py`. Classification never changes merge
rules; it only tells the reader how risky the change is.
