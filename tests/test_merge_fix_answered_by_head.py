"""Merge does not wait on a brain fix the brain answered with its own push
(bus/brain.py _refuse_pending_fix)."""
from __future__ import annotations

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bus import brain, tickets  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

REPO = "example/loom"
ENV = {"LOOM_BRIEF_AUTHORS": "alice-example"}
ASKED = "2030-01-01T00:00:00Z"


def fix_comment(head="dec0de"):
    body = "%s\nPull request: %s#8, branch loom/9-work\n%s\n\n- Check `lint` failed\n" % (
        tickets.FIX_MARKER, REPO, brain.BRAIN_FIX_MARKER)
    return {"user": {"login": "alice-example"}, "created_at": ASKED,
            "body": body + "<!-- loom:fix-head %s -->\n" % head}


def answer(at="2030-01-01T01:00:00Z", login="alice-example"):
    return {"user": {"login": login}, "created_at": at, "body": "Pushed the fix."}


def details(head="feed01", committed="2030-01-01T00:30:00Z"):
    return {"headRefOid": head, "reviewThreads": {"totalCount": 0, "nodes": []},
            "commits": {"nodes": [{"commit": {"committedDate": committed,
                                              "parents": {"totalCount": 1}}}]}}


def refuse(comments, found):
    gh = types.SimpleNamespace(paginate=lambda path: iter(comments))
    return brain._refuse_pending_fix(gh, REPO, 8, (REPO, 9), found, ENV)


class FixAnsweredByHeadTests(unittest.TestCase):
    def pending(self, comments, found):
        with self.assertRaisesRegex(ValueError, "brain fix is pending"):
            refuse(comments, found)

    def test_a_fix_the_brain_answered_with_a_newer_head_does_not_refuse(self):
        self.assertIsNone(refuse([fix_comment(), answer()], details()))
        # Same method: every case that must keep refusing, so none is a
        # separate test that passes on main.
        self.pending([fix_comment()], details())  # no comment after the marker
        self.pending([fix_comment(), answer()], details(head="dec0de"))  # head unchanged
        self.pending([fix_comment(), answer()],
                     details(committed="2029-12-31T23:00:00Z"))  # commit older than ask
        self.pending([fix_comment(), answer(login="mallory-example")], details())
        self.pending([fix_comment(), answer()], details(committed=""))  # time unknown


if __name__ == "__main__":
    unittest.main()
