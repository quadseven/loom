"""The improving page reads the slowdown lines the brains actually write
(the parser itself is tested in test_bus_friction.py)."""
import unittest

from dashboard.views import build_improvements, friction_lines

JOURNAL_URL = "https://github.com/example-owner/sample/issues/30#issuecomment-9001"


def journal(body, created="2030-01-02T00:00:00Z", url=JOURNAL_URL, comment_id=9001):
    return {"body": "<!-- loom:journal v1 -->\nTick %s, muse\n%s" % (created, body),
            "created_at": created, "html_url": url, "id": comment_id}


class FrictionCardTest(unittest.TestCase):
    def test_the_page_reads_journals_with_the_bus_parser(self):
        from bus import friction
        self.assertIs(friction_lines, friction.friction_lines)

    def test_the_page_builds_a_card_for_a_slowdown_note(self):
        data = build_improvements([journal("- What slowed the factory: resume took six minutes.")], [], [])
        self.assertEqual([row["friction"] for row in data["records"]], ["resume took six minutes."])
        self.assertEqual(data["records"][0]["stage"], 0)


class NoticedInTest(unittest.TestCase):
    def ticket(self, number, why):
        return {"repo": "example-owner/sample", "number": number, "title": "t%d" % number,
                "state": "queued", "refs": [],
                "body": "Difficulty: easy\n\n## Why\n%s\n\n## What\nx\n" % why}

    def test_a_noticed_in_line_with_a_quote_tickets_only_that_friction(self):
        entry = journal("- Friction: resume takes six minutes over eighteen repos\n"
                        "- Friction: the leak scan log needs escape sequences to read")
        tickets = [self.ticket(5, "The tick budget runs out early.\n\nNoticed in %s: "
                               "\"resume takes six minutes over eighteen repos\"" % JOURNAL_URL)]
        data = build_improvements([entry], tickets, [])
        by_text = {row["friction"]: row for row in data["records"]}
        self.assertIs(by_text["resume takes six minutes over eighteen repos"]["ticket"], tickets[0])
        self.assertEqual(by_text["resume takes six minutes over eighteen repos"]["stage"], 1)
        self.assertIsNone(by_text["the leak scan log needs escape sequences to read"]["ticket"])

    def test_a_noticed_in_line_by_comment_id_without_a_quote_tickets_the_journal(self):
        entry = journal("- Improving Loom itself: resume latency eats the tick budget")
        tickets = [self.ticket(6, "Ticks run out of time.\n\nNoticed in loom#30 issuecomment-9001")]
        data = build_improvements([entry], tickets, [])
        self.assertIs(data["records"][0]["ticket"], tickets[0])

    def test_a_noticed_in_line_for_another_journal_does_not_match(self):
        entry = journal("- Friction: resume latency eats the tick budget")
        tickets = [self.ticket(7, "Ticks run out of time.\n\nNoticed in "
                               "https://github.com/example-owner/sample/issues/30#issuecomment-90011")]
        data = build_improvements([entry], tickets, [])
        self.assertIsNone(data["records"][0]["ticket"])

    def test_a_carried_friction_keeps_the_ticket_its_first_journal_earned(self):
        older = journal("- Friction: resume latency eats the tick budget", created="2030-01-01T00:00:00Z")
        newer = journal("- What slowed the factory: resume latency eats the tick budget",
                        created="2030-01-01T02:00:00Z",
                        url="https://github.com/example-owner/sample/issues/30#issuecomment-9002",
                        comment_id=9002)
        tickets = [self.ticket(8, "Noticed in %s: \"resume latency eats the tick budget\"" % JOURNAL_URL)]
        data = build_improvements([older, newer], tickets, [])
        self.assertEqual(len(data["records"]), 1)
        self.assertIs(data["records"][0]["journal"], newer)
        self.assertIs(data["records"][0]["ticket"], tickets[0])


class DocumentedFormatTest(unittest.TestCase):
    def test_the_journal_template_carries_a_friction_line_the_page_reads(self):
        import pathlib
        import re
        doc = (pathlib.Path(__file__).resolve().parents[1] / "docs" / "BRAIN.md").read_text()
        template = re.search(r"```markdown\n(<!-- loom:journal v1 -->\n.*?)```", doc, re.S).group(1)
        filled = template.replace("<what slowed the factory this tick, or none>", "resume ran six minutes")
        self.assertEqual(friction_lines(filled), ["resume ran six minutes"])
        self.assertIn('Noticed in <journal comment link>: "<the journal\'s Friction line>"', doc)


if __name__ == "__main__":
    unittest.main()
