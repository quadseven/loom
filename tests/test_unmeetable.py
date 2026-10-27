"""A Live box that can never be met as written is flagged (loom#2028)."""
import unittest

from bus import unmeetable

REPO = "example-owner/example-repo"


def states(table):
    """A state_of over {(repo, number): (state, merged)}; anything else unreadable."""
    return lambda repo, number: table.get((repo, number))


def tree(*held):
    """A present() over a source tree holding these strings."""
    return lambda text: any(text in line for line in held)


class ClosedReferenceTest(unittest.TestCase):
    def test_a_box_waiting_on_a_closed_issue_in_another_repo_is_flagged(self):
        live = ("Live: after deploy, muster#163 is verified through the new mechanism "
                "and the log shows `verify handed`")
        found = unmeetable.reasons(
            live, REPO, 964, states({("example-owner/muster", 163): ("closed", False)}),
            tree("verify handed"))
        self.assertEqual(len(found), 1)
        self.assertIn("example-owner/muster#163", found[0])
        self.assertIn("closed", found[0])

    def test_a_box_waiting_on_a_closed_unmerged_pull_request_is_flagged(self):
        live = "Live: within an hour of #6151 merging, the log prints `pin bumped`"
        found = unmeetable.closed_refs(live, REPO, 612,
                                       states({(REPO, 6151): ("closed", False)}))
        self.assertEqual(found, ["%s#6151" % REPO])

    def test_an_open_issue_a_merged_pull_request_or_an_unreadable_one_is_not(self):
        live = "Live: once #10 and #11 and #12 are done, the log prints `all done here`"
        found = unmeetable.closed_refs(live, REPO, 1, states({
            (REPO, 10): ("open", False), (REPO, 11): ("closed", True)}))
        self.assertEqual(found, [])

    def test_the_ticket_itself_and_a_url_reference(self):
        live = ("Live: #7 is shown and https://github.com/other-owner/thing/issues/3 "
                "prints `thing ready`")
        table = {(REPO, 7): ("closed", False), ("other-owner/thing", 3): ("closed", False)}
        self.assertEqual(unmeetable.closed_refs(live, REPO, 7, states(table)),
                         ["other-owner/thing#3"])


class LogStringTest(unittest.TestCase):
    def test_a_quoted_log_string_absent_from_the_source_tree_is_flagged(self):
        live = ('Live: the running pod\'s fetch-loom log contains "Dashboard revision updated" '
                "within one hour of a dashboard change being merged.")
        found = unmeetable.reasons(live, REPO, 612, states({}),
                                   tree('print("loom %s ready" % rev)'))
        self.assertEqual(len(found), 1)
        self.assertIn("`Dashboard revision updated`", found[0])

    def test_a_quoted_log_string_present_in_the_source_tree_is_not_flagged(self):
        live = 'Live: the fetch-loom log contains "Dashboard revision updated" within an hour.'
        self.assertEqual(unmeetable.reasons(
            live, REPO, 612, states({}),
            tree('log.info("Dashboard revision updated to %s", rev)')), [])

    def test_an_unknown_search_an_absence_and_a_wide_template_flag_nothing(self):
        self.assertEqual(unmeetable.missing_strings(
            "Live: the log prints `daily status ready`", lambda text: None), [])
        self.assertEqual(unmeetable.missing_strings(
            "Live: no `Traceback (most recent` line appears over 24h", tree()), [])
        self.assertEqual(unmeetable.missing_strings(
            "Live: the log prints `loom <rev> ready`", tree()), [])

    def test_a_template_is_searched_by_its_longest_fixed_part(self):
        seen = []
        unmeetable.missing_strings("Live: the log prints `sync: ... (waiting for peers)`",
                                   lambda text: seen.append(text) or True)
        self.assertEqual(seen, ["(waiting for peers)"])


class AskTest(unittest.TestCase):
    def test_the_ask_carries_a_marker_for_its_box_text_only(self):
        live = "Live: the log prints `a string`"
        text = unmeetable.ask(live, ["it waits for the log string `a string`"])
        self.assertTrue(unmeetable.asked([{"body": text}], live))
        self.assertTrue(unmeetable.asked([{"body": text}], "Live:  the log prints `a string` "))
        self.assertFalse(unmeetable.asked([{"body": text}], "Live: the log prints `b string`"))
        self.assertTrue(text.isascii())


if __name__ == "__main__":
    unittest.main()
