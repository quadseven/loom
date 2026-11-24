"""LOOM_LANE_APPS: a lane the map names pushes, commits and opens its pull
requests as the mapped App; every other lane keeps the lanes App; a lane never
falls back to another App's identity."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import app, lane_apps  # noqa: E402
from bus.__main__ import main  # noqa: E402
from tests import test_run_lane as base  # noqa: E402

LUNA = {"app_id": "2002", "installation_id": "3003", "key_ssm": "/example/apps/luna/private_key",
        "git_name": "luna-app[bot]", "git_email": "42+luna-app[bot]@users.noreply.github.com"}
MAP = json.dumps({"codex,codex-2": LUNA})


class Parse(unittest.TestCase):
    def test_a_slot_and_its_lane_resolve_and_an_unmapped_lane_does_not(self):
        env = {"LOOM_LANE_APPS": json.dumps({"codex": LUNA})}
        for slot in ("codex", "codex-7"):
            self.assertEqual(lane_apps.resolve(slot, env).app_id, "2002")
        for slot in ("claude", "opencode-go-2", "codex-like"):
            self.assertIsNone(lane_apps.resolve(slot, env), slot)
        self.assertIsNone(lane_apps.resolve("codex", {}))

    def test_a_list_key_names_each_slot_and_a_slot_wins_over_its_lane(self):
        other = dict(LUNA, app_id="9")
        env = {"LOOM_LANE_APPS": json.dumps({"claude, claude-2": LUNA, "claude-3": other})}
        self.assertEqual(lane_apps.resolve("claude-2", env).app_id, "2002")
        self.assertEqual(lane_apps.resolve("claude-3", env).app_id, "9")
        self.assertIsNone(lane_apps.resolve("codex-2", env))

    def test_a_missing_installation_id_means_discover(self):
        entry = {k: v for k, v in LUNA.items() if k != "installation_id"}
        got = lane_apps.resolve("codex", {"LOOM_LANE_APPS": json.dumps({"codex": entry})})
        self.assertEqual(got.installation_id, lane_apps.DISCOVER)

    def test_a_malformed_map_is_an_error_never_an_empty_map(self):
        bad = ["{", "[]", json.dumps({"codex": "x"}), json.dumps({"codex": dict(LUNA, app_id="x")}),
               json.dumps({"codex": dict(LUNA, key_ssm="relative/key")}),
               json.dumps({"codex": dict(LUNA, git_email="not-mail")}),
               json.dumps({"codex": dict(LUNA, extra="1")}),
               json.dumps({"codex": dict(LUNA, installation_id="abc")}),
               json.dumps({"Codex!": LUNA}), json.dumps({"codex": LUNA, "codex,claude": LUNA})]
        for text in bad:
            with self.assertRaises(lane_apps.LaneAppError, msg=text):
                lane_apps.resolve("claude", {"LOOM_LANE_APPS": text})


def run(argv, **kw):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv, **kw)
    return code, out.getvalue(), err.getvalue()


class Cli(unittest.TestCase):
    def identity(self, slot, text=MAP):
        with mock.patch.dict(os.environ, {"LOOM_LANE_APPS": text}, clear=True):
            return run(["app", "lane-identity", slot])

    def test_prints_the_five_values_for_a_mapped_lane(self):
        code, out, _ = self.identity("codex-2")
        self.assertEqual((code, out.splitlines()), (0, list(LUNA.values())))

    def test_prints_nothing_for_an_unmapped_lane(self):
        self.assertEqual(self.identity("claude")[:2], (0, ""))

    def test_a_malformed_map_exits_3_with_no_identity(self):
        code, out, err = self.identity("claude", "{")
        self.assertEqual((code, out), (3, ""))
        self.assertIn("LOOM_LANE_APPS", err)


class Discover(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def fake(self, installations):
        def transport(method, url, headers, body):
            self.calls.append((method, url))
            if method == "GET" and "/app/installations" in url:
                page = int(url.split("&page=")[1]) if "&page=" in url else 1
                return 200, {}, json.dumps(installations[(page - 1) * 100:page * 100]).encode()
            return 201, {}, json.dumps({"token": "ghs_luna", "expires_at": "2099-01-01T00:00:00Z"}).encode()
        return transport

    def token(self, installations, owners="acme"):
        env = {"LOOM_GITHUB_APP_ID": "2002", "LOOM_GITHUB_APP_INSTALLATION_ID": lane_apps.DISCOVER,
               "LOOM_GITHUB_APP_KEY_SSM": "/example/key", "LOOM_REPO_OWNERS": owners}
        out = io.StringIO()
        with mock.patch("bus.app.read_ssm", return_value="KEY"):
            code = app.print_token(out, env, transport=self.fake(installations),
                                   sign=lambda data, key: b"sig")
        return code, out.getvalue()

    def test_the_installation_of_the_repo_owner_is_used(self):
        got = self.token([{"id": 1, "account": {"login": "other"}}, {"id": 2, "account": {"login": "Acme"}}])
        self.assertEqual(got, (0, "ghs_luna\n"))
        self.assertIn(("POST", "https://api.github.com/app/installations/2/access_tokens"), self.calls)

    def test_an_installation_past_the_first_page_is_found(self):
        many = [{"id": n + 10, "account": {"login": "o%d" % n}} for n in range(100)]
        many.append({"id": 2, "account": {"login": "acme"}})
        self.assertEqual(self.token(many), (0, "ghs_luna\n"))
        self.assertIn(("POST", "https://api.github.com/app/installations/2/access_tokens"), self.calls)

    def test_an_only_installation_is_used(self):
        self.assertEqual(self.token([{"id": 5, "account": {"login": "x"}}], owners="")[0], 0)

    def test_an_ambiguous_App_is_an_error_and_mints_nothing(self):
        with self.assertRaises(app.GitHubError):
            self.token([{"id": 1, "account": {"login": "a"}}, {"id": 2, "account": {"login": "b"}}])
        self.assertFalse([c for c in self.calls if c[0] == "POST"])


class LaneRunsAsMappedApp(base.LaneRunnerTest):
    def seen_cmd(self, seen, fail=False):
        script = ('echo "$LOOM_GITHUB_APP_ID $LOOM_GITHUB_APP_INSTALLATION_ID '
                  '$LOOM_GITHUB_APP_KEY_SSM ${LOOM_GITHUB_APP_KEY_FILE:-nofile}" >> ' + str(seen) + "\n")
        return self.app_token_cmd(script + ("exit 1\n" if fail else "echo ghs_tok\n"))

    def head_author(self):
        out = subprocess.run(["git", "--git-dir", str(self.remote), "log", "-1",
                              "--format=%an <%ae>|%cn <%ce>", "loom/T042-fix-the-thing"],
                             capture_output=True, text=True, check=True).stdout.strip()
        return out

    def lane(self, slot="codex", map_text=MAP, fail=False, **env):
        seen = Path(self._tmp.name) / "seen"
        self.drop_brief()
        result = self.run_lane(lane=slot, LOOM_GITHUB_APP_ID="124", LOOM_GITHUB_APP_INSTALLATION_ID="77",
                               LOOM_GITHUB_APP_KEY_SSM="/example/lanes/key",
                               LOOM_GITHUB_APP_KEY_FILE="/nonexistent/lanes.pem",
                               LOOM_LANE_APPS=map_text,
                               LOOM_APP_TOKEN_CMD=self.seen_cmd(seen, fail), **env)
        return result, (seen.read_text().split("\n") if seen.exists() else [])

    def test_a_mapped_lane_mints_as_the_mapped_app_and_commits_as_it(self):
        _, seen = self.lane()
        self.assertEqual({s for s in seen if s}, {"2002 3003 /example/apps/luna/private_key nofile"})
        self.assertTrue(any(c.startswith("gh pr create") for c in self.calls()), self.calls())
        who = "luna-app[bot] <42+luna-app[bot]@users.noreply.github.com>"
        self.assertEqual(self.head_author(), who + "|" + who)

    def test_an_unmapped_lane_keeps_the_lanes_app_and_its_identity(self):
        _, seen = self.lane(map_text=json.dumps({"codex-2": LUNA}))
        self.assertEqual({s for s in seen if s}, {"124 77 /example/lanes/key /nonexistent/lanes.pem"})
        self.assertEqual(self.head_author(), "t <t@example.invalid>|t <t@example.invalid>")

    def test_no_map_leaves_a_lane_unchanged(self):
        _, seen = self.lane(map_text="")
        self.assertEqual({s for s in seen if s}, {"124 77 /example/lanes/key /nonexistent/lanes.pem"})

    def test_a_failed_mint_posts_nothing_and_never_uses_the_lanes_app(self):
        _, seen = self.lane(fail=True)
        self.assertEqual({s for s in seen if s}, {"2002 3003 /example/apps/luna/private_key nofile"})
        self.assertFalse(any(c.startswith("gh pr create") for c in self.calls()), self.calls())
        heads = subprocess.run(["git", "--git-dir", str(self.remote), "show-ref", "--heads"],
                               capture_output=True, text=True).stdout
        self.assertNotIn("loom/T042", heads)

    def test_a_malformed_map_stops_the_lane_before_any_mint_or_write(self):
        result, seen = self.lane(map_text="{")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([s for s in seen if s], [])
        self.assertFalse(any(c.startswith("gh pr create") for c in self.calls()), self.calls())


for _name in dir(base.LaneRunnerTest):
    if _name.startswith("test_"):
        setattr(LaneRunsAsMappedApp, _name, None)

if __name__ == "__main__":
    unittest.main()
