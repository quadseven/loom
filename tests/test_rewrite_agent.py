"""lanes/rewrite_agent.sh run as a process: a temp loom home, a fake opencode
and a fake aws on PATH, and the real sandbox-exec, as the station runs it."""

import json
import re
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from tests import sandbox_caps

LANES = Path(__file__).resolve().parent.parent / "lanes"
WRAPPER = LANES / "rewrite_agent.sh"
KEY = "sk-test-rewrite-key-0123456789"
MODEL = "opencode-go/deepseek-v4.1-flash"
ANSWER = '{"action": "skip", "reason": "fake"}'

# Fake opencode. The wrapper empties the agent's environment, so the fake
# records to a path written into it (@LOG@, outside the test HOME, which the
# sandbox denies). It records its argv, its stdin, its environment, and
# whether the key shows in any process's argv. The prompt picks what it does:
# MODE=fail exits 3, MODE=error reports a provider error (naming the key)
# and exits 0, MODE=notext prints no text, MODE=leak answers with the key,
# MODE=plant leaves a json.py in its working directory. Otherwise it prints
# log noise on both streams and two text parts, the answer last.
FAKE_OPENCODE = r"""#!/usr/bin/env bash
[ "${1:-}" = --version ] && { echo "@VERSION@"; exit 0; }
log=@LOG@
if [ "${1:-}" = serve ]; then
  exec python3 -c 'import socket, sys, time
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind((sys.argv[1], int(sys.argv[2]))); s.listen(5); time.sleep(120)' "$3" "$5"
fi
prompt="$(cat)"
key="${OPENCODE_API_KEY:-}"
case "$(ps -axo args= 2>/dev/null)" in *"${key:-no-key-set}"*) argv=key-visible ;; *) argv=clean ;; esac
{
  printf 'argv=%s\n' "$*"
  printf 'key=%s\n' "${key:-none}"
  printf 'psargv=%s\n' "$argv"
  printf 'home=%s\n' "$HOME"
  printf 'pwd=%s\n' "$PWD"
  printf 'envnames=%s\n' "$(compgen -e | tr '\n' ' ')"
  printf 'config=%s\n' "${OPENCODE_CONFIG_CONTENT:-none}"
} >> "$log"
printf '%s' "$prompt" > "$log.stdin"
case "$prompt" in
  *MODE=rate-first*) case "$*" in *"@RATE_MODEL@"*) printf '%s\n' '{"type":"error","error":{"name":"RateLimitError","data":{"message":"temporarily rate limited"}}}'; exit 0 ;; esac ;;
  *MODE=fail*) echo "connection reset" >&2; exit 3 ;;
  *MODE=hang*) case "$*" in *free/*) sleep 30 ;; esac ;;
  *MODE=error*) printf '{"type":"error","error":{"name":"APIError","data":{"message":"bad key %s"}}}\n' "$key"; exit 0 ;;
  *MODE=notext*) printf '%s\n' '{"type":"step_start","part":{"type":"step-start"}}'; exit 0 ;;
  *MODE=leak*) printf '{"type":"text","part":{"type":"text","text":"the key is %s"}}\n' "$key"; exit 0 ;;
  *MODE=plant*) printf 'import pathlib\npathlib.Path("%s.planted").write_text("ran")\n' "$log" > json.py ;;
esac
echo "INFO service=bus starting" >&2
printf '%s\n' '{"type":"step_start","part":{"type":"step-start"}}'
printf '%s\n' '{"type":"text","part":{"type":"text","text":"Let me read the issue."}}'
echo "not an event"
printf '%s\n' '{"type":"text","part":{"type":"text","text":"{\"action\": \"skip\", \"reason\": \"fake\"}"}}'
printf '%s\n' '{"type":"step_finish","part":{"type":"step-finish"}}'
"""

# Fake aws: `ssm get-parameter --name N` prints $FAKE_SECRET (for N =
# $FAKE_SECRET_NAME) or fails like a missing parameter. Records its argv.
FAKE_AWS = """#!/usr/bin/env bash
printf 'aws %s\\n' "$*" >> "$FAKE_LOG"
while [ $# -gt 0 ]; do [ "$1" = --name ] && name="$2"; shift; done
[ "$name" = "${FAKE_SECRET_NAME:-}" ] && [ -n "${FAKE_SECRET:-}" ] || { echo "ParameterNotFound" >&2; exit 254; }
printf '%s\\n' "$FAKE_SECRET"
"""

FAKE_SANDBOX = """#!/usr/bin/env bash
[ "$1" = -p ] || exit 2
printf '%s\n' "$2" >> @SANDBOX_LOG@
shift 2
exec "$@"
"""

