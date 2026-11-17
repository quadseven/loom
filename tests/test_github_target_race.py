"""Two threads one request under the pass target cannot both read past it
(loom#3014): the target check and the request's count were two lock holds,
so a pass on several reader threads ended one request over its target."""
import json
import threading
import unittest
from unittest import mock

from bus import github
from bus.github import GitHub, TargetReached


def transport(method, url, headers, body):
    return 200, {}, json.dumps([{"id": 1}]).encode()


class TargetRaceTest(unittest.TestCase):
    def test_two_threads_at_one_under_the_target_send_one_read(self):
        gh = GitHub("token", transport=transport, target=1)
        # Both threads stop between the target check and the request; with
        # the check and the count in one hold, the second never gets there.
        gate = threading.Barrier(2, timeout=0.5)
        sent = []
        real_rest = GitHub.rest

        def rest(this, method, path, *args, **kwargs):
            try:
                gate.wait()
            except threading.BrokenBarrierError:
                pass
            sent.append(path)
            return real_rest(this, method, path, *args, **kwargs)

        outcomes = []

        def read(number):
            try:
                gh.paginate("/repos/example-owner/example-repo/issues/%d/comments" % number)
                outcomes.append("read")
            except TargetReached:
                outcomes.append("carried")

        with mock.patch.object(GitHub, "rest", rest):
            threads = [threading.Thread(target=read, args=(n,)) for n in (1, 2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(5)
        self.assertEqual(sorted(outcomes), ["carried", "read"])
        self.assertEqual(len(sent), 1)
        self.assertEqual(gh.counted, 1)

    def test_a_read_under_the_target_is_counted_once(self):
        gh = GitHub("token", transport=transport, target=10)
        gh.paginate("/repos/example-owner/example-repo/issues/1/comments")
        self.assertEqual(gh.counted, 1)
        self.assertEqual(gh.requests, 1)

    def test_a_reservation_the_request_did_not_use_is_not_carried_to_the_next(self):
        gh = GitHub("token", transport=transport, target=10)
        with mock.patch.object(GitHub, "rest", side_effect=github.GitHubError("boom", 0)):
            with self.assertRaises(github.GitHubError):
                gh.paginate("/repos/example-owner/example-repo/issues/1/comments")
        self.assertEqual(gh.requests, 1)
        gh.paginate("/repos/example-owner/example-repo/issues/2/comments")
        self.assertEqual(gh.requests, 2)


if __name__ == "__main__":
    unittest.main()
