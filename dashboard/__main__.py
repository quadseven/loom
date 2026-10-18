"""Run the dashboard HTTP server."""
import ipaddress
import os
import logging

from .server import make_server

log = logging.getLogger(__name__)


def server_from_env(store=None):
    """The dashboard server on LOOM_DASHBOARD_HOST (default: every interface)
    and LOOM_DASHBOARD_PORT (default 8080). The host is empty or one IPv4
    address (the server is IPv4 only); anything else is refused, never bound."""
    host = os.environ.get("LOOM_DASHBOARD_HOST", "").strip()
    if host:
        try:
            ipaddress.IPv4Address(host)
        except ValueError:
            raise ValueError("LOOM_DASHBOARD_HOST must be empty or one IPv4 address, got %r"
                             % host) from None
    port = int(os.environ.get("LOOM_DASHBOARD_PORT", "8080"))
    if os.environ.get("LOOM_TRANSCRIPTS_MEMORY") == "1":
        log.warning("transcripts are kept in memory (LOOM_TRANSCRIPTS_MEMORY=1); "
                    "they are lost when the dashboard restarts")
    server = make_server(host=host, port=port, store=store)
    log.info("dashboard listening on %s port %d",
             host or "every interface", server.server_address[1])
    return server


def main():
    logging.basicConfig(level=logging.INFO)
    server = server_from_env()
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
