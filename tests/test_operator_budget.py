"""Offline acceptance tests for operator REST budget sampling."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from bus import operator_budget as budget
from bus.github import GitHubError, RateLimited

NOW = 1900000000
CORE = {"remaining": 700, "limit": 5000, "reset": NOW + 3600}


class Client:
    def __init__(self, login="operator", core=None):
        self.login = login
        self.core = dict(CORE if core is None else core)
        self.calls = []

    def rest(self, method, path):
        self.calls.append((method, path))
        if path == "/user":
            if isinstance(self.login, Exception):
                raise self.login
            return SimpleNamespace(data={"login": self.login, "type": "User"})
        return SimpleNamespace(data={"resources": {"core": self.core,
                                                    "graphql": {"remaining": 99}}})


class IdentityTests(unittest.TestCase):
    def read(self, client, binding=None, token="synthetic-test-credential", operator="operator", now=NOW):
        return budget.read_core(operator, binding or {}, now,
                                resolve=lambda: token, client=lambda _: client)

    def test_verified_core_and_private_binding(self):
        client = Client()
        core, binding = self.read(client)
        self.assertEqual(core, CORE)
        self.assertIn(("GET", "/user"), client.calls)
        serialized = json.dumps(binding)
        self.assertNotIn("synthetic-test-credential", serialized)
        self.assertNotIn("operator", serialized)
        self.assertEqual(len(binding["key"]), 64)

    def test_zero_after_restart_uses_exact_verified_binding(self):
        _, binding = self.read(Client())
        client = Client(RuntimeError("must not read identity"), dict(CORE, remaining=0))
        core, _ = self.read(client, binding, now=NOW + 3600)
        self.assertEqual(core["remaining"], 0)
        self.assertEqual(client.calls, [("GET", "/rate_limit")])

    def test_rotation_operator_change_expiry_and_corruption_reverify(self):
        _, binding = self.read(Client())
        cases = [{"token": "rotated-test-credential"}, {"operator": "another"},
                 {"now": NOW + budget.BINDING_AGE + 1}, {"binding": {"key": "bad"}},
                 {"binding": dict(binding, at=NOW + 100)},
                 {"binding": dict(binding, at=float("nan"))}, {"binding": []}]
        for case in cases:
            with self.subTest(case=case):
                args = dict(binding=binding)
                args.update(case)
                with self.assertRaises(ValueError):
                    self.read(Client(login="wrong"), **args)

    def test_bot_wrong_and_missing_identity_are_refused(self):
        for login in ("app[bot]", "wrong", None):
            with self.subTest(login=login), self.assertRaises(ValueError):
                self.read(Client(login=login))

    def test_matching_bot_type_is_refused(self):
        client = Client()
        original = client.rest
        def rest(method, path):
            if path == "/user":
                return SimpleNamespace(data={"login": "operator", "type": "Bot"})
            return original(method, path)
        client.rest = rest
        with self.assertRaises(ValueError):
            self.read(client)

    def test_no_operator_never_resolves_credentials(self):
        resolve = mock.Mock()
        with self.assertRaises(ValueError):
            budget.read_core(None, {}, NOW, resolve=resolve)
        resolve.assert_not_called()

    def test_first_zero_without_identity_is_unknown(self):
        with self.assertRaises(GitHubError):
            self.read(Client(GitHubError("refused"), dict(CORE, remaining=0)))

    def test_invalid_core_is_refused(self):
        for core in ({}, dict(CORE, limit=0), dict(CORE, remaining=True),
                     dict(CORE, remaining=-1), dict(CORE, remaining=6000),
                     dict(CORE, reset="later")):
            with self.subTest(core=core), self.assertRaises(ValueError):
                self.read(Client(core=core))


class SamplerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / budget.FILE
        self.say = mock.Mock()
        self.reader = mock.Mock(return_value=(CORE, {"key": "a" * 64, "at": NOW}))

    def run_sample(self, now=NOW, active=False):
        return budget.sample(self.path, now, "operator", active, self.reader, self.say)

    def saved(self):
        return json.loads(self.path.read_text())

    def test_idle_hourly_restart_durability(self):
        self.run_sample()
        self.assertEqual(self.saved()["samples"][-1]["remaining"], 700)
        self.run_sample(NOW + 3599)
        self.assertEqual(self.reader.call_count, 1)
        self.run_sample(NOW + 3600)
        self.assertEqual(self.reader.call_count, 2)
        self.assertEqual(len(self.saved()["samples"]), 2)

    def test_failure_is_safe_nonfatal_and_retry_bounded(self):
        self.reader.side_effect = GitHubError("PRIVATE ERROR CONTENT")
        self.run_sample()
        saved = self.saved()
        self.assertEqual(saved["failures"][-1]["reason"], "read-failed")
        self.assertNotIn("PRIVATE", json.dumps(saved) + repr(self.say.call_args_list))
        self.run_sample(NOW + budget.RETRY - 1)
        self.assertEqual(self.reader.call_count, 1)
        self.run_sample(NOW + budget.RETRY)
        self.assertEqual(self.reader.call_count, 2)

    def test_expected_read_errors_are_sanitized_and_nonfatal(self):
        errors = (GitHubError("PRIVATE", body="PRIVATE BODY"), OSError("PRIVATE"),
                  ValueError("PRIVATE"), RateLimited("PRIVATE", 429, "PRIVATE BODY", NOW + 60))
        for index, error in enumerate(errors):
            with self.subTest(error=type(error).__name__):
                self.reader.side_effect = error
                self.run_sample(NOW + index * budget.RETRY)
                saved = self.saved()
                self.assertEqual(len(saved["failures"]), index + 1)
                self.assertEqual(saved["failures"][-1]["reason"], "read-failed")
                self.assertNotIn("PRIVATE", json.dumps(saved) + repr(self.say.call_args_list))

    def test_programming_error_escapes_without_becoming_a_read_failure(self):
        self.reader.side_effect = NameError("PRIVATE programmer detail")
        with self.assertRaises(NameError):
            self.run_sample()
        saved = self.saved()
        self.assertEqual(saved["attempted_at"], NOW)
        self.assertNotIn("failures", saved)
        self.assertNotIn("PRIVATE", json.dumps(saved))
        self.say.assert_not_called()

    def test_typed_record_roundtrip_discards_unrecognized_text(self):
        record = dict(CORE, at=NOW, active=True)
        budget.save(self.path, {"samples": [dict(record, note="ordinary text")]})
        self.assertEqual(budget.load(self.path)["samples"], [record])

    def test_low_active_flags_but_idle_does_not(self):
        self.reader.return_value = (dict(CORE, remaining=0), {})
        self.run_sample(active=False)
        self.assertNotIn("signal", self.saved())
        self.run_sample(NOW + 60, active=True)
        self.assertEqual(self.reader.call_count, 1)
        self.assertEqual(self.saved()["signal"]["remaining"], 0)

    def test_failure_preserves_signal_and_recovery_clears_it(self):
        self.reader.return_value = (dict(CORE, remaining=0), {})
        self.run_sample(active=True)
        self.reader.side_effect = GitHubError("private")
        self.run_sample(NOW + 3600)
        self.assertIn("signal", self.saved())
        self.reader.side_effect = None
        self.reader.return_value = (CORE, {})
        self.run_sample(NOW + 3900)
        self.assertNotIn("signal", self.saved())

    def test_corrupt_state_recovers_and_histories_are_bounded(self):
        self.path.write_text("not-json")
        self.run_sample()
        for index in range(180):
            self.run_sample(NOW + (index + 1) * 3600)
        self.assertLessEqual(len(self.saved()["samples"]), 169)

    def test_corrupt_json_shapes_fail_closed(self):
        for value in ([], 42, None, {"binding": [], "samples": [None, [], 42], "signal": []}):
            self.path.write_text(json.dumps(value))
            self.run_sample()
            self.assertEqual(self.saved()["samples"][-1]["remaining"], 700)
            self.assertEqual(self.reader.call_args.args[1], {})

    def test_expired_low_sample_does_not_flag_new_activity(self):
        self.reader.return_value = (dict(CORE, remaining=0, reset=NOW + 30), {})
        self.run_sample()
        self.run_sample(NOW + 60, active=True)
        self.assertNotIn("signal", self.saved())

    def test_attempt_is_durable_before_reader_runs(self):
        def read(*args):
            self.assertEqual(self.saved()["attempted_at"], NOW)
            raise GitHubError("failed")
        self.reader.side_effect = read
        self.run_sample()

    def test_write_failure_prevents_read_and_is_nonfatal(self):
        with mock.patch.object(budget, "save", side_effect=OSError("private path")):
            self.run_sample()
        self.reader.assert_not_called()
        self.assertNotIn("private", repr(self.say.call_args_list))


class IntegrationTests(unittest.TestCase):
    def test_sampling_precedes_budget_and_access_early_returns(self):
        from bus.station import Station
        for exhausted in (True, False):
            station = object.__new__(Station)
            events = []
            station.sample_operator_budget = lambda events=events: events.append("sample")
            station._budget_exhausted = lambda events=events, exhausted=exhausted: events.append("budget") or exhausted
            station.check_repo_access = lambda events=events: events.append("access") or False
            station.run_pass()
            self.assertEqual(events, ["sample", "budget"] if exhausted else ["sample", "budget", "access"])

    def test_station_injects_reader_and_observes_local_work(self):
        from bus.station import Station
        with tempfile.TemporaryDirectory() as directory:
            station = object.__new__(Station)
            station.state = directory
            station.now = NOW
            station.s = SimpleNamespace(authors=["app[bot]", "operator"], repos=[])
            station.operator_reader = mock.Mock(return_value=(dict(CORE, remaining=0), {}))
            station._operator_lane_active = lambda: True
            station.sample_operator_budget()
            state = budget.load(Path(directory) / budget.FILE)
            self.assertIn("signal", state)
            station.operator_reader.assert_called_once_with("operator", {}, NOW)

    def test_production_hook_is_wired_without_live_auth_in_injected_tests(self):
        from bus import station
        for supplied in (False, True):
            gh = SimpleNamespace(cache_dir="unused", app=None)
            settings = SimpleNamespace()
            with mock.patch.object(station, "Station") as constructor, \
                 mock.patch.object(station, "_trust_app_login"), \
                 mock.patch.object(station, "GitHub", return_value=gh), \
                 mock.patch("bus.github.auth_from_env", return_value="synthetic-test-credential") as auth:
                station._locked_pass(settings, "unused", gh if supplied else None, {})
                if supplied:
                    self.assertNotIn("operator_reader", constructor.call_args.kwargs)
                    auth.assert_not_called()
                else:
                    self.assertIs(constructor.call_args.kwargs["operator_reader"], budget.read_core)
                    auth.assert_called_once_with()

    def test_operator_sample_precedes_app_trust_failure(self):
        from bus import station
        gh = SimpleNamespace(cache_dir="unused", app=None)
        events = []
        with mock.patch.object(station, "Station") as constructor, \
             mock.patch.object(station, "_trust_app_login", side_effect=RuntimeError("app unavailable")):
            constructor.return_value.sample_operator_budget.side_effect = lambda: events.append("sample")
            with self.assertRaises(RuntimeError):
                station._locked_pass(SimpleNamespace(), "unused", gh, {})
        self.assertEqual(events, ["sample"])

    def test_extreme_state_numbers_are_unknown_not_exceptions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / budget.FILE
            path.write_text(json.dumps({"attempted_at": 10 ** 1000,
                                        "binding": {"key": "a" * 64, "at": 10 ** 1000}}))
            self.assertEqual(budget.load(path), {})

    def test_unknown_busy_scan_does_not_mean_active(self):
        from bus.station import Station
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "scans.json").write_text(json.dumps({"example/project": {"busy": True, "at": NOW}}))
            station = object.__new__(Station)
            station.state = directory
            station.now = NOW
            station.s = SimpleNamespace(authors=["operator"], repos=["example/project"])
            station.operator_reader = mock.Mock(return_value=(dict(CORE, remaining=0), {}))
            station._operator_lane_active = lambda: False
            station.sample_operator_budget()
            self.assertNotIn("signal", budget.load(Path(directory) / budget.FILE))

    def test_unbounded_core_values_fail_closed_in_reader_and_state(self):
        huge = 10 ** 1000
        for field in ("remaining", "limit", "reset"):
            core = dict(CORE, **{field: huge})
            with self.subTest(field=field), self.assertRaises(ValueError):
                budget.core_values(core)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / budget.FILE
            record = dict(CORE, remaining=0, limit=huge, at=NOW, active=True)
            path.write_text(json.dumps({"signal": record, "samples": [record]}))
            state = budget.load(path)
            self.assertNotIn("signal", state)
            self.assertEqual(state.get("samples"), [])
            budget.sample(path, NOW, "operator", True,
                          lambda *args: (dict(CORE, limit=huge), {}), lambda _: None)
            saved = budget.load(path)
            self.assertNotIn("signal", saved)
            self.assertEqual(saved["failures"][-1]["reason"], "read-failed")

    def test_queued_ticket_without_live_agent_does_not_signal(self):
        from bus.station import Station, LANES
        with tempfile.TemporaryDirectory() as directory:
            lane = next(iter(LANES))
            inbox = Path(directory, "briefs", lane)
            inbox.mkdir(parents=True)
            repo = "example/project"
            (inbox / "ticket.md").write_text("Issue: %s#%d\n" % (repo, 7))
            station = object.__new__(Station)
            station.state = str(Path(directory, "state", "station"))
            station.now = NOW
            station.s = SimpleNamespace(home=directory, authors=["operator"], repos=[repo])
            station.operator_reader = mock.Mock(return_value=(dict(CORE, remaining=0), {}))
            station._lock_pid = mock.Mock(return_value=None)
            station._pid_alive = mock.Mock(return_value=False)
            station.sample_operator_budget()
            self.assertNotIn("signal", budget.load(Path(station.state) / budget.FILE))
            station._pid_alive.assert_not_called()

    def test_full_week_retains_both_endpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / budget.FILE
            for hour in range(169):
                budget.sample(path, NOW + hour * 3600, "operator", False,
                              lambda *args: (CORE, {}), lambda _: None)
            samples = budget.load(path)["samples"]
            self.assertEqual(samples[0]["at"], NOW)
            self.assertEqual(samples[-1]["at"] - samples[0]["at"], 7 * 24 * 3600)

    def test_activity_requires_ticket_and_positive_live_agent_group(self):
        from bus.station import Station, LANES
        lane = next(iter(LANES))
        for assigned, pid, alive, expected in (
                (True, 123, True, True), (False, 123, True, False),
                (True, 123, False, False), (True, None, True, False),
                (True, -1, True, False), (True, 0, True, False),
                (True, True, True, False), (True, 10 ** 1000, True, False)):
            with self.subTest(assigned=assigned, pid=pid, alive=alive):
                station = object.__new__(Station)
                station.s = SimpleNamespace(home="unused")
                station._operator_ticket_assigned = lambda current, assigned=assigned: assigned and current == lane
                station._lock_pid = mock.Mock(return_value=pid)
                station._pid_alive = mock.Mock(return_value=alive)
                self.assertEqual(station._operator_lane_active(), expected)
                if not assigned or pid in (None, -1, 0, True, 10 ** 1000):
                    station._pid_alive.assert_not_called()
