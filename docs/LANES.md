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
- Rule: sparks power Loom. Sparks never touch a live realm.

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
- Each run unlocks that keychain first (it locks again on reboot), passing
  the password on stdin to a shell running as the lane user.

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
