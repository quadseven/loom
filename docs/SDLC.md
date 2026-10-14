# Software Development Life Cycle

This document describes the 8-stage Software Development Life Cycle (SDLC) of the quadseven/loom system - how a change moves from ticket to verified - including stage-specific code paths and labels.

## Stages

### brief

The `brief` stage is where a new ticket enters the system. The brain reads the issue and creates a work brief, then transitions it to `loom:briefed` state.

**Code involved:**
- `bus/brain.py` - `brief` function handles brief creation
- `bus/station.py` - polls for `loom:briefed` tickets and hands them to lanes
- `bus/labels.py` - defines the `loom:briefed` label

A brief includes the ticket's acceptance criteria, context, and any pre-specified constraints. Once brief is complete, the ticket is labeled `loom:briefed` and becomes eligible for lane assignment.

### run

The `run` stage assigns the brief to a lane (e.g., `spark`, `claude`, `codex`) via `bus/station.py`. The lane repository clones the repo, executes the brief, and attempts to make a commit.

**Code involved:**
- `bus/station.py` - claims `loom:briefed` tickets, runs lane scripts (`lanes/run_lane.sh`)
- `bus/handoff.py` - logs defer/done markers for stage transitions
- `bus/metrics.py` - records run duration and outcomes

If the lane succeeds, the branch is pushed and the ticket moves to `loom:pr`. If it fails, the ticket may be retried or moved to `loom:failed`.

### PR

The `PR` stage is triggered when a lane pushes a branch. The brain detects the new branch and creates a pull request, labeling it `loom:pr`.

**Code involved:**
- `bus/brain.py` - `pr` function creates PRs from lane branches
- `bus/factory.py` - factory controls PR state transitions
- `bus/handoff.py` - logs `merge` stage handoff with revision reference

The PR carries the fix and links back to the original ticket. Once created, the ticket enters the `review` stage.

### review

The `review` stage involves automated and/or human review of the pull request. The brain uses `bus/brain.py` to monitor review threads and apply fixes.

**Code involved:**
- `bus/brain.py` - `review` function monitors PR comments and review threads
- `bus/station.py` - review bot integration (`/grug improve` trigger)
- `bus/handoff.py` - logs `merge` stage handoff before moving to PR

The review stage ends when all review comments are addressed and the PR is approved.

### merge

The `merge` stage is when the approved PR is squash-merged into the default branch. The brain's `merge` function orchestrates this.

**Code involved:**
- `bus/brain.py` - `merge` function executes the merge
- `bus/labels.py` - sets `loom:merged` label
- `bus/handoff.py` - logs `after merge` stage completion

After merge, the ticket transitions to `after merge` stage.

### after merge

The `after merge` stage verifies that the merge succeeded and the branch is protected. The brain ensures the default branch is tested.

**Code involved:**
- `bus/brain.py` - `after_merge` function checks merge status
- `bus/station.py` - triggers CI verification for default branch
- `bus/handoff.py` - logs `deploy` stage handoff

If CI is red, the merge is auto-reverted per the system's deploy health vigilance.

### deploy

The `deploy` stage pushes the change to the target environment (typically via a deploy workflow triggered by the merge).

**Code involved:**
- `bus/brain.py` - `deploy` function monitors deploy workflows
- `bus/station.py` - detects deploy workflow status via `gh` CLI
- `bus/proof.py` - begins collecting runtime verification proof

The deploy stage ends when the change is live in the target environment.

### verify

The `verify` stage confirms the change meets acceptance criteria in production. The brain's `verify` function collects proof (runtime metrics, logs, or docs revision) and posts it on the ticket.

**Code involved:**
- `bus/brain.py` - `verify` function calls `bus/proof.py` to generate proof record
- `bus/proof.py` - checks the proof record and posts the `<!-- loom:proof v1 -->` comment
- `bus/labels.py` - sets `loom:verified` label to close ticket

Once verified, the ticket is closed and marked `loom:verified`. If verification fails, the ticket re-enters the lifecycle.

## Principles

A live principle names the module that carries it. A planned one is not in the tree
yet and cites the open ticket that tracks it.

### Live principles

- **No merge queue or global lock**: each change proceeds independently; `bus/factory.py` merges a lane pull request on its own once it is green and its review-bot threads are answered, so lane order rather than a queue sets priority.
- **One change class per lane pull request**, written into the Intent section the runner opens: `bus/change_class.py` classifies the changed files as sensitive, logic, tests or docs (#227).
- **Merge rules follow the change class**: `bus/factory.py` lands a docs or tests pull request on checks and a review pass alone, and marks a sensitive one `needs-brain` for the brain instead (#228).
- **The default branch is judged after every merge**: CI runs on it, and a red it did not see coming is bisected back to the merge that caused it; `bus/revert.py` files that revert as a ticket, which lands before other work (#229).
- **A goal home's `Trunk:` line names the branch its briefs and pull requests target**: `bus/brain.py` reads the line, targets it, and warns when work is headed somewhere else (#230).
- **One watcher per component log**: `bus/watch.py` reduces the lines a log gained into signatures and files one ticket per new repeated error (#231).
- **A leak-scan failure in a lane pull request's text is repaired in the text**: `bus/factory.py` rewrites each flagged line of the title and body instead of sending the change back as code (#232).
- **Stage handoff journaling**: `bus/journal.py` writes the goal journal and `bus/handoff.py` reads its defer and done markers, so a peer finishes stage work a stopped brain left behind (#103).
- **A failed lane run retries on another lane, and a lane at its provider limit stops taking work**: `bus/station.py` counts the retry lines on the ticket, caps them at `LOOM_RETRY_MAX`, and moves the ticket to the next lane in the brain's order (#112).
- **Deploy health vigilance**: a failed deploy or a 5xx endpoint stops the line; `dispatch/ship.py` is the one shipper and honors the deploy caps and roll windows on the goal home, and `dispatch/verify.py` closes a shipped ticket only on live proof.
- **Proof-based closure**: a ticket closes on a proof record checked against its own acceptance criteria; `bus/proof.py` checks the record and `bus/brain.py` posts the `<!-- loom:proof v1 -->` comment before closing.

### Planned principles

- **A ticket nobody claimed moves within its tier and never climbs the ladder** (#259).
- **Escalation counts only work failures, and a failed pull request create is transient** (#261).
- **The station ships a Loom ticket when it deploys the commit that carries it** (#262).
- **The status and the resume list the shipped tickets due for verification, and the brain spends a share of each hour on them** (#263).
