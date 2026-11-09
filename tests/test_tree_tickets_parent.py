"""A stale Part of line does not put a ticket under an epic GitHub says it
is not under.

On 2026-11-07 the station reported a closed epic as holding 2 open tickets
(loom#2767). Both were sub-issues of an open epic; only a "Part of #53" line
left in their bodies named the closed one. The goal walk counted every open
ticket whose Part of line named an epic, whatever its sub-issue parent, so
the closed-epic invariant fired on work that was not stranded.
"""
import unittest

from bus import brain

PARENT = "https://api.github.com/repos/example-owner/example-repo/issues/"


def ticket(number, body, parent=None):
    issue = {"number": number, "state": "open", "body": body,
             "labels": [{"name": "loom:ticket"}]}
    if parent is not None:
        issue["parent_issue_url"] = PARENT + str(parent)
    return issue


class AStalePartOfLine(unittest.TestCase):
    def test_a_sub_issue_of_another_epic_is_not_counted_under_the_epic_its_body_names(self):
        stale = ticket(27, "Part of #53\n", parent=11)
        self.assertEqual(brain._tree_tickets(53, [], [stale]), [])
        self.assertEqual(brain._tree_tickets(11, [stale], [stale]), [stale])

    def test_a_ticket_with_no_sub_issue_parent_still_counts_by_its_part_of_line(self):
        loose = ticket(28, "Part of #53\n")
        self.assertEqual(brain._tree_tickets(53, [], [loose]), [loose])

    def test_a_ticket_whose_parent_is_the_epic_is_counted_once(self):
        child = ticket(29, "Part of #53\n", parent=53)
        self.assertEqual(brain._tree_tickets(53, [child], [child]), [child])
        self.assertEqual(brain._tree_tickets(53, [], [child]), [child])

    def test_a_sub_issue_listed_under_an_epic_but_parented_elsewhere_is_not_counted(self):
        stray = ticket(30, "Part of #53\n", parent=11)
        self.assertEqual(brain._tree_tickets(53, [stray], []), [])
        self.assertEqual(brain._tree_tickets(11, [stray], [stray]), [stray])


if __name__ == "__main__":
    unittest.main()
