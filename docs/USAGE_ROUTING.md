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

## Tiers, not names

The dispatcher never hardcodes model names. Each ticket is scored
easy / medium / hard, then matched to the cheapest lane whose tier fits:

- frontier - the hardest tickets (deep root cause, tricky architecture)
- strong - standard implementation (the default)
- fast / free - high-volume small tickets (triage, tests, small fixes)

Lanes report their catalogs; new models appear automatically. When a lane
is near its budget or rate-limited, the dispatcher routes around it.
