"""lanes/rewrite_agent.sh run as a process: a temp loom home, a fake opencode
and a fake aws on PATH, and the real sandbox-exec, as the station runs it."""

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

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
  *MODE=fail*) echo "connection reset" >&2; exit 3 ;;
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
        self.write_fake_opencode("1.18.4")
        self.write_tool(self.bin / "aws", FAKE_AWS)

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
                    secret=KEY):
        env = {
            "HOME": str(self.home),
            "PATH": f"{self.bin}:/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin",
            "FAKE_LOG": str(self.log),
            "FAKE_SECRET_NAME": "loom/test/key",
            "FAKE_SECRET": secret,
        }
        return subprocess.run(["bash", str(WRAPPER), *args], input=prompt, env=env,
                              capture_output=True, text=True, timeout=60)

    def agent(self):
        lines = self.agent_log.read_text().splitlines() if self.agent_log.exists() else []
        return dict(line.split("=", 1) for line in lines if "=" in line)

    def test_prompt_reaches_the_agent_on_stdin_and_only_the_final_text_is_printed(self):
        prompt = 'Rewrite this.\nAnswer {"action": "brief"} with "quotes" kept.\n'
        result = self.run_wrapper(prompt)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, ANSWER + "\n")
        self.assertEqual(Path(str(self.agent_log) + ".stdin").read_text(), prompt.rstrip("\n"))
        argv = self.agent()["argv"]
        self.assertEqual(argv, f"run -m {MODEL} --format json")
        self.assertNotIn("Rewrite this", argv)

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

    def test_the_opencode_permission_config_denies_every_tool(self):
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 0, result.stderr)
        config = json.loads(self.agent()["config"])
        self.assertEqual(config["permission"], {"*": "deny"})
        self.assertEqual(config["model"], MODEL)
        self.assertEqual(config["small_model"], MODEL)

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

    def test_opencode_2_runs_standalone(self):
        self.write_fake_opencode("opencode v2.0.28")
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.agent()["argv"], f"run --standalone --auto -m {MODEL} --format json")

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
