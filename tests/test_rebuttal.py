"""A lane's answer to a review finding must hold on the pull request head
before it settles the thread (bus/rebuttal.py, loom#2081)."""

import base64
import json
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bus import brain, rebuttal, text  # noqa: E402
from bus.github import GitHub, RateLimited  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

REPO = "example-owner/example-repo"
NUMBER = 8
HEAD = "f" * 40
# The loom#1948 shape: a dead function next to the sibling its caller uses.
STATION = ("def _markings(s):\n    return []\n\n\n"
           "def _markings_with_accept(s, sha):\n    return []\n\n\n"
           "def _hand_back(s, sha):\n    found = _markings_with_accept(s, sha)\n    return found\n")
CALLED = STATION.replace("found = _markings_with_accept(s, sha)",
                         "found = _markings(s) + _markings_with_accept(s, sha)")
DEAD = ("medium | _maintainability_ | `dead-code` | quick win\n\n"
        "New function `_markings` has no caller outside test files.")
COVERAGE = "medium | _test coverage_ | `missing-test-coverage`\n\nThe test names a label only."
TEST_FILE = "\n".join(["# line %d" % i for i in range(1, 10)]
                      + ["self.assertFalse(c.startswith(tickets.FIX_MARKER))", "# end"]) + "\n"


def check(answer, files, finding=DEAD, path="bus/s.py", ancestors=()):
    return rebuttal.refusal(answer, files.get, lambda sha: sha in ancestors, path, finding)


class Refusal(unittest.TestCase):
    def test_a_false_caller_claim_on_a_dead_code_finding_does_not_hold(self):
        why = check("`_markings` is called by `_hand_back` at line 9, so it is not dead code.",
                    {"bus/s.py": STATION})
        self.assertIn("does not call `_markings`", why)

    def test_the_same_caller_claim_holds_when_the_head_has_the_call(self):
        self.assertEqual(check("`_markings` is called by `_hand_back` at line 9.",
                               {"bus/s.py": CALLED}), "")

    def test_it_is_called_from_a_line_that_has_no_such_call_does_not_hold(self):
        why = check("It is called from bus/s.py:10.", {"bus/s.py": STATION})
        self.assertIn("bus/s.py:10 does not call `_markings`", why)
        self.assertEqual(check("It is called from bus/s.py:10.", {"bus/s.py": CALLED}), "")

    def test_a_dead_code_rebuttal_with_no_call_on_the_head_does_not_hold(self):
        why = check("`_markings` is part of the shipped hand-back flow.", {"bus/s.py": STATION})
        self.assertIn("no call of `_markings`", why)

    def test_a_vague_used_in_claim_is_not_read_as_a_caller(self):
        self.assertEqual(check("`_markings` is used in the tests too.", {"bus/s.py": CALLED}), "")

    def test_a_caller_the_head_does_not_define_does_not_hold(self):
        why = check("`_markings` is called by `_gone`.", {"bus/s.py": STATION})
        self.assertIn("`_gone`", why)

    def test_an_answer_that_cites_nothing_does_not_hold(self):
        why = check("This finding is already addressed.", {"bus/s.py": CALLED}, finding=COVERAGE)
        self.assertIn("cites no file, line or symbol", why)

    def test_a_cited_symbol_and_lines_that_exist_on_the_head_hold(self):
        answer = ("The test checks that no comment starts with `tickets.FIX_MARKER` at "
                  "lines 9-10, so no fix round was sent.")
        self.assertEqual(check(answer, {"t/test_s.py": TEST_FILE}, COVERAGE, "t/test_s.py"), "")

    def test_a_cited_symbol_missing_from_the_head_does_not_hold(self):
        why = check("The test checks `tickets.OTHER_MARKER`.",
                    {"t/test_s.py": TEST_FILE}, COVERAGE, "t/test_s.py")
        self.assertIn("none of `tickets.OTHER_MARKER` is in t/test_s.py", why)

    def test_a_well_cited_answer_naming_a_library_symbol_holds(self):
        # The #2009 shape: the cited file has the handler, not the stdlib
        # name the answer also mentions; one symbol on the head is enough.
        github = "def _send(self):\n    try:\n        return go()\n    except OSError:\n        raise\n"
        answer = ("`_send` in bus/github.py:4 already catches OSError, and `socket.timeout` "
                  "is a subclass of it, so the timeout is handled.")
        self.assertEqual(check(answer, {"bus/github.py": github, "bus/station.py": "x = 1\n"},
                               COVERAGE, "bus/station.py"), "")

    def test_a_cited_file_or_line_missing_from_the_head_does_not_hold(self):
        files = {"t/test_s.py": TEST_FILE}
        self.assertIn("t/gone.py does not exist", check("See t/gone.py:3.", files, COVERAGE))
        self.assertIn("has no line 400", check("See t/test_s.py:400.", files, COVERAGE))

    def test_fixed_in_a_commit_on_the_head_history_holds(self):
        self.assertEqual(check("Fixed in abc1234", {}, ancestors={"abc1234"}), "")

    def test_a_lowercase_fixed_in_a_commit_on_the_head_history_holds(self):
        self.assertEqual(check("fixed in abc1234", {}, ancestors={"abc1234"}), "")

    def test_fixed_in_a_foreign_commit_does_not_hold(self):
        why = check("Fixed in 0ddba11", {}, ancestors={"abc1234"})
        self.assertIn("not on the head", why)

    def test_quoted_reads_the_lane_words_from_the_station_reply(self):
        body = "Lane codex answered this finding instead of changing the code:\n\n> Fixed in abc1234"
        self.assertEqual(rebuttal.quoted(body), "Fixed in abc1234")


