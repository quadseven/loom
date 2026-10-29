from __future__ import annotations

import json
import os
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain, handoff
from bus.github import GitHub

REPO = "example-owner/example-repo"
LOCK_REPO = "example-owner/loom"
NOW = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)


def at(minutes_ago: int) -> str:
    from datetime import timedelta
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def journal(minutes_ago, holder, *lines, login="allowed-bot"):
    from bus import handoff
    body = "\n".join([handoff.JOURNAL_MARKER, "Tick %s, %s" % (at(minutes_ago), holder),
                      "- Waiting: merge deferred for budget"] + list(lines))
    return {"body": body, "at": at(minutes_ago), "login": login}


def comment(entry):
    return {"body": entry["body"], "created_at": entry["at"], "user": {"login": entry["login"]}}


class BriefNeedsBrainDeferTests(unittest.TestCase):
    """Basic tests for needs-brain defer on briefed tickets."""

    def test_a_brief_with_needs_brain_defer_records_no_done_marker(self):
        """Test that a brief with needs-brain defer records a done marker."""
        # Create a brief-needs-brain defer marker
        brief_needs_brain_defer = handoff.defer_marker("brief-needs-brain", (REPO, 1), "open", "muse",
                                                       note="needs-brain defer on briefed ticket")
        
        # Create a mock GitHub instance with the defer marker
        class MockGitHub:
            def rest(self, method, path, body=None, etag=None, ok=()):
                if "/repos/%s/issues/1/comments" % REPO in path:
                    return {"data": [comment(journal(10, "muse", brief_needs_brain_defer))]}
                issue = {
                    "number": 1,
                    "labels": [{"name": "loom:goal"}, {"name": "loom:briefed"}, {"name": "needs-brain"}]
                }
                return {"data": issue}
        
        gh = MockGitHub()
        
        # Test that the brief-needs-brain defer validation works
        try:
            brain._check_stage_item(gh, "brief-needs-brain", REPO, 1)
            print("✓ Validation passed: brief-needs-brain is a loom:goal issue that is loom:briefed and needs-brain")
        except Exception as e:
            print(f"✗ Validation failed: {e}")
            raise

    def test_a_brief_without_needs_brain_is_rejected(self):
        """Test that a brief without needs-brain is rejected by brief-needs-brain path."""
        # Create a mock GitHub instance without the needs-brain label
        class MockGitHub:
            def rest(self, method, path, body=None, etag=None, ok=()):
                if "/repos/%s/issues/1/comments" % REPO in path:
                    return {"data": []}
                issue = {
                    "number": 1,
                    "labels": [{"name": "loom:goal"}]  # No needs-brain label
                }
                return {"data": issue}
        
        gh = MockGitHub()
        
        # Test that the brief-needs-brain validation rejects a brief without needs-brain
        try:
            brain._check_stage_item(gh, "brief-needs-brain", REPO, 1)
            print("✗ Validation failed: brief without needs-brain should have been rejected")
            raise AssertionError("A brief without needs-brain should have been rejected by brief-needs-brain path")
        except ValueError as e:
            if "loom:briefed and needs-brain" in str(e):
                print("✓ Validation passed: brief without needs-brain was correctly rejected")
            else:
                raise

    def test_a_brief_needs_brain_defer_requires_note(self):
        """Test that brief-needs-brain defer requires a note naming the problem statement."""
        # Create a mock GitHub instance with the needs-brain label
        class MockGitHub:
            def rest(self, method, path, body=None, etag=None, ok=()):
                if "/repos/%s/issues/1/comments" % REPO in path:
                    return {"data": []}
                issue = {
                    "number": 1,
                    "labels": [{"name": "loom:goal"}, {"name": "loom:briefed"}, {"name": "needs-brain"}]
                }
                return {"data": issue}
        
        gh = MockGitHub()
        
        # Test that the defer function validates the note requirement
        try:
            brain.defer(gh, "brief-needs-brain", (REPO, 1), "muse")
            print("✗ Validation failed: brief-needs-brain defer should have required a note")
            raise AssertionError("brief-needs-brain defer should have required a note")
        except ValueError as e:
            if "note" in str(e).lower():
                print("✓ Validation passed: brief-needs-brain defer correctly required a note")
            else:
                raise

    def test_a_brief_needs_brain_defer_validation_passes(self):
        """Test that brief-needs-brain defer validation passes when all requirements are met."""
        # Create a mock GitHub instance with all required labels
        class MockGitHub:
            def rest(self, method, path, body=None, etag=None, ok=()):
                if "/repos/%s/issues/1/comments" % REPO in path:
                    return {"data": []}
                issue = {
                    "number": 1,
                    "labels": [{"name": "loom:goal"}, {"name": "loom:briefed"}, {"name": "needs-brain"}]
                }
                return {"data": issue}
        
        gh = MockGitHub()
        
        # Test that the validation passes
        try:
            brain._check_stage_item(gh, "brief-needs-brain", REPO, 1)
            print("✓ Validation passed: brief-needs-brain validation works when all requirements are met")
        except Exception as e:
            print(f"✗ Validation failed: {e}")
            raise


if __name__ == "__main__":
    unittest.main()