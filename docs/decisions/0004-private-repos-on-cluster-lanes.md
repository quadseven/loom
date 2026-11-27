# 0004: Private repos on cluster lanes via a private lane-workflow repository

Status: accepted, 2026-11-27. Refs #2780, #2793.

## Context

Runner slots take public repos only, because a run's inputs and logs live in this public repository's Actions (quadseven/loom). 8 of the 19 registered repos are private; their tickets stay on the Mac and keep loading it. The LANE_RUNNERS_PLAN.md recorded this as "Private repos stay with Mac lanes until the workflow moves to a private repository."

Two paths were on the table:
1. A private lane-workflow repository with its own scale set and OIDC trust.
2. Keeping private repos on the Mac with a cap.

## Decision

We choose path 1: a private lane-workflow repository.

### Repository

`quadseven/loom-lanes-private` — a private repository that holds a copy of the `lane.yml` workflow and any private-repo-specific runner configuration. It owns its own Actions runner scale set (`arc-loom-lanes-private`) and its own OIDC role.

### OIDC subject anchor

The lane role in the private repository trusts the OIDC subject of its own `main` branch with no environment:

```
repo:quadseven/loom-lanes-private:ref:refs/heads/main
```

This matches the pattern used by the public `lane.yml` (quadseven/loom#1824), where the role trusts the workflow file on main only and the job sets no environment.

### Log exposure

Run inputs (the brief, settings, report keys) and run logs live in the private repository's Actions. They are not visible in the public quadseven/loom repository. The public station still dispatches the run (via `workflow_dispatch` across repositories), but the run executes entirely in the private repository's runner pool. The sealed report artifact is uploaded to the private repository's Actions and read back by the station; the public repository never sees the log content.

The public station's dispatch ledger records only the run id and the repository name (`quadseven/loom-lanes-private`), not the brief or any secret. The brief is passed as a workflow_dispatch input, which GitHub encrypts in transit and stores in the private repository's Actions runtime.

### Egress consequences

The private runner pool uses the same egress model as the public pool: default-deny with TCP 443 to public provider endpoints. The egress prove step (`lane_job.py egress`) runs before any key is read, using the same `LOOM_EGRESS_ALLOW_PROBE` and `LOOM_EGRESS_DENY_PROBES` variables (now repository variables in the private repo). No new network rules are needed.

The private repository's lane role assumes a dedicated AWS role (`LOOM_LANE_ROLE_ARN` in the private repo) that grants read access to the same SSM parameters (API keys, deny-list copy) plus any private-repo-specific parameters. The role's trust policy anchors to the private repository's OIDC subject above.

### Mac cap (fallback)

Until the private repository is provisioned and the first private-repo lane produces a pull request that a person has read, private repos stay on the Mac with a cap of 4 concurrent local lanes (the current `LOOM_CHECK_SLOTS` default). This cap is not a new limit; it is the existing host check-slot limit. The station continues to route private-repo tickets only to local lanes (`free-gemma`, `free-ollama`, `anthropic`, `claude`, `codex`, `opencode-go`, `copilot`, `agy`) while the private path is not ready.

## Consequences

- Private repos can use the full lane catalog (including `openrouter`, `workersai`, `zen`, `zai`, `free-*` gateway lanes) on cluster runners, once the private repository's workflow mirrors the public one.
- The public station dispatches to the private repository via `workflow_dispatch` using its GitHub App (which must be installed on the private repository). The dispatch input size limit (65,535 characters) still applies.
- The private repository's runner scale set is independent; its `minRunners: 0`, `maxRunners: 10` (matching the public pool plan) and can be sized separately.
- The Mac lanes for private repos retire only after the private path produces a verified pull request per lane (per LANE_RUNNERS_PLAN.md "Retiring a Mac agent").
- The operator must provision the private repository, install the station's GitHub App on it, create the OIDC role with the subject anchor above, and mirror the `lane.yml` workflow and runner image.

## Rejected

**Keeping private repos on the Mac with a cap.** This was the status quo. It limits private-repo throughput to the Mac's 3-4 heavy slots, blocks the retirement of Mac lanes (loom#2794), and does not scale with the cluster pool. The cap is a ceiling, not a path forward.

## Build tickets under epic #2780

The following build tickets must be filed (by the operator or a brain with network access) to implement this decision:

1. **Provision `quadseven/loom-lanes-private`** — create the private repository, push a copy of `.github/workflows/lane.yml`, configure repository variables (`LOOM_LANE_DISPATCHERS`, `LOOM_LANE_ROLE_ARN`, `LOOM_LANE_AWS_REGION`, `LOOM_LANE_EGRESS_ALLOW_PROBE`, `LOOM_RUNNER_GATEWAY_URL`, `LOOM_RUNNER_SLOTS`) and secret (`LOOM_LANE_EGRESS_DENY_PROBES`).

2. **Create the private lane OIDC role** — an AWS IAM role with trust policy for `repo:quadseven/loom-lanes-private:ref:refs/heads/main`, granting `ssm:GetParameter` on the same parameters as the public lane role plus any private-repo-specific parameters. Store the role ARN in the private repo's `LOOM_LANE_ROLE_ARN` variable.

3. **Mirror the runner image** — ensure the private runner scale set (`arc-loom-lanes-private`) uses the same runner image as the public pool (with `bwrap`, the three CLIs, and the SSM directory binary). Tracked in the private infrastructure repository.

4. **Install the station's GitHub App on the private repo** — so the public station can dispatch `workflow_dispatch` events to it. The station's App ID and installation ID for the private repo become new settings in the station's environment.

5. **Extend the station's dispatch to target the private repo for private-repo briefs** — in `bus/runners.py`, when `self.public(repo)` is false and the private repo is configured, dispatch to `quadseven/loom-lanes-private` instead of refusing. The ledger key becomes `private/<slot>/<ticket>`.

6. **Add the private repo's scale set to the station's runner pool config** — `LOOM_RUNNER_SLOTS` gains a `private-` prefix for slots backed by the private pool (e.g., `private-opencode-go-1`), or a separate `LOOM_PRIVATE_RUNNER_SLOTS` variable.

7. **Verify end-to-end with a private-repo ticket** — run a private-repo ticket through the private pool, confirm the sealed report is read back, the PR opens on the private repo, and the station moves the ticket to `loom:shipped`.

8. **Retire Mac lanes for private repos** — once each private-capable lane has a verified pull request from the private pool, remove its Mac launchd agent (per LANE_RUNNERS_PLAN.md "Retiring a Mac agent").