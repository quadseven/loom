"""The head approval stood aside for merge tests that pin other rules.

bus.brain merge needs an approval on the current head before it merges
(brain._refuse_unapproved_head, loom#3543). A test class whose fake GitHub
pins some other merge rule decorates itself with `head_approved`, so that
rule is read as if the review bot had approved the head:

    from head_approved import head_approved  # noqa: E402

    @head_approved
    class BrainMerge(unittest.TestCase): ...

tests/test_bus_brain.py HeadApprovalMergeTests runs the rule itself.
"""
from __future__ import annotations

from unittest import mock

from bus import brain


def head_approved(cls):
    return mock.patch.object(brain, "_refuse_unapproved_head",
                             new=lambda *args, **kwargs: None)(cls)
