"""bus/evidence_endpoint.py: the read-only evidence endpoint the remote brain
reads service logs and database rows through (#473)."""

import json
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional
from unittest import mock

from bus import evidence, evidence_endpoint as ep

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "t" * 40
PY = sys.executable

# A log: two matching lines, one credential-shaped matching line, one line
# that does not match.
APP_LOG = ("print('10:00 party failed: room 4 stalled')\n"
           "print('10:01 party failed: token=abcdef1234567890')\n"
           "print('10:02 all quiet')\n"
           "print('10:03 party failed: module refused')\n")
FLOOD = "for i in range(5000):\n    print('line %05d party failed ' % i + 'x ' * 150)\n"
SHORT = "for i in range(5000):\n    print('short %d party failed' % i)\n"
# A log that is the arguments it was started with, so a test can read what
# the template turned into.
ARGV = "import sys\nprint('argv ' + ' '.join(sys.argv[1:]))\n"
ROWS = "for i in range(5000):\n    print('%d,stalled' % i)\n"
# A query runner that answers with the SQL it was handed on stdin.
ECHO_SQL = "import sys\nprint('received: ' + ' '.join(sys.stdin.read().split()))\n"
TOUCH = "import sys\nopen(sys.argv[1], 'w').close()\nprint('touched party failed')\n"


class EndpointTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.marker = self.tmp / "ran"
        self.config = ep.parse_config({
            "logs": {
                "app": {"command": [PY, "-c", APP_LOG, "--since={since}h"]},
                "argv": {"command": [PY, "-c", ARGV, "--since={since}h", "{match}"]},
                "flood": {"command": [PY, "-c", FLOOD]},
                "short": {"command": [PY, "-c", SHORT]},
                "touch": {"command": [PY, "-c", TOUCH, str(self.marker)]},
                "broken": {"command": [PY, "-c", "import sys\nsys.stderr.write('secret path\\n')\nsys.exit(3)"]},
                "nomatch": {"command": [PY, "-c", "import sys\nsys.exit(1)"]},
            },
            "queries": {
                "run": {"command": [PY, "-c", ECHO_SQL],
                        "sql": "SELECT id, room FROM runs WHERE id = :id AND lane = :lane",
                        "params": {"id": "int", "lane": "text"}},
                "many": {"command": [PY, "-c", ROWS], "sql": "SELECT id, state FROM runs"},
                "touchq": {"command": [PY, "-c", TOUCH, str(self.marker)],
                           "sql": "SELECT 1 WHERE :lane = 'x'", "params": {"lane": "text"}},
            },
        })
        self.logged = []
        self.server = ep.make_server("127.0.0.1", 0, self.config, TOKEN, log=self.logged.append)
        thread = threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def call(self, path, params=None, token: Optional[str] = TOKEN, method="GET"):
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        req = urllib.request.Request(url, method=method)
        if token is not None:
            req.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return resp.status, json.loads(raw), len(raw)
        except urllib.error.HTTPError as err:
            with err:
                raw = err.read()
            return err.code, json.loads(raw), len(raw)

    # A named log source and a named query answer within the caps.

    def test_a_named_log_source_answers_its_matching_lines(self):
        status, body, _ = self.call("/evidence/logs", {"source": "app", "match": "party failed", "since": "6"})
        self.assertEqual(status, 200)
        self.assertEqual(body["lines"], ["10:00 party failed: room 4 stalled",
                                         "10:03 party failed: module refused"])
        self.assertEqual(body["source"], "app")
        self.assertEqual(body["since_hours"], 6)
        self.assertFalse(body["truncated"])

    def test_the_template_receives_since_and_match_as_single_arguments(self):
        status, body, _ = self.call("/evidence/logs", {"source": "argv", "match": "argv", "since": "6"})
        self.assertEqual(status, 200)
        self.assertEqual(body["lines"], ["argv --since=6h argv"])

    def test_since_defaults_to_a_day_and_a_source_without_it_says_so(self):
        _, body, _ = self.call("/evidence/logs", {"source": "argv", "match": "argv"})
        self.assertEqual(body["lines"], ["argv --since=24h argv"])
        _, body, _ = self.call("/evidence/logs", {"source": "flood", "match": "line 00001 "})
        self.assertIsNone(body["since_hours"])

    def test_a_log_answer_keeps_the_newest_lines_within_200_lines_and_64_kb(self):
        status, body, size = self.call("/evidence/logs", {"source": "flood", "match": "party failed"})
        self.assertEqual(status, 200)
        self.assertLessEqual(size, 64 * 1024)
        self.assertLessEqual(len(body["lines"]), 200)
        self.assertTrue(body["truncated"])
        self.assertTrue(body["lines"][-1].startswith("line 04999 "))

    def test_a_log_answer_stops_at_200_lines_when_they_are_short(self):
        _, body, size = self.call("/evidence/logs", {"source": "short", "match": "party failed"})
        self.assertEqual(len(body["lines"]), 200)
        self.assertEqual(body["lines"][-1], "short 4999 party failed")
        self.assertLessEqual(size, 64 * 1024)

    def test_a_large_output_is_read_from_its_newest_end_only(self):
        with mock.patch.object(ep, "MAX_READ", 10000):
            _, body, _ = self.call("/evidence/logs", {"source": "flood", "match": "party failed"})
        self.assertTrue(body["output_cut"])
        self.assertTrue(body["lines"][-1].startswith("line 04999 "))
        # The line the cut went through is left out, not quoted in part.
        self.assertTrue(all(line.startswith("line ") for line in body["lines"]))
        _, body, _ = self.call("/evidence/logs", {"source": "app", "match": "party failed"})
        self.assertFalse(body["output_cut"])

    def test_a_large_query_output_is_read_from_its_start_only(self):
        with mock.patch.object(ep, "MAX_READ", 100):
            _, body, _ = self.call("/evidence/query", {"name": "many"})
        self.assertTrue(body["output_cut"])
        self.assertEqual(body["rows"][0], "0,stalled")
        self.assertTrue(all(row.endswith(",stalled") for row in body["rows"]))

    def test_a_call_while_every_command_slot_is_busy_answers_503(self):
        with mock.patch.object(ep, "_SLOTS", threading.BoundedSemaphore(1)) as slots:
            slots.acquire()
            status, body, _ = self.call("/evidence/logs", {"source": "touch", "match": "x"})
        self.assertEqual(status, 503)
        self.assertIn("retry", body["error"])
        self.assertFalse(self.marker.exists(), "a busy call ran its command")

    def test_a_named_query_answers_with_its_typed_params_bound(self):
        status, body, _ = self.call("/evidence/query", {"name": "run", "id": "42", "lane": "claude-2"})
        self.assertEqual(status, 200)
        self.assertEqual(body["rows"], ["received: SELECT id, room FROM runs WHERE id = 42 AND lane = 'claude-2'"])
        self.assertEqual(body["params"], {"id": "42", "lane": "claude-2"})

    def test_a_query_answer_keeps_the_first_200_rows(self):
        status, body, size = self.call("/evidence/query", {"name": "many"})
        self.assertEqual(status, 200)
        self.assertEqual(len(body["rows"]), 200)
        self.assertEqual(body["rows"][0], "0,stalled")
        self.assertTrue(body["truncated"])
        self.assertLessEqual(size, 64 * 1024)

    def test_the_catalog_names_sources_and_queries_but_not_commands_or_sql(self):
        status, body, _ = self.call("/evidence")
        self.assertEqual(status, 200)
        self.assertEqual(body["queries"]["run"], {"id": "int", "lane": "text"})
        self.assertEqual(body["logs"]["app"], {"since": True})
        self.assertEqual(body["logs"]["flood"], {"since": False})
        self.assertNotIn("SELECT", json.dumps(body))
        self.assertNotIn(PY, json.dumps(body))

    # Unnamed sources and queries are refused.

    def test_an_unnamed_log_source_is_refused(self):
        status, body, _ = self.call("/evidence/logs", {"source": "sh", "match": "x"})
        self.assertEqual(status, 404)
        self.assertIn("no log source", body["error"])

    def test_an_unnamed_query_is_refused(self):
        for params in ({"name": "SELECT 1"}, {"name": "drop"}, {}):
            with self.subTest(params=params):
                status, body, _ = self.call("/evidence/query", params)
                self.assertEqual(status, 404)
                self.assertIn("no query", body["error"])

    def test_an_unknown_path_is_refused(self):
        self.assertEqual(self.call("/evidence/shell", {"cmd": "id"})[0], 404)

    # Metacharacters in parameters are refused before anything runs.

    METACHARACTERS = ["x'; DROP TABLE runs; --", "a;b", "$(id)", "`id`", "a|b", "a&b", "a>b",
                      "a<b", "a\\b", 'a"b', "a\nb", "a -- b", "*", "a{b}", "a#b", "a!b", "a$b"]

    def test_a_metacharacter_in_a_log_match_is_refused(self):
        for bad in self.METACHARACTERS:
            with self.subTest(match=bad):
                status, body, _ = self.call("/evidence/logs", {"source": "touch", "match": bad})
                self.assertEqual(status, 400)
                self.assertIn("metacharacter", body["error"])
        self.assertFalse(self.marker.exists(), "a refused call ran its command")

    def test_a_match_that_reads_as_a_command_option_is_refused(self):
        # {match} may be a whole argument: "-f/etc/passwd" or "-oout" would be
        # an option to the configured command, not a string to look for.
        for bad in ("-f/etc/passwd", "-oout", "-", "-v"):
            with self.subTest(match=bad):
                status, body, _ = self.call("/evidence/logs", {"source": "touch", "match": bad})
                self.assertEqual(status, 400)
                self.assertIn("may not start with -", body["error"])
        self.assertFalse(self.marker.exists(), "a refused call ran its command")
        _, body, _ = self.call("/evidence/logs", {"source": "argv", "match": "a-b"})
        self.assertEqual(body["lines"], ["argv --since=24h a-b"])

    def test_a_metacharacter_in_a_query_param_is_refused(self):
        for bad in self.METACHARACTERS:
            with self.subTest(lane=bad):
                status, _, _ = self.call("/evidence/query", {"name": "touchq", "lane": bad})
                self.assertEqual(status, 400)
        self.assertFalse(self.marker.exists(), "a refused call ran its command")

    def test_a_non_number_int_param_is_refused(self):
        for bad in ("1 OR 1=1", "-1", "1.5", "", "0x10"):
            with self.subTest(id=bad):
                status, _, _ = self.call("/evidence/query", {"name": "run", "id": bad, "lane": "a"})
                self.assertEqual(status, 400)

    def test_missing_unknown_and_repeated_params_are_refused(self):
        cases = ["name=run&id=1",
                 "name=run&id=1&lane=a&extra=1",
                 "name=run&id=1&id=2&lane=a",
                 "source=app",
                 "source=app&match=x&since=0",
                 "source=app&match=x&since=169",
                 "source=app&match=x&command=id"]
        for query in cases:
            with self.subTest(query=query):
                path = "/evidence/query" if query.startswith("name") else "/evidence/logs"
                req = urllib.request.Request(self.base + path + "?" + query,
                                             headers={"Authorization": "Bearer " + TOKEN})
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(req, timeout=30)
                caught.exception.close()
                self.assertEqual(caught.exception.code, 400)

    def test_a_write_method_is_refused(self):
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
            with self.subTest(method=method):
                self.assertEqual(self.call("/evidence/logs", method=method)[0], 405)

    def test_head_is_refused_without_a_body_and_logged(self):
        req = urllib.request.Request(self.base + "/evidence", method="HEAD",
                                     headers={"Authorization": "Bearer " + TOKEN})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req, timeout=30)
        with caught.exception as err:
            self.assertEqual(err.code, 405)
            self.assertEqual(err.read(), b"")
        self.assertIn("HEAD status=405", self.logged[-1])

    # Credential-shaped lines are dropped and counted.

    def test_a_credential_shaped_line_is_dropped_and_counted_in_the_log(self):
        _, body, _ = self.call("/evidence/logs", {"source": "app", "match": "party failed"})
        self.assertNotIn("abcdef1234567890", json.dumps(body))
        self.assertNotIn("dropped", body)
        self.assertIn("lines=2 dropped=1 ", self.logged[-1])

    def test_an_answer_never_says_whether_a_withheld_line_matched(self):
        # Otherwise match=token=a, token=b, ... would read a withheld secret
        # one character at a time.
        _, hit, _ = self.call("/evidence/logs", {"source": "app", "match": "token=abc"})
        _, miss, _ = self.call("/evidence/logs", {"source": "app", "match": "token=zzz"})
        self.assertEqual(dict(hit, match=None), dict(miss, match=None))
        self.assertIn("dropped=1 ", self.logged[-2])
        self.assertIn("dropped=0 ", self.logged[-1])

    def test_private_hosts_addresses_and_paths_are_dropped_from_rows_too(self):
        lines = ["ok row", "db.corp reached", "from 192.0.2.7", "at /Users/someone/x",
                 "Bearer abcdefghijklmnop", "ok \u00e9"]
        body, dropped = ep.capped(lines, {"query": "q"}, "rows", newest=False)
        self.assertEqual(body["rows"], ["ok row"])
        self.assertEqual(dropped, 5)

    # Failures are stated without the command's output.

    def test_a_failing_command_answers_502_without_its_stderr(self):
        status, body, _ = self.call("/evidence/logs", {"source": "broken", "match": "x"})
        self.assertEqual(status, 502)
        self.assertIn("exited 3", body["error"])
        self.assertNotIn("secret", json.dumps(body))

    def test_exit_1_with_nothing_on_stderr_is_no_match(self):
        status, body, _ = self.call("/evidence/logs", {"source": "nomatch", "match": "x"})
        self.assertEqual(status, 200)
        self.assertEqual(body["lines"], [])

    def test_a_command_that_runs_too_long_answers_504(self):
        config = ep.parse_config({"logs": {"slow": {"command": [PY, "-c", "import time\ntime.sleep(30)"]}}})
        with mock.patch.object(evidence, "TIMEOUT", 1):
            status, body, _ = ep.answer(config, "/evidence/logs", "source=slow&match=x")
        self.assertEqual(status, 504)
        self.assertIn("timed out", body["error"])

    # The bearer token, and a log line for every call.

    def test_a_call_without_the_bearer_token_is_refused_401(self):
        for token in (None, "", "wrong", TOKEN + "x", "t" * 39):
            with self.subTest(token=token):
                status, body, _ = self.call("/evidence/logs", {"source": "touch", "match": "x"}, token=token)
                self.assertEqual(status, 401)
                self.assertEqual(body, {"error": "unauthorized"})
        self.assertFalse(self.marker.exists(), "an unauthorized call ran its command")

    def test_a_token_that_is_not_ascii_is_refused_not_crashed(self):
        self.assertFalse(ep.authorized("Bearer \u00e9" * 3, TOKEN))
        self.assertFalse(ep.authorized("Bearer " + TOKEN, ""))
        self.assertTrue(ep.authorized("Bearer " + TOKEN, TOKEN))

    def test_every_call_is_logged_with_source_params_and_size(self):
        calls = [("/evidence/logs", {"source": "app", "match": "party failed"}, TOKEN),
                 ("/evidence/logs", {"source": "app", "match": "x"}, None),
                 ("/evidence/query", {"name": "run", "id": "7", "lane": "a;b"}, TOKEN),
                 ("/evidence", None, TOKEN)]
        sizes = [self.call(path, params, token=token)[2] for path, params, token in calls]
        self.assertEqual(len(self.logged), 4)
        first, unauthorized, refused, catalog = self.logged
        self.assertIn("status=200", first)
        self.assertIn('source="app"', first)
        self.assertIn('"match": ["party failed"]', first)
        self.assertIn("lines=2 dropped=1 bytes=%d" % sizes[0], first)
        self.assertIn("status=401", unauthorized)
        self.assertIn('source="app"', unauthorized)
        self.assertIn("status=400", refused)
        self.assertIn('source="run"', refused)
        self.assertIn('"lane": ["a;b"]', refused)
        self.assertIn("status=200 path=/evidence ", catalog)
        self.assertNotIn(TOKEN, "\n".join(self.logged))


