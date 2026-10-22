"""bus/criteria_gate.py: a ticket closes only when every criterion has evidence.

The fixtures are acceptance criteria of tickets an audit of 2026-10-22 found
closed as completed with their live criteria never checked, with names made
generic: a release roller "enabled" while it stayed paused, a cost run-rate
"recorded" by a runbook that says the run never happened, a model verdict
from a simulated run, an RBAC check against a service account that does not
exist, a gateway hint nobody tried on a long thread, a connector whose round
trip was never run, a service that must survive a reboot.
"""
from __future__ import annotations

import json
import os
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bus import criteria_gate, factory, station  # noqa: E402
from bus.github import GitHub  # noqa: E402
import test_bus_factory as tbs_factory  # noqa: E402
import test_bus_station as tbs  # noqa: E402

REPO = tbs.REPO

# Criteria of the audit's wrongly closed tickets: each needed a live system.
AUDIT_LIVE = (
    "A queued release rolls within the window only when the stated 60-minute "
    "interval gates pass.",
    "The first automatic roll's gate readings at merge time, running image digest, and "
    "verification evidence are recorded in the ticket.",
    "Calculate the seven-day run-rate and record the measurement period, result, and whether "
    "it is below $3/month in the issue.",
    "Retrieve daily `EstimatedCharges` datapoints for a seven-day window using a read-only "
    "method.",
    "The same judge dataset is scored on the incumbent and a second servable model through a "
    "non-vendor path.",
    "Verify the tenant's service account is denied get, list, and create access in the runner "
    "namespaces.",
    "Verify an over-budget pod is rejected with a quota error.",
    "Verify a long chat thread that previously truncated completes using the per-request hint.",
    "The pod obtains the required credentials, sends logs and metrics, and has working probes.",
    "The connector uses the gateway for inference and completes a verified request round trip.",
    "The autoconnect service starts the VPN with the required flags and succeeds across reboot.",
    "Live: the daily line shows it",
)
AUDIT_CODE = (
    "Requests without the hint retain the existing behavior.",
    "A test covers the history transition and fails on main.",
    "Provide a CI-runnable guard for these isolation checks.",
    "Any difference between the runs other than the model is explicitly documented.",
)
AUDIT_OPERATOR = (
    "Verified on a handset that an appliance call stops ringing.",
    "The operator signs off on the decision record before it is marked Accepted.",
)


def body(*criteria, ticked=False):
    box = "- [x] " if ticked else "- [ ] "
    return ("Difficulty: medium\n\n## Why\nx\n\n## Acceptance criteria\n"
            + "".join(box + c + "\n" for c in criteria))


class Kinds(unittest.TestCase):
    def test_each_audit_criterion_gets_its_kind(self):
        for kind, texts in ((criteria_gate.LIVE, AUDIT_LIVE), (criteria_gate.CODE, AUDIT_CODE),
                            (criteria_gate.OPERATOR, AUDIT_OPERATOR)):
            for text in texts:
                with self.subTest(text=text):
                    self.assertEqual(criteria_gate.kind(text), kind)

    def test_a_tick_is_nobodys_evidence(self):
        ticked = body(AUDIT_LIVE[0], AUDIT_CODE[0], AUDIT_OPERATOR[0], ticked=True)
        self.assertEqual([k for k, _ in criteria_gate.unverified(ticked, [])],
                         [criteria_gate.LIVE, criteria_gate.OPERATOR])
        # A passing station check clears a live criterion, never an operator one.
        passed = [criteria_gate.key(AUDIT_LIVE[0]), criteria_gate.key(AUDIT_OPERATOR[0])]
        self.assertEqual([k for k, _ in criteria_gate.unverified(ticked, passed)],
                         [criteria_gate.OPERATOR])

    def test_the_lanes_ticks_of_live_criteria_are_found(self):
        ticket = body(AUDIT_LIVE[6], AUDIT_CODE[1])
        pull = body(AUDIT_LIVE[6], AUDIT_CODE[1], ticked=True)
        self.assertEqual(criteria_gate.lane_ticks(ticket, pull), [AUDIT_LIVE[6]])


