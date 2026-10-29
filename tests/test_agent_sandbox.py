"""The API-lane agent's Seatbelt profile (lanes/agent_sandbox.sh): outbound
connections only to its provider's port, writes only to its tree and scratch
home. The agy lane is bounded the same way (its model endpoint's port is open,
keychain and /usr/bin/security denied), and the validation run keeps its own
rules."""

import os
import re
import shutil
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

# Run inside the agy profile as a command agy launched (not the profiled
# CLI's own binary): try to read each oauth token file (TOKEN_A in the file
# store, TOKEN_B in the CLI state directory) directly, through a hard link,
# through a symlink, after moving the file, and after moving its folder,
# and a file in the keychain folder. In t, $1 is the printed label (a or
# b), $2 where the links and moves land, $3 the token file.
TOKEN_PROBE = r"""
r() { if /usr/bin/head -c 1 "$2" > /dev/null 2>&1; then echo "$1=read"; else echo "$1=denied"; fi; }
t() {
  r "$1-direct" "$3"
  ln "$3" "$2-hard" 2> /dev/null; r "$1-hardlink" "$2-hard"
  ln -s "$3" "$2-soft" 2> /dev/null; r "$1-symlink" "$2-soft"
  mv "$3" "$2-moved" 2> /dev/null; r "$1-moved" "$2-moved"
  mv "$(dirname "$3")" "$2-dir" 2> /dev/null; r "$1-folder" "$2-dir/$(basename "$3")"
}
t a "$TREE/a" "$TOKEN_A"
t b "$TREE/b" "$TOKEN_B"
r keychain "$KEYCHAIN_FILE"
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

    def fake_agy(self, contents=None):
        # A stand-in binary at the path the lane itself launches for agy
        # (AGY_BIN in lanes/run_lane.sh: .local/bin/agy under the lane
        # user's home, here the scratch home the tests use for it). With
        # no contents it is a copy of cat, a dedicated binary; pass
        # contents to place a script there instead.
        path = self.scratch / ".local" / "bin" / "agy"
        path.parent.mkdir(parents=True, exist_ok=True)
        if contents is None:
            shutil.copy(shutil.which("cat"), path)
        else:
            path.write_text(contents)
        path.chmod(0o755)
        return path

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
        self.assertIn('(deny mach-lookup (global-name "com.apple.SecurityServer")', profile)
        self.assertIn('(literal "/usr/bin/security")', profile)
        self.assertIn(f'(deny file-read* (subpath "{self.scratch}/Library/Keychains"))', profile)
        # An arbitrary port on an arbitrary host is not allowed.
        self.assertNotIn('(allow network-outbound (remote ip "*:80"))', profile)

    def test_every_run_keeps_the_whole_keychain_deny(self):
        # agy reaches the keychain only through /usr/bin/security, so loosening
        # the keychain server or files for it changed nothing (#1620, #1636):
        # the agy agent run, an agy check run and other lanes all keep the
        # keychain-server deny, and agy runs keep the Keychains folder deny.
        folder = f'(deny file-read* (subpath "{self.scratch}/Library/Keychains"))'
        for mode, lane in (("", "agy"), ("check", "agy"), ("", "codex"), ("api", "opencode-go")):
            with self.subTest(mode=mode, lane=lane):
                profile = self.profile(mode, lane=lane)
                self.assertIn('(global-name "com.apple.SecurityServer")', profile)
                self.assertIn('(literal "/usr/bin/security")', profile)
                if lane == "agy":
                    self.assertIn(folder, profile)

    def test_the_agy_profile_denies_its_oauth_token_to_every_other_process(self):
        # #1623: agy keeps its sign-in in two files named antigravity-oauth-token,
        # its file store (Library/Application Support/antigravity, #1620) and its
        # CLI state directory (.gemini/antigravity-cli). A command agy launches
        # runs in the same sandbox and could read either and send it out, so the
        # profile denies each file, and linking or moving it or its folder, to
        # every process but agy's own binary (here bash, the profiled CLI).
        cli = str(Path(shutil.which("bash")).resolve())
        profile = self.profile("", lane="agy")
        for folder in (f"{self.scratch}/Library/Application Support/antigravity",
                       f"{self.scratch}/.gemini/antigravity-cli"):
            token = f"{folder}/antigravity-oauth-token"
            self.assertIn(
                f'(deny file-read* file-write-unlink file-link '
                f'(require-all (literal "{token}") (require-not (process-path "{cli}"))))',
                profile)
            self.assertIn(
                f'(deny file-write-unlink file-link '
                f'(require-all (literal "{folder}") (require-not (process-path "{cli}"))))',
                profile)

    def test_the_agy_check_profile_denies_its_oauth_token_to_every_process(self):
        # A validation run never runs agy, so its profile names no process
        # that may read the token: agent-written test code is denied too.
        profile = self.profile("check", lane="agy")
        for folder in (f"{self.scratch}/Library/Application Support/antigravity",
                       f"{self.scratch}/.gemini/antigravity-cli"):
            self.assertIn(
                f'(deny file-read* file-write-unlink file-link '
                f'(literal "{folder}/antigravity-oauth-token"))', profile)
        self.assertNotIn("process-path", profile)

    def test_only_the_agy_lane_profile_names_the_oauth_token(self):
        for mode, lane in (("", "codex"), ("api", "opencode-go"), ("check", "codex")):
            with self.subTest(mode=mode, lane=lane):
                self.assertNotIn("antigravity-oauth-token", self.profile(mode, lane=lane))

    def test_the_agy_token_exception_names_the_binary_at_agys_own_path(self):
        # The runner launches exactly one binary for this lane (AGY_BIN in
        # lanes/run_lane.sh) and builds the profile for that launch, so the
        # token exception names that binary's resolved path: the reader
        # the rule trusts is the launched binary itself, recorded here at
        # its real install path rather than a stand-in found on PATH, and
        # no other process is named anywhere in the profile.
        agy = self.fake_agy()
        result = self.bash('sandbox_profile "$1" "$2" ""', str(agy), lane="agy")
        self.assertEqual(result.returncode, 0, result.stderr)
        profile = result.stdout
        expected = str(agy.resolve())
        named = re.findall(r'\(process-path "([^"]+)"\)', profile)
        self.assertEqual(sorted(set(named)), [expected])
        self.assertEqual(len(named), 4)
        for folder in (f"{self.scratch}/Library/Application Support/antigravity",
                       f"{self.scratch}/.gemini/antigravity-cli"):
            self.assertIn(
                f'(deny file-read* file-write-unlink file-link '
                f'(require-all (literal "{folder}/antigravity-oauth-token") '
                f'(require-not (process-path "{expected}"))))',
                profile)

    def test_a_script_at_agys_path_names_no_interpreter_in_the_token_exception(self):
        # The danger in a script at the launched path is not the script:
        # its own contents are fixed, and an agent command invoking the
        # interpreter directly is a different image, still denied. The
        # danger is the repair such a script invites, widening the token
        # exception to the interpreter's path so the refresh works again:
        # every launched command under that runtime could then read the
        # token (#1623). So the built profile names the launched path
        # alone, never its interpreter; a reader that is not the launched
        # binary itself needs a distinguishing rule, not a broader
        # exception.
        agy = self.fake_agy("#!/usr/bin/env bash\nexit 0\n")
        result = self.bash('sandbox_profile "$1" "$2" ""', str(agy), lane="agy")
        self.assertEqual(result.returncode, 0, result.stderr)
        named = re.findall(r'\(process-path "([^"]+)"\)', result.stdout)
        self.assertEqual(sorted(set(named)), [str(agy.resolve())])
        self.assertNotIn(str(Path(shutil.which("bash")).resolve()), named)

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
    def test_inside_the_agy_profile_a_command_cannot_read_the_oauth_token(self):
        # #1623's probe: the profiled CLI is /bin/cat, standing in for agy's
        # binary; it alone may read the token. /bin/bash and the tools it
        # runs stand in for a command agy launched: they cannot read either
        # token file however they reach for it, nor the keychain folder.
        store = self.scratch / "Library" / "Application Support" / "antigravity" / "antigravity-oauth-token"
        state = self.scratch / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
        keychain = self.scratch / "Library" / "Keychains" / "fixture.keychain-db"
        for f in (store, state, keychain):
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("not-a-real-secret\n")
        script = ('export TOKEN_A="$2" TOKEN_B="$3" KEYCHAIN_FILE="$4" TREE="$1"\n'
                  'p="$(sandbox_profile "$1" cat "")" || exit 9\n'
                  'sandbox-exec -p "$p" /bin/bash -c "$5"\n'
                  'sandbox-exec -p "$p" /bin/cat "$2" > /dev/null && echo agy=read || echo agy=denied')
        result = self.bash(script, str(store), str(state), str(keychain), TOKEN_PROBE, lane="agy")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(),
                         ["a-direct=denied", "a-hardlink=denied", "a-symlink=denied",
                          "a-moved=denied", "a-folder=denied",
                          "b-direct=denied", "b-hardlink=denied", "b-symlink=denied",
                          "b-moved=denied", "b-folder=denied",
                          "keychain=denied", "agy=read"])
        self.assertEqual(store.read_text(), "not-a-real-secret\n")
        self.assertEqual(state.read_text(), "not-a-real-secret\n")

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
    def test_inside_the_agy_profile_the_binary_at_agys_own_path_reads_the_token(self):
        # The /bin/cat probe above, with the exception naming a binary
        # installed where the lane's own agy sits (.local/bin/agy under
        # the lane home): that binary reads a disposable token, which is
        # the refresh path, while a command agy launched is denied
        # however it reaches for either file. Commands agy runs are its
        # children, launched through the helper it writes into its state
        # directory (bin/agentapi): the probe's bash and its tools stand
        # in for their images, and the helper line runs a reader placed
        # at that helper's own path. Every image but the launched
        # binary's is denied alike. What this suite cannot pair is the
        # real process tree (there is no agy here to launch commands) or
        # a reader that is itself a shared interpreter: the exception
        # naming no interpreter is pinned by the test above, and which
        # image actually reads the token is recorded by the ticket's
        # live check.
        agy = self.fake_agy()
        store = self.scratch / "Library" / "Application Support" / "antigravity" / "antigravity-oauth-token"
        state = self.scratch / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
        keychain = self.scratch / "Library" / "Keychains" / "fixture.keychain-db"
        for f in (store, state, keychain):
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("not-a-real-secret\n")
        helper = state.parent / "bin" / "agentapi"
        helper.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(shutil.which("cat"), helper)
        helper.chmod(0o755)
        script = ('export TOKEN_A="$2" TOKEN_B="$3" KEYCHAIN_FILE="$4" TREE="$1"\n'
                  'p="$(sandbox_profile "$1" "$6" "")" || exit 9\n'
                  'sandbox-exec -p "$p" /bin/bash -c "$5"\n'
                  'sandbox-exec -p "$p" "$7" "$2" > /dev/null && echo helper=read || echo helper=denied\n'
                  'sandbox-exec -p "$p" "$6" "$2" > /dev/null && echo agy=read || echo agy=denied')
        result = self.bash(script, str(store), str(state), str(keychain), TOKEN_PROBE, str(agy), str(helper), lane="agy")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(),
                         ["a-direct=denied", "a-hardlink=denied", "a-symlink=denied",
                          "a-moved=denied", "a-folder=denied",
                          "b-direct=denied", "b-hardlink=denied", "b-symlink=denied",
                          "b-moved=denied", "b-folder=denied",
                          "keychain=denied", "helper=denied", "agy=read"])
        self.assertEqual(store.read_text(), "not-a-real-secret\n")
        self.assertEqual(state.read_text(), "not-a-real-secret\n")

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


class BwrapSandboxTest(unittest.TestCase):
    """LOOM_SANDBOX=bwrap, the lane pod's sandbox (docs/LANES.md, "Lane pods"):
    the profile is bubblewrap's arguments, one a line. The tree and scratch
    home sit under HOME, as in a pod."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.home = self.root / "home"
        self.tree = self.home / "loom" / "worktrees" / "T1"
        self.scratch = self.home / "loom" / "run" / "opencode-go-home"
        self.bin = self.root / "bin"
        for d in (self.tree / ".git", self.scratch / "tmp", self.bin):
            d.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def bash(self, script, *args, lane="opencode-go", **env):
        full = dict(os.environ, HOME=str(self.home), AGENT_HOME=str(self.scratch), LANE=lane,
                    SKILLS=str(self.root / "no-skills"), LOOM_SANDBOX="bwrap", NET_TIMEOUT="10")
        full.update(env)
        return subprocess.run(["bash", "-c", '. "$1"; shift; ' + script, "bash", str(SANDBOX),
                               str(self.tree), *args],
                              capture_output=True, text=True, env=full, timeout=60)

    def profile(self, mode, cli="bash", **env):
        result = self.bash('sandbox_profile "$1" "$2" "$3"', cli, mode, **env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.splitlines()

    @staticmethod
    def pairs(lines, flag):
        """The operands that follow each occurrence of flag (two for a bind)."""
        out = []
        for i, line in enumerate(lines):
            if line == flag:
                out.append(tuple(lines[i + 1:i + 3]) if "bind" in flag else lines[i + 1])
        return out

    def assert_confined_root(self, lines):
        """The root is an empty tmpfs: no bind of / at all, only the system
        folders read-only, a private /tmp, and the secret folders masked."""
        self.assertNotIn(("/", "/"), self.pairs(lines, "--bind"))
        self.assertNotIn(("/", "/"), self.pairs(lines, "--ro-bind"))
        self.assertEqual(self.pairs(lines, "--bind-try"), [])
        system = self.pairs(lines, "--ro-bind-try")
        self.assertIn(("/usr", "/usr"), system)
        self.assertIn(("/etc", "/etc"), system)
        self.assertTrue(all(src == dst and src.count("/") == 1 for src, dst in system), system)
        for hidden in ("/home", "/var", "/run", "/src", str(self.home)):
            self.assertNotIn((hidden, hidden), system)
        tmpfs = self.pairs(lines, "--tmpfs")
        # The sandbox's own mount points, not files this test creates.
        for masked in ("/tmp", "/run/secrets", "/var/run/secrets"):  # noqa: S108
            self.assertIn(masked, tmpfs)
        # Writable: the tree and the scratch home only.
        self.assertEqual(self.pairs(lines, "--bind"),
                         [(str(self.tree), str(self.tree)), (str(self.scratch), str(self.scratch))])
        git = str(self.tree / ".git")
        self.assertIn((git, git), self.pairs(lines, "--ro-bind"))
        self.assertLess(lines.index(str(self.tree)), lines.index(git))

    def test_an_api_run_sees_only_its_tree_and_scratch_home_and_the_system_folders(self):
        lines = self.profile("api")
        self.assert_confined_root(lines)
        for flag in ("--unshare-pid", "--die-with-parent", "--new-session"):
            self.assertIn(flag, lines)
        self.assertEqual(self.pairs(lines, "--proc"), ["/proc"])
        self.assertEqual(self.pairs(lines, "--chdir"), [str(self.tree)])
        # The root tmpfs takes no writes, after everything is bound in.
        self.assertEqual(self.pairs(lines, "--remount-ro"), ["/"])
        self.assertGreater(lines.index("--remount-ro"), max(i for i, w in enumerate(lines) if w == "--bind"))

    def test_a_validation_run_writes_only_its_tree_check_home_and_tmp(self):
        uv = self.home / ".local" / "bin" / "uv"
        uv.parent.mkdir(parents=True)
        uv.write_text("#!/bin/sh\n")
        uv.chmod(0o755)
        lines = self.profile("check", CHECK_UV_BINS=str(uv))
        self.assert_confined_root(lines)
        self.assertIn((str(uv), str(uv)), self.pairs(lines, "--ro-bind"))

    def test_a_cli_under_home_is_bound_back_read_only(self):
        cli = self.home / ".opencode" / "bin" / "opencode"
        cli.parent.mkdir(parents=True)
        cli.write_text("#!/bin/sh\n")
        cli.chmod(0o755)
        lines = self.profile("api", cli="opencode", PATH=f"{cli.parent}:{os.environ['PATH']}")
        self.assertIn((str(cli.parent), str(cli.parent)), self.pairs(lines, "--ro-bind"))
        self.assertNotIn((str(cli.parent), str(cli.parent)), self.pairs(lines, "--bind"))

    def test_the_seatbelt_sandbox_needs_no_egress_probes(self):
        sandbox_exec = self.bin / "sandbox-exec"
        sandbox_exec.write_text("#!/bin/sh\nexit 0\n")
        sandbox_exec.chmod(0o755)
        result = self.bash('sandbox_ready && echo ready || echo "$SANDBOX_WHY"',
                           PATH=f"{self.bin}:/usr/bin:/bin", LOOM_SANDBOX="seatbelt")
        self.assertEqual(result.stdout.strip(), "ready")

    def test_a_bind_at_or_above_home_is_refused(self):
        # The tree or scratch home at HOME, or above it, would expose all of
        # HOME (the loom home, the copied SSM values) to the agent.
        for name, env, tree in (("tree is HOME", {}, self.home),
                                ("tree above HOME", {}, self.root),
                                ("scratch is HOME", {"AGENT_HOME": str(self.home)}, self.tree)):
            with self.subTest(name):
                full = dict(os.environ, HOME=str(self.home), AGENT_HOME=str(self.scratch), LANE="opencode-go",
                            SKILLS=str(self.root / "no-skills"), LOOM_SANDBOX="bwrap")
                full.update(env)
                result = subprocess.run(["bash", "-c", '. "$1"; sandbox_profile "$2" bash api', "bash",
                                         str(SANDBOX), str(tree)],
                                        capture_output=True, text=True, env=full, timeout=30)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertEqual(result.stdout, "")
                self.assertIn("sits at or above HOME", result.stderr)

    def test_bwrap_refuses_the_agy_lanes_profile(self):
        result = self.bash('sandbox_profile "$1" bash ""', lane="agy", AGY_HOME=str(self.scratch))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("bwrap serves api and check runs only", result.stderr)

    def test_an_unknown_sandbox_is_refused(self):
        result = self.bash('sandbox_profile "$1" bash api', LOOM_SANDBOX="jail")
        self.assertEqual(result.returncode, 1)
        self.assertIn("unknown LOOM_SANDBOX 'jail'", result.stderr)
        result = self.bash('sandbox_ready || { echo "$SANDBOX_WHY"; exit 1; }', LOOM_SANDBOX="jail")
        self.assertEqual(result.returncode, 1)
        self.assertIn("unknown LOOM_SANDBOX 'jail'", result.stdout)

    def test_the_sandbox_command_is_bwrap_with_the_profile_lines_then_a_separator(self):
        result = self.bash('p="$(sandbox_profile "$1" bash api)" || exit 9\n'
                           'sandbox_command "$p" || exit 8\nprintf "%s\\n" "${SANDBOX_CMD[@]}"')
        self.assertEqual(result.returncode, 0, result.stderr)
        words = result.stdout.splitlines()
        self.assertEqual(words[0], "bwrap")
        self.assertEqual(words[-1], "--")
        self.assertEqual(words[1:-1], self.profile("api"))
        seatbelt = self.bash('sandbox_command "(version 1)" && printf "%s|" "${SANDBOX_CMD[@]}"',
                             LOOM_SANDBOX="seatbelt")
        self.assertEqual(seatbelt.stdout, "sandbox-exec|-p|(version 1)|")

    def setUpListener(self):
        """A local port that accepts connections: the positive control."""
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        self.addCleanup(listener.close)
        return "127.0.0.1:%d" % listener.getsockname()[1]

    @staticmethod
    def closed_port():
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def ready(self, bwrap_exit=0, path=None, **env):
        bwrap = self.bin / "bwrap"
        bwrap.write_text("#!/bin/sh\nexit %d\n" % bwrap_exit)
        bwrap.chmod(0o755)
        env.setdefault("KUBERNETES_SERVICE_HOST", "")
        return self.bash('sandbox_ready && echo ready || echo "$SANDBOX_WHY"',
                         PATH=path or f"{self.bin}:/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin",
                         **env).stdout.strip()

    @unittest.skipUnless(sandbox_caps.LOCALHOST, sandbox_caps.NO_LOCALHOST)
    def test_sandbox_ready_says_why_bwrap_cannot_run(self):
        good = {"LOOM_EGRESS_DENY_PROBES": "127.0.0.1:%d" % self.closed_port(),
                "LOOM_EGRESS_ALLOW_PROBE": self.setUpListener()}
        # A PATH with bash and nothing else: a Linux host has a real bwrap in
        # /usr/bin, so the system folders cannot stand in for "no bwrap".
        no_bwrap = self.root / "no-bwrap"
        no_bwrap.mkdir()
        (no_bwrap / "bash").symlink_to(shutil.which("bash", path="/usr/bin:/bin"))
        result = self.bash('sandbox_ready && echo ready || echo "$SANDBOX_WHY"',
                           PATH=str(no_bwrap), **good)
        self.assertEqual(result.stdout.strip(), "bwrap not available")
        self.assertIn("bwrap cannot start a sandbox here", self.ready(bwrap_exit=1, **good))
        self.assertEqual(self.ready(**good), "ready")

    def test_bwrap_fails_closed_without_a_declared_egress_policy(self):
        self.assertIn("set LOOM_EGRESS_DENY_PROBES", self.ready())
        self.assertIn("set LOOM_EGRESS_DENY_PROBES", self.ready(LOOM_EGRESS_DENY_PROBES=""))
        self.assertIn("set LOOM_EGRESS_DENY_PROBES", self.ready(LOOM_EGRESS_DENY_PROBES=" , ,"))
        self.assertIn("set LOOM_EGRESS_ALLOW_PROBE", self.ready(LOOM_EGRESS_DENY_PROBES="192.0.2.1:80"))

    def test_a_blocked_probe_must_be_a_literal_address(self):
        # A name that does not resolve fails to connect too, and would pass
        # as blocked on a pod with broken DNS.
        for target in ("metadata.invalid:80", "metadata", "192.0.2.1", "192.0.2.1:http"):
            with self.subTest(target=target):
                why = self.ready(LOOM_EGRESS_DENY_PROBES=target, LOOM_EGRESS_ALLOW_PROBE="127.0.0.1:1")
                self.assertIn("is not IPv4:port", why)

    def test_blocked_probes_may_be_comma_separated(self):
        # The cluster's secret holds them comma-separated; on 2026-10-26 the
        # first runner lanes refused the whole list as one bad address.
        why = self.ready(LOOM_EGRESS_DENY_PROBES="metadata:80,192.0.2.1:80",
                         LOOM_EGRESS_ALLOW_PROBE="127.0.0.1:1")
        self.assertIn("egress probe 'metadata:80' is not IPv4:port", why)

    @unittest.skipUnless(sandbox_caps.LOCALHOST, sandbox_caps.NO_LOCALHOST)
    def test_every_comma_separated_probe_is_tried(self):
        closed = "127.0.0.1:%d" % self.closed_port()
        allow = self.setUpListener()
        open_ = self.setUpListener()
        why = self.ready(LOOM_EGRESS_DENY_PROBES=f"{closed},{open_}", LOOM_EGRESS_ALLOW_PROBE=allow)
        self.assertEqual(why, f"egress not narrowed: {open_} accepts connections, so no agent runs here")

    @unittest.skipUnless(sandbox_caps.LOCALHOST, sandbox_caps.NO_LOCALHOST)
    def test_bwrap_fails_closed_when_a_blocked_destination_answers(self):
        closed = "127.0.0.1:%d" % self.closed_port()
        allow = self.setUpListener()
        open_ = self.setUpListener()
        why = self.ready(LOOM_EGRESS_DENY_PROBES=f"{closed} {open_}", LOOM_EGRESS_ALLOW_PROBE=allow)
        self.assertEqual(why, f"egress not narrowed: {open_} accepts connections, so no agent runs here")
        self.assertEqual(self.ready(LOOM_EGRESS_DENY_PROBES=closed, LOOM_EGRESS_ALLOW_PROBE=allow), "ready")

    @unittest.skipUnless(sandbox_caps.LOCALHOST, sandbox_caps.NO_LOCALHOST)
    def test_the_api_server_the_pod_is_given_is_always_probed(self):
        closed = "127.0.0.1:%d" % self.closed_port()
        allow = self.setUpListener()
        api = self.setUpListener()
        host, port = api.split(":")
        why = self.ready(LOOM_EGRESS_DENY_PROBES=closed, LOOM_EGRESS_ALLOW_PROBE=allow,
                         KUBERNETES_SERVICE_HOST=host, KUBERNETES_SERVICE_PORT=port)
        self.assertEqual(why, f"egress not narrowed: {api} accepts connections, so no agent runs here")

    @unittest.skipUnless(sandbox_caps.LOCALHOST, sandbox_caps.NO_LOCALHOST)
    def test_a_pod_with_no_network_does_not_pass_because_everything_fails(self):
        closed = "127.0.0.1:%d" % self.closed_port()
        why = self.ready(LOOM_EGRESS_DENY_PROBES=closed, LOOM_EGRESS_ALLOW_PROBE=closed)
        self.assertEqual(why, f"egress unproven: {closed} did not connect, so the blocked probes prove nothing")
        why = self.ready(LOOM_EGRESS_DENY_PROBES=closed, LOOM_EGRESS_ALLOW_PROBE="name.invalid:443")
        self.assertIn("egress unproven: name.invalid:443 did not connect", why)


class SsmDirTest(unittest.TestCase):
    """LOOM_SSM_DIR: a lane pod reads copied SSM values from files and holds
    no AWS key."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.dir = self.root / "home" / ".ssm"
        (self.dir / "k").mkdir(parents=True)
        (self.dir / "k" / "oc").write_text("fake-oc-key")
        (self.root / "outside").write_text("not a parameter")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "aws.log"
        aws = self.bin / "aws"
        aws.write_text(f"#!/bin/sh\necho called >> {self.log}\necho from-aws\n")
        aws.chmod(0o755)

    def tearDown(self):
        self._tmp.cleanup()

    def value(self, name, **env):
        full = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", NET_TIMEOUT="10", **env)
        return subprocess.run(["bash", "-c", '. "$1"; ssm_value "$2"', "bash", str(SANDBOX), name],
                              capture_output=True, text=True, env=full, timeout=30)

    def test_the_value_is_the_file_at_the_parameters_name(self):
        result = self.value("/k/oc", LOOM_SSM_DIR=str(self.dir))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "fake-oc-key")
        self.assertFalse(self.log.exists())

    def test_a_name_that_leaves_the_folder_or_is_missing_reads_nothing(self):
        for name in ("/../../outside", "k/oc", "/k/./oc", "/k/missing"):
            with self.subTest(name=name):
                result = self.value(name, LOOM_SSM_DIR=str(self.dir))
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
        self.assertFalse(self.log.exists())

    def test_without_the_folder_ssm_is_asked(self):
        result = self.value("/k/oc")
        self.assertEqual(result.stdout.strip(), "from-aws")
        self.assertTrue(self.log.exists())


if __name__ == "__main__":
    unittest.main()
