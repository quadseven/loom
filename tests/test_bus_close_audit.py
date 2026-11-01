"""A close that names a commit proves it (bus/close_audit.py)."""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import time
import unittest
import unittest.mock

from bus import brain, close_audit as ca, shadow, station
from bus.github import GitHubError

REPO = "example-owner/example-repo"
NOW = 1_900_000_000.0
ABSENT_SHA = "b80105fa"
HEAD_SHA = "c1c2c3c4" + "1" * 32
MERGE_SHA = "d1d2d3d4" + "2" * 32


def stamp(when):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when))


def comment(body, ago=7.0):
    return {"created_at": stamp(NOW - ago), "body": body}


def merged_pull(number=2360, head=HEAD_SHA, merge=MERGE_SHA):
    return {"number": number, "merged_at": stamp(NOW - 3600), "merge_commit_sha": merge,
            "head": {"sha": head}, "base": {"repo": {"full_name": REPO}}}


def places(known):
    """The lookup: each sha prefix's place, and a failure for any other."""
    def place(repo, sha):
        for prefix, where in known.items():
            if sha.startswith(prefix):
                return where
        raise AssertionError("no lookup expected for %s" % sha)
    return place


class Check(unittest.TestCase):
    def test_a_close_citing_an_absent_commit_with_no_merged_pull_is_unproven(self):
        # The close of 2026-10-31: the comment said the commit was on main,
        # GitHub had no such commit, and no pull request had merged.
        said = "Landed it directly on main as commit %s (13:09Z)." % ABSENT_SHA
        report = ca.check(REPO, stamp(NOW), [comment(said)], [],
                          places({ABSENT_SHA: ca.ABSENT}))
        self.assertEqual(report.verdict, ca.UNPROVEN)
        self.assertEqual([c.sha for c in report.citations], [ABSENT_SHA])
        self.assertIn(ABSENT_SHA, "\n".join(ca.lines(report)))

    def test_a_close_citing_a_merged_heads_hash_passes_with_both_hashes(self):
        said = "Done, see %s." % HEAD_SHA[:8]
        report = ca.check(REPO, stamp(NOW), [comment(said)], [merged_pull()],
                          places({MERGE_SHA[:8]: ca.ON}))
        self.assertEqual(report.verdict, ca.SOUND)
        self.assertEqual(report.citations[0].pull, 2360)
        printed = "\n".join(ca.lines(report))
        self.assertIn(HEAD_SHA[:8], printed)
        self.assertIn(MERGE_SHA, printed)

    def test_a_failed_lookup_is_unknown_never_sound(self):
        said = "On main as %s." % ABSENT_SHA
        report = ca.check(REPO, stamp(NOW), [comment(said)], [],
                          places({ABSENT_SHA: ca.UNSEEN}))
        self.assertEqual(report.verdict, ca.UNKNOWN)
        report = ca.check(REPO, stamp(NOW), [comment(said)], [merged_pull()],
                          places({ABSENT_SHA: ca.ON, MERGE_SHA[:8]: ca.UNSEEN}))
        self.assertEqual(report.verdict, ca.UNKNOWN)
        report = ca.check(REPO, stamp(NOW), [comment(said)], [],
                          places({ABSENT_SHA: ca.ON}), error="status 502")
        self.assertEqual(report.verdict, ca.UNKNOWN)
        self.assertIn("status 502", ca.lines(report)[0])

    def test_a_cited_hash_off_the_branch_beside_a_merged_pull_differs(self):
        said = "Merged as %s." % ABSENT_SHA
        report = ca.check(REPO, stamp(NOW), [comment(said)], [merged_pull()],
                          places({ABSENT_SHA: ca.OFF, MERGE_SHA[:8]: ca.ON}))
        self.assertEqual(report.verdict, ca.DIFFERS)

    def test_only_the_words_of_the_close_cite_a_commit(self):
        stale = comment("Earlier attempt %s." % ABSENT_SHA, ago=ca.CLOSE_WINDOW + 1)
        hidden = comment("<!-- marker %s --> See the note." % ABSENT_SHA)
        elsewhere = comment("Fixed by https://github.com/other/place/commit/%s and "
                            "other/place@%s." % (ABSENT_SHA, ABSENT_SHA))
        words = comment("A decade of 1234567 facade.")
        report = ca.check(REPO, stamp(NOW), [stale, hidden, elsewhere, words], [], places({}))
        self.assertEqual(report.verdict, ca.UNCITED)
        said = comment("https://github.com/%s/commit/%s" % (REPO, ABSENT_SHA))
        report = ca.check(REPO, stamp(NOW), [said], [], places({ABSENT_SHA: ca.ABSENT}))
        self.assertEqual(report.verdict, ca.UNPROVEN)

    def test_a_close_with_one_cited_commit_on_the_branch_is_not_unproven(self):
        said = "On main as %s, which replaces %s (dropped)." % (MERGE_SHA[:8], ABSENT_SHA)
        report = ca.check(REPO, stamp(NOW), [comment(said)], [],
                          places({MERGE_SHA[:8]: ca.ON, ABSENT_SHA: ca.ABSENT}))
        self.assertEqual(report.verdict, ca.DIFFERS)

    def test_fenced_logs_and_uuid_parts_cite_nothing(self):
        said = "Logs:\n```\nran %s\n```\nId 3fa9c21e-1b2c-4d5e-8f90-a1b2c3d4e5f6." % ABSENT_SHA
        report = ca.check(REPO, stamp(NOW), [comment(said)], [], places({}))
        self.assertEqual(report.verdict, ca.UNCITED)

    def test_too_many_citations_are_unknown(self):
        shas = ["%07xa" % n for n in range(ca.MAX_CITED + 1)]
        report = ca.check(REPO, stamp(NOW), [comment(" ".join(shas))], [],
                          lambda repo, sha: ca.ON)
        self.assertEqual(report.verdict, ca.UNKNOWN)


