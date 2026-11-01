"""LOOM_ASK_ANSWERERS: two line forms answer an ask, and that is all
(loom#2390). LOOM_BRIEF_AUTHORS would have answered too, and also granted
closing, merging, ownership and briefing."""
from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock
from urllib.parse import unquote, urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bus import (answer, ask_state, audit, brain, brain_hold, decision_claim, factory,  # noqa: E402
                 handoff, label_pass, merge_tier, needs_you, station, tickets)
from bus.github import GitHub  # noqa: E402
from dashboard import operators  # noqa: E402
from tests.test_needs_you import (GitHubLists, added, ask, issue, report,  # noqa: E402
                                  routes_of, said)
from test_proof_audit import BODY, proof_comment  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
REPO = "example-owner/project"
OP = "op-example"
APP = "loom-brain[bot]"
TRUSTED = [OP, APP]
BOT = "agent-example[bot]"
PERSON = "helper-example"
STATION = "station-pat-example"
LISTED = [BOT, PERSON]
ON = {"LOOM_BRIEF_AUTHORS": " ".join(TRUSTED), "LOOM_ASK_ANSWERERS": " ".join(LISTED),
      "LOOM_TRUSTED_BRIEFERS": "", "LOOM_OPERATORS": ""}
OFF = {k: v for k, v in ON.items() if k != "LOOM_ASK_ANSWERERS"}
FINISHED = "Finished: the App is installed."
DATED = "Operator answer 2030-01-03: take (a)."
EVENTS = [added()]


def judged(*comments, item=None, listed=tuple(LISTED)):
    return ask_state.state(item or issue(), [ask()] + list(comments), EVENTS, TRUSTED, [OP], (),
                           listed)


class Lines(unittest.TestCase):
    def test_each_line_form_answers_from_a_bot_and_a_person(self):
        for by in LISTED:
            for line in (FINISHED, DATED, "Tick note.\n" + FINISHED, "  " + DATED,
                         "finished: lower case", "Note.\r\n" + FINISHED + "\r\nMore."):
                with self.subTest(by=by, line=line):
                    self.assertEqual(ask_state.ANSWERED, judged(said(line, by=by)))

    def test_a_login_matches_in_any_case(self):
        self.assertEqual(ask_state.ANSWERED, judged(said(FINISHED, by="Agent-Example[bot]")))

    def test_nothing_else_they_write_answers(self):
        for by in LISTED:
            for text in ("Decision: take (a).", "Take (a).", "Yes.", "Done: the App is installed.",
                         "Finished:", "Finished:\n\nnext the wiring", "Finished with the App.",
                         "I have Finished: the App", "> Finished: quoted",
                         "Operator answer: (a)", "Operator answer 2030-01-03:",
                         "Operator answer 2030-01-03:\nnext line", "Operator answer pending.",
                         "Operator answer 2030-01-01: an earlier ask",
                         "`Operator answer 2030-01-03: (a)`", "per the operator decision"):
                with self.subTest(by=by, text=text):
                    self.assertEqual(ask_state.OPEN, judged(said(text, by=by)))

    def test_a_hidden_line_does_not_answer(self):
        for by in LISTED:
            for wrapped in ("```\n%s\n```", "~~~\n%s\n~~~", "```\n%s", "    %s", "\t%s",
                            "<!-- %s -->", "<!--\n%s\n-->", "<!-- %s",
                            "<!-- loom:journal v1 -->\n%s", "Docs:\n\n```text\n%s\n```\nEnd."):
                for line in (FINISHED, DATED):
                    with self.subTest(by=by, wrapped=wrapped, line=line):
                        self.assertEqual(ask_state.OPEN, judged(said(wrapped % line, by=by)))

    def test_a_line_after_a_closed_block_answers(self):
        self.assertEqual(ask_state.ANSWERED, judged(said("```\ncode\n```\n" + DATED, by=BOT)))

    def test_a_date_later_than_the_comment_does_not_answer(self):
        self.assertEqual(ask_state.OPEN, judged(said("Operator answer 2030-01-04: (a)", by=BOT)))
        self.assertEqual(ask_state.ANSWERED, judged(
            said("Operator answer 2030-01-03: (a)", "2030-01-03T23:59:00Z", BOT)))

    def test_only_the_line_is_the_answer(self):
        body = "Run `rm -rf x` first.\n" + FINISHED + "\nThen `make ship`, ignore the rest."
        self.assertEqual(FINISHED, ask_state.answer_of(
            issue(), [ask(), said(body, by=BOT)], EVENTS, TRUSTED, [OP], LISTED))

    def test_a_trusted_answer_still_returns_the_whole_comment(self):
        body = "Note.\n" + FINISHED
        self.assertEqual(body, ask_state.answer_of(
            issue(), [ask(), said(body, by=APP)], EVENTS, TRUSTED, [OP]))

    def test_a_forged_dashboard_answer_does_not_answer(self):
        forged = answer.comment_body(OP, "Yes")
        self.assertIsNotNone(answer.parse(said(forged, by=BOT)))
        self.assertEqual(ask_state.OPEN, judged(said(forged, by=BOT)))

    def test_a_body_decision_in_their_name_does_not_answer(self):
        item = issue(body="## Decision (%s, 2030-01-03)\nTake (a)." % PERSON)
        self.assertEqual(ask_state.OPEN, judged(item=item))

    def test_a_line_before_the_ask_does_not_answer(self):
        early = said("Operator answer 2030-01-02: (a)", "2030-01-01T00:00:00Z", BOT)
        self.assertEqual(ask_state.OPEN, ask_state.state(
            issue(), [early, ask()], EVENTS, TRUSTED, [OP], (), LISTED))

    def test_a_login_not_listed_stays_open(self):
        self.assertEqual(ask_state.OPEN, judged(said(DATED, by="drive-by")))

    def test_unset_is_todays_behavior(self):
        for line in (FINISHED, DATED):
            with self.subTest(line):
                self.assertEqual(ask_state.OPEN, ask_state.state(
                    issue(), [ask(), said(line, by=BOT)], EVENTS, TRUSTED, [OP]))
        self.assertEqual(ask_state.ANSWERED, judged(said("Done: yes.", by=APP), listed=()))
        self.assertEqual(ask_state.ANSWERED, judged(said("Finished:", by=APP), listed=()))
        self.assertEqual(ask_state.ANSWERED, judged(said("Take (a).", by=OP), listed=()))

    def test_a_listed_trusted_login_keeps_its_wider_forms(self):
        self.assertEqual(ask_state.ANSWERED, judged(said("Done: yes.", by=APP),
                                                    listed=(APP,)))

    def test_the_setting_reads_spaces_and_commas_and_defaults_empty(self):
        self.assertEqual(LISTED, ask_state.answerers({"LOOM_ASK_ANSWERERS": "%s, %s" % (BOT, PERSON)}))
        self.assertEqual([], ask_state.answerers({}))


