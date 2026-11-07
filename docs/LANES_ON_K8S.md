# Every lane subscription on cluster runners

Design for #2780, written 2026-11-07. It extends `docs/LANE_RUNNERS_PLAN.md`,
which kept the sign-in lanes on the Mac, and replaces that decision: the
operator decided on 2026-11-07 that lanes for every subscription run on the
cluster. Nothing here is built. Each slice is a child of #2780.

## Why

The lane host has 12 cores and a full swap. Each lane agent runs the repo's
whole test suite (wow-overseer starts 12 to 15 test processes), so the host
holds about 3 to 4 heavy tickets at once. On 2026-11-07, 15 local Luna lanes
put its load at 130 and claims fell to about 7 an hour with about 220 tickets
queued. More lanes on that host do not mean more work done.

## Where cluster lanes stand

Only two slots run on the pool. `free-gemma` succeeded 7 times on 2026-11-02
and failed 37 times since. `free-openrouter` failed all 56 of its runs from
2026-11-02 to 2026-11-04, each with `the agent did not run (limit)`: the slot
held on a provider limit before the agent started, and a pod has no memory of
the hold, so each dispatch only costs a pod start. #2781 finds the cause from
the sealed reports; #2782 does the same for `free-gemma`. Nothing else moves
until one slot runs end to end.

## Credential path per subscription

One rule for all of them: a credential reaches a pod only as a file the `ssm`
step of `lanes/k8s/lane_job.py` copies under HOME (mode 600) through the OIDC
role the lane workflow assumes on main. It is never a workflow input, a
repository secret, an environment value in a step, a log line or a commit. The
role names each parameter exactly (no prefix); the bubblewrap sandbox hides
the files from the agent, but an exported key is still visible in the agent's
own environment. The gateway path is the only one with no credential in the
agent.