FAKE_LANE = """#!/usr/bin/env bash
log=@LANE_LOG@
printf '%s|%s|%s|%s\n' "$(basename "$0")" "$*" "$HOME" "$PWD" >> "$log"
cat > "$log.prompt"
if [ "$(basename "$0")" = codex ] && grep -q 'MODE=fail-codex' "$log.prompt"; then exit 3; fi
[ "$(basename "$0")" = codex ] && printf 'cert=%s\n' "${SSL_CERT_FILE:-unset}" > "$log.codexenv"
if [ "$(basename "$0")" = claude ]; then
  case "${CLAUDE_CODE_OAUTH_TOKEN:-}" in *$'\nINJECTED=present') intact=yes ;; *) intact=no ;; esac
  printf 'token-intact=%s injected=%s\n' "$intact" "${INJECTED:-absent}" > "$log.env"
fi
case "$(basename "$0")" in
  codex) printf '%s\n' '{"type":"thread.started"}' '{"type":"item.completed","item":{"type":"agent_message","text":"{\\"action\\":\\"skip\\",\\"reason\\":\\"fake\\"}"}}' ;;
  claude) printf '%s\n' '{"type":"result","result":"{\\"action\\":\\"skip\\",\\"reason\\":\\"fake\\"}"}' ;;
esac
"""


class RewriteAgentTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name).resolve()
        self.home = root / "home"
        self.loom = self.home / "loom"
        self.home.mkdir()
        self.bin = root / "bin"
        self.bin.mkdir()
        self.log = root / "calls.log"
        self.log.touch()
        self.agent_log = root / "agent.log"
        self.lane_log = root / "lane.log"
        self.sandbox_log = root / "sandbox.log"
        self.write_fake_opencode("1.18.4")
        self.write_tool(self.bin / "aws", FAKE_AWS)
        self.write_tool(self.bin / "sandbox-exec", FAKE_SANDBOX.replace("@SANDBOX_LOG@", str(self.sandbox_log)))
        for lane in ("codex", "claude"):
            self.write_tool(self.bin / lane, FAKE_LANE.replace("@LANE_LOG@", str(self.lane_log)))

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def write_tool(path, text):
        path.write_text(text)
        path.chmod(0o755)

    def write_fake_opencode(self, version):
        self.write_tool(self.bin / "opencode", FAKE_OPENCODE.replace("@LOG@", str(self.agent_log))
                        .replace("@VERSION@", version))

    def run_wrapper(self, prompt="Rewrite issue 7 into a ticket.", args=("loom/test/key", MODEL),
                    secret=KEY, env_overrides=None):
        env = {
            "HOME": str(self.home),
            "PATH": f"{self.bin}:/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin",
            "FAKE_LOG": str(self.log),
            "FAKE_SECRET_NAME": "loom/test/key",
            "FAKE_SECRET": secret,
            "LANE_LOG": str(self.lane_log),
            "SANDBOX_LOG": str(self.sandbox_log),
        }
        if env_overrides:
            env.update(env_overrides)
        return subprocess.run(["bash", str(WRAPPER), *args], input=prompt, env=env,
                              capture_output=True, text=True, timeout=60)

    def agent(self):
        lines = self.agent_log.read_text().splitlines() if self.agent_log.exists() else []
        return dict(line.split("=", 1) for line in lines if "=" in line)

    def test_lane_clis_use_the_sandbox_and_print_only_final_text(self):
        for lane in ("codex", "claude"):
            with self.subTest(lane=lane):
                self.lane_log.write_text("")
                result = self.run_wrapper("Rewrite this", args=("-", lane + "/test-model"))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, '{"action":"skip","reason":"fake"}\n')
                self.assertIn(lane, self.lane_log.read_text())
                argv = self.lane_log.read_text().split("|", 2)[1]
                if lane == "codex":
                    self.assertIn("--sandbox read-only", argv)
                    self.assertIn("--json", argv)
                else:
                    self.assertIn("--tools  --strict-mcp-config", argv)
                self.assertEqual(Path(str(self.lane_log) + ".prompt").read_text(), "Rewrite this")
                self.assertIn('(deny file-read* file-write*', self.sandbox_log.read_text())
                self.assertIn('run/rewrite.', self.lane_log.read_text())
                self.assertIn('home/work', self.lane_log.read_text())
                self.assertNotIn('aws ssm', self.log.read_text())

    def test_the_wrapper_is_executable_as_the_station_runs_it(self):
        # LOOM_REWRITE_CMD names the script as the command itself, so a lost
        # execute bit fails every rewrite with PermissionError (2026-10-17).
        self.assertTrue(os.access(WRAPPER, os.X_OK), "%s lost its execute bit" % WRAPPER)

    def test_codex_verifies_tls_from_the_system_bundle_with_the_keychain_denied(self):
        result = self.run_wrapper("Rewrite this", args=("-", "codex/test-model"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(str(self.lane_log) + ".codexenv").read_text(),
                         "cert=/etc/ssl/cert.pem\n")
        self.assertIn('(global-name "com.apple.SecurityServer")', self.sandbox_log.read_text())

    def test_failed_lane_falls_back_to_next_entry(self):
        result = self.run_wrapper("MODE=fail-codex", args=("-", "codex/test-model,claude/test-model"),
                                  secret="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '{"action":"skip","reason":"fake"}\n')
        self.assertEqual([line.split("|", 1)[0] for line in self.lane_log.read_text().splitlines()],
                         ["codex", "claude"])

    def test_claude_token_cannot_add_another_environment_variable(self):
        result = self.run_wrapper(
            args=("-", "claude/test-model"), secret="placeholder\nINJECTED=present",
            env_overrides={"LOOM_REWRITE_CLAUDE_TOKEN_SSM": "loom/test/key"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("token-intact=yes injected=absent",
                      Path(str(self.lane_log) + ".env").read_text())

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
    def test_prompt_reaches_the_agent_on_stdin_and_only_the_final_text_is_printed(self):
        prompt = 'Rewrite this.\nAnswer {"action": "brief"} with "quotes" kept.\n'
        result = self.run_wrapper(prompt)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, ANSWER + "\n")
        self.assertEqual(Path(str(self.agent_log) + ".stdin").read_text(), prompt.rstrip("\n"))
        argv = self.agent()["argv"]
        self.assertEqual(argv, f"run -m {MODEL} --print-logs --log-level ERROR --format json")
        self.assertNotIn("Rewrite this", argv)

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
    def test_the_key_reaches_only_the_agent_environment(self):
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 0, result.stderr)
        seen = self.agent()
        self.assertEqual(seen["key"], KEY)
        self.assertEqual(seen["psargv"], "clean")
        self.assertNotIn(KEY, seen["config"])
        self.assertNotIn(KEY, seen["argv"])
        self.assertNotIn(KEY, result.stdout + result.stderr)
        self.assertNotIn(KEY, self.log.read_text())
        # An emptied environment: the wrapper's own variables stay out.
        names = set(seen["envnames"].split())
        self.assertNotIn("FAKE_LOG", names)
        self.assertNotIn("FAKE_SECRET", names)
        self.assertLessEqual(names, {"PATH", "HOME", "TMPDIR", "TMP", "TEMP", "TERM", "LANG",
                                     "OPENCODE_API_KEY", "OPENCODE_CONFIG_CONTENT",
                                     "PWD", "SHLVL", "_", "OLDPWD"})
        # A scratch home under the loom home, removed after the run.
        self.assertTrue(seen["home"].startswith(str(self.loom / "run" / "rewrite.")))
        self.assertEqual(list((self.loom / "run").iterdir()), [])

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
    def test_the_opencode_permission_config_denies_every_tool(self):
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 0, result.stderr)
        config = json.loads(self.agent()["config"])
        self.assertEqual(config["permission"], {"*": "deny"})
        self.assertEqual(config["model"], MODEL)
        self.assertEqual(config["small_model"], MODEL)

    def test_a_free_gateway_model_runs_on_the_route_with_no_key_and_no_tools(self):
        # Gemma's free tier answers small requests through the spark-gateway's
        # free route (2026-10-21): the public rewriter tries it first, keyless,
        # every tool denied. Private repos never get a free/ entry
        # (bus/rewrite.command_for).
        url = "https://gateway.invalid/free-public/gemini/v1"
        result = self.run_wrapper(args=("-", "free/gemma-4-31b-it"),
                                  env_overrides={"LOOM_REWRITE_FREE_URL": url})
        self.assertEqual(result.returncode, 0, result.stderr)
        config = json.loads(self.agent()["config"])
        self.assertEqual(config["model"], "free/gemma-4-31b-it")
        self.assertEqual(config["permission"], {"*": "deny"})
        self.assertEqual(config["provider"]["free"]["options"]["baseURL"], url)
        self.assertEqual(self.agent()["key"], "none")
        self.assertIn("rewrite_agent: answered by free/gemma-4-31b-it", result.stderr)
        self.assertNotIn("aws ssm", self.log.read_text() if self.log.exists() else "")

    def test_a_free_model_that_hangs_gives_way_within_its_own_timeout(self):
        # The gateway answers a cooled-down free pool with 503 and a
        # retry-after, and opencode keeps retrying: one rewrite hung its whole
        # 480 seconds, which a station pass would wait out (2026-10-21).
        started = time.time()
        result = self.run_wrapper("MODE=hang", args=("-", "free/gemma-4-31b-it,codex/test-model"),
                                  env_overrides={"LOOM_REWRITE_FREE_URL": "https://gateway.invalid/v1",
                                                 "LOOM_REWRITE_FREE_TIMEOUT": "3"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(time.time() - started, 25)
        self.assertIn("codex", self.lane_log.read_text())

    def test_a_free_model_with_no_route_set_falls_through_to_the_next(self):
        result = self.run_wrapper("Rewrite this", args=("-", "free/gemma-4-31b-it,codex/test-model"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '{"action":"skip","reason":"fake"}\n')
        self.assertIn("codex", self.lane_log.read_text())

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
    def test_a_failing_agent_exits_non_zero_and_prints_nothing(self):
        for mode in ("fail", "error", "notext"):
            with self.subTest(mode=mode):
                result = self.run_wrapper("MODE=%s" % mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertNotIn(KEY, result.stderr)
        self.assertIn("bad key [key]", self.run_wrapper("MODE=error").stderr)

    def test_an_answer_holding_the_key_is_not_printed(self):
        result = self.run_wrapper("MODE=leak")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertNotIn(KEY, result.stderr)

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
    def test_a_module_the_agent_leaves_is_never_imported(self):
        result = self.run_wrapper("MODE=plant")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, ANSWER + "\n")
        self.assertFalse(Path(str(self.agent_log) + ".planted").exists())

    def test_no_readable_key_fails_before_the_agent_runs(self):
        result = self.run_wrapper(secret="")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("no key readable", result.stderr)
        self.assertFalse(self.agent_log.exists())

    def test_bad_arguments_are_refused(self):
        for args in (("loom/test/key",), ("loom/test/key", "deepseek"),
                     ("loom/test/key", "someprovider/model"), ("bad name", MODEL)):
            with self.subTest(args=args):
                result = self.run_wrapper(args=args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse(self.agent_log.exists())

    def test_rate_limited_model_falls_back_to_the_next_model(self):
        first, second = MODEL, "openrouter/other-free-model"
        self.write_fake_opencode("1.18.4")
        path = self.bin / "opencode"
        path.write_text(path.read_text().replace("@RATE_MODEL@", first))
        path.chmod(0o755)
        result = self.run_wrapper("MODE=rate-first", args=("loom/test/key", first + "," + second))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, ANSWER + "\n")
        self.assertIn(first, result.stderr)
        self.assertIn("rate", result.stderr.lower())
        self.assertIn(f"run -m {second} --print-logs --log-level ERROR --format json", self.agent()["argv"])

    def test_bad_entry_in_model_list_is_refused_before_any_run(self):
        result = self.run_wrapper(args=("loom/test/key", MODEL + ",someprovider/model"))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(self.agent_log.exists())

    def test_all_failed_models_are_named_with_their_reasons(self):
        second = "openrouter/other-free-model"
        result = self.run_wrapper("MODE=notext", args=("loom/test/key", MODEL + "," + second))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn(MODEL, result.stderr)
        self.assertIn(second, result.stderr)
        self.assertIn("no text", result.stderr)

    @unittest.skipUnless(sandbox_caps.NESTED_SANDBOX, sandbox_caps.NO_NESTED_SANDBOX)
    def test_opencode_2_runs_against_its_own_server(self):
        self.write_fake_opencode("opencode v2.0.28")
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = self.agent()["argv"]
        self.assertRegex(argv, rf"^run --server http://127\.0\.0\.1:\d+ --auto -m {re.escape(MODEL)} --print-logs --log-level ERROR --format json$")
        self.assertNotIn("--standalone", argv)

    def test_the_lanes_and_the_rewriter_share_one_sandbox(self):
        shared = LANES / "agent_sandbox.sh"
        sources = re.compile(r'^\. "\$LOOM_ROOT/lanes/agent_sandbox\.sh"$', re.M)
        for user in (LANES / "run_lane.sh", WRAPPER):
            self.assertRegex(user.read_text(), sources, "%s does not source the shared sandbox" % user.name)
        for name in ("sandbox_profile", "api_agent", "opencode_config", "opencode_run", "ssm_value"):
            defined = [p.name for p in sorted(LANES.rglob("*.sh"))
                       if re.search(r"^%s\(\) \{" % name, p.read_text(), re.M)]
            self.assertEqual(defined, ["agent_sandbox.sh"], name)
        self.assertNotIn("aws ssm", (LANES / "run_lane.sh").read_text())
        self.assertNotIn("aws ssm", WRAPPER.read_text())
        self.assertIn("aws ssm", shared.read_text())


if __name__ == "__main__":
    unittest.main()
