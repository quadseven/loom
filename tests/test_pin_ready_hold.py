"""A ready pin pull request is left alone when a newer revision arrives."""
import unittest
from types import SimpleNamespace

from bus import pin
from tests.test_pin import BRANCH, NEXT, OLD, SPEC, SOURCE, FakeGitHub

HEAD = "e" * 40


class RunsGitHub(FakeGitHub):
    """A fake whose check runs are the ones a test sets."""

    def __init__(self, runs):
        super().__init__()
        self.runs = runs

    def rest(self, method, path, body=None, ok=()):
        if "/check-runs" in path:
            self.calls.append((method, path, body))
            return SimpleNamespace(data={"check_runs": self.runs})
        return super().rest(method, path, body, ok)


def run(name, conclusion="success", status="completed"):
    return {"name": name, "status": status, "conclusion": conclusion}


def pin_once(runs):
    gh = RunsGitHub(runs)
    gh.base = 'LOOM_REVISION="%s"\nLOOM_SHA256="%s"\n' % (OLD, "0" * 64)
    gh.branch = gh.base
    gh.branch_sha = HEAD
    gh.pull = {"number": 7, "head": {"ref": BRANCH, "sha": HEAD},
               "mergeable_state": "clean", "body": ""}
    logs = []
    pin.pin_one(gh, SOURCE, NEXT, SPEC, lambda: "digest", logs.append)
    writes = [c for c in gh.calls if c[0] in ("PUT", "PATCH", "POST")]
    return writes, logs


class ReadyPinHoldTest(unittest.TestCase):
    def test_a_ready_pull_request_is_not_rebuilt_for_a_newer_revision(self):
        writes, logs = pin_once([run("build"), run("Grug - Elder")])
        self.assertEqual(writes, [])
        self.assertTrue(any("pull request #7 is ready at " + OLD[:12] in line
                            and NEXT[:12] in line for line in logs), logs)

    def test_a_pull_request_without_a_passing_elder_check_is_rebuilt(self):
        for runs in ([run("build"), run("Grug - Elder", "neutral")], []):
            with self.subTest(runs=runs):
                writes, logs = pin_once(runs)
                self.assertTrue(any(c[0] == "PATCH" and "/git/refs/" in c[1]
                                    for c in writes))
                self.assertFalse(any("is ready at" in line for line in logs))


if __name__ == "__main__":
    unittest.main()
