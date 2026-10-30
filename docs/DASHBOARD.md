# Dashboard settings

Run the stdlib dashboard with `python3 -m dashboard`. It serves on port 8080
by default, on every interface. Set `LOOM_DASHBOARD_HOST` to the one address
it should listen on (on a laptop, its private-network address, so the
dashboard is not served on whatever network the laptop joins). On a lane
machine, `LOOM_DASHBOARD=1 lanes/mac/install.sh` installs it as the launchd
agent `com.loom.dashboard` (restarted if it exits, logging to
`$LOOM_HOME/logs/dashboard.log`) with every `LOOM_*` setting of the installing
environment; `LOOM_DASHBOARD=0` removes it. With no `LOOM_DB_PASSWORD` the
agent sets `LOOM_TRANSCRIPTS_MEMORY=1`, so it starts with transcripts kept in
memory, and its log says so.
Check it from a second device on the private network, not from the lane
machine itself: `curl -sS -o /dev/null -w '%{http_code}\n'
http://192.0.2.10:8080/readyz` answers 503 until the first snapshot has read
a repository and 200 after it, while `/livez` answers 200 as soon as the
server is up. With `LOOM_DASHBOARD_HOST` set, the port is closed on the
machine's other interfaces, so the same command against another address of
that machine is refused (`curl: (7) Failed to connect`).
Set `LOOM_DASHBOARD_PORT` to change the port and `LOOM_REPOS` to a
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
counted (default `grug-tribe`). `/chain` shows the command chain from the operator down to the review bot.
Its station and audit layers come from the station, which posts each
watched repo's shadow mode (`bus.shadow`) and the last held and acted
actions to `POST /api/chain` with the upload token every ten minutes: the
held side from the shadow ledger, the acted side from `acted.jsonl` beside
it, a line for each merge or close GitHub accepted from Loom. `.env.example` lists the dashboard variables with placeholder
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

