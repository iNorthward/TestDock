"""Core registry for optional Pack response-data validation and envelopes."""
from __future__ import annotations

from response_envelope import get_response_envelope

_DATA_VALIDATOR = None


def register_response_data_validator(validator):
    """Install the selected Pack hook; return the previous hook for scoped tests."""
    if validator is not None and not callable(validator):
        raise TypeError("响应数据校验 hook 须为 callable 或 None")
    global _DATA_VALIDATOR
    previous = _DATA_VALIDATOR
    _DATA_VALIDATOR = validator
    return previous


def validate_response_data(data, *, rules=None):
    """Apply only explicitly registered project rules; Core infers no fields."""
    if _DATA_VALIDATOR is None:
        return True, ""
    return _DATA_VALIDATOR(data, rules=dict(rules or {}))


def validate_response_envelope(response):
    """Validate the active project's response envelope contract."""
    envelope = get_response_envelope()
    if envelope is None:
        return False, "未注册 ResponseEnvelope adapter"
    return envelope.validate_envelope(response)
