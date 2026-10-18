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
as JSONB. The store removes runs older than 180 days whenever it initializes
or accepts an upload, and commits that delete; a read never deletes. This
bounds retained history during normal dashboard use; it does not depend on how
lane runs generate their transcripts. Every database connection times out
after 5 seconds and sets `statement_timeout` to 5 seconds, so a slow or locked
database fails one read instead of holding a page.

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
read, for brains that must not write the rules they follow. `GET /api/usage`
answers the station's live usage readings, `{readings, received_at}`, to the
same two tokens: a brain off the lane machine reads usage there
(`python3 -m bus.usage --live`), never from its own machine's files.

`/tickets` lists the tickets in flight in one table per goal home; `?done=1`
lists merged, shipped, verified and closed ones, and `state`, `goal` and
`model` filter it. Its model column is the model of the newest run among
three records: the stored transcript, the lane pull request footer, and the
station's claim comment (whose model follows the ladder rules). It says "not
run yet" when none exists and "model not recorded" when the newest run names
no model. The newest stored run of each ticket is read once per
background refresh pass, never by a page request, so the Floor, `/tickets` and
`/backlog` never wait on the database; a pass that cannot read it keeps the
last map and lists the error under the page. The age in state comes from the hourly label-event read.
A row, and the ticket page header, carry a "held by <login>" chip when
another operator owns the ticket: an assignee or `claim:<login>` label
outside `LOOM_BRIEF_AUTHORS`, by the rule the station uses
(`bus.tickets.owned_elsewhere`). Without `LOOM_BRIEF_AUTHORS` the dashboard
cannot tell this Loom's logins from another operator's, so every
`claim:<login>` label shows as a neutral chip instead. Ticket records keep
their assignees and author, and pull request records their assignees and
label names, for this.

`/operators` lists every open issue and pull request in `LOOM_REPOS` that
another operator holds, grouped by login, with its age and its linked pull
requests (open, merged or closed, and an open one's check counts; a
`neutral` check is not counted as passed). Loom leaves these alone. Its own
section lists `loom:queued` "Land or supersede" tickets another operator
claimed. The refresh pass reads it at most every 15 minutes, and never below
the budget floor (the last read stays shown): each repo's label list, then
one open-issue list per `claim:<login>` label outside `LOOM_BRIEF_AUTHORS`
(every claim label when it is not set), plus the ticket and pull request
records the pass already holds, and the check runs of at most 30 open
linked heads. An issue assigned to another operator with no claim label
shows only when it is a Loom ticket or a pull request.

`/free` lists, per repo, the open issues free for the operator named by
`?operator=<login>` (anyone, with none), from `bus.free`, the function
behind `python3 -m bus.free` (docs/LANES.md, "Two Looms on shared repos").
It reads every page of each repo's open issues (`GitHub.paginate`, 100 a
page, following the `Link` header) at most once an hour
(`FREE_ISSUES_SECONDS`), never below the budget floor, and takes the
covering tickets from the ticket records and the comment feed every pass
already holds. Issues whose Loom ticket is closed or merged are listed to
check.

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

`/lanes` reads top to bottom. A summary strip counts the lanes held, working
and ready, shows the Anthropic share of lane runs against its 50% cap, and
lists the held lanes grouped by their reason under "Needs attention". Then
three sections, each with a one-line note: "Lanes now" (one card per lane,
held first, then working, then ready, in slot order), "How tickets are
routed" (the `bus/factory.py` tiers and model ladder, each lane marked with
its state now) and "Pull requests by model" (lane pull requests counted by
footer model, easy to hard).

A lane's hold reason comes from its last heartbeat, which the lane rewrites
only on its next pass. A hold whose until-time has passed is read as ended
(loom#809): `/usage` counts that lane free and asks nothing about it, and
`/lanes` says the hold ended and that no heartbeat has come since, instead of
a past "until" read as a live hold.
