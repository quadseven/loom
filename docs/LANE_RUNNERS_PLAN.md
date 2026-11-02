# Moving lanes to ephemeral runners

Plan for quadseven/loom#2413, written 2026-11-01. It names each launchd agent,
its target, its credential path and what cannot move yet. Live state was read
that day from the station's LaunchAgents folder and the cluster.

## Where the runs go

Every moved lane uses the path `docs/LANES.md` describes in "Lanes on cluster
runners": the `lane` workflow, one job on an `arc-loom-lanes` pod, thrown away
when the job ends.

- Pool: `arc-loom-lanes`, 0 to 4 runners, one 2Gi request each, pinned to one
  build node. Measured 2026-11-01: that node has 71% of its memory requested,
  which leaves room for the four, and the pool stays off the nodes that hold
  the live game services. A fifth runner needs a capacity decision in the
  private infrastructure repo first.
- Sandbox: bubblewrap (`LOOM_SANDBOX=bwrap`), proved by the job's egress step
  before any key is read.
- Leak scan: the deny-list's copy under `LOOM_SSM_DIR`, read through
  `lanes/k8s/ssm-dir-bin/aws`. The pod holds no AWS key.
- Proof of a run: the pull request or diff it produced. A green Actions run
  says only that an agent started (loom#2418).

## Credential paths

- Gateway: the spark-gateway holds the provider key and serves one pinned
  route. The agent gets a URL and no key. Free routes refuse every paid model
  at the gateway. This is the only path that keeps every credential out of
  the agent.
- Sign-in or key under a CLI: anthropic's CLI takes an exported key from SSM
  and copilot an exported token. Both stay on the Mac with the sign-in lanes
  by operator decision.
- Exported key: the job copies the `LOOM_<LANE>_KEY_SSM` value under HOME
  through the OIDC role `loom-lane`, and `api_agent` exports it to the agent.
  bwrap hides the file, but the agent can read its own environment. This path
  does not meet "no credential visible to the agent".
- CLI sign-in: a CLI's own session under a Mac home directory. No runner has
  one.

## Every launchd agent

| Agent | Target | Credential path now | Credential path on a runner | State |
|---|---|---|---|---|
| free-gemma | runner | gateway | gateway | Moved before this plan |
| openrouter | runner, as slot free-openrouter | exported key | gateway | Slice 1 |
| free-kilo | runner | gateway | gateway | Next after slice 1 |
| free-nvidia | runner | gateway | gateway | Next after slice 1 |
| spark, spark-2, spark-3, spark-4 | runner | gateway | gateway | Parked: the box is out for repair |
| opencode-go, -2, -3, -4 | runner | exported key | exported key, or a gateway route | Slice 2, decision below |
| zen | runner | no key | no key | Later |
| zai | runner | exported key | gateway has a `zai` provider | Later |
| workersai | runner | exported key | no gateway provider yet | Later, needs a gateway route |
| codex, -2 to -8 | stays on the Mac | CLI sign-in | none yet | Design only |
| claude | stays on the Mac | CLI sign-in | none yet | Design only |
| anthropic, -2 to -8 | stays on the Mac | exported key under the CLI | none yet | Design only |
| copilot | stays on the Mac | exported token under the CLI | none yet | Design only |
| agy | stays on the Mac | a separate lane user's sign-in | none | Cannot move |
| station | stays on the Mac | App keys, launchd | none | Not a lane |
| brain | stays on the Mac | App key, launchd | none | Not a lane |
| viewer | stays on the Mac | none | none | Not a lane |

The table covers all 36 plists. Of those, 28 were loaded on 2026-11-01:
codex-4 to codex-8 and spark-2 to spark-4 are installed but not loaded.

## What cannot move yet

- Subscription CLI lanes (codex, claude, anthropic, copilot). codex and claude
  sign in from a Mac home directory. anthropic and copilot take an exported
  key, which a run on a shared pool could read. The design below is not built.
- agy. It signs in as a separate user and needs that user's keychain. No
  design moves it.
- Any private repo. A run's inputs and logs live in this public repository's
  Actions, so runner slots take public repos only. Private repos stay with
  Mac lanes until the workflow moves to a private repository.
- A repo whose check needs a tool the image lacks. The record now names the
  tool (`missing make`), and the runner image is where it is added.
- Exported-key lanes, for "no credential visible to the agent", until each has
  a gateway route.

## Design only: subscription CLI lanes

Nothing here is built. A later slice needs the operator's decision on these
points.

1. A sign-in broker on the operator's hardware keeps each CLI's session. The
   runner holds no session.
2. The agent's CLI talks to a local proxy outside the sandbox, as it would talk
   to the gateway. The proxy adds the credential. The agent sees only a URL.
3. Each session is one account, so a slot's concurrency stays what the account
   allows, and limits stay per account.
4. A CLI that refuses a proxied endpoint stays on the Mac.

Fallback if the proxy is refused: a pool of one long-lived Mac VM per
subscription. That keeps a home directory, so it gives up the ephemeral goal.

## Capacity of the OpenRouter route

The account has no purchased credit, so the gateway counts 50 requests a day
for OpenRouter and keeps 20% for review, leaving about 40 for lanes. The Mac
`openrouter` lane spends the same key. One brief may use most of a day's count,
so this slot suits small tickets and waits out each reset. Removing the Mac
lane gives the runner slot the whole allowance.

## Slices

1. openrouter. The gateway holds the key and serves only `:free` models, so
   slot `free-openrouter` in `bus/runners.py` TRACKED_SLOTS runs beside the Mac
   agent. It deploys by merge. The station's plist needs no change, because a
   plist changes only by a reinstall that replaces every lane.
2. opencode-go. Only after slice 1 has done real work, and in step with the
   work changing Go model selection. Its key is exported today, so the slice
   first decides between a gateway route and accepting an exported key on this
   lane.
3. free-kilo and free-nvidia, then zai and workersai through gateway routes.
4. The spark slots, once the box is back.

## Retiring a Mac agent

An agent goes only after its runner slot has produced a real pull request or
diff that a person or reviewer has read.

    launchctl bootout gui/$(id -u)/com.loom.openrouter && rm ~/Library/LaunchAgents/com.loom.openrouter.plist

Then remove `openrouter` from the installer's default lane list in a merged
change, so a later install does not bring the agent back. Until then the Mac
lane keeps running.
