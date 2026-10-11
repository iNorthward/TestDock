"""Loopback panel request policy; enforced before any Core or Pack route."""
from __future__ import annotations

import secrets
from urllib.parse import urlsplit

MAX_BODY_BYTES = 1024 * 1024


class RequestRejected(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def authorize(headers, *, port, token, api=False, session=False, mutation=False):
    authorities = {f"127.0.0.1:{port}", f"localhost:{port}"}
    host = headers.get("Host", "").lower()
    if host not in authorities:
        raise RequestRejected(403, "Host 不允许访问")
    if headers.get("Sec-Fetch-Site") in ("cross-site", "same-site"):
        raise RequestRejected(403, "仅允许同源访问")
    for name in ("Origin", "Referer"):
        raw = headers.get(name)
        if raw is not None:
            parsed = urlsplit(raw)
            if (parsed.scheme != "http" or parsed.netloc.lower() != host
                    or parsed.username is not None or parsed.password is not None):
                raise RequestRejected(403, "仅允许同源访问")
    if api and not session:
        supplied = headers.get("X-TestDock-Token", "")
        if not secrets.compare_digest(supplied.encode(), token.encode()):
            raise RequestRejected(403, "面板会话凭据无效，请刷新页面")
    if mutation:
        content_type = headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise RequestRejected(415, "请求须使用 application/json")
        if headers.get("Transfer-Encoding"):
            raise RequestRejected(400, "不支持 Transfer-Encoding")
        raw_length = headers.get("Content-Length", "0")
        if not raw_length.isascii() or not raw_length.isdecimal():
            raise RequestRejected(400, "Content-Length 无效")
        if int(raw_length) > MAX_BODY_BYTES:
            raise RequestRejected(413, "请求体超过 1 MiB")
