"""bus/scope_fit.py: the cheap-model check of a pull request's files against
its ticket (loom#1348)."""
import io
import json
import unittest
from unittest import mock

from bus import scope_fit

BODY = ("Difficulty: medium\n\n## Why\nx\n\n## What\nKeep four Deployments off one worker.\n\n"
        "## Size\nS\n")


class Parse(unittest.TestCase):
    def test_yes_fits_in_any_casing_and_decoration(self):
        for reply in ("YES", "yes.", "**YES**", "\nYes, it fits", "- Yes"):
            self.assertEqual(scope_fit.parse(reply).kind, scope_fit.FITS, reply)

    def test_no_with_a_reason_is_a_misfit(self):
        verdict = scope_fit.parse("NO: it edits the runner values, not the Deployments.")
        self.assertEqual(verdict, (scope_fit.MISFIT, "it edits the runner values, not the Deployments."))

    def test_an_unreadable_reply_is_unknown(self):
        for reply in (None, "", "   ", "Perhaps", "NO", "no:  ", "Now I think", "nope: x"):
            self.assertEqual(scope_fit.parse(reply).kind, scope_fit.UNKNOWN, reply)

    def test_a_reason_is_one_ascii_line_without_a_handle(self):
        verdict = scope_fit.parse("NO: wrong" + chr(0x2014) + "repo\nsee " + "@" + "grug and more")
        self.assertEqual(verdict.kind, scope_fit.MISFIT)
        self.assertNotIn("@", verdict.reason)
        self.assertNotIn("\n", verdict.reason)
        self.assertTrue(verdict.reason.isascii())

    def test_a_reason_that_cannot_be_posted_is_replaced(self):
        for bad in ("see " + "/Us" + "ers/example/work/x", "host build.internal", "fixes #12",
                    "token=abcdef0123456789abcdef"):
            verdict = scope_fit.parse("NO: " + bad)
            self.assertEqual(verdict, (scope_fit.MISFIT, scope_fit.NO_REASON), bad)

    def test_a_long_reason_is_clipped(self):
        self.assertLessEqual(len(scope_fit.parse("NO: " + "word " * 200).reason), scope_fit.REASON_MAX)


class Prompt(unittest.TestCase):
    def test_it_holds_the_what_section_and_the_files_only(self):
        prompt = scope_fit.build_prompt(scope_fit.what_section(BODY), ["a/b.py", "c.md"])
        self.assertIn("Keep four Deployments off one worker.", prompt)
        self.assertIn("- a/b.py", prompt)
        self.assertNotIn("## Size", prompt)

    def test_a_long_file_list_is_cut_to_a_count(self):
        files = ["f%d.py" % n for n in range(200)]
        prompt = scope_fit.build_prompt("x", files)
        self.assertIn("(and %d more files)" % (200 - scope_fit.FILES_SHOWN), prompt)
        self.assertNotIn("f199.py", prompt)
        self.assertLess(len(prompt), 5000)

    def test_a_ticket_without_a_what_section_is_not_asked(self):
        asked = []
        verdict = scope_fit.judge(lambda p: asked.append(p), "## Why\nx\n", ["a.py"])
        self.assertEqual((verdict.kind, asked), (scope_fit.UNKNOWN, []))

    def test_no_changed_files_is_not_asked(self):
        asked = []
        self.assertEqual(scope_fit.judge(lambda p: asked.append(p), BODY, []).kind, scope_fit.UNKNOWN)
        self.assertEqual(asked, [])


class Judge(unittest.TestCase):
    def test_a_reply_becomes_a_verdict(self):
        self.assertEqual(scope_fit.judge(lambda p: "YES", BODY, ["a.py"]).kind, scope_fit.FITS)
        self.assertEqual(scope_fit.judge(lambda p: "NO: wrong repo", BODY, ["a.py"]),
                         (scope_fit.MISFIT, "wrong repo"))

    def test_no_reply_or_a_raising_asker_is_unknown(self):
        self.assertEqual(scope_fit.judge(lambda p: None, BODY, ["a.py"]).kind, scope_fit.UNKNOWN)

        def boom(prompt):
            raise ValueError("x")
        self.assertEqual(scope_fit.judge(boom, BODY, ["a.py"]).kind, scope_fit.UNKNOWN)


class Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Asker(unittest.TestCase):
    URL = "https://gateway.invalid/free-public/gemini/v1"

    def reply(self, data, status=200):
        response = Response(json.dumps(data).encode())
        response.status = status
        return mock.Mock(return_value=response)

    def test_a_non_https_route_gives_no_asker(self):
        for url in ("", "http://gateway.invalid/v1", "https://x/v1?key=1", "gateway.invalid"):
            self.assertIsNone(scope_fit.asker(url), url)

    def test_it_posts_one_keyless_chat_request_and_returns_the_text(self):
        urlopen = self.reply({"choices": [{"message": {"content": "YES"}}]})
        ask = scope_fit.asker(self.URL, "gemma-x", urlopen=urlopen)
        self.assertEqual(ask("the prompt"), "YES")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, self.URL + "/chat/completions")
        self.assertNotIn("Authorization", {k.title() for k in request.headers})
        sent = json.loads(request.data)
        self.assertEqual((sent["model"], sent["messages"][0]["content"]), ("gemma-x", "the prompt"))
        self.assertLessEqual(sent["max_tokens"], 300)

    def test_a_dead_route_a_limit_or_junk_gives_none(self):
        for urlopen in (mock.Mock(side_effect=OSError("down")),
                        mock.Mock(side_effect=TimeoutError()),
                        self.reply({"choices": []}),
                        self.reply({"error": "limit"}),
                        self.reply({"choices": [{"message": {"content": None}}]}),
                        self.reply({"choices": [{"message": {"content": "YES"}}]}, status=429)):
            self.assertIsNone(scope_fit.asker(self.URL, urlopen=urlopen)("p"))

    def test_a_non_json_body_gives_none(self):
        response = Response(b"<html>busy</html>")
        self.assertIsNone(scope_fit.asker(self.URL, urlopen=mock.Mock(return_value=response))("p"))


if __name__ == "__main__":
    unittest.main()
