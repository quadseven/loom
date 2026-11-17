"""The REST client refuses a new comment that renders as nothing (#3200).

bus/notify.py refuses one before it reaches the client; this guard holds at
the client too, so a post whose path is built elsewhere (a parameter the
gateway scan cannot follow) or a review-thread reply is caught the same way.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bus import notify  # noqa: E402
from bus.github import GitHub  # noqa: E402

REPO = "example-owner/example-repo"
REPLY = ("mutation($id:ID!,$body:String!) { addPullRequestReviewThreadReply("
         "input:{pullRequestReviewThreadId:$id, body:$body}) { comment { id } } }")


class Fake:
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, headers, body):
        path = url.split("api.github.com", 1)[1]
        self.calls.append((method, path, json.loads(body) if body else None))
        if path == "/graphql":
            return 200, {}, b'{"data": {}}'
        return 201, {}, b'{"id": 1}'


EMPTY = ("<!-- loom:remote-claim v1 muse 204 1794932994.393 -->", "  \n\t",
         "<details>\n<summary></summary>\n</details>")
PATHS = ("/repos/%s/issues/7/comments" % REPO,
         "/repos/%s/pulls/7/comments" % REPO,
         "/repos/%s/pulls/7/comments/42/replies" % REPO)


class EmptyCommentAtTheClientTests(unittest.TestCase):
    def test_a_new_comment_with_no_visible_text_is_refused_and_never_sent(self):
        for path in PATHS:
            for body in EMPTY:
                with self.subTest(path=path, body=body):
                    fake = Fake()
                    with self.assertRaises(notify.EmptyComment):
                        GitHub("t", transport=fake).rest("POST", path, {"body": body})
                    self.assertEqual(fake.calls, [])

    def test_a_review_thread_reply_with_no_visible_text_is_refused(self):
        fake = Fake()
        with self.assertRaises(notify.EmptyComment):
            GitHub("t", transport=fake).graphql(REPLY, {"id": "T", "body": "<!-- x -->"})
        self.assertEqual(fake.calls, [])

    def test_visible_comments_and_other_writes_are_sent(self):
        fake = Fake()
        gh = GitHub("t", transport=fake)
        for path in PATHS:
            gh.rest("POST", path, {"body": "<!-- loom:x -->\nA line a reader sees."})
        gh.graphql(REPLY, {"id": "T", "body": "Fixed."})
        # An edit sends no notification, and a label or issue is no comment.
        gh.rest("PATCH", "/repos/%s/issues/comments/3" % REPO, {"body": "<!-- loom:x -->"})
        gh.rest("POST", "/repos/%s/issues/7/labels" % REPO, {"labels": ["x"]})
        self.assertEqual(len(fake.calls), 6)


if __name__ == "__main__":
    unittest.main()