class Checks(unittest.TestCase):
    def test_runnable_checks_and_what_they_expect(self):
        for text, want in (
                ("Live: `https://status.example.com/healthz` serves `ok`",
                 ("url", "https://status.example.com/healthz", ("ok",))),
                ("Live: https://status.example.com/v1 answers \"ready\".",
                 ("url", "https://status.example.com/v1", ("ready",))),
                ("`gh api repos/example-owner/example-repo/actions/variables/ROLLER` shows "
                 "`\"false\"`", ("gh", "/repos/example-owner/example-repo/actions/variables/ROLLER",
                                 ('"false"',))),
                ("`kubectl get cronjob roller -n dev -o jsonpath={.spec.suspend}` prints `false`",
                 ("kubectl", "kubectl get cronjob roller -n dev -o jsonpath={.spec.suspend}",
                  ("false",)))):
            with self.subTest(text=text):
                self.assertEqual(tuple(criteria_gate.check_of(text)), want)

    def test_a_write_a_secret_or_a_shell_is_never_run(self):
        for text in ("`gh api -X DELETE repos/o/r/hooks/1` succeeds",
                     "`gh api repos/o/r/hooks -f url=x` succeeds",
                     "`kubectl get secret app -o yaml` shows the key",
                     "`kubectl exec deploy/app -- sh` works",
                     "`kubectl delete pod app` restarts it",
                     "`kubectl get pods --kubeconfig /tmp/x` lists it",
                     "`curl http://192.0.2.1/` answers",
                     "the roller is enabled"):
            with self.subTest(text=text):
                self.assertIsNone(criteria_gate.check_of(text))

    def test_a_check_passes_only_with_every_expected_string(self):
        check = criteria_gate.check_of("Live: `https://status.example.com/` serves `ok` and `v2`")
        self.assertEqual(criteria_gate.run(check, None, fetch=lambda u, t: (200, "ok v2", "")),
                         (True, "HTTP 200, found `ok`, `v2`"))
        self.assertEqual(criteria_gate.run(check, None, fetch=lambda u, t: (200, "ok", "")),
                         (False, "HTTP 200, missing `v2`"))

        def broken(url, timeout):
            raise OSError("no route to 192.0.2.7")
        self.assertEqual(criteria_gate.run(check, None, fetch=broken),
                         (False, "the check could not run (OSError)"))

        # Security review: a redirect is not followed, so a 3xx (a login or
        # error page behind it) never passes unless the criterion names it.
        for status in (301, 302):
            with self.subTest(status=status):
                self.assertEqual(
                    criteria_gate.run(check, None, fetch=lambda u, t: (
                        status, "ok v2", "https://login.example.com/")),
                    (False, "HTTP %d (redirect not followed)" % status))
        named = criteria_gate.check_of("Live: `https://old.example.com/` answers `301` to "
                                       "`https://new.example.com/`")
        self.assertEqual(criteria_gate.run(named, None, fetch=lambda u, t: (
            301, "", "https://new.example.com/")),
            (True, "HTTP 301, found `301`, `https://new.example.com/`"))
        self.assertFalse(criteria_gate.run(named, None, fetch=lambda u, t: (
            302, "", "https://new.example.com/"))[0])

        def garbled(url, timeout):
            raise criteria_gate.http.client.BadStatusLine("x")
        self.assertEqual(criteria_gate.run(check, None, fetch=garbled),
                         (False, "the check could not run (BadStatusLine)"))
        gh = SimpleNamespace(rest=lambda m, p: SimpleNamespace(data={"value": "false"}))
        gh_check = criteria_gate.check_of("`gh api repos/o/r/actions/variables/X` shows `false`")
        self.assertEqual(criteria_gate.run(gh_check, gh), (True, "GitHub answered, found `false`"))
        kube = criteria_gate.check_of("`kubectl rollout status deploy/app -n dev` says "
                                      "`successfully rolled out`")
        ran = []

        def runner(argv, **kw):
            ran.append(argv)
            return SimpleNamespace(returncode=1, stdout="Waiting for rollout")
        self.assertEqual(criteria_gate.run(kube, None, runner=runner),
                         (False, "exit 1, missing `successfully rolled out`"))
        self.assertEqual(ran, [["kubectl", "rollout", "status", "deploy/app", "-n", "dev"]])


class FactoryTicks(unittest.TestCase):
    def test_a_lane_is_not_asked_to_tick_a_live_or_operator_criterion(self):
        ticket = body(AUDIT_LIVE[6], AUDIT_OPERATOR[1], AUDIT_CODE[1])
        self.assertEqual(factory.missing_criteria(ticket, body(AUDIT_CODE[1])), [AUDIT_CODE[1]])


class FactoryLaneTicks(tbs_factory.Base):
    def test_a_lane_that_ticks_a_live_criterion_is_sent_back(self):
        # 2026-10-22 audit: lane bodies ticked live boxes nothing had checked.
        ticket = body(AUDIT_LIVE[6], AUDIT_CODE[1])
        self.gh.ticket(5, "pr", body=ticket)
        self.gh.pull(50, 5, body=body(AUDIT_LIVE[6], AUDIT_CODE[1], ticked=True),
                     files=("bus/a.py", "tests/test_a.py"))
        f = self.go()
        self.assertEqual(self.merged, [])
        self.assertIn(factory.TICKED_HEADING + "\n\n- " + AUDIT_LIVE[6], f.notes)
        self.assertIn("Untick each one", f.notes)
        self.setUp()
        self.gh.ticket(5, "pr", body=ticket)
        self.gh.pull(50, 5, body=body(AUDIT_CODE[1], ticked=True) + "- [ ] " + AUDIT_LIVE[6] + "\n",
                     files=("bus/a.py", "tests/test_a.py"))
        self.go()
        self.assertEqual(len(self.merged), 1)


