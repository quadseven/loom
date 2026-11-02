"""bus/product_read.py: the product watch read names its URL and result.

loom#2481: Muse's WoW stuck read answered 404 for four ticks. It was asking
the Loom dashboard's host, which serves no product API, for /dev/api/v2/stuck;
the overseer's own host answers 200 at /dev/api/v2/stuck and 302s the bare
/api/v2/stuck to the HTML homepage. Nothing the tick printed named the host.
"""

import contextlib
import io
import pathlib
import subprocess
import unittest

from bus import product_read as pr

ROOT = pathlib.Path(__file__).resolve().parent.parent
OVERSEER = "https://overseer.example.test"
DEV_BASE = OVERSEER + "/dev"
DASHBOARD = "https://loom.example.test/api/transcripts"
STUCK = {"members": [{"name": "Grumbles", "kind": "death-loop"}],
         "digest": {"count": 1, "longest": 600}, "checked_at": 1}


def answering(status, body=b"", headers=None, seen=None):
    def fetch(url):
        if seen is not None:
            seen.append(url)
        return status, headers or {}, body
    return fetch


def no_ssm(name, region):
    raise OSError("could not read the parameter from SSM")


def run(argv, env, fetch, ssm=no_ssm):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = pr.main(argv, env=env, fetch=fetch, ssm=ssm)
    return code, out.getvalue(), err.getvalue()


class StuckUrlTest(unittest.TestCase):
    def test_the_dev_base_reads_dev_api_v2_stuck(self):
        # The working endpoint: the overseer host's /dev world.
        self.assertEqual(pr.read_url(DEV_BASE, "stuck"), DEV_BASE + "/api/v2/stuck")
        self.assertEqual(pr.read_url(DEV_BASE + "/", "health"), DEV_BASE + "/api/v2/health")

    def test_a_query_param_is_encoded(self):
        self.assertEqual(pr.read_url(DEV_BASE, "stuck", [("guild", "cave")]),
                         DEV_BASE + "/api/v2/stuck?guild=cave")

    def test_the_dashboard_host_is_refused_as_a_base(self):
        env = {"LOOM_PRODUCT_URL": "https://loom.example.test/dev", "LOOM_TRANSCRIPT_URL": DASHBOARD}
        with self.assertRaises(pr.BaseError) as ctx:
            pr.resolve_base(env)
        self.assertIn("Loom dashboard's host", str(ctx.exception))
        self.assertIn("https://loom.example.test/dev", str(ctx.exception))

    def test_a_base_naming_the_api_path_is_refused(self):
        for base in (DEV_BASE + "/api", DEV_BASE + "/api/v2", DEV_BASE + "/api/v2/stuck"):
            with self.assertRaises(pr.BaseError):
                pr.check_base(base, {})

    def test_a_plain_http_base_is_refused(self):
        with self.assertRaises(pr.BaseError):
            pr.check_base("http://overseer.example.test/dev", {})


class ResolveBaseTest(unittest.TestCase):
    def test_env_wins(self):
        env = {"LOOM_PRODUCT_URL": DEV_BASE + "/",
               "LOOM_HEALTH_URLS": "o/n#1=https://other.example.test/x/api/v2/health"}
        self.assertEqual(pr.resolve_base(env, no_ssm), (DEV_BASE, "LOOM_PRODUCT_URL"))

    def test_the_station_health_url_gives_the_base(self):
        env = {"LOOM_HEALTH_URLS": "o/n#1=%s/api/v2/health" % DEV_BASE}
        self.assertEqual(pr.resolve_base(env, no_ssm), (DEV_BASE, "LOOM_HEALTH_URLS"))

    def test_ssm_is_the_last_source(self):
        asked = []

        def ssm(name, region):
            asked.append(name)
            return DEV_BASE + "\n"
        self.assertEqual(pr.resolve_base({}, ssm), (DEV_BASE, "SSM /muse/product_url"))
        self.assertEqual(pr.resolve_base({"LOOM_PRODUCT_URL_SSM": "/x/y"}, ssm)[1], "SSM /x/y")
        self.assertEqual(asked, ["/muse/product_url", "/x/y"])

    def test_no_source_names_every_one_tried(self):
        def timeout(name, region):
            raise subprocess.TimeoutExpired("aws", 1)
        for ssm in (no_ssm, timeout):
            with self.assertRaises(pr.BaseError) as ctx:
                pr.resolve_base({}, ssm)
            text = str(ctx.exception)
            for name in ("LOOM_PRODUCT_URL", "LOOM_HEALTH_URLS", "/muse/product_url"):
                self.assertIn(name, text)


