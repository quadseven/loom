"""The Backlog page is one dense table of epics, with every open ticket that
sits outside an epic listed first as an incoherence (loom#607).

Live, the page was one tall card per epic (161 of them), each reading
"Completeness: 0/0" because the Hunt agent view carries `closed` and `total`
counts, not a `children` list. These pin the table, the outside-any-epic list
and the collapsed groups.
"""
import re
import unittest
from urllib.parse import urlsplit

from dashboard import server

REPO = "example-owner/app"


def epic(number, score, *, closed=1, total=3, tier="now", repo="app", next_child=None, open_children=()):
    return {"repo": repo, "number": number, "title": "Epic %d" % number,
            "url": "https://github.com/%s/issues/%d" % (REPO, number),
            "closed": closed, "total": total, "tier": tier, "score": score,
            "next": next_child, "open_children": [{"number": n, "title": "child %d" % n} for n in open_children]}


class State:
    def __init__(self, rows):
        self.rows = rows

    @property
    def data(self):
        return {REPO: self.rows}

    def response(self):
        return {"tickets": self.data, "pull_requests": []}


def render(document, rows=(), query=""):
    return server._backlog_page(document, urlsplit("/backlog" + query), State(list(rows)))


def ranked_rows(body):
    tables = re.findall(r'<table class="epic-table">.*?</table>', body, re.S)
    return [row for table in tables for row in re.findall(r"<tbody>(.*?)</tbody>", table, re.S)[0].split("</tr>") if row]


class EpicTable(unittest.TestCase):
    def test_one_table_row_per_epic_ranked_by_score_and_no_card(self):
        document = {"epics": [
            epic(10, 12.5, next_child={"number": 11, "title": "Do the small thing",
                                        "url": "https://github.com/%s/issues/11" % REPO}),
            epic(20, 99.0, tier=None),
            epic(30, 40.0, closed=0, total=2),
        ]}
        body = render(document)
        rows = ranked_rows(body)
        self.assertEqual(len(rows), 3)
        self.assertEqual([re.search(r"Epic (\d+)", row).group(1) for row in rows], ["20", "30", "10"])
        self.assertNotIn("backlog-epic", body)
        self.assertNotIn("<article", body)
        self.assertNotIn("Completeness:", body)
        self.assertNotIn("Open children:", body)
        first = rows[2]
        self.assertIn('href="https://github.com/example-owner/app/issues/11"', first)
        self.assertIn("#11 Do the small thing", first)
        self.assertIn("2 open / 1 done", first)
        self.assertIn('aria-label="1 of 3 children done, 2 open"', first)
        self.assertIn("unjudged", rows[0])
        self.assertIn("Age shows a dash", body)

    def test_empty_epics_are_collapsed_not_ranked(self):
        document = {"epics": [epic(10, 5.0), epic(40, -1.0, closed=0, total=0)]}
        body = render(document)
        self.assertEqual(len(ranked_rows(body)), 1)
        group = re.search(r'<details class="backlog-group"><summary>Empty epics \(1\).*?</details>', body, re.S)
        self.assertIsNotNone(group)
        self.assertIn("Epic 40", group.group(0))

    def test_ranks_past_the_first_screen_fold_into_a_collapsed_group(self):
        document = {"epics": [epic(n, float(n)) for n in range(1, server.RANKED_SHOWN + 6)]}
        body = render(document)
        self.assertEqual(len(ranked_rows(body)), server.RANKED_SHOWN + 5)
        self.assertIn("Ranks %d to %d (5 more)" % (server.RANKED_SHOWN + 1, server.RANKED_SHOWN + 5), body)
        first_table = body[:body.index("Ranks %d" % (server.RANKED_SHOWN + 1))]
        self.assertIn("Epic %d" % (server.RANKED_SHOWN + 5), first_table)
        self.assertNotIn(">Epic 1<", first_table)

    def test_tier_filter_still_narrows_the_table(self):
        document = {"epics": [epic(10, 5.0, tier="now"), epic(20, 9.0, tier="later")]}
        rows = ranked_rows(render(document, query="?tier=now"))
        self.assertEqual(len(rows), 1)
        self.assertIn("Epic 10", rows[0])


class OutsideAnyEpic(unittest.TestCase):
    def ticket(self, number, **extra):
        return {"repo": REPO, "number": number, "title": "Ticket %d" % number, "state": "queued", **extra}

    def section(self, body):
        self.assertIn("Outside any epic", body)
        self.assertIn("Epics by score", body)
        return body[body.index("Outside any epic"):body.index("Epics by score")]

    def test_an_open_ticket_with_no_epic_is_listed_and_one_under_an_epic_is_not(self):
        # loom#639: the record's epic decides, so a ticket that names only its
        # goal home (no epic) is outside any epic too (decision 0003).
        document = {"epics": [epic(10, 5.0, open_children=(12,), next_child={"number": 13, "title": "next"})]}
        rows = [self.ticket(11),
                self.ticket(12),
                self.ticket(13),
                self.ticket(14, goal=REPO + "#30"),
                self.ticket(15, state="merged"),
                self.ticket(16, open=False, state="failed"),
                self.ticket(17, goal=REPO + "#30", epic=REPO + "#40")]
        section = self.section(render(document, rows))
        self.assertIn('href="/t/example-owner/app/11"', section)
        for number in (11, 14):
            self.assertIn("Ticket %d" % number, section)
        for number in (12, 13, 15, 16, 17):
            self.assertNotIn("Ticket %d" % number, section)
        self.assertIn('<span class="machine-fact bad">2</span>', section)
        self.assertIn("incoherence", section)
        self.assertIn("open loom:ticket issues only", section)

    def test_the_section_comes_before_the_ranking(self):
        body = render({"epics": [epic(10, 5.0)]}, [self.ticket(11)])
        self.assertLess(body.index("Outside any epic"), body.index('<table class="epic-table">'))

    def test_every_open_ticket_in_an_epic_reads_as_the_empty_state(self):
        document = {"epics": [epic(10, 5.0, open_children=(11,))]}
        section = self.section(render(document, [self.ticket(11), self.ticket(
            12, goal=REPO + "#30", epic=REPO + "#40")]))
        self.assertIn('<p class="empty-state">Every open ticket is in an epic.</p>', section)

    def test_the_repo_filter_narrows_the_outside_list(self):
        document = {"epics": [epic(10, 5.0), epic(20, 5.0, repo="other")]}
        rows = [self.ticket(11), {"repo": "example-owner/other", "number": 21, "title": "Ticket 21", "state": "queued"}]
        section = self.section(render(document, rows, query="?repo=other"))
        self.assertIn("Ticket 21", section)
        self.assertNotIn("Ticket 11", section)


if __name__ == "__main__":
    unittest.main()
