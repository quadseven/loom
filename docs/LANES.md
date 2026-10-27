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
  Account-wide out-of-credit limits still hold the lane. The heartbeat names
  each limited model and the epoch it lifts (`limited_models`); the station
  claims no ticket for that model on the lane until then, and the factory
  moves it as for a lane at a limit (a ladder ticket waits on its rung).
- Each pass writes `state/usage/<lane>.json` with `available` and `at`. A lane
  whose CLI or key is missing reports `available: false` and leaves its briefs
  alone; the dispatcher routes only to fresh, available lanes.

## How a lane is told to work

After its brief, every agent gets a "How to work" note (lanes/run_lane.sh
`work_note`): work test-first (write the failing test, see it fail, fix,
see it pass), run the full suite or the validation check before finishing,
follow the review bot's checklist (new functions under cyclomatic
complexity 15, no swallowed errors, no dead code, docs in step, ASCII
only), and commit. On 2026-10-14, 27 of 95 tickets took six or more
rounds, and no lane ran a test before opening its pull request.

Before pushing a branch or opening a pull request on a public repository, the
lane runner uses infra-public's `check_private_leaks.py` on the diff and
generated pull request body, with the shared deny-list of names and products
(`--deny-list-ssm`, the SSM parameter `LOOM_LEAK_DENY_SSM`, default
`/infra/leak-scan/deny-list`). Every scan goes through `leak_scan`. Set
`LOOM_LEAK_SCANNER` to that script's path when it is not at
`../infra-public/.github/scripts/check_private_leaks.py` relative to the Loom
checkout. A missing scanner, a deny-list the scanner cannot read (it exits 2)
or a hit blocks the push; a scan that could not run parks the brief
`[transient]`. A lane pod or cluster runner (`LOOM_SSM_DIR`) holds no AWS
key: the scanner reads the deny-list's copy under `LOOM_SSM_DIR` through
`lanes/k8s/ssm-dir-bin/aws`, a stand-in for the AWS CLI put first on the
scanner's PATH only. Without that copy it pushes nothing to a public repo.
Private repositories skip this scan. Test IP addresses in public changes must
use the documentation ranges `192.0.2.0/24`, `198.51.100.0/24`, or
`203.0.113.0/24`; add `leak-guard-allow` on a line only when a deliberate
range bound must appear.

## Validation contract

A repo opts in with `.loom/validation.conf`, the same steps its CI runs:

```
# .loom/validation.conf
interpreter: python3.12
submodules: yes
setup: scripts/setup.sh
check: scripts/check.sh
affected: scripts/affected.sh
```

`interpreter` is a command that must be on the lane machine's PATH,
`submodules: yes` checks out the pinned submodule revisions, `setup`
prepares the tree (a venv, locked dependencies, a pinned linter) and `check`
is the required check. Point the repo's CI at the same two scripts so the
lane and CI cannot drift apart. Unknown lines are refused.

