"""Offline adapters used by the portable demo pack."""
from __future__ import annotations

from typing import Any


class DemoTokenProvider:
    def get_token(self, username: str, tenant_id: str) -> str:
        return f"offline:{username}@{tenant_id}"


class DemoRequestAuth:
    def build_headers(self, token=None, *, tenant=None, with_token=True, extra=None, **_):
        headers = {"x-demo-tenant": str(tenant or "")}
        if with_token and token:
            headers["x-demo-token"] = str(token)
        if extra:
            headers.update(extra)
        return headers

    @staticmethod
    def validate_headers(headers):
        if "x-demo-tenant" not in headers:
            raise ValueError("demo tenant header is required")


class DemoResponseEnvelope:
    @staticmethod
    def is_ok(response) -> bool:
        return isinstance(response, dict) and response.get("ok") is True

    @staticmethod
    def is_reject(response, code=None) -> bool:
        if not isinstance(response, dict) or response.get("ok") is not False:
            return False
        return code is None or response.get("code") == code

    @staticmethod
    def business_code(response):
        return response.get("code") if isinstance(response, dict) else None

    @staticmethod
    def message(response) -> str:
        return str(response.get("message") or "") if isinstance(response, dict) else ""

    @staticmethod
    def data(response):
        return response.get("data") if isinstance(response, dict) else None

    @staticmethod
    def normalize_code(value):
        if isinstance(value, bool) or value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def validate_envelope(response) -> tuple[bool, str]:
        if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
            return False, "demo response requires a boolean ok field"
        return True, "demo envelope is valid"


def install() -> None:
    """Register deterministic, offline-only adapters before case discovery."""
    from pathlib import Path
    import sys

    root = Path(__file__).resolve().parents[2]
    common = root / "test-platform" / "common"
    for path in (root, common):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))

    from api_client import register_request_auth, register_token_provider
    from response_envelope import register_response_envelope

    register_token_provider(DemoTokenProvider())
    register_request_auth(DemoRequestAuth())
    register_response_envelope(DemoResponseEnvelope())
