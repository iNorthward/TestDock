# -*- coding: utf-8 -*-
"""异常与安全（E 系列）—— 跨模块复用的拒绝 / 越权 / 注入断言。

项目策略由 Pack 提供。

收敛各 *_cases.py 里重复的
    ok = is_reject(d) or d.get("code") == 401
    detail = "已拒绝 code=… msg=…" / "⚠️意外成功 …"
这段逻辑，并提供注入 payload 全集、水平越权断言。

用法：
    from security_helpers import (
        assert_rejected, assert_unauthorized, sub_reject,
        INJECTION_STRINGS, PAGE_ABUSE_PARAMS, assert_no_cross_user_leak,
    )
"""
from __future__ import annotations

import os

from response_envelope import get_response_envelope


UNSCOPED_WRITE_OPT_IN = "PLATFORM_ENABLE_UNSCOPED_WRITE_CASE"
WRITE_CASE_OPT_IN = "PLATFORM_ENABLE_WRITE_CASE"


def parse_isolated_id(raw_value, *, minimum=2):
    """Parse an explicitly configured positive fixture ID and reject reserved low IDs.

    String comparisons are insufficient here: values such as ``"0001"`` resolve
    to the same database key as ``"1"`` and must receive the same rejection.
    """
    if isinstance(raw_value, bool) or raw_value is None:
        return None
    value = str(raw_value).strip()
    if not value or not value.isascii() or not value.isdigit():
        return None
    parsed = int(value)
    return parsed if parsed >= int(minimum) else None


def guard_unscoped_write(case_id, action="写请求"):
    """Fail closed unless this exact unscoped write case is explicitly enabled."""
    if os.environ.get(UNSCOPED_WRITE_OPT_IN) == case_id:
        return None
    return {
        "label": case_id,
        "ok": True,
        "status": "incomplete",
        "detail": (
            f"未执行：无资源标识的{action}可能扩大影响范围；仅在一次性隔离环境设置 "
            f"{UNSCOPED_WRITE_OPT_IN}={case_id} 后执行"
        ),
    }


def guard_write_case(case_id, reason="该用例可能产生持久化副作用"):
    """Require exact-case opt-in for a scoped but persistent test write."""
    if os.environ.get(WRITE_CASE_OPT_IN) == case_id:
        return None
    return {
        "label": case_id,
        "ok": True,
        "status": "incomplete",
        "detail": (
            f"未执行：{reason}；仅在隔离测试环境设置 "
            f"{WRITE_CASE_OPT_IN}={case_id} 后执行"
        ),
    }


def is_ok(d) -> bool:
    envelope = get_response_envelope()
    return bool(envelope and envelope.is_ok(d))


def is_reject(d, code=None) -> bool:
    """Use the selected envelope's explicit business-rejection semantics."""
    envelope = get_response_envelope()
    return bool(envelope and envelope.is_reject(d, code))


def _msg(d) -> str:
    envelope = get_response_envelope()
    return envelope.message(d) if envelope else ""


# ---------------------------------------------------------------------------
# 拒绝断言（返回 (ok, detail)，与现有 runner 的 detail 文案保持一致）
# ---------------------------------------------------------------------------
def assert_rejected(d, *, code=None, http_status=None):
    """断言请求被拒绝。

    - code：期望的业务错误码（能确定时必须传，规范要求断言具体码）。
    - http_status：透传的 HTTP 状态码（如 401），任一命中即算拒绝。
    返回 (ok, detail)。
    """
    rejected = is_reject(d)
    if (
        http_status is not None
        and isinstance(d, dict)
        and not is_ok(d)
        and _numeric_code(_business_code(d)) == _numeric_code(http_status)
        and _numeric_code(http_status) is not None
    ):
        rejected = True
    if not rejected:
        envelope = get_response_envelope()
        data = str(envelope.data(d) if envelope else d)[:40]
        return False, f"⚠️意外成功（疑似缺校验） data={data}"
    if code is not None and isinstance(d, dict):
        expected = _numeric_code(code)
        actual = _numeric_code(_business_code(d))
        matched = (
            actual == expected if expected is not None and actual is not None
            else _business_code(d) == code
        )
        if not matched:
            return False, f"已拒绝但码不符 期望={code} 实际={_business_code(d)} msg={_msg(d)}"
    return True, f"已拒绝 code={_business_code(d)} msg={_msg(d)}"


