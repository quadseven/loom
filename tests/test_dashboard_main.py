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
from tests import sandbox_caps


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


def port_free_on_other_address(start_dashboard, address, attempts=5):
    """True when some dashboard, started by start_dashboard, leaves its port
    number free on address.

    A refused bind is ambiguous: the dashboard may hold the port on that
    address, or another process may hold the same number there (a parallel
    test run binds many ephemeral ports). Only a refusal on every attempt,
    each against a fresh dashboard on a new port, shows the first."""
    for _ in range(attempts):
        port = start_dashboard().server_address[1]
        with socket.socket() as probe:
            try:
                probe.bind((address, port))
            except OSError:
                continue
        return True
    return False


class _ServerTest(unittest.TestCase):
    """A dashboard server on an ephemeral port, with no background reads."""

    def serve(self, **values):
        if not sandbox_caps.LOCALHOST:
            self.skipTest(sandbox_caps.NO_LOCALHOST)
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
        address = self.other_address()
        self.assertTrue(
            port_free_on_other_address(
                lambda: self.serve(LOOM_DASHBOARD_HOST="127.0.0.1"), address),
            "the dashboard holds the same port on %s as well, so it serves that "
            "interface too" % address)

    def test_without_an_address_the_other_interfaces_are_open(self):
        server = self.serve()
        address = self.other_address()
        port = server.server_address[1]
        with socket.socket() as probe:
            with self.assertRaises(OSError) as caught:
                probe.bind((address, port))
        self.assertEqual(caught.exception.errno, errno.EADDRINUSE)


class _Dashboard:
    """A stand-in dashboard: only the port it reports."""

    def __init__(self, port):
        self.server_address = ("127.0.0.1", port)


class PortProbeTest(_ServerTest):
    """port_free_on_other_address tells a port held by the dashboard from a
    port held by some other process (issue 2916)."""

    def free_port(self):
        """A loopback port number nothing holds right now."""
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]

    def test_a_foreign_socket_on_the_dashboards_port_number_is_not_a_failure(self):
        if not sandbox_caps.LOCALHOST:
            self.skipTest(sandbox_caps.NO_LOCALHOST)
        foreign = socket.socket()
        self.addCleanup(foreign.close)
        foreign.bind(("127.0.0.1", 0))
        foreign.listen()
        self.assertTrue(port_free_on_other_address(
            self.ports_after(foreign.getsockname()[1]), "127.0.0.1"))

    def ports_after(self, first):
        """A dashboard starter whose first dashboard reports first, and every
        later one a port free when it is asked for. It never runs out: a
        parallel process may take a free port before the probe binds it, and
        the check then asks for another dashboard, as it may up to its
        attempts (codex review of a8f3ac21)."""
        given = []

        def start():
            given.append(1)
            return _Dashboard(first if len(given) == 1 else self.free_port())

        return start

    def test_the_starter_never_runs_out_while_later_ports_are_taken(self):
        # Every later port is held by another process here, so each attempt
        # after the first is refused too: the check must end False, not raise.
        if not sandbox_caps.LOCALHOST:
            self.skipTest(sandbox_caps.NO_LOCALHOST)
        foreign = socket.socket()
        self.addCleanup(foreign.close)
        foreign.bind(("127.0.0.1", 0))
        foreign.listen()
        held = foreign.getsockname()[1]
        with patch.object(self, "free_port", return_value=held):
            self.assertFalse(port_free_on_other_address(
                self.ports_after(held), "127.0.0.1"))

    def test_a_port_refused_on_every_attempt_is_a_failure(self):
        if not sandbox_caps.LOCALHOST:
            self.skipTest(sandbox_caps.NO_LOCALHOST)
        foreign = socket.socket()
        self.addCleanup(foreign.close)
        foreign.bind(("127.0.0.1", 0))
        foreign.listen()
        started = []

        def start():
            started.append(1)
            return _Dashboard(foreign.getsockname()[1])

        self.assertFalse(port_free_on_other_address(start, "127.0.0.1", attempts=3))
        self.assertEqual(len(started), 3)

    def test_a_dashboard_on_every_interface_still_fails_the_check(self):
        address = _other_interface_address()
        if address is None:
            self.skipTest("this machine has no other interface to probe")
        self.assertFalse(port_free_on_other_address(self.serve, address))


if __name__ == "__main__":
    unittest.main()
