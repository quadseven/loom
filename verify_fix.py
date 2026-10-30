#!/usr/bin/env python3

import os
import sys
import tempfile

# Add the tests directory to PYTHONPATH so shadow_env can be imported
os.environ['PYTHONPATH'] = './tests:' + os.environ.get('PYTHONPATH', '')

sys.path.insert(0, './tests')
sys.path.insert(0, './bus')

# Mock discord module for testing
class DiscordError(Exception):
    pass

class MockDiscord:
    def __init__(self):
        self.calls = []
    
    def __call__(self, method, path, token, body):
        self.calls.append((method, path, token, body))
        raise DiscordError("Mock discord failure")

# Create mock discord in station module
import bus.station
bus.station.DiscordError = DiscordError
bus.station.discord = MockDiscord()

# Mock the necessary modules
from unittest import mock
import bus.github as github_module

# Create a temporary directory for the station
tmpdir = tempfile.TemporaryDirectory()
home = os.path.join(tmpdir.name, "loom")
os.makedirs(os.path.dirname(home), exist_ok=True)

# Create test configuration
settings = bus.station.Settings(home, ["example-owner/unreadable", "example-owner/example-repo"], 
                                 ["alice-example"], ["example-owner"])

# Create fake github
from tests.test_bus_station import FakeGitHub
fake = FakeGitHub()

# Add a goal to the REPO
fake.add_goal(40, journals=[], title="Goal")
fake.comments[40] = [{"body": "<!-- loom:report v1 -->\nold report", 
                      "user": {"login": "alice-example"},
                      "created_at": "2030-01-01T00:00:00Z"}]
fake.comments[40][0]["created_at"] = "2030-01-01T00:00:00Z"

# Create unreadable GitHub
class UnreadableGitHub:
    def __call__(self, method, url, headers, body):
        from urllib.parse import urlsplit
        path = urlsplit(url).path
        if path == "/repos/example-owner/unreadable/issues":
            from tests.test_bus_station import reply
            return reply(404, {"message": "Not Found"})
        # For other paths, delegate to fake
        return fake(method, url, headers, body)

# Create station
station = bus.station.Station(UnreadableGitHub(), settings, now=1700000000.0)

# Track the log calls
logged = []
def track_say(msg):
    logged.append(msg)

with mock.patch('bus.station._say', side_effect=track_say):
    # Run post_overdue_standups
    station.post_overdue_standups()

# Verify results
print(f"Logged messages: {logged}")
print(f"Number of logged messages: {len(logged)}")

# Check if at least one "standup: skipped" was logged
skipped = [msg for msg in logged if msg.startswith("standup: skipped")]
print(f"Skipped messages: {skipped}")

# Check if comment was posted
print(f"Comments on goal 40: {len(fake.comments[40])}")

# Expected behavior:
# 1. A "standup: skipped example-owner/unreadable: Not Found" message is logged
# 2. A report comment is posted on goal 40 (from example-owner/example-repo)

if skipped and any('example-owner/unreadable' in msg for msg in skipped) and any('404' in msg or 'Not Found' in msg for msg in skipped):
    print("✓ UNREADABLE repo was skipped with appropriate error message")
else:
    print("✗ UNREADABLE repo was not properly skipped")

if len(fake.comments[40]) > 1:
    print("✓ Report was posted on goal home in other repo")
else:
    print("✗ No report was posted on goal home")

# Cleanup
tmpdir.cleanup()
