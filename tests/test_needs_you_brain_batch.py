"""The asks step has a bounded job: bus.needs_you lists at most a few items
for the brain to review each tick, oldest ask first (loom#644)."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import needs_you  # noqa: E402

REPO = "example-owner/project"
OP = "op-example"
BRAIN = "loom-brain[bot]"
TRUSTED = [OP, BRAIN]


def item(number, asked, latest_by=""):
    return needs_you.Item(
        ref="%s#%d" % (REPO, number), section=needs_you.NEEDS_YOU, state="open",
        who=(OP,), ask="Ask %d?" % number, asked_at=asked,
        url="https://example.com/%d" % number, latest_by=latest_by,
        latest_at="2030-01-09T00:00:00Z" if latest_by else "")


def day(n):
    return "2030-01-%02dT00:00:00Z" % n


class BrainBatchTest(unittest.TestCase):
    def test_batch_is_the_oldest_asks_first_and_bounded(self):
        items = [item(n, day(20 - n)) for n in range(1, 9)]
        got = needs_you.brain_batch(items, TRUSTED, 5)
        self.assertEqual([8, 7, 6, 5, 4], [int(i.ref.split("#")[1]) for i in got])

    def test_size_sets_the_count(self):
        items = [item(n, day(n)) for n in range(1, 9)]
        self.assertEqual(2, len(needs_you.brain_batch(items, TRUSTED, 2)))
        self.assertEqual([], needs_you.brain_batch(items, TRUSTED, 0))

    def test_item_last_commented_by_a_trusted_login_drops_out(self):
        items = [item(1, day(1), latest_by=BRAIN.upper()), item(2, day(2), latest_by="someone"),
                 item(3, day(3), latest_by=OP), item(4, day(4))]
        got = needs_you.brain_batch(items, TRUSTED, 5)
        self.assertEqual([2, 4], [int(i.ref.split("#")[1]) for i in got])

    def test_unreadable_ask_time_goes_last(self):
        got = needs_you.brain_batch([item(1, ""), item(2, day(2))], TRUSTED, 5)
        self.assertEqual([2, 1], [int(i.ref.split("#")[1]) for i in got])

    def test_render_prints_the_section_only_when_asked(self):
        items = [item(1, day(1)), item(2, day(2))]
        self.assertNotIn(needs_you.BRAIN_HEADING, needs_you.render(items, []))
        text = needs_you.render(items, [], needs_you.brain_batch(items, TRUSTED, 1))
        head, _, rest = text.partition(needs_you.BRAIN_HEADING)
        self.assertIn(": 1\n", rest.splitlines(True)[0])
        self.assertIn("%s#1" % REPO, rest)
        self.assertNotIn("%s#2" % REPO, rest)

    def test_empty_batch_still_prints_the_section(self):
        text = needs_you.render([], [], [])
        self.assertIn("%s: nothing" % needs_you.BRAIN_HEADING, text)

    def test_option_default_and_flag(self):
        self.assertEqual(5, needs_you.BRAIN_BATCH)
        with self.assertRaises(SystemExit):
            needs_you.main(["--brain-batch", "-1"])


if __name__ == "__main__":
    unittest.main()
