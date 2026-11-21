"""_set_model checks the Model line it writes, not text already on the ticket."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain  # noqa: E402

REPO = "example-owner/example-repo"


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def rest(self, method: str, path: str, payload: dict[str, object]) -> None:
        self.calls.append((method, path, payload))


class SetModelExistingTextTest(unittest.TestCase):
    def test_a_path_already_on_the_ticket_does_not_block_the_model(self) -> None:
        body = "Priority: P2\n\n## Why\nThe config lives at /etc/example/resolv.conf.\n"
        gh = Recorder()
        brain._set_model(gh, REPO, 7, {"body": body}, "gpt-6-luna")
        self.assertEqual(len(gh.calls), 1)
        method, path, payload = gh.calls[0]
        self.assertEqual((method, path), ("PATCH", "/repos/%s/issues/7" % REPO))
        sent = str(payload["body"])
        self.assertIn("Model: gpt-6-luna", sent)
        self.assertIn("/etc/example/resolv.conf", sent)

    def test_a_model_name_that_is_a_refused_string_is_still_refused(self) -> None:
        gh = Recorder()
        with self.assertRaises(ValueError):
            brain._set_model(gh, REPO, 7, {"body": "## Why\nx\n"},
                             "/etc/example/model")
        self.assertEqual(gh.calls, [])


if __name__ == "__main__":
    unittest.main()
