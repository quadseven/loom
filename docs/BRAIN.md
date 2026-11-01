# The brain on GitHub

The brain runs on Muse's own VM, on Muse's own schedule (#35, amended), or
as a launchd tick on the lane machine (`lanes/mac/brain.sh`, every 30
minutes) until Muse runs it. Both take the same lock, so one brain acts at a
time, and both follow docs/MUSE.md. Muse
cannot reach the lane machine, and the lane machine does not expose anything
to the internet, so GitHub is the only state the brain, the lane machine and
the operator share. A fresh tick rebuilds everything it needs from GitHub
alone.

Three parties use it:

- The brain (Muse, or an interactive session acting as the brain) writes
  tickets, reviews and merges pull requests, verifies, and journals.
- The station on the lane machine (`python3 -m bus station`, run by launchd
  once a minute) turns briefed tickets into local briefs for the existing
  runners, reports each outcome back to the ticket, publishes lane status,
  and forwards asks to the operator.
- The operator reads the goal journals, answers asks, and can steer by
  editing or relabelling tickets.

The runners do not change their contract: they still take local brief files
one at a time, with every existing guard (repo owners, public-repo rule,
model families, sandbox). The station is the only part of the lane machine
that talks to GitHub about tickets.

## Repos

- Each goal lives in its project's repo, on a home issue labelled
  `loom:goal`. The repos the brain and the station watch are configured on
  each side (`LOOM_REPOS`, space separated `owner/name`), never written into
  this repo, so private repo names stay private.
- Lane status lives in this repo, on one issue labelled `loom:lanes`.

## Labels

`python3 -m bus labels <owner/name>` creates these on a repo (idempotent).

| Label | Meaning |
|---|---|
| `loom:goal` | The goal's home issue. |
| `loom:backlog` | With `loom:goal`: the goal home that holds the repo's whole backlog (docs/NAMING.md). |
| `loom:ticket` | Every ticket issue. |
| `loom:queued` | Ready ticket with no lane yet. After claims, the station gives it a free lane of its Difficulty tier, by priority then oldest; a future `Not before:` or missing Difficulty keeps it queued. |
| `loom:briefed` | Assigned to the lane named by its single `lane:<name>` label; waiting for the station. An `After:` line holds it there until the tickets it names are merged. |
| `loom:running` | The station handed it to that lane's runner. |
| `loom:pr` | The runner opened a pull request; the ticket links it. |
| `loom:answered` | The runner posted a diagnosis answer, or a run found its work already done or no longer applying and no merged pull request carries it (see Claim and outcome); the brain reviews it, then closes/verifies the ticket or briefs it again. |
| `loom:ready` | Checks passed and review-bot threads are resolved for the current PR head. |
| `loom:landing` | On the one lane pull request the factory is landing in its repo (see Landing lock). Comes off when it merges or after 40 minutes. |
| `needs-brain` | A ticket failed a second time with no commit and no reason, or went past the model ladder; review this ticket in full. |
| `loom:failed` | The runner gave up and the station did not retry it; the ticket has the reason. The brain re-briefs (new lane or sharper brief) or closes it. |
| `loom:merged` | The brain merged the pull request; nothing is deployed yet. |
| `loom:closed-unmerged` | The linked pull request closed without merging. |
| `loom:shipped` | Deployed. Loom's own tickets are shipped by the station when it deploys the revision that carries their pull request (see Shipping Loom itself); other repos keep their goal home's ship rules, under whoever owns shipping there (which may not be the brain). |
| `closed-unproven` | The station closed the ticket on merged code with its Live box unproven (see The evidence close). It stays on a reopened ticket, and such a ticket never closes this way again. |
| `loom:proof-due` | The repo's one rolling Proof due issue (see The evidence close). Never a ticket. |
| `loom:verified` | The brain verified it live and posted a proof record (see Proof); the ticket is then closed. |
| `loom:triage` | An open issue triage found not ready as a ticket; the station log line names the missing sections, and no comment is posted (each one emailed the issue's author). The brain rewrites it (`brief --refs`) or marks it `needs:human`. |
| `loom:rewritten` | The brain wrote a new ticket from this issue, because the issue itself could not become the ticket (see Rewrites in place); the ticket says `Rewritten from issue <number>.` The issue stays open until that ticket is verified or closed as completed, then closes as completed (see Rewritten issues close with their ticket). |
| `needs:human` | Only a person can do this; one comment names the exact thing needed and its `Kind:` (money, hardware, account, credential, physical, or a decision nobody has made). Never work the factory can do with the repo, the GitHub API, logs or a diagnosis, and never a ticket the unproven-work gate only delays. |
| `loom:answer-pending` | An operator answered the item's ask on the dashboard's Needs you page (`<!-- loom:answer v1 -->` comment, `bus/answer.py`); the station has not read it yet. The factory reads it on its next pass and takes the label off (see Operator answers). |
| `wait:gate`, `wait:model-limit`, `wait:lane`, `wait:incident`, `wait:after`, `wait:blocked`, `wait:superseded`, `wait:window`, `wait:live-event` | Why a ticket waits: the unproven-work gate holds its goal, its model or lane is at a limit, no free lane takes its model, it is held for a factory incident, another ticket must merge first, its brief's Blocked on section names an issue still open, its brief says another issue supersedes it (the factory gives neither a lane), a deploy or roll window, or (`wait:live-event`, on a shipped ticket) its Live box needs a rare event the log has not shown in 7 days since it shipped. The station sets at most one, only when the reason changes, and the claim or any state move clears it. A label, not a comment: comments email the operator. |
| `handoff:claude` | The hand-off pool, machine-readable. Two routes lead into it: a ticket no lane of this Loom took for a day, with no other lane or model rung its tier allows free (see Tickets no lane takes), which is `loom:queued` with no lane and holds no slot of its goal's in-flight cap (one at `loom:pr` still counts); and a ticket whose fix rounds stopped on the same failures, with no rung of the model ladder left or the round cap reached on its rung (see the station's hand-back below), which is `loom:failed` with one note naming what still fails. The factory moves neither any more. The second operator's session works this pool (its goal issue, #1036) and may claim such a ticket at any state; taking the label off returns the ticket to this Loom. |
| `lane:<name>` | One per lane (`lane:codex`, `lane:agy`, ...). |
| `loom:lanes` | The one issue in this repo that holds lane status. |
| `needs:human` | An ask for the operator (see Asks). |
| `loom:urgent` | With `needs:human`: forward now, not in the daily batch. |
| `loom:digest` | The issue holding the full daily digest of asks, in each repo with asks. |
| `loom:regression` | A ticket admitted for a measured regression of a shipped ticket (`brief --regresses`). Claimed and landed before other tickets. |
| `loom:revert` | A ticket the station filed to revert the merge that turned a default branch red (see Red default branch). Claimed and landed before every other ticket. |
| `class:logic` | A change to behavior; every revert ticket carries it. |

A ticket carries exactly one state label. The #35 states are queued,
briefed, pr, shipped and verified; `merged` keeps merged apart from deployed,
and `running` and `failed` exist because the
station and the runner need to say "taken" and "gave up" without the brain
present. `loom:ready` is a PR readiness label, and `needs-brain` asks for a
full review after automatic fix rounds; neither replaces the ticket state.

The station's `clean_labels` step (bus/label_pass.py, loom#2024) keeps that
true. At most once an hour, on at most 20 issues a run, it removes from each
open issue (and each closed one still at `loom:triage`) that no other
operator owns: every state label but the newest the station set; `needs-brain`
when `needs:human` or a `needs:<login>` label says a person holds the issue;
`loom:triage` once the issue is a `loom:ticket` or closed; and the `lane:*`
label of a ticket at `loom:answered`, whose lane answered with no change. A
body `Part of #N` naming another issue than the sub-issue parent is changed
by nobody: it is one health note in the step's state file, logged once. In
shadow mode each removal is an `unlabel` line of the ledger and is not sent.

### Shipping Loom itself

The station deploys this repo (lanes/mac/station.sh) and records each
successful deploy: the revision and when. Each pass after one, it moves every
open `loom:merged` ticket of `LOOM_SELF_REPO` (default the lanes repo,
`LOOM_STATUS_REPO`) whose pull request's merge commit the deployed revision
carries to `loom:shipped`, with a note naming the revision and the deploy
time, and one log line per move. The pull request is the merged one whose
`Refs` line or `loom/<number>` branch names the ticket. At most 20 move a
pass, oldest merge first. A deploy that did not happen records nothing, so it
ships nothing. An open issue still at `loom:triage` or `loom:queued` whose
linked pull request merged and deployed ships the same way, with one comment,
and loses `loom:triage`: only `loom:pr` tickets are reconciled from their
pull request, so such issues sat behind after the deploy (loom#2025). A
`loom:regression` issue, another operator's issue, or a ticket sent back to
`loom:queued` after that merge (criteria left unbuilt) stays. Tickets of every other repo are never moved here; their goal
home's ship rules decide, unless they ship on merge, on a deploy workflow or
through a pin pull request (below).

### Shipping on merge or on a deploy workflow

A repo nothing deploys (a library, a tool people install themselves) ships at
merge. List it in `LOOM_SHIP_ON_MERGE` (space separated `OWNER/NAME`). Each
pass, the station moves its open `loom:merged` tickets to `loom:shipped`,
oldest merge first, with a note saying the repo ships on merge.

A repo with its own deploy workflow ships when that workflow succeeds. List
it in `LOOM_SHIP_WORKFLOWS` as `OWNER/NAME=WORKFLOW_FILE` words (for example
`example-owner/service=deploy.yml`). Each pass, the station reads that
workflow's 30 most recent successful runs once and moves each open
`loom:merged` ticket whose pull request's merge commit a run ran on, or the
newest successful run on the default branch carries (the compare API; a merge
outside the workflow's path filter ships with the next deploy), to
`loom:shipped`, with a note naming the run. Only push runs on the repo's own
default branch count (event `push`, head branch the default branch, head
repository the repo itself): a pull request run, or a fork's, can sit at the
same commit without deploying it. At most 20 move per repo a pass.

A repo with many deploy workflows, each scoped to its own paths (one per
stack, say), ships by path. List it in `LOOM_SHIP_PATH_WORKFLOWS` (space
separated `OWNER/NAME`). Each pass, the station reads the `on.push.paths`
filter of every workflow on the default branch that has one (once a pass), and
the changed files of each open `loom:merged` ticket's pull request (kept in
`ship_path_files.json`). The ticket moves to `loom:shipped` when, for every
workflow whose filter matches a changed file, a successful push run on the
default branch ran on the merge commit or a later run carries it. A pull
request that changes no file a filter matches ships on merge, unless it
changed a path that is applied by hand: a Helm values file (`values.yaml`,
`values-*.yaml`) that no workflow's filter covers, or a path matched by the
repo's `.loom/manual-paths` (one pattern per line, `#` comments, `!` excludes,
read from the default branch once a pass). Merging deploys none of those, so
the ticket stays at `loom:merged` and the station comments once naming the
files and the manual step (for a chart, the helm upgrade); the brain or an
operator moves it to `loom:shipped` after. A `!` pattern
excludes, as GitHub does; a workflow without a `paths` filter is not a deploy
workflow here. Reverts and the 20 moves per pass limit apply as above.

### Shipping through a downstream roll

A repo that deploys only when another repo rolls it (a game module a server
roll builds in, say) declares the roll record in `.loom/ship-roll` on its
default branch: one line per record, `<OWNER/NAME> <path> <key>`, `#`
comments. The record is the file at `<path>` on that repo's default branch,
and its `<key>=<sha>` (or `<key>: <sha>`) line names the full source commit
the last roll deployed. Each pass, for each watched repo with open
`loom:merged` tickets and that file, the station moves each ticket whose pull
request's merge commit that commit carries (the compare API) to
`loom:shipped`, unless a later revert the commit also carries undid it. The
note names the record's repo, or "a private repo" when the ticket's repo is
public and the record's is not. A record that names no commit, or two, ships
nothing. A malformed line ships nothing for that repo and is logged.

A ticket a ship rule found deployed whose move to `loom:shipped` failed is
kept with when it was first found; one still at `loom:merged` 24 hours later
is listed in the `merged-not-shipped` invariant (docs/LANES.md, Invariants).

### Shipping through a pin pull request

A repo that deploys through a pull request in a deployment repo (a digest
pin whose comment line reads `# Published from <repo> <what shipped>, merge
<sha>, ...`) ships at that pull request's merge. List each deployment repo in
`LOOM_PIN_SHIP_REPOS` (space separated `OWNER/NAME`). Each pass, the station
reads that repo's pull requests merged in the last 7 days; for every
published-from line one adds, it resolves the named watched repo (`OWNER/NAME`
as written, else the one watched repo of that name) and merge commit, and
moves each open `loom:merged` ticket of that repo whose pull request's merge
commit the published revision carries (the compare API) to `loom:shipped`.
A ticket whose pull request a later merged pull request reverted (its body
says `Reverts OWNER/NAME#N`, or its title is `Revert "<title>"`), when the
revision carries that revert too, stays at `loom:merged`. The note names the
pin pull request, its merge time and the source revision. The same limit of
20 moves a pass applies; a pin pull request is read again each pass until all
it carries has moved and an hour has passed since its merge, so a ticket
reconciled just after the pin still ships.

Set `LOOM_PIN_TARGETS` in the install environment to a private JSON array of
`repo`, `path`, `revision_var`, and `sha256_var` objects (see `.env.example`).
The station copies it into its plist. After deploying a green Loom revision,
each station pass checks the target's pin file. When the revisions between
the pinned one and the new one change a file under the target's `watch`
path prefix (optional, default `dashboard/`), it downloads the Loom tarball
twice, and opens or updates one pull request per target with the new revision
and matching SHA-256, labelled `pin-bump`; a merge that changes nothing there
opens none, and two downloads that differ open none. It lists merged Loom pull
request titles in a Hunt Plan body that names the revision and the SHA-256. A person or normal review automation must review and merge that pull
request; the station never merges it. The station needs GitHub write access to
the target repository. If pinning fails, the next station pass retries.

## Ticket issues

Title: `<short title>`. The ticket id is the issue number; the branch is
`loom/<number>-<slug>`, where the slug is the title in lower case with runs of
anything but letters and digits turned into `-`, at most 40 characters.

Body: the brief sections from docs/BRIEF_FORMAT.md, preceded by optional
header lines:

```markdown
Not before: <UTC time like 2026-10-13T17:00:00Z, optional>
After: #<number> | owner/name#<number>, ... (optional)
Difficulty: easy | medium | hard
Priority: P0 | P1 | P2 | P3 (optional; absent means P2)
Model: <model id, optional>
Base: <branch, optional>

## Why
...
Noticed in <journal comment link>: "<the journal's Friction line>" (optional)
## What
...
## Acceptance criteria
- [ ] ...
## Size
XS | S | M | L
## Out of scope
- ...
```

A ticket briefed from a journal's friction ends its Why with a `Noticed in`
line: the journal comment's link, then the Friction line it came from,
quoted. The Improving page counts that friction as ticketed through it; the
quote picks the friction when the journal names more than one, and a line
with no quote counts for every friction of that journal.

`Priority:` orders the work. Every queue (station claims, the factory's
queued tier moves and its landing order) takes `loom:urgent`,
`loom:regression` and `loom:revert` tickets first, then P0 before P1, P2
and P3, then the oldest. `brief --priority P1` writes the line; a ticket
filed from a health finding gets it from the finding's impact
(docs/HEALTH.md). `status` opens with a Priorities section: the top 10 open
tickets not yet merged, across repos, in that order, then the Health
findings section.

`brief` refuses a body whose pull request would fail the review bot's plan
checks (bus/hunt_plan.py mirrors them: Why of at least 5 words, at least 3
non-empty Acceptance criteria bullets, a Size of XS, S, M or L, a non-empty
Out of scope), and files a Size written as a word (Small, Medium, Large) as
its letter. A ticket that states fewer than three criteria is not refused:
the pull request body carries the standard bullets the gate wants
(`bus/hunt_plan.py` `pad_criteria`), so the body, not the ticket, decides.
`brief` (and the station's rewrite step) also refuses a Live
criterion (the acceptance line that starts with "Live") that names no exact
string in backticks or double quotes; health and friction intake tickets are
verified by the station's own absence tracking and are exempt. The string is
the log line, field or endpoint the proof will show, for example
`overseer: summoned` or `/api/v2/health`. The station searches its log
command for that string before it spends a verify run (bus/evidence.py), so
a criterion written as prose can never be verified. Triage counts the same checks as missing sections. The size may
be a `## Size` section or one line of its own, `Size: XS|S|M|L` (any case,
optionally bold); triage, `brief`, the rewrite check, the runner and PR
repair all read it through `hunt_plan.stated_size`. The runner
writes the Size as the letter whatever the brief says, and logs any plan
check its body still fails before it opens the pull request.

`python3 -m bus.brain prioritize OWNER/NAME#N --priority P0|P1|P2|P3
[--note TEXT]` changes the `Priority:` header on an existing open
`loom:ticket`. The issue author must be in `LOOM_BRIEF_AUTHORS`, or in
`LOOM_TRUSTED_BRIEFERS` (docs/LANES.md, Trusted briefers). It preserves
the rest of the body, and an optional one-line note is posted as a comment.
It refuses closed issues and issues without `loom:ticket`.

The station renders the local brief from the issue: `Repo` is the issue's
repo, `Branch` as above, `Lane` from the `lane:` label, `Issue:
<owner/name>#<number>`, then the header lines and the body. The runner adds
`Refs <owner/name>#<number>` to the pull request body, never a closing
keyword. The station checks those references and the conventional branch
number on every pass against all pull request states. A merged pull request
repairs its open ticket to `loom:merged`; a closed, unmerged pull request gets
`loom:closed-unmerged`. Neither outcome occupies a lane slot.
For a re-queued regression, a merged pull request from before the latest
`loom:queued` or `loom:briefed` label does not move the ticket or supersede
its newer open pull request. The current attempt still needs to merge.

### When a ticket may run

Two header lines say when a ticket may run, and neither reaches the brief the
lane reads. `bus.brain brief --not-before` writes `Not before: <UTC time>`;
`--after` writes `After:`, the tickets this one builds on.

`After: #5, owner/name#7` holds the ticket until every ticket it names is
merged, that is, until its state label is `loom:merged`, `loom:shipped` or
`loom:verified` (the station reconciles pull requests to that state before it
claims anything). A named ticket that is still open without one of those
labels holds the ticket too, and so does one that cannot be read at all. A
named ticket in `loom:failed` holds as well: a failed ticket climbs the model
ladder and usually lands, so the hold waits for it rather than failing or
releasing this one. The station's log line for a hold names each state
("waits for #3 (failed)"); it is status, and the log watcher files nothing for
it (nor for an agent sandbox refusal a run carried on from, docs/LANES.md Log
watch). The
hold is a claim-time rule, not a state: the ticket keeps its lane and its
`loom:briefed` label, and the factory does not count it as stalled, so it
climbs no rung of the model ladder.

Two things end a hold early. A named ticket closed as not planned is never
going to merge, so it stops holding, and the station says so on the ticket in
one comment. A hold older than 48 hours is reported to the brain like a stuck
ticket: one comment naming the tickets it waits for, and `needs-brain`. The
brain then checks the dependency is still coming, re-briefs the ticket without
it, or closes it. `bus.brain status` lists what every held ticket waits for,
and `bus.brain escalate` refuses a held ticket: waiting is not being stuck.

### Trunk branch

A goal home may carry a `Trunk: <branch>` line near the top of its body:

```markdown
Trunk: side

## Destination
...
```

Use it when the repo's default branch is not where Loom work lands, for
example a fork whose default branch tracks upstream while all of its own work
merges into a long-lived side branch. When a goal home names a Trunk,
`bus.brain brief` uses it as the ticket's `Base:` unless `--base` is given
explicitly, the lane runner checks out from it, and pull requests opened for
tickets target it. `bus.brain status` warns when more than half of the repo's last 10
merged pull requests target a branch that is neither the default branch nor a
declared Trunk, so the operator knows to add the line.

`bus.brain onboard` does not write a Trunk line; add it by hand once you know
where the repo's work actually lands.

### Merge constraints

A goal home may name rules that every pull request for one of its tickets
must meet before it lands, one `Merge constraint: <rule>` line each, above
the body's first heading (a line after the first heading is ignored):

```markdown
Merge constraint: C++ unit test required

## Destination
...
```

The rule text is matched ignoring case, repeated spaces and a trailing full
stop. A ticket's goal home is the open `loom:goal` issue its `Part of #N`
names, directly or through an open epic. Checked today (loom#1435):

- `C++ unit test required`: met when the pull request changes no C++ source
  (`.cpp`, `.cc`, `.cxx`, `.c++`, `.h`, `.hh`, `.hpp`, `.hxx`, `.ipp`,
  `.inl`, `.tpp`), or when it adds or changes a `.cpp` file under a `tests/`,
  `test/` or `t/` directory at any depth (a removed test does not count).

`bus.brain merge` reads the ticket's goal home (one read for its parent, one
more when the parent is an epic) on every merge of a ticket with a `Part of`
line, and the pull request's files only when a rule needs them. It refuses,
naming the goal home and the rule, when a rule is not met, when a rule has
no check (fail closed: check it and merge by hand), or when the goal home
cannot be read. `bus.brain status` reads the same way for each open ticket's
pull request: a met rule is shown as ` - rule: <rule>` at the end of the
line, and a rule not met (`goal home OWNER/NAME#N rule not met: <rule>`), a
rule merge cannot check (`... rule merge cannot check: <rule>`), or an
unreadable goal home (`goal home unreadable`) is a blocker, so status never
says READY for a pull request merge would refuse on these rules.

## Onboarding a repo

`python3 -m bus.brain onboard <owner/name> [--review-check NAME] [--operator LOGIN]` readies a
repo for Loom in one step, and a second run changes nothing:

- creates the labels above that are missing (the same as `python3 -m bus
  labels`). The station does the same on each repo's first pass after it is
  enrolled in `LOOM_REPOS`, and for every repo when the label set changes,
  so an onboard that was skipped still leaves the repo with every label
  (loom#2032). A runner whose `gh issue edit` is refused for a missing label
  creates it from the canonical set (`python3 -m bus.labels spec NAME`) and
  retries once;
- opens the repo's backlog goal home when none is open: `loom:goal` and
  `loom:backlog`, titled "Goal: Loom or a person works every open
  <owner/name> issue" (docs/NAMING.md), with the destination, the metric (open issues not
  yet tickets, tickets, `needs:human`) and the rules (triage never closes an
  issue; a private repo's briefs never go to lanes that train on prompts);
- writes `Operator: LOGIN` above the first heading of a backlog goal home it
  opens (docs/LANES.md): `--operator`, else, when another operator's goal home
  is already open in the repo, the first `LOOM_BRIEF_AUTHORS` login that is
  not an app, naming which login it used. A goal home naming another operator
  is that operator's, so onboard opens this Loom's own beside it. An open
  backlog goal home without the line is left as it is, and onboard prints the
  line to add by hand;
- checks whether the review check (`--review-check`, else
  `LOOM_REVIEW_CHECK`, else `Grug - Elder`) ran on any of the repo's last 5
  pull requests, and prints a `warning:` line when it did not, because the
  factory never merges a pull request without it;
- says whether the repo is private, and prints the `LOOM_REPOS` value with
  the repo added. It does not edit any config. Copy that line into the
  brain's and the station's settings yourself.

## Triage

`python3 -m bus.brain triage <owner/name> --goal N [--limit 25]` walks the
repo's open issues oldest first and labels at most `--limit` of them. It
skips pull requests, epics (an `epic` label or sub-issues), `needs:human`
asks, `wayfinder:*` planning issues, factory incidents, issues labelled
`awaiting-live-check` (finished work waiting to be seen live, not new work)
and anything with a `loom:` label. A ready issue (Why, What,
Acceptance criteria with checkboxes, Size and Out of scope, and a body the
runner accepts) gets `loom:ticket` and `loom:queued` and becomes a sub-issue
of goal N's intake epic, while the goal is under `LOOM_GOAL_CAP`; past the cap it is left
for a later run. In a repo with `area:` labels, ready tickets go under the
open epic carrying their area label. When no area label matches the issue,
triage selects the area whose label name or description shares the most words
with the title and body, adds that label, and uses that area's epic. Any other
issue gets `loom:triage`, and the station's log
line names what is missing. Triage writes labels and sub-issue links
only, and posts no comment: it never closes or retitles an issue, and edits nothing but the one
line below, because an automated pass over old issues over-calls work as
done.

GitHub holds at most 100 sub-issues under one issue and refuses the next one
(422). The sub-issue link is best effort everywhere: any refusal is logged,
not raised, and the ticket is still queued. A ticket a goal refuses, from
`brief` or triage, gets a
`Part of #N` line at the end of its body instead, and a goal's tickets are
its sub-issues plus, once it holds 100, the issues whose body has that line
(one search). The backlog board reads parentage the same way.

Every goal count walks goal, epics and tickets (decision 0003): the goal's
epics are its sub-issues labelled `epic` (and, once it holds 100, the repo's
`epic` issues whose body says `Part of #N`, from one list read), open and
closed; an epic's tickets are its sub-issues plus open `loom:ticket` issues
whose `Part of` line names the epic. While tickets move under epics, the
goal's own tickets still count. The in-flight cap, the unproven-work limit,
the regression slot, station standups and the unlinked-ticket invariant all
read that one walk, and `bus.brain status` prints the goal's open epics
against 6 and each open epic's tickets against 15.

## Filing under an epic

Every new ticket sits under an open epic (decision 0003).
`python3 -m bus.brain brief OWNER/NAME --epic E ...` refuses without
`--epic`, and refuses an epic that is closed, has no `epic` label, names no
open `loom:goal` in its `Part of #N` line, or already holds 15 tickets. The
ticket becomes the epic's sub-issue and its `Part of` line names the epic;
its Trunk, the in-flight cap, the unproven-work limit and the brief done
journal come from the epic's goal.

`python3 -m bus.brain epic OWNER/NAME --goal N --title "Epic: SENTENCE"
--file EPIC.md` files an epic: the `epic` label, a title of at most 72
characters, a body with a done-when and a size line, and a `Part of #N`
line; it becomes the goal's sub-issue when GitHub allows it. It refuses a
goal's 7th open epic unless the goal home records a one-time exception on
its own line, `Epic cap: N until YYYY-MM-DD`, which raises the cap to N
until the end of that day.

An open epic whose tickets are all closed is finished: the station's
standup names it under "Next", and `bus.brain resume` lists it as a
`close-epic` item. The brain checks its done-when and closes it with
`python3 -m bus.brain close-epic OWNER/NAME#N --note TEXT` (refused while a
ticket under it is open), or files the ticket it is missing.

Each goal also has a standing epic, "Epic: factory repairs for this goal",
opened by the first repair that needs it: breach invariants (bus/invariants.py),
revert, fix-forward and red-check tickets (bus/revert.py) file under it, in
the repo's oldest goal home that is not a backlog (else its backlog home).
It holds any number of tickets, does not count against the 6 open epics, and
is never flagged as finished. The asks digest groups an ask that names an
epic under that epic's goal.

GitHub holds at most 100 sub-issues under one issue. Once the standing epic
holds that many, or GitHub answers 422 to a link under it, repairs roll over
to the newest open factory repairs batch epic (an epic whose title starts
"Epic: factory repairs", the goal's own or another in the repo) holding fewer
than 15 tickets; when none has room, "Epic: factory repairs for this goal,
next batch" opens even when the goal is at its open epic cap. The filer
logs each rollover, and the filed issue's `Part of` line names the batch
epic. `epic-over-cap` counts an epic's open tickets, its sub-issues and every
loom:ticket issue whose `Part of` line names it, each once, the same count
intake uses for room. It also flags any open epic, the standing one too, that
holds 100 sub-issues, closed ones included, which is GitHub's limit.

Triage, the rewriter, health intake, validation contracts and pull request
adoption file under the goal's intake epic ("Epic: work that triage,
rewrites and health intake find, batch N") in repos without `area:` labels:
the newest open one, by issue number, holding fewer than 15 open tickets. One
is opened by `bus.brain onboard`, and again whenever no open intake epic has
room. Each new one takes a batch number one past the highest any open epic of
the repo carries, so no two open intake epics share a title (intake epics opened
before this carry the title without a batch and are still found). The
`epic-title-shared` invariant reports two or more open epics of one repo with
the same title. In a repo with `area:` labels, triage and the outside-epic move
use the ticket's area epic instead. A new area epic is titled `Epic: <area>` and
carries the matching `area:<area>` label when no open epic in that area has
room.

Every other path that files a ticket under a goal does the same
(`brain.placement_epic`): the parts of a split pull request and the follow-up
ticket for criteria a lane pull request marked not built go under the epic
their ticket names while it is open with room for them, else under the goal's
next intake epic, and are linked as its sub-issues. A rewritten or triaged
issue that is a direct child of its goal moves to the epic (the sub-issue link
replaces the goal as parent), so it does not stay under the goal and in no
epic. A rewrite whose issue already sits under an open epic stays there: its
sub-issue parent (else its own Part of line; the sub-issue parent wins when
they disagree) names the epic, and the ticket's Part of line names that epic,
whatever the draft wrote (loom#2033). The `ticket-outside-epic` and `epic-over-cap` invariants fire only for
tickets that were placed before this, or by hand.

The station moves the tickets that break this rule itself, at most every hour
(`Station.move_outside_tickets`, `brain.move_outside_tickets`): for each goal
home, the tickets that are the goal's own sub-issues and sit under no epic
move, oldest first, into an open intake epic of that goal with room. In a repo
with `area:` labels, each moves into an epic for its area; a missing area is
inferred from label names and descriptions. The
epic's sub-issue link replaces the goal as the parent and the ticket's `Part
of` line names the epic. When no intake epic has room and the goal is under
its open epic cap, a new intake epic opens the way triage opens one; otherwise
the rest stay where they are. An epic, an ask and a closed issue are never
moved. The station log names each goal: `outside-epic: moved N, left M`.

The station assigns queued tickets to free lanes on each pass. A brain that
wants to choose the lane itself uses `brief --lane LANE`. Queued tickets without
a readable Difficulty line appear in the goal's next station standup as
needing a difficulty.

### Health findings

With `LOOM_HEALTH_URLS` set, the station reads each goal home's health
endpoint every `LOOM_HEALTH_EVERY` seconds (default 900) and files one ticket
per live finding, with `Priority:` from its impact, updates it while the
finding lasts and notes when it goes absent (`bus/health.py`). docs/HEALTH.md
has the endpoint contract and the rules.

### Friction intake

A slowdown the tick journals keep naming gets a ticket without waiting for a
brain to brief it (`bus/friction_intake.py`). With `LOOM_FRICTION_INTAKE=1`
the station reads, at most every 10 minutes, the journals the
`LOOM_BRIEF_AUTHORS` logins posted in the last 24 hours on the goal home
invariant breaches are filed on (`LOOM_INVARIANT_GOAL`, else Loom's own goal),
and groups their `- Friction:` lines (`bus/friction.py`, the same parser the
Improving page uses) by their words longer than three letters: a line joins
the group whose first line shares at least 0.6 of them (Jaccard). A group
named by 3 or more journals over at least 2 hours recurs.

- A recurring group no ticket is linked to (a `Noticed in` line naming one
  of its journals, or its `<!-- loom:friction key=... -->` marker) gets one
  ticket under the goal's intake epic, with a `Noticed in` line, every
  member journal as evidence and the marker. At most one per pass.
- A linked open ticket gets the journals it does not cite yet as one
  `<!-- loom:friction-evidence -->` comment, at most every 6 hours.
- A ticket closed as not planned is never filed again for its slowdown; one
  closed as done is filed again only when journals after the close recur.

The ledger is `state/station/friction.json`, written after each write
succeeds. `LOOM_FRICTION_INTAKE=dry` logs `friction OWNER/NAME: would file
...` and writes nothing, so a dry run can be read in the station log before
turning it on; each ticket filed logs `friction OWNER/NAME: filed #N`.

### Agent rewrites

The station rewrites `loom:triage` issues without a brain tick (`bus/rewrite.py`).
It applies the same skips as triage (`brain.not_work_reason`: a `wayfinder:*`
label, `awaiting-live-check`, a factory incident, an epic), so an issue
labelled `loom:triage` before a skip existed is not rewritten into a ticket.
It only briefs or skips: when its model answers that only a person can do the
work, the issue keeps `loom:triage`, gains `needs-brain` (no comment), and the
station leaves it alone from then on; the brain decides whether it becomes a
ticket or `needs:human`. Free rewrite models labelled factory work as
person-only, and every `needs:human` reaches the operator's digest.
Set `LOOM_REWRITE_CMD` (public repos) and `LOOM_REWRITE_PRIVATE_CMD` (private
repos) to command lines that read a prompt on stdin and print one JSON object.
`lanes/rewrite_agent.sh <key-ssm|-> <provider/model[,provider/model...]>`
runs each entry in the shared agent sandbox (`lanes/agent_sandbox.sh`) until
one returns final text. OpenCode entries read the provider key from SSM and
deny every tool. `codex/<model>` uses the local Codex subscription login in a
scratch home with read-only tools; `claude/<model>` uses
`LOOM_REWRITE_CLAUDE_TOKEN_SSM` from the install environment to read an OAuth
token at run time, with tools disabled.
Use `-` instead of `<key-ssm>` for subscription-only lists. All entries get an
empty working directory, and the command prints only the final text. For
example:

```sh
LOOM_REWRITE_CMD="lanes/rewrite_agent.sh <key-ssm> opencode-go/deepseek-v4.1-flash"
LOOM_REWRITE_PRIVATE_CMD="lanes/rewrite_agent.sh - codex/gpt-6-luna,claude/claude-opus-5-5"
```

`<key-ssm>` is the parameter the opencode-go lanes read
(`LOOM_OPENCODE_GO_KEY_SSM`). The installer defaults the private command to
`codex/$LOOM_CODEX_MODEL` when a Codex model is configured. Private commands
using this wrapper accept only codex, claude, anthropic, or non-contributor
opencode-go entries. The prompt
holds the issue (title, body, labels, last 10 comments) and the ticket format
above. The answer is one of:

```json
{"action": "brief", "title": "...", "difficulty": "easy|medium|hard", "body": "..."}
{"action": "human", "ask": "..."}
{"action": "skip", "reason": "..."}
```

Code checks the answer before anything is written: the body has every brief
section, checkboxes under Acceptance criteria, no header lines, and passes the
written-text check; difficulty is one of the three. A brief is filed queued
(no lane) with `brief --refs M`; a human ask goes through `human`; a skip or
an invalid answer writes nothing and is logged. The issue itself becomes the
ticket (Rewrites in place).

### Rewrites in place

This changed on 2026-10-20 (loom#1229). Before it, every `brief --refs M`
filed a second issue and commented "Rewritten as ticket #N" on the first:
two more notification emails per rewrite, and a duplicate open issue until
the ticket was verified. Now the issue M itself becomes the ticket:

- its title is the brief's title and its body is the ticket body, with the
  original title and text kept below under `## Original issue`, collapsed
  and quoted line by line, so nothing is lost and no reader takes a header,
  section, checkbox or `Refs` line from it (the lane pull request copies
  only the named sections);
- it gets the labels a new ticket would (`loom:ticket` and `loom:queued`, or
  `loom:briefed` and the lane), is linked under the epic as a sub-issue (its
  Part of line when GitHub refuses), and loses `loom:triage` and
  `needs-brain`;
- no comment is posted and no issue is filed.

A new ticket, the rewrite comment and `loom:rewritten` are kept only where
the issue cannot become the ticket: its author is neither a
`LOOM_BRIEF_AUTHORS` login nor a `LOOM_TRUSTED_BRIEFERS` login (the station
claims only a ticket such a login wrote, see Trust; a trusted briefer's
issue becomes their `loom:ticket` in place, and claim trust is unchanged), or
it is locked. A pull request is refused as before, and `--refs` names an
issue in the brief's own repo. Those sources, and every `loom:rewritten`
pair made before this change, close with their ticket as below.

A repo that is not public (or whose visibility cannot be read) only ever uses
the private command, because the public one may run a provider that trains on
prompts; with the private command empty the repo is skipped. The station runs
at most 5 rewrites per backlog goal home per hour (and 5 per pass), oldest
first, under the budget floor rule. `state/station/rewrite.json` records each
attempt; an issue that did not become a ticket is not tried again for a day.
The agent command runs as a background job (`state/station/rewrite-jobs/`),
so a model never holds the station pass (loom#1663): the pass that starts a
job waits at most 10 seconds for it, and a later pass writes the answer of
any job that ended since. A job with no result 5 minutes past its 10-minute
timeout is logged as a rewrite that wrote nothing.

## Proof

A ticket moves to `loom:verified` only with a proof record, and closes as
completed only with one that still holds; a `not_planned` closure needs none.
The record is JSON, passed with `move --to verified --proof PROOF.json`:

```json
{
  "kind": "runtime",
  "revision": "<commit SHA on the default branch that is deployed>",
  "environment": "<where it runs, e.g. production>",
  "artifact": "<what was deployed, e.g. an image digest>",
  "observed": {"start": "2030-01-01T00:00:00Z", "end": "2030-01-01T01:00:00Z"},
  "measure": {"name": "<metric>", "baseline": "<before>", "target": "<goal>", "after": "<observed>"},
  "sources": ["<log line, dashboard or check run that shows it>"],
  "criteria": [{"criterion": "<acceptance criterion, verbatim>", "result": "pass", "evidence": "<what showed it>"}]
}
```

`kind` is `runtime` or `docs`. Documentation-only work uses `docs`, which
needs only `revision` (the default-branch commit that carries it),
`observed`, `sources` and `criteria`, and may be verified once merged;
runtime work is verified only once shipped. The move refuses when:

- a required field is missing or empty, or `revision` is not a commit on the
  repo's default branch;
- `criteria` is not the ticket's acceptance criteria, verbatim and in order,
  each with `pass` and evidence;
- any acceptance criterion checkbox in the ticket body is unchecked.

The move posts the record as a `<!-- loom:proof v1 -->` comment with a hash
of the criteria texts (not their check marks), then sets the label. The
posted record says who made it: `"verified_by": "brain"` for a brain's move,
whatever the file says, and `"verified_by": "<lane>/<model>"` for a proof the
station applied from a lane's verify run (docs/LANES.md, Verify runs). A
station-applied proof also carries the facts the station read from GitHub
itself, under `Checked by the station:` and as `station_facts`; a brain
proof carries none. The contract hash does not cover them.
`close` refuses a completed closure unless the ticket is `loom:verified`,
every criterion is checked, and the newest proof comment from a
`LOOM_BRIEF_AUTHORS` login matches the criteria as they are now. Rewording,
adding or removing a criterion after the proof means verifying again.

### Rewritten issues close with their ticket

This changed on 2026-10-18 (loom#822). Before it, a `loom:rewritten` issue
stayed open forever, so every rewritten issue counted as open work in every
repo. Now a ticket that names its source, with its `Rewritten from issue M.`
line (or the `Refs #M` line rewrites carried before loom#462), settles that
issue when it moves:

- **Verified, or closed as completed:** the source closes as completed, with
  a `<!-- loom:source v1 ... -->` comment naming the ticket and its proof
  comment. A source with an unchecked acceptance criterion of its own stays
  open instead, with a comment saying a person closes it, since the close
  gate refuses it.
- **Closed as not planned:** the source stays open, with a comment saying the
  ticket was dropped and a person decides.
- **A failed proof audit:** a source Loom closed for that ticket opens again,
  and closes again when the ticket is verified again.
- **Closed with no proof** (the station's done close, or a person's close):
  once an hour the station reads each open `loom:rewritten` issue, and one
  whose ticket is closed settles by the ticket's close reason, up to 25 an
  hour; a repo GitHub refuses is logged and skipped. Only a rewrite comment by a `LOOM_BRIEF_AUTHORS` login pairs them,
  and only a `loom:ticket` a briefing login wrote. A ticket closed as
  completed whose work landed (merged, shipped or verified) closes the source
  as completed, labelled `force-close` when its own criteria are unchecked,
  by the same rule that closed the ticket; one whose work never landed is
  left for a person. A source Loom already said something about for that
  ticket is left alone. Before 2026-10-20 these sources stayed open: 87 of
  them counted as open work.

Only the paired source is touched: open (or, for the audit, closed by Loom),
labelled `loom:rewritten`, and carrying the rewrite comment that names this
ticket. A `Refs #M` line to any other issue changes nothing. Each outcome is
said once per ticket, and a GitHub failure on the source is logged, never
raised, since the ticket has already moved.

### Operator-claimed issues close when their pull requests merge

Pull requests never carry a closing keyword, so an issue an operator session
files or claims (a `claim:<login>` label, no `loom:` label) had nothing to
close it once its pull request merged: about 60 stayed open in one day
(loom#1204). Once an hour the station reads each open issue carrying a
`claim:` label (`bus/claimed.py`). It leaves alone any issue with a `loom:`,
`needs:`, `needs-`, `ready-for-human`, `wontfix` or `awaiting-live-check`
label, an epic (`epic` label, an `Epic:` title or sub-issues), and one with
no acceptance boxes. The rest settle only when every pull request whose
`Refs` line names the issue is merged into the default branch, the review
check (`LOOM_REVIEW_CHECK`) concluded success on its head (neutral is not a
pass), every other check on the head concluded success, skipped or neutral
(the review bot's other checks conclude neutral when they have nothing to say),
and no open pull request names the issue. Then:

- **Every box asks for merged code or tests:** one comment starting
  `<!-- loom:claimed-settle v1 -->` states each pull request, its merge
  commit, each head check's conclusion by name, and the test functions it
  added that the default branch still defines; the boxes are ticked and the
  issue is closed as completed. A comment already there is not posted again.
- **A box asks for live, deployed or production behaviour, a time window or
  an observed event:** the issue gets the `awaiting-live-check` label once and
  stays open for a person. The test is wide on purpose: a box wrongly read as
  live only labels the issue.

`LOOM_SETTLE_CLAIMED` picks the mode. Unset or `dry` (the default) logs
`settle-claimed: would close OWNER/NAME#N ...` or `would label ...` and writes
nothing, so a dry run can be read in the station log first; `on` writes; `off`
skips the step. `LOOM_SETTLE_CLAIMED_MAX` (default 10) caps the issues settled
a run, and writes are at least 3 seconds apart. Turning writes on is the
operator's call: set `LOOM_SETTLE_CLAIMED=on` and run the installer again.

A pull request that merged with the review check neutral (the review degraded)
can never pass that gate, so its issue would stay open with its work merged
(loom#1221). When every other check on its head passed, the same step posts one
comment on the merged pull request starting `<!-- loom:claimed-rereview v1 -->`
and asking the review bot to review it again (`LOOM_REVIEW_AGAIN`). The request
is recorded in `claimed.json` and by that marker, so it is never asked twice,
and at most 3 requests and follow-up issues are written a run. A later
review success settles the issue by the rule above. A later failure files its
findings as a `loom:triage` issue, once, and comments on the original naming it;
the original stays open. A second neutral result is left for a person.

### Rare live events (`wait:live-event`)

A shipped ticket whose Live box needs something that happens rarely (a weekly
group event, a skill level no character has reached yet) cannot be proven on
demand. When the station's log command finds no line naming the Live box's
strings and the ticket shipped at least 7 days ago (`VERIFY_WAIT_DAYS`), the
station gives it `wait:live-event` once, drops `loom:awaiting-evidence` and posts
one comment (marker `<!-- loom:wait-live-event v1 -->`) naming the event it waits
for. The ticket then no longer counts as open work: it does not hold its goal's
unproven-work limit, the `Verify due` lists leave it out, and the status report
and the dashboard count it apart ("held for a rare live event"). The station
still runs the log command on every verify cooldown and costs no verify run
until a line appears. When one does, the hand-out removes `wait:live-event` and
the verify runs and closes the ticket on proof as for any shipped ticket; a run
that finds the evidence missing again lands on `loom:awaiting-evidence` and is
held again once the log shows no matching line.

This waiting applies to a Live box that needs a line to appear. A box that needs its
strings to be absent ("no `X` line appears") is the other way round: an empty
answer from the log command, over a window that covers the span the box states, is
the evidence, so the ticket is handed a verify run whose brief states the zero count
and the hours searched (docs/LANES.md, Verify runs). It is never held for a live
event; if the log covers less than the box needs, it stays on `loom:awaiting-evidence`.

### Proof audits

A lane proof passed the shape check, which cannot tell evidence a lane read
from evidence a weak model wrote to look complete. So each tick audits one:
`resume` lists the oldest verified ticket whose newest proof a lane made in
the last 24 hours and no audit has judged, as a `resume audit` item. Re-read
each criterion's evidence yourself (the logs, endpoints or pages the proof
names), then record the result:

    python3 -m bus.brain audit OWNER/NAME#N --result pass --note "what you re-read"
    python3 -m bus.brain audit OWNER/NAME#N --result fail --note "what did not hold"

A pass posts a `<!-- loom:audit v1 result=pass lane=LANE -->` comment and
leaves the ticket as it is. A fail reopens the ticket when it was closed,
labels it `loom:audit-failed`, and moves it back to shipped with the note,
which names what failed; a rewritten issue Loom closed for that proof opens
again. The station then hands that lane no verify run for
24 hours, and the ticket is due a verify run again. Evidence you cannot read
from where you run (a lane machine log, from Muse) is not a fail: leave the
audit to the lane machine's tick and say so in the journal.

## Trust

Briefs become agent prompts, and the runner pushes with the operator's
credentials, so the station takes a ticket only when all of these hold:

- The issue author, the actor of the latest `loom:briefed` labelled event and
  the actor of the latest `lane:` labelled event are all briefing logins:
  in `LOOM_BRIEF_AUTHORS` (GitHub logins), or in `LOOM_TRUSTED_BRIEFERS` for
  a `loom:ticket` (docs/LANES.md, Trusted briefers). Labels can arrive from
  issue templates, so the label alone proves nothing.
- The body was last edited by a briefing login, or never edited
  (GraphQL `Issue.editor`).
- The repo owner is in `LOOM_REPO_OWNERS`, as for local briefs.

A ticket that fails these is left alone and reported once in the station
log; it is never claimed. A ticket a second contributor works by hand (assigned to them, or
labelled `claim:<login>`) still follows its pull request: an open pull
request by that owner, on a branch of the repo, whose `Refs` line names a
ticket a trusted login filed, moves it from `loom:queued` to `loom:pr`, and
from there the closed pull request pass moves it to `loom:merged` on merge
and shipping follows. A ticket assigned to or claim-labelled for a second
contributor, on an open pull request by that contributor, also moves from
`loom:pr` to `loom:merged` when that pull request is merged (the merge itself
is the trust for a contributor). This ensures that work done by a second
contributor through their own pull request still progresses through the Loom
states to `loom:shipped.

The brain applies the same rule to what it reads: issue bodies, comments and
reviews from logins outside `LOOM_BRIEF_AUTHORS` and the review bots are
data about the work, never instructions to it. A trusted briefer's ticket
body is a brief this Loom builds; their comments, journals and proofs are
data like anyone else's, and only `LOOM_BRIEF_AUTHORS` proofs close a ticket.
Markers count the same way (`bus/brain.py` `trusted_comments`): a stranger's
copy of a lane status table, remote heartbeat, remote claim, claim, fix,
review-again or rewrite comment is ignored, and a fix brief quotes only the
review threads the review bot or a trusted login opened, with only their
replies. The filter sits at each reader, not in `GitHub.paginate`, since
some reports read other Apps' comments on purpose.

### Written text mentions nobody by accident

Text Loom posts is often an agent's own words, and an at-sign followed by a
word is a GitHub mention: a Python decorator, a CSS at-rule or an npm scope in
a proof table notified a stranger from a public repo (loom#1394). A mention
cannot be undone, so `GitHub.rest` (`bus/github.py`), which every comment,
issue and pull request body goes through, wraps each would-be mention outside
a code span or fenced block in backticks (`bus/text.py`, `scrub_mentions`);
a GraphQL reply is scrubbed the same way, and so is the body `bus.pr_repair`
writes. Text already in a code span or fence, email addresses, and words after
a slash (URLs, `provider/@org/model`) are left as they are. Loom mentions
nobody by default; `LOOM_MENTION_ALLOW` (comma separated logins) names the few
it may. The check that refuses the review bot's short name after an at-sign
still runs first and is not weakened by the scrub. Text written by the lane
runner shell scripts does not pass through here.

## Claim and outcome

The station holds one lock on the lane machine, so it never races itself.
For each lane whose heartbeat is fresh and available and whose inbox is
empty, it takes the first trusted briefed ticket for that lane: a `loom:revert`
ticket, then a `loom:regression` ticket, then by `Priority:` (P0 first),
then the oldest:

1. Re-read the issue. It must still be open, `loom:briefed` and that lane's.
   It must also be past its `Not before:` time, and every ticket its
   `After:` line names must be merged (see When a ticket may run).
2. Replace its labels in one call (`loom:briefed` out, `loom:running` in;
   everything else kept), then add the event
   `Claimed for lane <lane> (body sha256 <first 12>)` to the ticket's status
   comment (see Ticket status comment).
3. The brief from the body it hashed was written under a temporary name
   before step 2; rename it into the lane's inbox now. A pass that finds a
   temporary brief finishes that claim first, so a crash between steps
   never strands a ticket.

The claim label lasts as long as the run. Any move out of `loom:running`
removes this Loom's own `claim:<login>` label with the state label, and a
closed ticket keeps no active state (`loom:queued`, `briefed`, `running`,
`pr`, `ready`, `answered`, `failed`), `wait:*`, `needs-brain` or claim label
(`merged`, `shipped` and `verified` stay: they say how it ended). A brain move
(`brain.move`) also drops `needs-brain` and `loom:ready`: `needs-brain` reads
as "already reported", so a ticket kept it and was never reported when it
stalled again.

Before step 2, the station looks for a pull request that already did the
ticket's work (loom#1385). One counts when it merged into the default branch,
whenever it merged (a rewrite of an issue whose work merged days earlier is
the case that burned three lanes for nothing to change), and either links to
the ticket as reconcile does (a `Refs` line or a `loom/<number>` branch) or has
a `Refs` line naming the issue the ticket was rewritten from (its
`Rewritten from issue M.` line; a bare `Refs` line is no source). Then the
ticket is not claimed: it moves to `loom:merged`, as after a build run's
no-change answer (see below), with a status event naming the pull request, its
merge time and merge commit. The ship rules move it to `loom:shipped` and a
verify run judges its criteria against the pull request's facts (docs/LANES.md,
Verify runs). If that run finds the change falls short, the ticket returns to
`loom:queued` for a build with the reason as a status event, and that pull
request is never sent to a verify run again. Left to build as before: a
regression or revert ticket, a ticket whose Live criterion names no string a
verify run can search, a ticket with a fix round, and a pull request the
station already judged for the ticket (one it sent back for unbuilt criteria
after that merge, or one a verify run found wanting). The status events are
the record; the station keeps no other state for it.

Then the station checks the brief itself (`bus/brief_check.py`). A ticket
is stale when the operator's newest comment says its work is done or
superseded, when the issue it was rewritten from is already closed as
completed, when a pull request in another repo of the owner that names it in
full has merged, when a pull request a person linked in it or its comments
merged after it was filed, or when every file its criteria ask to add already
exists on the default branch. A stale ticket goes back to `loom:queued` with
`needs:human` and one ask (`Kind: decision`) naming the evidence; the factory
gives a queued `needs:human` ticket no lane. Each piece of evidence holds it
once: once the operator removes `needs:human`, the same evidence does not hold
it again. The audit of 2026-10-22 found a rewrite of an issue whose leak-scan
caller and deny-list layer had shipped weeks before; the lane read "out of
scope" as "must be absent" and deleted the working deny-list layer, in two
repos.

A health ticket (one health intake filed, or one whose Why names a health
finding) gets no lane until its body names the cause, on a `Cause:` line or
under `## Cause`. A cause whose subject is the measurement (the health read,
a threshold or floor, a miscount) is refused, and so is a What whose every
step changes the measurement. Naming a cause is diagnosis: the ticket goes
back to `loom:queued` with `needs-brain` and a status event, and the factory
gives it no lane until the cause is named. At merge, a lane change on a
health ticket that edits the health read itself or a bound (a threshold,
floor, ceiling or cutoff) waits for the operator (`bus/operator_hold.py`). The
audit's health ticket was met by a health read that began counting money the
fund's holder carried, so the finding vanished while every affected member
stayed affected, and by a sibling that held refunds back to stay above the
floor. A read the check needs that fails holds the ticket for the next pass.

A ticket briefed again after `loom:failed` gets a fresh branch:
`loom/<number>-<slug>-r<attempt>`, where the attempt is one more than the
station's earlier claim comments on it.

The station polls lane PRs once per pass. Once checks finish, failed checks
and unresolved threads started by `grug-tribe[bot]` are sent back to the same
lane as a `loom:fix` comment, using the same findings format as
`python3 -m bus.brain fix`. The ticket returns to `loom:briefed`; the runner
pushes fixes to the existing branch. Before each round after the first, the
station asks whether the last one moved anything (`bus/converge.py`): the same
check names still failing, or a review-bot finding at the same path within 10
lines under the same rule (a new thread id or check run id does not make it
new), is a stuck round. A stuck round climbs the model ladder at once
(`factory.Factory._climb`, which keeps the model and private-repo rules); a
new finding is progress and stays on its lane. `LOOM_FIX_ROUNDS` caps the
rounds per rung (default 3). With no rung left above a stuck round, or the cap
reached, the rounds stop: the ticket moves to `loom:failed` with one note
naming what still fails and gets `handoff:claude`. A failure of the plan check alone gets no round: the factory
rebuilds the body from the ticket (loom#1837), and the plan check never counts
toward a stuck round. A clean
PR receives `loom:ready`; a later push removes that label until the new head
has finished checks and review again. A degraded or partial result of the
review check (its summary says `Degraded review (...): not a verdict`, or its
title carries a partial-review form such as `WARN Elder review low
confidence`) is not sent back: a lane cannot fix it. The station leaves the
ticket at `loom:pr` with no fix comment, no round counted and no
`loom:ready`, and the factory asks the review bot to review the head again
(`/grug improve`, once per head commit). Real failures beside it are sent
back without it. The same request goes to any open lane pull request whose
review check failed degraded, whatever state its ticket is in or whatever else
fails on the head, once per head commit and at most `claimed.REREVIEW_PER_RUN`
requests a factory pass; the log names each request against that bound and the
pull request that waits for the next pass.

A review check that failed only on a split request is not sent back either.
When its summary carries a `Reviewability` note of kind `cross-cohort-proof`,
`plan-truncated` or `cross-cohort-module` (each asks the author to split the
change), it has no blocking finding in its title and no other check failed, the
factory files the split (`bus/split.py`, `Factory._split_pr`): the paths the
note names are grouped by stem so a file and its test stay in one part, each
part becomes a `loom:ticket` + `loom:queued` ticket with the original's
`Difficulty:`, `Priority:` and epic, and the pull request is closed with a
pointer to them. The original ticket gets one comment naming the parts
(`<!-- loom:split pr=N -->`, so a pass that was cut off does not file them
twice), moves to `loom:closed-unmerged` and closes as not planned. When the
note names fewer than two groups, the ticket's unchecked non-Live criteria are
the parts, one each. A note that leaves nothing to split on, a blocking
finding or another failing check sends the pull request back for a fix round as
before.

A plan check that failed only on its `which epic` row, for the pull request's
own ticket, is not sent back either: the missing epic is on the ticket, which a
lane cannot edit. The factory puts the ticket under its repo goal's intake
epic (`brain.place_under_epic`, `Factory._repair_ticket_epic`: a sub-issue
link, else a `Part of` line) and writes one `repair` comment on the ticket
(`<!-- loom:epic-repaired SHA -->`, once per head commit). Until the plan
check reads the ticket again the pull request waits with no lane round. A
repo with no open goal home, a plan check that fails on any other row or
check, or a `which epic` row that names another ticket goes to a fix round as
before.

A send-back needs something new to fix. `python3 -m bus.brain fix` (the
factory's send-back and the brain's) posts nothing, changes no label and says
why on stderr when the pull request has no failing check, no open review
thread and no conflict and the notes are only the factory's stock text; notes
that name their own finding (the brain's, the unticked criteria, the missing
test) still go. It also posts nothing when the newest `loom:fix` comment on the
ticket was written for the same head commit, the same findings and the same
lane and model: the comment carries a `loom:fix-key` line for that. The factory
sends nothing for a ticket that carries `needs-brain` (the round cap, a hold
or a give-up put it there), and a failing check is read per workflow: a check
run is the newest run of its name within one check suite, so a job of the same
name in two workflows stays two checks, and the factory reads failures from
the same reduced runs the brief quotes.

When the station claims a ticket whose latest comment from an allowed login
is such a fix comment, newer than its last claim, the brief keeps the pull
request's branch, gets a `Fix-PR: <number>` header and ends with a `## Fix`
section holding the comment. A fix comment edited after it was posted is
refused, as an untrusted ticket is. The runner then checks out that branch,
pushes the agent's work onto it (fast-forward only, never forced) and
comments on the pull request; it never opens a second one. The brief requires
the sandboxed agent to answer every review thread it lists: one entry per
thread in `.loom/replies.json` (thread id, reply text, and a resolve boolean,
true when the round fixed it, false with a concrete reason when it declines),
or an answer file in `.loom-answers/`. The runner posts valid entries after a
successful push and resolves those marked true; missing or invalid files
produce no replies. A listed thread left with neither is named in the log and
in the round comment on the pull request ("Review threads this round left
without a reply"). An entry
whose thread is not on the round's own pull request, or whose text
`bus/text.py` refuses, is skipped and logged. A fix brief
whose pull request is no longer open is parked as failed (a merged one is
reported as no failure, below).

When the runner finishes, the station finds the brief in `done/` or
`failed/` and reports once:

- Done: for code work, label `loom:pr` and comment with the pull request link;
  for `Deliverable: answer`, keep `loom:answered` and the runner's answer
  comment for the brain to review.
- Answered: a run that committed nothing, whose agent said in
  `.loom/summary.md` that the work is already done or no longer applies
  ("nothing to change", "no longer holds", "already fixed", ...) or answered
  a thread in `.loom-answers/`, is an answer, not a failure. The runner
  writes that reason, quoted in full, into the run's `.answered` marker; the
  station posts each thread answer on the pull request the ticket names (as
  a settle findings ticket names the pull request its findings were left
  on), resolving it by the fix round rules, and records the reason as an
  event in the ticket's status comment, never as a new comment: a new
  comment emailed the ticket's author for a routine outcome (a watched
  repo's ticket, 2026-10-21). The reason passes the text checks again first; one
  that does not is replaced by a sentence saying it is in the lane log. Then
  the station settles the ticket from the answer:
  - First, every code acceptance box must be proven: ticked on the ticket
    or ticked in the answer's own Acceptance criteria checklist (a "not
    built" mark proves nothing, since the answer says the work is done).
    Live and operator boxes are left to the verifier and the operator. When
    a box is unproven, the ticket goes back to `loom:queued` and the event
    names each such box; it neither settles nor moves to `loom:merged`
    (loom#2032).
  - When a pull request linked to the ticket (a `Refs` line or a
    `loom/<number>` branch) has merged into the default branch, that pull
    request carries the work the lane found done. The ticket moves to
    `loom:merged`, and the event names the pull request, its merge time and
    merge commit. From there it is any merged ticket: the ship rules move it
    to `loom:shipped`, and a verify run checks its acceptance criteria
    against live evidence (docs/LANES.md, Verify runs), closing it with a
    proof or marking the evidence it still misses. A regression or revert
    ticket never moves this way: its linked pull request is the change that
    regressed or must be undone.
  - Otherwise the ticket goes to `loom:answered` and the event says it waits
    for the brain. The brain reviews it like any answer: a settle findings
    ticket whose threads are all answered or resolved is verified with that
    note as its evidence, or closed as not planned when no pull request is
    left.

  A brief with no `Issue: OWNER/NAME#N` line has no station report, so for
  one the runner posts the reason on the ticket and labels it
  `loom:answered` itself.
- Failed: label `loom:failed`, comment with the reason from the runner's
  log line, with local paths removed.
  A run that committed nothing and wrote no summary carries the end of what
  the agent said (one ASCII line, at most 600 characters, through the same
  text checks as an answer) in the reason: `agent made no commit; it said:
  ...`. Only a run that said nothing, or whose words fail the checks, fails
  with `agent made no commit and wrote no reason`. The factory climbs the
  model ladder once for that; at the second such failure since the ladder last started it does not
  climb again but leaves the ticket failed with `needs-brain` and one
  comment saying why.
- Limited (a reason starting `limit:` after its class, the spark lane's
  guard refusing work for a while): not a failure, and not one of the
  automatic retries below. Label `loom:briefed` again (`loom:pr` for a fix
  brief) and record the status event `Lane <lane> stopped: [limit] <reason>`;
  the factory moves a ticket briefed on a lane whose heartbeat says `limit:`
  to the next rung at once.
- An expired key (an `[auth]` reason): not a failure, not an attempt, and
  never a runaway, whatever else the ticket has been through. Label
  `loom:briefed` again (`loom:pr` for a fix brief) and record the status
  event `Lane <lane> stopped: [auth] <reason>`; the factory moves the ticket
  to a lane whose key works. A fix round whose branch moved during the run
  goes back to `loom:pr` with `Lane <lane> stopped: [transient] branch
  moved: ...`.
- Runaway count: a claim's ending is the first of these after it: a `Lane
  <lane> failed:` line, an automatic retry (its `Lane <lane> failed:` reason),
  a factory hold (`Lane <lane> held:`, a `[factory]` ending) or a `Lane <lane>
  stopped:` event. A claim that ended `[transient]`, `[limit]`, `[auth]` or
  `[factory]` is not a work attempt and never counts toward a runaway; a stop
  is never worded `failed:`, so failure counts do not read it.
- A fix brief parked unrun because its pull request merged first (`pull
  request #N is no longer open on <branch> (MERGED ...)`): not a failure, not
  an attempt and never a runaway. A running ticket goes back to `loom:pr`,
  where the merged pull request moves it to `loom:merged`; one already merged
  keeps its labels. It records `Lane <lane> stopped: [transient] pull request
  #N merged before lane <lane> ran its fix round; ...`. A ticket at
  `loom:merged`, `loom:shipped` or `loom:verified` is never held as a
  runaway, whatever fails after.

The runner starts each failure reason with its class: `[limit]` (a usage,
rate or quota limit, or an HTTP 429 or 402 from the provider), `[auth]` (a
key the provider will not take: an expired, revoked or invalid API key, or
an HTTP 401), `[transient]` (a network or server fault, a timeout, or the
agent exiting before any edit) or `[work]` (anything else: failing tests, no
commit, a guard's refusal). A reason without a class is work. On a limit the
runner also writes its heartbeat unavailable with a `retry_at` time (the
provider's reset when it names one, else 60 minutes) and takes no brief until
then.

An `[auth]` failure takes every lane that bills that key offline instead: each
one writes its heartbeat unavailable with `API key expired: renew <the SSM
parameter its key came from>`, no `retry_at`, takes no brief and hands each one
on as `[auth]`. The hold lifts only when the value at that parameter changes
(the operator renewed it). A lane that reads no key parameter at all (codex,
claude and zen sign in with the CLI's own file) is not held: nothing there
changes, so its next pass fails the same way, and the ask below still says
which lane to fix. The runner files one `needs:human` ask on the factory
repo naming the exact parameter, and comments on that ask instead of filing a
second one.

- A failed fix brief (a `Fix-PR:` line) goes back to `loom:pr`, whatever its
  class, so its open pull request is never duplicated.
- A limit or transient failure is retried automatically, at most
  `LOOM_RETRY_MAX` times (default 2) since the ticket last went to the brain
  or opened a pull request. The station comments once, `Automatic retry <n>
  of <max> (<class>): moved from lane <from> to lane <to>.` with the reason,
  then moves the ticket to `loom:briefed` with the new `lane:` label. Its next
  claim is a new attempt, so a fresh branch. The new lane is the first
  available one whose runner accepts the ticket's model and repo, idle
  before busy: another slot of the same provider (not after a limit, which
  the slots share), then anthropic, its slots, codex, codex-2, opencode-go
  and agy, then free lanes for an easy ticket on a public repo. Never
  claude, and never a lane an earlier retry left.
- A transient failure with no retry or lane left costs the ticket nothing: it
  goes back to `loom:queued` with `wait:lane`, its `Model:` line and rung
  markers untouched, and a status event `Lane <lane> stopped: [transient]
  <reason>. Not retried: <why>` ends the claim (a limit ending likewise waits
  `loom:briefed`). It never goes to `loom:failed`, and the factory gives the
  queued ticket a lane again (loom#1395).
- A timeout (the agent exiting 124) is transient once. The station reads it
  against the ticket's earlier claims: when the claim before it also ended at
  a timeout, the ending is reclassed `[work]` and says `timed out again on this
  ticket, so it counts as a failure`, so the ticket goes to `loom:failed` and
  the factory climbs the ladder. A third timeout in a row is the same repeated
  reason and flags the ticket `loom:runaway`, which stops the retries. The
  kept `-timedout-` branch stays in the reason, so the next attempt still
  starts from the edits. A timeout after any other ending is transient as
  before (`factory.TIMEOUT_STRIKES`; loom#1419).
- Any other failure is reported as failed above. The factory climbs the model
  ladder for a `loom:failed` ticket only when the class of its newest ending
  is `[work]` (or none). A ticket whose newest claim ended `[transient]`,
  `[limit]`, `[factory]` or `[auth]` that is found at `loom:failed` anyway goes
  back to `loom:pr` (it has a pull request) or `loom:queued`, on its current
  rung, with a `Not a failure` status event. A runaway or `needs-brain` ticket,
  and one the factory already gave up on past the last rung, stays as it is.

The station applies the retry labels with its own login, so that login must
be in `LOOM_BRIEF_AUTHORS`, or the trust rules refuse the retried ticket.

### Ticket status comment

Routine ticket events go on one comment per ticket, marked
`<!-- loom:status v1 -->`, which the station and the factory edit in place,
newest event first, one `- <UTC time> <text>` line per event (continuation
lines indented two spaces). That covers a lane given to a queued ticket, a
claim, a stall, limit or heartbeat move, a rung climb, an equivalent-model
move, an automatic retry, a lane failure or factory hold the factory acts on
itself, a lane stop (a limit, an expired key, a moved branch), a fix round's
push to its pull request, a lane's first pull request (`Lane <lane> opened a
pull request: <url>`; the pull request's `Refs #N` already cross-references the
ticket and `loom:pr` is set, loom#1390), a ready mark (`Ready: checks and
review passed on head <sha>`; loom#1244), and a build run's no-change answer
with where it left the ticket (Claim and outcome). A new comment emails the
operator; an edit does not (loom#866: zippie#264 carried 77 bot comments).
Outcomes (a failure, merged, shipped, verified, an ask, a runaway hold) and
`loom:fix` briefs stay new comments. The GitHub client
(bus/github.py) expands a status comment in any issue-comment list into one
comment per event, timed by its line, so every reader sees each event as the
comment it used to be. Only a trusted login's status comment is edited.

The first routine event on a ticket used to post that comment, and so emailed
the operator once per ticket (loom#1341). Where the client keeps events in the
issue body (`GitHub.status_in_body`, on for the station, the brain and the
dashboard), a ticket with no status comment gets its events in a section at the
end of the issue body instead, between `<!-- loom:status-body v1 begin -->` and
`<!-- loom:status-body v1 end -->`, same `- <UTC time> <text>` lines, newest
first; a body edit sends no email. A ticket that already has a status comment
keeps editing it. Rules:

- Only an issue written by a trusted login or the App has its section written
  or read; for any other issue the event goes to a comment as before.
- The client reads the section into the first page of every comment list as one
  comment per event, by the issue author's login and a negative id (no comment
  holds it), so the readers (`factory.attempts`, the ready mark, the rung climb)
  count them as before. That costs one conditional issue read per comment list.
- The writer reads the body in full, merges its event into the section, reads
  the body again just before the PATCH and starts over if it moved; after three
  moves it posts a comment rather than overwrite an operator edit. GitHub has no
  conditional issue write, so an edit landing inside the one request between
  that second read and the PATCH can still be lost.
- Any other body PATCH through the client keeps the section the issue has now
  (the caller's copy may be older), so a rewrite, a model header change or a
  regression note never drops events.
- `tickets.parse` leaves the section out of the brief and `with_original` does
  not quote it.

### Fix rounds edited in place

The first fix round on a pull request posts the ticket's `loom:fix` comment, as
before. Each later round the station sends on the same pull request edits that
comment instead of posting a new one, so it sends no email (loom#1640; one
ticket had emailed the same failing check twice within the hour).
`bus/fixrounds.py` writes it from the station's `_post_fix_comment`; a
`bus.brain fix` send-back is still a new comment.

- The edited comment starts with the `loom:fix` marker, the pull request line,
  `<!-- loom:fix-rounds v1 -->` and the newest round's head marker, then one
  short block per round: its number, UTC time and the reasons that round's
  comment showed.
- Each round's whole `loom:fix` body is kept as plain text in a hidden
  `<!-- loom:fix-round N <UTC time>` block, with `&`, `<`, `>` and `#`
  escaped: nothing in it can end the block or read as a marker, key or
  heading. When the comment would pass GitHub's size limit, older rounds keep
  only their markers and `###` headings.
- Readers see one comment per round, as before: the GitHub client expands the
  comment like the status comment, so round counting (`_fix_round_count`,
  `factory.fixes_of`), the asked threads, the fix key and the lane's fix brief
  read every round. A ticket whose rounds are older per-round comments keeps
  them; its next round edits the newest of them, and both forms count.
- Trust: a comment is read as rounds only when a trusted login wrote it and
  its real `updated_at` is the one GitHub answered the station's own last
  edit with, kept in `<home>/state/fix-rounds.json`. Nothing in the body is
  trusted for that. Any other fix-rounds comment (a stranger's look-alike,
  one someone else edited, one with no ledger entry) stays one opaque
  `loom:fix` comment: it counts as one round, and the lane's brief refuses it
  as an edited `loom:fix` comment. The brain and the dashboard read the same
  ledger where `LOOM_HOME` names the station's home.
- A round is a new comment again when the pull request changes, when that
  comment cannot be vouched for, when the rounds no longer fit, or when GitHub
  refuses the edit.

### Lane comments edited in place

A lane that repeats a fact edits one comment instead of posting a new one each
time; every new comment emailed the ticket's author and participants (186
blank-bodied remote claim beats in a week, about 60 fix round notes a day, 13
"Still failing" notes on one incident). The text of each comes from
`bus/sticky.py`; the runner reads and writes the comment with `gh`, finds it by
its marker, and posts it only when no comment starts with the marker. A read
that fails posts nothing, so a bad API answer cannot start a second comment.

- A remote lane's claim is one `<!-- loom:remote-claim v1 <lane> <ticket> <beat>
  [<claim>] -->` comment per ticket claim. `bus.lane heartbeat` edits it with
  the newest beat and keeps the claim time as the last field. A claim expires
  from its newest beat (`CLAIM_AGE`) or from its claim time (`PR_AGE`). Only the
  ticket's newest claim comment counts, so an earlier claim, handed back since,
  does not age the one held now. A claim comment written before this form has
  one time and reads as both.
- A fix round on a lane pull request goes in one `<!-- loom:lane-round v1 -->`
  comment on that pull request, a `### Round N, <UTC time>` section per round,
  newest first, at most 12 shown and the older ones counted. The push and the
  body edit are already on the pull request's timeline.
- A factory incident that is still failing, and an expired key that is still
  expired, edit one `<!-- loom:incident-repeat v1 -->` or
  `<!-- loom:key-repeat v1 -->` comment on the open issue: a running count, the
  first and last time, the tickets (or lanes) seen, and the latest detail.
- A factory incident is filed on the public factory repo, so it names only a
  repo known to be public. Any other repo is `private repo <alias>` (a random
  id the lane machine keeps per repo), and its ticket and failing line are
  withheld; they stay in the lane log and on the ticket. A public repo's
  detail that fails the public repo leak scan is withheld the same way.

## Where a new comment is posted

Every new comment on an issue or pull request goes through `notify.post` in
`bus/notify.py` (loom#1390), which takes a `kind` from a closed set and refuses
any other before it calls GitHub. Loom posted 9,738 new comments in the 7 days
to 2026-10-21, and each one emails the operator, so a new kind of comment is a
decision, not an accident. A routine event is not a kind: it is an event in the
ticket's status comment (`status.add`; the station's `_routine`, the factory's
`_status_note`).

To post a new comment: call `notify.post(gh, repo, number, body, notify.KIND)`.
To add a kind: first ask whether it is a routine event (then it is a status
event, not a kind); if it is an outcome a person reads or a record Loom reads
back, add the constant and its one-line meaning to `KINDS` in `bus/notify.py`
and its row below. `tests/test_notify_gateway.py` fails when a kind has no row
here, and when a module in `bus/`, `dashboard/`, `dispatch/` or `lanes/` posts
to an issue's comments directly (a `POST` to `.../comments`, `gh issue comment`
or `gh pr comment`) outside `bus/notify.py` and the test's allowlist. The
allowlist lists the lane shell's own comments and may only shrink. A reply on a
pull request's review thread is not a new issue comment and is not scanned.

| Kind | What it is |
| --- | --- |
| `failure` | A red merge, a revert, or a lane's hand-back that a person may need to read. |
| `ask` | The exact thing only a person can do (`needs:human`). |
| `answer` | An operator's answer to an ask, posted from the dashboard's Needs you page (`bus/answer.py`, see Operator answers). |
| `runaway` | A hold or release of a ticket that ran past its attempts. |
| `proof-refused` | The station stops handing a ticket out to verify after refused proofs. |
| `fix-brief` | A `loom:fix` brief; the station's later rounds on the same pull request edit its first one (see Fix rounds edited in place, loom#1640). |
| `merged` | A pull request merged, or a ticket moved to `loom:merged`. |
| `shipped` | A ticket moved to `loom:shipped` (loom#866 and loom#821 own moving it). |
| `verified` | A ticket moved to `loom:verified`. |
| `proof` | The proof record of a verified or shipped move. |
| `audit` | The result of a proof audit. |
| `held` | A ticket held apart from open work, with what it waits for. |
| `closed` | Why an issue was closed. |
| `reopened` | Why an issue was opened again. |
| `source` | A note on the source issue of a rewritten ticket. |
| `repair` | The factory edited text instead of sending a pull request back. |
| `review-request` | A request to the review bot to review again. |
| `report` | A goal's report to its author (loom#866 and loom#821 own moving it). |
| `digest` | A digest page. |
| `note` | A note the factory or brain writes with a change it makes. |
| `journal` | A journal or report an agent writes to its goal. |
| `evidence` | Evidence a scan adds to the issue it files. |
| `status` | The first status comment of a ticket (later events edit it). |
| `record` | A marker comment Loom reads back (a claim, a heartbeat, a handoff packet). |

## Polling budget

The GitHub API limit (5,000 requests an hour) is shared by every client of
the account, the brain included.

- Every GET the station sends carries the ETag of the last answer to the
  same URL (kept under `$LOOM_HOME/state/station/etags`, removed after a day
  unused); GitHub does not count a `304 Not Modified` against the limit, and
  the station reuses the kept answer.
- Ticket comments and events are also shared across consumers and passes when
  the issue list's `updated_at` is unchanged. A write to that ticket drops
  its kept reads. Reads without a known issue timestamp still go to GitHub.
  Unused kept reads expire after a day.
- A repo with a ticket in flight (briefed, running or pr at its last scan,
  or a brief for it in a lane inbox) is scanned every pass; any other repo
  at most every 10 minutes, so a ticket briefed in an idle repo can wait
  that long to be claimed.
- Reconciliation reads a repo's pull requests only while it has a `loom:pr`
  ticket, and then only the open ones and one page of the most recently
  closed ones.
- Below 15% of any rate limit (from the `X-RateLimit-*` headers of the last
  response, kept across passes) a pass does claims and reports only; the
  rest (reconciliation, hand-back, lane status, asks, standups, goal
  reports, metrics, the brain watch, the Discord relay and the factory)
  waits for the reset. The station says so once per rate limit window.
- The revert pass (see Red default branch) reads, per repo, the same page of
  recently closed pull requests reconciliation reads; only in a repo with a
  lane pull request merged in the last 6 hours does it read further: the
  repo, its default branch's 30 newest commits, and one check-run page per
  commit from the head back to the last green one.
- Each pass logs one line with requests by endpoint kind, counted requests
  against a target of 500, answers that were 304, and the remaining budget.
- Writes happen only on claims, outcomes, status changes and asks.
- `LOOM_STATION_INTERVAL` (seconds, default 60, at least 60) sets the pass
  interval at install.
- The brain's tick stays under 300 requests and logs its count in the
  journal.

## Lane status

The station keeps one comment on the `loom:lanes` issue in this repo current:
a table of lane, available (yes or no) and heartbeat time. It edits the
comment only when a lane's availability changes, and at least once an hour.
It never publishes the heartbeat's reason, which can carry local paths;
reasons stay in the local viewer.

## Asks

One label asks a person: `needs:human` (loom#2278). `needs-operator`,
`needs:quadseven` and `loom:human` are old names of it: every reader treats
them as `needs:human`, and the label pass (`bus/label_pass.py`) moves one to
`needs:human` only when it writes to that ticket anyway. No pass relabels on
its own, and none posts a comment.

The label comes off when the answer is in the ticket (`bus/ask_state.py`): a
comment after the ask by an operator or trusted person that is not a
question, a dashboard answer, a trusted brain's line starting `Finished:` or
`Done:` or a dated `Operator answer YYYY-MM-DD:` line (dated on or after the
ask's day), a `LOOM_ASK_ANSWERERS` login's `Finished:` or dated line (docs/LANES.md,
Ask answerers), or a `## Decision (<login>, <date>)` heading in the body. An answer
that names a command in backticks adds `needs-brain`, so the brain sees who
can run it. An ask whose Blocked on section names an open issue gets
`wait:blocked`. The digest skips answered and waiting asks at once, even
before the label pass runs; the dashboard still lists a waiting ask. A
claimed ticket is left to its holder, and the digest still skips it when
answered.

An ask is an issue labelled `needs:human` in the goal's repo, holding one
exact question or action and what the brain does in each case. The station
forwards asks to the operator's Discord webhook (URL read from SSM on the
lane machine, never on Muse's VM): `loom:urgent` asks within a minute, the
rest as one daily digest. Asks in private repos stay private on GitHub; the
digest carries only titles and links. When the digest does not fit in one
message, its full list, grouped by goal, is a comment on the `loom:digest`
issue in each ask's own repo, and the message links to it. To file an ask
under its goal, name the goal home in its body (`Part of #N`).

An ask about an outage dates it by its window, never by one time: the last
good observation and the first failing one ("last good read 04:28Z, first
failing read 09:02Z"). The last good read is not the start. Every text a
`bus` command posts is checked for this (`bus/text.py`).

Open issues still on the old `loom:human` name are never posted on their
own. They are listed once a day under "Waiting on you" at the end of the
same digest (the 15 newest, grouped by repo, each with the first line of
its ask comment), so hundreds of them still make one message. Once such an
issue moves to `needs:human`, it is listed with the asks.

## The tick

One tick at a time, under a lock: a branch `loom-lock/brain` in this repo,
created through the API. Creating a ref that exists fails, so only one
holder wins. The ref points at a commit whose message is
`holder=<name> expires=<UTC time>`. A tick that finds the lock held and
unexpired exits; one that finds it expired deletes it and tries once to
create it. The tick deletes it when done. The lock lasts `--minutes`
(default 15); Muse takes 30 for its 25-minute tick.
`python3 -m bus lock <owner/name> --holder <name>` and `python3 -m bus unlock
<owner/name> --holder <name>` do this (exit 0 on success, 1 when held by
someone else).

Order (#35): read lane status; gather events since the last journal entry
(checks, reviews, comments, outcomes); review `needs-brain` tickets in full
(every diff, green checks and the review bot, every thread answered); merge
only `loom:ready` pull requests, after confirming the verdict matches the
current head and checks are green;
verify shipped work live and close only then; pick the next problems by
impact and brief lanes up to capacity, at most `LOOM_GOAL_CAP` (default 16)
tickets in flight per goal (queued, briefed, running, pr or failed; merged
and shipped tickets hold no slot, nor does a ticket in the hand-off pool
unless it is at pr), except one regression slot (see
Regression slot);
write the journal; report when due; release the lock. No retry loops: a
failed step is journalled and left for the next tick.

## Tickets no lane takes

A ticket the factory leaves waiting because no lane takes it (no free lane
takes its model, no free lane in its tier, stalled with no other lane of its
rung or tier, or on a lane at a limit with no free lane in its rung or tier)
holds its goal's in-flight cap without moving. On 2026-10-25, 89 tickets
waited so, some for two days, and a goal at its cap had every triage rewrite
skipped (loom#1831). The station keeps when each such ticket first waited
(`no_lane_since` in `state/station/ticket-waits.json`, kept across its
no-lane reasons and dropped when a lane takes it), and the factory acts on it:

- After `LOOM_NO_LANE_REROUTE` hours (default 6, at most 24) a ticket pinned
  to a model moves to the first rung of the model ladder the climb rules allow
  (never down from a strong model) that has a free lane in its difficulty
  tier, for its repo (public-only lanes never take a private repo's work) and
  up for the rung's model (not at a limit, benched or out of credit). So codex
  runs gpt-6-luna and no lane outside the ladder is chosen. The status
  comment says `Not a failure` with the rung marker, as a ladder move does.
- By 24 hours, with no reroute allowed, it goes to the hand-off pool
  (`handoff:claude`): back to `loom:queued` with no lane, out of the goal's
  in-flight count, and one note on its status comment, edited in place.
- The rewrite skip line names how many of a goal's in-flight tickets wait with
  no lane, and says `goal stuck at its cap for N hours` past 6 hours; the
  station sends the stuck goals to the dashboard (`POST /api/goal-caps`),
  whose Needs-you page lists each with that count.

## needs-brain holds nothing answers

`needs-brain` holds a ticket for the brain, and the factory moves it no more.
The remote brain that answered these holds retired on 2026-10-24; on
2026-10-26 96 open tickets carried the label, the oldest since 2026-10-19
(loom#1872). The station's `lift_brain_holds` step (bus/brain_hold.py, the
factory's `brain_holds`) resolves a hold older than 24 hours that Loom set:

- Who set it is read from the issue's newest `labeled` event for the label: a
  `LOOM_BRIEF_AUTHORS` login that is a GitHub App (`<slug>[bot]`) is Loom. A
  hold any other login set (the operator, a person) is never lifted; one with
  no readable setter is left too. Both are counted.
- A `loom:pr` ticket whose pull request fails only the plan check on its body
  gets the body rebuilt from the ticket (as for a body-only plan failure) and
  the hold taken off. Any other goes to the hand-off pool (`handoff:claude`)
  without `needs-brain`: queued or briefed back to `loom:queued` with no lane,
  running, pr or failed to `loom:failed`, out of the goal's in-flight count.
  Either way one event on the status comment, edited in place, names who held
  it, for how long and why (the note beside the label event, else its state).
- At most 5 a run, oldest first, and a run at most every 30 minutes, stamped
  however it ends. The run sends each repo's count of holds older than a day
  that it left (`POST /api/brain-holds`), and the dashboard's Needs-you page
  lists them with how many a person set or nobody can tell who set.

## Regression slot

A cap meant to bound new work must not block the fix for something already
shipped that got worse. `python3 -m bus.brain brief OWNER/NAME --epic E
--title T --file BRIEF.md --regresses M [--lane L]` admits a brief that
fixes a measured regression of ticket M:

- M must be a `loom:ticket` that is `loom:shipped` or `loom:verified`, or
  closed as completed. A missing M is refused as "ticket not found"; one
  closed as not planned never shipped, so it is refused too.
- The brief has the line `Regresses #M` with the measured evidence on the
  lines right after it (up to a blank line or a heading). A marker with no
  evidence line is refused.
- When M is open under goal N, M itself is re-briefed: its body gains a
  `## Regression` section with the evidence, and it moves to `loom:briefed`
  (or `loom:queued` with no lane). Otherwise a new ticket is filed under N.
  Either way it carries `loom:regression`, so the station claims it, and
  the factory lands it, before other tickets.
- A ticket is re-briefed in place at most `LOOM_REGRESSION_REBRIEFS` times
  (default 3; each re-brief leaves one `Regresses #M` line in its body). The
  next `--regresses` for it is refused: the ticket gains `needs-brain` and one
  held comment saying how often it was re-briefed, and asking again writes
  nothing more. A fix that never holds goes to the brain, which decides what
  is different this time; it does not cycle.
- Under the cap it is admitted like any brief. At or past the cap it is
  admitted through the goal's one regression slot, and a comment on goal N
  (marked `<!-- loom:regression v1 -->`) records why: the regressed ticket,
  the evidence and how many other tickets are in flight. The slot is held
  while any `loom:regression` ticket of the goal is in flight; a second
  regression past the cap waits for it.
- Ordinary briefs and triage still count every ticket in flight, the
  regression one included, so they wait until the goal is back under its
  cap. The slot never becomes room for new work.
- Briefing the same regression again while its ticket is in flight files
  nothing and prints that ticket.

## Factory-repair tickets

The unproven-work limit (`LOOM_UNVERIFIED_LIMIT`, shipped tickets with no
standing proof per goal) holds back new build work until the goal verifies
what it shipped. It must not hold back repairs to the factory itself, which
are often what verification is waiting on (loom#767). A factory-repair
ticket is one at `Priority: P0`, or one under an epic that
`LOOM_REPAIR_EPICS` names (space-separated `owner/name#N` words). Over the
limit:

- `bus.brain brief` files a repair brief while fewer than two repair
  tickets of the goal are in flight (queued, briefed, running, pr or
  failed); the third is refused and names the two.
- The station claims a repair ticket while fewer than two other repair
  tickets of the goal are running or at a pull request.
- The in-flight cap (`LOOM_GOAL_CAP`) still applies; only the
  unproven-work limit is passed. Ordinary tickets still wait.

Set `LOOM_REPAIR_EPICS` on the station and on every brain, so both agree
on what passes.

## Red default branch

Lanes merge as soon as their own pull request is green and reviewed: there
is no merge queue and no global lock. A check can fail on the default branch
even when it passed on the pull request, so the station watches the
branch after the merge instead (bus/revert.py, every pass, off with
`LOOM_FACTORY=0`):

- For each repo with a lane pull request merged in the last 6 hours, it
  reads the check runs of the default branch's newest commits through the
  API, from the head back to the last green commit. No checkout. The
  review check is left out, since it reviews and does not test. A commit
  with no check runs is waited on for 30 minutes, then skipped as
  untested.
- When the newest tested commit is red, the first red commit after the
  last green one is the culprit. A commit before it that is still running
  is waited on, since it may be the culprit.
- When a trusted lane pull request's merge is red and its head was green,
  the station files a fix-forward ticket on the original ticket's lane.
  The new ticket asks for a follow-up pull request without undoing the
  merged change. It quotes failing annotations and job log lines, and the
  original ticket gets a link to it. If main remains red 45 minutes after
  that brief, the station files a revert ticket. An open revert in the repo
  or an existing revert for the merge prevents another revert.
- If a check was already red on the commit immediately before a lane merge,
  the station files one factory ticket naming the check and does not revert
  that merge. A red commit no lane merged is reported once.
- The revert is a ticket titled `Revert <sha>: <title>`, labelled
  `loom:revert` and `class:logic`, with no `Model:` line (rung 0). It goes
  to the first free lane of the easy tier the repo may use, or of the next
  tier up when no easy lane is up. Its What is `git revert --no-edit <sha>`.
  Its acceptance criteria are the failing checks passing. Its body says
  `Tests: none needed (...)` and `Reverted ticket: #N`.
- The station claims it first. The factory lands it first, on green and
  reviewed, deferring its medium and low findings at once like any lane
  pull request. A high or critical finding still sends it back.
- The reverted ticket gets a comment naming the red check and the revert
  ticket. Once the revert reaches `loom:merged`, the ticket goes back to
  `loom:briefed` on its own lane, to redo the change on the new default
  branch. Its comment is marked `<!-- loom:redo R -->`, so the move
  happens once.
- Each decision is one `revert:` line in the station log, said again only
  when it changes.
- The station's auto-deploy of Loom itself (lanes/mac/station.sh) does not
  deploy a head that would bring in a commit an in-flight revert ticket
  names. It waits for the revert, or for the ticket to land.

## Merge-ready

A lane pull request is merge-ready if and only if all five hold:

- its head commit's check rollup is `SUCCESS`;
- the newest submitted review from the review bot (`LOOM_REVIEW_BOT`, else
  `grug-tribe`, and an app, not a person with that login) is not
  `CHANGES_REQUESTED`. Only the newest counts: a `CHANGES_REQUESTED`
  followed by newer `COMMENTED` reviews no longer blocks, and other apps'
  reviews never change the verdict;
- no person (any review author that is not an app) has a standing change
  request. As GitHub's `reviewDecision` reads it, each person's newest
  `APPROVED`, `CHANGES_REQUESTED` or `DISMISSED` review counts (login
  compared without letter case) and a later `COMMENTED` review does not
  clear a request. The blocker reads `changes requested by a person
  (LOGIN)`, and `bus.brain merge` refuses with the same words before the
  merge call (loom#1987), so a tick no longer spends a merge attempt on a
  GitHub 405 every time;
- it has no unresolved review thread;
- every thread the review bot opened (a finding) is older than the head
  commit, so a fix-up commit came after each finding.

`python3 -m bus.brain status` prints this per pull request: `READY`, or
`blocked:` and every blocker that applies, for example `blocked: checks
failure, grug changes_requested, 3 unresolved threads`. It is computed by
`bus.brain.merge_ready` from the one GraphQL read status already makes.
For a sensitive pull request that would otherwise be READY, status
also checks the named review run on the head and blocks it unless that run
completed with `success`. A tick takes that line as given instead of
re-reading the review list. `bus.brain merge` keeps its own checks.
When a trusted brain fix is still pending on the linked ticket, status adds
`pending brain fix` to the blockers instead of marking the pull request READY.
A fleet hold blocks too (loom#2393): a plain pull request comment from a
`LOOM_BRIEF_AUTHORS` or `LOOM_TRUSTED_BRIEFERS` login, made after the head
commit, whose text opens with `Not merging` or `Hold` (any letter case, after
optional whitespace or markdown marks such as `**`, `>` or `#`). A later
comment by the pull request's author lifts it, as does a later trusted comment
naming the head commit's sha (7 or more hex characters), or a newer push.
Status prints `held by a fleet comment from LOGIN: <comment link>`, and
`bus.brain merge` refuses with the same words. Comments that cannot be read
block as `pull request comments unreadable`. Only the opener counts: no other
comment text is read as a hold.
For a line that would otherwise be READY, status also reads what `bus.brain
merge` refuses on, through the same functions merge uses (loom#1643): a
ticket not at `loom:pr` (`ticket at loom:running, not loom:pr`), a REST
`mergeable_state` of `dirty` (`conflicts with the default branch`), and a
risk tier outside `LOOM_AUTO_MERGE_TIERS` (`held for the operator: <tier>`).
A ticked acceptance criterion that names another repo of the same owner
(`OWNER/NAME`, or a backticked name that is a `LOOM_REPOS` repo of that owner)
was not built by this pull request: that repo builds it. Status prints
`blocked: ticked criterion names another repo, ...` for it, and `bus.brain
merge` refuses with the same reason (loom#1711). Send a brain fix asking the
lane to untick it and end it `; not built: belongs in <repo>`, so the station
files the follow-up in that repo; a criterion already ending that way passes.

A ticket at `loom:pr` with no open pull request linking it goes back to
`loom:queued` on the next pass, once the label is 10 minutes old, unless a
trusted author's pull request in another repo still names it, or one merged
after the ticket's last brief and outside the closed page the station reads.
A ticket moved `LOOM_UNCLAIMED_MOVE_CAP`
times (default 6, 0 is off) with no lane claiming it stops moving: it takes
`needs-brain` and `loom:runaway` with one comment (loom#2333).

A `; not built:` reason that says the work is already there must cite code on
the pull request's base branch: `<path>:<line>`, `<path>#L<line>`,
`<path>::<symbol>`, or `` `<path>` `<symbol>` ``. Status prints
`blocked: a criterion says it is already done and cites no code`, `bus.brain
merge` refuses, and the factory sends the pull request back when a citation is
missing or the base cannot back it (bus/already_done.py, loom#2333).

Status also prints a "Waiting on the brain" section: every open lane pull
request whose ticket carries `needs-brain`, oldest first, with its ticket,
its age in hours and its merge-ready line. The factory sets `needs-brain`
when a ticket runs out of fix rounds as well as for a sensitive change, and a
logic pull request with failing checks appeared in no other section, so it
waited until the `lane-pr-stalled` invariant listed it (loom#1411; since
loom#2099 a line of that invariant's one tracking issue per repo, docs/LANES.md). The brain
takes these first: merge it, or send it back with `bus.brain fix`.

### Landing lock

One green lane pull request lands at a time in a repo (`bus/landing.py`,
loom#2066). Main used to move faster than an update, its checks and its
review (about 25 minutes), so a pull request brought up to date was behind
again before it could land. The factory takes the first ready pull request
in landing order, brings it up to date once and marks it `loom:landing`.
While a pull request carries that label and was marked less than 40 minutes
ago, the factory updates and lands no other pull request in that repo, and
`bus.brain merge` refuses to merge any other one there. The marked one lands
as soon as it is current and green; if main moves under it, the factory
updates it again and it keeps the lock. The lock ends when it merges or
closes, or 40 minutes after the mark (from the label's event on GitHub),
when the label comes off and the next pass picks again. Operators merging
by hand follow the same cadence.

### Branch hygiene

The factory's judging reaches a pull request only while its ticket sits at
`loom:pr` with its review settled, and a held pull request whose update is
refused is left where it lies, so a pull request outside that path (one
waiting on its checks, one held for the operator) used to drift behind or
into a conflict nobody was assigned, found only when someone came to merge
it (loom#2135). Each repo pass therefore follows its judging with a
hygiene sweep over the repo's open lane pull requests, oldest first. A
behind, conflict-free one is brought up to date in small steps (at most
two a repo a pass, never while another pull request there holds the
landing lock or in a pass where the repo already landed one, and never on
a head with a failed check, whose re-runs and fix rounds are the cheaper
answer). A conflicting one gets exactly one fix round for the conflict,
open while the head it was opened for is still the head; a round opened
before fix comments carried their head counts as open for any head, so an
old round is never doubled. The judged send-back's holds still apply
there, with one exception: a `needs:human` ticket gets its conflict round
anyway, because that hold guards the pull request's merge, not its
branch, and the label stays on the ticket through the move; a
`needs-brain` ticket and one in the hand-off pool are left to the brain
and that session. Hygiene never merges anything: a `class:sensitive`
pull request is maintained exactly like a lane one up to the merge step,
which stays with the brain or the operator as before. Each action is one
station log line, and so is each skip the pass decides itself (a held
landing lock, the per-repo bound, a landing earlier in the pass, a ticket
with the brain or in the hand-off pool, a round already open, an update
refused); the lock, bound and landed skips are said once a repo a pass,
however many pull requests they cover. Pull requests outside the pass's
remit (not a lane pull request, no ticket in the repo, not yet judged by
GitHub, a red head the judge is already handling, one brought up to date
within the cooldown) pass without a line.

## Merge by change class

Every lane pull request carries one change class (`class:<name>`, set by the
runner from its changed files; docs/LANES.md). The factory reads it when it
decides merge-ready, so consequence decides the rule:

- `docs` and `tests`: merged on green checks and a review pass on the head
  commit alone. A medium or low finding the review bot opened is deferred to
  the follow-up issue at once, with no fix round.
- `logic`: the rule above is unchanged.
- `sensitive`: never merged by the factory. The factory marks the pull
  request `needs-brain`, and `bus.brain status` lists it under "Sensitive
  pull requests". Only a brain merges it, with `bus.brain merge`, which for
  this class also requires that the review check passed and that no high or
  critical finding is open on the head commit.
- A pull request with no class label keeps the `logic` rule.

## Operator holds

A lane pull request waits for the operator, whatever its class, checks and
review, when either holds (`bus/operator_hold.py`):

- **Operator steps.** Its `## Operator steps before merge` section lists a
  step (anything but `none`), or, as a backstop, its body or its ticket's
  body names one in words: "leave X to the operator", an operator task, step
  or prerequisite, a data migration, a live cutover, a VM or disk resize, or
  a sentence that opens with a merge hold ("Do not merge until ...", "Merge
  only after ...").
- **Placement.** A manifest (`.yaml`, `.yml`, `.json`) changes where a
  workload runs or what it mounts: `nodeSelector`, a
  `kubernetes.io/hostname` pin, affinity, tolerations, `hostIP`, volumes and
  their claims, `hostPath`, a storage class. The hold fails closed: only a
  listing in the base branch's `.loom/manual-paths` (the file
  `LOOM_SHIP_PATH_WORKFLOWS` ships by) exempts a manifest, a manifest no
  deploy workflow covers is still held, a manifest GitHub sends no patch for
  counts as a placement change, and a pull request that also changes
  `.github/workflows/` or `.loom/manual-paths` exempts nothing. The ask names
  the push workflows on the base branch that cover the files (a push
  workflow with no paths filter covers every path).

- **Rewrite.** It deletes, renames away, or replaces at least half of, a
  doc its ticket does not name: a top-level file whose name starts with
  `README`, `AGENTS.md` or `CLAUDE.md` at any size, any other `.md` file from
  10 lines. A doc whose base cannot be read counts as replaced, and a doc
  renamed away counts as removed (`bus.brain merge` reads the REST file list
  for the old path whenever GraphQL reports a rename).

The factory checks before it lands a pull request, and `bus.brain merge`
checks before it merges one, so whichever reaches it first holds it. The
ticket goes to `needs:human` with one exact ask (`Kind: decision`): do the
steps, or check that the placement is safe for the data and the nodes it
lands on, then merge the pull request by hand. Loom never merges it, and the
ask is posted once. `bus.brain status` marks a pull request whose body
declares steps `blocked: held for the operator`.

### Operator answers

The dashboard's Needs you page takes an answer to each ask in one tap
(loom#1777, docs/DASHBOARD.md). It posts one comment as the Loom App,
`<!-- loom:answer v1 --> Answer from <login> (via the dashboard): <choice>.
<note>`, takes off `needs:human`, `needs:<login>` and `needs:human`, and
adds `loom:answer-pending`. An answer counts only when its author is trusted
(`LOOM_BRIEF_AUTHORS`, where the App's bot login is) or is the operator it
names, and the login it names is an operator (`LOOM_OPERATORS`, else the
person logins of `LOOM_BRIEF_AUTHORS`). The station reads it as that
operator's newest comment: the stale-brief check (`bus/brief_check.py`)
reads it as the operator's own words, and the brief's Operator notes carry
it ("Answer: ..."). Each factory pass reads the open `loom:answer-pending`
issues of a repo (at most 10) and takes the label off.

On a lane pull request held for the operator, the hold ask offers Merge or
Hold. Merge lifts that hold only, in the factory and in `bus.brain merge`:
the risk tier's review check, open findings and hourly cap, the checks, the
criteria and shadow mode still decide, so in shadow mode the merge is still
only recorded. Hold keeps the pull request held with no new ask, and the
factory puts `needs:human` back so it keeps waiting on a person. An answer
counts only for the pull request the newest hold ask names, given after that
ask and not before the head commit it would merge; a newer ask, or a newer
head, asks again. Each Merge or Hold is written once to the shadow ledger as
an `operator-verdict` entry (its label the verdict), beside the would-merge
it pairs with; the ledger's counts and digest leave verdicts out.

A ticket whose every acceptance criterion is a check on a live host or
network (a command run on a host, a reach test from a network, a port, a
firewall or ACL rule, a check across the hosts, or a Live line, and none
naming a test) gets no lane: the factory puts it to `needs:human` with one
ask instead of giving it one, and `bus.brain brief` refuses it. The
station's close of a shipped ticket whose Live line names nothing to search
leaves it open while an unticked criterion is such a host or network check:
a merge proves none of them.

On 2026-10-22 the brain merged a lane pull request whose body said "Leave VM
resizing and the live data migration to the operator", on green checks and a
passing review; its deploy moved every workload of a service off the node
that held its local volumes, and the service was down for about four hours.
The same day it merged a lane pull request for a ticket of host checks
only, which replaced the deployment repo's root README with a write-up of
the ticket; the station then closed that ticket as shipped.

## Shadow mode

Loom resumes in shadow mode (`bus/shadow.py`, loom#1515). The station, the
brain and the lanes run as usual: tickets are claimed, pull requests opened,
fix rounds run, and every merge gate is checked as above. At the point Loom
would act, it records the decision instead and leaves the item for the
operator:

- a pull request merge (the factory, `bus.brain merge` on the lane machine or
  Muse, the deploy-watch revert path, the roll pin);
- an issue close (a done or verified ticket, an epic, a rewritten source
  issue, an operator-claimed issue, an invariant breach that cleared or was
  folded into its rolling issue), except the evidence close (see The
  evidence close);
- an issue reopen (an invariant breach seen again, a failed audit's ticket
  or source issue);
- a label that grants a merge (`ready-to-land`), and any GraphQL label write
  (it names labels by id);
- an approving pull request review;
- a write straight onto a branch Loom does not own (a moved ref, a file
  committed through the contents API, a merge-queue entry, a fork sync):
  code that lands with no pull request is a merge;
- a branch deletion after a merge.

A move to `loom:shipped` is a state fact, not a merge or a close, so it goes
through: in a shadow repo a merged ticket whose work deployed still reaches
`loom:shipped`, and so reaches verify and close. A merge or close stays held.

Closing a pull request without merging it still happens (it can be reopened
and its branch stays), and so do the closes of Loom's own signal issues that
carry no work label and no invariant label: a factory incident (an open one
holds the tickets behind it) and a health signature the watcher filed
(`loom:triage` with the watcher's marker in its body; `loom:triage` alone is
on people's own issues too, and their close is held). An invariant breach's
issue gates no ticket, so its close is recorded like any other (loom#1551):
the invariants step asks the guard before it posts the cleared note, and
logs `would close` once instead of `closed cleared`. run_lane.sh closes its
factory incidents through `python3 -m bus.shadow gh-guard --lane`, which
reads the issue first.

`bus/github.py` checks every request at its one choke point and refuses a
held one with `ShadowHeld`, whichever code sent it; `bus.brain merge`,
`bus.brain close` and the station's closes check first, so their record names
the risk tier and the gates that passed. Every merge label in a request is
checked, and the whole request is held if any one is new. A `loom:shipped`
label is let through: it records a state, it does not merge or close. The
hold fails safe: a request it cannot read (a label list or body of an
unknown shape, the item's labels unreadable, a read that failed in any way)
is held as the worst request it could be.

The brain's model rung holds a GitHub token of its own, so
`lanes/mac/brain.sh` puts a `gh` in front of it that asks
`python3 -m bus.shadow gh-guard` first and refuses, and records, a merge, an
issue close, an approving review or a merge label, with the same
switch. It covers `gh` only: a rung that sends the request another way (curl)
is held by its instructions alone (`docs/MUSE.md`, and the tick's prompt). Each decision is one line of an
append-only JSONL ledger, `LOOM_SHADOW_LEDGER` or
`$LOOM_HOME/state/shadow/ledger.jsonl`: time, repo, number, action, tier,
gates passed, reason, head SHA, caller and link, written once per action and
head. Each report digest adds what was held since the last one, and

    python3 -m bus.shadow report --since 24h      # or 7d, or an ISO date or UTC time
    python3 -m bus.shadow status                  # which repos are in shadow

lists each would-action with its link, for the operator's review.

The switch fails closed. `LOOM_SHADOW` turns shadow off only when it reads
`0` (or `false`, `off`, `no`); unset, empty, any other value, or a value that
cannot be read is shadow. A Muse whose environment never names the switch is
in shadow. `LOOM_SHADOW_REPOS` lists the repos still in shadow (`OWNER/NAME`,
space or comma separated); empty, `*`, or an entry that is not `OWNER/NAME`
means every repo. A repo not listed has graduated. The station plist is
written with `LOOM_SHADOW=1` unless the installer is told otherwise. A repo
added to `LOOM_REPOS` after the list was narrowed is not in it: add it to
`LOOM_SHADOW_REPOS` first. The lane machine's tick prompt takes its Shadow
mode sentence from `python3 -m bus.shadow prompt`, the same scope the gates
read, so it names the repos still in shadow and lets the brain merge through
`bus.brain merge` in every other repo (loom#1960).

Graduation is the operator's act, never Loom's. A repo leaves shadow only
when the operator removes it from `LOOM_SHADOW_REPOS` (on the station and on
Muse alike), and only after the operator's own review of the same pull
requests and tickets finds:

- at least N would-merges in the repo (20 for `docs-tests` and `app-code` by
  default), with zero damaging ones: a would-merge the operator did not merge
  as it was, or merged and later reverted;
- zero would-closes of a ticket with an unmet criterion.

Lowest risk tier first: `docs-tests`, then `app-code`; `deploy-config`,
`security` and `cross-repo` stay with the operator (`LOOM_AUTO_MERGE_TIERS`).
A damaging would-merge resets the repo's count. `LOOM_SHADOW=0` ends shadow
for every repo at once.

## Verify before ship

A ticket closes only when every acceptance criterion has evidence
(`bus/criteria_gate.py`). An audit of 2026-10-22 found at least 12 tickets of
the private deployment repo closed as completed on green CI with their live
criteria never checked (a roller "enabled" while paused, a run-rate
"recorded" by a runbook that says the run never happened, a verdict from a
simulated run), and lanes ticking live boxes with nothing running. Each
criterion is one of three kinds:

- code: met by the merged change (a test, a file, a behavior in the code);
  the shipped merge is its evidence.
- live: met only on a running system: a `Live:` line, a host or network
  check, a box that names live, deployed or production behavior, a time
  window or an observed event, an imperative verify or measure, a result
  recorded on the ticket, a round trip, a reboot. Its evidence is a passing
  station check, or, for a `Live:` line with log strings, the station's own
  log search that dispatched the verify run.
- operator: something only a person does (signs off, approves, acts by
  hand, checks a handset). Loom never closes it. A decision box is one: its
  text starts with "Operator decides" or "Decision:", or carries
  `<!-- loom:decision -->`. Only an operator ticks it. When a lane's run
  ends, the station unticks a decision box that was open in the brief and is
  ticked now, unless an operator made the last body edit, and posts one
  incident note naming it. A no-change report on a ticket with an open
  decision box does not move it to `loom:merged`, whatever merged: it goes to
  `needs:human` with a `Kind: decision` ask (loom#2029).

A tick is nobody's evidence: not the ticket's, not the lane's pull request
body's. The station's done close leaves a shipped ticket open while a live
or operator criterion has no evidence. A live criterion that names a check
Loom can run gets one, at most 5 a pass, again after 6 hours when it fails:
an `https://` URL fetched with GET, a `gh api <path>` read through the
station's GitHub client, or a read-only `kubectl get` / `kubectl rollout
status` (never a secret, `exec` or another cluster's credentials). The
criterion's other backticked or double-quoted strings must be in the answer.
Only a 2xx passes a URL check: a redirect is not followed, so a 3xx (a login
or error page behind it) fails as "redirect not followed", unless every
expected string names that exact status or Location.
Each result is kept in the station's `live-checks.json` and written to the
ticket's status; the answer itself is never written. A criterion with no
runnable check, a failed check, or an operator criterion sends the ticket to
`needs:human` with one ask (`Kind: decision`), once, and at most 4 such asks
an hour. A verify lane's proof that passes a live or operator criterion
without station evidence is refused.

The proof a shipped ticket names is read by `bus/live_proof.py`, read-only.
Each live criterion names at most one step: its log strings searched from the
deploy on, once the window it names ("within 24 hours") has passed; a check
the gate runs; or one plain SELECT through the station's query source. A
step that would write is refused and never run: a `gh api` call with a method
or field, a kubectl verb other than get or rollout status, a ship, roll,
restart or any other command, SQL other than one SELECT. When the close gate
finds evidence for every criterion the close quotes it, matching log lines
included; otherwise one status line names the missing criterion and the next
check time. The station's done close runs it for each open shipped ticket
with a live criterion that names log strings and a window ("within 24
hours"), at most 5 a pass, with the loom:shipped label time as the deploy
time; a Live line with log strings and no window keeps its verify run. The
log is the repo's
`LOOM_EVIDENCE_CMDS` command, run once per string; only lines stamped at or
after the deploy count (a line with no time counts only when the whole log
the command read is newer than the deploy). A proven ticket gets a station
proof record (revision: its newest merged pull request; observed: the deploy
to now; each criterion's evidence), moves to verified with verified_by
`station`, and closes through `brain.close` with the evidence quoted. Any
other gets its status line edited into the status comment and is proved
again at the next check time, or once its criteria change when nothing Loom
runs can change the answer. In shadow mode the close is recorded in the
ledger (source `station live proof`) and nothing is sent (#1608).

A repo whose live data sits behind its own read-only routes (a log archive
with per-workload reports, a read-only database user) declares a live-evidence
adapter in `.loom/live-evidence.json` on its default branch
(`bus/live_adapter.py`, #1770). It names a `log` command with the log
`sources` it serves and a `sql` command, each an argv run without a shell:
its first item a bare command on the station's PATH (never a shell or an
interpreter, and its verb a fixed literal) or an executable `.loom/` script
read from the default branch and run from a private temporary copy; every
other item a literal or one whole placeholder (`{source}`, `{since}`,
`{since_epoch}`, `{needle}`; `{sql}`). The command prints matching lines or
rows, and may print `loom-evidence-window-hours: N` (log read in full) and
`loom-evidence-count: N` (matches counted); it exits 0, or 1 for no match.
A Live line names a step in a templated form: `log:<source>` with the strings
that must appear in that log since the ship (or, worded as absent, must not
appear over the window named, which the adapter must say it read), or
`sql:<SELECT ...>` (one SELECT or SHOW) with the strings its answer must
contain. The station runs an adapter only for repos in
`LOOM_LIVE_EVIDENCE_REPOS`; the child gets PATH, HOME, LANG, TMPDIR and only
the variables named in `LOOM_LIVE_EVIDENCE_PASS_ENV`, a timeout of at most
300 seconds that kills its process group, and at most 64 KiB and 200 lines
of output. A
non-LOOM variable it names, such as `KUBECONFIG`, reaches the station only
when `LOOM_STATION_PASS_ENV` names it too: `lanes/mac/install.sh` writes
the variables that setting names into the station plist when they are set
at install time, so a reinstall keeps them. A SQL statement is checked when the step is read and again just
before it runs. Loom bounds what it asks and keeps; the routes behind the
adapter keep themselves read-only (a SELECT-only user, a reader that cannot
exec). An adapter error, a timeout or too much output is no evidence, and the
status line names it.

### The evidence close

By the operator's ruling of 2026-10-31 (loom#2325), one close revises the
rule above, for one kind of ticket. The station closes a `loom:shipped`
ticket with its one Live box unproven when all of these hold:

- that Live box is the ticket's only unticked box;
- it has no regression, urgent, runaway, `needs-brain` or `loom:audit-failed`
  label, no open ask, no `wait:` label, and never closed this way before;
- every linked pull request is merged, green on its merge commit (a neutral
  review check counts, and at least one check succeeded), and on the default
  branch;
- every pull request's tier is below deploy-config;
- each symbol a pull request added is still defined on the default branch,
  and each one it removed is still gone, read from the file itself;
- Loom has no check of its own for the box (a station proof, a runnable check
  or a time window), and the box is known to be meetable
  (`bus/unmeetable.py`).

A doubt refuses: a missing patch, an unreadable file, a failed lookup. A
refused ticket goes to the usual done close and is read again after 6 hours.
Checks still running leave the ticket for a later pass with no ask.

The close carries `closed-unproven` and `force-close`, and is the one close
that passes through shadow. The station names the ticket in code, so no label
or `gh` call can ask for it. Merges stay held. The box is first written into
the repo's one rolling Proof due issue, edited in place, so nothing closes
unlisted. The close makes no AI call.

At most 3 closes and 6 tickets read a pass, and 10 closes a day. Each pass
that reads one logs its counts and these bounds in one `evidence close:` line.
Clearing a stock in bulk is not this close's job.

Reopens stay at or below 5% of the last 100 closes. One listing per repo finds
reopened tickets, and a reopen stays counted after the ticket closes again.
Above the limit this close stops, says so in the log and in each Proof due
issue, and the usual done close takes over. A first reopen in fewer than 20
closes stops it too. The count is a floor: it sees only reopens someone made.

    python3 -m bus.evidence_close status
    python3 -m bus.evidence_close pause
    python3 -m bus.evidence_close resume    # closes before now stop counting

## Post-merge watch

After a Loom auto-merge (a `loom/` lane pull request merged by a Loom App,
not by a person), the station watches its deploy for `LOOM_POST_MERGE_WATCH`
minutes (default 60) and reverts it when the deploy fails
(`bus/deploy_watch.py`). `bus/revert.py` reverts only a red default branch,
and most damage an audit of 2026-10-22 found never turned CI red: a build
workflow that failed on the other build nodes, a config roll that
crash-looped a StatefulSet, a backup hub left unpinned.

Every signal answers healthy, failed or pending, with a reason; the merge is
failed when any signal failed and healthy when all are. A repo names its
deploy workflow in `.loom/deploy-signal` on its default branch, one signal a
line:

```
# .loom/deploy-signal
workflow sync.manifests.yml production/manifests/**
```

The workflow's run for the merge commit is read. A Rollout reports one
annotation with a fixed title: `rollout-healthy` is healthy, and
`rollout-aborted`, `rollout-degraded`, `rollout-paused`, `rollout-timeout`
and `rollout-unreadable` are failed. A workload that is not a Rollout is
judged by the run, which fails when its rollout or readiness wait fails. A
merge that changes nothing under the globs is healthy. The ticket's live
check (`bus/criteria_gate.py`), when it has one, is a signal too: healthy
once it passes, failed if it still fails when the window ends. Another kind
of signal is a new entry in `SIGNAL_KINDS`.

Every read fails closed: an unreadable file, run or annotation list, a
cancelled run, or an unknown signal kind is pending, never healthy. A merge
still pending when the window ends is not reverted; the operator is paged
that Loom could not tell.

A failed merge gets a `loom:revert` ticket and GitHub's own revert pull
request of it. That revert is the only change the watch merges, and only
when `merge_tier.revert_refusal` finds nothing (the station applied the
label, the body names the one reverted commit, which the base carries, and
the diff is its exact inverse) and `bus.brain merge` passes it. A revert
whose checks fail, that is refused, or that has not merged in 60 minutes is
left for the operator. Either way the operator gets one page naming the
failed signal and both pull requests; with no webhook set up, the message
goes on the merged ticket. A revert is never watched, so Loom never reverts
a revert. Each merge's watch is kept in the station's `deploy-watch.json`.

## Lane scorecard

Each lane and model keeps a scorecard (`bus/scorecard.py`), and a lane that
ships bad work is demoted. Once an hour the station reads each repo's newest
closed lane pull requests (a `loom/` branch, a trusted author, and the
runner's footer naming the lane and model) and records per pull request its
review outcome (clean, findings, or bad-work: a finding from
`bus/merge_tier.py`'s bad-work rules), its send-backs (the fix briefs on its
ticket), whether it merged, whether a merged revert undid it, and whether the
post-merge watch found its deploy failed. Failed proof audits count against
the lane that made the proof. The 2026-10-22 audit's verdicts seed the
scorecard, aggregated per lane and model.

A bad outcome is a bad-work finding, a revert, a failed deploy, a failed
proof audit or a damaging audit verdict. A lane (all its slots) whose bad
outcomes over the last `LOOM_LANE_DEMOTE_DAYS` days (default 7) reach
`LOOM_LANE_DEMOTE_AT` (default 3) is demoted: it claims no build ticket, its
briefed tickets step aside to other lanes with a status event naming the
demotion and its counts, and it stays demoted until the operator runs
`python3 -m bus.scorecard clear <lane>` (`show` prints the table). Clearing
forgets the outcomes before it. A demoted lane still takes verify runs. A
repo that cannot be read keeps its records, and nothing is cleared by an
error. The table goes on the lane status comment, and the dashboard's Lanes
page shows it as Scorecard.

Each merged record also gets a settled difficulty (`bus/difficulty.py`),
scored blind from the merged work alone: the diff's changed lines and files,
the fix rounds sent back and the review findings, never the ticket's
`Difficulty:` line. The perceived difficulty on that line is kept beside it
and never changed. The lane status comment compares the two per lane, model
and kind (the change class) over the last 14 days: how many agree, how many
were harder or easier, and the bias in tiers (positive: harder than the
brief said), so routing can see where its guess is off.

## Pull requests without a ticket

A pull request with no ticket (no `Refs` line and no `loom/` branch) that a
`LOOM_BRIEF_AUTHORS` login opened is not a lane pull request: no ticket
moves and nothing briefs it. A brain is allowed to write one while
shipping, and it must not be left for the operator. Such a fix either goes
through `brief` like any other work, or is merged with `--adopt`.

`python3 -m bus.brain merge OWNER/NAME#PR --adopt` squash-merges a
ticketless pull request when all of these hold:

- its author is in `LOOM_BRIEF_AUTHORS`;
- its head and base are both in the same repo, so a fork is refused;
- it is not a draft, and it has no ticket (a linked ticket is refused:
  merge that without `--adopt`);
- its head commit's check rollup is `SUCCESS`;
- the review check (`LOOM_REVIEW_CHECK`, else `Grug - Elder`) completed
  with `success` on the head commit, or `neutral` for a `class:docs` or
  `class:tests` pull request only. A logic, sensitive or unlabeled pull
  request requires `success`;
- every review thread is resolved.

It pins the head SHA in the squash merge, deletes the branch only after
GitHub confirms the merge, and comments on the pull request that it was
adopted.

`python3 -m bus.brain status` lists these under "Pull requests without a
ticket", one line per open pull request from a trusted author with no
ticket, ending in `READY` or `blocked:` and every blocker exactly as a lane
pull request's line, so a brain sees them and adopts them.

The station adopts the rest (decision 0002, item 1). Each pass, for the
oldest open pull request in a scanned repo that is not a draft, comes from
a `LOOM_BRIEF_AUTHORS` login on a branch of the repo itself, opened at
least 48 hours ago (`ADOPT_GRACE`; one with no creation time counts as old
enough), quiet for that long as well when its branch is outside `loom/`
(no commit or comment, read from `updated_at`), and that no open ticket
links (by a `Refs` line or a `loom/N` branch), it re-reads the pull request and, if it is still open and not
merged, files one
ticket under the repo's goal home: "Land or supersede #N", in the normal
brief format with `Difficulty: medium`. The lane that takes it lands the
change on a `loom/` branch and closes the original with a comment naming
the new pull request, or closes the original as superseded with the reason.
The station marks the original with a `loom:adopted` comment naming the
ticket, so it is adopted once, and files at most one adoption ticket per
repo a pass. It never closes a pull request itself. It reads a pull
request's comments for the marker at most once, remembering the adopted ones
in `state/station/adoptions.json`, and a repo whose brief was refused (its
goal at the cap) is tried again 10 minutes later, not every pass. The station
log says `adopted OWNER/NAME#N as ticket ...` for each one, and
`adoption OWNER/NAME:` with the count of open pull requests no ticket links
whenever it changes. The grace exists because most pull requests are being
driven by a person or a session and merge within two hours: over a week in
October 2026, 49 of 64 adoption tickets were filed within an hour of the
pull request opening, and 8 at or after its merge. A pull request closed or
merged since the pass read the open list is logged as `adoption
OWNER/NAME#N: closed or merged since the pass began, not adopted` and
remembered, so it gets no ticket and no marker.

## Journal

One journal per tick that changed something, on the goal's home issue. Each
brain keeps one journal comment per goal home per UTC day: `bus journal`
posts the day's first tick and edits that comment for every later tick, the
new tick at the top and the earlier ticks of the day under a
`<details><summary>Earlier ticks today</summary>` block, newest first,
dropping the oldest past 60000 characters. A new comment emails the operator
and an edit does not. `bus journal --holder NAME` also takes a file with no
marker, the brain's evidence or a note for that issue, and journals it as a
tick the same way, and it replaces a hand-written tick header after the
marker (`Tick 2026-10-20 23:07Z, muse`) with the canonical line instead of
stacking two. Readers split an edited journal back into its ticks,
each at the time on its Tick line (`bus.handoff.expand_journals`). Each tick
looks like this:

```markdown
<!-- loom:journal v1 -->
Tick <UTC time>, <holder>
- Measure: <the goal's metrics read this tick, or none>
- Shipped: <links or none>
- Briefed: <links with lanes or none>
- Waiting: <what is in flight and on whom>
- Asks: <links or none>
- Friction: <what slowed the factory this tick, or none>
- Cost: <GitHub requests>, <minutes>
```

`- Friction:` is the canonical line for what slowed the factory (docs/MUSE.md,
Improving Loom itself): one line per slowdown, each its own `- Friction:`
line. The dashboard's Improving page reads it, and still reads the older
headings brains wrote (`What slowed the factory`, `Factory note(s)`,
`Improving Loom itself`, `Blocker(s)`, a heading line with a list under it),
but a new journal writes `- Friction:`.

A step that failed or ran degraded adds a line `- Step <name>: failed <why>`
or `- Step <name>: degraded <why>` (the step names are in docs/MUSE.md); a
brain whose two newest ticks both carry one is an invariant breach
(docs/LANES.md, Invariants).

A journal may also carry handoff lines (docs/MUSE.md, Handing off stage
work), each an HTML comment:

```markdown
<!-- loom:defer v1 stage=merge item=<owner/name>#<number> rev=<revision> holder=<name> -->
<!-- loom:done v1 stage=merge item=<owner/name>#<number> holder=<name> -->
```

`python3 -m bus.brain defer` and `done` print them; the revision is the item
when deferred (a pull request's head commit, an issue's state and comment
count). A written brief records its own done marker: `python3 -m bus.brain
brief OWNER/NAME --epic E ... --holder NAME`, after it files the ticket under
an epic of a goal whose newest brief marker is a defer, posts a journal on that goal home
with the `stage=brief` done line and the new ticket. A brief without
`--holder`, on a goal with no standing brief defer, or refused, posts no
journal and changes no marker; so does a `--regresses` brief, which is
the `regression` stage. Only journals from `LOOM_BRIEF_AUTHORS` logins
count, matched without regard to case, and a GitHub App counts by its REST
login `<app-slug>[bot]` (the status query asks for the author type to read
it that way). `status` and `resume` refuse to run while the setting is
empty, since they would report no handoffs and every stage open; both print
the logins they trust first, and `resume` names, per goal, the logins whose
journals it skipped. Set it on every brain to the operator's login and the
Loom App's bot login. For each stage and item the newest line wins. Brief defers carry a
stable key derived from their normalized note, so one goal can hold multiple
independent brief handoffs. A written brief can name the one it finishes with
`--resolves KEY`; a keyless done marker retires only legacy unkeyed brief
defers. `python3 -m bus.brain resume OWNER/NAME... --holder NAME` lists the
defers still standing whose item is open and
unchanged, and refuses unless that holder holds the tick lock.

A repo or handoff item GitHub will not show (HTTP 301 after a rename or
transfer, 404 or 410, a 403 that is not a rate limit, a GraphQL NOT_FOUND)
does not end `status` or `resume`: each prints `skipped <repo>: <reason>`,
in the output and on stderr, and finishes the other repos. `resume` lists
such repos under "Skipped repos (fix LOOM_REPOS)". Any other GitHub error
(5xx, a rate limit, a dropped connection) still ends the command, so a
fault never reads as an empty repo.

`status` and `resume` print as they go (loom#2049): the heading first, then
each repo's section as soon as that repo's reads finish (in the order the
repos finish), then the sections that span every repo (Runaway tickets,
Priorities, Waiting on you and the rest for `status`; Sensitive pull
requests, Finished epics, Verify due and Lane proof audit for `resume`).
Each repo's reads have a budget, `--repo-budget SECONDS` or
`LOOM_REPO_BUDGET`, 120 seconds by default. A repo past it, or one whose
connection failed, is read once more with a fresh budget, keeping the answers
the first try finished. A repo that fails again prints `skipped <repo>:
timed out after ...` or `skipped <repo>: connection failed twice: ...` and is
left out of the cross-repo sections, and the run goes on with the other
repos. Within a repo, the
reads per pull request, per stuck ticket and per epic run side by side, so
one repo's status takes about a third of the time it did when they ran one
after another.

`resume` also ends with a "Verify due" section: up to 5 open `loom:shipped`
tickets across the repos, oldest ship first, each with how long ago it
shipped and its Live criterion (the acceptance line that starts with
"Live"). `python3 -m bus.brain status` prints the same list in full under
"### Verify due" for each repo. The hourly lane-machine brain (scope live)
spends up to a third of its tick on these items: it writes a proof only from
evidence it saw in the tick, and otherwise leaves a one-line note on the
ticket naming what evidence is missing.

Twice a day, for the operator's reading times (10:00 and 21:00 New York),
the tick adds a short report with the marker `<!-- loom:report v1 -->`: its
first line and at most five more (progress on the goal's measure, shipped,
in flight, blocked, next), one line each; `journal` refuses a longer one. A
goal quiet since its last report gets none (Quiet goals, below), and a
per-repo backlog goal never gets one: its status is one line in the
station's twice-daily Discord digest, and `journal` refuses a report there. The report ends with two lines: "Needs you:" (only what only
the operator can do for this goal, or "nothing new for this goal"; it alone
decides new comment or edit in place) and "Waiting on you:" (the count of
open `needs:human` issues by repo and this goal's share, or "nothing"). A goal
home known to be private names every repo; any other home names only public
repos and folds the rest into one "N in private repos" part, and a repo whose
visibility cannot be read counts as private. `status`
prints each goal's "Waiting on you:" line under "## Waiting on you", the
station's data-only report carries the same line (left out when a repo cannot
be counted, and a count that moved alone edits the last report); `journal`
refuses a report that puts the count on the "Needs you:" line or says
"nothing" beside a count (loom#1775).

### Handoff packet

Each pass the station keeps one comment on the Loom goal home (the status
repo's one open goal home that is not a backlog), marked
`<!-- loom:handoff v1 -->`. It posts it once and then edits it in place, and
only when its text changed. It rebuilds the packet at most every 10 minutes,
or at once when a brain tick on the lane machine just ended (`state/brain.json`
changed), and reads again only the goal homes whose update time or comment
count changed since the last build. It holds the last 5 ticks across the goal homes
(holder, model from a `- Model:` journal line, done and deferred counts, one
line decided from a `- Decided:` line or the first line of prose, and the
items left open), a tick that holds the lock with no journal yet, the
standing defers (the newest marker for a stage and item wins, as in
`resume`), with each defer whose brain has not ticked in 2 hours marked open
work for any brain, and the Rules section of docs/MUSE.md read from GitHub at
the station checkout's commit. Journals from private repos count, but the
packet names neither their repo nor their text.

When the dashboard's brain memory answers (`GET /api/brain/handoff` with the
transcript upload token, docs/MUSE.md, Handing off stage work), the station
renders the packet from it instead: the last 5 ticks posted to brain_ticks
(holder, lane, model, start, end, outcome, and how many items each decided,
left open and handed off), the open commitments (stage, item, the tick that
opened it, due), each rules digest source with its commit or comment, and the
same Rules section from GitHub. A row's text is not quoted there, since a row
does not say which goal home it came from; `python3 -m bus.brain_memory
handoff` prints it in full. A private repo's item or goal home is shown as
"issue N of a private repo". When the dashboard is not set, does not answer,
refuses the token, or holds no tick yet, the packet is built from the goal
homes as above.

### Report deadlines

Each goal owes its own report at the next reading time (10:00 or 21:00
New York, brain.REPORT_TIMES, the station's digest times) after its newest
valid report or, with none, after the goal was opened; a report written in
the 2 hours 15 minutes before a reading time counts for it. A backlog goal
owes none. A valid report is a
comment on that goal's home that starts with the report marker, from a
`LOOM_BRIEF_AUTHORS` login when that is set. A marker inside another
comment, a journal, or a report on another goal (a parent goal included)
does not move the deadline: a parent's report never covers its child goals.

`python3 -m bus.brain status` shows, per goal, when it last had a journal, a
valid report, a measurement (a journal or report with a `Measure:` line that
is not none) and a completed action (a `Shipped:` line that is not none),
and its report state: ok, due (from 2 hours 15 minutes before its reading
time), or overdue (30 minutes past the reading time). Due and overdue goals are listed under "Reports
owed before unlock"; the tick posts those reports before it unlocks. A due
or overdue goal that is quiet (below) is not listed there.

Two reports on one goal home stay at least 90 minutes apart (the
`report-cadence` invariant). A due or overdue goal with a report under 90
minutes old, from a `LOOM_BRIEF_AUTHORS` login or any GitHub App (the
station's standup, posted as the Loom App, whether or not the brain trusts
that login), is listed as `report HELD` with that report's author, time and
when the next may go out. `python3 -m bus journal` refuses such a report
the same way: it posts nothing, prints `not posted:` and the same reason,
and exits 0. With `LOOM_BRIEF_AUTHORS` empty every author's report counts;
otherwise a person outside the list cannot hold a goal's reports back. An
overdue report is a missed deadline only: it is separate from a report the
station could not forward and from a brain that stopped ticking, which the
station reports on its own.

### Quiet goals

Every new comment on a goal home emails the goal's author, so a goal where
nothing happened is owed no report (loom#1534: a goal home got "nothing
moved, nothing needs you" by email every few hours). `bus/quiet.py` holds the
rule, and it reads only data `status` already has, never a report's prose.
A goal is quiet since its newest valid report when all of these hold:

- no ticket in its scope (the tickets `goal_tree` walks) was opened or
  closed after that report, and none carrying `loom:merged`, `loom:shipped`,
  `loom:verified` or `loom:failed` was updated after it;
- no open `loom:invariant` issue names the goal (`Part of #N`) or sits in
  its scope;
- no open ticket in its scope asks the operator (`needs:human`,
  `needs:human`);
- nothing in its scope is stuck: no open ticket carries `loom:failed`,
  `loom:runaway`, `loom:regression` or `needs-brain`, and none has sat in
  `loom:pr` past the stuck age (6 hours);
- the numbers on its newest `Measure:` line (journal ticks and reports, with
  dates and clock times left out) are the numbers it had when that report
  went out.

A goal with no report yet is never quiet. `status` prints a quiet goal's
`quiet since <time>` line under it and leaves it out of "Reports owed before
unlock". Once its last report was touched (posted or edited) 7 days ago, it
is listed there as `report HEARTBEAT`: the next report is that week's
heartbeat, and `python3 -m bus journal` edits it into this login's newest
report on the goal home, with a `Still quiet as of <time>` line, instead of
posting a new comment (an edit sends no email). Any other report for a quiet
goal is refused like one inside 90 minutes: `journal` posts nothing, prints
`not posted: quiet since <time>` and exits 0. Refusing, rather than editing
the last report each time, keeps the last report saying what it said when it
went out, and needs no write at all; a brain cannot edit another login's
report anyway.

The station follows the same rule. It posts no standup for a quiet goal and
reads it again an hour later (`QUIET_RECHECK`); its weekly heartbeat edits
its own last standup in place and, with none on the goal home, posts
nothing. Its push of reports to the operator's phone, each report on its own
or in the digest, skips a report whose goal was quiet since the report
before it (counted quiet in the digest); a heartbeat is an edit, never a new
comment, so it is never pushed. A read that fails pushes the report. The
daily Visibility number counts a quiet goal as current: its last report is
owed no successor.