class Reply:
    def __init__(self, data, status=200):
        self.data = data
        self.status = status


class FakeGitHub:
    """One repo's closed tickets as GitHub answers for them."""

    def __init__(self, **broken):
        self.issues = {}
        self.comments = {}
        self.timelines = {}
        self.pulls = {}
        self.absent = set()
        self.carried = set()
        self.broken = broken
        self.delivered = {}
        self.reads = []
        self.writes = []

    def close(self, number, said, closed_by="a-brain[bot]", at=NOW, reason="completed"):
        self.issues[number] = {
            "number": number, "state": "closed", "state_reason": reason,
            "closed_at": stamp(at), "updated_at": stamp(at), "closed_by": {"login": closed_by}}
        self.comments[number] = [{"created_at": stamp(at - 7), "body": said,
                                  "user": {"login": closed_by}}]

    def link(self, number, pull):
        self.pulls[pull["number"]] = pull
        self.timelines.setdefault(number, []).append({
            "event": "cross-referenced", "source": {"issue": {
                "number": pull["number"], "repository": {"full_name": REPO},
                "pull_request": {"merged_at": pull["merged_at"]}}}})

    def _get(self, path, ok):
        if self.broken.get("compare") and "/compare/" in path:
            raise GitHubError("no", status=502)
        if "/compare/" in path:
            sha = path.split("...", 1)[1].split("?")[0]
            if any(sha.startswith(a) for a in self.absent):
                return Reply({"message": "Not Found"}, 404)
            carried = any(sha.startswith(c) for c in self.carried)
            return Reply({"status": "behind" if carried else "diverged"})
        tail = path.split("/repos/%s" % REPO, 1)[1]
        if tail.startswith("/commits/"):
            sha = tail.split("/")[2]
            return Reply(self.delivered.get(sha, []))
        if tail == "":
            return Reply({"default_branch": "main"})
        if tail.startswith("/issues?"):
            return Reply(sorted(self.issues.values(), key=lambda i: -i["number"]))
        parts = tail.split("?")[0].strip("/").split("/")
        number = int(parts[1])
        if parts[0] == "pulls":
            return Reply(self.pulls[number])
        if len(parts) == 2:
            return Reply(self.issues[number])
        if parts[2] == "comments":
            return Reply(self.comments.get(number, []))
        return Reply(self.timelines.get(number, []) if "page=1" in tail else [])

    def rest(self, method, path, body=None, ok=()):
        if method == "GET":
            self.reads.append(path)
            return self._get(path, ok)
        self.writes.append((method, path, body))
        if method == "PATCH" and self.broken.get("patch"):
            raise GitHubError("no", status=500)
        return Reply({})


