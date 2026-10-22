"""docs/ARCHITECTURE.md's worker-lane list must agree with the lanes
lanes/run_lane.sh runs.

The section named codex/, claude/ and five more as directories under lanes/
that never existed, and missed agy, workersai, spark and free, which
run_agent() had dispatched for weeks. Nothing read the document, so every new
lane widened the gap. This test reads the `case "$LANE"` arms of run_agent()
and the backticked tokens of the '3. Worker lanes' section, so a lane added to
one and not the other fails here by name.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "ARCHITECTURE.md"
RUNNER = ROOT / "lanes" / "run_lane.sh"
LANES_DIR = ROOT / "lanes"
TITLE = "3. Worker lanes"

HEADING = re.compile(r"^(#{1,6}) +(.*\S)\s*$")
SPAN = re.compile(r"`([^`\n]+)`")
ARM = re.compile(r"^\s*([A-Za-z0-9_|*-]+)\)")
NAME = re.compile(r"^[a-z][a-z0-9_.-]*$")
DIRNAME = re.compile(r"^[a-z][a-z0-9-]*/$")


def run_agent_lanes(script: str) -> set[str]:
    """The lane names the `case "$LANE"` arms of run_agent() dispatch, with
    the catch-all `*` arm dropped."""
    body = re.search(r"(?ms)^run_agent\(\) \{\n(.*?)^\}", script)
    if not body:
        return set()
    lanes = set()
    for line in body.group(1).splitlines():
        found = ARM.match(line)
        if found:
            lanes.update(n for n in found.group(1).split("|") if n != "*")
    return lanes


def section(text: str, title: str) -> str | None:
    """The text under the heading that starts with `title`, up to the next
    heading at its level or above; None when there is no such heading."""
    out, level, inside = [], None, False
    for line in text.splitlines():
        found = HEADING.match(line)
        if not found:
            if inside:
                out.append(line)
            continue
        if not inside:
            inside = found.group(2).startswith(title)
            level = len(found.group(1))
            continue
        if len(found.group(1)) <= level:
            break
        out.append(line)
    return "\n".join(out) if inside else None


def missing_lanes(text: str, lanes: set[str]) -> list[str]:
    """Lanes the section never names in backticks."""
    named = set(SPAN.findall(text))
    return sorted(lanes - named)


def stale_tokens(text: str, lanes: set[str], lanes_dir: Path) -> list[str]:
    """Backticked tokens that point at nothing: a `name/` that is not a
    directory under lanes/, a bare `name` that is neither a lane nor an entry
    under lanes/, or a `lanes/...` path that does not exist."""
    stale = []
    for span in SPAN.findall(text):
        if span.startswith("lanes/"):
            ok = (lanes_dir.parent / span).exists()
        elif DIRNAME.match(span):
            ok = (lanes_dir / span).is_dir()
        elif NAME.match(span):
            ok = span in lanes or (lanes_dir / span).exists()
        else:
            continue
        if not ok:
            stale.append(span)
    return stale


class ArchitectureDoc(unittest.TestCase):
    """The '3. Worker lanes' section against run_agent() and the tree."""

    def setUp(self):
        self.lanes = run_agent_lanes(RUNNER.read_text(encoding="utf-8"))
        self.text = section(DOC.read_text(encoding="utf-8"), TITLE)
        self.assertIsNotNone(self.text, "docs/ARCHITECTURE.md has no %r heading" % TITLE)

    def test_run_agent_arms_are_read(self):
        # A parse that found nothing would pass every check below.
        self.assertTrue({"codex", "claude", "anthropic"} <= self.lanes, self.lanes)
        self.assertNotIn("*", self.lanes)

    def test_every_run_agent_lane_is_listed(self):
        self.assertEqual(missing_lanes(self.text, self.lanes), [],
                         "lanes run_agent() dispatches that docs/ARCHITECTURE.md omits")

    def test_every_backticked_lane_token_exists(self):
        self.assertEqual(stale_tokens(self.text, self.lanes, LANES_DIR), [],
                         "tokens in docs/ARCHITECTURE.md that name no lane or file")


class Fixtures(unittest.TestCase):
    """The checks report what is wrong by name."""

    SCRIPT = ("run_agent() {\n"
              "  case \"$LANE\" in\n"
              "    codex|claude) agent_cli \"$LANE\" ;;\n"
              "    spark|free) opencode_agent - ;;\n"
              "    *) echo \"unknown lane $LANE\" >&2; return 2 ;;\n"
              "  esac\n"
              "}\n")

    def test_arms_split_on_bar_and_drop_the_catch_all(self):
        self.assertEqual(run_agent_lanes(self.SCRIPT),
                         {"codex", "claude", "spark", "free"})

    def test_a_missing_lane_is_reported_by_name(self):
        doc = "## 2. x\n\n### 3. Worker lanes\n\n- `codex`\n- `claude`\n- `spark`\n\n### 4. y\n\n- `free`\n"
        text = section(doc, TITLE)
        self.assertEqual(missing_lanes(text, run_agent_lanes(self.SCRIPT)), ["free"])

    def test_a_directory_that_does_not_exist_is_reported(self):
        text = "- `codex/` - x.\n- `sparks/` - y.\n- `briefs/<lane>/` is not a lane dir.\n"
        self.assertEqual(stale_tokens(text, {"codex"}, LANES_DIR), ["codex/"])

    def test_a_bare_name_must_be_a_lane_or_under_lanes(self):
        text = "`codex`, `run_lane.sh`, `nolane`, `lanes/nothing.sh`"
        self.assertEqual(stale_tokens(text, {"codex"}, LANES_DIR),
                         ["nolane", "lanes/nothing.sh"])


if __name__ == "__main__":
    unittest.main()