def reply(status=200, body=None):
    return status, {}, b"" if body is None else json.dumps(body).encode()


class Transport:
    """Answers contents and compare reads from a dict; records every call."""

    def __init__(self, files=None, ancestors=(), unresolve_status=200):
        self.files, self.ancestors, self.calls = files or {}, set(ancestors), []
        self.unresolve_status = unresolve_status
        self.comments = []

    def __call__(self, method, url, headers, body):
        path = url.split("api.github.com")[-1]
        self.calls.append((method, path, json.loads(body) if body else None))
        if path.split("?")[0] == "/repos/%s/issues/%d/comments" % (REPO, NUMBER):
            if method == "POST":
                self.comments.append({"id": len(self.comments) + 1, "body": json.loads(body)["body"]})
                return reply(201, self.comments[-1])
            return reply(200, list(self.comments))
        m = re.match(r"/repos/%s/contents/(.+)\?ref=%s$" % (REPO, HEAD), path)
        if m:
            text = self.files.get(m.group(1))
            if text is None:
                return reply(404, {"message": "Not Found"})
            return reply(200, {"type": "file", "encoding": "base64",
                               "content": base64.b64encode(text.encode()).decode()})
        m = re.match(r"/repos/%s/compare/(\w+)\.\.\.%s$" % (REPO, HEAD), path)
        if m:
            return reply(200, {"status": "ahead" if m.group(1) in self.ancestors else "diverged"})
        if path == "/graphql":
            return reply(self.unresolve_status, {"data": {}})
        raise AssertionError("unexpected call %s %s" % (method, path))


BOT = {"__typename": "Bot", "login": "grug-tribe"}
STATION_APP = {"__typename": "Bot", "login": "loom-station"}
ENV = {"LOOM_BRIEF_AUTHORS": "loom-station[bot]"}


def details(answer, finding=DEAD, resolved=True, last_author=STATION_APP):
    body = "Lane codex answered this finding instead of changing the code:\n\n> " + answer
    return {"headRefOid": HEAD, "reviewThreads": {"nodes": [{
        "id": "PRRT_1", "isResolved": resolved, "path": "bus/s.py",
        "comments": {"nodes": [{"author": BOT, "body": finding}]},
        "latest": {"nodes": [{"author": last_author, "body": body}]}}]}}


