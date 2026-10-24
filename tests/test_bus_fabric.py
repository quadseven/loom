"""Test bus.fabric: the station's one question to the Fabric workspace."""
import io
import json
import os
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from bus import fabric

ANSWER = ("The work landed in [#1402 [PR] A lane pull request is sent back only with a new "
          "actionable finding](fabric://c1a485b6-36a8-498b-bd3f-8911bf1a63b3), and the "
          "issue [#1389 [Issue] A lane pull request is sent back](fabric://"
          "9913d9d1-de94-4e7f-8b5d-1d0ab2b37a33) tracked it. Again: [#1402 [PR] A lane pull "
          "request is sent back only with a new actionable finding](fabric://"
          "c1a485b6-36a8-498b-bd3f-8911bf1a63b3).")


class CitedPullsTest(unittest.TestCase):
    def test_only_pull_request_cards_count_and_each_number_once(self):
        self.assertEqual(fabric.cited_pulls(ANSWER), [
            (1402, "A lane pull request is sent back only with a new actionable finding")])

    def test_a_plain_mention_is_not_a_citation(self):
        self.assertEqual(fabric.cited_pulls("See #1402 [PR] for details."), [])
        self.assertEqual(fabric.cited_pulls(""), [])


class SameTitleTest(unittest.TestCase):
    def test_case_and_spacing_do_not_matter(self):
        self.assertTrue(fabric.same_title("A lane  PR is sent back", "a lane pr is sent back"))

    def test_a_card_cut_short_matches_its_start(self):
        self.assertTrue(fabric.same_title("A lane pull request is sent back only with",
                                          "A lane pull request is sent back only with a finding"))

    def test_another_title_does_not_match(self):
        self.assertFalse(fabric.same_title("Fix the dashboard", "Fix the station"))
        self.assertFalse(fabric.same_title("Fix", "Fix the station"))
        self.assertFalse(fabric.same_title("", "Fix"))


class AskTest(unittest.TestCase):
    def test_unconfigured_asks_nothing(self):
        with mock.patch("bus.fabric.read_ssm") as ssm, mock.patch("urllib.request.urlopen") as op:
            self.assertEqual(fabric.ask("q", "", "us-east-1"), "")
        ssm.assert_not_called()
        op.assert_not_called()

    def test_the_question_goes_as_a_whole_library_chat_with_the_key(self):
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = b"an answer"
        with mock.patch("bus.fabric.read_ssm", return_value="k-123") as ssm, \
                mock.patch("urllib.request.urlopen", return_value=resp) as op:
            self.assertEqual(fabric.ask("which?", "/p/key", "us-east-1"), "an answer")
        ssm.assert_called_once_with("/p/key", "us-east-1")
        req = op.call_args.args[0]
        self.assertEqual(req.full_url, fabric.CHAT_URL)
        self.assertEqual(req.get_header("X-api-key"), "k-123")
        self.assertEqual(json.loads(req.data), {
            "scope": "user", "history": [{"role": "user", "content": "which?"}]})

    def test_a_failure_anywhere_is_no_answer(self):
        with mock.patch("bus.fabric.read_ssm", side_effect=OSError("no")):
            self.assertEqual(fabric.ask("q", "/p/key", "us-east-1"), "")
        for err in (urllib.error.URLError("down"), TimeoutError("slow"),
                    urllib.error.HTTPError(fabric.CHAT_URL, 429, "busy", {}, None)):
            with mock.patch("bus.fabric.read_ssm", return_value="k"), \
                    mock.patch("urllib.request.urlopen", side_effect=err):
                self.assertEqual(fabric.ask("q", "/p/key", "us-east-1"), "", err)


class AskCliTest(unittest.TestCase):
    def test_ask_asks_the_helper_with_the_documented_shape(self):
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = b"an answer"
        env = {"LOOM_FABRIC_ASK": "http://127.0.0.1:7341/ask"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("urllib.request.urlopen", return_value=resp) as op:
            self.assertEqual(fabric.main(["ask", "which?"]), 0)
        self.assertNotIn("LOOM_FABRIC_KEY_SSM", os.environ)
        self.assertNotIn("LOOM_AWS_REGION", os.environ)
        req = op.call_args.args[0]
        self.assertEqual(req.full_url, "http://127.0.0.1:7341/ask")
        self.assertEqual(json.loads(req.data), {
            "scope": "user", "history": [{"role": "user", "content": "which?"}]})

    def test_ask_without_a_helper_reports_not_configured(self):
        err = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch("sys.stderr", err), \
                mock.patch("bus.fabric.read_ssm") as ssm, \
                mock.patch("urllib.request.urlopen") as op:
            self.assertEqual(fabric.main(["ask", "which?"]), 2)
        self.assertEqual(err.getvalue(), "fabric: not configured\n")
        ssm.assert_not_called()
        op.assert_not_called()

    def test_ask_helper_failure_reports_not_configured(self):
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"LOOM_FABRIC_ASK": "http://127.0.0.1:7341/ask"}, clear=True), \
                mock.patch("sys.stderr", err), \
                mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            self.assertEqual(fabric.main(["ask", "which?"]), 2)
        self.assertEqual(err.getvalue(), "fabric: not configured\n")


class BrainEnvTest(unittest.TestCase):
    """The brain agent must never receive the Fabric key or the SSM name that
    names it (lanes/mac/brain.sh keeps keys and SSM names out of the agent's
    environment, so a prompt injected through an issue cannot reach the key)."""

    BRAIN_SH = Path(__file__).resolve().parent.parent / "lanes" / "mac" / "brain.sh"

    def test_brain_sh_keeps_fabric_key_ssm_out_of_agent_env(self):
        text = self.BRAIN_SH.read_text()
        settings = [line for line in text.splitlines()
                    if line.strip().startswith("BRAIN_SETTINGS=")]
        self.assertEqual(len(settings), 1)
        self.assertNotIn("LOOM_FABRIC_KEY_SSM", settings[0])
        self.assertNotIn("LOOM_AWS_REGION", settings[0])


if __name__ == "__main__":
    unittest.main()