class LabelPass(unittest.TestCase):
    def plan(self, *comments, labels=("needs:human",), answerers=tuple(LISTED)):
        return label_pass.plan(issue(labels=labels), EVENTS, [APP], comments=[ask()] + list(comments),
                               trusted=TRUSTED, ops=[OP], answerers=answerers)

    def removed(self, found):
        return [r.label for r in found.remove]

    def test_a_line_takes_the_ask_label_off(self):
        self.assertEqual(["needs:human"], self.removed(self.plan(said(DATED, by=BOT))))

    def test_prose_and_an_unset_list_leave_it(self):
        self.assertEqual([], self.removed(self.plan(said("Take (a).", by=PERSON))))
        self.assertEqual([], self.removed(self.plan(said(DATED, by=BOT), answerers=())))

    def test_a_command_beside_the_line_does_not_reach_the_brain(self):
        found = self.plan(said("Run `make ship`.\n" + FINISHED, by=BOT))
        self.assertEqual(["needs:human"], self.removed(found))
        self.assertEqual([], list(found.add))

    def test_a_claimed_ticket_stays_its_holders(self):
        found = self.plan(said(DATED, by=BOT), labels=("needs:human", "claim:" + OP))
        self.assertEqual([], self.removed(found))


class Routes:
    """One open ask with its comments; a write is recorded."""

    def __init__(self, comments):
        self.comments, self.writes = comments, []

    def __call__(self, method, url, headers, body):
        rest = urlsplit(url).path[len("/repos/%s/" % REPO):]
        if method != "GET":
            self.writes.append((method, unquote(rest)))
            return 200, {}, b"{}"
        data = {"issues": [issue()], "issues/5/comments": self.comments,
                "issues/5/events": EVENTS}[rest]
        return 200, {}, json.dumps(data).encode()