class BrainMergeGate(unittest.TestCase):
    """The brain refuses a merge whose bot thread was resolved on an answer
    that does not hold on the head, and reopens that thread."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"LOOM_REVIEW_BOT": ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def gate(self, transport, data):
        brain._refuse_unheld_answers(GitHub("t", transport=transport), REPO, NUMBER, data, ENV)

    def test_a_thread_resolved_on_a_false_caller_claim_refuses_and_reopens(self):
        transport = Transport({"bus/s.py": STATION})
        with self.assertRaisesRegex(ValueError, "does not hold on the head"):
            self.gate(transport, details("`_markings` is called by `_hand_back`."))
        mutation = [c for c in transport.calls if c[1] == "/graphql"]
        self.assertEqual(len(mutation), 1)
        self.assertIn("unresolveReviewThread", mutation[0][2]["query"])
        self.assertEqual(mutation[0][2]["variables"], {"id": "PRRT_1"})

    def test_a_thread_resolved_on_a_true_caller_claim_passes(self):
        transport = Transport({"bus/s.py": CALLED})
        self.gate(transport, details("`_markings` is called by `_hand_back`."))
        self.assertFalse([c for c in transport.calls if c[1] == "/graphql"])
        self.assertEqual(transport.comments, [])

    def test_a_refusal_posts_one_marker_comment_per_thread_and_head(self):
        # loom#2179: a refusal leaves a countable record on the pull request,
        # once per thread and head however many ticks refuse it.
        transport = Transport({"bus/s.py": STATION})
        answer = "`_markings` is called by `_hand_back`."
        for _ in range(2):
            with self.assertRaises(ValueError) as raised:
                self.gate(transport, details(answer))
        self.assertEqual(len(transport.comments), 1)
        body = transport.comments[0]["body"]
        marker = "<!-- loom:rebuttal-refused v1 thread=PRRT_1 head=%s -->" % HEAD
        self.assertIn(marker, body)
        self.assertIn("PRRT_1", body)
        self.assertIn("> " + answer, body)
        self.assertIn("`_hand_back`", body)
        why = "`_hand_back` does not call `_markings` on the head"
        self.assertIn(why, body)
        # The journal prints the merge refusal: it carries the same reason
        # and the marker's thread and head, so the two counts agree.
        self.assertIn(why, str(raised.exception))
        self.assertIn("rebuttal-refused thread=PRRT_1 head=%s" % HEAD, str(raised.exception))

    def test_a_new_head_posts_its_own_marker(self):
        transport = Transport({"bus/s.py": STATION})
        transport.comments.append({"id": 1, "body": "<!-- loom:rebuttal-refused v1 "
                                   "thread=PRRT_1 head=%s -->" % ("e" * 40)})
        with self.assertRaises(ValueError):
            self.gate(transport, details("`_markings` is called by `_hand_back`."))
        self.assertEqual(len(transport.comments), 2)

    def test_fixed_in_counts_only_for_a_commit_on_the_head_history(self):
        self.gate(Transport(ancestors={"abc1234"}), details("Fixed in abc1234"))
        with self.assertRaisesRegex(ValueError, "not on the head"):
            self.gate(Transport(ancestors={"abc1234"}), details("Fixed in 0ddba11"))

    def test_a_thread_the_bot_spoke_last_on_or_an_open_one_is_not_rechecked(self):
        transport = Transport()
        self.gate(transport, details("nothing", last_author=BOT))
        self.gate(transport, details("nothing", resolved=False))
        self.assertEqual(transport.calls, [])

    def test_a_rate_limit_while_checking_is_raised_and_reopens_nothing(self):
        gh = GitHub("t", transport=Transport())
        with mock.patch.object(gh, "rest", side_effect=RateLimited("limited", 403, "", 0)):
            with self.assertRaises(RateLimited):
                brain._refuse_unheld_answers(gh, REPO, NUMBER, details("`_markings` is called by `_x`."),
                                             ENV)

    # loom#2285 (on another repo): the station redacts any 32-character
    # word from the posted answer, so a long test name the answer cites is
    # gone from the quote and the quote alone "cites no file, line or symbol".
    TEST_NAME = "test_a_resolved_verified_answer_does_not_block_the_merge"

    def redacted_reply(self, head):
        raw = "`%s` covers it." % self.TEST_NAME
        said = text.SECRET_RE.sub("<redacted>", raw)
        self.assertNotIn(self.TEST_NAME, said)
        return said + "\n" + rebuttal.held_marker(head)

    def test_an_answer_citing_a_test_name_passes_after_redaction(self):
        transport = Transport()
        data = details(self.redacted_reply(HEAD))
        self.assertEqual(brain._refuse_unheld_answers(GitHub("t", transport=transport),
                                                      REPO, NUMBER, data, ENV), {"PRRT_1"})
        self.assertEqual(transport.calls, [])

    HIGH = ("high | _correctness_ | `intent-mismatch` | medium\n\n"
            "No acceptance criterion is met.")

    def held_ids(self, data):
        return brain._refuse_unheld_answers(GitHub("t", transport=Transport(ancestors={"abc1234"})),
                                            REPO, NUMBER, data, ENV)

    def test_only_a_minor_finding_marked_held_for_this_head_is_exempt(self):
        # loom#2519: an answer that merely holds is not the station's verdict;
        # only its mark for this head, on a finding it may settle, exempts.
        self.assertEqual(self.held_ids(details("Fixed in abc1234\n" + rebuttal.held_marker(HEAD))),
                         {"PRRT_1"})
        self.assertEqual(self.held_ids(details("Fixed in abc1234\n"
                                               + rebuttal.held_marker("e" * 40))), set())
        self.assertEqual(self.held_ids(details("Fixed in abc1234")), set())

    def test_an_answered_high_finding_is_never_exempt(self):
        data = details("Fixed in abc1234\n" + rebuttal.held_marker(HEAD), finding=self.HIGH)
        self.assertEqual(self.held_ids(data), set())

    def test_a_finding_from_a_person_with_the_bots_login_is_never_exempt(self):
        data = details("Fixed in abc1234\n" + rebuttal.held_marker(HEAD))
        data["reviewThreads"]["nodes"][0]["comments"]["nodes"][0]["author"] = {
            "__typename": "User", "login": "grug-tribe"}
        self.assertEqual(self.held_ids(data), set())

    def test_a_held_mark_for_another_head_is_checked_again_on_this_one(self):
        with self.assertRaisesRegex(ValueError, "cites no file, line or symbol"):
            self.gate(Transport(), details(self.redacted_reply("e" * 40)))

    def test_a_held_mark_inside_the_lane_quote_is_not_the_stations(self):
        # The lane writes the quoted lines; only the station's own line counts.
        forged = "nothing to see\n" + rebuttal.held_marker(HEAD)
        with self.assertRaisesRegex(ValueError, "cites no file, line or symbol"):
            self.gate(Transport(), details(forged.replace("\n", "\n> ")))

    def test_the_quote_leaves_out_the_held_mark(self):
        body = ("Lane codex answered this finding instead of changing the code:\n\n> Fine.\n"
                + rebuttal.held_marker(HEAD))
        self.assertEqual(rebuttal.quoted(body), "Fine.")
        self.assertTrue(rebuttal.marked_held(body, HEAD))
        self.assertFalse(rebuttal.marked_held(body, "e" * 40))
        self.assertFalse(rebuttal.marked_held(body, ""))

    def test_merge_runs_the_gate_before_it_merges(self):
        with mock.patch.object(brain, "_refuse_unheld_answers",
                               side_effect=ValueError("held")) as gate, \
                mock.patch.object(brain, "_validate_merge_pr"), \
                mock.patch.object(brain, "_await_settled", side_effect=lambda gh, r, n, p, d, e: (p, d)), \
                mock.patch.object(brain, "_pr_graphql", return_value={}):
            gh = GitHub("t", transport=lambda m, u, h, b: reply(200, {"state": "open"}))
            with self.assertRaisesRegex(ValueError, "held"):
                brain.merge(gh, REPO, 8, (REPO, 9), env=ENV)
        gate.assert_called_once()


class DeadMarkingsGone(unittest.TestCase):
    def test_elder_markings_is_gone_or_has_a_caller(self):
        # loom#1948 merged `_elder_markings` with no caller on a false answer.
        # It was removed (loom#2081); if it comes back, something must call it.
        root = os.path.join(os.path.dirname(__file__), "..", "bus")
        source = ""
        for name in sorted(os.listdir(root)):
            if name.endswith(".py"):
                with open(os.path.join(root, name), encoding="utf-8") as fh:
                    source += fh.read()
        if re.search(r"def _elder_markings\(", source):
            self.assertRegex(source, r"\._elder_markings\(")


if __name__ == "__main__":
    unittest.main()
