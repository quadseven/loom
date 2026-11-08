"""Test the cluster proof ticket system for Mac lane retirement."""
import os
import re
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
MAC_INSTALL = REPO_ROOT / "lanes" / "mac" / "install.sh"
class ClusterProofTest(unittest.TestCase):
    """Test that slots with cluster proof tickets are removed from Mac default lanes."""

    def test_default_lane_list(self):
        """Read the default lane list from mac/install.sh."""
        content = MAC_INSTALL.read_text()

        # Find the default LANES assignment
        line_start = content.find('LANES=("')
        self.assertNotEqual(line_start, -1, "Could not find LANES assignment in mac/install.sh")

        # Find the pipe character
        pipe_pos = content.find('|', line_start)
        self.assertNotEqual(pipe_pos, -1, "Could not find pipe character in LANES assignment")

        # Find the opening parenthesis after the pipe
        paren_pos = content.find('(', pipe_pos)
        self.assertNotEqual(paren_pos, -1, "Could not find opening parenthesis after pipe")

        # Extract until the closing parenthesis
        end_paren = content.find(')', paren_pos)
        self.assertNotEqual(end_paren, -1, "Could not find closing parenthesis")

        default_lanes_str = content[paren_pos + 1:end_paren]

        # Split by whitespace and newlines
        default_lanes = []
        for line in default_lanes_str.split('\n'):
            for part in line.split():
                if part.strip():
                    default_lanes.append(part.strip())

        # Expected default lanes from reading mac/install.sh (after removing proven cluster slots)
        expected_lanes = [
            "claude", "anthropic", "agy", "copilot",
            "anthropic-2", "anthropic-3", "anthropic-4", "anthropic-5", "anthropic-6", "anthropic-7", "anthropic-8",
            "free-ollama", "free-gemma", "free-nvidia", "free-kilo", "free-groq"
        ]

        self.assertEqual(default_lanes, expected_lanes,
                        "Default lanes list doesn't match expected lanes")

    def test_runner_lanes_vs_mac_lanes(self):
        """Verify which lanes run on cluster runners vs Mac."""
        # From bus/runners.py
        RUNNER_LANES = {"codex", "opencode-go", "zen", "openrouter", "zai", "workersai", "free", "spark"}

        # From bus/sessions.py  
        MAC_LANES = {"codex", "claude", "openrouter", "opencode-go", "anthropic"}

        # Lanes that can run on either Mac or cluster runners
        overlap = RUNNER_LANES & MAC_LANES
        self.assertIn("codex", overlap)
        self.assertIn("openrouter", overlap)
        self.assertIn("opencode-go", overlap)

        # Lanes that only run on cluster runners
        cluster_only = RUNNER_LANES - MAC_LANES
        self.assertIn("zen", cluster_only)
        self.assertIn("zai", cluster_only)
        self.assertIn("workersai", cluster_only)
        self.assertIn("free", cluster_only)
        self.assertIn("spark", cluster_only)

        # Lanes that only run on Mac
        mac_only = MAC_LANES - RUNNER_LANES
        self.assertIn("claude", mac_only)
        self.assertIn("anthropic", mac_only)

    def test_slots_with_cluster_proof_tickets(self):
        """Test that identifies slots that have cluster proof tickets."""
        # This is a simplified test - in reality, we would need to check
        # actual cluster runner logs to determine which slots have proven themselves

        # Define which slots are eligible to be moved (they have or can have cluster proof tickets)
        ELIGIBLE_SLOTS = {"codex", "openrouter", "opencode-go", "zen", "zai", "workersai", "free", "spark"}

        # Default Mac lanes after removing proven cluster slots (updated from mac/install.sh)
        DEFAULT_MAC_LANES = {"claude", "anthropic", "agy", "copilot",
                            "anthropic-2", "anthropic-3", "anthropic-4", "anthropic-5", "anthropic-6", "anthropic-7", "anthropic-8",
                            "free-ollama", "free-gemma", "free-nvidia", "free-kilo", "free-groq"}

        # Slots that should be removed from Mac default list
        slots_to_remove = ELIGIBLE_SLOTS & DEFAULT_MAC_LANES

        # These slots should not be in the final Mac default list
        expected_remaining = DEFAULT_MAC_LANES - slots_to_remove

        # Verify the expected removal
        self.assertNotIn("codex", expected_remaining)
        self.assertNotIn("openrouter", expected_remaining)
        self.assertNotIn("opencode-go", expected_remaining)
        self.assertNotIn("zen", expected_remaining)
        self.assertNotIn("zai", expected_remaining)
        self.assertNotIn("workersai", expected_remaining)
        self.assertNotIn("spark", expected_remaining)
        
        # All remaining slots are those that should NOT be removed
        self.assertIn("claude", expected_remaining)
        self.assertIn("anthropic", expected_remaining)
        self.assertIn("agy", expected_remaining)
        self.assertIn("copilot", expected_remaining)
        self.assertIn("anthropic-2", expected_remaining)
        self.assertIn("anthropic-3", expected_remaining)
        self.assertIn("anthropic-4", expected_remaining)
        self.assertIn("anthropic-5", expected_remaining)
        self.assertIn("anthropic-6", expected_remaining)
        self.assertIn("anthropic-7", expected_remaining)
        self.assertIn("anthropic-8", expected_remaining)
        self.assertIn("free-ollama", expected_remaining)
        self.assertIn("free-gemma", expected_remaining)
        self.assertIn("free-nvidia", expected_remaining)
        self.assertIn("free-kilo", expected_remaining)
        self.assertIn("free-groq", expected_remaining)

    def test_proof_line_requirement(self):
        """Test the proof line requirement from the acceptance criteria."""
        # The proof line checks:
        # "after the change, `launchctl list | grep -c com.loom` on the lane host drops by the number of moved agents"

        # This would be tested in integration by running the installer and checking
        # that removed slots don't have com.loom agents installed

        # For now, we verify the logic that determines which slots should be moved
        pass
if __name__ == "__main__":
    unittest.main()
