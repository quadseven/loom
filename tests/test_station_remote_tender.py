"""Remote checks are tended on their own cadence while a pass runs."""
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bus import remote_checks, station


class Tender(unittest.TestCase):
    def test_a_request_filed_mid_pass_is_tended_without_the_pass_step(self):
        ticks = threading.Event()
        calls = []
        with tempfile.TemporaryDirectory() as home:
            remote_checks.submit(home, "example/repo", "a" * 40, "", "no", "", "true")
            runner = station.Station.__new__(station.Station)

            def tend(now=None):
                calls.append(now)
                ticks.set()

            runner.tend_remote_checks = tend
            with mock.patch.object(station, "REMOTE_TEND_SECONDS", 0.01):
                runner.start_remote_tender()
                try:
                    self.assertTrue(ticks.wait(5))
                finally:
                    runner.stop_remote_tender()
            self.assertIsNotNone(calls[0])

    def test_stopping_ends_the_thread(self):
        runner = station.Station.__new__(station.Station)
        runner.tend_remote_checks = lambda now=None: None
        with mock.patch.object(station, "REMOTE_TEND_SECONDS", 0.01):
            runner.start_remote_tender()
            thread = runner._remote_thread
            runner.stop_remote_tender()
            thread.join(5)
            self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
