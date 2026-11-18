"""Stateless intake preserves explicit non-build waits (#1901 criterion 4 slice)."""
from shadow_env import setUpModule, tearDownModule  # noqa: F401
from test_bus_station import Base, GitHub, station


class BlockedStatelessIntakeTest(Base):
    def test_blocked_ticket_is_not_queued_and_other_work_can_move(self):
        for number, labels in ((31, ["blocked"]), (32, ["awaiting-live-check"]),
                               (33, []), (34, ["claim:other-example"])):
            self.fake.add(number)
            self.fake.issues[number]["labels"] = [
                {"name": label} for label in ["loom:ticket"] + labels]
        st = station.Station(GitHub("token", transport=self.fake), self.settings)
        st.queue_stateless_tickets()
        for number in (31, 32, 34):
            self.assertNotIn("loom:queued", self.fake.names(number))
            self.assertEqual(self.fake.comments.get(number, []), [])
        self.assertIn("loom:queued", self.fake.names(33))

    def test_removing_blocked_allows_the_next_scheduled_intake(self):
        self.fake.add(31)
        self.fake.issues[31]["labels"] = [{"name": "loom:ticket"}, {"name": "blocked"}]
        st = station.Station(GitHub("token", transport=self.fake), self.settings)
        st.queue_stateless_tickets()
        self.assertNotIn("loom:queued", self.fake.names(31))
        self.fake.issues[31]["labels"] = [{"name": "loom:ticket"}]
        st.now += station.STATELESS_EVERY + 1
        st.queue_stateless_tickets()
        self.assertIn("loom:queued", self.fake.names(31))

    def test_no_change_replay_does_not_overwrite_a_new_block(self):
        self.fake.add(31)
        issue = self.fake.issues[31]
        issue["labels"] = [{"name": "loom:ticket"}, {"name": "loom:running"}]
        st = station.Station(GitHub("token", transport=self.fake), self.settings)
        self.assertFalse(st._no_change_intervened(issue, None, 0))
        issue["labels"].append({"name": "blocked"})
        self.assertTrue(st._no_change_intervened(issue, None, 0))
