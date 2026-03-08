"""Lightweight HTTP health endpoint for container monitoring."""

from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

LOGGER = logging.getLogger("daft_monitor.health")

_DEFAULT_PORT = 8080


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        """Suppress default stderr logging — handled by the app logger."""


class HealthServer:
    """Runs an HTTP health endpoint in a daemon thread."""

    def __init__(self, port: int = _DEFAULT_PORT) -> None:
        self._port = port
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._server = HTTPServer(("0.0.0.0", self._port), _HealthHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        LOGGER.info("Health endpoint started on port %d", self._port)

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            LOGGER.info("Health endpoint stopped")