class ReadClose(unittest.TestCase):
    def test_the_replay_reads_an_absent_commit_as_unproven(self):
        gh = FakeGitHub()
        gh.close(2328, "On main as commit %s." % ABSENT_SHA)
        gh.absent.add(ABSENT_SHA)
        report = ca.read_close(gh, REPO, 2328)
        self.assertEqual(report.verdict, ca.UNPROVEN)
        self.assertEqual(report.citations[0].place, ca.ABSENT)

    def test_a_merged_head_resolves_to_its_merge_commit(self):
        gh = FakeGitHub()
        gh.close(5, "See %s." % HEAD_SHA[:8])
        gh.link(5, merged_pull(7))
        gh.carried.add(MERGE_SHA)
        report = ca.read_close(gh, REPO, 5)
        self.assertEqual(report.verdict, ca.SOUND)
        self.assertEqual(report.citations[0].merge_commit, MERGE_SHA)

    def test_a_failed_compare_reads_as_unknown(self):
        gh = FakeGitHub(compare=True)
        gh.close(5, "See %s." % ABSENT_SHA)
        self.assertEqual(ca.read_close(gh, REPO, 5).verdict, ca.UNKNOWN)

    def test_an_open_ticket_has_no_close_to_audit(self):
        gh = FakeGitHub()
        gh.close(5, "See %s." % ABSENT_SHA)
        gh.issues[5]["state"] = "open"
        self.assertEqual(ca.read_close(gh, REPO, 5).verdict, ca.UNKNOWN)

    def test_a_timeline_longer_than_the_read_is_unknown(self):
        gh = FakeGitHub()
        gh.close(5, "See %s." % ABSENT_SHA)
        gh.absent.add(ABSENT_SHA)
        full = [{"event": "labeled"}] * ca.PAGE
        original = gh._get

        def get(path, ok):
            return Reply(full) if "/timeline" in path else original(path, ok)
        gh._get = get
        self.assertEqual(ca.read_close(gh, REPO, 5).verdict, ca.UNKNOWN)
        self.assertEqual(len([p for p in gh.reads if "/timeline" in p]), ca.TIMELINE_PAGES)


    def test_another_authors_hash_is_not_the_closers_citation(self):
        gh = FakeGitHub()
        gh.close(5, "Done.")
        gh.comments[5].append({"created_at": stamp(NOW - 5), "user": {"login": "someone"},
                               "body": "Status: merged %s." % ABSENT_SHA})
        self.assertEqual(ca.read_close(gh, REPO, 5).verdict, ca.UNCITED)

    def test_an_intermediate_commit_of_a_squash_resolves_to_its_pull(self):
        gh = FakeGitHub()
        gh.close(5, "See %s." % ABSENT_SHA)
        gh.delivered[ABSENT_SHA] = [merged_pull(7)]
        gh.carried.add(MERGE_SHA)
        report = ca.read_close(gh, REPO, 5)
        self.assertEqual(report.verdict, ca.SOUND)
        self.assertEqual(report.citations[0].pull, 7)
        self.assertEqual(report.citations[0].merge_commit, MERGE_SHA)


