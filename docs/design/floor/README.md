# Floor design handoff

Design references for the merged Loom dashboard (decision:
docs/decisions/0001-dashboard-and-backlog.md). These are prototypes, not code
to ship: rebuild each view in `dashboard/` with stdlib Python and one small
vanilla JS file. `app.dc.html` and `site.dc.html` are the prototypes (their
design-tool runtime `support.js` is not included, so read them as markup and
styles; they do not run on their own); `loom-data.js` holds invented sample
data whose shapes are the suggested API contract. Non-ASCII glyphs are kept as
HTML entities or `\u` escapes. Sample repo names are placeholders.

# Handoff: Loom floor (operator dashboard) + public site

## Overview
A redesign of the Loom dashboard (currently a ticket list + /models page) into:
1. **The floor** - a mobile-first operator app showing every ticket's path through the SDLC, live and past lane transcripts, the brain's reasoning per tick, where the factory slows down, repeat failures, the self-improvement loop (loom#30 / grug#1143), lanes and models, and asks for the operator.
2. **The public site** - a short explainer of what Loom is, in the spirit of agents.md: one idea, plain words, real numbers as proof.

Visual language comes from Hunt, the backlog board: cold dark palette, one amber accent, **mono = machine fact, sans = human judgement**, honest empty states.

## About the design files
The files in this bundle are **design references created in HTML** - prototypes showing intended look and behavior, not production code to copy. Recreate them in the loom repo's environment. Loom has no web framework today (`lanes/viewer/serve.py` + `render.py` are stdlib Python). Recommended: keep it server-rendered Python emitting static HTML + one small vanilla-JS file for tabs, filters and live polling, matching how the viewer works now. A SPA is not required.

## Fidelity
**High-fidelity.** Final colors, type, spacing and copy tone. Data is invented (`loom-data.js`); wire to real sources below.

## Data sources (map each view to the bus)
| View | Source in loom |
|---|---|
| Ticket state, counts | state labels, `bus/labels.py` (queued, briefed, running, pr, merged, shipped, verified, failed) |
| Median wait per state | label event timestamps (GitHub issue timeline) - new: compute in `bus/metrics.py` |
| Ticket path | issue timeline + `<!-- loom:rung N -->` comments + station outcome comments + `bus/handoff.py` markers |
| Transcripts | `bus/transcripts.py` upload body `{ticket, slot, model, outcome, started_at, finished_at, events:[{ts, role, kind, text}]}` - one per run. The brain's ticks use the same shape with `slot: "brain"` |
| grug threads | PR review threads (severity, resolved, lane answer) - already read by `bus.brain status` |
| Brain tick steps / thoughts | brain tick transcript + journal comments on goal homes |
| Standup | latest `<!-- loom:report v1 -->` comment per goal |
| Factory numbers | the nine numbers `LOOM_METRICS_GOAL` posts daily |
| Lanes | `loom:lanes` issue heartbeat table; quota from `bus/spend.py` |
| Models scoreboard | current /models page logic |
| Asks | open `needs-operator` issues; `loom:human` counts by repo |
| Improvements | loom#30 / grug#1143 child tickets; friction line from the journal that spawned them |

## Screens / views

### Shell
- **Breakpoint**: `>= 960px` = desktop (left nav 232px, sticky, full height, right border `#1e242e`). Below = mobile: sticky 52px top bar + fixed 76px bottom tab bar (floor, goals, tickets, brain, more). "more" opens a bottom sheet (hot spots, improving, lanes & models, asks, About Loom). Sheet rows are min 52px tall.
- Content: `max-width:1320px`, padding `24px clamp(16px,3.5vw,40px) 120px`, sections gap 28px.
- Route and open ticket persist in localStorage (`loomProto`). Real build: use URLs (`/`, `/goals`, `/tickets?state=pr&goal=loom%2330`, `/t/loom/94?run=15&tab=transcript`). Shareable URLs fix the Hunt review's top complaint.

### Floor
- **Standup** line, sans 21-28px (clamp), weight 500, letter-spacing -0.015em; meta mono 11px `#7a8596`.
- **Conveyor**: grid `repeat(auto-fill,minmax(88px,1fr))`, gap 6. Card: bg `#151a22`, 1px `#222935`, radius 10, padding 10/11. Count mono 22/600, tabular-nums, colored by state; state name mono 11 `#c9d0db`; median wait mono 10.5 `#7a8596`. The bottleneck (largest median) gets an amber border at 45% alpha and a header note "pr is the bottleneck: 3.1h". Click filters Tickets.
- **Moving now**: ticket rows (see Ticket row) for running, pr, briefed, merged, shipped.
- **Right column** (wraps under on mobile): Brain's next move card (amber border 38% alpha, radius 14), Since last report (well / not well / stuck / next, colored mono key 58px wide), Needs you card (count, links to Asks).

### Ticket row (shared)
bg `#121720`, 1px `#1e242e`, radius 10, padding 12/14, gap 7. Hover: bg `#171d27`, border `#2c3442`. Line 1 mono 11.5: short id left, flag right (flag color by state; `attempt N /` prefix when attempts > 1; red for regressions). Line 2 sans 14/1.38 `#d3d9e3` title. Line 3: seven 4px progress segments (done green, current amber or red if failed, future `#232a35`), max 240px, plus `lane / model` mono 10.5.

### Goals
Card grid `minmax(min(100%,360px),1fr)`. Each: id + tag (improving green / regression red, red border), title 17/600, metric value mono 30/600, "metric / was prev", a 7-bar sparkline (120x44, last bar amber or red), state count chips, report sentence, "N tickets on this goal ->" (filters Tickets by goal).

### Tickets
State chips (pill, mono 12, only states with tickets) + goal chips; grid of ticket rows `minmax(min(100%,420px),1fr)`. Empty result shows a sentence and "Clear filters". It is never a blank page.

### Ticket (path + transcript + PR)
- Header: back, flag, `id / goal`, title 19-26px/600, meta mono (`lane / model / difficulty / N attempts / rung N`), 7-state strip with labels.
- **Desktop**: three columns `260px | minmax(0,1fr) | 300px`, gap 28: Path, Transcript, PR.
- **Mobile**: segmented control (transcript / path / pr), sticky under the top bar. One pane at a time.
- **Path**: 3 checks card ([x] green / [ ] grey), then a timeline: 9px dot (brain blue, lane grey, grug/fail red, clean green, next `#232a35`), 1px rail `#262d39`, `who` + time mono 11, text 13/1.42, optional meta mono 10.5.
- **Transcript**:
  - attempt chips (one per run; failed red tint, done green tint, selected outlined), a run line (`attempt N / slot / model / outcome`), and a thoughts on/off toggle.
  - Events render from the real event shape:
    - `role:user` -> **brief**, collapsed to its first line with "show the full brief"
    - `role:thinking` -> **thought**, italic 13.5 `#9aa4b4`
    - `kind:tool_use` -> **ran** (`$ command`) or **edited** (file + colored +/- lines), with the next `tool_result` merged into the same block. Output lines turn red on FAIL/Error/failed and green on passed/OK.
    - `role:assistant text` -> **said**, 14 `#e2e6ee`
    - `role:runner log` -> **runner**, mono 11.5
    - PR review threads interleave by timestamp as **grug** (red-bordered quote) and **answered** (lane reply).
  - Running tickets append a live row (amber dot, "thinking... streaming from <slot>"); poll for new events every few seconds.
- **PR**: checks list, files with +/-, grug threads (severity / open/resolved), "Across all attempts" summary (what the failing attempts had in common), and a Proof card (waiting amber / seen live green).

### Brain
Tick progress (10 steps: lock, status, merge, verify, failed, asks, brief, triage, journal, unlock; done green 50%, current amber), next move, "Thinking, this tick" (tag colored: regression red, operator amber, merged green, verifying amber, deferred grey), ranking card (members affected x hours lost, with the arithmetic in words, not bare numbers), handoff markers (mono, verbatim), recent ticks with holder and outcome.

### Hot spots
"Where it slows" (median wait per state, worst in text, bottleneck amber, note), what grug keeps sending back (count + trend arrow), lane failure rate (5 vertical bars, red above 20%), repeatedly stuck tickets (rung > 0 or failed).

### Improving
A 4-stage loop with counts: noticed (journal) -> ticketed (loom#30 / grug#1143) -> shipped -> moved (a factory number changed). Each card has 4 progress segments, the friction quote, source journal, ticket, state, and "moved: X" in green when it moved. Below that are the nine factory numbers, each with "was" and a color for good or bad direction.

### Lanes & models
Anthropic share bar with a 50% cap marker; lane cards (state dot busy amber / free green / held red, model, ticket or note, quota bar red under 25%; clicking a busy lane opens its ticket); models scoreboard by tier with stacked merged/open/closed bars (keeps today's /models content).

### Asks
Card per `needs-operator` issue: one exact ask, buttons min 44px (primary amber fill, secondary outline). Acting shows "'X' sent. The brain picks it up on the next tick." Footer line with `loom:human` counts.

### Public site (`Loom Site.dc.html`)
Single column max 820px. Sections: hero (H1 34-56px, two CTAs 46px tall, 4 live numbers), the factory (label chips + 7 numbered steps), follow one ticket (grug#1146 in 5 lines + transcript link), it improves itself (4 cards ending in green "moved"), lanes (difficulty routing), the rules (4 cards), run it (code block), footer.

## Interactions & behavior
- Every ticket reference anywhere is clickable and opens its ticket.
- Conveyor cards and goal links set Tickets filters.
- Attempt chips switch the transcript run; thoughts toggle hides `thinking` events; brief expands in place.
- Hover: rows and cards lighten to `#171d27`. There are no transitions beyond this; the design is deliberately still.
- Live: running transcripts poll; the top bar dot is green when the last tick finished OK.
- Touch targets: tabs, chips and buttons are at least 44px on mobile.

## Design tokens
- Background `#0e1117`; surface `#151a22`; row `#121720`; raised `#1b212b`; code `#090c10`.
- Lines `#1a1f28` (hairline), `#1e242e`, `#222935`, `#262d39`, `#2c3442` (hover), `#3a4352` (strong).
- Text `#e2e6ee`; body `#d3d9e3` / `#c9d0db`; muted `#9aa4b4` (5:1+); dim `#7a8596` (labels only); faint `#5f6a7b` (timestamps only).
- Amber (act on this) `oklch(0.82 0.14 75)`; green (done/good) `oklch(0.78 0.13 155)`; red (failing) `oklch(0.72 0.15 25)`; blue (brain/shipped) `oklch(0.76 0.1 245)`; future segment `#232a35`.
- State colors: queued dim, briefed `#c9d0db`, running/pr amber, merged/verified green, shipped blue, failed red.
- Type: Geist (sans) for sentences; Geist Mono for ids, numbers, labels and code. Labels are mono 10.5-11, uppercase, letter-spacing .08em. Body 13-14, titles 17-26/600, numbers tabular-nums.
- Radius: 4 (chips), 6-8, 10 (rows/cards), 12-14 (panels), 999 (pills). No shadows.
- Spacing: 3/4/6/8/10/12/14/16/18/22/28.

## Files
- `Loom App.dc.html` - the floor prototype (all views, responsive)
- `Loom Site.dc.html` - public explainer
- `loom-data.js` - sample data; its shapes are the suggested API contract
- `Loom Concept.dc.html` - the earlier static concept board (mobile + desktop artboards)
