"""A ThreadingHTTPServer for tests that skips the reverse-DNS lookup.

HTTPServer.server_bind sets server_name from socket.getfqdn(host). On the
macOS runner that lookup takes tens of seconds per server (issue 1188); no
test reads server_name, so bind the socket and record the address as is."""

import socketserver
from http.server import ThreadingHTTPServer


class LocalHTTPServer(ThreadingHTTPServer):
    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]
