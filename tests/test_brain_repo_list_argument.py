"""One command-line argument holding several repositories (loom#3040).

zsh does not split an unquoted variable, so `status $LOOM_REPOS` reaches the
command as one argument. The command accepts that form besides one argument
per repository.
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain  # noqa: E402

A = "example-owner/alpha"
B = "example-owner/beta"
C = "example-owner/gamma"


class RepoListArgumentTest(unittest.TestCase):
    def run_status(self, *repos):
        with mock.patch.object(brain, "auth_from_env", return_value="t"), \
                mock.patch.object(brain, "_print_clock"), \
                mock.patch.object(brain, "status", return_value="") as status, \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = brain.main(["status", *repos])
        return code, status, err.getvalue()

    def read(self, status):
        return status.call_args.args[1]

    def test_one_argument_split_by_a_space(self):
        code, status, _ = self.run_status("%s %s" % (A, B))
        self.assertEqual(code, 0)
        self.assertEqual(self.read(status), [A, B])

    def test_one_argument_split_by_a_comma(self):
        code, status, _ = self.run_status("%s,%s" % (A, B))
        self.assertEqual(code, 0)
        self.assertEqual(self.read(status), [A, B])

    def test_mixed_with_separate_arguments_and_empty_pieces(self):
        code, status, _ = self.run_status("%s, %s," % (A, B), C)
        self.assertEqual(code, 0)
        self.assertEqual(self.read(status), [A, B, C])

    def test_separate_arguments_still_work(self):
        code, status, _ = self.run_status(A, B)
        self.assertEqual(code, 0)
        self.assertEqual(self.read(status), [A, B])

    def test_a_bad_piece_is_refused_and_named(self):
        code, status, err = self.run_status("%s not-a-repo" % A)
        self.assertEqual(code, 1)
        status.assert_not_called()
        self.assertIn("not-a-repo", err)


if __name__ == "__main__":
    unittest.main()
