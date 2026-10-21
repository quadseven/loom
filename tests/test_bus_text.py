"""bus/text.py keeps one definition of each check (loom#503)."""
import ast
import collections
import os
import sys
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bus import text  # noqa: E402


class TextModuleTest(unittest.TestCase):
    def test_each_module_level_name_is_bound_once(self):
        # A second binding silently replaces the first, so two copies of a
        # pattern can drift and only the later one is ever used.
        tree = ast.parse((ROOT / "bus" / "text.py").read_text())
        names = collections.Counter(
            target.id
            for node in tree.body if isinstance(node, ast.Assign)
            for target in node.targets if isinstance(target, ast.Name))
        self.assertEqual(sorted(n for n, c in names.items() if c > 1), [])

    def test_private_hostname_is_refused(self):
        with self.assertRaisesRegex(ValueError, "private hostname"):
            text.check("the job on service.internal failed")
        text.check("see docs/ROADMAP.md for the plan")


class ScrubMentionsTest(unittest.TestCase):
    """A plain-text at-sign word is a GitHub mention that notifies a real
    person and cannot be undone (wow-overseer#921, #866)."""

    def test_decorator_at_rule_and_npm_scope_in_plain_text_are_wrapped(self):
        for token in ("@property", "@media", "@types/node", "@app.route", "@dataclass"):
            out = text.scrub_mentions("proof row uses %s here" % token)
            self.assertNotIn(" " + token, out, token)
            self.assertIn("`%s" % token.split(".")[0], out, token)
        self.assertEqual(text.scrub_mentions("uses @property in (@media) too"),
                         "uses `@property` in (`@media`) too")
        self.assertEqual(text.scrub_mentions("@types/node pins"), "`@types/node` pins")

    def test_every_mention_in_a_line_and_across_lines_is_wrapped(self):
        self.assertEqual(text.scrub_mentions("@a and @b\n- @c-d"),
                         "`@a` and `@b`\n- `@c-d`")

    def test_the_review_bot_short_name_is_wrapped_too(self):
        self.assertEqual(text.scrub_mentions("ping @grug please"), "ping `@grug` please")

    def test_code_spans_and_fences_are_left_unchanged(self):
        for body in (
                "use `@property` here",
                "use ``@media`` and `@types/node` here",
                "```python\n@property\ndef x(): ...\n```",
                "~~~\n@media print\n~~~",
                "> ```\n> @dataclass\n> ```",
                "``` \n@unclosed fence runs to the end",
        ):
            self.assertEqual(text.scrub_mentions(body), body, body)

    def test_text_around_code_is_still_wrapped(self):
        self.assertEqual(
            text.scrub_mentions("@a `@b` @c\n```\n@d\n```\n@e"),
            "`@a` `@b` `@c`\n```\n@d\n```\n`@e`")
        # A lone backtick opens nothing, so the word after it is prose.
        self.assertEqual(text.scrub_mentions("it`s @x"), "it`s `@x`")

    def test_emails_urls_and_bare_at_signs_are_not_mentions(self):
        for body in ("mail user@example.com now", "https://example.com/@user/page",
                     "meet @ 5pm", "price 3 @ 4", "provider/@org/model", ""):
            self.assertEqual(text.scrub_mentions(body), body, body)

    def test_an_allowlisted_login_stays_a_mention(self):
        self.assertEqual(
            text.scrub_mentions("cc @Trusted-One and @stranger", allow=("trusted-one",)),
            "cc @Trusted-One and `@stranger`")

    def test_the_environment_allowlist_is_read_at_each_call(self):
        with mock.patch.dict(os.environ, {text.MENTION_ALLOW_ENV: " alpha , Beta "}):
            self.assertEqual(text.scrub_mentions("@alpha @beta @gamma"),
                             "@alpha @beta `@gamma`")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(text.MENTION_ALLOW_ENV, None)
            self.assertEqual(text.scrub_mentions("@alpha"), "`@alpha`")

    def test_scrubbing_twice_changes_nothing_more(self):
        once = text.scrub_mentions("@a `@b` @c/d\n```\n@e\n```")
        self.assertEqual(text.scrub_mentions(once), once)

    def test_check_still_refuses_the_review_bot_short_name(self):
        with self.assertRaisesRegex(ValueError, "short name"):
            text.check("ask @%s" % text.REVIEW_BOT_SHORT_NAME)
        # check() judges the raw text: scrubbing is not a way past it.
        text.check(text.scrub_mentions("run /%s improve" % text.REVIEW_BOT_SHORT_NAME))


if __name__ == "__main__":
    unittest.main()
