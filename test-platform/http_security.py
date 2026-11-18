"""Loopback panel request policy; enforced before any Core or Pack route."""
from __future__ import annotations

import secrets
from urllib.parse import urlsplit

MAX_BODY_BYTES = 1024 * 1024


class RequestRejected(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def authorize(headers, *, port, token, api=False, session=False, mutation=False, panel_navigation=False):
    if hasattr(headers, 'get_all'):
        for name in ('Host','Origin','Referer','Sec-Fetch-Site','X-TestDock-Token',
                     'Sec-Fetch-Dest','Sec-Fetch-Mode','Content-Type','Content-Length','Transfer-Encoding'):
            if len(headers.get_all(name, [])) > 1:
                raise RequestRejected(400, "请求保护头不得重复")
    authorities = {f"127.0.0.1:{port}", f"localhost:{port}"}
    host = headers.get("Host", "").lower()
    if host not in authorities:
        raise RequestRejected(403, "Host 不允许访问")
    panel_navigation = (panel_navigation and not api and not session and not mutation
                        and headers.get("Sec-Fetch-Dest") == "document"
                        and headers.get("Sec-Fetch-Mode") == "navigate")
    allowed_sources = authorities if panel_navigation else {host}
    has_source = False
    for name in ("Origin", "Referer"):
        raw = headers.get(name)
        if raw is not None:
            if any(ord(char) < 32 or ord(char) == 127 for char in raw):
                raise RequestRejected(403, "来源 URL 无效")
            try:
                parsed = urlsplit(raw)
            except ValueError as exc:
                raise RequestRejected(403, "来源 URL 无效") from exc
            if (parsed.scheme != "http" or parsed.netloc.lower() not in allowed_sources
                    or parsed.username is not None or parsed.password is not None):
                raise RequestRejected(403, "仅允许同源访问")
            has_source = True
    # Browser fetch metadata may retain the whole redirect chain's site relationship.
    if headers.get("Sec-Fetch-Site") in ("cross-site", "same-site") and not (panel_navigation and has_source):
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
        if len(raw_length) > 20 or not raw_length.isascii() or not raw_length.isdecimal():
            raise RequestRejected(400, "Content-Length 无效")
        if int(raw_length) > MAX_BODY_BYTES:
            raise RequestRejected(413, "请求体超过 1 MiB")
