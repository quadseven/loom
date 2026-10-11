#!/usr/bin/env bash
# Loom sparks usage heartbeat  -  GPU load, read directly.
set -euo pipefail

OUT="$HOME/loom/state/usage/sparks.json"
mkdir -p "$(dirname "$OUT")"

# nvidia-smi may be absent (no GPU) or fail (driver hiccup). Under
# set -euo pipefail a bare failure kills the heartbeat entirely, so probe
# first and write a degraded heartbeat instead of nothing.
if command -v nvidia-smi >/dev/null 2>&1 \
  && nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total \
    --format=csv,noheader,nounits > "$OUT.raw" 2>/dev/null; then
  python3 -c "
import json
rows = [l.split(', ') for l in open('$OUT.raw').read().strip().splitlines() if l.strip()]
print(json.dumps({
    'lane': 'sparks',
    'gpus': [{'util_pct': int(r[0]), 'mem_used_mb': int(r[1]), 'mem_total_mb': int(r[2])} for r in rows],
    'rate_limited': False,
}, indent=2))
" > "$OUT"
  rm -f "$OUT.raw"
else
  python3 -c "
import json
print(json.dumps({
    'lane': 'sparks',
    'gpus': [],
    'rate_limited': False,
    'degraded': 'nvidia-smi unavailable',
}, indent=2))
" > "$OUT"
fi
