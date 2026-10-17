# Themed epics for every child of the Loom goal home (#30)

Research for #621 on the map #620. Data read 2026-10-17 from the GitHub API: the sub-issues of #30 and one search for issues whose body has a line starting `Part of #30`. Pull requests are excluded. Nothing was moved, closed or created.

## Answer

#30 has 235 children: 164 open and 71 closed. 70 are sub-issues only, 135 carry only a `Part of #30` line, and 30 have both.

The settled caps cannot hold every open child today. Six live epics of 15 hold at most 90 tickets, and 164 tickets are open. Two cleanups the repo's own rules already call for bring that to 127:

- 32 open tickets carry `loom:verified`. docs/SDLC.md says a verified ticket is closed, and `bus/brain.py` closes as completed only a verified ticket, so these are finished work that was never closed. They go to history epics H7 to H9 once closed.
- 4 older tickets are superseded by a newer ticket with the same scope: #36 by #574, #39 by #575, #65 by #580, #77 by #582. They close as duplicates into the same history epics.
- #613 is already an epic (dashboard pages, done-when written, children #607 to #612). It becomes live epic E1 rather than a member ticket.

The remaining 127 open tickets need at least 9 epics of 15. The proposal has 6 live epics holding 78 tickets, every queued, briefed, running, pr or merged ticket among them, and 4 waiting epics holding 49 tickets. 34 of the waiting tickets are already shipped and await evidence, so verification alone empties most of them.

Recommended answer for the operator: approve the cleanups, file the 6 live epics now, and file each waiting epic only when a live one closes. Until then a waiting ticket keeps its `Part of #30` line, and this file is its record. The alternative is a one-time exception that lets 10 epics stay open until the count falls under the cap.

History epics: 9, holding the 71 closed children plus the 36 to be closed, filed and closed at once.

## Method

