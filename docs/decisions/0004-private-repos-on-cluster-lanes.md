# 0004: Private repos stay on the Mac with a cap

Status: accepted, 2026-11-08. Refs #2793.

## Context

Runner slots take public repos only, because a run's inputs and logs
live in this public repository's Actions. 8 of the 19 registered repos
are private, so their tickets stay on the Mac and keep loading it.
The Mac has 12 cores and a full swap. Each lane agent runs the repo's
whole test suite, so the host holds about 3 to 4 heavy tickets at
once. The cluster design (#2780) left the path for private repos to
this decision.

## Options

**A. A private lane-workflow repository with its own scale set and
OIDC trust.** A second copy of `.github/workflows/lane.yml` in a
private workflow repository. The station would dispatch private
briefs there, so their inputs, job logs and report artifacts stay in
a private repository instead of this public one. It would need its
own runner scale set, its own AWS role trusting that repository, and
a second dispatch path in the station.

**B. Keep private repos on the Mac with a cap.** Private tickets never
enter a workflow repository. The Mac lanes keep taking them, capped
at 3 concurrent private tickets, leaving headroom for the brain,
the station and the agy lane. Public tickets already move to the
cluster, so the Mac cap is sustainable.

## Decision

**B. Keep private repos on the Mac with a cap of 3 concurrent
private tickets.**

- Repository: quadseven/loom stays the only lane workflow
  repository. No private lane-workflow repository is created.
  Private briefs are never dispatched to `.github/workflows/lane.yml`.
  The lane workflow keeps taking public repos only.
- OIDC subject anchor: unchanged. The lane role trusts this
  repository's numeric ids, the main branch and
  `.github/workflows/lane.yml` on main, and nothing else. No second
  role is created, and no other workflow file or branch can assume it.
- Log exposure: a cluster run's inputs and job logs live in this
  public repository's Actions and can be read by anyone signed in.
  Private briefs never enter those Actions, so their ticket text,
  paths and failure reasons never appear in a public log. Private
  runs stay on the Mac, and their logs stay on the Mac under
  `$LOOM_HOME/logs`, not in Actions. The sealed report path stays
  for public runs only.

## Trust and egress consequences

- Trust: no new trust is added. The existing lane role stays
  read-only for the public slots' key parameters, the lanes App key
  and the leak scan deny-list. A private workflow repository would
  have needed a second role anchored to that repository's numeric
  ids and its own workflow file, plus the station App's dispatch
  permission there, doubling the surface that could start a pod with
  a key. Keeping private work on the Mac avoids that second anchor
  entirely.
- Egress: no new network rule is needed. The cluster egress stays
  default-deny with DNS and TCP 443 to public addresses, which
  covers every provider and the gateway. Private tickets use the
  Mac's network path as today, which already reaches the providers
  and the gateway. No cloud metadata address, API server or LAN
  range is allowed for cluster pods, and that stays true.

## Proof

No build tickets are filed for a private path, because none is
built. The decision itself records the Mac cap above, which is the
second proof line for this epic. A later decision can revisit it if
the Mac cap shows it can no longer hold the private queue.
