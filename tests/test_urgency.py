from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from bus import urgency  # noqa: E402

NOW = 1_900_000_000.0


def reply(score, person=0.0, dup=0.0, exposure=0.0, tokens=500):
    return {"model": "jev-latest", "usage": {"input_tokens": tokens, "output_tokens": 4},
            "answers": {
                "urgency": {"type": "score", "score": score, "confidence": 0.5,
                            "legend": {}, "probabilities": {}},
                "needs_person": {"type": "noul", "noul": person},
                "duplicate": {"type": "noul", "noul": dup},
                "public_exposure": {"type": "noul", "noul": exposure}}}


class Stub:
    """A client that records each request and answers from a queue."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def state(title="Grant the deploy role", body="The lane cannot deploy."):
    return urgency.build_state("needs-operator ask", "example-owner/example-repo", title,
                               body, ["needs-operator"], 5, "lane", None, ["Other ask"])


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "urgency.json")
        self.history = os.path.join(self.dir, "urgency.jsonl")

    def scorer(self, stub, mode=urgency.ACT, cap=urgency.DAILY_TOKENS, now=NOW):
        cfg = urgency.Config(mode=mode, daily_tokens=cap)
        return urgency.Scorer(cfg, self.path, self.history, client=lambda: stub, now=now)


class RoutingTest(Base):
    def test_four_questions_in_one_request_and_the_routing_table(self):
        cases = [
            ("exposure", reply(0.5, exposure=0.7), urgency.URGENT),
            ("blocks with a person", reply(3.2, person=0.6), urgency.URGENT),
            ("blocks, no person", reply(3.5, person=0.5), urgency.DIGEST),
            ("today", reply(2.0), urgency.DIGEST),
            ("digest floor", reply(1.5), urgency.DIGEST),
            ("can wait", reply(1.49), urgency.STATUS),
            ("repeat", reply(3.9, person=0.9, dup=0.8), urgency.FOLD),
        ]
        for name, answer, want in cases:
            with self.subTest(name):
                stub = Stub(answer)
                got = self.scorer(stub).decide("r#%s" % name, state(title=name),
                                               ["needs-operator"])
                self.assertEqual(len(stub.requests), 1)
                sent = stub.requests[0]
                self.assertEqual(set(sent["questions"]),
                                 {"urgency", "needs_person", "duplicate", "public_exposure"})
                self.assertEqual(sent["questions"]["urgency"]["type"], "score")
                self.assertEqual(len(sent["questions"]["urgency"]["criteria"]), 5)
                self.assertEqual(sent["state"]["title"], name)
                self.assertEqual(got.route, want)
                self.assertEqual(got.scored, want)

    def test_the_body_is_cut_to_the_excerpt(self):
        s = state(body="x" * 9000)
        self.assertLessEqual(len(s["body"]), urgency.BODY_MAX + len("\n[cut]"))
        self.assertEqual(s["open_asks"], ["Other ask"])

    def test_thresholds_are_settings(self):
        cfg = urgency.Config.from_env({"LOOM_URGENCY_DIGEST": "2.5",
                                       "LOOM_URGENCY_MODE": "act",
                                       "LOOM_URGENCY_DAILY_TOKENS": "1000"})
        self.assertEqual((cfg.mode, cfg.daily_tokens, cfg.thresholds.digest),
                         (urgency.ACT, 1000, 2.5))
        self.assertEqual(urgency.route(urgency.Answer(2.0, 0, 0, 0), cfg.thresholds),
                         urgency.STATUS)
        self.assertEqual(urgency.Config.from_env({}).mode, urgency.SHADOW)
        self.assertEqual(urgency.Config.from_env({"LOOM_URGENCY_MODE": "loud"}).mode,
                         urgency.SHADOW)

    def test_one_request_per_revision(self):
        stub = Stub(reply(2.0), reply(4.0, person=1.0))
        first = self.scorer(stub)
        first.decide("r#1", state(), ["needs-operator"])
        first.save()
        sc = self.scorer(stub, now=NOW + 60)
        self.assertEqual(sc.decide("r#1", state(), ["needs-operator"]).route, urgency.DIGEST)
        self.assertEqual(len(stub.requests), 1)
        changed = state(body="Now the site is down.")
        self.assertEqual(sc.decide("r#1", changed, ["needs-operator"]).route, urgency.URGENT)
        self.assertEqual(len(stub.requests), 2)


class ShadowTest(Base):
    def test_shadow_routes_by_the_labels_and_records_the_score(self):
        stub = Stub(reply(4.0, person=0.9), reply(0.2))
        sc = self.scorer(stub, mode=urgency.SHADOW)
        loud = sc.decide("r#1", state(), ["needs-operator"])
        quiet = sc.decide("r#2", state(title="Old"), ["needs-operator", "loom:urgent"])
        sc.save()
        self.assertEqual((loud.route, loud.scored), (urgency.DIGEST, urgency.URGENT))
        self.assertEqual((quiet.route, quiet.scored), (urgency.URGENT, urgency.STATUS))
        with open(self.path) as fh:
            scores = json.load(fh)["scores"]
        self.assertEqual(scores["r#1"]["route"], urgency.URGENT)
        self.assertEqual(scores["r#1"]["today_route"], urgency.DIGEST)
        self.assertEqual(scores["r#1"]["urgency"], 4.0)
        with open(self.history) as fh:
            lines = [json.loads(ln) for ln in fh]
        self.assertEqual([(ln["key"], ln["mode"]) for ln in lines],
                         [("r#1", "shadow"), ("r#2", "shadow")])

    def test_off_sends_nothing(self):
        stub = Stub()
        got = self.scorer(stub, mode=urgency.OFF).decide("r#1", state(), ["needs-operator"])
        self.assertEqual(got, urgency.Decision(urgency.DIGEST, None, None, False))
        self.assertEqual(stub.requests, [])


class FallbackTest(Base):
    def test_past_the_daily_cap_no_request_is_sent(self):
        stub = Stub(reply(4.0, person=1.0, tokens=900))
        sc = self.scorer(stub, cap=1000)
        self.assertEqual(sc.decide("r#1", state(), ["needs-operator"]).route, urgency.URGENT)
        got = sc.decide("r#2", state(title="Next"), ["needs-operator"])
        self.assertEqual(len(stub.requests), 1)
        self.assertEqual((got.route, got.scored, got.fallback), (urgency.DIGEST, None, True))
        self.assertEqual(sc.fallbacks, 1)
        # The same capped ask counts once a day, not once a pass.
        sc.decide("r#2", state(title="Next"), ["needs-operator"])
        self.assertEqual(sc.fallbacks, 1)
        sc.save()
        # A new UTC day resets the count and the ask is scored.
        stub.answers.append(reply(0.1))
        tomorrow = self.scorer(stub, cap=1000, now=NOW + 86400)
        self.assertEqual(tomorrow.decide("r#2", state(title="Next"), ["needs-operator"]).route,
                         urgency.STATUS)

    def test_a_429_or_5xx_falls_back_to_the_labels_and_counts(self):
        for status in (429, 500, 503):
            with self.subTest(status):
                os.path.exists(self.path) and os.remove(self.path)
                stub = Stub(urgency.ApiError(status))
                sc = self.scorer(stub)
                got = sc.decide("r#1", state(), ["needs-operator", "loom:urgent"])
                self.assertEqual((got.route, got.scored, got.fallback),
                                 (urgency.URGENT, None, True))
                self.assertEqual(sc.fallbacks, 1)
                # Within the retry wait no request is sent again.
                sc.decide("r#1", state(), ["needs-operator", "loom:urgent"])
                self.assertEqual((len(stub.requests), sc.fallbacks), (1, 1))

    def test_an_unreadable_reply_falls_back(self):
        sc = self.scorer(Stub({"answers": {"urgency": {"score": "high"}}}))
        got = sc.decide("r#1", state(), ["needs-operator"])
        self.assertEqual((got.route, got.fallback), (urgency.DIGEST, True))
        self.assertEqual(sc.fallbacks, 1)

    def test_the_http_client_maps_errors_and_sends_the_key(self):
        seen = []

        def fail(req, timeout):
            seen.append(req)
            raise urllib.error.HTTPError(req.full_url, 429, "slow down", {}, io.BytesIO(b""))
        send = urgency.http_client("k-example", urlopen=fail)
        with self.assertRaises(urgency.ApiError) as err:
            send(urgency.payload(state()))
        self.assertEqual(err.exception.status, 429)
        self.assertEqual(seen[0].full_url, urgency.ENDPOINT)
        self.assertEqual(seen[0].get_header("Authorization"), "Bearer k-example")


if __name__ == "__main__":
    unittest.main()
