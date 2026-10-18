# Dashboard settings

Run the stdlib dashboard with `python3 -m dashboard`. It serves on port 8080
by default. Set `LOOM_DASHBOARD_PORT` to change the port and `LOOM_REPOS` to a
space-separated list of `OWNER/NAME` repositories. Set
`LOOM_DASHBOARD_GITHUB_TOKEN` to provide a read-only GitHub token; without it,
the dashboard also checks the standard GitHub CLI authentication. To read
GitHub as the Loom GitHub App instead, so the dashboard spends the App
installation's request budget and not the operator account's, set
`LOOM_GITHUB_APP_ID`, `LOOM_GITHUB_APP_INSTALLATION_ID` and
`LOOM_GITHUB_APP_KEY_FILE`, the path to the App's private key. The
deployment mounts that key from a Kubernetes secret as a file; the dashboard
reads it at each token mint, signs with the openssl CLI and never logs it.
When the key file cannot be read at start, the dashboard logs the file's
name and keeps its token. `LOOM_GITHUB_APP_KEY_SSM` (a parameter-store name)
works too where the aws CLI is installed; the key file wins when both are
set. Set
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

`/tickets` lists the tickets in flight in one table per goal home; `?done=1`
lists merged, shipped, verified and closed ones, and `state`, `goal` and
`model` filter it. Its model column is the model of the newest run among
three records: the stored transcript, the lane pull request footer, and the
station's claim comment (whose model follows the ladder rules). It says "not
run yet" when none exists and "model not recorded" when the newest run names
no model. The age in state comes from the hourly label-event read.

`/goals` shows one card per top-level goal home. A goal home rolls up into a
bigger one through a `Parent goal: OWNER/NAME#N` line in its body or its
GitHub sub-issue parent, and then shows as a row inside that card. A pass
below the budget floor keeps the goal cards of the last pass that read them.
Each ticket record carries `epic` and `goal` (decision 0003): the issue its
`Part of` line or sub-issue parent names is its epic when that issue is
labelled `epic`, and the epic's own `Part of` line or sub-issue parent is the
goal; a ticket naming a goal home directly has no epic. Each goal card lists
its open epics against 6, each with its tickets against 15 and a link to
`/tickets?epic=OWNER/NAME#N`, and its closed epics collapsed. Only epics the
tickets name are listed. The ticket header reads "epic Y / goal X", and the
Backlog's "Outside any epic" lists open tickets with no epic.

The dashboard reads repository sources with up to eight concurrent workers.
`/readyz` stays unavailable until the first snapshot has fetched at least one
repository, and startup logs the time taken for that snapshot.
Each refresh keeps only the fields the pages read from each pull request and
comment it lists (`PULL_REQUEST_FIELDS` and `COMMENT_FIELDS` in
`dashboard/github.py`), not GitHub's whole answers with their embedded
repositories and users, and the Ticket page keeps at most 64 tickets' details.

Pull requests are read in full once an hour (`PR_FULL_READ_SECONDS`). Between
those reads a refresh asks only for the pull requests updated since the newest
update it already holds (`sort=updated`, newest first, stopping at the first
page that reaches back past it) and merges them by number, so a push to a
large repository costs one page instead of every page. Once an hour the
dashboard logs the GitHub requests it spent ("GitHub requests in the last N
minutes: ... counted (by kind), ... not modified").
