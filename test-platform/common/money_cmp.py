# -*- coding: utf-8 -*-
"""金额比较统一入口 —— 资金 / 结算 / 日统计对账。

用法::

    from money_cmp import money_eq, money_diff, to_decimal

    ok, detail = money_eq(api_amount, db_amount)
    ok, detail = money_eq(a, b, places=4)  # 默认 4 位小数

禁止资金类字段继续用 ``abs(float(a)-float(b)) < 1e-4`` 散落各处。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Optional, Tuple


def to_decimal(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value).strip().replace(",", ""))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _quantize(d: Decimal, places: int) -> Decimal:
    q = Decimal("1").scaleb(-places)  # 10 ** -places
    return d.quantize(q, rounding=ROUND_HALF_UP)


def _amount_state(value, parsed) -> str:
    if parsed is not None:
        return "有效"
    if value is None or value == "":
        return "为空"
    return "无效"


def money_eq(
    a,
    b,
    *,
    places=4,
    allow_both_null=False,
) -> Tuple[bool, str]:
    """两金额在 ``places`` 位小数下是否相等。返回 (ok, detail)。"""
    da, db_ = to_decimal(a), to_decimal(b)
    if da is None and db_ is None and allow_both_null and a in (None, "") and b in (None, ""):
        return True, "均为 null（显式豁免）"
    if da is None or db_ is None:
        return False, (
            f"金额无法比较 a={a!r}({_amount_state(a, da)}) "
            f"b={b!r}({_amount_state(b, db_)})"
        )
    try:
        qa, qb = _quantize(da, places), _quantize(db_, places)
    except InvalidOperation:
        return False, f"金额超出可比较范围 a={a!r} b={b!r}"
    if qa == qb:
        return True, f"{qa}=={qb}"
    return False, f"{qa}!={qb} (raw {a!r} vs {b!r})"


def money_diff(a, b, *, places=4) -> Optional[Decimal]:
    """a - b（量化后）。任一侧无法解析则返回 None。"""
    da, db_ = to_decimal(a), to_decimal(b)
    if da is None or db_ is None:
        return None
    try:
        _quantize(da, places)
        _quantize(db_, places)
        return _quantize(da - db_, places)
    except InvalidOperation:
        return None


def money_field_map_eq(
    api_obj: dict,
    db_row: dict,
    field_map: dict,
    *,
    places=4,
) -> Tuple[bool, str]:
    """按 field_map {api_field: db_column} 逐项金额对账。"""
    if not api_obj or not db_row:
        return False, "缺少 api 或 db 行"
    diffs = []
    for api_f, db_f in field_map.items():
        ok, detail = money_eq(api_obj.get(api_f), db_row.get(db_f), places=places)
        if not ok:
            diffs.append(f"{api_f}/{db_f}: {detail}")
    if diffs:
        return False, "; ".join(diffs[:5])
    return True, "金额一致"
