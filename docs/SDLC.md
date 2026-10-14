# Software Development Life Cycle

This document describes the 8-stage Software Development Life Cycle (SDLC) of the quadseven/loom system—how a change moves from ticket to verified—including stage-specific code paths and labels.

## Stages

### brief

The `brief` stage is where a new ticket enters the system. The brain reads the issue and creates a work brief, then transitions it to `loom:briefed` state.

**Code involved:**
- `bus/brain.py` — `brief` function handles brief creation
- `bus/station.py` — polls for `loom:briefed` tickets and hands them to lanes
- `bus/labels.py` — defines `BRIEFED = "loom:briefed"` label

A brief includes the ticket's acceptance criteria, context, and any pre-specified constraints. Once brief is complete, the ticket is labeled `loom:briefed` and becomes eligible for lane assignment.

### run

The `run` stage assigns the brief to a lane (e.g., `spark`, `claude`, `codex`) via `bus/station.py`. The lane repository clones the repo, executes the brief, and attempts to make a commit.

**Code involved:**
- `bus/station.py` — claims `loom:briefed` tickets, runs lane scripts (`lanes/run_lane.sh`)
- `bus/handoff.py` — logs defer/done markers for stage transitions
- `bus/metrics.py` — records run duration and outcomes

If the lane succeeds, the branch is pushed and the ticket moves to `loom:pr`. If it fails, the ticket may be retried or moved to `loom:failed`.

### PR

The `PR` stage is triggered when a lane pushes a branch. The brain detects the new branch and creates a pull request, labeling it `loom:pr`.

**Code involved:**
- `bus/brain.py` — `pr` function creates PRs from lane branches
- `bus/factory.py` — factory controls PR state transitions
- `bus/handoff.py` — logs `merge` stage handoff with revision reference

The PR carries the fix and links back to the original ticket. Once created, the ticket enters the `review` stage.

### review

The `review` stage involves automated and/or human review of the pull request. The brain uses `bus/brain.py` to monitor review threads and apply fixes.

**Code involved:**
- `bus/brain.py` — `review` function monitors PR comments and review threads
- `bus/station.py` — review bot integration (`/grug improve` trigger)
- `bus/handoff.py` — logs `merge` stage handoff before moving to PR

The review stage ends when all review comments are addressed and the PR is approved.

### merge

The `merge` stage is when the approved PR is squash-merged into the default branch. The brain's `merge` function orchestrates this.

**Code involved:**
- `bus/brain.py` — `merge` function executes the merge
- `bus/labels.py` — sets `loom:merged` label
- `bus/handoff.py` — logs `after merge` stage completion

After merge, the ticket transitions to `after merge` stage.

### after merge

The `after merge` stage verifies that the merge succeeded and the branch is protected. The brain ensures the default branch is tested.

**Code involved:**
- `bus/brain.py` — `after_merge` function checks merge status
- `bus/station.py` — triggers CI verification for default branch
- `bus/handoff.py` — logs `deploy` stage handoff

If CI is red, the merge is auto-reverted per the system's deploy health vigilance.

### deploy

The `deploy` stage pushes the change to the target environment (typically via a deploy workflow triggered by the merge).

**Code involved:**
- `bus/brain.py` — `deploy` function monitors deploy workflows
- `bus/station.py` — detects deploy workflow status via `gh` CLI
- `bus/proof.py` — begins collecting runtime verification proof

The deploy stage ends when the change is live in the target environment.

### verify

The `verify` stage confirms the change meets acceptance criteria in production. The brain's `verify` function collects proof (runtime metrics, logs, or docs revision) and posts it on the ticket.

**Code involved:**
- `bus/brain.py` — `verify` function calls `bus/proof.py` to generate proof record
- `bus/proof.py` — validates proof and creates `loom:proof` comment
- `bus/labels.py` — sets `loom:verified` label to close ticket

Once verified, the ticket is closed and marked `loom:verified`. If verification fails, the ticket re-enters the lifecycle.

## Principles

### Live principles

- **No merge queue or global lock**: Each change proceeds independently; the factory's lane order determines priority, not a merge queue.
- **Default branch tested after each merge**: CI verifies the merged state immediately; failures trigger auto-revert.
- **Deploy health vigilance**: Failed deploys and 5xx/SSL endpoints halt progress until resolved.
- **Proof-based closure**: Tickets require a `loom:proof` comment documenting how acceptance criteria were met.

### Planned principles

- **Stage handoff journaling**: See [issue #227](https://github.com/quadseven/loom/issues/227) — full defer/done markers for each stage transition.
- **Automated retry limits**: See [issue #228](https://github.com/quadseven/loom/issues/228) — transient and limit errors trigger automatic lane retries.
- **Lane-specific brief constraints**: See [issue #229](https://github.com/quadseven/loom/issues/229) — lanes may specify execution constraints in brief analysis.