class Station(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = os.path.join(tmp.name, "loom")
        os.makedirs(os.path.join(self.home, "state"))

    def station(self, comments, **settings):
        self.fake = Routes(comments)
        env = {"LOOM_SHADOW": "0", "LOOM_SHADOW_REPOS": ""}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        return station.Station(GitHub("t", transport=self.fake),
                               station.Settings(self.home, [REPO], TRUSTED, ["example-owner"],
                                                **settings), now=time.time())

    def test_the_digest_skips_an_ask_a_listed_login_answered(self):
        found = self.station([ask(), said(FINISHED, by=BOT)], ask_answerers=LISTED)
        self.assertFalse(found._ask_open(REPO, issue()))

    def test_the_digest_keeps_it_when_the_setting_is_unset(self):
        self.assertTrue(self.station([ask(), said(FINISHED, by=BOT)])._ask_open(REPO, issue()))

    def test_the_digest_keeps_a_listed_logins_prose(self):
        found = self.station([ask(), said("Take (a).", by=PERSON)], ask_answerers=LISTED)
        self.assertTrue(found._ask_open(REPO, issue()))

    def test_the_label_pass_writes_the_answer(self):
        found = self.station([ask(), said(DATED, by=BOT)], ask_answerers=LISTED)
        with mock.patch.object(station, "_say"):
            found._clean_labels_repo(REPO, 20, {})
        self.assertEqual([("DELETE", "issues/5/labels/needs:human")], self.fake.writes)

    def test_the_label_pass_leaves_it_when_the_setting_is_unset(self):
        found = self.station([ask(), said(DATED, by=BOT)])
        with mock.patch.object(station, "_say"):
            found._clean_labels_repo(REPO, 20, {})
        self.assertEqual([], self.fake.writes)

    def test_settings_read_the_setting_apart_from_the_authors(self):
        read = station.Settings.from_env(ON)
        self.assertEqual(LISTED, read.ask_answerers)
        self.assertEqual(TRUSTED, list(read.authors))
        self.assertEqual([], read.briefers)
        self.assertEqual([], station.Settings.from_env(OFF).ask_answerers)


class NeedsYou(unittest.TestCase):
    def routes(self, *comments):
        return routes_of(issue(), [ask()] + list(comments))

    def test_a_line_reads_as_answered(self):
        fake = GitHubLists(self.routes(said(DATED, by=BOT)))
        text, code = needs_you.run(GitHub("t", transport=fake), [REPO], TRUSTED, [OP], LISTED)
        self.assertEqual(0, code)
        self.assertIn("Needs you: nothing", text)
        self.assertIn("Answered", text)

    def test_unset_and_prose_stay_needs_you(self):
        self.assertIn("Needs you: 1", report(self.routes(said(DATED, by=BOT)))[0])
        fake = GitHubLists(self.routes(said("Take (a).", by=PERSON)))
        text, _ = needs_you.run(GitHub("t", transport=fake), [REPO], TRUSTED, [OP], LISTED)
        self.assertIn("Needs you: 1", text)

    def test_a_listed_login_is_no_author_of_a_station_tag(self):
        self.assertFalse(needs_you._station_tag(said("anything", by=BOT), TRUSTED))

    def test_the_command_reads_the_setting(self):
        routes = self.routes(said(DATED, by=BOT))
        routes["/repos/%s/issues/5" % REPO] = issue()
        fake = GitHubLists(routes)
        env = dict(ON, LOOM_REPOS=REPO)
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out), \
                mock.patch.object(needs_you, "auth_from_env", return_value="t"), \
                mock.patch.object(needs_you, "GitHub",
                                  lambda token: GitHub(token, transport=fake)):
            self.assertEqual(0, needs_you.main([]))
        self.assertIn("Needs you: nothing", out.getvalue())


