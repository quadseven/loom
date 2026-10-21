"""The Tree page (loom#1257): every open issue as goal, epic, child, with what
Loom is doing on each, and filters that run in the page."""
import re
import unittest

from dashboard import tree
from dashboard.server import _tree
from dashboard.shell import VIEWS

REPO = "octo/app"
GOAL = REPO + "#10"
EPIC_A = REPO + "#20"
EPIC_B = REPO + "#21"


def _ref(number):
    return "%s#%d" % (REPO, number)


def _row(number, title, state, epic=EPIC_A, lane=None, labels=None, **extra):
    labels = (["loom:ticket", "loom:" + state] if state else []) + (labels or [])
    return dict({"repo": REPO, "number": number, "title": title, "state": state, "goal": GOAL if epic else None,
                 "epic": epic, "lane": lane,
                 "html_url": "https://github.com/%s/issues/%d" % (REPO, number), "open": True, "labels": labels,
                 "updated_at": "2026-10-17T10:00:00Z"}, **extra)


class State:
    stale = False
    context = {}
    waits = {}

    def __init__(self, rows):
        self.data = {REPO: list(rows)}

    def response(self):
        epics = [{"ref": EPIC_A, "title": "Epic: Faster index", "open": True, "tickets": 2, "in_flight": 1},
                 {"ref": EPIC_B, "title": "Epic: Safer tokens", "open": True, "tickets": 1, "in_flight": 0}]
        return {"tickets": self.data, "pull_requests": [], "issue_comments": [], "state_entered": {},
                "latest_runs": {}, "goal_context": {GOAL: {"title": "Goal: Make the app better", "epics": epics}}}


def _rows():
    return [_row(31, "Cache the index", "running", lane="codex"),
            _row(32, "Trim the payload", "queued"),
            _row(41, "Check the token", "pr", epic=EPIC_B, lane="claude", pr={"number": 77, "url": "https://example.com/pull/77"}),
            _row(50, "Stray cleanup", "queued", epic=None),
            _row(51, "Needs a decision", "human", epic=None),
            _row(52, "Not in the factory", None, epic=None, labels=["bug"]),
            _row(33, "Already closed", "queued", open=False)]


def _render():
    return _tree(State(_rows()))


def _item(body, number):
    found = re.findall(r'<li class="tree-row"[^>]*>(?:(?!</li>).)*?%s\b.*?</li>' % re.escape(_ref(number)), body, re.S)
    return found


class TreePageTest(unittest.TestCase):
    def test_each_child_renders_once_under_its_epic_with_state_and_lane(self):
        body = _render()
        epics = re.findall(r'<details class="tree-box tree-epic".*?</details>', body, re.S)
        self.assertEqual(len(epics), 2)
        first = next(e for e in epics if "Faster index" in e)
        second = next(e for e in epics if "Safer tokens" in e)
        for number in (31, 32):
            self.assertEqual(len(_item(body, number)), 1)
            self.assertIn(_ref(number), first)
        self.assertIn(_ref(41), second)
        self.assertNotIn(_ref(41), first)
        running = _item(body, 31)[0]
        self.assertIn("running", running)
        self.assertIn("codex", running)
        self.assertIn('href="https://github.com/octo/app/issues/31"', running)
        self.assertIn("#77", _item(body, 41)[0])
        self.assertIn("2 open / 0 done / 2 in flight", first)
        self.assertNotIn("Already closed", body)

    def test_open_issues_under_no_epic_get_a_per_repo_group(self):
        body = _render()
        group = re.search(r'<details class="tree-box tree-orphans".*?</details>', body, re.S).group(0)
        self.assertIn("under no epic", group.split("</summary>")[0])
        self.assertIn("octo/app", group.split("</summary>")[0])
        for number in (50, 51, 52):
            self.assertIn(_ref(number), group)
            self.assertEqual(len(_item(body, number)), 1)

    def test_being_worked_filter_shows_only_running_and_pr_children(self):
        body = _render()
        self.assertIn('data-tree-filter="worked"', body)
        kinds = {int(re.search(r"#(\d+)", item).group(1)): set(kind.split())
                 for kind, item in re.findall(r'<li class="tree-row" data-kinds="([^"]*)"[^>]*>(.*?)</li>', body, re.S)}
        worked = {n for n, k in kinds.items() if "worked" in k}
        self.assertEqual(worked, {31, 41})
        self.assertEqual({n for n, k in kinds.items() if "operator" in k}, {51})
        self.assertEqual({n for n, k in kinds.items() if "unlabeled" in k}, {52})
        self.assertIn("<script>", body)
        self.assertNotIn("<script src", body.split("<main")[1])

    def test_navigation_links_the_page_next_to_goals_and_tickets(self):
        keys = [key for key, _, _ in VIEWS]
        self.assertEqual(keys[keys.index("goals") + 1:keys.index("goals") + 3], ["tree", "tickets"])
        self.assertIn('href="/tree"', _render())

    def test_text_is_escaped(self):
        rows = [_row(60, "<script>alert(1)</script>", "queued")]
        body = tree.render(rows, {}, entered={}, now=0, ticket_href=lambda r: "/t/x", short_ref=lambda s: s)
        self.assertNotIn("<script>alert", body)


if __name__ == "__main__":
    unittest.main()
