"""This repo is public: dispatch code must not publish to it or pin a machine.

#13: the dispatcher used to push state to a branch of this repo every cycle
and load a credential helper from another machine's home directory.
"""

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DISPATCH = ROOT / "dispatch"

# Writing a ref or a file through the GitHub API, or pushing with git.
WRITES_TO_GITHUB = re.compile(r"git/refs|git/commits|/contents/|git\s+push")
HOME_PATH = re.compile(r"/(?:home|Users)/[a-z][\w.-]*/")


class DispatchHygiene(unittest.TestCase):
    def sources(self):
        files = sorted(DISPATCH.glob("*.py"))
        self.assertTrue(files)
        sources = {}
        for f in files:
            try:
                sources[f.name] = f.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as e:
                self.fail(f"cannot read {f.name}: {e}")
        return sources

    def test_no_dispatch_code_writes_to_github(self):
        for name, text in self.sources().items():
            self.assertIsNone(WRITES_TO_GITHUB.search(text), name)

    def test_no_dispatch_code_names_a_home_directory(self):
        for name, text in self.sources().items():
            self.assertIsNone(HOME_PATH.search(text), name)


class RepoText(unittest.TestCase):
    """Every tracked file is ASCII: lanes write prose here, and an em dash or a
    smart quote slips in unnoticed unless a test says so."""

    def tracked(self):
        out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        files = [ROOT / p for p in out.stdout.decode().split("\0") if p]
        self.assertTrue(files)
        return files

    # Copied verbatim from the shared public-repo rules in infra-public, as in
    # the operator's other public repos; it changes there, not here.
    VENDORED = {"AGENTS.md"}
    PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

    @classmethod
    def is_png(cls, path):
        """A real PNG image: the .png suffix and the PNG signature, so a text
        file renamed to .png is still checked."""
        if path.suffix != ".png":
            return False
        with open(path, "rb") as fh:
            return fh.read(len(cls.PNG_SIGNATURE)) == cls.PNG_SIGNATURE

    def test_a_png_is_exempt_only_when_it_is_one(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            real = Path(d, "icon.png")
            real.write_bytes(self.PNG_SIGNATURE + b"\xff\x00")
            fake = Path(d, "notes.png")
            fake.write_text("smart quote \u201c here\n", encoding="utf-8")
            other = Path(d, "icon.bin")
            other.write_bytes(self.PNG_SIGNATURE + b"\xff")
            self.assertTrue(self.is_png(real))
            self.assertFalse(self.is_png(fake))
            self.assertFalse(self.is_png(other))

    def test_every_tracked_file_is_ascii(self):
        bad = []
        for f in self.tracked():
            if not f.is_file() or f.relative_to(ROOT).as_posix() in self.VENDORED or self.is_png(f):
                continue
            for n, line in enumerate(f.read_bytes().splitlines(), 1):
                if any(b > 0x7F for b in line):
                    bad.append("%s:%d" % (f.relative_to(ROOT), n))
        self.assertEqual(bad, [], "non-ASCII text: " + ", ".join(bad[:20]))


if __name__ == "__main__":
    unittest.main()
