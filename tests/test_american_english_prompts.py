"""Agent prompts must tell writers to use American English spelling.

A public brain comment used the British form "enrol" instead of "enroll"
because the brain, rewrite, and lane prompts never said which spelling to
use. This test keeps the rule line in those sources.
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The exact line every listed source must carry.
AMERICAN_ENGLISH_RULE = (
    "Write American English spelling: enroll, color, behavior, canceled, "
    "organize, not enrol, colour, behaviour, cancelled, organise."
)

# Shared AGENTS.md text above this marker is replaced on every sync.
AGENTS_REPO_MARKER = "<!-- repo-specific below -->"

# Prompt and instruction files agents read when they write comments, tickets,
# or pull request text. British spellings elsewhere in the repo are out of
# scope for this test.
PROMPT_SOURCES = (
    ROOT / "docs" / "MUSE.md",
    ROOT / "docs" / "BRIEF_FORMAT.md",
    ROOT / "lanes" / "mac" / "brain.sh",
    ROOT / "lanes" / "run_lane.sh",
    ROOT / "bus" / "rewrite.py",
)


def _agents_repo_section() -> str:
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    if AGENTS_REPO_MARKER not in text:
        raise AssertionError("AGENTS.md is missing the repo-specific marker")
    return text.split(AGENTS_REPO_MARKER, 1)[1]


class AmericanEnglishPromptRule(unittest.TestCase):
    def test_the_rule_is_in_the_repo_specific_agents_section(self):
        self.assertIn(AMERICAN_ENGLISH_RULE, _agents_repo_section())

    def test_the_rule_is_in_each_agent_prompt_source(self):
        missing = [
            str(path.relative_to(ROOT))
            for path in PROMPT_SOURCES
            if AMERICAN_ENGLISH_RULE not in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(missing, [], "missing the American English rule: %s"
                         % ", ".join(missing))


if __name__ == "__main__":
    unittest.main()
