#!/usr/bin/env python3
"""North-star metric collection, per goal.

Each goal defines its north-star metric on its goal home issue, in the
project's own repo; Loom holds no goal-specific definitions.

The dispatcher samples it every 30 minutes (its loop cadence); a goal doc
may define a slower sample cadence for expensive rollups.

Output: append-only state/metrics/<goal>.jsonl, one JSON object per sample
with a timestamp. Never overwrite history.
"""
