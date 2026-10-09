"""Strict JSON predicates and explicitly registered project wire rules."""
from __future__ import annotations

import re
import json
import math
from functools import lru_cache
from pathlib import Path
from datetime import date, datetime
from decimal import Decimal, InvalidOperation


_PROJECT_WIRE_RULES = {}


def register_wire_rule(name, validator):
    """Register a Pack rule; return its previous validator for scoped tests."""
    builtin = {"int64_string", "decimal_string", "json_integer", "json_number", "decimal_text"}
    if not isinstance(name, str) or not name.isidentifier() or name in builtin:
        raise ValueError("项目 wire 规则名称无效或覆盖平台内置规则")
    if validator is not None and not callable(validator):
        raise TypeError("wire validator 须为 callable 或 None")
    previous = _PROJECT_WIRE_RULES.get(name)
    if validator is None:
        _PROJECT_WIRE_RULES.pop(name, None)
    else:
        _PROJECT_WIRE_RULES[name] = validator
    return previous


_INT64_MAX = 2**63 - 1
_PLAIN_DECIMAL = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")


class JsonDecimal(Decimal):
    """An exact fractional JSON number, distinct from an unvalidated DB Decimal."""
    def __new__(cls, token):
        if not isinstance(token, str) or not re.fullmatch(
                r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?', token, flags=re.ASCII):
            raise ValueError('JsonDecimal 只接受原始 JSON 数值 token')
        value = super().__new__(cls, token)
        value.json_token = token
        return value


def lossless_json_loads(text):
    """Explicit transport strategy; integers stay int and strings stay strings."""
    def invalid_constant(token):
        raise json.JSONDecodeError('非法 JSON 数值常量 ' + token, text, 0)
    def number(token):
        try:
            return JsonDecimal(token)
        except (InvalidOperation, ValueError, OverflowError):
            raise json.JSONDecodeError('JSON 数值超出精确解码器支持范围', text, 0) from None
    return json.loads(text, parse_float=number, parse_constant=invalid_constant)


def int64_string(value) -> int | None:
    """Parse only a canonical signed int64 JSON string."""
    if not isinstance(value, str) or not re.fullmatch(r"-?(?:0|[1-9][0-9]*)", value, flags=re.ASCII):
        return None
    try:
        parsed = int(value)
    except (ValueError, OverflowError):
        return None
    return parsed if -(2**63) <= parsed <= _INT64_MAX and str(parsed) == value else None


def non_negative_int64_string(value) -> int | None:
    parsed = int64_string(value)
    return parsed if parsed is not None and parsed >= 0 else None


def json_integer(value) -> int | None:
    """Validate a JSON integer, excluding booleans and numeric strings."""
    return value if type(value) is int and -(2**63) <= value <= _INT64_MAX else None


def is_json_integer(value) -> bool:
    return json_integer(value) is not None


def json_number_amount(value) -> Decimal | None:
    """Validate number decoding; plain DB Decimal, bool and strings are rejected."""
    if type(value) is JsonDecimal:
        return Decimal(value) if value.is_finite() else None
    if type(value) not in (int, float) or (type(value) is float and not math.isfinite(value)):
        return None
    try:
        parsed = Decimal(str(value))
        return parsed if parsed.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


@lru_cache(maxsize=8)
def _load_wire_manifest(path_text, mtime_ns, size):
    # Cache by configured source/version, never by schema name alone.
    path = Path(path_text)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"[incomplete] 无法读取 JSON wire 清单 {path}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("schemas"), dict):
        raise ValueError(f"[incomplete] JSON wire 清单缺少 schemas 对象: {path}")
    return payload["schemas"]


def _wire_manifest():
    from scripts.project_inputs import project_input_path

    path = project_input_path("PLATFORM_JSON_WIRE_MANIFEST")
    try:
        stat = path.stat()
    except OSError as exc:
        raise ValueError(f"[incomplete] JSON wire 清单不存在或不可读: {path}") from exc
    return _load_wire_manifest(str(path.resolve()), stat.st_mtime_ns, stat.st_size)


def schema_wire_errors(value, schema_name, *, _path="", _depth=0) -> list[str]:
    """Validate present numeric fields against source-derived 项目 wire facts.

    Required fields/nullability and business ranges remain explicit case assertions.
    Nested model references reuse the same strict checks.
    """
    if _depth > 30:
        return [f"{_path or schema_name}: 响应嵌套过深"]
    models = _wire_manifest()
    if schema_name not in models:
        raise ValueError(f"缺少 项目 wire 元信息：{schema_name}")
    if not isinstance(value, dict):
        return [f"{_path or schema_name}: 不是 JSON object"]
    model = models[schema_name]
    parsers = {"int64_string": int64_string, "decimal_string": finite_decimal_string,
               "json_integer": json_integer, "json_number": json_number_amount,
               "decimal_text": finite_decimal_text, **_PROJECT_WIRE_RULES}
    errors = []
    for field, facts in model["fields"].items():
        rule = facts["wire"]
        if rule not in parsers:
            raise ValueError(f"[incomplete] 所选 Pack 未注册 wire 规则: {rule}")
        if field in value and value[field] is not None and parsers[rule](value[field]) is None:
            errors.append(f"{_path + field}: 须为 {facts['wire']}")
    for field, ref in model["refs"].items():
        child = value.get(field)
        if child is None or ref["schema"] not in models:
            continue
        if ref["array"]:
            if not isinstance(child, list):
                errors.append(f"{_path + field}: 不是 JSON array")
                continue
            for index, item in enumerate(child):
                errors.extend(schema_wire_errors(item, ref["schema"], _path=f"{_path}{field}[{index}].", _depth=_depth + 1))
        else:
            errors.extend(schema_wire_errors(child, ref["schema"], _path=f"{_path}{field}.", _depth=_depth + 1))
    return errors


def positive_int64_string(value) -> int | None:
    """Return an ID value only when it is a canonical positive int64 JSON string."""
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        return None
    try:
        parsed = int(value)
    except (ValueError, OverflowError):
        return None
    if parsed <= 0 or parsed > _INT64_MAX or str(parsed) != value:
        return None
    return parsed


def finite_decimal_string(value) -> Decimal | None:
    """Parse the plain decimal string expressed as a plain decimal string."""
    if not isinstance(value, str) or not _PLAIN_DECIMAL.fullmatch(value):
        return None
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def api_date(value) -> date | None:
    """Parse an API date serialized as yyyy-MM-dd or a date-time string."""
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        return date.fromisoformat(raw)
    except ValueError:
        pass
    normalized = raw.replace("Z", "+00:00")
    if " " in normalized and "T" not in normalized:
        normalized = normalized.replace(" ", "T", 1)
    try:
        return datetime.fromisoformat(normalized).date()
    except ValueError:
        return None


def finite_decimal_text(value) -> Decimal | None:
    """Finite JSON string from default decimal toString serialization (may use exponent).

    Only source/response-verified unannotated decimal fields use this predicate.
    Plain decimal fields keep finite_decimal_string's plain-decimal contract.
    """
    if not isinstance(value, str) or not re.fullmatch(
        r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?", value, flags=re.ASCII
    ):
        return None
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None
