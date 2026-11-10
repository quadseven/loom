import json
import unittest

from bus import brain
from bus.github import GitHub


REPO = "%s/%s" % ("example-owner", "example-repo")
GOAL = 1
BATCH = 2
REPAIRS = 3


def issue(number, title, body, labels, state="open"):
    return {"number": number, "title": title, "body": body, "state": state,
            "labels": [{"name": label} for label in labels]}


class FullBatchWithRepairs:
    def __init__(self):
        self.issues = {
            GOAL: issue(GOAL, "Goal", "", ["loom:goal"]),
            BATCH: issue(BATCH, "Epic: batch", "Part of #1", ["epic"]),
            REPAIRS: issue(REPAIRS, brain.REPAIRS_TITLE, "Part of #1", ["epic"]),
        }
        self.subs = {BATCH: list(range(10, 10 + brain.EPIC_TICKET_CAP))}
        self.issues.update({n: issue(n, "Ticket", "", ["loom:ticket"])
                            for n in self.subs[BATCH]})

    def __call__(self, method, url, headers, body):
        path = url.split("api.github.com")[-1]
        prefix = "/repos/%s/issues" % REPO
        if method == "GET" and path == prefix + "/%d" % BATCH:
            return 200, {}, json.dumps(self.issues[BATCH]).encode()
        if method == "GET" and path == prefix + "/%d" % GOAL:
            return 200, {}, json.dumps(self.issues[GOAL]).encode()
        if method == "GET" and path.startswith(prefix + "/%d/sub_issues" % BATCH):
            return 200, {}, json.dumps([self.issues[n] for n in self.subs[BATCH]]).encode()
        if method == "GET" and path.startswith(prefix + "?labels=loom:ticket&state=open"):
            return 200, {}, b"[]"
        if method == "GET" and path.startswith(prefix + "?labels=epic&state=all"):
            return 200, {}, json.dumps([self.issues[BATCH], self.issues[REPAIRS]]).encode()
        raise AssertionError("unexpected request: %s %s" % (method, path))


class OpenEpicStandingRefusalTests(unittest.TestCase):
    def test_full_batch_refusal_names_open_standing_repairs_epic(self):
        gh = GitHub("token", transport=FullBatchWithRepairs())
        with self.assertRaisesRegex(ValueError, r"standing repairs epic #3"):
            brain.brief(gh, REPO, BATCH, "New ticket", "## Why\nThe request needs a small change.\n## What\nAdd it.\n"
                       "## Acceptance criteria\n- [ ] one\n- [ ] two\n- [ ] three\n"
                       "## Size\nXS\n## Out of scope\n- none", "codex")


if __name__ == "__main__":
    unittest.main()
