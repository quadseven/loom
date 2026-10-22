"""loom#1341: the first routine event on a ticket no longer posts a comment.

A new comment emails the operator, and the first event had no comment to edit.
A client that reads the issue body's status section back keeps the events
there; a body edit sends no email. Each test names one property the station
relies on: no comment, no lost edit, no forged event, no change for a ticket
that already has a status comment.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import factory, status, tickets  # noqa: E402
from bus.github import GitHub  # noqa: E402

REPO = "example-owner/example-repo"
APP = {"login": "example-loom[bot]"}
OPERATOR = {"login": "operator-example"}
T0 = 1893456000  # 2030-01-01T00:00:00Z
TICKET = ("Difficulty: medium\n\nFix the thing.\n\n## Acceptance criteria\n\n"
          "- [ ] it works\n- [ ] it is tested\n")
CLAIM = "Claimed for lane codex (body sha256 0123456789ab)"


class FakeTransport:
    """Issue 7, its comments, and a hook to edit its body mid-write."""

    def __init__(self, author=OPERATOR, body=TICKET):
        self.issue = {"id": 4242, "number": 7, "user": author, "body": body}
        self.comments = []
        self.calls = []
        self.next_id = 100
        # Called with the number of issue reads so far, after each read.
        self.after_read = None
        self.reads = 0

    def __call__(self, method, url, headers, body):
        path = urlsplit(url).path
        self.calls.append((method, path))
        data = json.loads(body) if body else None
        base = "/repos/%s/issues/7" % REPO
        if method == "GET" and path == base:
            self.reads += 1
            answer = json.dumps(self.issue).encode()
            if self.after_read:
                self.after_read(self.reads)
            return 200, {}, answer
        if method == "PATCH" and path == base:
            self.issue.update(data)
            return 200, {}, json.dumps(self.issue).encode()
        if method == "GET" and path == base + "/comments":
            return 200, {}, json.dumps(self.comments).encode()
        if method == "POST" and path == base + "/comments":
            self.next_id += 1
            made = {"id": self.next_id, "user": APP, "body": data["body"],
                    "created_at": "2030-01-01T00:00:00Z", "updated_at": "2030-01-01T00:00:00Z",
                    "issue_url": "https://api.github.com" + base}
            self.comments.append(made)
            return 201, {}, json.dumps(made).encode()
        if method == "PATCH" and path.startswith("/repos/%s/issues/comments/" % REPO):
            ident = int(path.rsplit("/", 1)[1])
            for comment in self.comments:
                if comment["id"] == ident:
                    comment["body"] = data["body"]
                    return 200, {}, json.dumps(dict(
                        comment, issue_url="https://api.github.com" + base)).encode()
            return 404, {}, b"{}"
        raise AssertionError("unexpected %s %s" % (method, path))

    def writes(self, method):
        return [c for c in self.calls if c[0] == method]


def client(fake, in_body=True):
    gh = GitHub("token", transport=fake)
    gh.status_authors.update({APP["login"], OPERATOR["login"]})
    gh.status_in_body = in_body
    return gh


class SectionTest(unittest.TestCase):
    def test_events_round_trip_through_the_section_and_the_body_around_it_is_kept(self):
        events = [("2030-01-01T02:00:00Z", "<!-- loom:rung 2 -->\nRepeatedly stuck (x): moved."),
                  ("2030-01-01T01:00:00Z", CLAIM)]
        body = status.with_body_events(TICKET, events)
        self.assertTrue(body.startswith(TICKET.rstrip("\n")))
        self.assertEqual(status.body_events(body), events)
        self.assertEqual(status.strip_body(body).strip(), TICKET.strip())
        # A second write replaces the section, never adds another.
        again = status.with_body_events(body, events[:1])
        self.assertEqual(again.count(status.BODY_BEGIN), 1)
        self.assertEqual(status.body_events(again), events[:1])

    def test_a_quoted_copy_of_a_section_is_not_a_section(self):
        quoted = "\n".join("> " + x for x in status.with_body_events("", [("2030-01-01T00:00:00Z", CLAIM)]).split("\n"))
        self.assertEqual(status.body_events(TICKET + "\n" + quoted), [])
        self.assertEqual(status.strip_body(TICKET + "\n" + quoted), TICKET + "\n" + quoted)

    def test_the_oldest_events_drop_past_the_body_limit_and_a_body_with_no_room_refuses(self):
        events = [("2030-01-01T%02d:00:00Z" % (h % 24), "x" * 5000) for h in range(20)]
        body = status.with_body_events(TICKET, events)
        self.assertLessEqual(len(body), 65536)
        self.assertEqual(status.body_events(body)[0], events[0])
        with self.assertRaises(ValueError):
            status.with_body_events("y" * 65000, [("2030-01-01T00:00:00Z", "x" * 5000)])


class AddToBodyTest(unittest.TestCase):
    def test_the_first_event_goes_in_the_body_and_posts_no_comment(self):
        fake = FakeTransport()
        status.add(client(fake), REPO, 7, CLAIM, T0, [OPERATOR["login"]])
        self.assertEqual(fake.writes("POST"), [])
        self.assertEqual(fake.comments, [])
        self.assertEqual([t for _, t in status.body_events(fake.issue["body"])], [CLAIM])
        self.assertEqual(status.strip_body(fake.issue["body"]).strip(), TICKET.strip())

    def test_later_events_edit_the_section_newest_first(self):
        fake = FakeTransport()
        gh = client(fake)
        for k, text in enumerate([CLAIM, "second", "third"]):
            status.add(gh, REPO, 7, text, T0 + 60 * k, [OPERATOR["login"]])
        self.assertEqual(fake.writes("POST"), [])
        self.assertEqual([t for _, t in status.body_events(fake.issue["body"])],
                         ["third", "second", CLAIM])
        self.assertEqual(fake.issue["body"].count(status.BODY_BEGIN), 1)

    def test_a_ticket_with_a_status_comment_keeps_editing_it(self):
        fake = FakeTransport()
        fake.comments = [{"id": 5, "user": APP, "created_at": "2030-01-01T00:00:00Z",
                          "body": status.render([("2030-01-01T00:00:00Z", CLAIM)])}]
        status.add(client(fake), REPO, 7, "second", T0 + 60, [])
        self.assertEqual(fake.writes("POST"), [])
        self.assertEqual(fake.issue["body"], TICKET)
        self.assertEqual([t for _, t in status.parse(fake.comments[0]["body"])], ["second", CLAIM])

    def test_a_client_that_does_not_read_the_section_posts_a_comment(self):
        fake = FakeTransport()
        status.add(client(fake, in_body=False), REPO, 7, CLAIM, T0, [OPERATOR["login"]])
        self.assertEqual(len(fake.writes("POST")), 1)
        self.assertEqual(fake.issue["body"], TICKET)

    def test_a_stranger_authored_issue_is_never_edited(self):
        fake = FakeTransport(author={"login": "stranger"})
        status.add(client(fake), REPO, 7, CLAIM, T0, [OPERATOR["login"]])
        self.assertEqual(fake.writes("PATCH"), [])
        self.assertEqual(fake.issue["body"], TICKET)
        self.assertEqual(len(fake.writes("POST")), 1)

    def test_an_edit_made_during_the_write_is_merged_not_overwritten(self):
        fake = FakeTransport()
        edited = TICKET + "\nOperator note added meanwhile.\n"

        def edit(reads):
            if reads == 1:  # between the first read and the read before the write
                fake.issue["body"] = edited
        fake.after_read = edit
        status.add(client(fake), REPO, 7, CLAIM, T0, [OPERATOR["login"]])
        self.assertIn("Operator note added meanwhile.", fake.issue["body"])
        self.assertEqual([t for _, t in status.body_events(fake.issue["body"])], [CLAIM])
        self.assertEqual(fake.writes("POST"), [])

    def test_a_body_that_keeps_moving_sends_the_event_to_a_comment_and_writes_no_body(self):
        fake = FakeTransport()

        def edit(reads):
            fake.issue["body"] = TICKET + "\nedit %d\n" % reads
        fake.after_read = edit
        status.add(client(fake), REPO, 7, CLAIM, T0, [OPERATOR["login"]])
        self.assertEqual(fake.writes("PATCH"), [])
        self.assertEqual(len(fake.writes("POST")), 1)
        self.assertNotIn(status.BODY_BEGIN, fake.issue["body"])


class ReadBackTest(unittest.TestCase):
    def written(self):
        fake = FakeTransport()
        gh = client(fake)
        status.add(gh, REPO, 7, "<!-- loom:rung 2 -->\nRepeatedly stuck (x): moved.", T0,
                   [OPERATOR["login"]])
        status.add(gh, REPO, 7, CLAIM, T0 + 60, [OPERATOR["login"]])
        return fake, gh

    def test_every_reader_of_the_comment_list_sees_the_body_events_as_comments(self):
        fake, gh = self.written()
        fake.comments = [{"id": 1, "user": OPERATOR, "created_at": "2030-01-01T00:00:30Z",
                          "body": "A human note"}]
        listed = gh.paginate("/repos/%s/issues/7/comments" % REPO)
        self.assertEqual([c["body"] for c in listed], [
            "<!-- loom:rung 2 -->\nRepeatedly stuck (x): moved.", "A human note", CLAIM])
        self.assertEqual({c["user"]["login"] for c in listed}, {OPERATOR["login"]})
        self.assertEqual(factory.tried_rungs(listed), {2})
        self.assertEqual(factory.CLAIMED_RE.match(listed[-1]["body"]).group(1), "codex")

    def test_an_event_read_from_the_body_names_no_comment_to_edit(self):
        fake, gh = self.written()
        listed = gh.paginate("/repos/%s/issues/7/comments" % REPO)
        self.assertTrue(all(c["id"] < 0 for c in listed))
        self.assertIsNone(status.find(gh, REPO, 7, [OPERATOR["login"]]))

    def test_a_section_in_a_strangers_issue_is_not_read(self):
        fake = FakeTransport(author={"login": "stranger"},
                             body=status.with_body_events(TICKET, [("2099-01-01T00:00:00Z", CLAIM)]))
        listed = client(fake).paginate("/repos/%s/issues/7/comments" % REPO)
        self.assertEqual(listed, [])

    def test_a_client_that_does_not_read_the_section_adds_no_issue_read(self):
        fake, _ = self.written()
        before = fake.reads
        client(fake, in_body=False).paginate("/repos/%s/issues/7/comments" % REPO)
        self.assertEqual(fake.reads, before)

    def test_only_the_first_page_carries_the_body_events(self):
        fake, gh = self.written()
        listed = gh.paginate("/repos/%s/issues/7/comments?page=2" % REPO)
        self.assertEqual(listed, [])


class KeepSectionTest(unittest.TestCase):
    def test_a_writer_with_a_stale_body_cannot_drop_the_events(self):
        fake = FakeTransport()
        gh = client(fake)
        stale = fake.issue["body"]  # a writer read the body now
        status.add(gh, REPO, 7, CLAIM, T0, [OPERATOR["login"]])
        gh.rest("PATCH", "/repos/%s/issues/7" % REPO, {"body": stale + "\nA new line.\n"})
        self.assertIn("A new line.", fake.issue["body"])
        self.assertEqual([t for _, t in status.body_events(fake.issue["body"])], [CLAIM])

    def test_a_patch_that_does_not_set_the_body_is_left_alone(self):
        fake = FakeTransport()
        gh = client(fake)
        status.add(gh, REPO, 7, CLAIM, T0, [OPERATOR["login"]])
        reads = fake.reads
        gh.rest("PATCH", "/repos/%s/issues/7" % REPO, {"title": "New"})
        self.assertEqual(fake.reads, reads)


class TicketTest(unittest.TestCase):
    def test_the_brief_does_not_carry_the_status_section(self):
        body = status.with_body_events(TICKET, [("2030-01-01T00:00:00Z", CLAIM)])
        headers, rest = tickets.parse(body)
        self.assertEqual(headers, {"Difficulty": "medium"})
        self.assertNotIn("Claimed for lane", rest)
        self.assertNotIn(status.BODY_BEGIN, rest)

    def test_a_rewrite_keeps_the_section_live_and_does_not_quote_it(self):
        body = status.with_body_events(TICKET, [("2030-01-01T00:00:00Z", CLAIM)])
        out = tickets.with_original("Difficulty: easy\n\nNew text.\n",
                                    {"title": "Old", "body": body})
        self.assertEqual([t for _, t in status.body_events(out)], [CLAIM])
        self.assertNotIn("> " + CLAIM, out)
        self.assertNotIn("> - 2030", out)


if __name__ == "__main__":
    unittest.main()
