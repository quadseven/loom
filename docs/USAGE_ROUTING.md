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

`limits` carries the lane's current usage vs limit (loom#1101), computed per
lane type by `bus/limits.py` (`python3 -B -m bus.limits <lane>`):

```json
"limits": {
 "remaining": 0.75,
 "reset_at": 1731234567,
 "expires_at": null,
 "unit": "credits",
 "source": "openrouter key endpoint",
 "stale": false
}
```

`remaining` is the fraction of capacity left (null when the lane type
exposes no usage figure), `reset_at` the epoch the current window resets,
`expires_at` the epoch prepaid credits expire, and `unit` what the fraction
measures. The dispatcher prefers lanes whose credits expire soonest, then
the most remaining capacity. Meter reads (OpenRouter, nvidia-smi) are
cached 5 minutes so a slow or failed probe never blocks the heartbeat.

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
  recorded; when every anthropic slot reports "out of credit", the credit
  reads as used up.

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