- `gh api repos/quadseven/loom/issues/30/sub_issues --paginate`: 100 sub-issues (the GitHub cap).
- `gh api "search/issues?q=repo:quadseven/loom+\"Part of #30\"" --paginate`: 361 hits, 150 of them pull requests. 165 issues have a line matching `^Part of #30` (not #300 and up). Of the other 46 issues, 44 are sub-issues that mention the goal mid-line, and the last two are #30 itself and #621.
- State is the issue state plus its `loom:` labels, with `loom:ticket` left out. The theme is the epic's short theme.
- `git log origin/main` shows merged commits that reference #410, #412, #414, #417, #420, #422, #426, #429 and #431, which carry no state label. They sit in live epics until a verify run settles them.
- Titles that name another repo or contain a word this public repo avoids are shown as a neutral theme.

## Live epics (open now, at most 6)

### E1. Dashboard pages each answer one question (15 tickets)

Adopts the existing epic #613 as this epic, and its open children are the first six members.

Done when: Every page loads fresh data with no unexplained missing text, fills a widescreen and fits a phone, checked in a browser after each member ships.

| # | Title | State | Theme |
|---|---|---|---|
| #607 | Backlog shows epics only as one dense table, and lists every ticket outside an epic as an incoherence | open queued | dashboard pages |
| #608 | Tickets is a dense table grouped by goal, in-flight by default, with the model each run used | open queued | dashboard pages |
| #609 | Small goals roll up into their parent goal, and goal cards show real numbers instead of placeholders | open queued | dashboard pages |
| #610 | Every stale banner and empty panel says what is missing, why, and when it refreshes, in plain words | open briefed | dashboard pages |
| #611 | Lanes tier text comes from the factory tables, and Usage shows each lane's real limit state in local time | open running | dashboard pages |
| #612 | A merged dashboard change deploys itself through a pin-bump pull request | open queued | dashboard pages |
| #410 | The Ticket page shows its real path, pull request and typed transcript, as the design does | open | dashboard pages |
| #412 | The brain's tick steps are recorded, and the Brain and Hot spots screens match the design | open | dashboard pages |
| #414 | The Improving, Lanes and Needs you screens match the design and show real reasons | open | dashboard pages |
| #417 | Floor and Goals read like the design and render in milliseconds | open | dashboard pages |
| #420 | The Floor shows the newest journal's lead when it names no next move | open | dashboard pages |
| #426 | Brain and Hot spots section labels read at label size | open | dashboard pages |
| #574 | Store lane run transcripts in Postgres and display on phone dashboard | open shipped | dashboard pages |
| #295 | The Improving and Lanes and models views | open shipped,ready | dashboard pages |
| #408 | Every link and control on the phone layout is a 44px target, and the shell checks run in CI | open shipped | dashboard pages |

### E2. Brain ticks, handoffs and status (13 tickets)

Done when: A brain tick runs on a timer from one handoff packet, records its steps and state where any reader sees them, and fits its time budget.

| # | Title | State | Theme |
|---|---|---|---|
| #582 | Timer-driven brain tick via launchd with self-resolving operator asks | open queued | brain operation |
| #422 | The brain's status reads repos side by side and fits a tick again | open | brain operation |
| #442 | A brief one brain names but cannot write is handed to the next brain | open shipped,awaiting-evidence | brain operation |
| #518 | Resume drops a defer whose item has a done on any goal | open shipped,awaiting-evidence | brain operation |
| #522 | A brief written from a resume brief item records its own done marker | open shipped,awaiting-evidence | brain operation |
| #586 | Brief defers on one goal each keep their own marker, so an unrelated brief done does not drop them | open merged | brain operation |
| #589 | The brain tick floats to the first lane with usage instead of only the claude CLI | open merged | brain operation |
| #590 | Brain ticks, commitments and rules live in Postgres through the dashboard | open pr,ready | brain operation |
| #591 | Every brain starts its tick from one handoff packet on the goal home | open queued | brain operation |
| #439 | The brain can rank an existing ticket, so the Priorities list is not all P2 | open shipped,awaiting-evidence | brain operation |
| #431 | Loom ranks work by priority and turns live health findings into tickets | open | brain operation |
| #304 | The brain can send a review to a lane while its round is still running | open shipped,ready,awaiting-evidence | brain operation |
| #103 | Transfer overdue stage work even while another brain is journaling | open shipped | brain operation |

### E3. Lane runs and their pull requests recover on their own (15 tickets)

Done when: A failed, empty, stray or orphaned lane run is classified correctly and moves to a fix, a live lane or the ladder with no brain step.

| # | Title | State | Theme |
|---|---|---|---|
| #171 | The factory sends back a lane pull request that changes code without a test | open shipped | lane failures and fix rounds |
| #367 | Lanes: defer minor findings at once and tell agents to work test-first | open shipped | lane failures and fix rounds |
| #379 | Fix briefs quote the lines that say why a check failed, not post-job cleanup | open shipped | lane failures and fix rounds |
| #381 | A fix round that fails its work climbs the ladder instead of looping on one lane | open shipped | lane failures and fix rounds |
| #498 | A lane run whose agent committed its own work is pushed, not failed | open shipped | lane failures and fix rounds |
| #500 | A run that correctly finds nothing to change is an answer, not a failure | open shipped,awaiting-evidence | lane failures and fix rounds |
| #517 | Validation-environment failures are classified as factory incidents, not ticket work failures | open shipped | lane failures and fix rounds |
| #519 | Escalation skips a pull request with nothing to fix and never re-picks a model the ticket already ran | open shipped,awaiting-evidence | lane failures and fix rounds |
| #261 | Escalation counts only work failures, and a failed PR create is transient | open shipped,ready,awaiting-evidence | lane failures and fix rounds |
| #499 | A runaway ticket is flagged at its second same failure and held for the brain to review | open shipped,awaiting-evidence | lane failures and fix rounds |
| #572 | Runaway detector must not count transient/limit infra exits as work attempts | open queued | lane failures and fix rounds |
| #490 | Every lane knows its repo's checks: a contract for each repo, and CI's run lines until then | open shipped,awaiting-evidence | lane failures and fix rounds |
| #521 | A lane run whose agent commits files outside its ticket is refused before the push | open shipped | lane failures and fix rounds |
| #538 | A running ticket on a lane with no heartbeat for 30 minutes moves to a live lane | open running,runaway | lane failures and fix rounds |
| #588 | The station adopts open pull requests that no ticket links, so none sits without an owner | open queued | lane failures and fix rounds |

### E4. Lane machine, sandbox and remote lanes (11 tickets)

Done when: Every lane, local or remote, runs contained to its provider and tree, survives a reinstall, and pulls its brief without a duplicate run.

| # | Title | State | Theme |
|---|---|---|---|
| #20 | Run API-lane agents as a separate macOS user | open human | lane isolation and remote lanes |
| #575 | Restrict API-lane agent traffic and writes to provider and tree | open queued | lane isolation and remote lanes |
| #580 | Reinstalling lanes mid-brief orphans the agent and starts a second one on the same ticket | open queued | lane isolation and remote lanes |
| #584 | Add timeout to subprocess call in bus/station.py | open queued | lane isolation and remote lanes |
| #124 | The agy lane can run commands inside its sandbox, so it can run the tests its briefs ask for | open shipped | lane isolation and remote lanes |
| #46 | Muse brain ticks: its schedule, the tick order, the lock and budget | open triage | lane isolation and remote lanes |
| #49 | Muse: standing instructions, GitHub access scope and tailnet proxy | open human | lane isolation and remote lanes |
| #55 | Lane runners pull their briefs from GitHub | open triage | lane isolation and remote lanes |
| #473 | The remote brain reads logs and rows through a read-only evidence endpoint | open shipped,awaiting-evidence | lane isolation and remote lanes |
| #190 | One slot can run opencode 2.0, and its transcripts still show | open shipped,ready,awaiting-evidence | lane isolation and remote lanes |
| #204 | An opencode 2.0 slot reaches its own server inside the sandbox | open shipped,ready,awaiting-evidence | lane isolation and remote lanes |

### E5. Ship, deploy and verify close the loop (12 tickets)

Done when: Every merged change deploys through its pin pull request and every shipped ticket is verified or sent back within a day, with no hand step.

| # | Title | State | Theme |
|---|---|---|---|
| #263 | Status and resume list shipped tickets due for verification, and the hourly brain spends a share on them | open shipped,ready,awaiting-evidence | ship and verify |
| #436 | Free lane slots verify shipped tickets, and the station applies the proof | open shipped,awaiting-evidence | ship and verify |
| #461 | A verify brief quotes the log lines its live criterion names, read by the station | open shipped,awaiting-evidence | ship and verify |
| #602 | Verify briefs carry the GitHub facts a verifier needs, since verify lanes have no network | open merged | ship and verify |
| #585 | A Health ticket whose finding stays absent 24 hours after its ship is queued for verification | open queued | ship and verify |
| #484 | A health finding that outlives its ticket's ship re-briefs the ticket as a regression | open shipped | ship and verify |
| #486 | A ticket whose merged pull request is reverted by hand goes back for a fresh attempt | open shipped | ship and verify |
| #456 | The station opens the dashboard's pin pull request when Loom main moves | open shipped,awaiting-evidence | ship and verify |
| #528 | A merged module change gets its source-pin pull request from the station, so a roll never waits hours for a brain | open shipped,awaiting-evidence | ship and verify |
| #506 | The station polls the family gate and merges or alerts held roll pin PRs in the window | open briefed,regression | ship and verify |
| #383 | Main's test job gets room to finish, so a slow runner never holds the deploy | open shipped | ship and verify |
| #433 | A new dashboard pod is ready within two minutes, so deploys stop reporting false failures | open shipped,regression,awaiting-evidence | ship and verify |

### E6. Goals, limits and factory invariants (12 tickets)

Done when: Goals link by Part-of line, name their trunk and parent, enforce capacity and unproven-work limits, and every breach becomes a self-closing invariant.

| # | Title | State | Theme |
|---|---|---|---|
| #37 | Goal registration: the loom:goal label and the ordering file | open triage | goals and invariants |
| #47 | Ticket state labels and the goal journal | open triage | goals and invariants |
| #510 | Goal linking survives the sub-issue cap: Part-of line is canonical | open shipped,awaiting-evidence | goals and invariants |
| #617 | Small goal homes name their repo's main goal home as their parent goal | open queued | goals and invariants |
| #230 | A goal home can name its trunk, and every brief and PR in that repo uses it | open shipped,ready | goals and invariants |
| #483 | A goal with too much unproven work finishes it before Loom starts more | open shipped,runaway,awaiting-evidence | goals and invariants |
| #102 | Reserve goal capacity for a verified live regression | open queued,regression | goals and invariants |
| #109 | Track report and action deadlines separately for every goal | open shipped,audit-failed,awaiting-evidence | goals and invariants |
| #354 | Goal home #30 has 100 sub-issues | open rewritten,invariant,recurring | goals and invariants |
| #441 | An invariant that cleared closes itself; one that keeps breaking stays as recurring | open shipped,awaiting-evidence | goals and invariants |
| #564 | A gate that holds briefed work for an hour becomes an invariant and a standup line | open shipped | goals and invariants |
| #593 | A failure that repeats for an hour with no success becomes an invariant, from a structured event log | open queued | goals and invariants |

## Waiting epics (filed when a live epic closes)

### W1. Merge gate and review hand-back agree (14 tickets)

Done when: The merge gate, the READY verdict and the brain agree on every lane pull request, and a red main is fixed forward or reverted automatically.

| # | Title | State | Theme |
|---|---|---|---|
| #94 | The factory loop: review-bot findings go straight back to the authoring lane, clean PRs are marked ready | open shipped | merge gate |
| #108 | Persist a disposition for every pre-roll finding before handing off a ship | open shipped,ready | merge gate |
| #111 | Validate the PR-to-ticket association before any merge write | open shipped,audit-failed | merge gate |
| #198 | Settle the review findings left from PR #117 | open shipped,audit-failed,awaiting-evidence | merge gate |
| #225 | A brain merges a green, reviewed pull request a trusted author opened without a ticket | open shipped,ready | merge gate |
| #229 | A red default branch after a factory merge is bisected and reverted automatically | open shipped | merge gate |
| #232 | A leak-scan failure in a lane PR's text is fixed in the text, not sent back as code | open shipped,ready | merge gate |
| #388 | A sensitive pull request's conflicts and failures go back to its lane; only its merge waits for the brain | open shipped | merge gate |
| #449 | A red main is fixed forward by the merging lane first, and reverted only if that fails | open shipped,awaiting-evidence | merge gate |
| #509 | A READY verdict and the merge gate agree on a neutral Elder review | open shipped,awaiting-evidence | merge gate |
| #576 | A PR linked to a queued or briefed ticket moves the ticket to pr so the merge path sees it | open shipped | merge gate |
| #578 | READY lane pull requests the brain cannot merge: merge gate and status verdict disagree | open shipped | merge gate |
| #397 | A brain merge waits while the ticket's brain fix has not run | open shipped | merge gate |
| #363 | Brain resume hands every ready sensitive pull request older than 2 hours to the brain | open shipped,ready,awaiting-evidence | merge gate |

### W2. Lane providers, limits and routing (12 tickets)

Done when: Every provider's limit is detected and routed around within its tier, and each candidate provider is added or declined.

| # | Title | State | Theme |
|---|---|---|---|
| #34 | How each lane detects and reports its limit, and how routing reacts | open triage | providers and routing |
| #40 | Contained assay runs for the Sparks coding-model candidates | open triage | providers and routing |
| #44 | Bedrock as paid overflow: spend cap and when to use it | open human | providers and routing |
| #45 | Muse Spark through the opencode-go lane for public-repo tickets | open triage | providers and routing |
| #54 | Add GitHub Copilot as a lane | open human | providers and routing |
| #209 | An empty API credit is a lasting limit, and nothing is briefed onto it | open shipped,ready,awaiting-evidence | providers and routing |
| #271 | A ticket whose whole tier is at a limit falls back to the next tier with a free lane | open shipped,ready,awaiting-evidence | providers and routing |
| #203 | A finished spark run never ends on a vLLM 400 | open shipped,ready,awaiting-evidence | providers and routing |
| #259 | A ticket nobody claimed moves within its tier and never climbs the ladder | open shipped,ready,awaiting-evidence | providers and routing |
| #495 | A ladder model name runs on agy with its effort, or is refused before the run | open shipped,awaiting-evidence | providers and routing |
| #565 | A runaway caused by one dead lane moves to a live lane without waiting for a brain | open shipped | providers and routing |
| #125 | Every lane has the operator's skills, not only the claude and codex lanes | open shipped | providers and routing |

### W3. Intake, reports and operator asks (15 tickets)

Done when: Every backlog issue is triaged and rewritten without a brain, and the operator gets one standup, one digest and one reply channel, each posted once.

| # | Title | State | Theme |
|---|---|---|---|
| #224 | Triaged issues are rewritten into tickets by a configured agent, without a brain tick | open shipped,awaiting-evidence | intake and reports |
| #265 | The triage rewriter has a sandboxed opencode command to run, with its key from SSM and every tool denied | open shipped,ready,awaiting-evidence | intake and reports |
| #440 | The triage rewriter falls back across free models instead of failing on a rate limit | open shipped,awaiting-evidence | intake and reports |
| #488 | Private triage issues are rewritten by a lane CLI that already sees private code | open shipped,awaiting-evidence | intake and reports |
| #524 | Goal repos with no Backlog home still get their loose issues triaged | open shipped | intake and reports |
| #48 | A Loom Discord webhook for asks and urgent alerts | open human | intake and reports |
| #110 | Make the daily operator digest cover every trusted ask | open shipped,ready | intake and reports |
| #129 | Talk to the brain from Discord: operator messages become goal notes, brain replies come back | open shipped | intake and reports |
| #150 | Human-only work collects in the daily digest, never one message per issue | open shipped,ready | intake and reports |
| #192 | The station posts the 2-hour standup when no brain did | open shipped,ready,awaiting-evidence | intake and reports |
| #200 | A report goes to Discord once, even when the station restarts mid-pass | open shipped,awaiting-evidence | intake and reports |
| #393 | The station's standup goes out before a goal's report turns two hours old | open shipped | intake and reports |
| #523 | The daily metrics line names the tickets and PRs behind each failing count | open shipped,awaiting-evidence | intake and reports |
| #395 | The quality number asks for a test only when a fix changes code, as the factory does | open shipped | intake and reports |
| #429 | The README explains Loom plainly, with a table of terms and no flow chart | open | intake and reports |

### W4. Open health findings (8 tickets)

Done when: Each finding has cleared and closed itself or been turned into a ticket under a live epic.

| # | Title | State | Theme |
|---|---|---|---|
| #459 | Remote lane muse has not posted a heartbeat for 2.1 hours | open triage,invariant,recurring | health findings |
| #479 | Lane workersai: 5 runs in 24 hours and none done | open triage,invariant | health findings |
| #537 | Health finding: a lane pull request held by its review check | open triage,invariant | health findings |
| #540 | Brain muse reported a failed or degraded step two ticks in a row | open triage,invariant | health findings |
| #557 | Lane spark: 6 runs in 24 hours and none done | open triage,invariant | health findings |
| #561 | Health finding: a lane pull request held by its review check | open triage,invariant | health findings |
| #562 | Health finding: a lane pull request held by its review check | open triage,invariant | health findings |
| #566 | Health finding: a lane pull request held by its review check | open triage,invariant | health findings |

## History epics (filed and closed at once)

H7 to H9 hold open tickets that are verified or superseded. They close first, then go into the epic.

### H7. History: dashboard views and transcripts (verified) (12 tickets)

Done when: every member is closed (true when filed).

| # | Title | State | Theme |
|---|---|---|---|
| #281 | The dashboard package serves ticket states across every repo from shareable URLs | open verified,ready | dashboard views |
| #284 | A public explainer page with no live or private data | open verified | dashboard views |
| #287 | The dashboard shell, tokens and one small script every screen shares | open verified,ready | dashboard views |
| #291 | The Ticket page: path, every attempt's transcript and the pull request | open verified | dashboard views |
| #292 | The Floor, Goals and Tickets views, with a backlog step before queued | open verified,ready | dashboard views |
| #293 | Hunt's backlog inside the dashboard, and one Needs you queue | open verified,ready | dashboard views |
| #294 | The Brain and Hot spots views | open verified,ready | dashboard views |
| #296 | One Usage page for every subscription's limits | open verified,ready | dashboard views |
| #341 | The Lanes and Improving screens show the data they read | open verified,ready | dashboard views |
| #353 | The Floor reads its standup, waits, journal and asks from the goal homes | open verified | dashboard views |
| #362 | Lane cards show the heartbeat time, the lane's model and its quota | open verified | dashboard views |
| #36 | Transcripts in Postgres, shown on the dashboard, reachable from the phone (duplicate of #574) | open rewritten | dashboard views |

### H8. History: lanes, routing and sandbox (verified) (11 tickets)

Done when: every member is closed (true when filed).

| # | Title | State | Theme |
|---|---|---|---|
| #222 | The station gives each queued ticket a lane of its tier when one is free | open verified | lanes and routing |
| #236 | An empty API credit holds every slot that shares it, not only the slot that hit it | open verified | lanes and routing |
| #278 | A remote pull lane: Muse claims lane tickets from GitHub and works them on its own VM | open verified | lanes and routing |
| #279 | The station never claims a ticket for a lane that cannot run its Model line | open verified,ready | lanes and routing |
| #289 | Usage limits for every lane provider, collected on the lane machine | open verified | lanes and routing |
| #310 | A lane falls back to its next model when one model hits its quota | open verified | lanes and routing |
| #319 | The brain can move a queued ticket to a remote lane with its own command | open verified,ready | lanes and routing |
| #356 | The spark usage record reads its hold end as a time, not a duration | open verified,ready | lanes and routing |
| #375 | A lane pull request never carries the files its agent wrote to probe the sandbox | open verified,ready | lanes and routing |
| #39 | API-lane agents reach only their model provider and write only their tree (duplicate of #575) | open rewritten | lanes and routing |
| #65 | Reinstalling lanes mid-brief orphans the agent and starts a second one on the same ticket (duplicate of #580) | open rewritten | lanes and routing |

### H9. History: brain, merge and verify (verified) (13 tickets)

Done when: every member is closed (true when filed).

| # | Title | State | Theme |
|---|---|---|---|
| #88 | A lane fixes its own pull request when checks or reviews fail | open verified | brain, merge and verify |
| #104 | Prepare and record each repo's CI-compatible lane validation environment | open verified | brain, merge and verify |
| #185 | A file under t/ or named TestX counts as a test | open verified | brain, merge and verify |
| #223 | The station triages every backlog goal home once an hour | open verified,ready | brain, merge and verify |
| #288 | The brain records its current tick step and thoughts where any reader can see them | open verified,ready | brain, merge and verify |
| #290 | A brief can wait for the tickets it builds on with an After: line | open verified | brain, merge and verify |
| #344 | A diagnosis ticket's answer is posted on the ticket, not discarded as no commit | open verified,ready | brain, merge and verify |
| #359 | The brain status pull request lines live in their own helper | open verified,ready | brain, merge and verify |
| #387 | A lane pull request superseded by a newer one, or by a merge elsewhere, is closed by the factory | open verified | brain, merge and verify |
| #424 | The station's rewriter runs the lanes' opencode and logs why a rewrite failed | open verified | brain, merge and verify |
| #460 | A lane-made proof says which lane made it, and the brain audits one each tick | open verified | brain, merge and verify |
| #542 | A merged bridge pin pull request moves the tickets it deploys to loom:shipped | open verified | brain, merge and verify |
| #77 | The brain runs on a timer on the lane machine, so the goals move when no session is open (duplicate of #582) | open rewritten | brain, merge and verify |

### H1. History: charting the sources and first lanes (8 tickets)

Done when: every member is closed (true when filed).

| # | Title | State | Theme |
|---|---|---|---|
| #31 | Inventory every inference source the operator has or can get free | closed | charting and first lanes |
| #32 | What is Muse, why did it fail, and can it be a lane now and the brain later | closed | charting and first lanes |
| #33 | Sparks today: vLLM, the best coding model in assay, and an endpoint a lane can call | closed | charting and first lanes |
| #35 | The brain's tick: what it reads, decides and writes each cycle, and where its state lives | closed | charting and first lanes |
| #41 | Add the Google plan as a lane through agy -p | closed | charting and first lanes |
| #42 | Add free-model lanes: OpenCode Zen and Z.ai Flash | closed | charting and first lanes |
| #43 | Cloudflare Workers AI: confirm the stored token can reach it, then add it as a lane | closed | charting and first lanes |
| #159 | A spark lane runs easy tickets on the local vLLM coder, one at a time, and steps aside while the box is hot | closed verified | charting and first lanes |

### H2. History: station, reports and dashboard foundations (12 tickets)

Done when: every member is closed (true when filed).

| # | Title | State | Theme |
|---|---|---|---|
| #66 | The glossary defines the station, ticket states, journal, tick lock and lane status | closed verified | station and reports |
| #70 | The station publishes lane status and forwards asks to Discord | closed verified | station and reports |
| #71 | A stale lane lock kills its orphaned agent, and reinstalling never interrupts a busy lane | closed verified | station and reports |
| #82 | The station sends each goal's report to the operator's phone | closed verified | station and reports |
| #83 | The brain alarm also counts journals from any brain, so a stopped Muse is reported | closed verified | station and reports |
| #89 | The station uploads every finished transcript to the dashboard | closed verified,ready | station and reports |
| #132 | Discord reports are one short line per goal that expands to the full report, and notes carry screenshots | closed verified | station and reports |
| #135 | Loom tracks what each anthropic run costs, the remaining credit and the burn rate, without an Admin key | closed verified | station and reports |
| #156 | Loom measures the nine north-star numbers and posts them daily with the streak | closed verified,ready | station and reports |
| #262 | The station ships a Loom ticket when it deploys the commit that carries it | closed verified,ready | station and reports |
| #282 | Median and worst wait per ticket state from the label timeline | closed verified | station and reports |
| #283 | The dashboard's transcript store reads every attempt of a ticket back | closed verified,ready | station and reports |

### H3. History: lane runs, retries and the ladder (11 tickets)

Done when: every member is closed (true when filed).

| # | Title | State | Theme |
|---|---|---|---|
| #100 | A fix request survives later comments, so a fix round never opens a second PR | closed verified | lane runs and ladder |
| #112 | Failed lane runs retry automatically on another lane, and a lane at its provider limit stops taking work | closed verified,ready | lane runs and ladder |
| #141 | Repeatedly stuck tickets climb a model ladder with one brain command | closed verified | lane runs and ladder |
| #145 | Fix briefs bring their branch up to date with the default branch, so a lane can resolve a conflict | closed verified | lane runs and ladder |
| #170 | A push refused because the branch moved is not a failure, and a ticket never runs on two lanes at once | closed verified | lane runs and ladder |
| #175 | The brain rules on every ticket past the last rung | closed verified,ready | lane runs and ladder |
| #178 | A ticket moved off a lane at a limit stays in its difficulty tier | closed verified | lane runs and ladder |
| #202 | A refused temp-file permission never ends an opencode run | closed verified,ready | lane runs and ladder |
| #212 | CI runs the sandbox tests on macOS, and deploys wait for them | closed briefed | lane runs and ladder |
| #218 | The station stays inside its GitHub budget at any number of repos | closed verified,ready | lane runs and ladder |
| #267 | An OpenCode Go usage limit holds the lane instead of timing out every run | closed verified,ready | lane runs and ladder |

### H4. History: audit fixes, SDLC and merge rules (13 tickets)

Done when: every member is closed (true when filed).

| # | Title | State | Theme |
|---|---|---|---|
| #105 | Repair generated PR text without requiring a source commit | closed verified | audit and SDLC |
| #106 | Require a proof record and satisfied criteria before verified and completed | closed verified | audit and SDLC |
| #107 | Reconcile lane tickets against their authoritative PR state | closed verified,ready | audit and SDLC |
| #148 | One command onboards a repo: labels, a backlog goal home, and a review check | closed verified | audit and SDLC |
| #149 | Triage turns old issues into queued tickets or marks what is missing, and never closes anything | closed verified | audit and SDLC |
| #169 | One canonical merge-ready signal, so two ticks never disagree about a lane PR | closed verified | audit and SDLC |
| #199 | Settle the review findings left from PR #176 | closed failed | audit and SDLC |
| #226 | docs/SDLC.md shows how a change moves from ticket to verified, and the principles behind it | closed verified,ready | audit and SDLC |
| #227 | Every lane PR opens with an intent section and one change class | closed verified,ready | audit and SDLC |
| #228 | Merge rules follow the change class: docs and tests land fast, sensitive paths need a brain | closed verified,ready | audit and SDLC |
| #231 | Each component's log has a watcher that files one ticket per new repeated error | closed verified,ready | audit and SDLC |
| #260 | docs/SDLC.md is kept honest by a test, and its live and planned marks are current | closed verified,ready | audit and SDLC |
| #320 | The station checks the factory's own invariants each pass and files a ticket on a breach | closed verified,ready | audit and SDLC |

### H5. History: health findings, part 1 (15 tickets)

Done when: every member is closed (true when filed).

| # | Title | State | Theme |
|---|---|---|---|
| #348 | Health finding: duplicate reports on a goal home | closed triage,invariant | health findings |
| #349 | The daily metrics line posted 13 times for 2026-10-14 on #30 | closed triage,invariant | health findings |
| #350 | Reports posted 5 minutes apart on #30 | closed triage,invariant | health findings |
| #351 | Health finding: duplicate reports on a goal home | closed triage,invariant | health findings |
| #352 | Lane openrouter: 7 runs in 24 hours and none done | closed triage,invariant | health findings |
| #355 | Health finding: a goal home near the sub-issue cap | closed triage,invariant | health findings |
| #358 | Health finding: duplicate reports on a goal home | closed triage,invariant | health findings |
| #385 | Lane opencode-go held by a limit for 2.1 hours while 1 tickets of its tier wait | closed triage,invariant | health findings |
| #386 | Lane opencode-go-2 held by a limit for 2.1 hours while 1 tickets of its tier wait | closed triage,invariant | health findings |
| #399 | Lane anthropic held by a limit for 2.0 hours while 1 tickets of its tier wait | closed triage,invariant | health findings |
| #400 | Lane anthropic-2 held by a limit for 2.0 hours while 1 tickets of its tier wait | closed triage,invariant | health findings |
| #401 | Lane anthropic-3 held by a limit for 2.0 hours while 1 tickets of its tier wait | closed triage,invariant | health findings |
| #402 | Lane anthropic-4 held by a limit for 2.0 hours while 1 tickets of its tier wait | closed triage,invariant | health findings |
| #403 | Lane anthropic-5 held by a limit for 2.0 hours while 1 tickets of its tier wait | closed triage,invariant | health findings |
| #404 | Lane anthropic-6 held by a limit for 2.1 hours while 1 tickets of its tier wait | closed triage,invariant | health findings |

### H6. History: health findings, part 2 (12 tickets)

Done when: every member is closed (true when filed).

| # | Title | State | Theme |
|---|---|---|---|
| #405 | Lane anthropic-7 held by a limit for 2.1 hours while 1 tickets of its tier wait | closed triage,invariant | health findings |
| #406 | Lane anthropic-8 held by a limit for 2.1 hours while 1 tickets of its tier wait | closed triage,invariant | health findings |
| #407 | Lane opencode-go-2: 5 runs in 24 hours and none done | closed triage,invariant | health findings |
| #416 | Brain unnamed reported a failed or degraded step two ticks in a row | closed triage,invariant | health findings |
| #419 | Health finding: duplicate reports on a goal home | closed triage,invariant | health findings |
| #428 | Health finding: a lane pull request held by its review check | closed triage,invariant | health findings |
| #434 | Health finding: a lane pull request held by its review check | closed triage,invariant | health findings |
| #435 | Health finding: a lane pull request held by its review check | closed triage,invariant | health findings |
| #443 | Health finding: a lane pull request held by its review check | closed triage,invariant | health findings |
| #457 | Health finding: a lane pull request held by its review check | closed triage,invariant | health findings |
| #458 | Health finding: a lane pull request held by its review check | closed triage,invariant | health findings |
| #515 | Lane spark-3: 5 runs in 24 hours and none done | closed triage,invariant | health findings |

## Coverage check

Every one of the 235 children appears exactly once: 78 in live epics, 49 in waiting epics, 107 in history epics, and #613 adopted as E1. No epic holds more than 15 tickets.

## Open questions

- Waiting epics or a temporary exception to the 6-epic cap (recommended: waiting epics, as above).
- Whether the open `loom:verified` tickets close in one sweep before the epics are filed (recommended: yes, through `bus.brain close`, which requires the verified label).
- Whether the nine merged tickets with no state label are verified by a verify run or closed by the operator (recommended: a verify run).
