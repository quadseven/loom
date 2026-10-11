# Muse: what it is, why it failed as a Loom worker, and whether it can be a lane or the brain

Research for #32, as of 2026-10-11. Part of #30.

Method: the installed Mac app bundle, its preferences and local caches were
read on the lane Mac (read only, no secret values). The Muse web client,
which shows the same account as the Mac app, was used to read the Loom side
chat, the scheduled task, its run history and its permissions. A new side
chat asked Muse factual questions about its own runtime; those answers are
marked "self-report". Vendor facts come from Meta's own pages, cited inline.
Loom facts come from this repo's history and the lane Mac's Loom state.

## Short answer

- Muse is Meta's consumer personal agent (Meta Platforms, Inc.), powered by
  the Muse Spark model family. The agent, its shell and its files run on a
  per-user Linux cloud VM hosted by Meta, not on the Mac.
- The Mac app is a client plus a "paired device": the agent can list, read
  and write Mac files and control apps through it, but has no shell on the
  Mac, and scheduled runs skip the Mac entirely.
- The Loom poller fails because it watches a folder on Muse's own VM that
  nothing in Loom ever writes to. Loom's dispatcher has no `muse` lane and
  writes briefs only to the lane Mac's disk.
- There is no API, CLI, webhook or task-accepting URL scheme for the Muse
  agent. Loom cannot drive it headlessly, so it cannot be a lane or the
  brain in its current form.
- The Muse Spark model, by contrast, is reachable today through the
  existing `opencode-go` lane, so "Muse as a lane" is achievable as a model
  choice, not as the agent.

## 1. What Muse is

