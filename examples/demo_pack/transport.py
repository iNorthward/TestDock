"""Fake HTTP transport that never opens a socket."""
from __future__ import annotations

from urllib.parse import urlsplit


class MockTransport:
    def __init__(self):
        self.calls = []

    def request(self, method, url, headers, *, data=None, timeout=30):
        path = urlsplit(url).path
        self.calls.append({"method": method, "path": path, "headers": dict(headers)})
        if method.upper() == "GET" and path == "/health" and data is None:
            return 200, {"ok": True, "data": {"status": "ready", "source": "mock"}}
        return 404, {"ok": False, "code": 404, "message": "mock route not found"}
