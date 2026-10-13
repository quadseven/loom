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
- Each pass writes `state/usage/<lane>.json` with `available` and `at`. A lane
  whose CLI or key is missing reports `available: false` and leaves its briefs
  alone; the dispatcher routes only to fresh, available lanes.

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
`python3 -m bus station` from this checkout every 60 seconds. It turns trusted
`loom:briefed` ticket issues into briefs in the lane inboxes
(`<owner>.<name>-<number>.md`, with an `Issue:` line), marks the ticket
`loom:running`, and later reports the run from `done/` or `failed/` back to the
ticket, with local paths removed from failure reasons; the runner adds
`Refs <owner/name>#<number>` to the pull request. It reads `LOOM_REPOS`,
`LOOM_BRIEF_AUTHORS` and `LOOM_REPO_OWNERS` from the environment of the
install (empty `LOOM_REPOS` keeps it idle) and uses `LOOM_GITHUB_TOKEN`,
`GITHUB_TOKEN` or `gh auth token`. docs/BRAIN.md has the rules.

Lane status: every pass the station reads each lane's heartbeat (`yes` when
available and at most 600 seconds old, else `no`) and keeps one comment on the
open `loom:lanes` issue in `LOOM_STATUS_REPO` (default: this checkout's
`origin`; the issue is created when missing) current. It edits the comment
only when a yes or no changes, when an anthropic run is recorded, and at
least hourly, and never publishes the heartbeat's reason. With anthropic
spend recorded or a budget set, it ends with the spend line ("Anthropic
spend" below).

Factory: at most every 5 minutes the station runs `bus/factory.py` over
`LOOM_REPOS`, with no model. A lane pull request (a `loom/` branch whose
ticket is `loom:pr`) whose checks have all finished, whose review check
(`LOOM_REVIEW_CHECK`, default `Grug - Elder`) has run on its head commit,
and that is mergeable, green and free of open threads is squash-merged
through `bus.brain merge`. One with a failing check, an open thread or a
conflict goes back to a lane as a fix brief. Its first send-back stays on its
lane while the pull request is under 6 hours old; later ones, failed
tickets, and briefed tickets no lane claimed for 2 hours climb the model
ladder (claude `claude-sonnet-5-5`, then codex `gpt-6-sol`, then
`claude-opus-5-5`), each climb recorded as a `<!-- loom:rung N -->` comment.
A rung whose lanes each already hold 2 briefed or running tickets is passed
over for the next. A fix brief that ends with nothing committed (most often
every thread was already fixed and the review thread is stale) is not a
failure: the station posts the lane's explanation on the pull request, asks
the review bot to review it again (`LOOM_REVIEW_AGAIN`, default
`/grug improve`), and returns the ticket to `loom:pr`.
Past the last rung the ticket is left `loom:failed` for the brain. At most 12
actions per pass. `LOOM_FACTORY=0` turns it off.

Asks: the station reads open `needs-operator` issues in each `LOOM_REPOS` repo
(ETag polls) and forwards those whose author and latest `needs-operator`
labeller are in `LOOM_BRIEF_AUTHORS` to a Discord webhook whose URL it reads
from the SSM parameter named by `LOOM_DISCORD_WEBHOOK_SSM` (region
`LOOM_AWS_REGION`, default us-east-1; never printed, and with the setting
empty nothing is forwarded). A `loom:urgent` ask is posted at once, once; all
open asks go in one digest per day after `LOOM_DIGEST_HOUR` (local hour,
default 9). Posts mention nobody except the numeric user id in
`LOOM_DISCORD_MENTION`; a failed post is retried on the next pass.

Reports: the station also reads comments on open `loom:goal` issues in each
`LOOM_REPOS` repo. On first run it records the newest report comment per repo
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

## Transcript viewer

`lanes/viewer/serve.py` (installed with the lanes as `com.loom.viewer`) serves
every lane's agent session as a chat at `http://localhost:7340/`: the brief,
the agent's messages, each tool call with its output, edits as diffs. It reads
Claude Code transcripts (claude and anthropic lanes), opencode's SQLite store
(openrouter, opencode-go) and codex rollouts. It binds localhost only and
answers only localhost Host headers; transcripts never leave the machine.
API-lane agents run on the same machine, so their sandbox denies every
loopback connection; they cannot query the viewer or any other local service.

## API lanes: openrouter, opencode-go, anthropic (and zen, zai below)

- `openrouter` (free models via opencode), `opencode-go` (subscription models
  via opencode) and `anthropic` (API credits via the official Claude Code CLI).
- The provider key is read from SSM at run time (`LOOM_<LANE>_KEY_SSM`, set at
  install, never in this repo) and exported to the agent, never passed in
  argv, in an otherwise empty environment with a scratch HOME and TMPDIR.
- The agent runs under a macOS Seatbelt profile (`sandbox-exec`): no reads or
  writes anywhere under the real HOME except its worktree, its scratch home
  and the directory holding its CLI, and no keychain access. It can edit its
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
- Logs stay on the lane machine. Nothing is pushed except the ticket branch.
- API-lane session transcripts (Claude Code `.jsonl`, opencode's `opencode.db`)
  are copied to `logs/<lane>/<ticket>/` after each run, before the next run
  wipes the scratch home. Links are never followed.
- No background processes: launchd starts a pass every minute and a lock
  keeps one pass per lane.

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
- Edits only. agy stores its token so that `/usr/bin/security` can read it
  without a prompt, and reads it that way itself, so the sandbox cannot block
  that tool; a shell command from the agent could copy the token out. The
  runner never passes `--dangerously-skip-permissions`, passes
  `--disable-slash-commands`, and tells agy in the brief that shell commands
  are unavailable (print mode soft-denies them, and a denied command ends the
  run with no output).
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
  config, and MCP servers.
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
  model. An API lane calls `api_agent <key var or -> <CLI...>`, which runs it
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
