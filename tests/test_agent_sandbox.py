"""The API-lane agent's Seatbelt profile (lanes/agent_sandbox.sh): outbound
connections only to its provider's port, writes only to its tree and scratch
home. The agy lane is bounded the same way (its model endpoint's port is open,
keychain and /usr/bin/security denied), and the validation run keeps its own
rules."""

import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests import sandbox_caps

SANDBOX = Path(__file__).resolve().parent.parent / "lanes" / "agent_sandbox.sh"
WRITE_DENY = '(deny file-write* (subpath "/"))'
NET_DENY = '(deny network-outbound (remote ip "*:*"))'

# Run inside the api profile: try each write and the loopback connection, and
# print what happened. $1 is the tree, $2 a path outside it, $3 a local port.
CONFINED_PROBE = r"""
w() { if : > "$1" 2> /dev/null; then echo "$2=written"; else echo "$2=denied"; fi; }
w "$1/edit.txt" tree
w "$AGENT_HOME/tmp/scratch.txt" scratch
w "$2" outside
w /dev/null devnull
if /usr/bin/nc -z -w 1 127.0.0.1 "$3" > /dev/null 2>&1; then echo lo=reachable; else echo lo=denied; fi
"""

# Run inside the agy (default) profile: try a connection to a documentation
# range address on a port the profile does not allow, and try to run
# /usr/bin/security, and print what happened. $1 is the tree.
AGY_PROBE = r"""
if /usr/bin/nc -z -w 1 192.0.2.1 80 > /dev/null 2>&1; then echo net=reachable; else echo net=denied; fi
if /usr/bin/security --help > /dev/null 2>&1; then echo security=ran; else echo security=denied; fi
"""


class AgentSandboxTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.home = self.root / "home"
        self.tree = self.root / "tree"
        self.scratch = self.root / "scratch"
        for d in (self.home, self.tree, self.scratch / "tmp"):
            d.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def bash(self, script, *args, lane="opencode-go", **env):
        full = dict(os.environ, HOME=str(self.home), AGENT_HOME=str(self.scratch), LANE=lane,
                    AGY_HOME=str(self.scratch), SKILLS=str(self.root / "no-skills"), **env)
        return subprocess.run(["bash", "-c", '. "$1"; shift; ' + script, "bash", str(SANDBOX),
                               str(self.tree), *args],
                              capture_output=True, text=True, env=full, timeout=60)

    def profile(self, mode, lane="opencode-go", **env):
        result = self.bash('sandbox_profile "$1" bash "$2"', mode, lane=lane, **env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_an_api_agent_may_write_only_its_tree_and_scratch_home(self):
        lines = self.profile("api").splitlines()
        self.assertEqual(lines[2], WRITE_DENY)
        self.assertTrue(lines[3].startswith("(allow file-write* "), lines[3])
        self.assertIn('(literal "/dev/null")', lines[3])
        self.assertNotIn("subpath", lines[3])
        allowed = f'(allow file-read* file-write* (subpath "{self.tree}") (subpath "{self.scratch}"))'
        self.assertGreater(lines.index(allowed), 3)

    def test_an_api_agent_may_connect_only_to_its_providers_port(self):
        lines = self.profile("api").splitlines()
        allows = [line for line in lines if line.startswith("(allow network-outbound (remote ip")]
        self.assertEqual(allows, ['(allow network-outbound (remote ip "*:443"))'])
        self.assertGreater(lines.index(allows[0]), lines.index(NET_DENY))

    def test_the_spark_lane_also_reaches_its_endpoints_port(self):
        cases = {"http://spark.example.invalid:8000/v1": ["443", "8000"],
                 "http://spark.example.invalid/v1": ["443", "80"],
                 "https://spark.example.invalid/v1": ["443"],
                 "https://spark.example.invalid:443": ["443"]}
        for url, ports in cases.items():
            with self.subTest(url=url):
                profile = self.profile("api", lane="spark", SPARK_URL=url)
                allowed = [line.split('"*:')[1].split('"')[0] for line in profile.splitlines()
                           if line.startswith('(allow network-outbound (remote ip "*:')]
                self.assertEqual(allowed, ports)

    def test_a_spark_url_without_a_usable_port_is_refused(self):
        for url in ("http://spark.example.invalid:0/v1", "http://spark.example.invalid:70000/v1",
                    "http://spark.example.invalid:/v1", "spark.example.invalid:8000"):
            with self.subTest(url=url):
                result = self.bash('sandbox_profile "$1" bash api', lane="spark", SPARK_URL=url)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertIn("refusing sandbox: no usable port in the spark URL", result.stderr)

    def test_the_validation_run_keeps_its_rules(self):
        profile = self.profile("check")
        self.assertNotIn(WRITE_DENY, profile)
        self.assertNotIn(NET_DENY, profile)
        self.assertIn('(deny network-outbound (remote ip "localhost:*"))', profile)

    def test_the_agy_lane_is_bounded_by_the_outer_profile(self):
        profile = self.profile("", lane="agy")
        self.assertIn(NET_DENY, profile)
        allows = [line for line in profile.splitlines()
                  if line.startswith("(allow network-outbound (remote ip")]
        self.assertEqual(allows, ['(allow network-outbound (remote ip "*:443"))'])
        self.assertIn(NET_DENY, profile)
        # The allow for 443 comes after the blanket deny.
        self.assertGreater(profile.index('(allow network-outbound (remote ip "*:443"))'),
                           profile.index(NET_DENY))
        self.assertIn('(literal "/usr/bin/security")', profile)
        # An arbitrary port on an arbitrary host is not allowed.
        self.assertNotIn('(allow network-outbound (remote ip "*:80"))', profile)

    def test_the_agy_agent_run_reaches_its_own_sign_in(self):
        # 2026-10-22 to 10-24: the keychain deny (#1465) hid agy's stored
        # sign-in from agy itself, and every agy run asked for a browser login
        # and failed auth (#1620). Allowing the one keychain file was not
        # enough: the agent run may reach the keychain server and read the lane
        # user's keychain folder; /usr/bin/security stays unrunnable.
        profile = self.profile("", lane="agy")
        self.assertNotIn('(global-name "com.apple.SecurityServer")', profile)
        self.assertNotIn("/Library/Keychains", profile)
        self.assertIn('(literal "/usr/bin/security")', profile)

    def test_check_runs_and_other_lanes_keep_the_whole_keychain_deny(self):
        keychain = f"{self.scratch}/Library/Keychains/loom.keychain-db"
        for mode, lane in (("check", "agy"), ("", "codex"), ("api", "opencode-go")):
            with self.subTest(mode=mode, lane=lane):
                profile = self.profile(mode, lane=lane, AGY_KEYCHAIN=keychain)
                self.assertIn('(global-name "com.apple.SecurityServer")', profile)
                self.assertNotIn(f'(allow file-read* (literal "{keychain}"))', profile)
        self.assertIn(f'(deny file-read* (subpath "{self.scratch}/Library/Keychains"))',
                      self.profile("check", lane="agy"))

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
    def test_inside_the_agy_profile_connections_and_security_stay_confined(self):
        result = self.bash(
            'p="$(sandbox_profile "$1" bash "")" || exit 9\n'
            'sandbox-exec -p "$p" /bin/bash -c "$2"',
            AGY_PROBE, lane="agy")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(),
                         ["net=denied", "security=denied"])

    def test_an_unknown_mode_is_refused(self):
        result = self.bash('sandbox_profile "$1" bash agent')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("refusing sandbox: unknown mode agent", result.stderr)

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
    @unittest.skipUnless(sandbox_caps.LOCALHOST, sandbox_caps.NO_LOCALHOST)
    def test_inside_the_api_profile_writes_and_connections_stay_confined(self):
        outside = self.root / "outside"
        with socket.socket() as service:
            service.bind(("127.0.0.1", 0))
            service.listen(1)
            port = str(service.getsockname()[1])
            result = self.bash(
                'p="$(sandbox_profile "$1" bash api)" || exit 9\n'
                'TMPDIR="$AGENT_HOME/tmp" sandbox-exec -p "$p" /bin/bash -c "$2" probe "$1" "$3" "$4"',
                CONFINED_PROBE, str(outside), port)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(),
                         ["tree=written", "scratch=written", "outside=denied", "devnull=written", "lo=denied"])
        self.assertFalse(outside.exists())


if __name__ == "__main__":
    unittest.main()
