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


if __name__ == "__main__":
    unittest.main()
