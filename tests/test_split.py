"""bus/split.py: reading the review's split note and choosing the parts."""

import unittest

from bus import split

NOTE = ("Coverage: 1/2 cohorts completed.\n\n**Reviewability**\n"
        "- `cross-cohort-proof`: Related changes do not fit; split the pull request. "
        "Paths: `bus/a.py`, `tests/test_a.py`, `bus/b.py`")


class Notes(unittest.TestCase):
    def test_reads_kind_and_paths(self):
        self.assertEqual(split.notes(NOTE),
                         [("cross-cohort-proof", ["bus/a.py", "tests/test_a.py", "bus/b.py"])])

    def test_no_reviewability_section_has_no_notes(self):
        self.assertEqual(split.notes("- `cross-cohort-proof`: x. Paths: `a.py`"), [])

    def test_asks_split_only_without_a_blocking_finding(self):
        self.assertTrue(split.asks_split("WARN Elder review coverage partial", NOTE))
        self.assertTrue(split.asks_split("Elder markings - 0 blocking, 1 total", NOTE))
        self.assertFalse(split.asks_split("Elder markings - 2 blocking, 3 total", NOTE))

    def test_other_notes_do_not_ask_for_a_split(self):
        other = "**Reviewability**\n- `oversized-hunk`: One hunk is too big. Paths: `a.py`"
        self.assertFalse(split.asks_split("WARN Elder review coverage partial", other))


class Plan(unittest.TestCase):
    def test_a_file_and_its_test_share_a_part(self):
        parts = split.plan(NOTE, [])
        self.assertEqual([p["paths"] for p in parts],
                         [["bus/a.py", "tests/test_a.py"], ["bus/b.py"]])

    def test_a_single_stem_falls_back_to_criteria(self):
        note = "**Reviewability**\n- `cross-cohort-proof`: split. Paths: `a.py`, `test_a.py`"
        parts = split.plan(note, [" A1", "x A2", " A3", " Live: seen"])
        self.assertEqual([p["criteria"] for p in parts], [["A1"], ["A3"]])

    def test_nothing_to_split_on_is_none(self):
        note = "**Reviewability**\n- `cross-cohort-proof`: split. Paths: `a.py`"
        self.assertIsNone(split.plan(note, [" A1"]))

    def test_parts_are_capped(self):
        paths = ", ".join("`m%d.py`" % n for n in range(9))
        parts = split.plan("**Reviewability**\n- `cross-cohort-proof`: split. Paths: " + paths, [])
        self.assertEqual(len(parts), split.MAX_PARTS)
        self.assertEqual(sum(len(p["paths"]) for p in parts), 9)


class Body(unittest.TestCase):
    def test_a_part_passes_the_plan_checks(self):
        from bus import hunt_plan
        part = {"paths": ["bus/a.py"], "criteria": []}
        body = split.ticket_body(5, 50, ["Difficulty: medium"], part, 1, 2, 7, [" A1 x"])
        self.assertEqual(hunt_plan.problems(body), [])
        self.assertTrue(body.endswith("Part of #7\n"))

    def test_header_lines_and_epic(self):
        body = "Priority: P1\nDifficulty: easy\n\nPart of #9\n"
        self.assertEqual(split.header_lines(body), ["Difficulty: easy", "Priority: P1"])
        self.assertEqual(split.header_lines(""), ["Difficulty: medium"])
        self.assertEqual(split.epic_of(body, 3), 9)
        self.assertEqual(split.epic_of("x", 3), 3)


class Docs(unittest.TestCase):
    def test_brain_doc_names_the_split_path(self):
        import pathlib
        text = (pathlib.Path(__file__).resolve().parents[1] / "docs" / "BRAIN.md").read_text()
        self.assertIn("bus/split.py", text)
        self.assertIn("loom:split pr=N", text)
