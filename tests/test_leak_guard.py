"""Every bot comment on a public repo passes one leak guard (loom#1862).

The guard is bus/text.public_safe; the GitHub client applies it to every
write's text (bus/github.py), so notify.post, status edits, metrics, journal
ticks and fix comments all pass it.
"""
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bus import fixrounds, metrics, notify, status, text  # noqa: E402
from bus.github import GitHub, leak_deny_terms  # noqa: E402

PUBLIC = "example-owner/open-goal"
HIDDEN = "example-owner/hidden"
OTHER_PUBLIC = "example-owner/open-tools"
PRIVATE = "example-owner/closed-goal"
# owner/name#N, the shape that must not reach a public repo for a private one.
REF = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+#[0-9]+")


def reply(status_code=200, body=None):
    return status_code, {}, b"" if body is None else json.dumps(body).encode()


class Routed:
    """Fake transport: answers whether a repo is private, records writes."""

    VISIBLE = {PUBLIC: False, OTHER_PUBLIC: False, PRIVATE: True, HIDDEN: True}

    def __init__(self, comments=None, issue=None):
        self.comments = comments or []
        self.issue = issue
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, body))
        path = url.split("api.github.com", 1)[1].split("?")[0]
        repo = re.fullmatch(r"/repos/([^/]+/[^/]+)", path)
        if method == "GET" and repo:
            if repo.group(1) not in self.VISIBLE:
                return reply(404, {"message": "Not Found"})
            return reply(200, {"private": self.VISIBLE[repo.group(1)]})
        if method == "GET" and re.fullmatch(r"/repos/[^/]+/[^/]+/issues/[0-9]+", path):
            return reply(200, self.issue)
        if method == "GET":
            return reply(200, self.comments)
        return reply(201 if method == "POST" else 200,
                     {"id": 9, "updated_at": "2026-10-29T00:00:00Z"})

    def sent(self):
        return [json.loads(body)["body"] for method, _, body in self.calls if method != "GET"]


def client(transport, deny=()):
    gh = GitHub("tok", transport=transport)
    gh.deny_terms = tuple(deny)
    return gh


class PublicSafeTest(unittest.TestCase):
    def private(self, repo):
        return Routed.VISIBLE.get(repo, True)

    def test_a_private_repo_issue_is_written_in_words(self):
        out = text.public_safe("Done: %s#12 merged." % HIDDEN, PUBLIC, self.private)
        self.assertEqual(out, "Done: issue 12 of a private repo merged.")

    def test_a_public_repo_issue_and_the_target_itself_are_kept(self):
        body = "See %s#3 and %s#4." % (OTHER_PUBLIC, PUBLIC)
        self.assertEqual(text.public_safe(body, PUBLIC, self.private), body)

    def test_a_link_and_a_bare_name_of_a_private_repo_are_written_in_words(self):
        body = "https://github.com/%s/pull/5#top and %s." % (HIDDEN, HIDDEN)
        self.assertEqual(text.public_safe(body, PUBLIC, self.private),
                         "issue 5 of a private repo and a private repo.")

    def test_a_link_without_a_scheme_and_a_path_in_a_private_repo_are_written_in_words(self):
        body = "www.github.com/%s/issues/6 and github.com/%s, file %s/src/x.py" % (
            HIDDEN, HIDDEN, HIDDEN)
        self.assertEqual(text.public_safe(body, PUBLIC, self.private),
                         "issue 6 of a private repo and a private repo, "
                         "file a private repo/src/x.py")

    def test_a_bare_name_of_another_owner_is_never_looked_up(self):
        asked = []

        def private(repo):
            asked.append(repo)
            return True
        body = "edit bus/text.py on branch loom/1862-x"
        self.assertEqual(text.public_safe(body, PUBLIC, private), body)
        self.assertEqual(asked, [])

    def test_a_private_suffix_and_a_deny_listed_host_are_replaced(self):
        body = "pull build.ci.internal:5000/app from reg.blocked-example.test failed"
        out = text.public_safe(body, PUBLIC, self.private, deny=("blocked-example.test",))
        self.assertEqual(out, "pull <private-host>:5000/app from <private-host> failed")

    def test_a_deny_listed_name_outside_a_host_is_redacted(self):
        out = text.public_safe("asked Codename Zed", PUBLIC, self.private,
                               deny=("codename zed",))
        self.assertEqual(out, "asked <redacted>")

    def test_a_marker_comment_is_left_for_loom_to_read_back(self):
        body = "<!-- loom:adopted v1 ticket=%s#2 -->\nsee %s#2" % (HIDDEN, HIDDEN)
        self.assertEqual(text.public_safe(body, PUBLIC, self.private),
                         "<!-- loom:adopted v1 ticket=%s#2 -->\nsee issue 2 of a private repo"
                         % HIDDEN)

    def test_a_private_target_changes_nothing(self):
        body = "%s#12 on build.ci.internal" % HIDDEN
        self.assertEqual(text.public_safe(body, PRIVATE, self.private,
                                          deny=("build",)), body)

    def test_a_body_with_nothing_to_guard_never_asks_about_the_target(self):
        def private(repo):
            raise AssertionError("looked up %s" % repo)
        self.assertEqual(text.public_safe("all green", PUBLIC, private), "all green")

    def test_the_deny_list_is_read_like_the_leak_scanner(self):
        raw = "# names\n\nCodename Zed\ncodename zed\nblocked-example.test\n"
        self.assertEqual(text.parse_deny_list(raw), ("Codename Zed", "blocked-example.test"))


