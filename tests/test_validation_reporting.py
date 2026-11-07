"""Command success and changed-path coverage are separate evidence."""
import json
import os
import re
import sys
import subprocess
import unittest
from pathlib import Path


RUNNER = Path(__file__).resolve().parents[1] / "lanes" / "run_lane.sh"


class ValidationReporting(unittest.TestCase):
    def report(self, paths="main.py", changed_exit=0, validated="check exited 0 at abc123"):
        source = RUNNER.read_text()
        names = ("assess_validation_paths", "validation_summary", "validation_round_note",
                 "fix_contract_line")
        functions = "\n".join(re.search(r"^%s\(\) \{\n.*?^\}\n" % name,
                                        source, re.M | re.S).group(0) for name in names)
        script = functions + '''
set -uo pipefail
validation_changed_paths() { "$PYTHON" -c 'import json, os, sys; sys.stdout.buffer.write(b"\\0".join(p.encode() for p in json.loads(os.environ["CHANGED"])))'; return "$CHANGED_EXIT"; }
fix_checks() { printf 'build'; }
assess_validation_paths
validation_summary; printf '\\n'
validation_round_note; printf '\\n'
fix_contract_line /dev/null
'''
        result = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True,
                                env={"PATH": os.environ["PATH"], "PYTHON": sys.executable,
                                     "REPO_ROOT": str(RUNNER.parents[1]),
                                     "CHANGED": json.dumps(paths.splitlines() if isinstance(paths, str) else paths),
                                     "CHANGED_EXIT": str(changed_exit),
                                     "VALIDATED": validated, "HAS_CONTRACT": "yes",
                                     "CONTRACT_FILE": ".loom/validation.conf"})
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_python_source_is_unknown_even_after_a_successful_python_check(self):
        result = self.report("service/main.py")
        self.assertIn("check exited 0 at abc123", result)
        self.assertIn("not validated: contract path coverage unknown for `service/main.py`", result)
        self.assertNotIn("the contract check for the failing checks", result)
        self.assertIn("CI must check the new head", result)

    def test_build_inputs_and_documentation_are_not_silently_exempted(self):
        paths = "CMakeLists.txt\nsettings.gradle.kts\npackage.json\nREADME.md\nconfig.yaml"
        result = self.report(paths)
        self.assertIn(", ".join("`" + path + "`" for path in paths.splitlines()), result)
        self.assertIn("not validated", result)

    def test_failed_diff_read_does_not_claim_coverage(self):
        result = self.report("", changed_exit=1)
        self.assertIn("not validated: changed paths could not be read", result)
        self.assertIn("CI must validate", result)

    def test_empty_diff_does_not_invent_unknown_paths(self):
        result = self.report("")
        self.assertNotIn("not validated", result)
        self.assertIn("check exited 0 at abc123", result)

    def test_host_red_and_affected_evidence_remains_verbatim(self):
        evidence = ("final check passed with base tests deselected at abc123; "
                    "GitHub's checks passed on the base; affected check exited 0")
        result = self.report(validated=evidence)
        self.assertIn(evidence, result)
        self.assertIn("not validated", result)
        self.assertIn("CI and review run again on the new head", result)

    def test_retry_evidence_remains_verbatim(self):
        evidence = "final check passed on a second run at abc123"
        self.assertIn(evidence, self.report(validated=evidence))

    def test_long_list_is_bounded_and_keeps_ci_guidance(self):
        paths = ["source/" + "x" * 110 + str(n) + ".py" for n in range(700)]
        result = self.report(paths)
        summary, note, log = result.splitlines()
        self.assertLess(len(summary), 1000)
        self.assertLess(len(note), 1500)
        self.assertIn("more paths omitted (coverage unknown)", summary)
        self.assertIn("check exited 0 at abc123", summary)
        self.assertTrue(note.endswith("the factory lands the pull request once they pass."))

    def test_special_paths_render_inside_literal_spans(self):
        path = "src/@example/[sample](https://example.invalid)/`name`\nfile.kt"
        result = self.report([path])
        summary = result.splitlines()[0]
        self.assertIn(r"`src/@example/[sample](https://example.invalid)/\u0060name\u0060\nfile.kt`", summary)
        self.assertEqual(summary.count("`"), 2)

    def test_one_very_long_path_is_explicitly_truncated(self):
        result = self.report(["source/" + "x" * 5000 + ".py"])
        self.assertIn("[path truncated]", result)
        self.assertLess(len(result.splitlines()[0]), 400)
