"""bus/product_read.py: `path` reads any documented /api/ path (loom#3126)."""

import contextlib
import io
import json
import unittest

from bus import product_read as pr

BASE = "https://product.example.test/world"


def run(argv, fetch):
    out, err = io.StringIO(), io.StringIO()
    env = {"LOOM_PRODUCT_URL": BASE}
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = pr.main(argv, env=env, fetch=fetch)
    return code, out.getvalue(), err.getvalue()


class PathReadTest(unittest.TestCase):
    def test_path_prints_the_json_with_params(self):
        seen = []

        def fetch(url):
            seen.append(url)
            return 200, {}, b'{"rows": [1]}'

        code, out, _ = run(["path", "/api/guildgear", "--param", "member=a"], fetch)
        self.assertEqual(code, 0)
        self.assertEqual(seen, [BASE + "/api/guildgear?member=a"])
        self.assertEqual(json.loads(out.splitlines()[-1]), {"rows": [1]})

    def test_a_path_outside_api_is_refused_with_exit_2(self):
        seen = []

        def fetch(url):
            seen.append(url)
            return 200, {}, b"{}"

        for bad in (
            "/admin/x", "api/x", "/api/../x", "/api/x?a=b", "",
            "/api/%2e%2e/admin", "/api/%2E%2E/x", "/api/.%2e/x",
            "/api/%252e%252e/x", "/api/x%3fa=b", "/api/x%5c..%5cy",
            "/api/%2e/x", "/api/x/%2e%2e",
        ):
            code, _, err = run(["path", bad], fetch)
            self.assertEqual(code, 2, bad)
            self.assertIn("not a plain /api/ path", err)
        self.assertEqual(seen, [])


if __name__ == "__main__":
    unittest.main()
