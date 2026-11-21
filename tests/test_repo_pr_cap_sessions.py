"""The open pull request cap counts Loom pull requests, not a coding session's."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_bus_station as fixtures
from test_bus_station import Base, BODY_WITH_CRITERIA, REPO, AUTHORS, GitHub, station

SESSION = "session-app-example"


def pr(number, login, ref, bot=True, body=""):
    return dict(number=number, state="open", draft=False, body=body,
                user={"login": login, "type": "Bot" if bot else "User"},
                head={"ref": ref})


class SessionPrCapTest(Base):
    def setUp(self):
        super().setUp()
        self.fake.default_body = BODY_WITH_CRITERIA
        self.settings.briefers = [SESSION]

    def runner(self):
        return station.Station(GitHub("token", transport=self.fake), self.settings)

    def test_session_bot_prs_do_not_hold_a_build_ticket_at_the_cap(self):
        self.fake.pulls = ([pr(100 + i, AUTHORS[0], "loom/%d-work" % i) for i in range(4)]
                           + [pr(200 + i, SESSION, "claude/work-%d" % i) for i in range(6)])
        self.fake.add(12)
        runner = self.runner()
        self.assertEqual(runner._repo_pr_count(REPO), 4)
        self.assertTrue(runner._claim(REPO, "codex", self.fake.issues[12]))
        self.assertNotIn("open pull requests (cap 8)", str(self.fake.comments[12]))

    def test_session_pr_on_a_loom_head_or_with_the_loom_line_still_counts(self):
        self.fake.pulls = [
            pr(1, SESSION, "loom/1-work"),
            pr(2, SESSION, "other/work",
               body="Loom ticket n/a (direct request), lane main-chat, model m-1."),
        ]
        self.assertEqual(self.runner()._repo_pr_count(REPO), 2)

    def test_a_bot_from_a_brief_author_login_still_counts(self):
        self.fake.pulls = [pr(1, AUTHORS[0], "manual/work")]
        self.assertEqual(self.runner()._repo_pr_count(REPO), 1)


if __name__ == "__main__":
    unittest.main()
