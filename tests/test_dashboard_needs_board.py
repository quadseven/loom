"""The Needs-you board at the top of the dashboard: oldest first, an item
with no action written is shown as such, and the empty state."""
import unittest

from dashboard import needs_you

REPO = "example-owner/app"


def _item(number, created, ask, title="Task", labels=("needs:human",)):
    return {"repo": REPO, "number": number, "title": title, "created_at": created,
            "html_url": "https://github.example/%s/issues/%d" % (REPO, number),
            "body": ("## Ask\n%s\n" % ask) if ask else "", "labels": list(labels)}


def _entries(*items):
    return needs_you.entries(list(items), [], [REPO])


class BoardTests(unittest.TestCase):
    def test_oldest_first(self):
        found = _entries(_item(2, "2026-02-01T00:00:00Z", "Do the second thing."),
                         _item(1, "2026-01-01T00:00:00Z", "Do the first thing."),
                         _item(3, "2026-03-01T00:00:00Z", "Do the third thing."))
        html = needs_you.board(found)
        self.assertLess(html.index("first thing"), html.index("second thing"))
        self.assertLess(html.index("second thing"), html.index("third thing"))
        self.assertIn("Needs you (3)", html)

    def test_missing_action_is_shown_not_hidden(self):
        html = needs_you.board(_entries(_item(7, "2026-01-01T00:00:00Z", "")))
        self.assertIn("no action written", html)
        self.assertIn("%s#7" % REPO, html)

    def test_empty_state(self):
        html = needs_you.board([])
        self.assertIn("Nothing needs you right now.", html)
        self.assertNotIn("<table", html)

    def test_row_has_who_unblocks_age_and_link(self):
        ask = "Run the one command.\nUnblocks: the nightly report"
        html = needs_you.board(_entries(_item(4, "2026-01-01T00:00:00Z", ask, title="Needs Pat: run it")))
        self.assertIn("<td>Pat</td>", html)
        self.assertIn("Run the one command.", html)
        self.assertIn("the nightly report", html)
        self.assertIn('href="https://github.example/%s/issues/4"' % REPO, html)
        self.assertNotIn("age unknown", html)

    def test_unblocks_not_stated(self):
        html = needs_you.board(_entries(_item(5, "2026-01-01T00:00:00Z", "Do it.")))
        self.assertIn("not stated", html)


if __name__ == "__main__":
    unittest.main()
