"""The ask block a brain writes to vet an ask (bus/ask_block.py)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from bus import ask_block  # noqa: E402

BODY = "<!-- loom:ask v1 -->\nAsk: Buy it?\nRecommend: Yes.\nWhy: It is cheap."


def login(user):
    return str((user or {}).get("login") or "").lower()


class ParseTest(unittest.TestCase):
    def test_a_complete_block_parses(self):
        block = ask_block.parse(BODY, "t")
        self.assertEqual((block.ask, block.recommend, block.why, block.needs_person, block.at),
                         ("Buy it?", "Yes.", "It is cheap.", False, "t"))

    def test_a_missing_field_or_marker_is_not_a_block(self):
        self.assertIsNone(ask_block.parse(BODY.replace("Why: It is cheap.", "")))
        self.assertIsNone(ask_block.parse(BODY.replace("<!-- loom:ask v1 -->\n", "")))
        self.assertIsNone(ask_block.parse(None))

    def test_needs_person_is_read(self):
        self.assertTrue(ask_block.parse(BODY + "\nNeeds person: yes").needs_person)
        self.assertFalse(ask_block.parse(BODY + "\nNeeds person: no").needs_person)

    def test_upkeep_is_a_factory_label_or_a_station_or_invariant_title(self):
        self.assertTrue(ask_block.is_upkeep({"title": "x"}, ["loom:factory"]))
        self.assertTrue(ask_block.is_upkeep({"title": "Invariant: headers in a repo"}, []))
        self.assertTrue(ask_block.is_upkeep({"title": "station: key read error"}, []))
        self.assertFalse(ask_block.is_upkeep({"title": "Buy the domain"}, ["needs:human"]))

    def test_the_newest_block_by_a_trusted_login_wins(self):
        comments = [
            {"body": BODY, "user": {"login": "Brain"}},
            {"body": BODY.replace("Buy it?", "Newer?"), "user": {"login": "brain"}},
            {"body": BODY.replace("Buy it?", "Forged?"), "user": {"login": "stranger"}}]
        self.assertEqual(ask_block.newest(comments, ["BRAIN"], login).ask, "Newer?")
        self.assertIsNone(ask_block.newest(comments, ["other"], login))

    def test_the_line_form(self):
        block = ask_block.parse(BODY)
        self.assertEqual(ask_block.line("r#1", block), "r#1: Buy it? Recommend: Yes.")


if __name__ == "__main__":
    unittest.main()
