# 0001: One dashboard for the factory, with the backlog inside it

Status: accepted, 2026-10-14. Refs #30.

## Context

The operator watches the factory in three places today, and none of them
shows the whole path from backlog to verified:

- **The cluster dashboard.** A single stdlib Python file kept in the
  operator's infrastructure repo, deployed to the operator's Kubernetes
  cluster behind the tailnet ingress. Every 60 seconds it rebuilds its state
  from a GitHub search for lane pull requests (the runner footer "Loom ticket
  <id>, lane <lane>, model <model>."), unauthenticated unless a token is set.
  It renders one card per ticket and a models page, and since 2026-10-14 it
  stores lane and brain transcripts in Postgres: the station POSTs each
  finished run (`bus/transcripts.py` bodies) and the dashboard serves
  `/api/transcripts` and `/transcripts`.
- **The lane viewer** (`lanes/viewer/`). Stdlib Python on the lane machine,
  bound to localhost, no scripts. It renders every local agent session as a
  chat, from the same parser the station uses (`bus/sessions.py`). It cannot
  be read from a phone.
- **Hunt**, the backlog board, also kept in the infrastructure repo. An
  hourly workflow on a self-hosted runner collects every open epic across the
  operator's repos with a read token, has the Cave (a local model behind the
  spark gateway, reached in-cluster) judge each epic into a tier, computes a
  deterministic score, renders a page, and publishes the page plus
  `backlog.json` into a ConfigMap that an nginx deployment serves on the
  tailnet. `backlog.json` is a real interface: agents read
  `epics[0].next.number`, and its keys (`generated_at`, `judged_by`, `repos`,
  `totals`, `epics`, `needs_operator`, `ready_for_human`,
  `queue_comments_read`) and rank order are a contract. It is the agent view,
  not the raw collector output (which carries every epic body).

The redesign merges these into one product: Floor, Backlog, Goals, Tickets,
Ticket (path, transcript, pull request), Brain, Hot spots, Improving, Lanes
and models, Needs you, and a public explainer page. Hunt stays read-only, the
brain stays the only thing that changes state, and the dashboard and
`backlog.json` stay tailnet-only.

Two questions decide everything after: where the merged app's code lives and
where it runs, and where Hunt's code lives.

## Options

### Where the merged app lives and runs

**A. Grow the lane viewer on the lane machine.** Its code is already in this
repo and reads local sessions directly. But it binds localhost, so a phone
cannot reach it without new ingress on the lane machine; it has no store, so
transcripts of runs whose files were cleaned up are gone; and the lane
machine already carries the station, the brain and every runner.

**B. Grow the cluster dashboard where it is, in the infrastructure repo.** It
already has the ingress, two-replica deploys, Postgres and the transcript
store. But its code sits in a private repo next to unrelated systems, so the
factory's own view is not reviewed or tested with the factory, lanes working
on this repo cannot change it, and it duplicates logic this repo owns
(labels, ticket states, the transcript shape, the nine numbers).

**C. Move the app into this repo, deploy it to the cluster.** The code, tests
and docs live here as a stdlib Python package (server-rendered HTML plus one
small vanilla JS file), importing `bus/` for labels, states, transcripts and
metrics. The infrastructure repo keeps only the deployment: the manifest,
the ingress route, the secrets and the Postgres database, and it pins a
revision of this repo. Everything private (hostnames, repo list, tokens, the
backlog source) is read from the environment at runtime.

### Where Hunt's code lives

**H1. Move the whole emitter into this repo.** One codebase, but the
collector and judge depend on things that are the infrastructure repo's: a
cross-repo read token in its parameter store, the in-cluster Cave gateway,
the self-hosted runner, and verdict reuse through that workflow's own
artifacts. Its product notes name people and private hosts, and its metric
step targets a monitoring service that is now off. Moving it now means
porting all of that before the board shows anything new.

**H2. Keep the emitter where it is; the merged app reads `backlog.json`.**
The collector, the Cave and the ranking stay untouched, so the rank order
cannot drift. The emitter keeps publishing `backlog.json` (and stops needing
its own page once the Backlog view ships). The merged app mounts or fetches
it, renders the Backlog view and the single Needs you queue from it, and
serves `/backlog.json` byte-for-byte at the same path, so agents keep the
same contract. The cost is two codebases for one product until a later move.

