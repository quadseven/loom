import unittest
import json
from unittest.mock import patch

from bus import brain
from bus.github import GitHub


TICKET = ("## Acceptance criteria\n"
          "- [ ] The code change is complete.\n"
          "- [ ] Live: the deployed service responds.\n")
PR = {"number": 9, "title": "Work", "body": "## Acceptance criteria\n"
      "- [ ] The code change is complete.\n"}
TICKET_ISSUE = {"state": "open", "body": TICKET,
                "labels": [{"name": "loom:ticket"}, {"name": "loom:pr"}]}


class Transport:
    def __call__(self, method, url, headers, body):
        if "/issues/7" in url:
            return 200, {}, json.dumps(TICKET_ISSUE).encode()
        return 200, {}, b'{"mergeable": true, "mergeable_state": "clean"}'


class ReadyUntickedBoxesTests(unittest.TestCase):
    def verdict(self, pr_body):
        pr = dict(PR, body=pr_body)
        gh = GitHub("unused", transport=Transport())
        with patch.object(brain, "_status_pr_line", return_value="- #9 Work - READY"), \
                patch.object(brain, "_safe_ticket_association", return_value=("example/repo", 7)), \
                patch.object(brain, "_docs_criterion_override", side_effect=lambda gh, repo, ticket, line: line), \
                patch.object(brain, "_stacked_base_blocker", return_value=""), \
                patch.object(brain, "_ticket_refusal", return_value=""), \
                patch.object(brain, "_risk_tier", return_value=("app-code", None)), \
                patch.object(brain, "_repo_cap_refusal", return_value=""):
            return brain._pull_request_line(gh, "example/repo", pr,
                                            {"prs": {}}, "grug-tribe", (),
                                            "Grug - Elder", hold=[])

    def test_code_box_left_open_blocks_ready_verdict(self):
        line = self.verdict(PR["body"])
        self.assertIn("blocked:", line)
        self.assertIn("criteria not ticked", line)

    def test_live_box_alone_does_not_block_ready_verdict(self):
        body = ("## Acceptance criteria\n"
                "- [x] The code change is complete.\n"
                "- [ ] Live: the deployed service responds.\n")
        self.assertTrue(self.verdict(body).endswith("READY"))


if __name__ == "__main__":
    unittest.main()
