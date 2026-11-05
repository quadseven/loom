"""bus/scorecard.py: a refresh that carries over at the pass's request target
keeps what it read and resumes on the next pass (loom#2651).

The station's refresh used to throw away every record a carried-over pass had
read and start again from scratch, so on a busy pass budget it never finished
and demotions were never re-scored.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bus import scorecard  # noqa: E402
from bus.github import GitHub, TargetReached  # noqa: E402
from test_scorecard import (BOT, LANES_LOGIN, NOW, REPO, Route, finding,  # noqa: E402
                            lane_pr, quiet)

# Each lane pull request costs four counted reads: its review threads, its
# ticket's comments, its commits and its reviews. Four of them need 16, so a
# pass budget of 10 can never finish a refresh that starts from scratch.
LIMIT = 10


class Budget:
    """A GitHub client whose deferrable reads stop at a pass target: past
    `limit` reads, each one raises TargetReached, as bus/github.py does."""

    def __init__(self, gh, limit):
        self.gh, self.limit, self.used = gh, limit, 0

    def _spend(self):
        if self.limit is not None and self.used >= self.limit:
            raise TargetReached("read carried over at request target")
        self.used += 1

    def paginate(self, path):
        self._spend()
        return self.gh.paginate(path)

    def graphql(self, query, variables=None):
        self._spend()
        return self.gh.graphql(query, variables)

    def __getattr__(self, name):
        return getattr(self.gh, name)


def routes():
    pulls = [lane_pr(1), lane_pr(2), lane_pr(3), lane_pr(4, lane="codex", model="gpt-6-luna")]
    fake = Route({"/repos/%s/pulls?state=closed" % REPO: pulls,
                  "/repos/%s/pulls/" % REPO: {"additions": 3, "deletions": 1, "changed_files": 1},
                  "/repos/%s/issues/" % REPO: [],
                  "/repos/%s/commits/" % REPO: {"check_runs": []}},
                 {1: [finding("dead-code")]})
    for pr in pulls:
        number = pr["number"]
        fake.routes["/repos/%s/pulls/%d/commits" % (REPO, number)] = [{"sha": "%040d" % number}]
        fake.routes["/repos/%s/pulls/%d/reviews" % (REPO, number)] = []
    return fake


def station_pass(state, now, limit, env=None):
    """One station pass's refresh, the way Station._refresh_scorecard runs
    it: saved only when refresh returns. Whether it completed."""
    card = scorecard.Scorecard(state, now, env=env or {})
    gh = Budget(GitHub("t", transport=routes()), limit)
    try:
        quiet(card.refresh, gh, [REPO], [LANES_LOGIN], BOT, {}, {})
    except TargetReached:
        return False
    card.save()
    return True


def saved(state):
    with open(os.path.join(state, scorecard.FILE)) as fh:
        return json.load(fh)


class Resume(unittest.TestCase):
    def test_a_carried_over_refresh_completes_on_a_later_pass_and_saves(self):
        state = tempfile.mkdtemp()
        self.assertFalse(station_pass(state, NOW, LIMIT))
        # The carried-over pass kept what it read, and is still due.
        kept = saved(state)
        self.assertEqual(kept["refreshed"], 0)
        self.assertTrue(kept["records"])
        self.assertTrue(scorecard.Scorecard(state, NOW + 60).due())
        self.assertTrue(station_pass(state, NOW + 60, LIMIT))
        done = saved(state)
        self.assertEqual(done["refreshed"], NOW + 60)
        self.assertEqual(len(done["records"]), 4)
        self.assertTrue(all("rounds" in r for r in done["records"].values()))
        self.assertEqual(done["cycle"], {})
        self.assertFalse(scorecard.Scorecard(state, NOW + 120).due())

    def test_a_float_refresh_time_loads_so_the_card_waits_an_hour(self):
        state = tempfile.mkdtemp()
        seed = scorecard.Scorecard(state, NOW + 0.5, env={})
        seed.data["refreshed"] = NOW + 0.5
        seed.save()
        self.assertEqual(scorecard.Scorecard(state, NOW + 60).data["refreshed"], NOW + 0.5)
        self.assertFalse(scorecard.Scorecard(state, NOW + 60).due())
        self.assertTrue(scorecard.Scorecard(state, NOW + 0.5 + scorecard.REFRESH).due())

    def test_a_completed_refresh_lifts_a_demotion_whose_outcomes_aged_out(self):
        state = tempfile.mkdtemp()
        seed = scorecard.Scorecard(state, NOW, env={})
        old = NOW - 8 * 86400
        for n in range(3):
            seed.data["records"]["%s#%d" % (REPO, 900 + n)] = {
                "lane": "zen", "model": "unknown", "at": old, "review": "bad-work",
                "merged": True, "sent_back": 0, "settled": "easy", "rounds": 0}
            seed.data["records"]["%s#%d" % (REPO, 950 + n)] = {
                "lane": "agy", "model": "unknown", "at": NOW - 3600, "review": "bad-work",
                "merged": True, "sent_back": 0, "settled": "easy", "rounds": 0}
        seed.data["demoted"]["zen"] = {"at": old, "why": "3 bad outcomes", "kind": "bad-outcomes"}
        seed.data["demoted"]["agy"] = {"at": NOW - 3600, "why": "3 bad outcomes",
                                       "kind": "bad-outcomes"}
        seed.save()
        self.assertFalse(station_pass(state, NOW, LIMIT))
        self.assertIn("zen", saved(state)["demoted"])
        self.assertTrue(station_pass(state, NOW + 60, LIMIT))
        demoted = saved(state)["demoted"]
        self.assertNotIn("zen", demoted)
        self.assertIn("agy", demoted)


if __name__ == "__main__":
    unittest.main()
