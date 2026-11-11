"""Host timeouts in unchanged tests are not lane work failures."""
import unittest



class ValidationTimeoutClass(unittest.TestCase):
    def setUp(self):
        from bus.validation_timeout import only_untouched_test_timeouts

        self.classify = only_untouched_test_timeouts

    def test_only_timeout_errors_in_untouched_test_files_are_transient(self):
        output = """ERROR: test_case (test_example.Example.test_case)
Traceback (most recent call last):
  File \"test_example.py\", line 1, in test_case
    subprocess.run(command, timeout=60)
subprocess.TimeoutExpired: Command '['bash']' timed out after 60 seconds
"""
        self.assertTrue(self.classify(
            output, {"bus/brain.py"}, {"tests/test_example.py"}))

    def test_changed_test_file_is_not_classified_as_transient(self):
        output = """ERROR: test_case (test_example.Example.test_case)
Traceback (most recent call last):
subprocess.TimeoutExpired: Command '['bash']' timed out after 60 seconds
"""
        self.assertFalse(self.classify(
            output, {"tests/test_example.py"}, {"tests/test_example.py"}))

    def test_module_level_timeout_in_a_changed_test_file_is_not_transient(self):
        output = """ERROR: test_case (tests.test_validation_timeout_class)
Traceback (most recent call last):
subprocess.TimeoutExpired: Command '['bash']' timed out after 60 seconds
"""
        self.assertFalse(self.classify(
            output, {"tests/test_validation_timeout_class.py"},
            {"tests/test_validation_timeout_class.py"}))

    def test_mixed_timeout_and_assertion_failures_are_not_transient(self):
        output = """ERROR: test_case (test_example.Example.test_case)
subprocess.TimeoutExpired: Command '['bash']' timed out after 60 seconds
FAIL: test_other (test_example.Example.test_other)
AssertionError: mismatch
"""
        self.assertFalse(self.classify(
            output, set(), {"tests/test_example.py"}))

    def test_assertion_failure_alone_is_not_transient(self):
        output = """FAIL: test_case (test_example.Example.test_case)
AssertionError: mismatch
"""
        self.assertFalse(self.classify(
            output, set(), {"tests/test_example.py"}))


class ValidationTimeoutStationOutcome(unittest.TestCase):
    def test_second_unchanged_test_timeout_is_reported_as_work(self):
        import test_bus_station as fixtures
        from bus import station
        from bus.github import GitHub

        helper = fixtures.RetryTest(methodName="runTest")
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        reason = ("[transient] validation failed: only unchanged test files reported "
                  "TimeoutExpired errors at e992b44d7458 (base check exited 0)")
        helper.failed(reason=reason, body=fixtures.BODY_WITH_CRITERIA)
        helper.timeout_history(["Lane a stopped: " + reason])
        runner = station.Station(
            GitHub("fixture-only", transport=helper.fake), helper.settings)

        runner.report_outcomes()

        names = helper.fake.names(12)
        bodies = helper.bodies()
        self.assertIn("loom:failed", names, bodies)
        self.assertNotIn("loom:running", names)
        self.assertNotIn("loom:queued", names)
        failures = [body for body in bodies if body.startswith(
            "Lane codex failed: [work] validation failed: only unchanged test files "
            "reported TimeoutExpired errors")]
        self.assertEqual(len(failures), 1, bodies)
        self.assertIn("timed out again on this ticket", failures[0])
        self.assertFalse(any(body.startswith("Automatic retry") for body in bodies), bodies)

    def _reported_timeout(self, prior, reason):
        import test_bus_station as fixtures
        from bus import station
        from bus.github import GitHub

        helper = fixtures.RetryTest(methodName="runTest")
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        helper.failed(reason=reason, body=fixtures.BODY_WITH_CRITERIA)
        helper.timeout_history(prior)
        station.Station(GitHub("fixture-only", transport=helper.fake),
                        helper.settings).report_outcomes()
        return helper.fake.names(12), helper.bodies()

    def test_first_unchanged_test_timeout_stays_transient(self):
        reason = ("[transient] validation failed: only unchanged test files reported "
                  "TimeoutExpired errors at e992b44d7458 (base check exited 0)")
        names, bodies = self._reported_timeout([], reason)
        self.assertIn("loom:queued", names, bodies)
        self.assertNotIn("loom:failed", names)
        self.assertTrue(any(body.startswith("Lane codex stopped: " + reason)
                            for body in bodies), bodies)

    def test_second_ordinary_check_timeout_is_still_reported_as_work(self):
        reason = ("[transient] validation failed: the check timed out after 1800s "
                  "at e992b44d7458 (base check exited 0)")
        names, bodies = self._reported_timeout(["Lane a stopped: " + reason], reason)
        self.assertIn("loom:failed", names, bodies)
        self.assertTrue(any(body.startswith("Lane codex failed: [work]")
                            and "timed out again on this ticket" in body
                            for body in bodies), bodies)

    def test_non_timeout_ending_resets_unchanged_test_timeout_strikes(self):
        reason = ("[transient] validation failed: only unchanged test files reported "
                  "TimeoutExpired errors at e992b44d7458 (base check exited 0)")
        prior = ["Lane a stopped: " + reason,
                 "Lane a failed: [work] validation failed: the check exited 1"]
        names, bodies = self._reported_timeout(prior, reason)
        self.assertIn("loom:queued", names, bodies)
        self.assertNotIn("loom:failed", names)
        self.assertTrue(any(body.startswith("Lane codex stopped: " + reason)
                            for body in bodies), bodies)