class ConfigTest(unittest.TestCase):
    def query(self, sql, params=None, command=None):
        return ep.parse_config({"queries": {"q": {"command": command or ["psql"], "sql": sql,
                                                  "params": params or {}}}})

    def test_a_query_must_be_one_read_only_select(self):
        for sql in ("DELETE FROM runs", "SELECT 1; DROP TABLE runs", "UPDATE runs SET a = 1",
                    "SELECT * INTO copy_of_runs FROM runs", "SELECT * FROM runs FOR UPDATE",
                    "WITH gone AS (DELETE FROM runs RETURNING id) SELECT id FROM gone",
                    "INSERT INTO runs VALUES (1)", "TRUNCATE runs", "COPY runs TO PROGRAM 'id'"):
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                self.query(sql)

    def test_reads_with_casts_and_column_names_like_write_words_load(self):
        config = self.query("WITH r AS (SELECT id, created_at, updated_at, deleted FROM runs) "
                            "SELECT id::text FROM r WHERE id = :id", {"id": "int"})
        self.assertEqual(config.queries["q"].params, {"id": "int"})

    def test_declared_params_and_placeholders_must_agree(self):
        for sql, params in (("SELECT 1 WHERE a = :a", {}),
                            ("SELECT 1", {"a": "int"}),
                            ("SELECT 1 WHERE a = :a", {"a": "float"}),
                            ("SELECT 1 WHERE a = :name", {"name": "int"})):
            with self.subTest(sql=sql, params=params), self.assertRaises(ValueError):
                self.query(sql, params)

    def test_names_and_commands_are_checked(self):
        for data in ({"logs": {"Bad Name": {"command": ["tail"]}}},
                     {"logs": {"a": {"command": "tail -f x | sh"}}},
                     {"logs": {"a": {"command": []}}},
                     {"logs": {"a": {"command": ["tail"], "shell": True}}},
                     {"logs": []},
                     {"scripts": {}},
                     []):
            with self.subTest(data=data), self.assertRaises(ValueError):
                ep.parse_config(data)

    def test_the_example_config_loads(self):
        config = ep.load_config(str(ROOT / "lanes" / "mac" / "evidence.example.json"))
        self.assertTrue(config.logs)
        self.assertTrue(config.queries)