| Fact | Evidence |
|---|---|
| Vendor: Meta Platforms, Inc. | App signed "Developer ID Application: Meta Platforms, Inc."; bundle id `com.meta.endo` (internal names Endo and Hatch); version 7.0; Sparkle auto-update from a facebook.com appcast. Terms: [Muse Supplemental Terms](https://muse.ai/terms) are an agreement with Meta Platforms, Inc. |
| Product: a consumer personal agent launched 2026-09-08 on iOS, Android, web and WhatsApp; the Mac app followed | [Meta announcement](https://about.fb.com/news/2026/09/introducing-muse-personal-ai-agent/) |
| Model: Muse Spark (1.3 per Meta's safety write-up); the agent cannot switch models itself | [Meta safety write-up](https://research.meta.ai/blog/security-and-safety-for-ai-agents-our-approach-with-muse); the app's preferences hold `hatch_selected_model = auto/auto`; self-report: "I don't get to switch models independently" |
| Where it runs: a dedicated per-user cloud VM, "an isolated linux box with a browser" | [Meta safety write-up](https://research.meta.ai/blog/security-and-safety-for-ai-agents-our-approach-with-muse); the app's preferences hold a `hatch_selected_vm` id; cached chat messages carry a `hatchVMName`; self-report: "My own cloud VM, not your Mac" |
| Plan on this account: free plan with a weekly limit, plus a pool of additional tokens | Settings, General, Usage (observed). An earlier attempt stopped when the weekly free usage ran out (Muse main chat, 2026-10-03). |

The `muse-spark-*` entries in opencode catalogues are Meta's Muse Spark
models served by API, not the Muse agent. Meta sells them directly as
`muse-spark-1.3` and `muse-spark-1.3-contributor` (1M context; standard
$1.25/$4.25 per MTok; contributor $0.10/$0.20 per MTok, and contributor
traffic "is used to improve Meta's products") through an OpenAI-compatible
API ([model page](https://dev.meta.ai/models/muse-spark/),
[build guide](https://dev.meta.ai/resources/blog/build-with-muse-spark/)).
The live catalogues list `muse-spark-1.2-contributor` and
`muse-spark-1.3-contributor` on OpenCode Go, and the same plus `-free`
variants on OpenCode Zen (fetched from the public `/v1/models` endpoints,
2026-10-11).

## 2. How Muse executes work

Runtime on the VM, per Meta's safety write-up:

- The harness daemon, workspace and every tool run in a `systemd-nspawn`
  container with its own Debian root filesystem.
- A separate host-side agent, Sentinel, is "the sole permission authority"
  for connector actions and all network egress. It returns allow, deny or
  ask for each request. On ask, "execution stops" and an approval dialog
  appears in the client UI, not in the chat.
- The agent sees only surrogate tokens; real credentials are swapped in at
  the network boundary.
- A Chromium browser sub-agent sees an accessibility tree, not the DOM.

Observed on this account and in the app:

| Capability | Observed |
|---|---|
| Shell, git | On the VM. Self-report: "Yes to git". Loom code it wrote earlier loaded a GitHub helper from the VM's workspace directory (fixed in #13). |
| GitHub | A GitHub connector (read: Allow, write: Ask, from the scheduled task's permission page) and, per self-report, a stored personal access token used through a helper script so writes skip the per-call approval. |
| Mac files | The Mac is a paired device (Settings, Devices). Self-report: device file commands can list, read, write and edit Mac files "only while the Mac app is running and reachable", with "no shell on the Mac side". The Mac app requests Accessibility, Screen Recording, Apple Events and folder access, and keeps a computer-control lock under its Application Support directory. |
| Browser on the Mac | The app installs a Chrome extension, "Muse Browser Node", that pairs the local browser as a controllable node (all-URL host permission, debugger, tabs, history). |
| Network to private services | None by default. Reaching the private dashboard needed a dedicated tailnet tag plus an ACL grant (infra#5281), and then a proxy. |
| Schedules | Muse-managed cron tasks, shown under "Daily" with run history. The Loom poller runs every 15 minutes. |
| How it is driven | Muse app chats (Mac, iOS, Android, web), WhatsApp, paired devices, and its own scheduled tasks. |
| CLI, API, webhook, URL scheme | None found. The Mac app registers `hatch://` and `endo-window://`; self-report says `hatch://` is in-app navigation only (for example `hatch://search`). No public agent API appears on Meta's developer site, which offers only the Muse Spark model API and the separate Muse Code terminal agent. |

## 3. Why the earlier attempts failed

Two phases failed for different reasons.

### Phase 1: Muse as the Loom foreman (until 2026-10-11 afternoon)

The original design made Muse the foreman (`docs/ARCHITECTURE.md` still
says "Muse (the foreman) runs everything"). Muse ran the dispatcher on its
VM and delivered briefs to the Mac by pushing them to a public `briefs`
branch of this repo; Mac pollers fetched them over HTTP (commit `fc1f0cb`).
Problems recorded in the repo and in Muse's own chats:

- It could not see the Mac. Its fix for the Mac pollers (#11) edited
  `lanes/*/poller.sh`, which were not the scripts the Mac actually ran, and
  was closed as superseded by #12.
- Its dispatcher and state publisher were pinned to the VM's home directory
  and wrote to a public branch (#13, #21). #12 then removed GitHub brief
  delivery altogether.
- It had no network path to the private dashboard until infra#5281.
- Background runs cannot complete approval-gated GitHub writes. Muse
  reported twice that "writes are blocked from the background side" and
  asked the operator to post or edit by hand.
- It ran out of weekly free usage once mid-project.
- It reported work as done before it was (for example a fix still failing
  CI), which the operator flagged as not following through.

### Phase 2: Muse as a worker lane (from 2026-10-11, 16:50 local)

After being demoted, Muse wrote itself a lane README and a scheduled task,
"Muse Loom assignment poller", every 15 minutes, which "checks
`~/loom/briefs/muse/`". Evidence that it can never fire:

1. The path is on Muse's VM. Self-report: "It checks ~/loom/briefs/muse/
   on my VM, not your Mac. It has never found an assignment."
2. Nothing writes to that VM folder. Since #12 the dispatcher writes briefs
   only to the lane Mac's own disk, and no channel carries them to the VM.
3. Loom has no `muse` lane. `DIFFICULTY_LANE` in `dispatch/dispatcher.py`
   lists only anthropic, claude, codex, openrouter and opencode-go, and the
   lane Mac's briefs directory has no `muse` folder.
4. Scheduled runs skip the Mac anyway. Muse's own log line for a run:
   "Mac briefs inbox skipped: device access unavailable in this scheduled
   run (owner-authorized access required)."
5. Even with a brief, a scheduled run could not open the PR through the
   connector: GitHub write is "Ask", and an ask with nobody present "stays
   pending in the queue and the write doesn't happen" (self-report,
   consistent with Meta's "execution stops" description).
6. The task reports "Succeeded" every 15 minutes (run history observed at
   17:14 through 18:14), so an empty inbox looks like health.

## 4. Can Loom drive it headlessly?

### (a) Muse as a lane now

As the agent: no. There is no programmatic way to hand Muse a ticket or
read its result; the only inbound paths are app chats, WhatsApp and Muse's
own schedules. Driving the app or web UI with browser or screen automation
would be fragile and runs into the terms below. A file inbox cannot work
either: scheduled runs do not read the Mac, and the VM's folders are not
reachable from Loom.

As a model: yes, with no code change. The `opencode-go` lane accepts any
`opencode-go/...` model in a brief's `Model:` line (`lanes/run_lane.sh`),
and OpenCode Go serves `muse-spark-1.3-contributor`. A brief with
`Model: opencode-go/muse-spark-1.3-contributor` should run Muse Spark under
the existing sandbox, keyless agent, and runner-opened PR. This has not been
run yet. Contributor-tier traffic trains Meta's models, so it suits public
repos only; private repos need the standard tier from Meta's own API, which
would be a new lane with its own key.

### (b) Muse as the brain later

Not in its current form. A brain must run unattended, read Loom state on
the lane Mac, write briefs, and act on GitHub. Muse's scheduled runs cannot
reach the Mac, cannot complete approval-gated writes, and are capped by a
weekly plan limit. Its terms also expect a human in the loop:

- "Oversight, Review and Correction": users must keep "meaningful human
  oversight" and avoid directing Muse "to engage highly autonomously
  without appropriate oversight and approvals in place"
  ([terms](https://muse.ai/terms)).
- Restrictions forbid chaining automated actions in a way designed to
  obscure their automated nature, and the disputes section treats engaging
  with the service "in unauthorized ways (for example, automated ways)" as
  a claim outside arbitration ([terms](https://muse.ai/terms)).
- Meta "may access and retain logs of actions taken by Muse" and may
  suspend actions at its discretion ([terms](https://muse.ai/terms)).

Things that would change this verdict: a published agent API or webhook,
scheduled runs that can use the paired Mac, or pre-authorized grants that
let unattended runs write. Meta's safety write-up already lists
task-scoped, time-bounded and perpetual grant types, but it does not say
how scheduled runs use them. A brain built on the Muse Spark model through
an API (inside an official harness such as opencode) is a separate option
and would be judged like any other model source in
`research/inference-sources.md`.

## Recommendation

- Treat the Muse agent as a chat assistant, not a Loom component. The
  operator may pause the "Muse Loom assignment poller", since it can never
  find work. This research left all chats, settings and schedules as found.
- Try Muse Spark as a model in the `opencode-go` lane on one public-repo
  ticket, and record the result.
- Update `docs/ARCHITECTURE.md`, which still names Muse as foreman and
  shipper.
