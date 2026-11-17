import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain
from bus.github import GitHub
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401


class Routes:
    def __init__(self, issue):
        self.issue = issue
        self.calls = []

    def __call__(self, method, url, headers, body):
        path = url.split("api.github.com")[-1]
        payload = json.loads(body) if body else None
        self.calls.append((method, path, payload))
        if method == "GET" and path.startswith("/repos/example-owner/example-repo/issues?"):
            return 200, {}, json.dumps([self.issue]).encode()
        if method == "POST":
            return 201, {}, b"{}"
        raise AssertionError("unexpected %s %s" % (method, path))


class UntrustedTriageAuthorTest(unittest.TestCase):
    def test_ready_issue_by_untrusted_author_stays_in_triage(self):
        number = 257
        body = ("## Why\nUsers need this change for reliable work.\n## What\nImplement it.\n"
                "## Acceptance criteria\n- [ ] Works\n- [ ] Tested\n- [ ] Live\n"
                "## Size\nS\n## Out of scope\n- None")
        issue = {"number": number, "id": 1257, "state": "open", "body": body,
                 "user": {"login": "outside-author"}, "labels": []}
        routes = Routes(issue)
        with mock.patch.dict(os.environ, {"LOOM_BRIEF_AUTHORS": "trusted-author",
                                         "LOOM_TRUSTED_BRIEFERS": "trusted-briefer"}), \
                mock.patch.object(brain, "_goal_in_flight", return_value=0), \
                mock.patch.object(brain, "goal_cap", return_value=10), \
                mock.patch.object(brain, "goal_journals", return_value=set()):
            lines = brain.triage(GitHub("token", transport=routes),
                                 "example-owner/example-repo", 1)

        label_writes = [call[2]["labels"] for call in routes.calls
                        if call[0] == "POST" and call[1].endswith("/labels")]
        self.assertEqual(label_writes, [["loom:triage"]])
        self.assertNotIn("loom:ticket", sum(label_writes, []))
        self.assertNotIn("loom:queued", sum(label_writes, []))
        self.assertFalse(any(call[1].endswith("/sub_issues") for call in routes.calls))
        self.assertEqual(lines, ["#257 loom:triage, untrusted author"])


if __name__ == "__main__":
    unittest.main()
