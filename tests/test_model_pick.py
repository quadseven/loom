"""bus/model_pick.py: each Go or Zen run picks a live free model first, and a
paid model only under the budget."""
import json
import tempfile
import unittest
from pathlib import Path

from bus import model_pick, tickets

NOW = 2_000_000_000.0
GO_LIST = "https://go.test/models"
ZEN_LIST = "https://zen.test/models"
STATUS = "https://status.test/api.json"
GO_PROBE = "https://go.test/chat"
ZEN_PROBE = "https://zen.test/chat"
ENV = {"LOOM_OPENCODE_GO_MODELS_URL": GO_LIST, "LOOM_ZEN_MODELS_URL": ZEN_LIST,
       "LOOM_MODELS_STATUS_URL": STATUS, "LOOM_OPENCODE_GO_PROBE_URL": GO_PROBE,
       "LOOM_OPENCODE_GO_PROBE_HEADER": "x-opencode-session", "LOOM_ZEN_PROBE_URL": ZEN_PROBE}
MIMO = "opencode-go/mimo-v2.6-flash"
MUSE = "opencode-go/muse-spark-1.3-contributor"


def listing(*ids):
    return json.dumps({"data": [{"id": i} for i in ids]}).encode()


class Provider:
    """The provider side: the lists and a probe answer per model."""

    def __init__(self):
        self.go = ["beam-free", "iceman-contributor-free", "mimo-v2.6-flash",
                   "muse-spark-1.3-contributor", "kimi-k3", "deepseek-v4.1-flash"]
        self.zen = ["glm-5.3-flash-free", "flint-40b-free"]
        self.deprecated = set()
        self.answers = {}
        self.probed = []
        self.down = set()

    def status_doc(self):
        def block(ids):
            return {"models": {i: ({"status": "deprecated"} if i in self.deprecated else {})
                               for i in ids}}
        return json.dumps({"opencode-go": block(self.go), "opencode": block(self.zen)}).encode()

    def __call__(self, request, timeout):
        url = request.full_url
        if url in self.down:
            return None
        if url == GO_LIST:
            return 200, {}, listing(*self.go)
        if url == ZEN_LIST:
            return 200, {}, listing(*self.zen)
        if url == STATUS:
            return 200, {}, self.status_doc()
        model = json.loads(request.data)["model"]
        self.probed.append((url, model))
        return self.answers.get(model, (200, {}, b"{}"))


class ModelPickTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.provider = Provider()
        self.now = NOW

    def pick(self, lane="opencode-go", private=False):
        return model_pick.pick(lane, "slot", private, "fake-key", self.home, self.now,
                               ENV, self.provider)

    def later(self, seconds=model_pick.LIST_FRESH + 1):
        self.now += seconds

    def test_a_go_slot_takes_the_first_free_model(self):
        got = self.pick()
        self.assertEqual((got.model, got.kind), ("opencode-go/beam-free", "free"))

    def test_a_zen_slot_takes_only_zen_free_models(self):
        got = self.pick("zen")
        self.assertEqual((got.model, got.kind), ("opencode/glm-5.3-flash-free", "free"))

    def test_a_model_marked_deprecated_or_gone_from_the_status_list_is_never_picked(self):
        self.provider.deprecated.add("beam-free")
        self.assertEqual(self.pick().model, "opencode-go/iceman-contributor-free")
        self.later()
        self.provider.go.remove("iceman-contributor-free")
        self.provider.status_doc = lambda: json.dumps(
            {"opencode-go": {"models": {"mimo-v2.6-flash": {}}},
             "opencode": {"models": {"flint-40b-free": {}}}}).encode()
        self.assertEqual(self.pick().model, "opencode/flint-40b-free")

    def test_a_free_model_that_left_the_list_is_not_picked(self):
        self.provider.go.remove("beam-free")
        self.assertNotEqual(self.pick().model, "opencode-go/beam-free")

    def test_a_429_pauses_the_model_for_every_slot_and_the_next_free_model_runs(self):
        self.provider.answers["beam-free"] = (429, {"Retry-After": "600"}, b'{"error":"busy"}')
        got = self.pick()
        self.assertEqual(got.model, "opencode-go/iceman-contributor-free")
        self.assertIn("beam-free", got.note)
        self.provider.probed.clear()
        self.assertEqual(self.pick().model, "opencode-go/iceman-contributor-free")
        self.assertNotIn((GO_PROBE, "beam-free"), self.provider.probed)
        self.now += 601
        del self.provider.answers["beam-free"]
        self.assertEqual(self.pick().model, "opencode-go/beam-free")

    def test_an_unavailable_model_is_not_retried_for_a_day(self):
        self.provider.answers["beam-free"] = (400, {}, b'{"error":{"message":"Model is unavailable."}}')
        self.pick()
        self.now += 3600
        self.provider.probed.clear()
        self.pick()
        self.assertNotIn((GO_PROBE, "beam-free"), self.provider.probed)

    def test_a_zen_free_limit_pauses_all_zen_models_until_the_reset(self):
        self.provider.answers["glm-5.3-flash-free"] = (
            429, {"Retry-After": "30889"}, b'{"error":{"type":"FreeUsageLimitError"}}')
        got = self.pick("zen")
        self.assertIsNone(got.model)
        self.assertEqual(got.until, NOW + 30889)
        self.assertEqual([p for p in self.provider.probed if p[1] == "flint-40b-free"], [])

    def test_a_private_repo_never_gets_a_model_that_trains_on_prompts(self):
        self.provider.answers["beam-free"] = (429, {}, b"{}")
        got = self.pick(private=True)
        self.assertEqual((got.model, got.kind), (MIMO, "paid"))

    def free_models_down(self):
        for model in ("beam-free", "iceman-contributor-free", "glm-5.3-flash-free", "flint-40b-free"):
            self.provider.answers[model] = (429, {"Retry-After": "90000"}, b"{}")

    def test_a_paid_model_runs_only_after_no_free_model_works(self):
        got = self.pick()
        self.assertEqual(got.kind, "free")
        self.assertNotIn(MIMO.split("/")[1], [m for _, m in self.provider.probed])
        self.free_models_down()
        self.later(90001)
        got = self.pick()
        self.assertEqual((got.model, got.kind), (MIMO, "paid"))
        self.assertIn("no free model", got.note)

    def test_only_listed_high_allowance_paid_models_can_ever_run(self):
        self.assertLessEqual(set(model_pick.GO_PAID), set(tickets.OPENCODE_GO_MODELS))
        self.assertNotIn("opencode-go/kimi-k3", model_pick.GO_PAID)
        self.assertNotIn("opencode-go/deepseek-v4.1-flash", model_pick.GO_PAID)
        for limits in model_pick.GO_PAID.values():
            self.assertGreaterEqual(limits[0], 10_000)

    def test_a_paid_model_on_a_429_pauses_every_paid_model_when_the_plan_is_spent(self):
        self.free_models_down()
        self.provider.answers["mimo-v2.6-flash"] = (
            429, {"Retry-After": "7200"}, b'{"error":{"type":"GoUsageLimitError"}}')
        got = self.pick()
        self.assertIsNone(got.model)
        self.assertEqual(got.until, NOW + 7200)
        self.assertNotIn("muse-spark-1.3-contributor", [m for _, m in self.provider.probed])

    def test_the_budget_stops_paid_runs_below_the_published_limit(self):
        self.free_models_down()
        runs = 0
        while self.pick().model == MIMO:
            runs += 1
            self.assertLess(runs, 1000, "no budget stopped the paid runs")
        limit = model_pick.GO_PAID[MIMO][0]
        spent = runs * model_pick.RUN_RESERVE
        self.assertLess(spent, limit * model_pick.BUDGET_SHARE + model_pick.RUN_RESERVE)
        self.assertGreater(runs, 0)
        self.assertEqual(self.pick(private=True).model, None)

    def test_the_budget_is_one_plan_across_paid_models(self):
        self.free_models_down()
        share = model_pick.BUDGET_SHARE * model_pick.GO_PAID[MUSE][0]
        model_pick.settle(self.home, self.now - 60, MUSE,
                          [json.dumps({"type": "step_finish", "part": {"tokens": {"output": 300}}})]
                          * int(share * 0.99) + ["x"], reserve=0)
        self.assertEqual(self.pick(private=True).model, None)

    def test_the_budget_refills_as_the_window_passes(self):
        self.free_models_down()
        self.later(90001)
        while self.pick(private=True).model is not None:
            pass
        self.now += 5 * 3600 + 1
        self.free_models_down()
        self.assertEqual(self.pick(private=True).model, MIMO)

    def test_hidden_reasoning_counts_against_the_budget(self):
        step = json.dumps({"type": "step_finish",
                           "part": {"tokens": {"output": 10, "reasoning": 8182}}})
        self.assertEqual(model_pick.effective_requests([step]), 28)
        self.assertEqual(model_pick.effective_requests(
            ['{"type":"step_finish","part":{"tokens":{"output":4}}}', "noise"]), 1)

    def test_settling_a_run_replaces_its_reservation_with_what_it_used(self):
        self.free_models_down()
        self.assertEqual(self.pick().model, MIMO)
        model_pick.settle(self.home, self.now, MIMO,
                          ['{"type":"step_finish","part":{"tokens":{"output":1}}}'])
        got = model_pick.spent(self.home, self.now, MIMO)
        self.assertAlmostEqual(got[0], 1 / model_pick.GO_PAID[MIMO][0])

    def test_a_run_with_no_readable_events_keeps_its_reservation(self):
        self.free_models_down()
        self.pick()
        before = model_pick.spent(self.home, self.now, MIMO)
        model_pick.settle(self.home, self.now, MIMO, ["not json"])
        self.assertEqual(model_pick.spent(self.home, self.now, MIMO), before)

    def test_a_free_model_settles_nothing(self):
        model_pick.settle(self.home, self.now, "opencode-go/beam-free",
                          ['{"type":"step_finish","part":{"tokens":{"output":99999}}}'])
        self.assertFalse(model_pick.budget_file(self.home).exists())

    def test_a_failed_run_pauses_by_what_the_cli_said(self):
        cases = (("ProviderModelNotFoundError: x", "opencode-go/beam-free", 86400, "opencode-go/beam-free"),
                 ("AI_APICallError: Free usage limit reached", "opencode/flint-40b-free", 900, "zen-free"),
                 ("GoUsageLimitError", MIMO, 900, "go-paid"),
                 ("GoUsageLimitError", "opencode-go/beam-free", 900, "opencode-go/beam-free"),
                 ("429 Too Many Requests", "opencode/flint-40b-free", 900, "opencode/flint-40b-free"),
                 ("Unexpected server error", "opencode/flint-40b-free", 1800, "opencode/flint-40b-free"))
        for text, model, seconds, key in cases:
            with self.subTest(text=text, model=model):
                model_pick.pause_file(self.home).unlink(missing_ok=True)
                model_pick.failed(self.home, self.now, model, text)
                held = json.loads(model_pick.pause_file(self.home).read_text())
                self.assertEqual(held[key]["until"], NOW + seconds)

    def test_a_pause_never_shortens_a_longer_one(self):
        model_pick.pause(self.home, "m", NOW + 900, "a", NOW)
        model_pick.pause(self.home, "m", NOW + 60, "b", NOW)
        self.assertEqual(json.loads(model_pick.pause_file(self.home).read_text())["m"]["until"], NOW + 900)

    def test_an_unreadable_list_uses_the_last_good_one_then_stops(self):
        self.assertEqual(self.pick().model, "opencode-go/beam-free")
        self.provider.down = {GO_LIST, ZEN_LIST, STATUS}
        self.later()
        self.assertEqual(self.pick().model, "opencode-go/beam-free")
        self.later(model_pick.LIST_STALE + 1)
        got = self.pick()
        self.assertIsNone(got.model)
        self.assertEqual(got.until, self.now + model_pick.RETRY_NOTHING)

    def test_no_probe_answer_pauses_that_model_briefly(self):
        self.provider.answers["beam-free"] = None
        self.assertEqual(self.pick().model, "opencode-go/iceman-contributor-free")

    def test_a_refused_probe_of_a_cli_only_model_still_runs(self):
        self.provider.answers["glm-5.3-flash-free"] = (403, {}, b'{"error":"FreeTierError"}')
        self.assertEqual(self.pick("zen").model, "opencode/glm-5.3-flash-free")

    def test_status_says_when_nothing_can_run_without_asking_the_provider(self):
        self.assertIsNone(model_pick.status("zen", self.home, self.now))
        self.provider.answers["glm-5.3-flash-free"] = (
            429, {"Retry-After": "3000"}, b'{"error":{"type":"FreeUsageLimitError"}}')
        self.pick("zen")
        self.provider.probed.clear()
        self.assertEqual(model_pick.status("zen", self.home, self.now)[0], NOW + 3000)
        self.assertIsNone(model_pick.status("zen", self.home, self.now + 3001))
        self.assertEqual(self.provider.probed, [])

    def test_the_key_never_reaches_a_note_or_a_state_file(self):
        self.provider.answers["beam-free"] = (429, {}, b"{}")
        got = self.pick()
        self.assertNotIn("fake-key", got.note)
        for path in (self.home / "state" / "usage").iterdir():
            self.assertNotIn("fake-key", path.read_text())


if __name__ == "__main__":
    unittest.main()