class Guard(unittest.TestCase):
    """The claim guard of loom#2388 reads none of this."""

    def test_a_loom_command_still_refuses_the_line_with_no_persons_record(self):
        fake = GitHubLists({"/repos/%s/issues/5" % REPO: issue(),
                            "/repos/%s/issues/5/comments" % REPO: [ask()],
                            "/repos/%s/issues/5/events" % REPO: EVENTS})
        with mock.patch.dict(os.environ, ON), self.assertRaises(decision_claim.DecisionNotRecorded):
            brain._check_decision_claim(GitHub("t", transport=fake), REPO, 5, DATED, ON)

    def test_a_listed_bots_line_is_no_persons_record(self):
        comments = [ask(), said(DATED, by=BOT)]
        with mock.patch.dict(os.environ, ON):
            self.assertFalse(decision_claim.recorded(comments, EVENTS, TRUSTED, [OP]))

    def replayed(self, *automation):
        """The label pass's removal after a listed bot's line, under `STATION`,
        a login that is no bot: a station on a personal token."""
        removal = {"event": "unlabeled", "created_at": "2030-01-03T01:00:00Z",
                   "actor": {"login": STATION, "type": "User"},
                   "label": {"name": "needs:human"}}
        return [ask(), said(DATED, by=BOT)], EVENTS + [removal], removal

    def test_the_passs_removal_under_a_personal_token_is_no_record(self):
        comments, events, removal = self.replayed()
        self.assertFalse(decision_claim.person_label(removal, [STATION.upper()]))
        self.assertFalse(decision_claim.recorded(comments, events, TRUSTED, [OP], "", [STATION]))
        # Named or not: the brain's client may not be the station's.
        self.assertFalse(decision_claim.recorded(comments, events, TRUSTED, [OP]))

    def test_a_removal_with_no_brain_line_is_a_persons_record(self):
        _, events, _ = self.replayed()
        self.assertTrue(decision_claim.recorded([ask()], events, TRUSTED, [OP]))
        self.assertFalse(decision_claim.recorded([ask()], events, TRUSTED, [OP], "", [STATION]))

    def test_a_hollow_line_does_not_erase_a_persons_removal(self):
        _, events, _ = self.replayed()
        for text in ("Finished:", "Done: yes.", "Operator answer 2030-01-03:", "Take (a)."):
            with self.subTest(text):
                comments = [ask(), said(text, by=BOT)]
                self.assertTrue(decision_claim.recorded(comments, events, TRUSTED, [OP]))

    def test_a_trusted_bots_bare_line_clears_so_its_removal_is_no_record(self):
        _, events, _ = self.replayed()
        for text in ("Finished:", "Done: yes."):
            with self.subTest(text):
                comments = [ask(), said(text, by=APP)]
                self.assertFalse(decision_claim.recorded(comments, events, TRUSTED, [OP]))

    def test_the_guard_names_its_own_client(self):
        comments, events, _ = self.replayed()
        routes = {"/repos/%s/issues/5" % REPO: issue(), "/repos/%s/issues/5/comments" % REPO: [ask()],
                  "/repos/%s/issues/5/events" % REPO: events, "/user": {"login": STATION}}
        gh = GitHub("t", transport=GitHubLists(routes))
        with mock.patch.dict(os.environ, dict(ON, LOOM_GITHUB_LOGIN="")), \
                self.assertRaises(decision_claim.DecisionNotRecorded):
            brain._check_decision_claim(gh, REPO, 5, DATED, ON)

    def test_a_listed_login_is_no_name_a_claim_can_cite(self):
        with mock.patch.dict(os.environ, ON):
            self.assertEqual([OP], decision_claim.person_logins(TRUSTED, [OP]))
            self.assertEqual("", decision_claim.claim(PERSON + " decided", [OP]))


