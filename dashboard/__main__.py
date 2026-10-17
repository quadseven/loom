"""Run the dashboard HTTP server."""
import os
import logging

from .server import make_server


def main():
    logging.basicConfig(level=logging.INFO)
    port = int(os.environ.get("LOOM_DASHBOARD_PORT", "8080"))
    server = make_server(port=port)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
