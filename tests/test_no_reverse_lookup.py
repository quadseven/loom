"""Servers bind without a reverse-DNS lookup (issue 1188).

HTTPServer.server_bind calls socket.getfqdn on the bound host. That lookup is
instant on a laptop and takes tens of seconds per server on the macOS runner,
which put test_dashboard at 610s on CI against 15s locally."""

import re
import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest.mock import patch

from dashboard.server import make_server
from tests.local_http import LocalHTTPServer

TESTS = Path(__file__).resolve().parent


class _Store:
    """The two TicketStore calls make_server makes; no background reads."""

    def start_background(self):
        pass

    def stop_background(self):
        pass


def _no_lookup(*args):
    raise AssertionError("socket.getfqdn called while binding a server")


class NoReverseLookupTest(unittest.TestCase):
    def test_the_dashboard_server_binds_without_a_reverse_lookup(self):
        with patch("socket.getfqdn", _no_lookup):
            server = make_server(port=0, store=_Store())
        try:
            self.assertEqual(server.server_port, server.server_address[1])
            self.assertNotEqual(server.server_port, 0)
        finally:
            server.server_close()

    def test_the_test_server_binds_without_a_reverse_lookup(self):
        with patch("socket.getfqdn", _no_lookup):
            server = LocalHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        try:
            self.assertEqual(server.server_name, "127.0.0.1")
            self.assertEqual(server.server_port, server.server_address[1])
        finally:
            server.server_close()

    def test_no_test_builds_a_server_that_looks_itself_up(self):
        bare = re.compile(r"(?<![\w.])ThreadingHTTPServer\(\(")
        offenders = sorted(p.name for p in TESTS.glob("*.py")
                           if p.name != Path(__file__).name and bare.search(p.read_text()))
        self.assertEqual(offenders, [], "use local_http.LocalHTTPServer instead")


if __name__ == "__main__":
    unittest.main()
