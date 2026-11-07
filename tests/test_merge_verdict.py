import unittest

from bus import merge_verdict


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

    def test_node_verdict_matches_brain_merge_ready(self):
        bot = {"__typename": "Bot", "login": "grug-tribe"}

        def ready_node(checks="SUCCESS", reviews=(), threads=()):
            return {"commits": {"nodes": [{"commit": {"committedDate": "2030-01-01T12:45:00Z",
                                                       "statusCheckRollup": {"state": checks}}}]},
                    "reviews": {"nodes": list(reviews)},
                    "reviewThreads": {"nodes": list(threads)}}

        person = {"__typename": "User", "login": "reviewer-one"}
        cases = [
            ({}, "blocked: checks unknown"),
            (ready_node(), "READY"),
            (ready_node(checks="FAILURE"), "blocked: checks failure"),
            (ready_node(reviews=[{"author": bot, "state": "CHANGES_REQUESTED",
                                  "submittedAt": "2030-01-01T08:06:00Z"}]),
             "blocked: grug changes_requested"),
            (ready_node(threads=[{"isResolved": False}] * 3),
             "blocked: 3 unresolved threads"),
            (ready_node(threads=[{"isResolved": True,
                                 "root": {"nodes": [{"author": bot,
                                                   "createdAt": "2030-01-01T13:00:00Z"}]}}]),
             "blocked: 1 grug findings with no fix-up commit after them"),
            (ready_node(reviews=[{"author": {"__typename": "User", "login": "grug-tribe"},
                                  "state": "CHANGES_REQUESTED",
                                  "submittedAt": "2030-01-01T08:06:00Z"}]),
             "blocked: changes requested by a person (grug-tribe)"),
            (ready_node(checks="PENDING", threads=[{"isResolved": False}]),
             "blocked: checks pending, 1 unresolved threads"),
            (ready_node(reviews=[{"author": person, "state": "CHANGES_REQUESTED",
                                  "submittedAt": "2030-01-01T09:00:00Z"}]),
             "blocked: changes requested by a person (reviewer-one)"),
        ]
        for node, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(merge_verdict.decide_for_node(node, "grug-tribe").text,
                                 expected)
                self.assertEqual(merge_verdict.ready_blockers(node, "grug-tribe") == [],
                                 expected == "READY")

    def test_node_verdict_combines_caller_blockers(self):
        verdict = merge_verdict.decide_for_node(
            {"commits": {"nodes": [{"commit": {"committedDate": "2030-01-01T12:45:00Z",
                                             "statusCheckRollup": {"state": "SUCCESS"}}}]},
             "reviews": {"nodes": []}, "reviewThreads": {"nodes": []}},
            "grug-tribe", ticket={"number": 3},
            blockers=["held for the operator: security"], actors=["operator"])
        self.assertEqual(verdict.text, "blocked: held for the operator: security")
        self.assertEqual(verdict.actors, ("operator",))
        self.assertEqual(verdict.ticket, {"number": 3})

    def test_node_verdict_deduplicates_extra_blockers(self):
        verdict = merge_verdict.decide_for_node(
            {"commits": {"nodes": [{"commit": {"committedDate": "2030-01-01T12:45:00Z",
                                             "statusCheckRollup": {"state": "PENDING"}}}]}},
            "grug-tribe", blockers=["checks pending", "held"])
        self.assertEqual(verdict.text, "blocked: checks pending, held")


if __name__ == "__main__":
    unittest.main()
