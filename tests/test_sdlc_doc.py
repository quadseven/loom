"""docs/SDLC.md must agree with the tree: its stage headings, every path and
label it cites, and the live and planned marks on its principles.

#226 merged the document with no test behind it, so its planned principles
kept citing tickets whose work had already merged and nobody noticed. A test
is the only thing in this repo that reads the document on every change.

CI holds no GitHub token, so "open" is not asked of the API: a planned
principle must write its ticket out as `#N` (not hide it in a link), and must
not cite a number this branch's history already carries. On a shallow CI
checkout the history holds one commit, and the second half simply has less to
look at; the shape of the citation is checked either way.
"""
from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "SDLC.md"
MUSE = ROOT / "docs" / "MUSE.md"

sys.path.insert(0, str(ROOT))

from bus import labels  # noqa: E402

STAGES = ["brief", "run", "PR", "review", "merge", "after merge",
          "deploy", "verify"]

HEADING = re.compile(r"^(#{1,6}) +(.*\S)\s*$")
BULLET = re.compile(r"^[-*] +(\S.*)$")
SPAN = re.compile(r"`([^`\n]+)`")
PATH = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+\.[A-Za-z0-9]+$")
LABEL = re.compile(r"^(?:loom|class):[a-z0-9:_-]+$")
TICKET = re.compile(r"#([0-9]+)\b")
LINK_TARGET = re.compile(r"\]\([^)]*\)")


class SdlcDoc(unittest.TestCase):
    """docs/SDLC.md, read the way a reader reads it: headings, bullets and
    the backticked tokens between them."""

    def setUp(self):
        self.text = DOC.read_text(encoding="utf-8")
        self.lines = self.text.splitlines()

    def section(self, title):
        """The lines under a heading, up to the next heading at its level or
        above: a '## Stages' section keeps its '### ' stages."""
        out, level, inside = [], None, False
        for line in self.lines:
            found = HEADING.match(line)
            if not found:
                if inside:
                    out.append(line)
                continue
            if not inside:
                inside = found.group(2) == title
                level = len(found.group(1))
                continue
            if len(found.group(1)) <= level:
                break
            out.append(line)
        self.assertTrue(inside, "docs/SDLC.md has no %r heading" % title)
        return out

    def bullets(self, title):
        return [BULLET.match(line).group(1)
                for line in self.section(title) if BULLET.match(line)]

    def spans(self):
        return SPAN.findall(self.text)

    def merged(self):
        """Ticket numbers this branch's history already carries."""
        log = subprocess.run(["git", "log", "--format=%s"], cwd=ROOT,
                             capture_output=True, text=True, check=False)
        if log.returncode != 0:
            return set()
        return set(TICKET.findall(log.stdout))

    def test_the_eight_stages_are_headed_in_order(self):
        stages = [HEADING.match(line).group(2)
                  for line in self.section("Stages") if HEADING.match(line)]
        self.assertEqual(stages, STAGES)

    def test_every_backticked_repo_path_exists(self):
        named = [span for span in self.spans() if PATH.match(span)]
        self.assertTrue(named, "docs/SDLC.md names no module")
        for span in named:
            self.assertTrue((ROOT / span).exists(), "%s does not exist" % span)

    def test_every_backticked_label_is_defined(self):
        named = [span for span in self.spans() if LABEL.match(span)]
        self.assertTrue(named, "docs/SDLC.md names no label")
        for span in named:
            self.assertTrue(span in labels.LABELS, "no label named %s" % span)

    def test_every_live_principle_names_a_module(self):
        live = self.bullets("Live principles")
        self.assertTrue(live, "docs/SDLC.md lists no live principles")
        for line in live:
            named = [span for span in SPAN.findall(line) if PATH.match(span)]
            self.assertTrue(named, "no module named: %s" % line)

    def test_every_planned_principle_cites_an_open_ticket(self):
        merged = self.merged()
        for line in self.bullets("Planned principles"):
            cited = TICKET.findall(LINK_TARGET.sub("", line))
            self.assertEqual(len(cited), 1,
                             "one ticket written out as #N: %s" % line)
            self.assertNotIn(cited[0], merged,
                             "already merged, so not planned: %s" % line)

    def test_each_muse_tick_step_records_its_name_first(self):
        text = MUSE.read_text(encoding="utf-8")
        for step in ("lock", "status", "merge", "verify", "failed", "asks",
                     "brief", "triage", "sweep", "journal", "unlock"):
            with self.subTest(step=step):
                number = {"lock": 1, "status": 2, "merge": 3, "verify": 4,
                          "failed": 5, "asks": 6, "brief": 7, "triage": 8,
                          "sweep": 9, "journal": 10, "unlock": 11}[step]
                end = r"(?=\n%d\.|\Z)" % (number + 1)
                self.assertRegex(text, r"(?ms)^%d\..*?python3 -m bus\.brain step %s.*?%s"
                                 % (number, step, end))


if __name__ == "__main__":
    unittest.main()
