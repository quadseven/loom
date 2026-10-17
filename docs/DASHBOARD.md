# Dashboard settings

Run the stdlib dashboard with `python3 -m dashboard`. It serves on port 8080
by default. Set `LOOM_DASHBOARD_PORT` to change the port and `LOOM_REPOS` to a
space-separated list of `OWNER/NAME` repositories. Set
`LOOM_DASHBOARD_GITHUB_TOKEN` to provide a read-only GitHub token; without it,
the dashboard also checks the standard GitHub CLI authentication. Set
`LOOM_DASHBOARD_BUDGET_FLOOR` to change the remaining-request threshold below
which the last ticket snapshot is served as stale. The default is 500. Set
`LOOM_IMPROVEMENT_HOMES` to the space-separated `OWNER/NAME` repositories
whose goal journals and child tickets feed `/improving`; set
`LOOM_METRICS_GOAL=OWNER/NAME#N` for the daily factory numbers. Improvement
homes may also be specific goal issues as `OWNER/NAME#N`. Set
`LOOM_STATUS_REPO=OWNER/NAME` to read the station's lane status comment for
`/lanes` and the brain's tick record on its lock branch for `/brain`.
`/brain` also reads the goal journals; `/hot-spots` reads ticket waits and
the last two weeks of issue and review comments in `LOOM_REPOS`. With
`LOOM_BRIEF_AUTHORS` set, only those logins' comments count as station
outcomes, and `LOOM_REVIEW_BOT` names the review bot whose findings are
counted (default `grug-tribe`). `.env.example` lists the dashboard variables with placeholder
values. Set `LOOM_UPLOAD_TOKEN` to the same private value for the dashboard
and station so completed runs can upload to `POST /api/transcripts`.
Keep the dashboard ingress tailnet-only; the bearer token authenticates
uploads but does not replace the network boundary. The endpoint rejects
uploads without that token. Transcripts live in the configured Postgres
database in one row per ticket, lane slot, and start time, with events stored
as JSONB. The store removes runs older than 180 days whenever it initializes,
accepts an upload, or serves a transcript read. This bounds retained history
during normal dashboard use; it does not depend on how lane runs generate
their transcripts.

Brain memory (docs/decisions/0002, item 3) lives in the same database, in
three tables the dashboard creates on first use: `brain_ticks` (one row per
brain tick, keyed by holder and start: lane, model, end, outcome, and what it
decided, left open and handed off), `brain_commitments` (work a tick
deferred, keyed by item, stage and brief key, open until a later done marker
closes it) and `brain_rules` (the reviewed rules, one row per source and the
commit or comment it came from). The writes take the upload token and refuse
a request without it: `POST /api/brain/ticks` stores a tick with its
commitments (`{holder, lane, model, started, ended, outcome, decided,
left_open, handed_off, commitments: [{item, stage, key, what, due,
state}]}`, state `open` or `done`), `POST /api/brain/commitments` closes the
open commitments a list of `done` ones match (`{commitments: [...]}`), and
`POST /api/brain/rules` stores `{rules: [{source, source_ref, text}]}`, where
an empty text retires its source. `GET /api/brain/handoff` answers the
handoff packet, `{ticks, commitments, rules}`: the last 5 ticks, the open
commitments and each source's newest rules still in force. It takes the
upload token or `LOOM_HANDOFF_TOKEN`, a separate private value that can only
read, for brains that must not write the rules they follow.

`/tickets` lists the tickets in flight in one table per goal home; `?done=1`
lists merged, shipped, verified and closed ones, and `state`, `goal` and
`model` filter it. Its model column is the model of the newest run among
three records: the stored transcript, the lane pull request footer, and the
station's claim comment (whose model follows the ladder rules). It says "not
run yet" when none exists and "model not recorded" when the newest run names
no model. The age in state comes from the hourly label-event read.

The dashboard reads repository sources with up to eight concurrent workers.
`/readyz` stays unavailable until the first snapshot has fetched at least one
repository, and startup logs the time taken for that snapshot.
