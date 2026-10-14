"""Run the dashboard HTTP server."""
import os

from .server import make_server


def main():
    port = int(os.environ.get("LOOM_DASHBOARD_PORT", "8080"))
    server = make_server(port=port)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
