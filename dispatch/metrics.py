#!/usr/bin/env python3
"""North-star metric collection, per goal.

Each goal defines its metric in its goal doc. The WoW goal's north star is
member-hours of natural progress:
 - members gaining XP
 - median and lowest level
 - dungeon clears per attempts
 - class quests done

The dispatcher samples it every 30 minutes (its loop cadence); a goal doc
may define a slower sample cadence for expensive rollups.

Output: append-only state/metrics/<goal>.jsonl, one JSON object per sample
with a timestamp. Never overwrite history.
"""