class Fetch(unittest.TestCase):
    def test_only_https_is_fetched(self):
        for url in ("file:///etc/passwd", "http://192.0.2.1/", "ftp://example.com/x"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    criteria_gate._fetch(url, 1)


class DoneClose(tbs.Base):
    """The station's done close keeps a ticket with unverified criteria open."""

    def close(self, *bodies, fetch=None):
        self.fake.private = False
        for n, text in enumerate(bodies, 1):
            self.fake.add(n, lane="codex", body=text, state="shipped")
        st = station.Station(GitHub("token", transport=self.fake), self.settings)
        with mock.patch.object(criteria_gate, "_fetch", fetch or (lambda u, t: (200, "ok", ""))):
            st.close_done_tickets()
        return st

    def asks(self, number):
        return [c["body"] for c in self.fake.comments[number] if "Kind: decision" in c["body"]]

    def test_the_audits_closed_tickets_stay_open_with_one_ask(self):
        # Each closed as completed by the done close with live criteria unmet.
        roller = body(AUDIT_CODE[1], AUDIT_LIVE[0], AUDIT_LIVE[1])
        cost = body(AUDIT_LIVE[3], AUDIT_LIVE[2])
        rbac = body(AUDIT_LIVE[5], AUDIT_LIVE[6], AUDIT_CODE[2])
        self.close(roller, cost, rbac)
        for n in (1, 2, 3):
            self.assertEqual(self.fake.issues[n]["state"], "open")
            self.assertIn("loom:human", self.fake.names(n))
            self.assertEqual(len(self.asks(n)), 1)
        self.assertIn("no check Loom can run", self.asks(1)[0])
        # A second pass asks nothing more.
        st = station.Station(GitHub("token", transport=self.fake), self.settings)
        st.close_done_tickets()
        self.assertEqual(len(self.asks(1)), 1)

    def test_a_passing_station_check_closes_and_a_failing_one_asks(self):
        # Not a Live line: a Live line with strings waits for a verify run.
        live = "Verify `https://status.example.com/healthz` serves `ok` after the deploy."
        self.close(body(AUDIT_CODE[1], live))
        self.assertEqual(self.fake.issues[1]["state"], "closed")
        state = json.load(open(os.path.join(self.home, "state", "station",
                                            station.LIVE_CHECKS_FILE)))
        self.assertEqual(state["%s#1" % REPO][criteria_gate.key(live)]["result"], "pass")
        self.setUp()
        self.close(body(AUDIT_CODE[1], live), fetch=lambda u, t: (503, "down", ""))
        self.assertEqual(self.fake.issues[1]["state"], "open")
        self.assertIn("its check failed: HTTP 503, missing `ok`", self.asks(1)[0])

    def test_an_operator_criterion_never_closes_on_its_own(self):
        self.close(body(AUDIT_CODE[1], AUDIT_OPERATOR[0]))
        self.assertEqual(self.fake.issues[1]["state"], "open")
        self.assertIn("an operator action", self.asks(1)[0])

    def test_asks_are_capped_an_hour(self):
        self.close(*[body(AUDIT_LIVE[n]) for n in range(station.LIVE_ASKS_PER_HOUR + 2)])
        asked = [n for n in range(1, station.LIVE_ASKS_PER_HOUR + 3) if self.asks(n)]
        self.assertEqual(len(asked), station.LIVE_ASKS_PER_HOUR)


class LaneProof(tbs.Base):
    """A verify lane's proof may not pass a live criterion the station never checked."""

    good = tbs.VerifyResultTest.good

    def setUp(self):
        super().setUp()
        self.fake.add(5, lane="codex", body=tbs.SHIPPED_BODY, state="shipped",
                      events=tbs.shipped_at(30))

    def test_a_lane_proof_passing_an_unchecked_live_criterion_is_refused(self):
        rbac = AUDIT_LIVE[5]
        self.fake.issues[5]["body"] = body("A test passes", rbac,
                                           "Live: prints `daily status ready`")
        criteria = self.good()["criteria"]
        criteria.insert(1, {"criterion": rbac, "result": "pass",
                            "evidence": "denied for the account, seen at 2026-10-20T10:00Z"})
        st = station.Station(GitHub("token", transport=self.fake), self.settings, now=time.time())
        with mock.patch("bus.station._say") as say:
            st._apply_proof("codex", REPO, 5, self.good(criteria=criteria), "model-x")
        self.assertNotIn("loom:verified", self.fake.names(5))
        self.assertEqual(self.fake.issues[5]["state"], "open")
        said = " ".join(str(c) for c in say.call_args_list)
        self.assertIn("a verify lane may not pass a live or operator criterion", said)

    def test_a_live_line_the_station_logs_found_still_verifies(self):
        st = station.Station(GitHub("token", transport=self.fake), self.settings, now=time.time())
        st._apply_proof("codex", REPO, 5, self.good(), "model-x")
        self.assertIn("loom:verified", self.fake.names(5))


if __name__ == "__main__":
    unittest.main()
