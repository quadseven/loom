"""python3 -m dashboard listens on LOOM_DASHBOARD_HOST when it is set, and on
every interface when it is not (issue 678)."""

import os
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


class DashboardHostTest(unittest.TestCase):
    def serve(self, **values):
        with _env(LOOM_DASHBOARD_PORT="0", **values):
            server = server_from_env(store=_Store())
        self.addCleanup(server.server_close)
        return server

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
        for bad in ("127.0.0.1; true", "dashboard.example", "127.0.0.1 10.0.0.1", "::1", "300.1.1.1"):
            with self.subTest(bad=bad), _env(LOOM_DASHBOARD_PORT="0", LOOM_DASHBOARD_HOST=bad):
                with self.assertRaisesRegex(ValueError, "LOOM_DASHBOARD_HOST"):
                    server_from_env(store=_Store())

    def test_memory_transcripts_are_announced_in_the_log(self):
        with self.assertLogs("dashboard.__main__", level="WARNING") as logs:
            self.serve(LOOM_TRANSCRIPTS_MEMORY="1")
        self.assertIn("LOOM_TRANSCRIPTS_MEMORY=1", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
