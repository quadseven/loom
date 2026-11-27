# 0004: Keep private repo lanes on the Mac

Status: accepted, 2026-11-27. Refs #2780, #2793.

## Context

Cluster runner slots take public target repositories only because their run
inputs and Actions logs live in the workflow repository. Private-repo tickets
therefore continue on local Mac lanes. The alternatives were a separate private
workflow repository and runner pool, or keeping private-repo work local with a
capacity cap.

## Decision

Keep private-repo tickets on local Mac lanes. Cap private-repo work at three
concurrent heavy lane runs across the Mac lanes. This records a routing policy;
it does not add a new cluster workflow or runner pool.

### Repository and OIDC trust

The workflow repository remains the public `quadseven/loom`. The existing
public cluster lane role keeps its main-branch subject anchor:

```
repo:quadseven/loom:ref:refs/heads/main
```

The role also restricts trust to the repository identity and
`.github/workflows/lane.yml` on `main`, as described in `docs/LANES.md` under
"Lanes on cluster runners". The subject alone does not identify the workflow
file. Private-repo Mac runs do not request an OIDC token or assume the cluster
lane role; their existing local authentication remains in place.

### Log exposure

The brief, transcripts, validation output, and lane logs for private-repo runs
stay in local Loom state on the Mac. They are not sent to the public workflow
repository's Actions logs. The resulting branch and pull request remain in the
private target repository through its existing local runner path.

### Egress consequences

Private-repo runs keep the sandbox and host network behavior of their selected
Mac lane. They do not receive the cluster runner pod's default-deny egress
policy or its egress proof. No new cluster network rule or OIDC role is needed
for this path.

### Mac cap

The operating cap is three simultaneous heavy private-repo lane runs across
the Mac lanes. The existing `LOOM_CHECK_SLOTS` default is also three, but it
limits concurrent validation steps, not active lane builds. The station has
no separate private-run counter today, so this is a routing policy rather
than an automated hard limit. A separate implementation ticket is needed if
the cap must be enforced in station code.

## Consequences

- Private-repo tickets continue to compete for Mac capacity and cannot use the
  cluster-only lanes.
- The Mac keeps existing local authentication and host egress behavior for
  these runs; the cluster's isolation controls do not apply.
- No private workflow repository, runner scale set, OIDC role, or cross-repo
  dispatch is created by this decision.
- No cluster build tickets are required for the selected path. If automated
  enforcement of the three-run cap is wanted, brief that station change under
  epic #2780.

## Rejected

A separate private workflow repository and runner pool would move private
briefs and logs into another repository's Actions and require a new OIDC trust
policy, runner pool, dispatch integration, and egress review. The added trust
and operational surface is not selected for this ticket.
