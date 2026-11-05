#!/usr/bin/env python3
"""Loom dispatcher - the foreman's 30-minute loop.

Each run:
 1. For every active goal: collect the north-star metric (dispatch/metrics.py)
 and append to state/metrics/<goal>.jsonl.
 2. Rank blockers by metric cost; ensure the top blocker has a ticket.
 3. Score the ticket easy/medium/hard; pick the cheapest lane whose tier
 fits and whose heartbeat shows budget (state/usage/<lane>.json).
 4. Render the brief (docs/BRIEF_FORMAT.md) into briefs/<lane>/.
 5. Health-check lanes: stale briefs get reassigned; quiet workers get
 nudged via their pollers.
 6. Watch open PRs: CI status, bot comments (answer every one).
 7. Hand green PRs to dispatch/ship.py; hand shipped fixes to
 dispatch/verify.py.

Asks the owner only for: irreversible actions, rule conflicts, lineup
changes, HITL tickets.
"""
