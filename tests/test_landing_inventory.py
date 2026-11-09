from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).parent.parent
BUS_DIR = REPO_ROOT / "bus"
LANES_DIR = REPO_ROOT / "lanes"
LANDING_DOC = REPO_ROOT / "docs" / "LANDING.md"


MERGE_CALL_PATTERNS = [
    (re.compile(r"\bbrain\.merge\s*\("), "bus.brain.merge"),
    (re.compile(r"\bself\.merge\s*\("), "bus.factory._merge (via self.merge)"),
]


EXPECTED_CALL_SITES = {
    "bus/roll_pin.py:372": "bus.roll_pin -> brain.merge (adopt=True)",
    "bus/deploy_watch.py:592": "bus.deploy_watch -> brain.merge (normal)",
    "bus/factory.py:4282": "bus.factory._merge -> self.merge (first attempt)",
    "bus/factory.py:4287": "bus.factory._merge -> self.merge (retry after dismiss)",
}


def scan_merge_calls() -> dict[str, list[tuple[int, str]]]:
    """Scan bus/ and lanes/ for merge endpoint calls. Returns dict of file: [(line, match)]."""
    results: dict[str, list[tuple[int, str]]] = {}
    for root in (BUS_DIR, LANES_DIR):
        if not root.exists():
            continue
        for py_file in root.rglob("*.py"):
            rel = py_file.relative_to(REPO_ROOT)
            try:
                text = py_file.read_text(encoding="utf-8")
            except Exception:
                continue

            # Track docstring state
            in_docstring = False
            docstring_delim = None
            lines = text.splitlines()

            for line_no, line in enumerate(lines, 1):
                stripped = line.strip()

                # Handle docstring start/end
                if '"""' in line or "'''" in line:
                    if not in_docstring:
                        in_docstring = True
                        # Check if it's a single-line docstring
                        if line.count('"""') >= 2 or line.count("'''") >= 2:
                            in_docstring = False
                        else:
                            docstring_delim = '"""' if '"""' in line else "'''"
                    elif docstring_delim in line:
                        in_docstring = False
                        docstring_delim = None

                # Skip comments and docstrings
                if stripped.startswith("#") or in_docstring:
                    continue

                # Check patterns
                for pattern, label in MERGE_CALL_PATTERNS:
                    if pattern.search(line):
                        results.setdefault(str(rel), []).append((line_no, f"{label}: {stripped}"))
    return results


def load_landing_doc() -> str:
    """Load the LANDING.md documentation."""
    if not LANDING_DOC.exists():
        return ""
    return LANDING_DOC.read_text(encoding="utf-8")


class LandingInventoryTest(unittest.TestCase):
    """Test that every merge endpoint call in bus/ and lanes/ is documented in docs/LANDING.md."""

    def test_landing_doc_exists(self):
        self.assertTrue(LANDING_DOC.exists(), "docs/LANDING.md must exist")

    def test_all_merge_calls_documented(self):
        """Every merge call site must appear in LANDING.md."""
        calls = scan_merge_calls()
        doc = load_landing_doc()

        undocumented: list[str] = []
        for file_path, matches in calls.items():
            for line_no, match_text in matches:
                key = f"{file_path}:{line_no}"
                if key not in EXPECTED_CALL_SITES:
                    if key not in doc:
                        undocumented.append(f"{key} — {match_text}")

        if undocumented:
            self.fail(
                "Undocumented merge call sites found:\n  - "
                + "\n  - ".join(undocumented)
                + "\n\nAdd them to docs/LANDING.md under the appropriate endpoint."
            )

    def test_expected_call_sites_present(self):
        """All expected call sites must be present in the codebase (not removed)."""
        calls = scan_merge_calls()
        all_keys = {f"{f}:{l}" for f, matches in calls.items() for l, _ in matches}

        missing = [k for k in EXPECTED_CALL_SITES if k not in all_keys]
        if missing:
            self.fail(
                "Expected merge call sites no longer found in codebase:\n  - "
                + "\n  - ".join(missing)
                + "\n\nUpdate docs/LANDING.md and this test's EXPECTED_CALL_SITES."
            )

    def test_landing_doc_mentions_all_endpoints(self):
        """LANDING.md must document both merge endpoints by name."""
        doc = load_landing_doc()
        self.assertIn("bus.brain.merge", doc, "LANDING.md must document bus.brain.merge")
        self.assertIn("bus.factory._merge", doc, "LANDING.md must document bus.factory._merge")
        self.assertIn("bus/roll_pin.py:372", doc, "LANDING.md must document roll_pin call site")
        self.assertIn("bus/deploy_watch.py:592", doc, "LANDING.md must document deploy_watch call site")
        self.assertIn("bus/factory.py:4282", doc, "LANDING.md must document factory first attempt")
        self.assertIn("bus/factory.py:4287", doc, "LANDING.md must document factory retry")


if __name__ == "__main__":
    unittest.main()