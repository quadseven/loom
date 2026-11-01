# Usage-aware routing

## Heartbeats

Every lane writes `state/usage/<lane>.json` on every runner pass (including when a lane is unavailable). The file contains:

```json
{
 "lane": "codex",
 "at": 1731234567,
 "available": true,
 "reason": ""
}
```

`lane` is the lane name, `at` is a Unix timestamp from the runner's current time, `available` is true when the lane can take new work, and `reason` explains unavailability. The dispatcher reads this file and routes only to lanes with fresh heartbeats (`within 600 seconds`) that report `available: true`.

Quota fields (models, budget, burn_rate_per_hour, rate_limited) are not written yet and are a planned addition.

## Sources (ToS-clean)

- OpenRouter: `GET /api/v1/key` - exact quota and reset times.
- Sparks: `nvidia-smi` - GPU load directly.
- Claude / Codex subscriptions: no usage API, so we watch the official
 usage websites. A scheduled, logged-in browser check opens the owner's own
 account usage pages and reads the numbers into state. Their dashboard,
 official site, just looking.
- Everywhere else: self-reported burn from worker session logs, plus
 explicit rate-limit signals from the workers themselves.

## The Usage view's readings (bus/usage.py)

The station collects one reading per provider window each pass
(`state/usage/limits.json`) and uploads it to the dashboard's `/usage` page.
Each reading names the machine that collected it (`host`).

- codex: the 5-hour and weekly windows from the newest codex session log on
  the collecting machine (`~/.codex/sessions`). A machine where codex never
  ran says "no codex session logs on this machine"; a figure read over two
  hours ago, or from a window that has reset since, is marked stale.
- claude and agy: a "Limit" reading from the lane heartbeat. Neither CLI
  leaves a usage figure on disk; the runner records each limit hold, with the
  reset the CLI printed, as `retry_at`. While held the reading says "limited
  until" the reset; after, the last limit and when it lifted
  (`state/usage/<lane>.last-limit.json`).
- muse: the remote lane reports only a heartbeat, so its reading says no
  usage or limit reaches Loom and how old the heartbeat is.
- anthropic: spend from `state/usage/spend.jsonl` against
  `LOOM_ANTHROPIC_BUDGET_USD` counted from `LOOM_ANTHROPIC_BUDGET_SINCE`. No
  Anthropic API reads the remaining prepaid balance (the Admin API usage and
  cost reports give spend only), so the operator sets the budget to the
  console's balance. Unset, the reading names both settings and the spend
  recorded; when `LOOM_NO_TOPUP` lists anthropic or every anthropic slot
  reports "out of credit", the credit reads as used up.

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
