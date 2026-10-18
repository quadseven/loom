"""Owner chips (loom#785): a /tickets row and a ticket header name the other
operator that holds a ticket, by the rule the station uses
(bus.tickets.owned_elsewhere), and show claims neutrally when the dashboard
does not know this Loom's logins."""
import os
import time
import unittest
from unittest import mock

from bus import tickets
from dashboard import operators, ticket as ticket_view, tickets_table
from dashboard.github import pull_request_record, ticket_record

REPO = "example-owner/app"
LOOM = "loom-bot"
OTHER = "other-operator"


def _issue(number, labels=(), assignees=(), author=LOOM):
    return {"number": number, "title": "Ticket %d" % number, "state": "open",
            "html_url": "https://github.com/%s/issues/%d" % (REPO, number),
            "body": "Part of #30\n", "user": {"login": author, "type": "User"},
            "labels": [{"name": "loom:ticket"}, {"name": "loom:queued"}]
            + [{"name": name} for name in labels],
            "assignees": [{"login": login} for login in assignees],
            "updated_at": "2026-10-17T10:00:00Z"}


def _table(rows):
    return tickets_table.render(rows, {}, goal_titles={}, entered={}, now=time.time(),
                                ticket_href=lambda row: "/t/%s/%s" % (row["repo"], row["number"]),
                                short_ref=lambda ref: str(ref))


class RecordsKeepOwnersTest(unittest.TestCase):
    def test_ticket_record_keeps_assignees_and_author(self):
        row = ticket_record(REPO, _issue(1, ["claim:" + OTHER], [OTHER.upper()], author="Some-Author"))
        self.assertEqual(row["assignees"], [OTHER])
        self.assertEqual(row["author"], "some-author")
        self.assertIn("claim:" + OTHER, row["labels"])

    def test_pull_request_record_keeps_assignees_and_labels(self):
        pr = pull_request_record({"number": 9, "user": {"login": OTHER, "type": "User"},
                                  "assignees": [{"login": OTHER, "id": 1, "avatar_url": "x"}],
                                  "labels": [{"name": "claim:" + OTHER, "color": "5319e7"}]})
        self.assertEqual(pr["assignees"], [OTHER])
        self.assertEqual(pr["labels"], ["claim:" + OTHER])


class OwnerChipTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"LOOM_BRIEF_AUTHORS": LOOM, "LOOM_TRUSTED_BRIEFERS": ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_tickets_row_names_the_other_operator(self):
        page = _table([ticket_record(REPO, _issue(1, ["claim:" + OTHER], [OTHER]))])
        self.assertIn('class="owner-chip"', page)
        self.assertIn("held by %s" % OTHER, page)

    def test_this_looms_own_claim_shows_no_chip(self):
        mine = ticket_record(REPO, _issue(2, ["claim:" + LOOM], [LOOM]))
        # One owner among this Loom's logins makes it this Loom's (the
        # station's rule), even beside another operator's label.
        shared = ticket_record(REPO, _issue(3, ["claim:" + OTHER], [LOOM]))
        page = _table([mine, shared])
        self.assertNotIn("owner-chip", page)

    def test_ticket_header_names_the_other_operator(self):
        record = ticket_record(REPO, _issue(4, [], [OTHER]))
        body = ticket_view.render(record, [], None, [])
        header = body.split('<div class="state-strip"')[0]
        self.assertIn("held by %s" % OTHER, header)

    def test_chip_agrees_with_the_station_rule(self):
        fixtures = [_issue(10), _issue(11, ["claim:" + OTHER]), _issue(12, [], [OTHER]),
                    _issue(13, ["claim:" + LOOM], [OTHER]), _issue(14, ["claim:third"], ["fourth"]),
                    _issue(15, ["claim:" + OTHER], [], author=OTHER)]
        for issue in fixtures:
            expected = tickets.owned_elsewhere(issue, [LOOM])
            found = operators.owners(ticket_record(REPO, issue))
            self.assertEqual([o["login"] for o in found], [expected] if expected else [], issue["number"])


class NeutralChipTest(unittest.TestCase):
    def test_without_brief_authors_every_claim_label_is_a_neutral_chip(self):
        with mock.patch.dict(os.environ, {"LOOM_BRIEF_AUTHORS": ""}):
            page = _table([ticket_record(REPO, _issue(1, ["claim:" + LOOM], [LOOM])),
                           ticket_record(REPO, _issue(2, [], [OTHER]))])
        self.assertIn('class="owner-chip neutral"', page)
        self.assertIn("claim:%s</span>" % LOOM, page)
        self.assertIn("cannot tell", page)
        # An assignee alone is not shown: whose Loom it is cannot be told.
        self.assertEqual(page.count("owner-chip"), 1)
        self.assertNotIn("held by", page)


if __name__ == "__main__":
    unittest.main()
