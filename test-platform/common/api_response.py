# -*- coding: utf-8 -*-
"""项目响应信封统一入口与分页拆包。

新增用例一律::

    from api_response import is_ok, is_reject, page_records, api_total

禁止在 *_cases.py 再本地定义 ``is_ok`` / ``is_reject``。
拒绝断言、注入 payload 仍从 ``security_helpers`` 导入。
"""
from __future__ import annotations

from response_envelope import get_response_envelope
from security_helpers import is_ok, is_reject  # noqa: F401
from page_filter_helpers import page_records  # noqa: F401
from db_reconcile_helpers import api_total, nested_records  # noqa: F401


def api_data(d):
    """成功时返回 data，否则 None。"""
    if not is_ok(d):
        return None
    envelope = get_response_envelope()
    return envelope.data(d) if envelope else None


def is_http_ok(status, d) -> bool:
    """成功响应必须同时有 2xx HTTP 状态和 success=true 业务信封。"""
    return (
        isinstance(status, int)
        and not isinstance(status, bool)
        and 200 <= status < 300
        and is_ok(d)
    )


def api_msg(d, limit=60) -> str:
    envelope = get_response_envelope()
    return envelope.message(d)[:limit] if envelope else ""


def api_code(d):
    envelope = get_response_envelope()
    return envelope.business_code(d) if envelope else None
