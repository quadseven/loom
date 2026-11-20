"""The opencode-go strong tier names only models the CLI still runs."""
import unittest
from pathlib import Path
import tempfile

from bus import model_pick, tickets
from test_model_pick import ENV, NOW, Provider

GONE = "opencode-go/gpt-6-luna"


class GoStrongOfferedTest(unittest.TestCase):
    def test_the_strong_tier_never_picks_the_model_the_cli_dropped(self):
        provider = Provider()
        provider.go.append(GONE.split("/", 1)[1])  # the lists still name it
        with tempfile.TemporaryDirectory() as tmp:
            got = model_pick.pick("opencode-go", "slot", False, "fake-key",
                                  Path(tmp), NOW, ENV, provider,
                                  tier=model_pick.TIER_STRONG,
                                  brief_demoted="none")
        self.assertNotEqual(got.model, GONE)
        self.assertNotIn(GONE, model_pick.GO_PAID)

    def test_every_strong_model_is_on_the_go_allow_list(self):
        for model in model_pick.GO_STRONG:
            self.assertIn(model, tickets.OPENCODE_GO_MODELS)


if __name__ == "__main__":
    unittest.main()
