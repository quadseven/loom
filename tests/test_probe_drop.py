"""bus.probe_drop: files an agent added only to probe the sandbox."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bus import probe_drop  # noqa: E402


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class ProbeDropTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tree = Path(self._tmp.name) / "repo"
        git("init", "-q", "-b", "main", str(self.tree), cwd=self._tmp.name)
        git("-c", "user.name=t", "-c", "user.email=t@example.invalid",
            "commit", "-q", "--allow-empty", "-m", "base", cwd=self.tree)
        self.base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.tree,
                                   check=True, capture_output=True,
                                   text=True).stdout.strip()

    def tearDown(self):
        self._tmp.cleanup()

    def commit(self, files, message="agent work"):
        for path, content in files.items():
            target = self.tree / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        git("add", "-A", cwd=self.tree)
        git("-c", "user.name=t", "-c", "user.email=t@example.invalid",
            "commit", "-qm", message, cwd=self.tree)

    def probes(self):
        return probe_drop.find_probes(str(self.tree), self.base, "HEAD")

    def test_dot_test_and_bin_probe_files_are_found(self):
        self.commit({"fix.txt": b"fixed\n",
                     ".test_write": b"# test\n",
                     "bin/test": b"test bin in workspace\n"})
        self.assertEqual(sorted(self.probes()), [".test_write", "bin/test"])

    def test_empty_probe_file_is_found(self):
        self.commit({".test_write": b""})
        self.assertEqual(self.probes(), [".test_write"])

    def test_real_change_is_not_a_probe(self):
        self.commit({"fix.txt": b"fixed\n"})
        self.assertEqual(self.probes(), [])

    def test_multi_line_dot_test_file_is_not_a_probe(self):
        self.commit({".test_helper": b"line one\nline two\n"})
        self.assertEqual(self.probes(), [])

    def test_bin_file_referenced_by_the_diff_is_kept(self):
        self.commit({"bin/deploy": b"#!/bin/sh\n",
                     "Makefile": b"deploy:\n\t./bin/deploy\n"})
        self.assertEqual(self.probes(), [])

    def test_dot_test_file_referenced_by_the_diff_is_kept(self):
        self.commit({".test_fixture": b"data\n",
                     "test_all.py": b'open(".test_fixture").read()\n'})
        self.assertEqual(self.probes(), [])

    def test_bin_file_is_not_a_probe_when_bin_already_exists(self):
        git("checkout", "-q", "-b", "with-bin", cwd=self.tree)
        self.commit({"bin/keep": b"#!/bin/sh\necho keep\n"}, message="base bin")
        base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.tree,
                              check=True, capture_output=True,
                              text=True).stdout.strip()
        self.commit({"bin/test": b"probe\n"})
        found = probe_drop.find_probes(str(self.tree), base, "HEAD")
        self.assertEqual(found, [])

    def test_modified_files_are_never_probes(self):
        self.commit({"fix.txt": b"fixed\n"})
        (self.tree / "fix.txt").write_bytes(b"fixed\nagain\n")
        git("add", "fix.txt", cwd=self.tree)
        git("-c", "user.name=t", "-c", "user.email=t@example.invalid",
            "commit", "-qm", "touch fix", cwd=self.tree)
        self.assertEqual(self.probes(), [])

    def test_cli_prints_one_probe_per_line(self):
        self.commit({".test_write": b"# test\n", "fix.txt": b"fixed\n"})
        proc = subprocess.run(
            [sys.executable, "-B", "-m", "bus.probe_drop",
             str(self.tree), self.base, "HEAD"],
            capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.splitlines(), [".test_write"])

    def test_cli_usage_error(self):
        proc = subprocess.run([sys.executable, "-B", "-m", "bus.probe_drop"],
                              capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main()
