#!/usr/bin/env python3

import os
import sys

# Add the tests directory to PYTHONPATH so shadow_env can be imported
os.environ['PYTHONPATH'] = './tests:' + os.environ.get('PYTHONPATH', '')

# Import the test module
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

# Now import and run the test
from test_bus_station import ReportsTest, Base

# Run the specific test
import unittest
import io
import contextlib

# Create a test suite with just the test we want
suite = unittest.TestLoader().loadTestsFromTestCase(ReportsTest)

# Filter to get only the test we want
filtered_suite = unittest.TestSuite()
for test in suite:
    if hasattr(test, '_testMethodName'):
        method_name = test._testMethodName
        if 'test_one_unreadable_repo' in method_name:
            filtered_suite.addTest(test)

# Run the test
runner = unittest.TextTestRunner(stream=io.StringIO(), verbosity=2)
result = runner.run(filtered_suite)

# Print the result
print(result)