class ReadTest(unittest.TestCase):
    ENV = {"LOOM_PRODUCT_URL": DEV_BASE, "LOOM_TRANSCRIPT_URL": DASHBOARD}

    def test_a_200_prints_the_url_and_the_body(self):
        seen = []
        code, out, err = run(["stuck"], self.ENV,
                             answering(200, b'{"members": [], "digest": {"count": 0}}', seen=seen))
        self.assertEqual(code, 0, err)
        self.assertEqual(seen, [DEV_BASE + "/api/v2/stuck"])
        first, body = out.splitlines()
        self.assertIn("GET %s/api/v2/stuck answered HTTP 200" % DEV_BASE, first)
        self.assertEqual(pr.json.loads(body)["digest"], {"count": 0})

    def test_a_404_names_the_url_and_the_status(self):
        code, out, err = run(["stuck"], self.ENV, answering(404, b"not found"))
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("GET %s/api/v2/stuck answered HTTP 404" % DEV_BASE, err)
        self.assertIn("LOOM_PRODUCT_URL", err)

    def test_a_redirect_names_its_target(self):
        # The bare /api/v2/stuck path on the overseer host 302s to /dev/.
        env = {"LOOM_PRODUCT_URL": OVERSEER}
        code, _, err = run(["stuck"], env, answering(302, headers={"Location": OVERSEER + "/dev/"}))
        self.assertEqual(code, 1)
        self.assertIn("GET %s/api/v2/stuck answered HTTP 302 to %s/dev/" % (OVERSEER, OVERSEER), err)
        self.assertIn("path prefix", err)

    def test_an_html_200_is_not_a_read(self):
        code, _, err = run(["health"], self.ENV, answering(200, b"<!doctype html><p>home"))
        self.assertEqual(code, 1)
        self.assertIn("GET %s/api/v2/health answered HTTP 200 but not JSON" % DEV_BASE, err)

    def test_a_connection_failure_names_the_url(self):
        def down(url):
            raise OSError("connection refused")
        code, _, err = run(["stuck"], self.ENV, down)
        self.assertEqual(code, 1)
        self.assertIn("GET %s/api/v2/stuck did not connect" % DEV_BASE, err)

    def test_no_base_exits_2_without_a_read(self):
        seen = []
        code, _, err = run(["stuck"], {}, answering(200, b"{}", seen=seen))
        self.assertEqual(code, 2)
        self.assertEqual(seen, [])
        self.assertIn("no product base", err)

    def test_the_dashboard_base_is_never_asked(self):
        seen = []
        env = {"LOOM_PRODUCT_URL": "https://loom.example.test/dev", "LOOM_TRANSCRIPT_URL": DASHBOARD}
        code, _, _ = run(["stuck"], env, answering(404, seen=seen))
        self.assertEqual((code, seen), (2, []))

    def test_a_param_reaches_the_url(self):
        seen = []
        run(["stuck", "--param", "guild=bonkers"], self.ENV, answering(200, b"{}", seen=seen))
        self.assertEqual(seen, [DEV_BASE + "/api/v2/stuck?guild=bonkers"])


class RedirectTest(unittest.TestCase):
    def test_the_real_fetch_does_not_follow_a_redirect(self):
        fetch = pr.urllib_fetch()
        opener = fetch.opener
        self.assertTrue(any(isinstance(h, pr._NoRedirect) for h in opener.handlers))

    def test_the_tailnet_proxy_is_used_only_for_the_get(self):
        fetch = pr.urllib_fetch("http://proxy.example.test:3130")
        opener = fetch.opener
        proxies = [h.proxies for h in opener.handlers if isinstance(h, pr.urllib.request.ProxyHandler)]
        self.assertIn({"https": "http://proxy.example.test:3130", "http": "http://proxy.example.test:3130"},
                      proxies)


class MuseDocTest(unittest.TestCase):
    """docs/MUSE.md pins the product watch read to the /dev world's base."""

    def setUp(self):
        self.doc = (ROOT / "docs" / "MUSE.md").read_text(encoding="utf-8")

    def test_the_doc_names_the_helper_and_the_dev_endpoint(self):
        self.assertTrue("python3 -m bus.product_read stuck" in self.doc, "python3 -m bus.product_read stuck")
        self.assertTrue("/dev/api/v2/stuck" in self.doc, "/dev/api/v2/stuck")
        self.assertTrue("LOOM_PRODUCT_URL" in self.doc, "LOOM_PRODUCT_URL")

    def test_the_doc_says_the_dashboard_host_is_not_the_base(self):
        self.assertTrue("never the Loom dashboard's host" in self.doc, "never the Loom dashboard's host")


if __name__ == "__main__":
    unittest.main()
