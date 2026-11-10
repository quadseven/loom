"""No file under tests/ holds a cross-repo issue ref literal.

The public repo leak scan (infra-public check_private_leaks.py, rule
`private-issue-ref`) flags `name#N` and `owner/name#N`, and the lane scan
exempts only the ticket's own repo. A fixture literal of that shape parks any
lane run that edits or copies the line. Build such strings in code instead, or
mark the line with the guard's allow marker. This test defines the same shape
in code, so it holds no flaggable literal itself.
"""
import re
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
MARKER = "leak-guard-" + "allow"
OWN_REPO = "quadseven/loom"
# Mirrors the scanner rule: not mid-word or a path tail, optional owner/,
# a name ending alphanumeric, then '#' and digits.
REF = re.compile(
    r"(?<![\w/-])"
    r"(?:[a-z0-9][a-z0-9._-]*/)?"
    r"[a-z][a-z0-9_]*(?:-[a-z0-9]+)*[a-z0-9]" + "#" + r"\d+\b",
    re.I,
)
OWN_NAMES = (OWN_REPO, OWN_REPO.split("/", 1)[1])


def flagged(line):
    """The ref literals on `line` the scan would flag; [] on a marked line."""
    if MARKER in line:
        return []
    hits = []
    for m in REF.finditer(line):
        if m.group(0).split("#", 1)[0] in OWN_NAMES:
            continue
        hits.append(m.group(0))
    return hits


class FlaggedShape(unittest.TestCase):
    def test_flags_qualified_and_bare_refs(self):
        self.assertTrue(flagged("x = '%s#%d'" % ("example/repo", 1)))
        self.assertTrue(flagged("x = '%s#%d'" % ("other-repo", 12)))

    def test_exempts_own_repo_marker_and_hyphenated_prose(self):
        self.assertEqual(flagged("see %s#%d" % (OWN_REPO, 5)), [])
        self.assertEqual(flagged("see %s#%d" % ("loom", 5)), [])
        self.assertEqual(flagged("a %s#%d  # %s" % ("example/repo", 1, MARKER)), [])
        self.assertEqual(flagged("Post-#77"), [])


class NoRefLiteralsInTests(unittest.TestCase):
    def test_no_cross_repo_ref_literal_under_tests(self):
        found = []
        for path in sorted(TESTS.glob("*.py")):
            text = path.read_text(encoding="utf-8", errors="replace")
            for n, line in enumerate(text.splitlines(), 1):
                for hit in flagged(line):
                    found.append("tests/%s:%d: %s" % (path.name, n, hit))
        self.assertEqual(
            found, [],
            "cross-repo ref literal the leak scan flags; build it in code or "
            "mark the line %s:\n  %s" % (MARKER, "\n  ".join(found)),
        )


if __name__ == "__main__":
    unittest.main()
