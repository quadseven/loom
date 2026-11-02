"""bus/model_pick.py: each Go or Zen run picks a live free model first, and a
paid model only under the budget."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from bus import model_pick, tickets

NOW = 2_000_000_000.0
GO_LIST = "https://go.test/models"
ZEN_LIST = "https://zen.test/models"
STATUS = "https://status.test/api.json"
GO_PROBE = "https://go.test/chat"
GO_USAGE = "https://go.test/usage"
ZEN_PROBE = "https://zen.test/chat"
ENV = {"LOOM_OPENCODE_GO_MODELS_URL": GO_LIST, "LOOM_ZEN_MODELS_URL": ZEN_LIST,
       "LOOM_MODELS_STATUS_URL": STATUS, "LOOM_OPENCODE_GO_PROBE_URL": GO_PROBE,
       "LOOM_OPENCODE_GO_PROBE_HEADER": "x-opencode-session", "LOOM_ZEN_PROBE_URL": ZEN_PROBE, "LOOM_OPENCODE_GO_USAGE_URL": GO_USAGE}
MIMO = "opencode-go/mimo-v2.6-flash"
MUSE = "opencode-go/muse-spark-1.3-contributor"
STRONG = model_pick.GO_STRONG[0]


def stamp(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def usage_doc(now, rolling=1, weekly=1, monthly=1, status="ok"):
    return json.dumps({"usage": {
        "rolling": {"status": status, "percent": rolling, "resetsAt": stamp(now + 3600)},
        "weekly": {"status": status, "percent": weekly, "resetsAt": stamp(now + 3 * 86400)},
        "monthly": {"status": status, "percent": monthly, "resetsAt": stamp(now + 15 * 86400)}}}).encode()


def listing(*ids):
    return json.dumps({"data": [{"id": i} for i in ids]}).encode()


class Provider:
    """The provider side: the lists and a probe answer per model."""

    def __init__(self):
        self.go = ["beam-free", "iceman-contributor-free", "mimo-v2.6-flash",
                   "muse-spark-1.3-contributor", "kimi-k3", "deepseek-v4.1-flash"]
        self.go += [m.split("/", 1)[1] for m in model_pick.GO_STRONG]
        self.usage = None
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
        if url == GO_USAGE:
            return self.usage if isinstance(self.usage, tuple) else (200, {}, usage_doc(NOW, **(self.usage or {})))
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
        self.brief_demoted = "none"

    def pick(self, lane="opencode-go", private=False, tier=model_pick.TIER_EASY):
        return model_pick.pick(lane, "slot", private, "fake-key", self.home, self.now,
                               ENV, self.provider, tier=tier, brief_demoted=self.brief_demoted)

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

    def test_only_listed_paid_models_can_ever_run(self):
        self.assertLessEqual(set(model_pick.GO_PAID), set(tickets.OPENCODE_GO_MODELS))
        self.assertLessEqual(set(model_pick.GO_STRONG), set(model_pick.GO_PAID))
        self.assertNotIn("opencode-go/kimi-k3", model_pick.GO_PAID)
        self.assertNotIn("opencode-go/deepseek-v4.1-flash", model_pick.GO_PAID)
        for model, limits in model_pick.GO_PAID.items():
            self.assertTrue(model in model_pick.GO_STRONG or limits[0] >= 10_000, model)

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
        self.assertLessEqual(spent, limit * model_pick.MODEL_SHARE)
        self.assertGreater(runs, 0)
        self.assertEqual(self.pick(private=True).model, None)

    def test_a_verify_run_never_gets_a_paid_model(self):
        self.free_models_down()
        self.assertIsNone(self.pick(tier=model_pick.TIER_NONE).model)
        self.assertFalse(model_pick.budget_file(self.home).exists())

    def test_a_medium_or_hard_ticket_starts_on_the_strongest_model(self):
        got = self.pick(tier=model_pick.TIER_STRONG)
        self.assertEqual((got.model, got.kind), (STRONG, "paid"))
        self.assertIn("strong tier", got.note)

    def test_an_easy_ticket_stays_free_first(self):
        self.assertEqual(self.pick(tier=model_pick.TIER_EASY).kind, "free")

    def test_beam_takes_easy_tickets_only(self):
        self.provider.usage = {"rolling": 95}
        got = self.pick(tier=model_pick.TIER_STRONG)
        self.assertEqual(got.model, "opencode-go/iceman-contributor-free")
        self.assertEqual(self.pick(tier=model_pick.TIER_EASY).model, "opencode-go/beam-free")

    def test_a_go_limit_falls_back_to_free_and_never_fails_a_ticket(self):
        self.provider.answers[STRONG.split("/", 1)[1]] = (
            429, {"Retry-After": "7200"}, b'{"error":{"type":"GoUsageLimitError"}}')
        got = self.pick(tier=model_pick.TIER_STRONG)
        self.assertEqual((got.model, got.kind), ("opencode-go/iceman-contributor-free", "free"))

    def test_the_strong_tier_stops_at_each_usage_stop(self):
        for usage, reset in (({"rolling": 60}, NOW + 3600), ({"weekly": 80}, NOW + 3 * 86400),
                             ({"monthly": 90}, NOW + 15 * 86400)):
            with self.subTest(usage=usage):
                self.provider.usage = usage
                got = self.pick(private=True, tier=model_pick.TIER_STRONG)
                self.assertIsNone(got.model)
                self.assertEqual(got.until, reset)
                self.assertIn("is at its", got.note)

    def test_spending_is_paced_evenly_to_the_renewal(self):
        monthly = NOW + 15 * 86400
        usage = {"rolling": (1.0, NOW + 3600), "weekly": (1.0, NOW + 3 * 86400)}
        gone = 1 - 15 / 30.4
        allowed = model_pick.USAGE_STOP["monthly"] * gone + model_pick.PACE_SLACK
        self.assertIsNone(model_pick.plan_stop(dict(usage, monthly=(allowed - 1, monthly)), NOW))
        stop = model_pick.plan_stop(dict(usage, monthly=(allowed + 1, monthly)), NOW)
        self.assertIn("ahead of its", stop[1])

    def test_usage_that_cannot_be_read_fails_closed(self):
        broken = ((500, {}, b"down"), (200, {}, b"not json"), (200, {}, b'{"usage":{}}'),
                  (200, {}, json.dumps({"usage": {"rolling": {"percent": "x"}}}).encode()))
        for answer in broken:
            with self.subTest(answer=answer[2]):
                self.provider.usage = answer
                got = self.pick(private=True, tier=model_pick.TIER_STRONG)
                self.assertIsNone(got.model)
                self.assertIn("usage could not be read", got.note)
        self.assertFalse(model_pick.budget_file(self.home).exists())

    def test_unreadable_usage_still_runs_free_models(self):
        self.provider.usage = (500, {}, b"down")
        self.assertEqual(self.pick(tier=model_pick.TIER_STRONG).model,
                         "opencode-go/iceman-contributor-free")

    def test_a_low_limit_model_holds_less_and_stops_at_its_own_share(self):
        limit = min(model_pick.GO_PAID.values())
        model = next(m for m, v in model_pick.GO_PAID.items() if v == limit)
        self.assertLessEqual(model_pick.run_reserve(model), limit[0] / 4)
        model_pick._spend(self.home, self.now - 60, model, int(limit[0] * model_pick.MODEL_SHARE))
        self.assertFalse(model_pick._has_room(self.home, self.now, model))
        self.assertTrue(model_pick._has_room(self.home, self.now + 5 * 3600 + 1, model))

    def test_a_demoted_model_is_skipped_and_its_slot_runs_another(self):
        scorecard = self.home / "state" / "station" / "scorecard.json"
        scorecard.parent.mkdir(parents=True)
        scorecard.write_text(json.dumps({"demoted": {"model:opencode-go/beam-free": {"why": "x"}}}))
        self.assertEqual(self.pick().model, "opencode-go/iceman-contributor-free")

    def scorecard_file(self):
        path = self.home / "state" / "station" / "scorecard.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def test_an_unreadable_scorecard_leaves_only_free_models(self):
        self.scorecard_file().write_text("{broken")
        got = self.pick(tier=model_pick.TIER_STRONG)
        self.assertEqual(got.kind, "free")
        self.assertIn("demotions could not be read", got.note)
        self.assertFalse(model_pick.budget_file(self.home).exists())

    def test_a_scorecard_with_a_broken_demoted_field_counts_as_unreadable(self):
        self.scorecard_file().write_text('{"demoted": []}')
        self.assertEqual(self.pick(tier=model_pick.TIER_STRONG).kind, "free")

    def test_a_missing_scorecard_uses_the_brief_line(self):
        self.brief_demoted = "%s opencode-go/beam-free" % STRONG
        got = self.pick(tier=model_pick.TIER_STRONG)
        self.assertEqual(got.model, model_pick.GO_STRONG[1])
        self.assertEqual(self.pick(tier=model_pick.TIER_EASY).model, "opencode-go/iceman-contributor-free")

    def test_no_scorecard_and_no_brief_line_leaves_only_free_models(self):
        self.brief_demoted = ""
        self.assertEqual(self.pick(tier=model_pick.TIER_STRONG).kind, "free")

    def test_a_brief_line_saying_unreadable_leaves_only_free_models(self):
        self.brief_demoted = "unreadable"
        self.assertEqual(self.pick(tier=model_pick.TIER_STRONG).kind, "free")

    def test_a_private_repo_never_runs_muse(self):
        for model in model_pick.GO_PAID:
            if model != MUSE:
                self.provider.go.remove(model.split("/", 1)[1])
        self.free_models_down()
        self.assertIsNone(self.pick(private=True).model)
        self.later(90001)
        self.assertEqual(self.pick(private=False).model, MUSE)

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

    def test_an_unrecorded_spend_stops_paid_models_for_an_hour(self):
        self.free_models_down()
        self.later(1)
        model_pick.usage_dir(self.home).mkdir(parents=True, exist_ok=True)
        model_pick.unsettled_file(self.home).write_text("%d\n" % self.now)
        self.assertIsNone(self.pick(private=True).model)
        self.now += model_pick.PAUSE_UNSETTLED + 1
        self.free_models_down()
        self.assertEqual(self.pick(private=True).model, MIMO)

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

    def test_a_list_url_that_is_not_http_opens_nothing(self):
        env = dict(ENV, LOOM_MODELS_STATUS_URL="file:///etc/hosts")
        got = model_pick.pick("zen", "slot", False, "", self.home, self.now, env, self.provider,
                              brief_demoted="none")
        self.assertIsNone(got.model)

    def test_the_key_never_reaches_a_note_or_a_state_file(self):
        self.provider.answers["beam-free"] = (429, {}, b"{}")
        got = self.pick()
        self.assertNotIn("fake-key", got.note)
        for path in (self.home / "state" / "usage").iterdir():
            self.assertNotIn("fake-key", path.read_text())


if __name__ == "__main__":
    unittest.main()
