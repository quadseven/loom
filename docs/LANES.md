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

## API lanes: openrouter, opencode-go, anthropic (and zen, zai, agy below)

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

## agy/ - the Google plan

- Tool: the Antigravity CLI, called directly per brief:
  `agy -p <brief> --model <id> --dangerously-skip-permissions`. Never wrapped
  inside another agent (agy's terms allow direct scripting only). agy's own
  approval prompts are off because the Seatbelt profile is the boundary.
- Models: agy's catalogue (`gemini-*`, `claude-*`, `gpt-oss-*`; default
  `gemini-3.8-flash-high`). Anything else is refused.
- Sign-in: agy keeps its Google login in the macOS keychain, not in an API
  key, and looks for it in `$HOME/Library/Keychains/login.keychain-db`. The
  agent cannot be given the operator's login keychain: it also holds `gh`'s
  token, which any process able to run `security` can read. So agy signs in
  once into a keychain of its own, `agy.keychain-db` in
  `LOOM_AGY_LOGIN_HOME` (default `~/loom/agy-login`). Each run the runner
  unlocks it by path (empty password; the file is the secret) and links it
  into the fresh scratch home as `Library/Keychains/login.keychain-db`. The
  sandbox lets the agent reach the keychain server and read that one file,
  not write it, and still denies the rest of the real HOME, including the
  operator's login keychain. The agent can read agy's own token, as other
  API-lane agents can read their key. A lane with no sign-in keychain
  reports itself unavailable.
- One-time sign-in, by the operator at the Mac:

  ```sh
  D="$HOME/loom/agy-login"; mkdir -p "$D/home/Library/Keychains"
  security list-keychains -d user        # note your search list
  security create-keychain -p '' "$D/agy.keychain-db"
  security list-keychains -d user -s ...  # restore the list noted above
  security set-keychain-settings "$D/agy.keychain-db"   # no auto-lock
  ln -s "$D/agy.keychain-db" "$D/home/Library/Keychains/login.keychain-db"
  HOME="$D/home" agy                     # sign in; the token lands in agy.keychain-db
  ```

  The file must not be named `login.keychain-db` itself: macOS will not
  unlock a file of that name with a password given on the command line.
- Transcripts: agy's per-conversation SQLite files
  (`.gemini/antigravity-cli/conversations/`) are archived like the other
  API lanes'. The transcript viewer does not render them yet.

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

## Adding a lane

New lane = directory under `lanes/` with: a poller or runner script, a
heartbeat writer, and a section appended here. No lane may merge, ship, or
hold another lane's credentials.