| Subscription | Path | Done once by the operator | Risk |
|---|---|---|---|
| Codex (Luna) | Broker-owned lane login; pods read a copy with no refresh token (below) | One device sign-in on the broker | Highest: a rotating session. The design below contains it |
| Claude | A long-lived OAuth token for Claude Code, exported to the CLI only; it does not rotate on use, so many pods share it | Generate the token once and store it in SSM | Visible in the agent's environment; all slots share one subscription limit |
| Anthropic (API credits) | Exported key, as the Mac lane does today | None (the key is in SSM); the role needs the parameter | Visible in the agent's environment; spend is recorded per run |
| opencode-go | Exported key; a gateway route later | None | Same; four slots share one monthly limit, held by each report's heartbeat |
| OpenRouter | Gateway route (the slot exists) | None | None in the agent; the free daily count is small (see #2781) |
| Z.ai | Gateway route | None | None in the agent |
| Workers AI | Gateway route (to build); until then the token and the account id copied by the `ssm` step | None | Interim exported key; the account id is not readable by the role today |
| Zen | No key | None | None |
| agy (Gemini) | Stays on the Mac until #2792 shows its file store works on Linux and does not rotate | None yet | It signs in as a separate macOS user through a keychain; capped at 2 light slots if it stays |
| Copilot | Exported token with Copilot access only | Create the scoped token once (none is set for the Mac lane today) | Visible in the agent's environment; CLI headless behaviour is unproven (#2791) |

What can be verified without the operator: the OIDC role, the `ssm` copy, the
egress proof, every API and gateway lane, and the failing-run reports through
the station's own tooling. What needs the operator once: the Codex device
sign-in on the broker, the Claude token, and the Copilot token. The operator
never pastes a value into a ticket; each is stored with `ssm-put` and read by
parameter name only.

### Codex: one writer, read-only copies

A ChatGPT sign-in file holds a refresh token that rotates when used. If many
pods refresh from one file, the first refresh invalidates the rest and can
end the session on the Mac too. Options:

1. Per-pod sessions. Each pod needs its own login, which needs a person at a
   browser, and one subscription seat limits parallel logins. Rejected.
2. A shared file in every pod. Races on rotation. Rejected.
3. Single writer, read-only copies. Recommended. A broker owns a dedicated
   lane login (a separate session from the operator's own Codex login, so
   the operator's Mac login is never refreshed by the broker). It is the
   only process that refreshes. It publishes a copy to one lane-scoped SSM
   parameter with the refresh token removed. A pod copies that file into its
   Codex home for one job. A pod that cannot refresh cannot rotate anything,
   and a stolen copy expires on its own.
4. A credential-adding proxy outside the sandbox, so the agent sees a URL and
   no token (the "design only" section of `LANE_RUNNERS_PLAN.md`). The
   strongest, and the follow-up once option 3 works.

Option 3 depends on three facts about the installed CLI that are unproven
until the spike in #2784 runs: it works with an access token and no refresh
token, how long the access token lasts (this sets the broker's schedule), and
it does not write the file back. If any fails, the fallback is option 4. A
copy older than the broker's maximum age makes the slot report unavailable
instead of failing runs. The broker's identity may write one parameter and
nothing else; the lane role stays read-only.

## Capacity plan

Measured read-only on 2026-11-07 (allocatable against requests). The pool
today is capped at 4 pods of 500m CPU and 2Gi, pinned to one build node.

| Node class | CPU / memory | Requested | Free by requests | Use |
|---|---|---|---|---|
| Build node (pool today) | 12 / 34.6Gi | 69% / 73% | 3.7 CPU, 9.4Gi | 2 heavy pods |
| Large x86 storage worker | 16 / 50.4Gi | 45% / 19% | 8.7 CPU, 40Gi | 5 heavy pods; it also holds the registry and stateful services, so lower priority and disk re-measured |
| Second small x86 node | 8 / 26.9Gi | 41% / 41% | 4.7 CPU, 15.7Gi | 2 heavy pods; it holds a game realm |
| Smallest x86 node, arm64 Macs, OCI nodes, GPU worker | small, arm64, or NotReady | | | not used |

Heavy pod: requests 1500m CPU, 3Gi memory, 8Gi ephemeral storage; limits 4
CPU, 6Gi, 15Gi; work volume 12Gi. These are starting values: #2783 records
each run's peak memory and CPU in its report, and the infrastructure ticket
sets the requests from a measured wow-overseer run. Pool: `minRunners: 0`,
`maxRunners: 10`, spread over the three nodes. Expected about 9 heavy pods
plus the 3 to 4 the Mac keeps for private repos and agy, so about 3 times
today's heavy capacity. The station dispatches only what the pool can start
(`LOOM_RUNNER_POOL_MAX`), so a held pod does not turn into a cancelled run
after the 30 minute queue limit.

Concurrency is per slot, so slots are the parallelism: codex-2 to codex-8
first, codex-9 to codex-16 after the sizing ticket. New slots deploy by merge
through `TRACKED_SLOTS`, which needs no plist change.

## Constraints that stay

- Public repos only: a run's inputs and logs live in this repository's
  Actions. 8 of the 19 registered repos are private; their tickets stay on
  the Mac until #2793 decides a path.
- Network egress is default-deny with TCP 443 to public addresses, which
  covers every provider; no new network rule is needed.
- A green Actions run says only that an agent started; the proof of a lane
  is the pull request or diff it produced.
- A Mac lane goes only after its slot produced a pull request someone read
  (#2794).

## Slices

1. #2781 free-openrouter end to end, with #2782 free-gemma.
2. #2783 pool sizing and dispatch bound, with the infrastructure pool change.
3. #2784 Codex broker, then #2785 the Luna slots.
4. #2786 claude, #2787 anthropic, #2788 opencode-go, #2789 zai and zen,
   #2790 Workers AI, #2791 copilot.
5. #2792 agy and #2793 private repos decisions, then #2794 retiring Mac lanes.

The paired infrastructure work (pool placement and sizing, runner image with
the three CLIs, the role's exact parameter list, the broker) is tracked in the
private infrastructure repository under the same epic.
