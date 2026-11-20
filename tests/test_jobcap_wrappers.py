"""lanes/jobcap/xcodebuild and lanes/jobcap/swift run as processes: the
wrappers a lane run finds first on PATH, in front of a stand-in for the real
tool that records the arguments it was started with."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

JOBCAP = Path(__file__).resolve().parent.parent / "lanes" / "jobcap"

# The stand-in real tool: a marker for the call, then each argument, every
# one ended by a NUL, so an empty argument, a space and a newline inside one
# all show. Exits $REAL_EXIT.
CALL = "\x01call"
REAL_TOOL = """#!/bin/bash
{
  printf '\\001call\\0'
  [ $# -eq 0 ] || printf '%s\\0' "$@"
} >> "$CALLS"
cat > /dev/null
exit "${REAL_EXIT:-0}"
"""


class JobCapWrapperTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.real_bin = self.root / "real"
        self.real_bin.mkdir()
        self.calls = self.root / "calls"
        self.calls.write_text("")
        for name in ("xcodebuild", "swift"):
            (self.real_bin / name).write_text(REAL_TOOL)
            (self.real_bin / name).chmod(0o755)

    def tearDown(self):
        self._tmp.cleanup()

    def run_tool(self, tool, *args, jobs="3", path=None, **env):
        """Run `tool` by name with the wrapper directory first on PATH.
        Returns the process and the argument lists the real tool got."""
        self.calls.write_text("")
        full = {"PATH": path or f"{JOBCAP}:{self.real_bin}:/usr/bin:/bin",
                "CALLS": str(self.calls), **env}
        if jobs is not None:
            full["LOOM_BUILD_JOBS"] = jobs
        # Found on PATH by a shell, as an agent or a check command finds it.
        result = subprocess.run(["/bin/bash", "-c", 'exec "$0" "$@"', tool, *args], env=full,
                                capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
        got = []
        for word in self.calls.read_text().split("\0")[:-1]:
            if word == CALL:
                got.append([])
            else:
                got[-1].append(word)
        return result, got

    def passed(self, tool, *args, **kwargs):
        """The one argument list the real tool got."""
        result, got = self.run_tool(tool, *args, **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(got), 1, got)
        return got[0]


class XcodebuildWrapperTest(JobCapWrapperTest):
    def test_a_build_or_test_call_gets_the_job_flag(self):
        for args in (["build"], ["-scheme", "App", "test"], ["clean", "build"],
                     ["-workspace", "A.xcworkspace", "-scheme", "App", "build-for-testing"],
                     ["archive", "-archivePath", "out"], ["analyze"], ["test-without-building"],
                     ["-scheme", "App", "-destination", "platform=macOS", "build", "CODE_SIGNING_ALLOWED=NO"]):
            with self.subTest(args=args):
                self.assertEqual(self.passed("xcodebuild", *args), ["-jobs", "3", *args])

    def test_a_call_with_no_action_builds_so_it_gets_the_flag(self):
        # xcodebuild's default action is build.
        self.assertEqual(self.passed("xcodebuild"), ["-jobs", "3"])
        self.assertEqual(self.passed("xcodebuild", "-scheme", "App", "-configuration", "Debug"),
                         ["-jobs", "3", "-scheme", "App", "-configuration", "Debug"])

    def test_an_options_value_is_never_read_as_an_action(self):
        # A scheme, target or configuration can be named like an action or a
        # report: the call builds all the same, so it is capped.
        for args in (["-scheme", "clean"], ["-target", "installsrc", "-configuration", "clean"],
                     ["-scheme", "-list"], ["-destination", "clean", "-scheme", "App"],
                     ["-scheme", "clean", "build"], ["-derivedDataPath", "-version", "test"]):
            with self.subTest(args=args):
                self.assertEqual(self.passed("xcodebuild", *args), ["-jobs", "3", *args])
        # And a real clean beside such a value still builds nothing.
        args = ["-scheme", "build", "clean"]
        self.assertEqual(self.passed("xcodebuild", *args), args)

    def test_a_call_with_its_own_job_count_is_left_alone(self):
        for args in (["-jobs", "12", "build"], ["build", "-jobs", "1"],
                     ["build", "-IDEBuildOperationMaxNumberOfConcurrentCompileTasks=6"]):
            with self.subTest(args=args):
                self.assertEqual(self.passed("xcodebuild", *args), args)

    def test_a_call_that_builds_nothing_passes_through(self):
        for args in (["-version"], ["-list"], ["-list", "-json", "-project", "A.xcodeproj"],
                     ["-showBuildSettings", "-scheme", "App"], ["-showsdks"], ["clean"],
                     ["-scheme", "App", "clean"], ["-exportArchive", "-archivePath", "a", "-exportPath", "b"],
                     ["-resolvePackageDependencies"], ["-showdestinations", "-scheme", "App"],
                     ["-help"], ["-usage"], ["-license"], ["-runFirstLaunch"],
                     # An action word beside a call that only reports is still only a report.
                     ["-showBuildSettings", "build", "-scheme", "App"]):
            with self.subTest(args=args):
                self.assertEqual(self.passed("xcodebuild", *args), args)

    def test_every_argument_is_passed_on_unchanged(self):
        args = ["-scheme", "My App", "", "build", "OTHER_SWIFT_FLAGS=-D A -D B", "*", "$HOME",
                "a\nb", "it's", '"q"', "--", "-n"]
        self.assertEqual(self.passed("xcodebuild", *args), ["-jobs", "3", *args])

    def test_no_cap_passes_every_call_through_untouched(self):
        for jobs in ("0", None, "", "many", "-2", "00"):
            with self.subTest(jobs=jobs):
                self.assertEqual(self.passed("xcodebuild", "-scheme", "My App", "build", jobs=jobs),
                                 ["-scheme", "My App", "build"])

    def test_the_real_tools_exit_status_and_output_come_back(self):
        result, got = self.run_tool("xcodebuild", "build", REAL_EXIT="65")
        self.assertEqual(result.returncode, 65)
        self.assertEqual(got, [["-jobs", "3", "build"]])


class SwiftWrapperTest(JobCapWrapperTest):
    def test_build_test_and_run_get_the_job_flag(self):
        for args in (["build"], ["test"], ["run"], ["build", "-c", "release"],
                     ["test", "--filter", "FooTests"], ["build", "--product", "tool"]):
            with self.subTest(args=args):
                self.assertEqual(self.passed("swift", *args), [args[0], "--jobs", "3", *args[1:]])

    def test_the_flag_never_reaches_the_program_swift_run_starts(self):
        # Arguments after the program's name are the program's own.
        self.assertEqual(self.passed("swift", "run", "tool", "--verbose", "in.txt"),
                         ["run", "--jobs", "3", "tool", "--verbose", "in.txt"])
        self.assertEqual(self.passed("swift", "run", "tool", "--", "-j", "9"),
                         ["run", "--jobs", "3", "tool", "--", "-j", "9"])

    def test_a_call_with_its_own_job_count_is_left_alone(self):
        for args in (["build", "-j", "12"], ["build", "-j12"], ["test", "--jobs", "2"],
                     ["build", "--jobs=8"], ["run", "-j", "1", "tool"]):
            with self.subTest(args=args):
                self.assertEqual(self.passed("swift", *args), args)

    def test_a_call_that_builds_nothing_passes_through(self):
        for args in ([], ["--version"], ["package", "resolve"], ["package", "clean"],
                     ["package", "describe", "--type", "json"], ["script.swift", "build"],
                     ["-e", "print(1)"], ["sdk", "list"], ["repl"], ["-frontend", "-version"]):
            with self.subTest(args=args):
                self.assertEqual(self.passed("swift", *args), args)

    def test_every_argument_is_passed_on_unchanged(self):
        args = ["build", "-Xswiftc", "-D A", "", "*", "$HOME", "a\nb", "it's", '"q"']
        self.assertEqual(self.passed("swift", *args), ["build", "--jobs", "3", *args[1:]])

    def test_no_cap_passes_every_call_through_untouched(self):
        for jobs in ("0", None, "", "many", "-2", "00"):
            with self.subTest(jobs=jobs):
                self.assertEqual(self.passed("swift", "build", "-c", "release", jobs=jobs),
                                 ["build", "-c", "release"])

    def test_the_real_tools_exit_status_comes_back(self):
        result, got = self.run_tool("swift", "test", REAL_EXIT="1")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(got, [["test", "--jobs", "3"]])


class WrapperFindsTheRealToolTest(JobCapWrapperTest):
    """A wrapper runs the next tool of its name on PATH and never itself."""

    def test_the_wrapper_directory_twice_on_path_still_reaches_the_real_tool_once(self):
        for tool in ("xcodebuild", "swift"):
            with self.subTest(tool=tool):
                path = f"{JOBCAP}:{JOBCAP}/:{JOBCAP}/../jobcap:{self.real_bin}:/usr/bin:/bin"
                got = self.passed(tool, "build", path=path)
                self.assertEqual(sorted(got), sorted(["build", "-jobs" if tool == "xcodebuild" else "--jobs", "3"]))

    def test_a_link_to_the_wrapper_directory_or_to_a_wrapper_is_not_taken_for_the_real_tool(self):
        linked_dir = self.root / "linked"
        os.symlink(JOBCAP, linked_dir)
        linked_file = self.root / "links"
        linked_file.mkdir()
        for tool in ("xcodebuild", "swift"):
            os.symlink(JOBCAP / tool, linked_file / tool)
        for tool in ("xcodebuild", "swift"):
            with self.subTest(tool=tool):
                path = f"{linked_dir}:{linked_file}:{JOBCAP}:{self.real_bin}:/usr/bin:/bin"
                result, got = self.run_tool(tool, "build", path=path)
                self.assertEqual(result.returncode, 0, result.stderr)
                # One call reached the real tool, with the flag added once.
                self.assertEqual(len(got), 1, got)
                self.assertEqual(sum(a in ("-jobs", "--jobs") for a in got[0]), 1, got)

    def test_with_no_real_tool_the_wrapper_says_so_and_does_not_run_itself(self):
        for tool in ("xcodebuild", "swift"):
            for jobs in ("3", "0"):
                with self.subTest(tool=tool, jobs=jobs):
                    result, got = self.run_tool(tool, "build", jobs=jobs,
                                                path=f"{JOBCAP}:{JOBCAP}:/nonexistent::/bin")
                    self.assertEqual(result.returncode, 127)
                    self.assertIn(f"{tool}: command not found", result.stderr)
                    self.assertEqual(got, [])

    def test_a_directory_named_like_the_tool_is_skipped(self):
        decoy = self.root / "decoy"
        (decoy / "swift").mkdir(parents=True)
        path = f"{JOBCAP}:{decoy}:{self.real_bin}:/usr/bin:/bin"
        self.assertEqual(self.passed("swift", "build", path=path), ["build", "--jobs", "3"])

    def test_the_wrappers_are_executable_and_run_under_the_lane_hosts_bash(self):
        for tool in ("xcodebuild", "swift"):
            self.assertTrue(os.access(JOBCAP / tool, os.X_OK), tool)
            self.assertEqual((JOBCAP / tool).read_text().splitlines()[0], "#!/bin/bash")
        self.assertEqual(sorted(p.name for p in JOBCAP.iterdir()), ["swift", "xcodebuild"])


if __name__ == "__main__":
    unittest.main()
