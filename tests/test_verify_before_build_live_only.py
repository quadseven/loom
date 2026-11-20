"""A ticket whose only open boxes are Live boxes with no quoted string moves to
loom:merged when a merged pull request covers it, and asks the brain to
restate the Live criterion."""
from unittest import mock

from bus import station, tickets
from bus.github import GitHub
from test_bus_station import Base, REPO
from shadow_env import setUpModule, tearDownModule  # noqa: F401

LIVE_ONLY = (
    "## Acceptance criteria\n"
    "- [x] A test shows the change.\n"
    "- [ ] Live: after the deploy, the station log shows the new line.\n")
CODE_OPEN = (
    "## Acceptance criteria\n"
    "- [ ] A test shows the change.\n"
    "- [ ] Live: after the deploy, the station log shows the new line.\n")
PULL = {"number": 77, "merged_at": "2030-01-01T00:00:00Z", "merge_commit_sha": "%040x" % 77}


class VerifyBeforeBuildLiveOnly(Base):
    def run_check(self, body):
        self.fake.add(12, state="queued", body=body)
        runner = station.Station(GitHub("token", transport=self.fake), self.settings)
        notes, labels = [], []
        with mock.patch.object(runner, "_merged_linked", return_value=[dict(PULL)]), \
                mock.patch.object(runner, "_status_texts", return_value=[]), \
                mock.patch.object(runner, "_routine",
                                  side_effect=lambda r, n, t: notes.append(t)), \
                mock.patch.object(runner, "_add_label",
                                  side_effect=lambda r, n, lab: labels.append(lab)):
            moved = runner._verify_before_build(REPO, self.fake.issues[12])
        return moved, notes, labels

    def test_only_live_boxes_open_moves_to_merged_and_asks_the_brain(self):
        moved, notes, labels = self.run_check(LIVE_ONLY)
        self.assertTrue(moved)
        self.assertIn("loom:merged", self.fake.names(12))
        self.assertTrue(any("#77" in n for n in notes))
        self.assertIn(tickets.LIVE_RESTATE_NOTE, notes)
        self.assertEqual(labels, ["needs-brain"])

    def test_an_open_code_box_still_goes_to_a_build_lane(self):
        moved, notes, labels = self.run_check(CODE_OPEN)
        self.assertFalse(moved)
        self.assertNotIn("loom:merged", self.fake.names(12))
        self.assertEqual((notes, labels), ([], []))
