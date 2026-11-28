# Usage-aware routing

## Heartbeats

Every lane writes `state/usage/<lane>.json` on every runner pass (including when a lane is unavailable). The file contains:

```json
{
 "lane": "codex",
 "at": 1731234567,
 "available": true,
 "reason": "",
 "state": "serving",
 "state_reason": "Accepting work",
 "limited_models": {}
}
```

`at` is the runner's Unix timestamp. `available` and `reason` record runner
readiness. The station evaluates the heartbeat again when it reads it so
freshness and policy apply even when the runner has not reported recently.
The factory routes to serving lanes, subject to ticket and model rules.

The station publishes each lane's state, reason, and heartbeat time in the
table shared by `bus.brain status` and the dashboard. A ready lane is
`serving`, a runner at a limit is `limited`, a lane held by policy is
`parked`, and a lane without a usable heartbeat is `off`.

## Sources (ToS-clean)

- OpenRouter: `GET /api/v1/key` - exact quota and reset times.
- Sparks: `nvidia-smi` - GPU load directly.
- Subscription usage comes from Codex session logs, Claude CLI events,
  OpenCode Go's plan check, and agy CLI meter files where available.
- Everywhere else: self-reported burn from worker session logs, plus
 explicit rate-limit signals from the workers themselves.

## The Usage view's readings (bus/usage.py)

The station collects one reading per provider window each pass
(`state/usage/limits.json`) and uploads it to the dashboard's `/usage` page.
Each reading names the machine that collected it (`host`).

- codex: the newest reading for each of the 5-hour and weekly windows from
  recent local session logs, configurable with `LOOM_CODEX_SESSIONS`.
  The station fills a missing window from `state/usage/<slot>.meter.json`,
  which each codex lane writes from its `CODEX_HOME/sessions` on an idle
  pass (at most every five minutes) and after each run. A station that has
  only a local 5-hour reading can still show the forwarded weekly reading.
  With neither source, it reports the reason no reading is available.
- claude: 5-hour and weekly readings from rate-limit events in ordinary
  lane runs. The lane saves the figures and reset times in its CLI meter.
- opencode-go: rolling, weekly, and monthly readings from the plan check
  the model picker already uses before a paid run.
- agy: the newest CLI meter when the lane supplies one.
  Without a CLI meter, the board still shows the Claude and agy limit holds
  from their heartbeats.
- muse: the remote lane reports only a heartbeat, so its reading says no
  usage or limit reaches Loom and how old the heartbeat is.
- anthropic: spend from `state/usage/spend.jsonl` against
  `LOOM_ANTHROPIC_BUDGET_USD` counted from `LOOM_ANTHROPIC_BUDGET_SINCE`. No
  Anthropic API reads the remaining prepaid balance (the Admin API usage and
  cost reports give spend only), so the operator sets the budget to the
  console's balance. Unset, the reading names both settings and the spend
  recorded; when every anthropic slot reports "out of credit", the credit
  reads as used up.

## Pace (bus/pace.py)

After collecting, the station paces each provider with a current reading
of a window of known length (its longest such window, weekly where there
is one): used share minus elapsed share, against `LOOM_PACE_SLACK`
(default 10 points). Behind with a ticket waiting for a free lane, it asks
for one more slot than the provider's slots running now (fresh
heartbeats), up to its most: the registry's slots of that provider, or
`LOOM_PACE_MAX_<PROVIDER>`. On or ahead of pace it asks for none. A
reading that is stale or from a window that has reset gives no verdict.
The plan is written to `state/usage/pace.json`. The lane status comment
shows each subscription's longest window, used and elapsed percentages,
UTC reset, pace, and running and allowed slots. Each Claude subscription
has its own read-only row from its slot's meter. The lane status and Usage
count slots with a fresh heartbeat or runner keepalive, including runners
that are held or limited, and exclude jobs the station knows are absent.
Usage shows the running count as unknown once its runner evidence expires.
The meter shows no reading when it is missing, stale, or reset. A current
slot request appears when its plan agrees with the displayed counts. The
plan itself is data only: a lane container runs
the slots its deployment names, so the request is what a pod that can
start slots at runtime would read.

Subscription meters in `state/usage/<lane>.meter.json` carry the observed
time, percent used, and reset time when the provider supplies them. A
figure over two hours old or from a window that has reset is marked stale.
The Usage board shows the reset time and countdown for each window.

The lane status comment and the Usage board show OpenRouter requests and
Workers AI Neurons used out of their daily allowances. The station's daily
meter windows reset at midnight UTC. Missing, stale, and reset figures show
no reading. To display pace, set `LOOM_DAILY_PACE_TARGET_HOUR` to a UTC hour
from 1 to 24 and `LOOM_DAILY_PACE_TARGET_SHARE` to the percentage of the
allowance to use by then. For example, `18` and `90` target 90% by 18:00 UTC.
Pace compares against a straight line from midnight to that target, then
holds at the target through reset. Spending less than the curve by more
than `LOOM_PACE_SLACK` is behind pace. The counts are visible without a target.
This view leaves dispatch and slot requests to the existing plan.

`python3 -m bus.usage` collects on the lane machine only: on a machine with
no lane heartbeat in the last 10 minutes it says so and prints no figures,
since its files there are leftovers nothing refreshes. Anywhere else, such as
a remote brain, `python3 -m bus.usage --live` prints the station's live
snapshot from the dashboard (`GET /api/usage`, with `LOOM_TRANSCRIPT_URL` and
the read-only `LOOM_HANDOFF_TOKEN`, or the upload token at
`LOOM_TRANSCRIPT_TOKEN_SSM`).

## Tiers, not names

The dispatcher never hardcodes model names. Each ticket is scored
easy / medium / hard, then matched to the cheapest lane whose tier fits:

- frontier - the hardest tickets (deep root cause, tricky architecture)
- strong - standard implementation (the default)
- fast / free - high-volume small tickets (triage, tests, small fixes)

Lanes report their catalogs; new models appear automatically. When a lane
is near its budget or rate-limited, the dispatcher routes around it.
When assigning queued work, the station moves an eligible lane up within its
normal tier or pinned-model order if a fresh reading shows its weekly window
under half used and resetting in less than 24 hours. A lane with a fresh
window at its limit gets no preference.

## October 2026 price changes

Routing decisions recorded 2026-10-28, from the vendors' October price
changes. Prices are US dollars per million tokens.

- High-volume subtasks on priced Anthropic routes pin `claude-haiku-5-5`.
  Haiku 5.5 (released 2026-10-07) costs $0.10 input / $0.50 output for
  prompts up to 100k tokens ($0.50 / $2.50 above that), against Haiku 4.5's
  $1.00 / $5.00. Triage, extraction, classification and other small,
  well-scoped subtasks that would otherwise burn a priced Sonnet or Opus
  route belong on it. The cheap tier stops at 100k prompt tokens (cache
  reads and writes count toward the prompt), so volume pins keep their
  prompts short.
- Cache-heavy work stays on `claude-sonnet-5-5`. On 2026-10-07 its
  cache-read price halved, $0.20 to $0.10 (cache writes unchanged at
  $2.50 for the 5-minute cache and $4.00 for the 1-hour cache), which the
  vendor puts at roughly 20% cheaper on most agentic workloads. Work that
  re-reads a large shared context - the model ladder's first rung above
  all (bus/factory.py) - is exactly the traffic that benefits, so the
  price change widened Sonnet's lead for that shape of work instead of
  opening the rung to a smaller model.
- The spark slots' local model is under evaluation, not changed: see
  "Mellum 2.1 evaluation" in docs/LANES.md.
