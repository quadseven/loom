"""A lane refuses to push a commit whose author email ends in @local."""
import unittest

from tests import test_run_lane as base


class LaneCommitAuthorTest(base.LaneRunnerTest):
    def test_a_local_author_is_refused_before_the_push(self):
        self.drop_brief()
        self.run_lane(GIT_AUTHOR_EMAIL="loom-lane@local", GIT_COMMITTER_EMAIL="loom-lane@local")
        failed = self.failed_line()
        self.assertIn("nothing pushed", failed)
        self.assertIn("@local", failed)
        self.assertFalse(any(c.startswith("gh pr create") for c in self.calls()))

    def test_a_resolvable_author_still_pushes(self):
        self.drop_brief()
        self.run_lane(GIT_AUTHOR_EMAIL="42+lane[bot]@users.noreply.github.com",
                      GIT_COMMITTER_EMAIL="42+lane[bot]@users.noreply.github.com")
        self.assertTrue(any(c.startswith("gh pr create") for c in self.calls()), self.calls())


for _name in dir(base.LaneRunnerTest):
    if _name.startswith("test_") and _name not in LaneCommitAuthorTest.__dict__:
        setattr(LaneCommitAuthorTest, _name, None)

if __name__ == "__main__":
    unittest.main()