**H3. H2 now, H1 later.** As H2, with the collector and judge moving into this
repo as a follow-up once they read every private value from the environment
and have a generic home for the Cave call.

## Decision

**C for the app, H3 for Hunt.**

- The merged app is a new stdlib Python package in this repo, `dashboard/`,
  with its tests in `tests/`. It replaces the cluster dashboard file; the
  lane viewer stays as the lane machine's local fallback for raw sessions and
  is not extended.
- It runs on the cluster, deployed by the infrastructure repo, which pins a
  revision of this repo and owns the ingress, secrets, Postgres and the
  tailnet-only exposure. The public explainer is a separate static page that
  carries no data.
- Hunt's collector and Cave judge stay in the infrastructure repo for now.
  The merged app reads `backlog.json` from a source named by an environment
  variable (a mounted ConfigMap path or a URL), never writes to GitHub for
  it, and serves `/backlog.json` unchanged. Hunt's hostname is routed to the
  merged app when the Backlog view ships, so its URL keeps working. Moving
  the collector and judge here is a follow-up ticket, not part of this build.

Data ownership stays where it is: GitHub labels and comments are the truth
for ticket state, journals, lane status and the daily numbers; Postgres holds
transcripts; the emitter owns the backlog ranking. The app only reads.

## Consequences

- One review path for the factory's own view: lanes can build every screen
  through tickets on this repo, with tests that fail without the change.
- This repo stays public, so the app ships no hostnames, names, private repo
  names, IPs or paths. A test asserts the package reads them only from the
  environment, and the existing hygiene tests cover ASCII and the review
  bot's handle.
- The app needs its own read-only GitHub token (repo contents and issues
  across the operator's repos). Reading timelines for every ticket must use
  conditional requests and a budget floor, as the station does (#218), so it
  never spends the station's budget.
- Until the follow-up lands, a Backlog change that needs new collector data
  is two pull requests: the emitter in the infrastructure repo, then the view
  here.
- A missing source is shown as missing, with the reason. `null` means "not
  looked", `[]` means "looked, found nothing". Degraded, never fake.

## Build order

Each step is one or more tickets, each with a test that fails without it.

1. **Data endpoints and metrics.**
   - Ticket states across `LOOM_REPOS`: issues labelled `loom:ticket` and
     their state label (exists, as `bus.brain status` reads them).
   - Median and worst wait per state: label events from each ticket's
     timeline (new, in `bus/metrics.py`).
   - Transcripts per ticket and per attempt: the Postgres store, one row per
     slot and start time (exists; the station writes the ticket as
     `owner/name#N`); a read endpoint by ticket and attempt (new).
   - Lane status: the station's comment on the lane status issue (exists).
   - The nine numbers: the daily metrics comment on Loom's goal home (exists).
2. **Ticket page with transcripts.** The path from the label timeline (new,
   from step 1); the transcript events `{ts, role, kind, text}` (exists);
   the pull request, its checks and the review bot's threads with lane
   answers, merged by timestamp (new GraphQL read); every attempt selectable;
   running tickets poll for new events.
3. **Floor and Tickets.** Tickets by state with a "backlog" step on the left
   (backlog counts from `backlog.json`, exists); the brain's next move from
   its latest journal (exists as journal text, `bus/handoff.py`).
4. **Backlog merge and the single Needs you queue.** Epics, tiers, scores,
   completeness, parentage and the start-here card from `backlog.json`
   (exists); each epic's children joined to their Loom state (new, computed
   from step 1); the backlog health inspectors (finished but still open,
   unlinked children, nonstandard titles; new, computed from the same data);
   Needs you from `needs_operator` and `ready_for_human` (exists), merged
   with Loom's `needs-operator` asks, deduplicated by repo and number, in
   Hunt's order, with the `loom:human` count as a footer (count is new).
5. **Brain, Hot spots, Improving, Lanes and models.**
   - Brain: tick transcripts (slot `brain`, exists) and journals (exists);
     the current tick step (new: the brain tick writes it to its state file
     and transcript).
   - Hot spots: waits from step 1, failures per model and rung from ticket
     comments (exists as factory comments; aggregation new).
   - Improving: tickets under Loom's goal home and the review bot's goal home,
     each linked to the journal line that spawned it and the number it moved
     (new link).
   - Lanes and models: today's models page data (exists in the cluster
     dashboard; moves with it).
6. **Public explainer.** A static page with no data and no private names,
   the only part that may be served publicly.
