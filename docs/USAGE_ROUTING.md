# Usage-aware routing

## Heartbeats

Every lane writes `state/usage/<lane>.json` on a schedule (every 30 min):

```json
{
 "lane": "codex",
 "models": ["gpt-6-astra", "gpt-6-sol", "gpt-6-luna"],
 "budget": {"used": 312, "limit": 1000, "unit": "requests", "resets_at": "..."},
 "burn_rate_per_hour": 41,
 "rate_limited": false
}
```

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