class ValidationTimeoutRunnerOutcome(unittest.TestCase):
    def _run_validation_failure(self, *, mixed_failure=False, changed_test=False):
        import test_run_lane as fixtures

        helper = fixtures.LaneRunnerTest(methodName="runTest")
        helper.setUp()
        self.addCleanup(helper.tearDown)
        helper.write_tool(helper.bin / "sandbox-exec", '#!/bin/sh\nshift 2\nexec "$@"\n')
        helper.contract_repo("interpreter: loom-test-python\ncheck: python3 check.py\n")
        # The runner fixture keeps this checkout beside its synthetic remote.
        work = helper.home.parent / "contract-work"
        test_file = work / "tests" / "test_example.py"
        test_file.parent.mkdir()
        test_file.write_text("VALUE = 'original'\n")
        output = ("ERROR: test_case (test_example.Example.test_case)\n"
                  "Traceback (most recent call last):\n"
                  "subprocess.TimeoutExpired: Command '['bash']' timed out after 60 seconds")
        if mixed_failure:
            output += "\nFAIL: test_other (test_example.Example.test_other)\nAssertionError: mismatch"
        check = ("from pathlib import Path\nimport sys\n"
                 "unchanged = Path('fix.txt').read_text().strip() == 'todo' and "
                 "Path('tests/test_example.py').read_text() == \"VALUE = 'original'\\n\"\n"
                 "if unchanged:\n    sys.exit(0)\n"
                 "print(" + repr(output) + ")\nsys.exit(1)\n")
        (work / "check.py").write_text(check)
        fixtures.git("add", "check.py", "tests/test_example.py", cwd=work)
        fixtures.git("-c", "user.name=t", "-c", "user.email=t@example.invalid",
                     "commit", "-qm", "Seed validation timeout fixture", cwd=work)
        fixtures.git("push", "-q", "origin", "main", cwd=work)
        brief = fixtures.BRIEF
        env = {}
        if changed_test:
            brief += "\nChange tests/test_example.py.\n"
            env = {"FAKE_AGENT": "source", "FAKE_SOURCE_PATH": "tests/test_example.py",
                   "FAKE_SOURCE_TEXT": "VALUE = 'changed'"}
        helper.drop_brief(text=brief)

        result = helper.run_contract_lane(**env)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((helper.inbox / "failed" / "T042-fix-the-thing.md").exists(),
                        helper.lane_log())
        self.assertFalse(any(call.startswith("gh pr create") for call in helper.calls()))
        finals = [record for record in helper.records() if record["phase"] == "final"]
        self.assertEqual([record["exit"] for record in finals], [1, 1], helper.lane_log())
        return helper.lane_log()

    def test_final_validation_reports_unchanged_test_timeouts_as_transient(self):
        log = self._run_validation_failure()
        self.assertIn("failed: [transient] validation failed: only unchanged test files "
                      "reported TimeoutExpired errors", log)
        self.assertNotIn("failed: [work]", log)

    def test_final_validation_keeps_mixed_assertion_failures_as_work(self):
        log = self._run_validation_failure(mixed_failure=True)
        self.assertIn("failed: [work] validation failed: the check exited 1", log)
        self.assertNotIn("failed: [transient]", log)

    def test_final_validation_keeps_changed_test_timeouts_as_work(self):
        log = self._run_validation_failure(changed_test=True)
        self.assertIn("failed: [work] validation failed: the check exited 1", log)
        self.assertNotIn("failed: [transient]", log)

if __name__ == "__main__":
    unittest.main()