_AUTH_REJECT_CODES = frozenset({401, 403})


def _numeric_code(value):
    """Normalize a response or HTTP code through the active envelope adapter."""
    envelope = get_response_envelope()
    return envelope.normalize_code(value) if envelope else None


def _business_code(response):
    envelope = get_response_envelope()
    return envelope.business_code(response) if envelope else None


def codes_equal(actual, expected) -> bool:
    """Compare envelope codes allowing int/digit-string equivalence."""
    left, right = _numeric_code(actual), _numeric_code(expected)
    if left is not None and right is not None:
        return left == right
    return actual == expected


def code_in(actual, expected_codes) -> bool:
    """Membership check for expected business codes with int/string normalization."""
    if expected_codes is None:
        return False
    return any(codes_equal(actual, expected) for expected in expected_codes)


def _is_explicit_auth_reject(d, http_status=None):
    """Only 401/403 proves an authorization decision; 400/404/5xx do not."""
    if not isinstance(d, dict) or is_ok(d):
        return False

    status = _numeric_code(http_status)
    if status is not None:
        if status == 0 or (400 <= status <= 599 and status not in _AUTH_REJECT_CODES):
            return False
        if status in _AUTH_REJECT_CODES:
            # An HTTP auth status is evidence only when the envelope does not
            # contradict it. Transport/other business codes can accompany a
            # gateway response and must not turn an unrelated failure green.
            body_code = _numeric_code(_business_code(d))
            if body_code is not None and body_code not in _AUTH_REJECT_CODES:
                return False
            return True

    return _numeric_code(_business_code(d)) in _AUTH_REJECT_CODES


def assert_unauthorized(d, *, http_status=None):
    """断言未授权/坏 token 被拦（仅接受 HTTP/业务码 401 或 403）。"""
    ok = _is_explicit_auth_reject(d, http_status=http_status)
    code = _business_code(d)
    status = _numeric_code(http_status)
    status_detail = f" http={status}" if status is not None else ""
    return ok, f"code={code}{status_detail} msg={_msg(d)}"


def sub_reject(case_id, d, *, code=None, http_status=None, label=None):
    """直接产出一个 subs 条目（runner 常用返回 [sub_reject(...)]）。"""
    ok, detail = assert_rejected(d, code=code, http_status=http_status)
    return {"label": label or case_id, "ok": ok, "detail": detail}


def sub_unauthorized(case_id, d, *, http_status=None, label=None):
    ok, detail = assert_unauthorized(d, http_status=http_status)
    return {"label": label or case_id, "ok": ok, "detail": detail}


# ---------------------------------------------------------------------------
# 注入 / 健壮性 payload 全集（E 规范 §2.2）
# ---------------------------------------------------------------------------
# keyword / 模糊字段注入：SQL 注入、通配符、XSS、超长串、emoji
INJECTION_STRINGS = [
    "' OR '1'='1",
    "'; DROP TABLE project_entity; --",
    "%",                      # LIKE 通配符不得被当模式导致全量命中
    "_",
    "<script>alert(1)</script>",
    "😀🚀",
    "a" * 512,               # 超长串
]

# 分页越界参数（current/size 的非法组合）
PAGE_ABUSE_PARAMS = [
    {"current": 0, "size": 10},
    {"current": -1, "size": 10},
    {"current": 999999, "size": 10},
    {"current": 1, "size": 0},
    {"current": 1, "size": -1},
]

# size 超大：不得全量拉表
PAGE_SIZE_ABUSE = {"current": 1, "size": 10000}


