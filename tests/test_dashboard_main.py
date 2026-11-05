"""python3 -m dashboard listens on LOOM_DASHBOARD_HOST when it is set, and on
every interface when it is not (issue 678)."""

import errno
import fcntl
import os
import socket
import struct
import sys
import unittest
from unittest.mock import patch

from dashboard.__main__ import server_from_env


class _Store:
    """The two TicketStore calls make_server makes; no background reads."""

    def start_background(self):
        pass

    def stop_background(self):
        pass


def _env(**values):
    env = {k: v for k, v in os.environ.items()
           if k not in ("LOOM_DASHBOARD_HOST", "LOOM_DASHBOARD_PORT", "LOOM_TRANSCRIPTS_MEMORY")}
    env.update(values)
    return patch.dict(os.environ, env, clear=True)


def _interface_addresses():
    """The IPv4 addresses this machine's network interfaces carry.

    The kernel is asked, not a name resolver, so a machine whose hostname
    does not resolve still knows its own addresses. An interface that carries
    no IPv4 address answers with an error, and is skipped."""
    request = 0x8915 if sys.platform.startswith("linux") else 0xc0206921  # SIOCGIFADDR
    found = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        for _, name in socket.if_nameindex():
            try:
                raw = fcntl.ioctl(probe.fileno(), request,
                                  struct.pack("256s", name[:15].encode()))
                address = socket.inet_ntoa(raw[20:24])
            except (OSError, UnicodeError):
                continue
            if address not in found:
                found.append(address)
    return found


def _other_interface_address():
    """One IPv4 address of this machine that is not loopback and that this
    process may bind, or None when it has none.

    An address no interface carries cannot be bound, so it cannot be probed."""
    for address in _interface_addresses():
        if address.startswith("127."):
            continue
        with socket.socket() as probe:
            try:
                probe.bind((address, 0))
            except OSError:
                continue
        return address
    return None


class _ServerTest(unittest.TestCase):
    """A dashboard server on an ephemeral port, with no background reads."""

    def serve(self, **values):
        with _env(LOOM_DASHBOARD_PORT="0", **values):
            server = server_from_env(store=_Store())
        self.addCleanup(server.server_close)
        return server


class DashboardHostTest(_ServerTest):
    def test_a_given_host_is_the_only_address_bound(self):
        server = self.serve(LOOM_DASHBOARD_HOST="127.0.0.1")
        self.assertEqual(server.server_address[0], "127.0.0.1")
        self.assertNotEqual(server.server_address[1], 0)

    def test_without_a_host_every_interface_is_bound(self):
        server = self.serve()
        self.assertEqual(server.server_address[0], "0.0.0.0")  # noqa: S104 - asserts the default

    def test_a_blank_host_is_every_interface(self):
        server = self.serve(LOOM_DASHBOARD_HOST="  ")
        self.assertEqual(server.server_address[0], "0.0.0.0")  # noqa: S104 - asserts the default

    def test_a_host_that_is_not_one_ipv4_address_is_refused(self):
        for bad in ("127.0.0.1; true", "dashboard.example", "127.0.0.1 10.0.0.1", "01.2.3.4", "10.0.0.08", "::1", "300.1.1.1"):
            with self.subTest(bad=bad), _env(LOOM_DASHBOARD_PORT="0", LOOM_DASHBOARD_HOST=bad):
                with self.assertRaisesRegex(ValueError, "LOOM_DASHBOARD_HOST"):
                    server_from_env(store=_Store())

    def test_memory_transcripts_are_announced_in_the_log(self):
        with self.assertLogs("dashboard.__main__", level="WARNING") as logs:
            self.serve(LOOM_TRANSCRIPTS_MEMORY="1")
        self.assertIn("LOOM_TRANSCRIPTS_MEMORY=1", "\n".join(logs.output))


class DashboardInterfaceTest(_ServerTest):
    """The dashboard serves only the address it is given: the port is closed on
    the machine's other interfaces (issue 678).

    A listening socket holds its address and port, so the port is free on
    every other interface and taken on the one it was given."""

    def other_address(self):
        address = _other_interface_address()
        if address is None:
            self.skipTest("this machine has no other interface to probe")
        return address

    def test_a_given_address_leaves_the_other_interfaces_closed(self):
        server = self.serve(LOOM_DASHBOARD_HOST="127.0.0.1")
        address = self.other_address()
        port = server.server_address[1]
        with socket.socket() as probe:
            try:
                probe.bind((address, port))
            except OSError as exc:
                self.fail("the dashboard holds %s:%d as well, so it serves that "
                          "interface too (%s)" % (address, port, exc))

    def test_without_an_address_the_other_interfaces_are_open(self):
        server = self.serve()
        address = self.other_address()
        port = server.server_address[1]
        with socket.socket() as probe:
            with self.assertRaises(OSError) as caught:
                probe.bind((address, port))
        self.assertEqual(caught.exception.errno, errno.EADDRINUSE)


if __name__ == "__main__":
    unittest.main()