class RunPass(unittest.TestCase):
    def setUp(self):
        patcher = unittest.mock.patch.object(shadow, "active", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_pass(self, gh, state, now=NOW + 60):
        said = []
        ca.run_pass(gh, [REPO], state, now, lambda: "the-station[bot]", said.append)
        return said

    def posted(self, gh):
        return [w for w in gh.writes if w[1].endswith("/comments")]

    def reopens(self, gh):
        return [w for w in gh.writes if w[0] == "PATCH" and w[2] == {"state": "open"}]

    def started(self):
        return {"since": {REPO: NOW - 600}}

    def test_the_first_pass_reads_nothing_closed_before_it(self):
        gh = FakeGitHub()
        gh.close(1, "On main as %s." % ABSENT_SHA)
        gh.absent.add(ABSENT_SHA)
        state = {}
        self.run_pass(gh, state)
        self.assertEqual(state["since"], {REPO: NOW + 60})
        self.assertEqual(gh.writes, [])

    def test_an_unproven_close_gets_one_comment_and_one_reopen(self):
        gh = FakeGitHub()
        gh.close(1, "On main as %s." % ABSENT_SHA)
        gh.absent.add(ABSENT_SHA)
        state = self.started()
        said = self.run_pass(gh, state)
        self.assertEqual(len(self.posted(gh)), 1)
        self.assertIn(ABSENT_SHA, self.posted(gh)[0][2]["body"])
        self.assertEqual(len(self.reopens(gh)), 1)
        self.assertLess(gh.writes.index(self.posted(gh)[0]), gh.writes.index(self.reopens(gh)[0]))
        self.assertIn("1 closes read, 1 refused", "\n".join(said))
        self.run_pass(gh, state, now=NOW + 60 + ca.EVERY)
        self.assertEqual(len(self.posted(gh)), 1)
        self.assertEqual(len(self.reopens(gh)), 1)

    def test_a_ticket_closed_again_is_not_reopened_twice(self):
        gh = FakeGitHub()
        gh.close(1, "On main as %s." % ABSENT_SHA)
        gh.absent.add(ABSENT_SHA)
        state = self.started()
        self.run_pass(gh, state)
        gh.close(1, "Still on main as %s." % ABSENT_SHA, at=NOW + 100)
        said = self.run_pass(gh, state, now=NOW + 60 + ca.EVERY)
        self.assertEqual(len(self.reopens(gh)), 1)
        self.assertEqual(len(self.posted(gh)), 2)
        self.assertIn("reopened once already", "\n".join(said))

    def test_the_stations_closes_and_other_closes_are_left_alone(self):
        gh = FakeGitHub()
        gh.close(1, "On main as %s." % ABSENT_SHA, closed_by="the-station[bot]")
        gh.close(2, "Not doing %s." % ABSENT_SHA, reason="not_planned")
        gh.close(3, "Old %s." % ABSENT_SHA, at=NOW - 3600)
        gh.absent.add(ABSENT_SHA)
        self.run_pass(gh, self.started())
        self.assertEqual(gh.writes, [])

    def test_a_failed_lookup_writes_nothing_and_comes_back(self):
        gh = FakeGitHub(compare=True)
        gh.close(1, "On main as %s." % ABSENT_SHA)
        state = self.started()
        self.run_pass(gh, state)
        self.assertEqual(gh.writes, [])
        gh.broken = {}
        gh.absent.add(ABSENT_SHA)
        self.run_pass(gh, state, now=NOW + 60 + ca.EVERY)
        self.assertEqual(len(self.reopens(gh)), 1)

    def test_a_pass_stays_inside_its_read_budget_and_the_rest_wait(self):
        gh = FakeGitHub()
        for number in range(1, 41):
            gh.close(number, "On main as %s." % ABSENT_SHA)
            gh.link(number, merged_pull(1000 + number))
        gh.carried.update({ABSENT_SHA, MERGE_SHA})
        state = self.started()
        said = self.run_pass(gh, state)
        self.assertLessEqual(len(gh.reads), ca.READS_PER_PASS)
        self.assertIn("waiting", "\n".join(said))
        before = len(gh.reads)
        self.run_pass(gh, state, now=NOW + 60 + ca.EVERY)
        self.assertLessEqual(len(gh.reads) - before, ca.READS_PER_PASS)
        self.assertEqual(gh.writes, [])

    def test_a_failed_reopen_retries_without_a_second_comment(self):
        gh = FakeGitHub(patch=True)
        gh.close(1, "On main as %s." % ABSENT_SHA)
        gh.absent.add(ABSENT_SHA)
        state = self.started()
        self.run_pass(gh, state)
        gh.broken = {}
        self.run_pass(gh, state, now=NOW + 60 + ca.EVERY)
        self.assertEqual(len(self.posted(gh)), 1)
        self.assertEqual(len(self.reopens(gh)), 2)
        self.assertEqual(state["posted"], {})

    def test_a_close_that_stays_unknown_is_read_three_times(self):
        gh = FakeGitHub(compare=True)
        gh.close(1, "On main as %s." % ABSENT_SHA)
        state = self.started()
        for n in range(ca.UNKNOWN_TRIES + 2):
            self.run_pass(gh, state, now=NOW + 60 + n * ca.EVERY)
        issue_reads = [p for p in gh.reads if p.endswith("/issues/1")]
        self.assertEqual(len(issue_reads), ca.UNKNOWN_TRIES)

    def test_a_repo_that_cannot_be_listed_does_not_stop_the_next(self):
        gh = FakeGitHub()
        calls = []
        real = gh.rest

        def rest(method, path, body=None, ok=()):
            if "/repos/broken/place/" in path:
                raise GitHubError("gone", status=404)
            calls.append(path)
            return real(method, path, body, ok)
        gh.rest = rest
        state = {"since": {REPO: NOW - 600, "broken/place": NOW - 600}}
        ca.run_pass(gh, ["broken/place", REPO], state, NOW + 60, lambda: "x", lambda line: None)
        self.assertTrue(calls)

    def test_the_most_a_ticket_can_read_is_inside_its_reservation(self):
        gh = FakeGitHub()
        said = " ".join("%07xa" % n for n in range(ca.MAX_CITED))
        gh.close(1, said)
        for number in range(ca.MAX_PULLS + 1):
            gh.link(1, merged_pull(100 + number, head="e" * 40, merge="f%039d" % number))
        original = gh._get

        def get(path, ok):
            return Reply([{"event": "labeled"}] * ca.PAGE) if "/timeline" in path \
                and "page=1" not in path and "page=2" not in path else original(path, ok)
        gh._get = get
        ca.read_close(gh, REPO, 1)
        self.assertLessEqual(len(gh.reads), ca.TICKET_READS)

    def test_reopens_per_pass_are_capped(self):
        gh = FakeGitHub()
        for number in range(1, ca.REOPENS_PER_PASS + 3):
            gh.close(number, "On main as %s." % ABSENT_SHA)
        gh.absent.add(ABSENT_SHA)
        self.run_pass(gh, self.started())
        self.assertEqual(len(self.reopens(gh)), ca.REOPENS_PER_PASS)

    def test_shadow_mode_records_the_reopen_and_posts_nothing(self):
        gh = FakeGitHub()
        gh.close(1, "On main as %s." % ABSENT_SHA)
        gh.absent.add(ABSENT_SHA)
        with unittest.mock.patch.object(shadow, "active", return_value=True), \
                unittest.mock.patch.object(shadow, "record_held") as held:
            self.run_pass(gh, self.started())
        self.assertEqual(gh.writes, [])
        self.assertEqual(held.call_args[0][:3], (shadow.REOPEN, REPO, 1))

    def test_a_closer_that_cannot_be_told_stays_for_a_later_pass(self):
        gh = FakeGitHub()
        gh.close(1, "On main as %s." % ABSENT_SHA)
        gh.absent.add(ABSENT_SHA)
        state = self.started()
        ca.run_pass(gh, [REPO], state, NOW + 60, lambda: "", lambda line: None)
        self.assertEqual(gh.writes, [])
        self.assertEqual(state["seen"], {})


class StationStep(unittest.TestCase):
    def test_the_step_keeps_its_state_and_starts_at_its_first_pass(self):
        gh = FakeGitHub()
        with tempfile.TemporaryDirectory() as home:
            settings = station.Settings(home, [REPO], [], [], status_repo=REPO)
            st = station.Station(gh, settings)
            st.now = NOW
            st.audit_closes()
            with open(os.path.join(st.state, ca.STATE_FILE)) as fh:
                self.assertEqual(json.load(fh)["since"], {REPO: NOW})
            self.assertEqual(gh.reads, [])

    def test_a_state_file_that_cannot_be_read_stops_the_step_and_is_kept(self):
        gh = FakeGitHub()
        with tempfile.TemporaryDirectory() as home:
            st = station.Station(gh, station.Settings(home, [REPO], [], [], status_repo=REPO))
            st.now = NOW
            path = os.path.join(st.state, ca.STATE_FILE)
            os.makedirs(st.state, exist_ok=True)
            with open(path, "w") as fh:
                fh.write("{broken")
            st.audit_closes()
            with open(path) as fh:
                self.assertEqual(fh.read(), "{broken")
            self.assertEqual(gh.reads, [])

    def test_the_step_runs_after_the_stations_own_closes(self):
        with open(os.path.join(os.path.dirname(station.__file__), "station.py")) as fh:
            self.assertIn("self.close_done_tickets, self.audit_closes,", fh.read())


class Command(unittest.TestCase):
    def run_command(self, gh):
        out = io.StringIO()
        args = brain.parser().parse_args(["audit-close", "%s#%d" % (REPO, 5)])
        with contextlib.redirect_stdout(out):
            code = brain.COMMANDS[args.command](gh, args)
        return code, out.getvalue()

    def test_audit_close_prints_the_facts_and_exits_zero_only_when_sound(self):
        gh = FakeGitHub()
        gh.close(5, "See %s." % HEAD_SHA[:8])
        gh.link(5, merged_pull(7))
        gh.carried.add(MERGE_SHA)
        code, printed = self.run_command(gh)
        self.assertEqual(code, 0)
        self.assertIn(HEAD_SHA[:8], printed)
        self.assertIn(MERGE_SHA, printed)
        self.assertIn(ca.ON, printed)
        gh = FakeGitHub()
        gh.close(5, "See %s." % ABSENT_SHA)
        gh.absent.add(ABSENT_SHA)
        code, printed = self.run_command(gh)
        self.assertEqual(code, 1)
        self.assertIn("unproven", printed)
