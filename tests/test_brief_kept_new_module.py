"""acceptance_refusal refuses a kept-behaviour criterion that points at a new
test module: kept behaviour passes on main, so its test cannot fail there."""

import unittest

from shadow_env import setUpModule, tearDownModule  # noqa: F401

from bus import brain


def body(*criteria):
    return ("## Why\nwhy\n## What\nwhat\n## Acceptance criteria\n%s\n## Size\nS\n"
            "## Out of scope\n- none" % "\n".join("- [ ] " + c for c in criteria))


NEW = "A new test module `tests/test_x.py` shows that a gate refuses X; it fails on main."


class KeptNewModule(unittest.TestCase):
    def test_a_kept_criterion_naming_the_same_new_module_is_refused(self):
        refusal = brain.acceptance_refusal(body(
            "A new test module `tests/test_x.py` shows that a gate refuses X; it fails on main.",
            "The same new module shows that Y still counts."))
        self.assertRegex(refusal, "existing test module")

    def test_a_kept_criterion_naming_a_path_another_criterion_makes_new_is_refused(self):
        refusal = brain.acceptance_refusal(body(
            NEW, "A test in `tests/test_x.py` shows that Y still counts."))
        self.assertRegex(refusal, "existing test module")

    def test_a_kept_criterion_naming_an_existing_path_is_accepted(self):
        self.assertIsNone(brain.acceptance_refusal(body(
            NEW, "`tests/test_bus_brain.py` passes unchanged, so Y still counts.")))


if __name__ == "__main__":
    unittest.main()
