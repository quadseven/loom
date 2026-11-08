import unittest

from bus import merge_verdict

BOT = "grug-tribe"
BOT_AUTHOR = {"__typename": "Bot", "login": BOT}
PERSON = {"__typename": "User", "login": "reviewer-one"}


def node(checks="SUCCESS", reviews=(), threads=()):
    """A pull request's ready facts: head committed at 12:45 on 2030-01-01."""
    return {"commits": {"nodes": [{"commit": {"committedDate": "2030-01-01T12:45:00Z",
                                              "statusCheckRollup": {"state": checks}}}]},
            "reviews": {"nodes": list(reviews)}, "reviewThreads": {"nodes": list(threads)}}


def review(author, state, at="2030-01-01T08:06:00Z"):
    return {"author": author, "state": state, "submittedAt": at}


def thread(resolved, author=BOT_AUTHOR, at="2029-01-01T00:00:00Z"):
    return {"isResolved": resolved,
            "root": {"nodes": [{"author": author, "createdAt": at}]}}


class ReadyVerdictTests(unittest.TestCase):
    def test_characterization_table(self):
        cases = [
            ({}, "blocked: checks unknown", ("lane",)),
            (node(), "READY", ()),
            (node(checks="FAILURE"), "blocked: checks failure", ("lane",)),
            (node(checks="PENDING", threads=[thread(False)]),
             "blocked: checks pending, 1 unresolved threads", ("lane",)),
            (node(reviews=[review(BOT_AUTHOR, "CHANGES_REQUESTED")]),
             "blocked: grug changes_requested", ("lane",)),
            (node(threads=[thread(False)] * 3), "blocked: 3 unresolved threads", ("lane",)),
            (node(threads=[thread(True, at="2030-01-01T13:00:00Z")]),
             "blocked: 1 grug findings with no fix-up commit after them", ("lane",)),
            (node(reviews=[review(PERSON, "CHANGES_REQUESTED")]),
             "blocked: changes requested by a person (reviewer-one)", ("operator",)),
            (node(reviews=[review(PERSON, "CHANGES_REQUESTED"),
                           review(PERSON, "DISMISSED", "2030-01-02T00:00:00Z")]),
             "READY", ()),
            (node(reviews=[review({"__typename": "User", "login": BOT},
                                  "CHANGES_REQUESTED")]),
             "blocked: changes requested by a person (grug-tribe)", ("operator",)),
            (node(reviews=[dict(review(PERSON, "CHANGES_REQUESTED"),
                                authorCanPushToRepository=False)]),
             "READY", ()),
        ]
        for facts, expected, actors in cases:
            with self.subTest(expected=expected):
                verdict = merge_verdict.ready_verdict(facts, BOT)
                self.assertEqual(verdict.text, expected)
                self.assertEqual(verdict.ready, expected == "READY")
                self.assertEqual(verdict.actors, actors)

    def test_blockers_keep_the_rule_order(self):
        verdict = merge_verdict.ready_verdict(
            node(checks="FAILURE", reviews=[review(BOT_AUTHOR, "CHANGES_REQUESTED")],
                 threads=[thread(False)]), BOT)
        self.assertEqual(verdict.blockers, ("checks failure", "grug changes_requested",
                                            "1 unresolved threads"))

    def test_ready_verdict_does_not_change_its_input(self):
        facts = node(threads=[thread(False)])
        before = repr(facts)
        merge_verdict.ready_verdict(facts, BOT)
        self.assertEqual(repr(facts), before)


class MergeVerdictTests(unittest.TestCase):
    def test_characterization_table(self):
        cases = [
            ({}, None, [], "READY", ()),
            ({"number": 7}, {"number": 3}, ["checks pending"],
             "blocked: checks pending", ("operator",)),
            ({"head": {"sha": "abc"}}, {"labels": ["loom:pr"]},
             ["conflict", "operator hold", "conflict"],
             "blocked: conflict, operator hold", ("operator", "brain")),
        ]
        for pr, ticket, blockers, expected, actors in cases:
            with self.subTest(expected=expected):
                verdict = merge_verdict.decide(pr, ticket, blockers, actors)
                self.assertEqual(verdict.text, expected)
                self.assertEqual(verdict.ready, expected == "READY")
                self.assertEqual(verdict.actors, tuple(dict.fromkeys(actors)))
                self.assertEqual(verdict.pull_request, pr)
                self.assertEqual(verdict.ticket, ticket)

    def test_input_facts_are_snapshotted(self):
        pr = {"number": 7}
        verdict = merge_verdict.decide(pr)
        pr["number"] = 8
        self.assertEqual(verdict.pull_request["number"], 7)


if __name__ == "__main__":
    unittest.main()