class SettingsTest(unittest.TestCase):
    ENV = {"LOOM_EVIDENCE_HOST": "100.64.0.9", "LOOM_EVIDENCE_CONFIG": "/dev/null",
           "LOOM_EVIDENCE_TOKEN_SSM": "/example/token", "LOOM_AWS_REGION": "us-east-1"}

    def test_the_service_binds_only_a_tailnet_or_loopback_address(self):
        for host in ("100.64.0.1", "100.127.255.254", "fd7a:115c:a1e0::1", "127.0.0.1", "::1"):
            with self.subTest(host=host):
                self.assertTrue(ep.tailnet_host(host))
        for host in ("", "0.0.0.0", "::", "192.168.1.5", "10.0.0.2", "100.128.0.1",
                     "203.0.113.4", "example.internal", "fd00::1"):
            with self.subTest(host=host):
                self.assertFalse(ep.tailnet_host(host))
                with self.assertRaises(ValueError):
                    ep.Settings.from_env(dict(self.ENV, LOOM_EVIDENCE_HOST=host))

    def test_settings_need_config_token_and_region(self):
        settings = ep.Settings.from_env(self.ENV)
        self.assertEqual(settings.port, ep.DEFAULT_PORT)
        for key in ("LOOM_EVIDENCE_CONFIG", "LOOM_EVIDENCE_TOKEN_SSM", "LOOM_AWS_REGION"):
            with self.subTest(missing=key), self.assertRaises(ValueError):
                ep.Settings.from_env(dict(self.ENV, **{key: ""}))
        for port in ("0", "70000", "web"):
            with self.subTest(port=port), self.assertRaises(ValueError):
                ep.Settings.from_env(dict(self.ENV, LOOM_EVIDENCE_PORT=port))

    def test_main_refuses_a_short_token_and_a_bad_config(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        good = tmp / "config.json"
        good.write_text('{"logs": {}}')
        bad = tmp / "bad.json"
        bad.write_text("{")
        env = dict(self.ENV, LOOM_EVIDENCE_HOST="127.0.0.1", LOOM_EVIDENCE_PORT="1")
        with mock.patch("sys.stderr"):
            self.assertEqual(ep.main(dict(env, LOOM_EVIDENCE_CONFIG=str(good)),
                                     read_secret=lambda name, region: "short"), 2)
            self.assertEqual(ep.main(dict(env, LOOM_EVIDENCE_CONFIG=str(bad)),
                                     read_secret=lambda name, region: TOKEN), 2)


if __name__ == "__main__":
    unittest.main()
