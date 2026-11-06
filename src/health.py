"""health.py - a tiny HTTP endpoint so hosting platforms and uptime monitors can see the bot.

Why the bot needs a web page at all: it receives Telegram messages by long polling, which
only makes OUTGOING requests. Web hosts (Render) expect something listening on a port, and
free plans put an app to sleep when it gets no inbound requests for a while. This endpoint
gives them that, and doubles as a liveness check:

    GET or HEAD  /  or  /health   ->  200 "ok" while the bot is running, 503 otherwise

An uptime monitor that requests it every few minutes both keeps the app awake and emails
you if the bot ever stops. Nothing else is served and nothing about the bot (users, tokens,
counts) is ever included in a response.

It is started only when HEALTH_PORT (or the PORT variable that hosts like Render set) is
present in the environment (see bot.main), so running the bot locally opens no port.
"""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class BotStatus:
    """Whether the polling loop is up. bot.post_init sets it, bot.post_shutdown clears it."""

    def __init__(self):
        self.running = False

    def set_running(self, value):
        self.running = bool(value)


STATUS = BotStatus()


def make_handler(status):
    class HealthHandler(BaseHTTPRequestHandler):
        server_version = "ScrollPingHealth"
        sys_version = ""

        def _respond(self, send_body):
            path = self.path.split("?", 1)[0]

            if path in ("/", "/health"):
                if status.running:
                    code, body = 200, b"ok\n"
                else:
                    code, body = 503, b"bot not running\n"
            else:
                code, body = 404, b"not found\n"

            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if send_body:
                self.wfile.write(body)

        def do_GET(self):
            self._respond(send_body=True)

        def do_HEAD(self):
            self._respond(send_body=False)

        def log_message(self, format, *args):
            pass  # a monitor pinging every few minutes would otherwise flood the logs

    return HealthHandler


def start_health_server(status, port, host="0.0.0.0"):
    """Start serving in a background (daemon) thread and return the server object.
    Pass port=0 to let the OS choose a free port (read it back from server.server_address)."""
    server = ThreadingHTTPServer((host, port), make_handler(status))
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.1}, name="health-server", daemon=True
    )
    thread.start()
    return server