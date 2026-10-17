# 0002: A floating brain with its memory in Postgres, and no pull request outside a ticket

Status: accepted, 2026-10-17. Refs #30.

## Context

On 2026-10-17 the scanned repos held 24 open pull requests, and the brains
were not moving them. Two causes accounted for 22 of them:

- **Pull requests outside any ticket.** The remote brain wrote 7 pull
  requests itself (footer lane `main-chat`) on issues that were not Loom
  tickets. The station lands, sends back and reconciles only pull requests
  linked to a `loom:ticket`, so failing checks and review findings on them sat
  with no owner. Another 15 were left by past sessions on branches outside
  `loom/`, some for more than 30 days.
- **One brain per subscription.** Muse ticks on its own plan. The lane
  machine's hourly tick (`lanes/mac/brain.sh`) runs only the `claude` CLI on
  Anthropic credit, and it takes the GitHub stages only after no brain has
  journaled for 2 hours. When Muse's usage runs out, the factory has one
  hourly brain on one provider. When that provider is out too, no brain ticks.

A brain that starts cold also has nothing but GitHub comments to learn from.
It reads the goal homes and journals, but has no single record of what the
last brain decided, what it left half done, or which rules changed since its
model last saw them.

## Decision

1. **No pull request outside a ticket.** A brain never opens a pull request
   for an issue that is not a Loom ticket. To get code written, it briefs a
   ticket and a lane takes it. The station adopts the ones that exist anyway.
   For each open pull request by a trusted author on a same-repo branch that
   is linked to no ticket, it files an adoption ticket ("Land or supersede
   #N") in the normal brief format. The lane that takes it either lands the
   change on a `loom/` branch and closes the original with a note, or closes
   it as superseded with the reason. Pull requests from forks and untrusted
   authors are never adopted.

2. **The brain floats to whichever subscription has usage.** The lane
   machine's brain tick picks its runner from a brain ladder: the first
   brain-capable lane whose `state/usage/<lane>` file says it is available.
   The default order is the Anthropic lanes, then codex on its strongest
   model, then agy, then the Spark lanes. A tick that fails with a limit
   reason marks that lane and tries the next rung in the same tick, once.
   The 2-hour rule stays, but it counts only brains that actually ticked: a
   brain that is out of usage stops journaling, so the floating brain takes
   every stage within 2 hours, and on whatever model is up. The lock is
   unchanged, so only one brain acts at a time.

3. **Brain memory lives in Postgres; ticket truth stays on GitHub.** This
   amends the data ownership in 0001. GitHub labels and comments remain the
   truth for ticket state, journals, lane status and the daily numbers.
   Postgres, through the dashboard that already owns it, adds three tables:
   - `brain_ticks`: one row per tick, with the holder, lane, model, start,
     end, outcome, and a short structured summary (decided, left open,
     handed off).
   - `brain_commitments`: work that a brain said it will finish, with its
     owner tick, item, due time and state. A new brain starts with the open
     ones.
   - `brain_rules`: the digest of the reviewed rules (this repo's
     `docs/MUSE.md` Rules and each goal home's rules section), keyed by the
     commit or comment it came from. Rules change only through a pull request
     or a goal home edit by a trusted author. Postgres caches them, so a
     brain on any model reads the same text.

   The station writes the tick and commitment rows from each tick's journal
   and step records, through an authenticated dashboard endpoint, as it
   already does for transcripts. It renders a handoff packet (the last 5
   ticks, open commitments, rules digest and its commit) into one comment
   on the Loom goal home each pass. Every brain starts its tick by reading
   that packet. Brains that cannot reach the tailnet, such as the remote
   one, read the same text on GitHub. A dashboard that is down costs the
   packet its freshness, not the tick.

## Consequences

- A tick that names a model in its row is auditable. The autonomy and
  self-repair numbers can be read from `brain_ticks` instead of parsed from
  journals.
- When the operator's Muse usage ends, flow continues on another
  subscription within one interval of the 2-hour rule, with no operator
  step.
- The adoption pass empties the pile of untracked pull requests through the
  normal ladder instead of by hand. The first passes will file up to one
  adoption ticket per repo per pass, so the backlog does not flood.
- Muse loses the shortcut of writing code directly. The remote pull lane
  stays the one way it writes code, through `bus.lane pr`, which links the
  ticket.

## Rejected

- Moving ticket state into Postgres. GitHub is the place every brain and
  every reviewer can read, and two sources of truth for ticket state would
  need reconciling forever.
- A brain that runs inside the station process. The station is
  deterministic and must not depend on a model. The floating brain stays a
  separate, locked tick.
