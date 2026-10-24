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
    person and cannot be undone (loom#1394)."""

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
        bot = "@" + text.REVIEW_BOT_SHORT_NAME
        self.assertEqual(text.scrub_mentions("ping %s please" % bot),
                         "ping `%s` please" % bot)

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


class OutageWindowTest(unittest.TestCase):
    # On 2026-10-22 an ask and three journals dated an outage from 04:28Z,
    # the last good read; the service went down at 08:02Z.
    WRITTEN = (
        "Since ~04:28Z (~6.6h, observed across 4 brain ticks) the service has been "
        "unreachable: both endpoints return 503.",
        "The service has been down since ~04:28Z; the ask sits with the operator.",
        "the service has been unreachable since ~04:28Z today (0 stuck then), about 5 hours.",
        "the service went unreachable around 10:02Z, so nothing live could be measured.",
    )

    def test_an_outage_dated_by_one_time_is_refused(self):
        for written in self.WRITTEN:
            with self.subTest(written=written):
                with self.assertRaisesRegex(ValueError, "last good observation and the first"):
                    text.check(written)

    def test_an_outage_with_its_window_or_a_plain_observation_passes(self):
        text.check("Unreachable: last good read 04:28Z (0 stuck), first failing read "
                   "09:02Z; it went down between them.")
        text.check("Both endpoints answered 503 at 11:10Z (retried, same body); this is new "
                   "since the 04:28Z read.")
        text.check("3 runs in a row failed since 2026-10-22T07:00Z; the last at 08:00Z.")
        text.check("Pre-flight: unchanged since the last journal at 07:41Z.")


if __name__ == "__main__":
    unittest.main()
