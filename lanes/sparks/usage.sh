#!/usr/bin/env bash
# Loom sparks usage heartbeat  -  GPU load, read directly.
set -euo pipefail

OUT="$HOME/loom/state/usage/sparks.json"
mkdir -p "$(dirname "$OUT")"

nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total \
  --format=csv,noheader,nounits | python3 -c "
import json, sys
rows = [l.split(', ') for l in sys.stdin.read().strip().splitlines()]
print(json.dumps({
    'lane': 'sparks',
    'gpus': [{'util_pct': int(r[0]), 'mem_used_mb': int(r[1]), 'mem_total_mb': int(r[2])} for r in rows],
    'rate_limited': False,
}, indent=2))
" > "$OUT"