class EveryPathTest(unittest.TestCase):
    def test_a_goal_report_names_a_private_issue_in_words(self):
        t = Routed()
        notify.post(client(t), PUBLIC, 30, "Goal report: %s#12 shipped." % HIDDEN,
                    notify.REPORT)
        self.assertEqual(t.sent(), ["Goal report: issue 12 of a private repo shipped."])
        self.assertIsNone(REF.search(t.sent()[0]))

    def test_a_fix_comment_quoting_a_log_line_has_the_host_replaced(self):
        t = Routed()
        body = ("<!-- loom:fix -->\nPull request: %s#4, branch loom/4-x\n\n```\n"
                "ERROR pull reg.blocked-example.test/app:1 denied\n```\n" % PUBLIC)
        fixrounds.post_round(client(t, deny=("blocked-example.test",)), PUBLIC, 4, body,
                             authors=(), ledger=None)
        self.assertIn("ERROR pull <private-host>/app:1 denied", t.sent()[0])
        self.assertNotIn("blocked-example", t.sent()[0])
        self.assertIn("Pull request: %s#4, branch loom/4-x" % PUBLIC, t.sent()[0])

    def test_a_status_comment_edit_is_guarded(self):
        old = status.render([("2026-10-28T00:00:00Z", "lane given")])
        t = Routed([{"id": 5, "user": {"login": "loom-op"}, "body": old,
                     "created_at": "2026-10-28T00:00:00Z"}])
        status.add(client(t), PUBLIC, 7, "waits on %s#8" % HIDDEN, 1793232000.0,
                   trusted=("loom-op",))
        method, url, _ = t.calls[-1]
        self.assertEqual((method, url.endswith("/issues/comments/5")), ("PATCH", True))
        self.assertIn("waits on issue 8 of a private repo", t.sent()[-1])
        self.assertIsNone(REF.search(t.sent()[-1]))

    def test_a_status_event_in_the_issue_body_is_guarded_and_the_rest_kept(self):
        after = "After: %s#3" % HIDDEN
        t = Routed(issue={"number": 7, "body": after + "\n\nThe brief.",
                          "user": {"login": "loom-op"}})
        gh = client(t)
        gh.status_in_body = True
        status.add(gh, PUBLIC, 7, "waits on %s#8" % HIDDEN, 1793232000.0,
                   trusted=("loom-op",))
        method, url, _ = t.calls[-1]
        self.assertEqual((method, url.endswith("/issues/7")), ("PATCH", True))
        self.assertTrue(t.sent()[-1].startswith(after + "\n"))
        self.assertIn("waits on issue 8 of a private repo", t.sent()[-1])

    def test_a_metrics_comment_is_guarded(self):
        t = Routed()
        body = metrics.render([metrics.Number("regressions", "1: %s#9" % HIDDEN, "0",
                                              False)], "2026-10-29", 0)
        client(t).rest("POST", "/repos/%s/issues/30/comments" % PUBLIC, {"body": body})
        self.assertIn("1: issue 9 of a private repo", t.sent()[0])
        self.assertIsNone(REF.search(t.sent()[0]))

    def test_a_review_thread_reply_is_guarded(self):
        t = Routed()
        client(t).rest("POST", "/repos/%s/pulls/4/comments/11/replies" % PUBLIC,
                       {"body": "as in %s#2" % HIDDEN})
        self.assertEqual(t.sent(), ["as in issue 2 of a private repo"])

    def test_a_graphql_writer_guards_its_text_through_the_client(self):
        gh = client(Routed(), deny=("blocked-example.test",))
        self.assertEqual(gh.public_safe(PUBLIC, "> pull reg.blocked-example.test failed"),
                         "> pull <private-host> failed")

    def test_the_same_body_on_a_private_repo_is_unchanged(self):
        t = Routed()
        body = "Goal report: %s#12 on build.ci.internal" % HIDDEN
        notify.post(client(t, deny=("build",)), PRIVATE, 30, body, notify.REPORT)
        self.assertEqual(t.sent(), [body])

    def test_a_repo_the_client_cannot_see_counts_as_private(self):
        t = Routed()
        gh = client(t)
        unseen = "example-owner/unseen"
        notify.post(gh, PUBLIC, 30, "see %s#2 and again %s#3" % (unseen, unseen), notify.REPORT)
        self.assertEqual(t.sent(), ["see issue 2 of a private repo and again "
                                    "issue 3 of a private repo"])
        reads = [url for method, url, _ in t.calls if url.endswith("/repos/" + unseen)]
        self.assertEqual(len(reads), 1)

    def test_a_body_with_nothing_to_guard_costs_no_read(self):
        t = Routed()
        notify.post(client(t), PUBLIC, 30, "all green", notify.REPORT)
        self.assertEqual([c[0] for c in t.calls], ["POST"])


class DenyListTest(unittest.TestCase):
    def test_the_copy_under_the_ssm_dir_is_read_first(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "infra", "leak-scan")
            path.mkdir(parents=True)
            (path / "deny-list").write_text("# terms\nblocked-example.test\n")

            def never(name, region):
                raise AssertionError("read SSM")
            self.assertEqual(leak_deny_terms({"LOOM_SSM_DIR": tmp}, read=never),
                             ("blocked-example.test",))

    def test_the_parameter_is_read_from_ssm_otherwise(self):
        asked = []

        def read(name, region):
            asked.append((name, region))
            return "Codename Zed\n"
        env = {"LOOM_LEAK_DENY_SSM": "/x/deny", "LOOM_AWS_REGION": "eu-west-1"}
        self.assertEqual(leak_deny_terms(env, read=read), ("Codename Zed",))
        self.assertEqual(asked, [("/x/deny", "eu-west-1")])


if __name__ == "__main__":
    unittest.main()