def assert_injection_safe(d, *, records=None, max_records=None):
    """注入字符串的响应必须安全：不得 5xx、不得 SQL 报错、`%`/`_` 不得全量命中。

    - d：响应体（success 可 true 但结果应为空/受控，或明确业务拒绝）。
    - records / max_records：给定时校验命中条数未爆表（防 `%` 通配符全量命中）。
    返回 (ok, detail)。
    """
    code = _business_code(d)
    numeric_code = _numeric_code(code)
    if (
        not isinstance(d, dict)
        or d.get("_transport_error") is True
        or d.get("_parse_error") is True
        or numeric_code == 0
    ):
        return False, "响应缺失或请求未到达服务端，无法确认注入安全"
    msg = _msg(d).lower()
    if any(k in msg for k in ("sql", "syntax", "sqlexception", "jdbc")):
        return False, f"响应疑似泄露 SQL 错误：msg={_msg(d)}"
    if numeric_code is not None and 500 <= numeric_code < 600:
        return False, f"注入触发 5xx code={code}"
    if records is not None and max_records is not None and len(records) > max_records:
        return False, f"疑似通配符全量命中 records={len(records)} > {max_records}"
    return True, "注入被安全处理（无 5xx / 无 SQL 报错 / 未全量命中）"


# 分页壳上的元数据。没有 records、也没有目标 id 时视为空页，而不是一条业务记录。
_PAGE_META = frozenset({"total", "current", "size", "pages"})


def _records_from_data(data, id_field):
    """把 data 收成待扫描记录。null 视为没有记录；无法识别时返回 None。"""
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if "records" in data:
            recs = data.get("records")
            return [] if recs is None else recs
        if _PAGE_META.intersection(data) and id_field not in data:
            return []
        return [data]
    return None


# ---------------------------------------------------------------------------
# 水平越权断言（E 规范 §2.3，CLI 最高优先级）
# ---------------------------------------------------------------------------
def assert_no_cross_user_leak(
    d, victim_id, *, id_field="userId", records=None, http_status=None,
):
    """用户 A 的 token 传用户 B 的资源 id：要么被拒，要么结果里绝不含 B 的数据。

    - d：A token 携带 B 资源 id 的响应。
    - victim_id：B 的资源标识（userId / 订单 id 等）。
    - records：显式传入结果列表；不传则从 d.data.records、列表或单对象取。
      ``data`` 为 null、或 ``records`` 为 null，视为没有记录。
    成功响应里每条记录都必须带 ``id_field``，缺字段不能当成「属于当前用户」。
    拒绝响应只有明确 401/403 才能证明鉴权拦截；若 body 里仍带有对方记录，同样判泄露。
    若调用方保留了 HTTP 状态，可通过 ``http_status`` 传入以排除路由/服务端错误。
    返回 (ok, detail)。
    """
    if not isinstance(d, dict):
        return False, "响应不是对象，无法确认越权已被拒绝"
    if records is None:
        envelope = get_response_envelope()
        records = _records_from_data(envelope.data(d) if envelope else None, id_field)
    shape_ok = isinstance(records, list) and all(isinstance(r, dict) for r in records)
    if not shape_ok:
        if not is_ok(d) and not _is_explicit_auth_reject(d, http_status=http_status):
            return False, "响应缺少明确 401/403 拒绝标记，无法确认越权已被拒绝"
        return False, "响应数据结构无法校验用户归属"
    leaked = [r for r in records if id_field in r and str(r.get(id_field)) == str(victim_id)]
    if leaked:
        return False, f"泄露 {len(leaked)} 条他人（{id_field}={victim_id}）数据"
    if is_ok(d):
        missing = sum(1 for r in records if id_field not in r)
        if missing:
            return False, f"有 {missing} 条记录缺少 {id_field}，无法确认用户归属"
        return True, "未泄露他人数据（结果均归属当前 token）"
    if _is_explicit_auth_reject(d, http_status=http_status):
        return True, f"越权被拒 code={_business_code(d)}"
    return False, "响应缺少明确 401/403 拒绝标记，无法确认越权已被拒绝"
