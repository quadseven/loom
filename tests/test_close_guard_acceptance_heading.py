"""The completed-close guard counts boxes under any Acceptance heading."""
from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus.github import GitHub, refuse_incomplete_close  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

PATH = "/repos/example-owner/example-repo/issues/7"


def _gh(body):
    issue = {"body": body, "labels": []}
    return GitHub("placeholder", transport=lambda m, u, h, b:
                  (200, {}, json.dumps(issue).encode()))


class CloseGuardAcceptanceHeadingTests(unittest.TestCase):
    def test_any_acceptance_heading_blocks_completed_close(self):
        for heading in ("## Acceptance", "## Acceptance Criteria"):
            gh = _gh("## Why\nx\n\n%s\n\n- [ ] Prove it.\n" % heading)
            with self.assertRaisesRegex(ValueError, "unchecked acceptance criterion"):
                refuse_incomplete_close(gh, "PATCH", PATH, {"state": "closed"})
        done = _gh("## Acceptance\n\n- [x] Prove it.\n")
        refuse_incomplete_close(done, "PATCH", PATH, {"state": "closed"})
        open_box = _gh("## Acceptance\n\n- [ ] Prove it.\n")
        refuse_incomplete_close(open_box, "PATCH", PATH,
                                {"state": "closed", "state_reason": "not_planned"})


if __name__ == "__main__":
    unittest.main()