`affected` is optional: a command for the suites the repo's CI runs only for
some paths (a per-directory unit suite, a generated file compared with its
source), which a repo-wide `check` leaves out. The runner runs it after
`check` passes on the committed tree, with the pull request's changed files,
one path per line, on stdin and in `LOOM_CHANGED_FILES` (the lane's commits
on a new run; what the pull request adds to the default branch on a fix
round). It passes like `check` (exit 0, no tracked file changed), a failure
parks the brief as `validation failed: the affected check exited N` and
pushes nothing, and its record is the `affected` phase. A lane pull request
in the deploy repo passed its check and went red in CI on two such suites
(loom#1773).

The runner's reader (`bus/contract.py`) also accepts a list of
`<kind> | <command>` lines, or `[kind]`, `[kind.name]` and `[[kind]]` tables
with a `command = "..."` key, for the kinds lint, format, type and test. It
joins the commands, in that order, into one `check`. The kind `affected`
(`affected | <command>`, `[affected]`) becomes the `affected` command instead.
List only commands that run on a lane machine with no secrets, simulators or
unset variables.

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
- A lane change that adds or edits `.loom/validation.conf` must read and pass
  under itself. Before the push the runner reads the committed file with the
  same reader and runs its `check` (and its `setup`, when that differs from
  the base's) on the lane's tree. A file the reader refuses, a check that
  exits non-zero or changes tracked files, or a missing interpreter parks the
  brief as `commit refused: ...; nothing pushed`. The records are the
  `contract-setup` and `contract-check` phases.
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
  HOME (`run/<lane>-check`), under `sandbox-exec`, with no access to the
  runner's HOME, but with the network and the writes outside HOME a step's
  setup may need (an API-lane agent has neither). A machine without `sandbox-exec` fails setup
  for repos with a contract. Each step has `LOOM_CHECK_TIMEOUT` seconds
  (default 1800).
- `logs/<lane>/<ticket>.validation.jsonl` holds one line per step: phase
  (`prepare`, `setup`, `baseline`, `contract-setup`, `contract-check`, `final`, `affected`), command, exit code, tree
  revision, `ok` and detail (the interpreter's version, the submodule
  revisions).
- A repo without the file is not validated by the runner; CI still is.

### Validation gaps

When a lane pull request goes back for a fix round on a red CI job, the
station checks whether the contract runs that job's command
(`bus/ci_gap.py`). It maps each failing GitHub Actions check run to its
workflow file, job and failing step (the Actions job API), reads the step's
`run:` command and working directory from the workflow at the pull request's
head, and looks for it in the contract's `setup`, `check` and `affected` text
on the base branch. A command the contract does not run is a validation gap:

- The station files one issue per repo per gap in that repo, labelled
  `loom:validation-gap`, with a `<!-- loom:validation-gap v1 key=... -->`
  marker. It names the job, the workflow and step, the command CI runs, the
  workflow's `on.pull_request` paths filter and the changed files it matched,
  and the line to add: `affected: <command>` for a path-scoped job, else
  `check: <check> && <command>`. A gap already in
  `state/station/validation_gaps.json`, or on an issue with its marker (open
  or closed), is not filed again.
- The fix round's comment tells the lane to run the command in the tree
  before it pushes, naming the gap issue.
- The station records once per lane pull request whether CI was red on its
  first push (its checks finished before any fix round; the review bot's
  check is not counted). `bus.brain status` shows, per repo, the first-push
  CI red rate and the gaps filed, under "Validation gaps".

A gap is judged only for a repo with a contract; a repo without one gets the
"Add the repository validation contract" ticket instead.

### What a fix round is told about a red job

The loom:fix comment quotes each failing check's log excerpt and, above it,
the failing test names (pytest, unittest, go test and TAP forms) and up to
eight assertion lines from the excerpt. When the failure text names a command
that regenerates a file (a quoted command on a line that says a file is stale
or must be regenerated, or a command that renders into a file with a
redirect, such as `tool --render-rbac > rbac.yaml`), the comment's first line
after its header says to run that command and commit what it writes
(`bus/ci_facts.py`).

### Live-cluster check

Before a lane opens a pull request, or pushes to one in a fix round, whose
commits change a Kubernetes manifest, the runner checks what the added lines
name against the live cluster (`bus/live_cluster.py`, over the range the
leak scan reads):

- every pod template's `nodeSelector` pair and required node affinity
  expression must match a Ready node's labels in `kubectl get nodes -o
  json`, and some Ready node must match them all and carry no NoSchedule or
  NoExecute taint the pod does not tolerate;
- every SSM parameter an ExternalSecret key (an SSM path), an
  ssm-secret-sync JSON file or an `aws ssm ... --name` line names must exist,
  by `aws ssm describe-parameters` (names only, never a value);
- every host a probe, an analysis template or a smoke or canary check calls
  must resolve (getaddrinfo, 3 seconds); in-cluster names are skipped. The
  agent wrote these names and the lookup leaves its sandbox, so only names
  under a zone in `LOOM_LIVE_CHECK_ZONES` (comma or space separated, the
  operator's own domains), plain ASCII (no Unicode or punycode label), of at
  most six labels and 100 characters, are looked up. With no zones set, no
  name is.

The check runs after the public repo leak scan passes. Its git reads of the
agent's tree run no repo-configured program (external diff, textconv,
fsmonitor, hooks), reach no remote, and carry no GitHub, App or AWS
credential; kubectl and aws keep the runner's own environment.

A miss fails the run as work, names each missing label, key or host, and
pushes nothing, so the lane or the next rung fixes it. A kubectl, AWS or DNS
that cannot be reached at all, and a lane pod (no kubeconfig, no AWS key),
does not stop the run: the log and the pull request body (or the round's
comment) carry `live-cluster check could not run: <reason>`.

## The brain tick

`lanes/mac/install.sh` also installs `com.loom.brain`, which runs
`lanes/mac/brain.sh` every `LOOM_BRAIN_INTERVAL` seconds (default 1800). A
tick takes the brain lock in `LOOM_STATUS_REPO` (a held lock ends the tick
before any model call), updates its own clone of this repo under
`$LOOM_HOME/brain`, runs an agent CLI on docs/MUSE.md with a hard stop of
`LOOM_BRAIN_MINUTES` (default 40), and releases the lock, also after a
failure. It logs to
`$LOOM_HOME/logs/brain.log`. `LOOM_BRAIN=0` at install removes it. A live tick waits up to `LOOM_BRAIN_WAIT` minutes (default 25) for a lock another brain holds, because it is the only tick that ships. With
`LOOM_BRAIN_SCOPE=live` the tick does only the work that needs the cluster
(ships and rolls, checks that need cluster or database access) and leaves the
rest to Muse (docs/MUSE.md). The lane machine runs with `all` (decision
0002): both brains take every stage under the one lock, and the defer and
done markers keep them from repeating each other's work.
Its prompt tells the brain to brief only under an open epic
(`bus.brain brief --epic E`), to propose a new epic within the cap of 6 open
epics per goal (`bus.brain epic`), to chart a wayfinder map for an epic whose
route is unclear, to resolve research and task tickets alone, and to batch
the questions only the operator can answer into one daily ask with
recommended answers (docs/BRAIN.md, Filing under an epic).

The agent floats to whichever subscription has usage (decision 0002, item
2). `LOOM_BRAIN_LADDER` lists lanes in order, default `claude codex spark`.
The tick runs the first rung whose heartbeat
(`$LOOM_HOME/state/usage/<lane>.json`, written by that lane's runner) says
available and is at most 10 minutes old, with the lane's model and the
same CLI invocation `lanes/run_lane.sh` uses, from the shared
`lanes/agent_cli.sh`:

| Rung | Model | Run |
|---|---|---|
| `claude` | `LOOM_BRAIN_MODEL` (default the current Opus) | `claude -p`, every permission prompt skipped |
| `codex` | `LOOM_CODEX_MODEL` | `codex exec` with full access (a lane's run has no network; the brain needs GitHub) |
| `spark` | `LOOM_SPARK_MODEL` at `LOOM_SPARK_URL` | opencode in the lane sandbox (`lanes/agent_sandbox.sh`) with the checkout as its tree, so its `.git` stays read-only; given this machine's GitHub token, a CA file for TLS and an allowlist of non-secret `LOOM_*` settings in its environment |

A run that fails with a limit, as `run_lane.sh` classes a lane's failure
from the last lines of output, hands the same tick to the next rung with
usage, once, under the same lock and within the same hard stop. A rung with
no model set, or a name with no runner, is passed over. agy has no rung:
its CLI runs only as the agy lane user, which cannot read the brain's
checkout or GitHub sign-in, and run as the operator it would read the login
keychain. The `tick start` line in `brain.log` names the lane and model.
A tool call a rung's sandbox refuses (the spark rung's opencode denies paths
outside the checkout) reaches the agent as a tool error and the tick goes
on; the log watch counts it on the tick and files it only when it hurt the
tick or keeps coming back (Log watch).
Each tick resets the checkout to origin's head with hooks and fsmonitor
off and removes untracked and ignored files, nested repositories included;
a checkout that still is not clean is cloned again, so nothing a run left
there runs or steers the next tick.

Each tick writes `$LOOM_HOME/state/brain.json` (how it ended, and the
holder, lane and model that ran) and touches
`$LOOM_HOME/state/brain-ok` when it ended well, a skip for a held lock
included. When no rung has usage, the tick takes no lock and runs no CLI: it
records `skipped` with each rung's reason and leaves `brain-ok` alone, so
the brain-stopped check below reports it with that reason. The station adds a `brain` row to the lane status. The brain also
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
`LOOM_RETRY_MAX` times (default 2); one with no retry left goes back to
`loom:queued` on its rung, never to `loom:failed` (loom#1395). A run that exits 124 after editing pushes
its edits to a side branch `<branch>-timedout-<time>` and names it in the
failure reason; the retry's brief tells the next attempt to start from that
branch. On a public repo that side branch, like a `-unvalidated-` one, is
pushed only after the public repo leak scan passes on its edits; otherwise no
branch is kept. A lane commit that adds files outside the brief's paths is
refused and nothing is pushed, except in a fix round: there the runner removes
exactly those files (never the round's other changes) in a commit on top,
names them in the run log, and pushes the rest, so the round does not retry
the same refusal; a stray file it cannot remove still refuses the commit,
naming the file (loom#1476). A fix round that fails transiently goes
back to `loom:pr`, and one that fails its work goes to `loom:failed`, so the
factory climbs the ladder and sends the fix to the next rung; a lane that hit its provider limit stays unavailable until the
provider's reset (else 60 minutes; delete its heartbeat file in
`state/usage/` to clear it sooner). A key the provider will not take is
`[auth]`, not a transient failure: it costs the ticket no attempt, never
flags `loom:runaway`, and every lane that bills that key stays unavailable
with `API key expired: renew <the SSM parameter>` in its heartbeat, with no
`retry_at`, until the value at that parameter changes. The runner opens one
`needs-operator` ask on the factory repo naming that parameter. opencode
lanes run with `--print-logs --log-level ERROR`, because opencode's own
output says only "UnknownError: Unexpected server error" for every provider
fault: the logged error shows an upstream 429 (a limit) or a model opencode
no longer offers (`ProviderModelNotFoundError`, which it reports for a model
models.dev marks deprecated). A missing model is a limit on that model for
24 hours, with `model <id> not found by the agent CLI` in the heartbeat. It reads
`LOOM_REPOS`,
`LOOM_BRIEF_AUTHORS` and `LOOM_REPO_OWNERS` from the environment of the
install (empty `LOOM_REPOS` keeps it idle) and uses `LOOM_GITHUB_TOKEN`,
`GITHUB_TOKEN` or `gh auth token`. docs/BRAIN.md has the rules.

GitHub App: with `LOOM_GITHUB_APP_ID`, `LOOM_GITHUB_APP_INSTALLATION_ID` and
`LOOM_GITHUB_APP_KEY_SSM` (the name of the SSM parameter that holds the App's
private key, never the key itself; region `LOOM_AWS_REGION`) all set, the
station, the brain commands and any `bus` call in that environment send an
App installation token instead, so their requests spend the installation's
own hourly budget, not the operator account's. The token is minted at run
time: the key is read from SSM, an RS256 JWT is signed with the `openssl`
CLI (key on stdin, never on the command line or on disk), and
`POST /app/installations/<id>/access_tokens` returns a token kept in memory
until 5 minutes before its `expires_at`. The station runs one pass per process
and GitHub's ETags differ per token, so it also keeps the token in
`state/station/app-token.json` (mode 0600; the only other place a token is
written is a lane agent's per-run gh store, below)
and the next pass reuses it until 5 minutes before expiry; the ETags cached on
the last pass then answer 304 (loom#1306). A 401 deletes the file. Mints count under `app token` in the
requests line, and the budget line reads `GitHub App budget`. With any of the
three unset, the token above is used as before. Instead of SSM, the station
and the brain can read the key from a file: set `LOOM_GITHUB_APP_KEY_FILE` to
its path (it wins when both are set). `lanes/mac/install.sh` writes it into
both plists, refuses a key file readable by group or others (it must be mode
600), and refuses to install at all when only some of the App id, the
installation id and a key setting are set, naming the missing one, so a half
setup never quietly runs on the personal token. The station reads the App's
slug once per pass (`GET /app`, counted under `app login`) and trusts
`<slug>[bot]` wherever `LOOM_BRIEF_AUTHORS` is, so the labels and comments the
App writes need no hand-listed login; its startup log line `trusting briefing
logins` names every login trusted. More than three briefed tickets refused in
an hour for one untrusted labeller file the `untrusted-labeler` invariant,
naming the login. The App needs contents,
issues and pull requests read and write, and checks and actions read, on the
scanned repos. To set one up, `python3 -m bus app manifest --owner OWNER`
(add `--org` for an organisation) prints the create page and a manifest with
exactly those permissions and no webhook; `--form` prints an HTML page whose
one button creates the App. Install it on the repos in `LOOM_REPOS` only and
generate a private key; then, with the key in `LOOM_GITHUB_APP_KEY_FILE` or
`LOOM_GITHUB_APP_KEY_SSM`, `python3 -m bus app ids --app-id N` lists the
installations and the repos each covers, prints the `LOOM_GITHUB_APP_ID` and
`LOOM_GITHUB_APP_INSTALLATION_ID` lines to paste, and warns about a repo in
`LOOM_REPOS` the installation misses, one it covers outside `LOOM_REPOS`, and
a permission Loom needs that it lacks. It never prints or writes the key or
the tokens it mints. The dashboard takes the same three settings when its host has
the `openssl` and `aws` CLIs, and otherwise keeps
`LOOM_DASHBOARD_GITHUB_TOKEN`.

The station, the brain and the lanes each post as their own App, never as
the operator (loom#1142). The installer writes the station's App
(`LOOM_GITHUB_APP_*`) into the station and dashboard, `LOOM_LANES_GITHUB_APP_*`
into every lane and `LOOM_BRAIN_GITHUB_APP_*` into the brain, each as that
launchd job's own `LOOM_GITHUB_APP_*`; the brain keeps the station's App until
it has its own, and half an App's settings install nothing.
`python3 -m bus app token` prints the installation token of the App those
settings name, and fails with no output without them. A lane with the
settings drops any personal `GH_TOKEN` or `GITHUB_TOKEN`, mints a token at the
start of each run and again after its agent (a token lasts an hour), clones
over https with `gh` as git's credential helper, and runs its agent without
the token in its environment, the App settings or that helper; a failed mint
parks the run `[transient]` before anything is posted. So that the agent's own
`gh` calls spend the App's budget and not the operator's (loom#665), each run
writes the token into a fresh gh config directory, `$LOOM_HOME/run/<lane>-gh.*`
(mode 700, the token in its `hosts.yml`), points the agent's `GH_CONFIG_DIR`
at it and removes it when the agent exits. The brief tells the agent not to
poll pull request checks or reviews, and the run's log records
`<ticket> agent gh calls: N`, the `gh` commands counted in the agent's output. `lanes/mac/brain.sh` mints once per
tick, after the lock, and gives every rung that token as `GH_TOKEN`,
`GITHUB_TOKEN` and `LOOM_GITHUB_TOKEN`; a failed mint, or a
`LOOM_BRAIN_MINUTES` over 50, runs no rung. The station trusts its own App's
login by itself, so the brain's and the lanes' logins (`<slug>[bot]`) go in
`LOOM_BRIEF_AUTHORS`, and a repo whose workflows admit only collaborators must
admit the lanes' login for lane pull requests to run.
A new App is easiest made from
`https://github.com/settings/apps/new?name=NAME&url=URL&public=false&webhook_active=false&contents=write&issues=write&pull_requests=write&checks=read&actions=read`.

After claims on every pass, the station gives `loom:queued` tickets, in
priority order (P0 first, then the oldest), free lanes in their Difficulty tier and moves them to `loom:briefed`.
It skips future `Not before:` times and tickets without a readable Difficulty.
The latter appear in their goal's next station standup. A pass that gives no
queued ticket a lane logs `assigned 0 of N queued: <reason>`, the reason
being the first ticket's in that order.

Lane status: every pass the station reads each lane's heartbeat (`yes` when
available and at most 600 seconds older than the pass's start, else `no`; a
beat written while the pass runs counts, one stamped more than a minute
ahead of the clock does not) and keeps one comment on the
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
starts a ticket's ladder again. The ladder (claude `claude-sonnet-5-5`, then codex `gpt-6-luna`, then
`claude-opus-5-5`), each
climb recorded as a `<!-- loom:rung N -->` comment.
A climb takes the lowest rung the ticket has not run on since its ladder
last started; a rung whose lanes each already hold 2 briefed or running
tickets is passed over for the next untried one, and stays untried. Once it
ran on Opus, no stronger model is left (gpt-6-sol and gemini-4-argon are
forbidden since 2026-10-21). A ticket that failed on a ladder model
has tried that rung: the model a claim ran is its `Model:` line, else its
pull request's runner footer, else the lane's `LOOM_<LANE>_MODEL`. With no
lane of its next rung up, the ticket waits and the factory posts nothing; it
gives up ("tried every rung") only once every rung with a lane for its repo
was tried. When every
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
An answered thread that survives and leaves its ticket with no lane up for
its next rung does not wait with nothing to move it: the factory asks the
review bot to judge the answer instead, commenting `/grug improve` on the
pull request once per head commit, so Elder re-reviews the head with the
reply in view. It clears the head if the answer holds, and keeps the finding
if it does not, and the ticket then waits for its rung as before.
A fix brief that ends with nothing committed (most often
every thread was already fixed and the review thread is stale) is not a
failure: the station posts the lane's explanation on the pull request, asks
the review bot to review it again (`LOOM_REVIEW_AGAIN`, default
`/grug improve`), and returns the ticket to `loom:pr`. A pull request that
merges while its ticket waits at `loom:queued` or `loom:briefed` (a fix round
sent it back and nobody took it yet, or the pull request opened and merged
between two passes) moves the ticket to `loom:merged` with a one-line note,
under the same author and trust rules as an open pull request, unless the
ticket went back to queued or briefed after the merge. Before a fix brief's
agent runs, the runner merges the default branch into the pull request's
branch (the agent's sandbox cannot fetch): a clean merge is pushed even when
the agent changes nothing, and a conflict is handed to the agent with the
file list, its markers in the tree; the runner completes the merge, or parks
the brief with "conflict markers left in FILES".
A lane at a limit (its heartbeat reason starts `limit:`, as the spark lane's
does while its box is hot) is not failing: a ticket briefed on it moves at
once to a free lane in its difficulty tier (docs/MUSE.md, Lane choice), with
no rung marker, or a ticket on a ladder rung to a free lane of that rung; with
none free it waits where it is for its lane to come back. A `loom:running`
ticket on a local lane with no live heartbeat for 30 minutes (an unloaded or
crashed lane, or a machine that is off) moves the same way, with a comment
naming the lane and how long it was silent: to a free lane of its tier, or of
its Model, else back to `loom:queued`. It is no failure and no retry, so it
never climbs, and the station's `silent-lane` invariant lists the lane.
A lane deep in a run is not silent: its runner writes the heartbeat between
runs, so while it lives it also stamps `state/usage/<lane>.alive` with the time
every `LOOM_KEEPALIVE_SECONDS` (default 60), and the station counts a lane
with a stamp under five minutes old, like one whose runner holds its lock with
a live pid, as running. The stamp says nothing of availability. When the
factory does find a lane silent with a running ticket, the station log names
what it saw of the runner (its lock and the stamp's age).
A failed or stalled
ticket climbs, and past the last rung it is left `loom:failed` for the brain. Landing
keeps the default branch green: a ready pull request that is behind its base
is brought up to date first (GitHub's update-branch) and lands only after its
checks and review pass again on the new head; each repo lands at most one a
pass, `loom:urgent` and `loom:regression` tickets first, then by their
ticket's `Priority:`, then the oldest. A pull request whose only
fault is a failed Actions job gets that job run once more before it is sent
back. A ready pull request that changes a code file (by extension: `.py`,
`.js`, `.ts`, `.tsx`, `.jsx`, `.css`, `.cpp`, `.h`, `.hpp`, `.go`, `.rs`,
`.sh`, `.lua`, `.sql`) and no test file (a `test` or `tests` folder in its
path, or `test_`, `_test.`, `.test.` or `.spec.` in its name) goes back
asking for a test that fails without the change, instead of landing; that
round climbs only if the next one still adds no test. A repo with no test
harness opts out in `LOOM_NO_TEST_REPOS` (space separated `OWNER/NAME`), and
a ticket whose body has the line `Tests: none needed (<reason>)` skips the
check. Scope check (`bus/scope_fit.py`): with `LOOM_SCOPE_CHECK_URL` set to a
keyless https chat-completions route (for example the gateway's free-public
route for Gemma) and optionally `LOOM_SCOPE_CHECK_MODEL` (default
`gemma-4-31b-it`), a ready pull request on a public repo is checked once
before it lands: the model gets the ticket's `## What` section and the
changed-file list and answers whether the files fit the ticket and the repos
and resources it names. `YES` lands it as before. `NO: <reason>` holds it:
`needs-brain` on the pull request, a comment with the reason and a hold marker
(once per head commit, so a later pass does not ask again), and a note on the
ticket; the brain or a person reads the diff and merges it or sends it back.
No route, a limit, a timeout, a reply that is not `YES` or `NO: <reason>`, a
ticket with no `## What` section or an unset URL lands it as before: the check
is advisory and never blocks a merge on a dead route. A private repo is never
asked. At most 12 actions per pass. `LOOM_FACTORY=0` turns it off.

Merged first (`bus/fabric.py`, loom#1437): before a briefed ticket goes to a
build lane, a pull request already merged into the default branch for it sends
it to a verify run instead. A linked pull request counts (a Refs line or a
`loom/<number>` branch). With `LOOM_FABRIC_KEY_SSM` naming the SSM parameter
that holds the Fabric workspace key, a ticket nothing links is also asked
about once: the station asks Fabric's whole-library chat which merged pull
requests already do the work, and each cited pull request must be of this
repo, merged into the default branch, and carry the cited card's title. At
most 2 questions a pass and 3 citations each; the key is read for each call
and only asks, never writes. Fabric unset, down, slow or silent builds the
ticket as before.

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
line and a link; a count of the goal homes where nothing moved; one line
per backlog goal home (labelled `loom:backlog`), counted from its tickets,
since a backlog goal posts no report on GitHub; and the
factory's daily numbers, which then no longer go out on their own. Each time
is sent once, and a failed send is tried again on the next pass. Switching
the digest on sends nothing until the next digest time. Setting
`LOOM_REPORT_DIGEST_TIMES` empty forwards each report as below. Neither
pushes a report whose goal was quiet since the report before it
(docs/BRAIN.md, Quiet goals); the digest counts that goal as quiet.

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

### Two Looms on shared repos

Two operators may each run a Loom against the same repos. Each Loom works
only what is its own, and adding an operator is configuration:

- An issue or pull request assigned to a GitHub login belongs to that login.
  So does one carrying a `claim:<login>` label, which marks claims an
  operator's own sessions make outside a Loom. A Loom acts only on ones with
  no owner and on ones with an owner among its own logins
  (`LOOM_BRIEF_AUTHORS`). Triage, rewrite, the factory's queue
  routing and pull request pass, `bus.brain move`, the station's claim, pull
  request adoption and the verify picker all skip one assigned to anyone
  else. The station logs each skip once (`state/station/owned.json`).
- A claim is not forever. Once an hour the station reads each open issue
  another operator claims (a `claim:<login>` label not its own, or an
  assignment to a `LOOM_TRUSTED_BRIEFERS` login). When neither the claim nor
  any act of the claimer's on it (a comment, a label, an assignment, a
  reference from their issue or pull request) is newer than
  `LOOM_CLAIM_IDLE_HOURS` (default 24; 0 is off), and no open pull request of
  theirs names it, the station removes the claim label and unassigns them,
  at most 20 issues a run (`bus/stale_claims.py`). The issue is then this
  Loom's like any unowned one. A label removal sends no email.
- A Loom's own claim lapses too. The same hourly step reads each open ticket
  carrying this Loom's `claim:<login>` label and a Loom label (`loom:*`,
  `wait:*` or `needs-brain`). When it is not `loom:running`, nothing on its
  timeline is newer than `LOOM_CLAIM_IDLE_HOURS`, and no open pull request
  naming it (a `Refs` line) was updated within that time, the station removes
  the claim label, unassigns this operator, and adds a status note saying why,
  so another operator's Loom may take the ticket by the rules above. A wait
  label does not keep a claim alive: a ticket held only by a model limit, a
  brain or a runaway hold lapses once it has been quiet that long. A claim
  with no Loom label is an operator's own session's and is left to the
  claimed-issue settle (`bus/stale_claims.py`). The station's own relabels
  (events by this Loom's App logins) are not activity for this lapse, so a
  ticket that moves between lanes and the brain still lapses; only the claim
  label's own putting-on, a person's act, or a pull request update count.
- The claim marks a run, not a ticket's whole life. Any station or brain move
  out of `loom:running` (a retry, a limit, a failure, a pull request opened, a
  merge) removes this Loom's own `claim:<login>` label in the same labels
  write, and so does any close of a ticket. A `claim:<login>` label of another
  login is never touched. The next claim adds it again.
- When the station claims a ticket, it assigns the issue to its operator
  login, the first `LOOM_BRIEF_AUTHORS` login that is not an app (`[bot]`),
  before it relabels it, and adds `claim:<that login>` with the relabel
  (creating the label in the repo once a pass if it is missing), so the other
  Loom sees the claim before it acts and a label search lists every claim. A
  refused assignment is logged and the claim goes on.
- A goal home may name its operator with an `Operator: <login>` line in its
  header, above the first heading (as `Trunk:` is written). A Loom works only
  goal homes that name one of its own logins, or name none: a goal home with
  no such line belongs to every `LOOM_BRIEF_AUTHORS` login, as before. The
  station leaves another operator's goal home out of triage, rewrites,
  adoption, reports, standups and invariants, and claims no ticket under it.

Each operator keeps their own `LOOM_BRIEF_AUTHORS`; neither lists the
other's login. A person working a ticket by hand claims it the same way:
assign it to yourself and add `claim:<your login>` before any Loom briefs it.

To find what is free to take, run `python3 -m bus.free OWNER/NAME ...
--operator <your login>` (read only), or open the dashboard's `/free` page
(`?operator=<your login>`). An open issue is free when no one else is
assigned or holds a `claim:` label on it (the same `owned_elsewhere` rule
triage uses), it is not an epic, goal home or Loom ticket, and no open Loom
ticket covers it: none says "Rewritten from issue M." or "Refs #M" in its
body, and none is named by a "Rewritten as ticket #N" or "Briefed as #N"
comment on the issue. `loom:triage` and `loom:rewritten` alone do not hold
an issue. An issue whose covering ticket is closed or merged is listed apart,
to verify and close; so is a `loom:rewritten` issue whose ticket is not found.

Each Loom sets these for itself when it installs; a second operator running
from a clone of another operator's checkout must not inherit them:

- `LOOM_REPOS`: the repos this Loom watches.
- `LOOM_REPO_OWNERS`: the owners whose repos it may brief (default: this
  checkout's owner).
- `LOOM_STATUS_REPO`: where the brain lock `loom-lock/brain` and the
  `loom:lanes` issue live (default: this checkout's origin).
- `LOOM_BRIEF_AUTHORS`: this operator's own logins.
- The App settings, `LOOM_GITHUB_APP_ID`, `LOOM_GITHUB_APP_INSTALLATION_ID`
  and `LOOM_GITHUB_APP_KEY_SSM`, so each Loom spends its own installation's
  budget.
- `LOOM_BRAIN_HOLDER`: the name on the brain lock (default `lane-machine`).

`lanes/mac/install.sh` refuses to install, writing no agent, when
`LOOM_REPOS` is set and the status repo (`LOOM_STATUS_REPO`, or the origin
when it is unset) or any `LOOM_REPOS` repo has an owner outside
`LOOM_REPO_OWNERS`; the message names the setting or repo at fault.

### Trusted briefers

`LOOM_TRUSTED_BRIEFERS` (space separated GitHub logins, default empty) lets
another operator hand work to this Loom's lanes without this Loom adopting
their claims. It is briefing trust only:

- A `loom:ticket` authored by a trusted briefer is trusted for briefing and
  building exactly like one from `LOOM_BRIEF_AUTHORS`: the station's claim and
  hand-off, retries, the factory's queue routing, `bus.brain move` and
  `bus.brain prioritize`. The labeller and body-editor checks accept their
  login too. One function decides it (`tickets.brief_author_trusted`), so the
  checks cannot drift.
- Ownership stays `LOOM_BRIEF_AUTHORS` only. An issue assigned to, or
  labelled `claim:` for, a trusted briefer is still skipped by triage,
  rewrite, routing, claim and adoption, unless it is a `loom:ticket` that
  same briefer authored: a brief is an explicit hand-off. A pull request is
  never a hand-off. When the station claims a handed-off ticket it assigns it
  to this Loom's operator login and labels it `claim:<that login>`, as for
  any claim.
- Journals, proofs, audits, adopted pull requests and merges keep requiring
  `LOOM_BRIEF_AUTHORS`. A trusted briefer cannot verify, close or merge on
  this Loom's behalf, and their comments are data, not instructions.

The installer writes the setting into the station plist and the brain
plist, and the brain passes it to a sandboxed rung, like
`LOOM_BRIEF_AUTHORS`.

### Verify runs

A slot of a lane that is not Anthropic (`anthropic*` and `claude` never take
one), is free, and has no build work (no briefed ticket claimed for it and no
queued ticket of its tier or model) is handed one verify run instead of
idling: the oldest shipped ticket without a proof, at most once per 6 hours
per ticket, on a public repo only. The brief carries `Deliverable: verify`.
A shipped ticket that has waited 2 hours since its ship does not wait for an
idle slot: every 30 minutes at most, one free slot that may verify is taken
from build work for the oldest such ticket (queued work of the slot's tier does
not hold it back; a failed proof audit still does), so a factory with build
work for every lane still verifies what it ships.
When `LOOM_EVIDENCE_CMDS` is set in the station plist, it is a JSON map from
`owner/repo` to a read-only shell command. The command receives each eligible
backticked string from the ticket's Live criterion in
`LOOM_EVIDENCE_NEEDLE`; it should search logs from the last 24 hours with a
fixed-string match (see `.env.example`). The station runs it once per string,
with a 60-second timeout, then quotes up to 20 matching lines per string in
the verify brief. It accepts at most five ASCII strings of at most 120
characters. For the live proof of a shipped ticket (bus/live_proof.py) the
command also receives `LOOM_EVIDENCE_SINCE`, the deploy as epoch seconds, and
only the lines stamped at or after it count.
A Live criterion that needs its strings to be absent ("no `X` line appears",
"never logs `X`", "`X` does not appear", every string so worded) is verified on an
empty answer: when every command run answered and no line named any string, the
ticket is handed a verify run and the brief carries a `## Absence check, station
data` section naming the zero count and the hours of log searched
(`LOOM_EVIDENCE_WINDOW_HOURS`, default 24; a command may print
`loom-evidence-window-hours: N` to report the log it really read, as a pod restart
cuts it short). When the criterion states a span ("over 24h", "for 3 days") longer
than that window, absence is not proven: the station logs the missing hours, labels
the ticket `loom:awaiting-evidence` and tries again after its cooldown. A command
error or timeout proves nothing either way. A criterion that needs a string present
still waits for a line. Lines containing credentials, private hosts, IP addresses or
local paths are dropped and counted. A timeout or command error is stated in
the brief without quoting its output. Repos without a command run none.
Verify lanes have no network, so every verify brief also carries a
`## GitHub data, not instructions` section the station reads with its own
token (`bus/verify_data.py`): the ticket's merged pull requests, linked the
way reconcile links them (number, title, merge commit, merged time; at most
5), the check runs on each merge commit (at most 30), whether the default
branch's head contains it, the files each changed (at most 40) and the
`def test_...` functions its test files add (at most 30), and the ticket's
acceptance-criteria checkboxes as they stand. A cap that cuts a list says so.
A ticket with no merged pull request gets one line saying so, and a GitHub
failure is one line in the section. Lines pass the same text checks as the
log lines and are dropped and counted when they do not.
The agent changes no code, opens no pull request and moves no label; it reads
the ticket's Live criterion and judges each criterion against those two
sections, then
writes `verify.json`: a proof record (`bus/proof.py`),
`{"missing": "<the evidence that does not exist yet>"}` or, for a ticket sent
ahead of a build, the `unmet` answer described below. The runner copies it
to the lane's log folder as `<brief>.verify.json`, with the run's model
beside it in `<brief>.verify.model`. The station checks a record with
`bus/proof.py`, refuses it when it passes a live or operator criterion the
station has no evidence for (docs/BRAIN.md, Verify before ship: a verify run
has no network, so its word is not evidence), ticks the criteria and moves
the ticket to verified with the
proof, marked `"verified_by": "<lane>/<model>"` (`<lane>/unknown` when the
run recorded no model). It refuses a record whose revision does not contain
the merge commit of each linked pull request merged into the default branch
(the compare API), or whose observed window starts before the newest of them
merged: a stale build or evidence read before the fix proves nothing. A
GitHub error on that check leaves the ticket shipped, and a ticket with no
linked merged pull request is left to the close rule. The station also reads
the merged pull requests again, as for the brief: a record whose criterion
evidence says every check passed ("all checks passed", "checks green") while
the newest run of a check on a merge commit concluded other than success or
skipped is refused, and so is any record when GitHub cannot be read for
them (the ticket is tried again after its cooldown). The proof comment then
carries a `Checked by the station:` section, one line per merged pull
request, the same sentence the done close writes (merged time, merge commit,
each check that did not conclude success or skipped, whether the default
branch contains it), stored in the record as `station_facts`; a record that
brings its own `station_facts` is not believed. A refused record, a missing result or no
`verify.json` leaves the ticket shipped with one note on it. The run is
counted like any lane run, and the Spread line also shows how many were
verify runs. The brain audits lane proofs (docs/BRAIN.md, Proof audits): a
lane whose proof failed its audit in the last 24 hours is handed no verify
run, and the daily line's Throughput number splits the verified tickets into
lane and brain proofs.

A ticket the station sent to verification before any build (a merged pull
request already covers it; docs/BRAIN.md, Claim and outcome) gets one more
section in its brief, `## Sent to verification before any build`, and one more
possible answer: `{"unmet": "<which criteria the merged change does not meet
and why>"}`. The station then moves the ticket back to `loom:queued` for a
build, with the reason as a status event, and does not send that pull request
to a verify run again. An `unmet` answer on any other ticket, or one that says
nothing, only leaves the ticket shipped.

A ticket whose proofs were refused `VERIFY_REFUSAL_CAP` times (3,
`bus/station.py`; the count is kept in `state/station/verify-refused.json`)
leaves the verify queue instead of coming back every 6 hours: the station
posts one comment listing the refusals and adds `needs-brain`, and the brain
proves the ticket or rewrites it. Every station log line starts with its UTC
time (`[2026-10-21T08:29:40Z] station: ...`), so evidence that quotes a
station log line can name when it was seen.

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

The same finished tick is also posted once as a `brain_ticks` row to the
dashboard's `/api/brain/ticks`, next to `LOOM_TRANSCRIPT_URL` and with the
same token (bus/brain_memory.py, docs/DASHBOARD.md). The row takes its
holder, lane, model, start, end and outcome from `state/brain.json`; what it
decided from the step notes on the lock commit (when that holder started it
within 15 minutes before the agent) and the `Shipped` lines of the journals
the holder posted while the agent ran; what it left open from their defer
markers and `Waiting` lines; and what it handed off from their `Briefed`
lines. Its commitments are those defer markers, open and due 2 hours after
each was written, and the done markers any brain wrote since the last ones
sent (and from the tick's start), each closing the commitment for the same
stage, item and brief key. Between ticks, at most every 10 minutes, the
station sends the new done markers to `/api/brain/commitments`, so work
another brain finished closes without waiting for this machine's next tick.
After a tick posts, the station loads the rules digest's sources that
changed: the Rules section of `docs/MUSE.md`, keyed by the commit that last
changed it, and each goal home's rules, keyed by its newest rules comment
from an allowed login (one opening with a "Rules" or "Loom rules" heading),
else by a digest of the body's Rules section. A source that no longer has
rules (a closed goal, a removed section) is retired; `docs/MUSE.md` only
when it was read. The ledger `state/station/brain_memory.json` keeps the
last tick's result, where the sent done markers end, and the loaded rules; a
failed tick post is tried on the next pass, 5 times in all.

Before the agent starts, `lanes/mac/brain.sh` reads the handoff packet with
`python3 -m bus.brain_memory handoff` (the token from the same SSM
parameter) and puts it in the prompt. With either setting empty, or the
dashboard down, the tick runs without it.

## Disk

The loom home is kept bounded by the station and the runner (loom#643).

- A lane's logs (`logs/<slot>/`: each run's log, result, validation records
  and archived transcripts) are removed once nothing in an entry changed for
  `LOOM_LOG_KEEP_DAYS` days (default 7; `0` keeps them all). An entry is kept
  however old while a brief of its name is waiting or running in a lane
  inbox, and, while transcripts are uploaded, until the ledger holds the run
  as sent or settled; an unreadable inbox or ledger removes nothing. The
  station's own logs at the top of `logs/` are not touched.
- The runner removes a check's scratch home (`run/<slot>-check`) after the
  final check and when it exits, so `run/` holds one only while that lane runs.
- While the home's disk has less than `LOOM_MIN_FREE_GB` free (default 15;
  the older name `LOOM_DISK_FREE_MIN_GB` is still read) the station claims no
  new work (loom#1808). It says `claims paused: ...` in `station.log` once
  when the state changes, `claims resumed: ...` once when the disk has that
  much free again, and the lane status shows a `Claims paused` line
  meanwhile. The state is kept in `state/station/disk-guard.json`.
- When a run ends the runner drops the build output git ignores from its
  tree and its default-branch check copy: `.venv`, `node_modules`,
  `__pycache__` and `.pytest_cache` folders that `git check-ignore`
  confirms and that hold no tracked path. The tracked tree stays for fix
  rounds.
- A lane tree is removed `TREE_KEEP` (an hour) after its run, or at once,
  with its `-default-check` copy and the runner's files beside it, when its
  ticket is done: its pull request merged or closed, or the ticket closed or
  dropped, as that pass read it. A tree whose ticket has a brief in a lane
  inbox stays. An idle lane (no runner lock with a live pid, an empty inbox)
  loses its run scratch (`run/<slot>-home`, `-check`, `-check-default`) once
  the ticket it last ran is done, and its home's `.cache`, `.cargo` and `tmp`
  every pass meanwhile.
- Every hour the station removes the old lane logs, compresses each finished
  run's opencode session store (`logs/<slot>/<run>/.local/share/opencode/
  opencode.db`, to `opencode.db.gz`, once its transcript is uploaded; the
  readers read either), and logs the home's size by top-level folder
  (`loom home holds ...`), kept in `state/station/disk.json`. A compressed
  store goes with its run's other logs after `LOOM_LOG_KEEP_DAYS`.

## Log watch

Each station pass reads the lines each component added to its log since the
last pass: by default `logs/station.log` (as `station`), `logs/brain.log`
(`brain`) and each lane's `logs/<lane>-service.log` (named after the lane).
`LOOM_WATCH` replaces that list with lines of `name=path`, a relative path
being under `$LOOM_HOME`. Each line first loses its terminal escape codes
(colours and the like, raw or shown in caret notation as `^[[91m`) and other
control characters. A line counts when it says `error`, `failed`,
`Traceback` or `refused`, or carries an HTTP 4xx or 5xx; a Python traceback
counts once, as its exception line. A line of a GitHub Actions job log that
an agent echoed (a seven-digit-fraction timestamp, after the job and step
names `gh run view --log` adds) never counts: it is the run's output, and a
green run's test names say `failed` too. Nor does the brain's own status
output: a stage handoff line (`- stage failed: held by ...`), a markdown
heading of its report (`#### failed`), a ticket or goal row of the report
(free-text titles and `REPEATEDLY STUCK` marks), a defer resume line
(`- dropped failed ...: finished or closed since the defer`), or the tick
record or journal comment it echoes as JSON. Anything more on such a line is
judged as usual. Each line is reduced to a signature:
times, numbers, hex ids and SHAs, long mixed ids, repos and issue numbers,
paths, addresses and mentions become placeholders, so a 403 on two repos is
one signature (the status code is kept).

An agent CLI's sandbox or permission refusal is not an error and never a
signature: opencode's `The user has specified a rule which prevents you from
using this specific tool call` (a lane or the spark brain rung reaching
outside its tree, which `lanes/agent_sandbox.sh` denies on purpose) or `The
user rejected permission to use this specific tool call`, claude's `Claude
requested permissions to ... but you haven't granted it yet` and `Permission
to use ... has been denied`, codex's `rejected by user approval settings`
patch refusals and gemini's `Tool execution ... denied by policy`. The agent
gets it as a tool error and carries on (loom#202). The watcher counts each
refusal on its run, keyed by the tool and the denied path; the line opencode
prints right before it (`<cross> Read <path> failed`) names both and is not a
signature either. A run is what the log marks between a start and an end:
the brain's `tick start (lane <rung>, ...)` and `tick end (exit <n>)`, or a
lane run's `<ticket> start, lane <slot>` and its `done, ...` or `failed:
[<class>] ...` line. Refusals outside a marked run settle at the end of the
pass as one run of unknown outcome.

When a run ends its refusals go into `watch.json` (the day's runs and
counts per refusal) and the hourly summary line (`N agent refusals (M that
hurt a run)`). A refusal files an issue only when it hurt:

- the run failed (a non-zero tick exit, or a lane failure not classed
  `[transient]` or `[limit]`) or made no change (`made no commit`,
  `nothing to change`);
- it repeated 5 times in that one run (`REFUSAL_REPEAT`); or
- it came back in 5 runs within 24 hours (`REFUSAL_RUNS`), even when each
  carried on: a brief or prompt that keeps steering the agent outside its tree.

Then it files or counts one `loom:triage` issue per component (with the
brain's rung or the lane), tool and denied path, titled `<component>/<lane>:
sandbox refused <tool> <path>` (`brain/spark: sandbox refused Read
~/.config/gh/hosts.yml`). It names the CLI, the denied path, the rule (for
opencode the last deny rule it lists, `external_directory * deny`), how often
in the last run and the last 24 hours, and the effect, so a person decides:
widen the sandbox for that path or teach the brief to stay inside it. The
watcher never loosens the sandbox. The path is shown with the home directory
as `~`, a temp directory as `<tmp>`, a goal repo's name (any `LOOM_REPOS`
repo but the lanes repo) as `<repo>`, and numbers and ids as placeholders, so
one path is one issue; a path that still fails the written-text check (an
absolute path outside home and temp) is withheld. The issue then rolls like a
signature's: one count comment an hour while the refusal recurs, edited in
place, also for runs it did not hurt, and a quiet close after 24 hours.

A signature seen 3 or more times within 30 minutes, and not seen in the 24
hours before, files one `loom:triage` issue in `LOOM_STATUS_REPO` titled
`<component>: <signature>`, with the count and the first and last example
lines. An example keeps the whole line up to 2000 characters; a longer one
keeps its start and its end around ` ... `. The examples have paths,
credential shapes, mail addresses, mentions, private hosts, IP addresses and
repo names replaced, and each must pass the
written-text check (`bus/text.py`) or is withheld. An open `loom:triage` issue
with the same title is counted instead of filed again. While the signature
keeps repeating its open issue gets a count comment at most once an hour,
edited in place and saying when it was last seen; a closed one is left alone.
The issue a signature was filed or counted on is remembered until it closes,
so a signature that comes back after a quiet day is counted on that issue
even after the brain has retitled or relabelled it: one rolling issue per
signature. No model is involved: diagnosis is the brain's.

`state/station/watch.json` keeps each log's inode and offset, each
signature seen in the last 24 hours, and each signature's open issue (at most
500). The first scan starts at the end of each
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
held by a limit, queued tickets, each goal home's epics and tickets (one walk
through goal, epics and tickets), the review bot's plan
check on open lane pull requests (`LOOM_PLAN_CHECK`, default
`Grug - Chief`), the lane pull requests nothing drives and remote heartbeats.
Each invariant is one function returning breaches with evidence:

- One brain's two newest ticks each journaled a failed or degraded step
  (`- Step <name>: failed ...`, docs/BRAIN.md Journal), or no brain journaled
  for 90 minutes once one has been seen.
- A `loom:metrics` line posted more than once for one day, or two
  `loom:report` comments within 90 minutes, on one goal home.
- A lane with 5 or more runs in 24 hours and none done (one issue per lane:
  the station reads these to release a runaway a dead lane held).
- Lanes whose newest 5 runs all failed after a success earlier in the day,
  limits, expired keys and factory incidents aside.
- Lanes held by a limit for over 2 hours while tickets of their tier are
  queued. Only truly queued tickets count: `loom:queued` is their only state
  label, and no other operator has claimed them. `LOOM_NO_TOPUP` names the
  provider lanes whose credit the operator never tops up (for example
  `anthropic`); a slot of one held out of credit is skipped, since it waits
  for the credit to renew and nothing is briefed onto it meanwhile. Unset,
  every held lane counts. A hold whose reset has passed is no hold: the lane
  is free again, as the Usage view reads the same heartbeat.
- The epic shape (decision 0003): a goal home with open `loom:ticket` issues
  under no epic (one rolling issue for every goal, a line per goal home,
  rewritten in place while the list changes; the older per-goal issues are
  closed as folded into it), an epic holding more than 15 tickets,
  a goal with more open epics than its cap (6, or the goal home's
  `Epic cap: N until DATE` exception), and a closed epic that still holds
  open tickets. These replaced the goal home sub-issue count check.
- An open lane pull request whose plan check failed more than 30 minutes ago.
- An open lane pull request nothing drives for over 6 hours (the time a lane
  has to open one): no open `loom:ticket` carries it, its ticket is past the
  last rung of the model ladder (`loom:failed`), its ticket is marked
  `needs-brain`, or it holds unresolved review threads from the review bot
  with no fix round queued. The breach names which of those it is and the one
  command that unsticks it (`bus.brain move`, `bus.brain brief` as a
  re-ticket, `bus.brain fix`, or closing the pull request as superseded).
- A remote lane whose newest heartbeat is older than its `beat_max`.
- Lanes that held a running ticket while they sent no heartbeat
  (`silent-lane`, above).
- Goal homes whose body names a roll (the word `roll` or `rolls`) that no
  `LOOM_ROLL_SHIP_DOCS` entry names (`roll-pin-off`, one rolling issue for
  every such goal): the station opens no source-pin pull request when their
  module changes merge (README.md, Roll source pins). The issue says when
  the setting is empty, which turns the pass off.

The failing-streak, lane-held and silent-lane invariants are one rolling
issue each, a line per lane, rewritten in place while the list changes, as
the tickets-outside-an-epic issue is: one provider limit opened 9 lane-held
issues in 6 minutes on 2026-10-15. An older per-lane issue of the same
invariant is closed as folded into the rolling one, unless it carries a
`needs:<login>` label, which leaves an operator's ask open on its own issue.

The lane-held issue is also a standing one (loom#845): when no lane is held any
more the station edits it in place to say "No lane is held by a limit now" with
the time it went clear, and leaves it open; the next hold edits it back, so a
hold that comes and goes never closes and reopens an issue (each reopen mailed
the people watching it). The lanes it listed move into a "Hold log" in the body
(a line per hold, newest first, the last 20).

Each breach files one issue labelled `loom:triage` and `loom:invariant` under
the standing repairs epic (docs/BRAIN.md, Filing under an epic; the goal home
itself when that epic cannot be read) of the invariant goal home
(`LOOM_INVARIANT_GOAL`; empty means
`LOOM_METRICS_GOAL` when it is in the self repo, else the self repo's one
goal home that is not a backlog), as a sub-issue and with a `Part of #N`
line, carrying the evidence. A hidden marker holds a hash of the invariant
and its subject: an open issue for the same breach files nothing, and a
closed one is reopened only for evidence newer than its close, at most once
while the breach stands (it stays closed if something closes it again before
the breach clears; the station logs that and says nothing on the issue). The
reopen is one "Broken again" note on the issue, edited with a count and the last
time, never a new comment each time. An issue that carries `loom:ticket` (an
invariant issue rewritten into work) is no longer the breach's issue: it is
never reopened, rewritten or commented on, and a breach that outlives a closed
ticket gets one separate invariant issue that refs the ticket (loom#1223
flipped 18 times in a day before this). Issues and pull requests in a private repo are named in words, and
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
loopback connection outside their provider's port (443, and the spark endpoint's on the spark lane); they
cannot query the viewer or any other local service.

## API lanes: openrouter, opencode-go, anthropic, copilot (and zen, zai below)

- `openrouter` (free models via opencode), `opencode-go` (subscription models
  via opencode), `anthropic` (API credits via the official Claude Code CLI),
  and `copilot` (GitHub Copilot CLI with its Free allowance).
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
- Writes go only to the worktree (its `.git` stays read-only) and the scratch
  home, plus the device nodes every CLI opens (`/dev/null`, `/dev/fd/*` and a
  few more): nothing outside HOME is writable either, `/tmp`, the runner's
  temp directory and Homebrew's prefix included.
- Outbound connections go only to the provider's port: 443 (the provider's
  HTTPS API, which also carries the CLI's own model catalogue and package
  fetches), plus on the spark lane the port of `LOOM_SPARK_URL`. Every other
  port, on any host, is refused. A Seatbelt profile cannot name a host, and on
  the same port it cannot tell loopback from a remote address, so 443 stays
  open on loopback as well; every other loopback port stays closed. DNS goes
  through the system resolver's socket. The agy lane and the validation run
  keep the network and their writes outside HOME.
- A brief whose branch already has a PR in any state moves to `done/`
  without running. Restarts cannot re-run a ticket.
- Otherwise: fresh clone, the lane's official CLI with the brief, then the
  runner (not the agent) pushes the branch and opens the PR in the format
  the Chief check requires.
- `copilot` reads its token from `LOOM_COPILOT_KEY_SSM` and exports it only as
  `COPILOT_GITHUB_TOKEN`. It invokes one `copilot -p` prompt with
  `--allow-all-tools`; the run output, including any CLI usage details, is
  retained in the lane log. Monthly usage-limit messages hold the lane until
  the next UTC month begins.
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
  `GoUsageLimitError` holds the lane until the reset the provider's
  `Retry-After` header gives (heartbeat `limit: the provider's weekly limit
  ...` with a `retry_at`) and hands the brief on as `[limit]`; a provider that
  gives no reset holds the lane for a week, the fallback for a window the
  runner does not know. Any other answer lets the run go ahead. Another
  provider can name its own endpoint in the same setting.
- A lane held by such a limit asks the provider again every 30 minutes and
  comes back as soon as it serves the lane again, because the provider's
  allowance refills while the lane sleeps (OpenCode Go refills 20% of a
  model's allowance every 5 hours). It asks about the model the probe found at
  a limit (or the slot's own model, when the slot now runs another), and
  only for a hold the probe itself took: a hold the agent CLI
  recorded (a model it no longer offers, a spent allowance) stands, for no
  probe says otherwise. On 2026-10-20 the four opencode-go slots stayed held
  for days (the provider's `Retry-After` was never read, so every limit fell
  back to a week) and the lane-held invariant fired while 33 tickets of the
  tier waited.
- The opencode-go slots run only the models on `bus/tickets.py`
  `OPENCODE_GO_MODELS`: Go's free models and the paid ones allowed at least
  10,000 requests per 5 hours (Beam Free; MiMo-V2.6-Flash, DeepSeek V4.1
  Flash, Muse Spark 1.3 Contributor). A slot whose model, or any model of its
  ladder, is off the list does not start: its heartbeat says why. A brief's
  Model: line naming another Go model runs the slot's own model, and the
  station never routes such a pin to these slots. On 2026-10-23 Kimi K2.7
  Code (1,350 requests per 5 hours) spent the plan's monthly allowance in
  hours. Defaults: `opencode-go` and `opencode-go-2` Beam Free, `opencode-go-3`
  DeepSeek V4.1 Flash, `opencode-go-4` Muse Spark 1.3 Contributor.
- A Go limit never holds a slot on a free model (`opencode-go/*-free`): the
  free models sit outside the plan's allowance, so such a slot is a hold
  group of its own. An expired key still holds all four.
- The Usage view's meters for the free lanes (loom#812). After a ready
  heartbeat the openrouter and workersai lanes read their provider's
  read-only meter with the key they already hold, at most every five minutes,
  through `bus/meter.py` (key and account id on stdin, never argv or a log),
  into `state/usage/<slot>.meter.json`; `bus/usage.py` shows it. openrouter:
  `GET /api/v1/key`, the key's free-model requests in the current UTC day and
  its credit limit if it has one. workersai: the GraphQL Analytics API,
  `aiInferenceAdaptiveGroups` `totalNeurons` for today, against
  `LOOM_WORKERSAI_DAILY_NEURONS` (default 10000, the free allocation; reset
  00:00 UTC); the token needs Account Analytics read besides Workers AI, or
  Usage says the read failed and why. `LOOM_<SLOT>_METER_URL` overrides the
  endpoint. zai and zen have no meter: Z.ai documents no usage endpoint for
  API keys (its quota monitor covers the GLM Coding Plan only) and Zen's free
  models take no key, so Usage says "no meter: provider exposes no usage".

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
random high loopback port (49152-65535) inside the agent's sandbox. The
port must bind free on 127.0.0.1 when picked, and the server does not start
if something listens on it by the time it would, so the agent never talks to
another process's listener. When no port can be had, the run parks with the
reason in its log. It allows only that
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
- the `copilot` lane (Free allowance; public repositories only),
- `opencode-go` runs of a Muse Spark Contributor model (it trains Meta's
  models); other Go models keep 0 to 30 days and take any repo.

## API lanes: zen, zai

- `zen`: OpenCode Zen free models through opencode, `-m opencode/<id>-free`
  (default `opencode/deepseek-v4.1-flash-free`). Zen serves its free models
  with no stored credential, so the lane reads no key; the agent's
  environment holds no provider key at all. opencode sends Zen's
  `x-opencode-session` header itself, and Zen refuses free-tier calls from
  any other client (FreeTierError). Zen rotates its free models often: on
  2026-10-19 `space-bunny-free` had left the catalogue, and zen runs
  failed "Unexpected server error". Check `https://opencode.ai/zen/v1/models`
  for the current `-free` ids before changing the default, and try a
  candidate with `opencode run -m opencode/<id>` (that day
  `minimax-m3.1-flash-free` answered in seconds; three others hung; it left
  the catalogue on 2026-10-22. On 2026-10-23 eight free ids each fixed a
  five-test ticket; `deepseek-v4.1-flash-free` took 22 s with 125
  reasoning tokens).
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
 When the base check left a uv cache, the agent gets a copy of it and runs uv
 offline (`UV_OFFLINE`, `PIP_NO_INDEX`), so tests that need packages run
 without network (loom#1665).
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

## free provider slots on the spark-gateway

Set `LOOM_FREE_GATEWAY_URL` to your gateway's origin. The five free slots use
separate pinned routes: `free-ollama` uses `/free/ollama-cloud/v1`,
`free-gemma` uses `/free/gemini/v1`, and `free-nvidia`, `free-kilo` and
`free-groq` use `/free-public/nvidia-nim/v1`, `/free-public/kilo/v1` and
`/free-public/groq/v1`. A slot's `LOOM_FREE_<PROVIDER>_URL` overrides its
derived URL. Each slot probes its own route's `/models` before a run. A 503
holds only that slot for 15 minutes as a limit, so the station can assign the
ticket elsewhere without counting it as a work failure. Ollama and Gemma
accept private repos; the other three accept public repos only. The routes
need your own gateway and provider configuration.

## opencode lanes: one continue for a described step

Every opencode lane (openrouter, opencode-go, zen, zai, workersai, spark and
the free slots) runs `opencode run --format json`. A model that answers with
a described step ("Let me read the build files.") and then stops, with no
tool call, no commit, and no "nothing to change" summary, gets one more
run with `--continue` asking it to carry out that step. A second such end
fails the ticket as "ended on a described step without taking it". The spark
lane's log keeps the raw events; every other lane's log keeps what a plain run
printed (the model's text and any non-event line, such as a provider error),
so its failure classes and the final message quoted in a failure reason are
unchanged for a run that did not end on a described step.

## spark: a local vLLM coder, one stream at a time

- opencode against a vLLM server you run yourself, through a custom
  OpenAI-compatible provider named `spark`. `LOOM_SPARK_URL` is its base URL
  (e.g. `http://your-spark.internal:8000/v1`, reachable from the lane machine;
  besides 443 the agent's sandbox opens only that URL's port, the scheme's
  default when it names none); it takes no key.
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
  `gemini-3.8-flash-high`. A bare model in the brief gets
  `LOOM_AGY_EFFORT` (`high` by default); a model with `-low`, `-medium`, or
  `-high` keeps its suffix. A model outside the catalog is refused as a
  transient lane mismatch before agy starts.
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
  web fetching (`read_url`, `execute_url`). `enableTerminalSandbox` is
  false: agy's own terminal sandbox denied commands those rules allow, and
  the `sandbox-exec` profile stays the boundary. Of the old file, only
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
- agy's sign-in file, `Library/Application Support/antigravity/antigravity-oauth-token`,
  is readable only by agy's own binary (a profile rule on the process path).
  A command agy launches cannot read, link, move or delete it, or move its
  folder; the same profile keeps the keychain server and `Library/Keychains`
  denied. No token helper outside the sandbox is needed.
- Before each run, the runner clears agy's memory of earlier runs
  (`agy_forget`), deleting everything in its state directory except sign-in
  and install state. Kept entries in the state directory are `settings.json`,
  `bin`, `builtin`, `installation_id`, `jetski_state.pbtxt`, `updater`,
  `last_check.timestamp`, `log`, `cli.log`, `crashes`, and `cache`; kept
  entries under `cache` are `onboarding.json` and `default_project_id.txt`.
  Kept entries must be real files and directories: a link left in place of
  a kept entry is removed, never followed, so deletion cannot reach into
  the keychain folder or another ticket's tree.

## Lane pods: an API lane on a Linux cluster

An API lane can run in a container on a Kubernetes cluster instead of on
the station Mac, so each run's checkout and agent state live on the
cluster's disk, not the Mac's (quadseven/loom#1824). The pod runs the same
`lanes/run_lane.sh`; `lanes/k8s/pod.sh` starts it for each slot in
`LOOM_POD_SLOTS` every `LOOM_POD_INTERVAL` seconds (default 60), as launchd
does on the Mac.

What changes in a pod:

- `LOOM_SANDBOX=bwrap`: the agent and the validation run are confined by
  bubblewrap instead of `sandbox-exec`. The sandbox root is an empty tmpfs
  with only the system folders (`/usr`, `/bin`, `/lib*`, `/etc`, `/opt`)
  bound in read-only, so HOME, `/run/secrets`, `/var/run/secrets` and the
  loom checkout are not there at all (the two secret folders are also masked
  with empty tmpfs). The run's tree and scratch home are the only writable
  binds, `.git` stays read-only, the CLI's folder and the skill snapshot are
  read-only, and `/tmp` is private. A validation run gets the same root:
  its writes go to the tree, its check home and `/tmp`. A fresh `/proc` in a
  new PID namespace hides the runner's processes. bwrap needs user
  namespaces inside the container; a lane whose bwrap cannot start reports
  itself unavailable (`sandbox_ready`). The agy lane and the codex and
  claude lanes stay on the Mac.
- Egress, fail closed: bwrap narrows no network, so the pod's network
  policy must (no cloud metadata address, no API server, no cluster-internal
  ranges; DNS and 443 to the internet only). `sandbox_ready` proves it
  before every pass. `LOOM_EGRESS_DENY_PROBES` names blocked destinations as
  literal `IPv4:port` (a name is refused: one that does not resolve would
  fail to connect and pass as blocked); the API server at
  `KUBERNETES_SERVICE_HOST` is added to them when the pod has it; each must
  fail to connect. `LOOM_EGRESS_ALLOW_PROBE` (`host:port`, the provider's
  HTTPS endpoint) must connect, so a pod with no network or no DNS does not
  pass because everything fails. Anything else holds the lane unavailable,
  with the reason in its heartbeat, so a policy the cluster network does not
  enforce holds the lane too.
- `LOOM_DROP_TREES=1`: each run's tree, the runner's files beside it and the
  scratch homes are removed when the run ends, whatever its outcome
  (`drop_run_scratch`). Logs and the brief stay under `LOOM_HOME`.
- `LOOM_SSM_DIR`: a folder under HOME holding a copy of each SSM parameter
  the pod reads, at the parameter's name (`/a/b` is `$LOOM_SSM_DIR/a/b`),
  kept in step with SSM by the cluster. `ssm_value` reads the file and makes
  no AWS call, so the pod holds no AWS key. The GitHub App key comes from
  `LOOM_GITHUB_APP_KEY_FILE`, also under HOME.

Everything the agent must not read (the copied parameters, the App key,
`LOOM_HOME`) sits under HOME, outside the system folders bwrap binds; the
agent sees nothing there but its own tree and scratch home. The pod mounts
no service account token, and its code is read-only and owned by root. The image carries bash, coreutils, git, gh,
python3, uv, rsync, openssl, netcat, procps, bubblewrap and the opencode
CLI, plus this repo and the leak scanner at a pinned commit.

A long-lived lane pod takes no work from the station. Lane runs on the
cluster go through ephemeral runners instead (next section).

## Lanes on cluster runners

A slot named in `LOOM_RUNNER_SLOTS` (space- or comma-separated, empty by
default) has no runner on the station's machine. Each brief the station
claims for it is dispatched to the `lane` workflow
(`.github/workflows/lane.yml`), which runs `lanes/run_lane.sh` once for that
brief on an ephemeral self-hosted runner, with `LOOM_SANDBOX=bwrap` and
`LOOM_DROP_TREES=1`. The runner pod and its disk go when the job ends, so no
tree, cache or session database stays anywhere. API lanes only: opencode-go,
zen, openrouter, zai, workersai, the free provider slots and spark (both
through the gateway). codex, claude, agy and anthropic stay local.

The runner image carries the lane runner's tools, not every tool a repo's
check calls. A check that cannot start there (exit 125 to 127 on the
unmodified base, such as `make` or `ruff` not installed) is not held behind
its factory incident, which that runner could never clear: the station
retries the ticket on another lane as a transient ending.

The job, step by step (`lanes/k8s/lane_job.py`):

1. Reads the dispatch inputs (slot, ticket key, brief, run id, the slot's
   `NAME=value` settings, the station's report public key and the run's
   own report MAC key, masked at once) from the event file, never from the
   environment, which GitHub prints in the public log.
2. Proves the runner's egress is narrowed, before any key is read:
   `sandbox_ready` from `lanes/agent_sandbox.sh` ("Lane pods" above). A runner
   that fails it runs no agent; its heartbeat reason goes into the report.
3. Assumes the lane role by OIDC and copies each value the slot's `*_KEY_SSM`
   settings name, the leak scan deny-list and the lanes App key into files
   under HOME (the `LOOM_SSM_DIR` layout, mode 600). The AWS session lives in
   that step only, and nothing printed names a parameter or a value.
4. Runs `lanes/run_lane.sh <slot>` once, with no Actions token and no AWS
   session in its environment. Its output goes into the report, not the log.
5. Seals a report of exactly what the station reads for the run: the
   brief's `done/` or `failed/` move, the run log with its failure class
   line, the `.answered` and `.verify` files, the transcripts, answers and
   the slot's heartbeat. It is a tar.gz encrypted with a random key, the
   key is RSA-OAEP encrypted to the station's own key
   (`state/runners/report-key.pem`, made on first use), because a public
   repository's artifacts can be downloaded by anyone signed in, and both
   carry an HMAC-SHA256 under the run's MAC key, so a report sealed by
   anyone else does not pass.

Each pass (`bus/runners.py`, after `finish_claims`) the station:

- Dispatches the next brief in each runner slot's inbox with its App token,
  one run per slot at a time. The run is recorded in
  `state/runners/runs.json` before the request goes, so a brief is never
  dispatched twice; a ledger it cannot read stops every dispatch.
- Reads each run's state, one request per run per pass on the existing
  cadence (conditional requests, so an unchanged run costs a 304). It
  follows only the run id the dispatch returned, and believes a run only
  when it ran `.github/workflows/lane.yml` on main, as a workflow_dispatch
  the station's own App triggered.
- When a run ends, downloads its report, checks its MAC before decrypting,
  bounds every size before reading (each zip member, the decrypted file and
  all files together, 128 MB), and writes its files into
  the loom home where a local runner writes them, then moves the inbox brief
  to `done/` or `failed/` as the runner did. The factory and the rest of the
  station read the outcome as for any lane.
- Parks the brief `[transient]`, so the ticket goes to another lane, when a
  run ends with no report, a report cannot be read, a report finishes no
  brief (for example a failed egress proof), a run fails the checks above, a
  dispatch gets no answer or no run id (never sent again), or no runner
  takes a run within 30 minutes (the run is cancelled and the slot held for
  30 minutes).
- Refuses to dispatch, and parks `[transient]` so a local lane takes the
  ticket, a brief whose inputs pass GitHub's 65,535-character limit and a
  brief for a repo that is not public. Runner slots take public repos only:
  a run's inputs and logs live in this public repository's Actions.
- Writes the slot's heartbeat each pass, as a runner would: available, or
  held until the reset a run's heartbeat named (else for 30 minutes), and a
  keepalive stamp while a run is in flight.

What the cluster side must provide:

- A runner set with the label `arc-loom-lanes`, ephemeral (one job per pod),
  scaled to zero when idle. Its image carries bash, coreutils, git, gh,
  python3, uv, rsync, openssl, procps, bubblewrap with user namespaces and
  the opencode CLI; nothing is installed at run time, and no AWS CLI is
  needed (the job signs its SSM reads itself). The job keeps its files,
  trees and scratch under the workspace (`LOOM_JOB_DIR`), which sits under
  HOME, so no agent sees it, and on the runner's large work volume, not its
  small `/tmp`.
- A network policy for the runner pods that the cluster enforces: DNS, the
  providers and GitHub on 443, the package indexes, AWS STS and SSM, and
  the gateway's in-cluster service; no cloud metadata address, API server or
  LAN. Until it is enforced the egress proof fails and no agent runs.
- An AWS role this repository's `lane` workflow assumes by OIDC, its trust
  anchored on the repository's numeric ids, the main branch and the
  workflow file `.github/workflows/lane.yml` on main, allowed to read only
  the lanes' key parameters, the lanes App key and the leak scan deny-list.
  The station always dispatches on main, and the job sets no `environment:`
  (one would change the token's subject).
- Repository variables `LOOM_LANE_DISPATCHERS`, `LOOM_LANE_ROLE_ARN`, `LOOM_LANE_AWS_REGION`,
  `LOOM_LANE_EGRESS_ALLOW_PROBE` (`host:port`, the gateway service) and
  `LOOM_RUNNER_GATEWAY_URL` (the gateway's in-cluster origin; a spark or free
  slot's endpoint URLs are moved onto it, path kept), and the repository
  secret `LOOM_LANE_EGRESS_DENY_PROBES` (literal `IPv4:port` addresses the
  runner must not reach), a secret so its private addresses are masked in
  the public log.
- The station App with `actions: write` on this repository, to dispatch and
  cancel runs (`python3 -m bus app manifest --role station`; `app ids
  --role station` checks it). The lanes' and the brain's Apps keep
  `actions: read`.

What keeps the runners safe. This repository is public, and a repository's
self-hosted runners cannot be held to one workflow, so a pull request that
adds a workflow naming `arc-loom-lanes` could start a job there. The `lane`
job itself runs only when dispatched on main, and its first step refuses a
dispatcher outside `LOOM_LANE_DISPATCHERS` (a repository variable: the
station App's login and any trusted operator login), but a rogue workflow
skips both. The real controls are:

- The OIDC role trusts only `.github/workflows/lane.yml` on main, so any
  other workflow, or this one from a branch, gets no AWS session and no key.
- Default-deny egress on the runner pods, enforced by the cluster network.
- Ephemeral pods: one job each, deleted with their disk when it ends.
- The repository setting that requires approval before workflows run for
  any outside collaborator's pull request (managed in the private
  infrastructure code).

To switch a slot over: set `LOOM_RUNNER_SLOTS` (for example
`opencode-go-2`) and re-run `lanes/mac/install.sh`. The installer removes
that slot's launchd runner and gives the station the slot's settings
(`python3 -m bus.runners station-env`: its `*_KEY_SSM`, `*_URL` and probe
settings and the lanes App's `LOOM_LANES_GITHUB_APP_*`; never a key). Let the
slot's inbox empty first: a brief left there is dispatched like a new one.
To switch back, drop the slot from `LOOM_RUNNER_SLOTS` and re-run the
installer.

## Adding a lane

A lane is a name in `lanes/run_lane.sh`, not a directory of its own: that one
runner polls, heartbeats, commits, pushes and opens the PR for every lane.
Adding one takes:

- In `lanes/run_lane.sh`, a CLI in `lane_ready`, and the name in the header
  comment's lane list. An API lane also joins the case that wants a
  sandbox (`sandbox_ready`) and a key read from `LOOM_<LANE>_KEY_SSM`; a lane whose CLI
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

## Operator steps before merge

Every lane pull request body also carries an `## Operator steps before
merge` section, after `## Out of scope`: each step a person must take
before the change can merge and deploy (a data migration, a cutover, a VM
or disk resize, scaling a workload down), one per line, or the single word
`none`. The agent writes it in `.loom/summary.md`; without one the runner
copies the ticket's section of that name, else writes `none stated`.
A pull request that lists a step is never merged by Loom (docs/BRAIN.md,
Operator holds).

## Bugs discovered

A lane that finds a bug outside its ticket writes a `## Bugs discovered`
section in `.loom/summary.md` (also read as "Bugs found", "Found bugs" or
"Discovered bugs"), one bug per line. Each line must cite an issue: `#N`
in the pull request's own repo, or OWNER/NAME#N. `bus/bug_refs.py` looks
each cited issue up and keeps the line only when the issue exists, is an
issue rather than a pull request, and is open or was filed in the last two
days; a failed lookup rejects the line. A new pull request body carries the
kept lines under `## Bugs discovered`, after the operator steps, and the
lane log names each line left out. On a fix round the PR text repair
refuses while any line is rejected, with a reason asking the lane to file
the issue and cite it, or drop the line; a round whose summary has no such
section keeps the body's earlier one. In a public repo another repo's
citation is written as "issue N of another repo".