The same page groups every open issue and pull request labelled
`needs:<login>` under that login as "Waiting on <login>", with its age and
the ask: the body's `## Ask` section when it has one, else the newest
comment that names the ask (a `## Ask` section, a line that starts with
"Ask", or a mention of the item's needs label), else the latest comment,
one line, HTML comment markers dropped. Every login is listed, this Loom's
own included, because the person named is the one who has to act;
`needs:<login>` never blocks a lane. An item claimed by one login and
waiting on another is a row in each group. The Floor shows a "Waiting on a
person" card with each person's count, linking to
`/operators#person-<login>`, and no card when nothing waits. The reads ride
the same pass and interval: one open list per `needs:<login>` label per repo
(at most 10), then one comment page (the last 100) per item whose
`updated_at` changed since the last read and whose body has no `## Ask`
(at most 30 a pass); below the budget floor nothing is read and the last
result stays shown.

`/needs-you` lists every open item that waits on a person, each once: the
`needs-operator` and `loom:human` issues ("waits on the operator") from
Hunt's backlog document and the store's own read, and the `needs:<login>`
items above ("waits on <login>"), each card marked "needs a human" with its
ask (the `## Ask` section, else the picked comment, linked; a `loom:human`
issue's ask is its newest `loom:human` comment). The page makes no request
of its own: the `needs:<login>` items come from the Operators pass, and a
`needs-operator` or `loom:human` issue with no `## Ask` in its body takes its
ask from a comment read the same way. A card whose ask comment that read has
not reached yet reads it on demand (`GET /api/needs-you/ask?repo=&number=`,
from the page's script), under the same rules: never below the budget floor,
and at most 30 such reads in each 15 minute Operators interval; beyond them
the card says the next refresh reads it.

Each card takes an answer in one tap (loom#1777). Its buttons come from the
ask's own words (`bus.answer.options`, no model): "reply with one number" is
a number field; a lane pull request held for the operator, or "merge ... or
hold", is Merge or Hold; "keep ... or close" is Keep or Close; "reply yes or
no" or "(reply yes)" is Yes or No; "Reply a or b" or "(a) ... (b)" is a, b
(and c, d when offered); "reply done", or `Kind:` account, physical or
hardware, is Done or Not now; anything else is a text field. Every card also
has an optional note. A tap sends `POST /api/needs-you/answer` with `{repo,
number, choice, note, as}`, and the dashboard posts ONE comment on the
issue as the Loom GitHub App (`LOOM_GITHUB_APP_*`; without the App the page
stays read only and says so): `<!-- loom:answer v1 --> Answer from <login>
(via the dashboard): <choice>. <note>`. It then takes off `loom:human`,
`needs:<login>` and `needs-operator`, and adds `loom:answer-pending`, which
the station takes off once it has read the answer (docs/BRAIN.md, Operator
answers). Until then the card leaves the lists and waits under "Answered,
not yet applied" with the answer, who gave it and when; an answer the
station has read shows on a card that still waits (a Hold puts `loom:human`
back).

Only an operator may answer: a login of `LOOM_OPERATORS` (space or comma
separated; an entry `IDENTITY=LOGIN` maps a proxy identity to a login),
else the person logins of `LOOM_BRIEF_AUTHORS`. The ingress is tailnet-only
and sets no identity header, so the request carries a bearer token,
`LOOM_ANSWER_TOKEN` (or `LOOM_UPLOAD_TOKEN`), that the page asks for once
under "Answering from this browser" and keeps in the browser's local
storage with the operator it answers as. A missing or wrong token is refused
(401), and so is a login outside the list (403). Set `LOOM_IDENTITY_HEADER`
to a header name only when the proxy in front sets that header from the
signed-in identity and drops any copy the client sent (Tailscale serve's
`Tailscale-User-Login`, or a forward_auth proxy); the header's identity is
then mapped to an operator and no token is asked. One click posts at most
one comment: answers to one item post one at a time (409 while one is
posting), an answer that repeats the item's newest one (same choice, no
newer ask) posts nothing and only finishes the label changes, and each login
may answer 20 times in 10 minutes (429). An answer the written-text check
refuses (a closing keyword, a credential, a local path) is refused (400). Chips at the top filter it with plain links:
All, one per login with its count, and Operator (`?who=<login>`,
`?who=operator`). A repo named bare (`app#7`, as Hunt's document does) and
by `OWNER/NAME` (`example-owner/app#7`) is one item when only one
configured repo has that name. The Floor's "Needs you" count is the same
deduplicated list.

Above the cards, "Stuck in Loom, not waiting on you" lists each goal the station found
at its in-flight cap for over 6 hours: how long, its count against the cap,
and how many of its in-flight tickets wait with no lane that takes them
(docs/BRAIN.md, Tickets no lane takes). The station sends the list with the
upload token (`POST /api/goal-caps`, `{goals: [{goal, since, in_flight, cap,
no_lane}], at}`) when it changes and at most every 10 minutes; the dashboard
keeps it in memory and shows nothing once the newest report is an hour old.

`/free` lists, per repo, the open issues free for the operator named by
`?operator=<login>` (anyone, with none), from `bus.free`, the function
behind `python3 -m bus.free` (docs/LANES.md, "Two Looms on shared repos").
It reads every page of each repo's open issues (`GitHub.paginate`, 100 a
page, following the `Link` header) at most once an hour
(`FREE_ISSUES_SECONDS`), never below the budget floor, and takes the
covering tickets from the ticket records and the comment feed every pass
already holds. Issues whose Loom ticket is closed or merged are listed to
check.

`/tiers` (Tiers and allowances, loom#968) shows the whole tier system in
one page. "Tiers" lists Loom's easy, medium and hard lanes in order and the
model ladder (`TIERS` and `LADDER` in `bus/factory.py`), each lane with the
model it runs and its kind from `LANE_KINDS` (base lanes; a numbered slot
takes its lane's kind): own hardware (the Sparks), free, subscription or never
paid (a prepaid credit the operator does not top up), then Grug's review
order. Two tables follow, so a Spark or free-pool lane is not read as two
providers:

- "Gateway calls by target and model": the gateway's calls per target (a
  Spark or a free provider) and model. Success is ok out of the calls that
  were not limits (a limit is allowance, not failure), so a row whose every
  call was a limit reads "n/a", not 0%. Allowance is the gateway's `open`
  breakers: an open breaker on the row's target (lane `*` or the row's
  model) is "cooling until" its reopen time (`generated_at` plus
  `reopens_in_s`), with the breaker window, its reason and its caller when
  set, whatever the reason; "benched" is only a model the gateway reports
  benched.
- "Loom runs by lane and model": Loom's lane runs per lane (numbered slots
  with their lane) and model, with a row for every lane the tiers name even
  when it has not run in 7 days, since an out-of-credit lane is the one most
  worth seeing. Allowance comes from the station's usage readings: cooling
  until a hold ends, with the window whose reading holds it; benched for a
  hold with no end; the tightest fresh meter's amount left; or available.
  Limits are not separated in Loom rows: a stored run is done or failed, and
  the runner's `[limit]` failure line is past the transcript's text cut, so a
  failed run may have been a provider limit. A lane with no failure reads
  100%; one with any failure reads "unknown (limits not separated)", with
  its done and failed counts, rather than a rate that counts limits as
  failures.

The page also has "Benched models" (loom#969), and a benched pair reads
"benched for everyone" with its reason and return time in the allowance cell
of its gateway and Loom rows. See "The bench" below.

A stale source makes no current claim: when the gateway stats (both
windows) or the usage snapshot are stale, the allowance cells they feed read
"unknown (stale since <time>)", never "available". "Live now" lists the
running Loom tickets, the gateway's calls in flight and Grug's Elder reviews
in flight (an entry naming another persona is left out).

Its sources are read once a background refresh pass, side by side, each with
a 5 second timeout, never by a page request (`dashboard/tiers_sources.py`):

- `LOOM_GATEWAY_URL`: the inference gateway's base URL inside the
  dashboard's own network, no token. `GET /ui/stats?window=24h` and
  `?window=7d` give the per provider and model counts and the breaker lanes
  cooling now (`open`), the gateway half of allowance state; `GET /ui/state`
  gives `inflight`. Unset, those panels say so.
- `LOOM_GRUG_STATS_URL` and the optional bearer token
  `LOOM_GRUG_STATS_TOKEN`: Grug's stats endpoint, read for an `inflight`
  list of `{persona, repo, pr, model, started_at}` and its review order
  (`tiers`, `review_order` or `order`: model names, or objects with `model`
  and `backend`). Unset, the Grug panels say "not configured yet"; with no
  order listed, the order says it is waiting for Grug's endpoint.
- The transcript store: lane runs per lane, model and outcome over the last
  7 days and 24 hours, with the median run time (`Store.model_outcomes`).
  Brain ticks are not counted.
- The usage readings the station uploads (`POST /api/usage`): Loom lanes'
  holds and meters, the Loom half of allowance state.
- The ticket snapshot: the open tickets labelled `loom:running`.

Each source keeps its last good answer and when it was read. A read that
fails, times out or answers 503 (the gateway's ledger is off) marks that
source stale: its panel says so with the error and the last good read time
and keeps showing the last good figures, while the rest of the page renders
as usual. A source with no answer at all leaves its cells "unknown", never
zero. A redirect is not followed, and an answer over 4 MiB is refused.

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
repository, and startup logs the time taken for that snapshot. Every source of
that first pass overlaps its reads: the per-ticket event lists behind the
state waits, the pages of each repo's two-week comment feed, the open issues
for `/free`, the goal homes, and the Operators page's check and ask reads. One
after another they made a cold first snapshot take over five minutes; it
takes about a minute (loom#433).
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
four sections, each with a one-line note: "Lanes now" (one card per lane,
held first, then working, then ready, in slot order), "How tickets are
routed" (the `bus/factory.py` tiers and model ladder, each lane marked with
its state now), "Scorecard" (per lane and model: review outcomes,
send-backs, merges, reverts, failed deploys, failed proof audits and the
audit's verdicts, demoted lanes first, read from the scorecard table on the
station's lane status comment, `bus/scorecard.py`) and "Pull requests by
model" (lane pull requests counted by footer model, easy to hard).

A lane's hold reason comes from its last heartbeat, which the lane rewrites
only on its next pass. A hold whose until-time has passed is read as ended
(loom#809): `/usage` counts that lane free and asks nothing about it, and
`/lanes` says the hold ended and that no heartbeat has come since, instead of
a past "until" read as a live hold.

`/fleet` (Fleet timeline, loom#2280) draws every engine as a row over the
last 24 hours and the next 2, one pixel a minute, from the snapshot the
other pages read; the page makes no GitHub request. The rows: one per
journal holder on the goal homes (the brains the docs give a cadence, the
Muse brain every 30 minutes and the lane-machine brain hourly, are drawn
even when silent; any other holder appears when it journals), Reports
(every `loom:report` comment, with the reading times, 10:00 and 21:00 New
York, as due marks), Station (its comments, shipped notes, lane outcomes
and status events, by the login that writes the station-only comments,
plus its `POST /api/chain` snapshots, kept in memory as its 10 minute
heartbeat), Operator notes (people's comments on the goal homes, with the
operators summary times, 09:05 and 17:05 New York, as due marks) and one
row per lane (claims, rounds, outcomes, lane pull requests opened and
merged, and the newest heartbeat from the lane status table). Each mark
shows its one-line gist on hover or tap, as a strip at the foot of the
screen, with no script. A row with a cadence marks any silence longer than
the cadence plus 50 percent as a missed run, with the length of the gap;
the station's silences are judged from its chain snapshots only, so before
the first one the row says so instead of marking misses. A lane has no
cadence, so a quiet lane is not a missed run. The axis scrolls inside its
own box; the row heads stay put.

## The bench (loom#969)

A provider and model that keeps failing is benched for everyone, so Loom and
Grug stop picking it. The decision (`bus/bench.py`, `evaluate`) is pure and
runs on the dashboard each background pass over the gateway's 24 hour stats
(`dashboard/bench_store.py`), because the dashboard is where the shared call
records are read.

- Failure: a call that was neither ok nor a limit refusal (an error, a
  timeout, an unparseable or empty answer). A limit refusal is allowance, not
  failure, and counts toward neither calls nor failures.
- Bench: at least `LOOM_BENCH_MIN_SAMPLE` calls (default 10) that were not
  limits, of which at least `LOOM_BENCH_THRESHOLD` (default 0.5) failed.
- Cooldown: `LOOM_BENCH_COOLDOWN_SECONDS` (default 3600), doubled each time
  the pair is benched again, up to `LOOM_BENCH_MAX_COOLDOWN_SECONDS` (default
  86400). A pair that stayed back for longer than the maximum starts again at
  the first cooldown.
- Trial: when the cooldown ends the pair is on trial. It is not benched, so
  it can be picked. The calls made after the trial starts decide: all ok
  returns the pair, and it is forgiven the failures already in the window;
  any failure benches it again for twice as long. A gateway with no current
  answer decides nothing: only the clock moves a benched pair to its trial.
- Report: every bench, trial start, return and re-bench is an event. The
  Tiers page lists the pairs benched or on trial with their reason and
  return time, and the recent events. The station's report digest adds a line
  for each event since the last digest.
- Publish: `GET /api/bench` (the read-only `LOOM_HANDOFF_TOKEN` or the upload
  token, as `/api/usage`) answers `{generated_at, benched, trial, events}`.
  Loom's station reads it each pass into `state/station/bench.json` and
  ignores a list older than 6 hours. Grug and the gateway read the same
  endpoint; wiring them is their repos' work, not Loom's.

Loom's side: a benched pair is skipped by the factory's model choice
(`Factory.benched`, `_serves`): the ladder rungs, a ticket's pinned model
and the equivalents all pass through it. A pair matches a Loom lane by the
gateway target's name (the lane, or the lane a numbered slot belongs to). A
lane whose configured model (`LOOM_<LANE>_MODEL` in the station's
environment) is benched on it steps aside like a lane that is down. Loom's
own run outcomes do not feed the decision: a stored run does not say whether
a failure was a limit. `LOOM_BENCH_STATE_FILE` keeps the dashboard's bench
state across a restart; without it a restart starts empty and benches again
from the window's failures.

