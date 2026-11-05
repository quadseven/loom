#!/usr/bin/env bash
# Loom opencode usage heartbeat  -  OpenRouter key quota.
#
# Writes state/usage/opencode.json with the key's credit usage and limits.
# OpenRouter GET /api/v1/key returns fields nested under `data`: credit
# spend (usage, usage_daily/weekly/monthly), the spend cap (limit), the
# remaining headroom (limit_remaining), and is_free_tier.
#
# Note: the /key endpoint does NOT expose the free-model daily request
# counter (50/day, or 1000/day after the one-time credit purchase). For
# free-tier budget awareness the dispatcher also watches worker
# self-reported burn and 429 rate-limit signals.
set -euo pipefail

OUT="$HOME/loom/state/usage/opencode.json"
mkdir -p "$(dirname "$OUT")"

RESP="$(curl -s https://openrouter.ai/api/v1/key \
  -H "Authorization: Bearer $OPENROUTER_API_KEY")"

python3 - "$RESP" > "$OUT" << 'EOF'
import json, sys
d = json.loads(sys.argv[1]).get("data") or {}
print(json.dumps({
    "lane": "opencode",
    "models": "see config.json providers",
    "budget": {
        "label": d.get("label"),
        "usage": d.get("usage"),
        "usage_daily": d.get("usage_daily"),
        "usage_weekly": d.get("usage_weekly"),
        "usage_monthly": d.get("usage_monthly"),
        "limit": d.get("limit"),
        "limit_remaining": d.get("limit_remaining"),
        "limit_reset": d.get("limit_reset"),
        "is_free_tier": d.get("is_free_tier"),
    },
    "free_model_request_quota": "not exposed by /key; see worker burn + 429 signals",
    "rate_limited": False,
}, indent=2))
EOF