class Powers(unittest.TestCase):
    """Each power, read with the setting on: a listed login gains none. The
    readers take LOOM_BRIEF_AUTHORS through the same two functions."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ, ON)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.authors = brain.trusted_authors(ON)

    def test_the_trust_lists_do_not_change(self):
        self.assertEqual(brain.trusted_authors(OFF), self.authors)
        self.assertEqual(TRUSTED, self.authors)
        self.assertEqual([], brain.trusted_briefers(ON))

    def test_ownership(self):
        held = {"number": 1, "assignees": [{"login": PERSON}], "labels": [], "user": {"login": OP}}
        self.assertEqual(PERSON, tickets.owned_elsewhere(held, self.authors))
        self.assertEqual(PERSON, brain.owned_elsewhere(held, ON))
        self.assertEqual(OP, tickets.operator_login(self.authors))

    def test_proofs(self):
        found = proof_comment(1, login=BOT)
        fake = GitHubLists({"/repos/%s/issues/5/comments" % REPO: [found]})
        with self.assertRaisesRegex(ValueError, "no loom:proof comment"):
            brain._check_proven(GitHub("t", transport=fake), REPO, 5, {"body": BODY}, ON)

    def test_audits(self):
        self.assertEqual((None, ""), audit._standing([proof_comment(1, login=BOT)], self.authors))

    def test_journals(self):
        row = {"body": handoff.JOURNAL_MARKER + " note", "at": "2030-01-03T00:00:00Z", "login": BOT}
        self.assertEqual([], handoff.journals([row], self.authors))
        self.assertEqual([], brain.trusted_comments([said("x", by=BOT), said("y", by=PERSON)]))

    def test_adoption(self):
        run = factory.Factory(GitHub("t", transport=GitHubLists({})), [REPO], 0.0,
                              trusted=station.Settings.from_env(ON).authors)
        self.assertFalse(run._trusted({"user": {"login": BOT}, "assignees": [], "labels": []}))
        self.assertFalse(run._trusted_ticket({"user": {"login": PERSON}, "assignees": [],
                                              "labels": [{"name": "loom:ticket"}]}))

    def test_merges(self):
        pull = {"state": "open", "user": {"login": BOT}, "head": {"sha": "abc"}}
        fake = GitHubLists({})
        with self.assertRaisesRegex(ValueError, "not a trusted login"):
            brain._validate_adopt_pr(GitHub("t", transport=fake), REPO, pull, {}, ON)

    def test_briefing(self):
        brief = {"user": {"login": PERSON}, "labels": [{"name": "loom:ticket"}]}
        self.assertFalse(tickets.brief_author_trusted(brief, self.authors, brain.trusted_briefers(ON)))
        self.assertNotIn(PERSON, tickets.briefing_logins(self.authors, brain.trusted_briefers(ON)))

    def test_holds(self):
        event = {"event": "labeled", "created_at": "2030-01-03T00:00:00Z", "actor": {"login": BOT},
                 "label": {"name": brain_hold.LABEL}}
        found = brain_hold.setter([event], brain_hold.loom_logins(self.authors))
        self.assertEqual(brain_hold.PERSON, found.kind)

    def test_revert_labels(self):
        self.assertEqual(TRUSTED, merge_tier.revert_labelers(ON))

    def test_dashboard_operators(self):
        self.assertEqual([OP], answer.operators(ON))
        self.assertEqual({OP: OP}, answer.operator_map(ON))
        self.assertEqual(answer.operators(OFF), answer.operators(ON))
        record = {"number": 1, "labels": ["claim:" + PERSON], "assignees": []}
        self.assertEqual([{"login": PERSON, "known": True}], operators.owners(record, ON))

    def test_person_comment(self):
        self.assertFalse(decision_claim.person_comment(said(DATED, by=BOT)))
        self.assertTrue(decision_claim.person_comment(said(DATED, by=PERSON)))
        with mock.patch.dict(os.environ, OFF):
            os.environ.pop("LOOM_ASK_ANSWERERS", None)
            self.assertFalse(decision_claim.person_comment(said(DATED, by=BOT)))
            self.assertTrue(decision_claim.person_comment(said(DATED, by=PERSON)))


class Reach(unittest.TestCase):
    """The setting has one reader and three users, so no later reader of
    `trusted` or of the environment can take it up by accident."""

    def users(self, word):
        found = {}
        for tree in ("bus", "dashboard"):
            for path in (ROOT / tree).rglob("*.py"):
                if word in path.read_text():
                    found[str(path.relative_to(ROOT))] = path.read_text()
        return found

    def test_only_ask_state_reads_the_environment_variable(self):
        readers = [path for path, text in self.users("LOOM_ASK_ANSWERERS").items()
                   if '"LOOM_ASK_ANSWERERS"' in text]
        self.assertEqual(["bus/ask_state.py"], readers)

    def test_only_the_judges_take_the_logins(self):
        self.assertEqual({"bus/ask_state.py", "bus/label_pass.py", "bus/needs_you.py",
                          "bus/station.py"}, set(self.users("answerers")))

    def test_the_station_passes_them_only_to_the_ask_judges(self):
        text = (ROOT / "bus" / "station.py").read_text()
        users = {node.name for node in ast.walk(ast.parse(text))
                 if isinstance(node, ast.FunctionDef)
                 and "answerers" in ast.get_source_segment(text, node)}
        self.assertEqual({"__init__", "from_env", "_ask_inputs", "_clean_labels_repo"}, users)

    def test_decision_claim_and_the_dashboard_never_name_it(self):
        self.assertNotIn("answerer", (ROOT / "bus" / "decision_claim.py").read_text().lower())
        self.assertNotIn("answerer", (ROOT / "bus" / "answer.py").read_text().lower())
        self.assertFalse([p for p in self.users("nswerers") if p.startswith("dashboard/")])


if __name__ == "__main__":
    unittest.main()
