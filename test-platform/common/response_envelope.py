"""Core protocol and registry for project-specific response envelopes."""
from __future__ import annotations

from typing import Any, Protocol


class ResponseEnvelope(Protocol):
    def is_ok(self, response: Any) -> bool: ...

    def is_reject(self, response: Any, code=None) -> bool: ...

    def business_code(self, response: Any): ...

    def message(self, response: Any) -> str: ...

    def data(self, response: Any): ...

    def normalize_code(self, value): ...

    def validate_envelope(self, response) -> tuple[bool, str]: ...


_ENVELOPE: ResponseEnvelope | None = None


def register_response_envelope(envelope: ResponseEnvelope | None):
    """Set the active adapter and return the previous one for scoped tests."""
    global _ENVELOPE
    previous = _ENVELOPE
    _ENVELOPE = envelope
    return previous


def get_response_envelope() -> ResponseEnvelope | None:
    return _ENVELOPE
