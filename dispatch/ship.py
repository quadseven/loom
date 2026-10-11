#!/usr/bin/env python3
"""Single shipper. Squash merges, answers every bot comment, batches ships.

Per-goal ship rules come from the goal doc. WoW rules (the strict ones):
 - at most one worldserver roll per hour
 - never while a family is inside a dungeon (check campaign state first,
 wait up to 60 minutes for guild runs to finish)
 - batch bridge restarts (each restart resets the jobs clock)
 - mod-overseer bumps move all five pins together; worldserver and
 db-import pinned as a pair; keep the 3-line comment

Other goals get their own ship rules. Loom never invents leniency.
"""
