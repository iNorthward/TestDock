# -*- coding: utf-8 -*-
"""分页查询接口 —— 筛选条件断言辅助。

项目筛选字段与策略由 Pack 显式声明。
"""
from decimal import Decimal, DecimalException

from response_envelope import get_response_envelope
from response_base_rules import validate_response_data
from security_helpers import assert_unauthorized, is_ok
from time_cst import parse_dt as _parse_dt  # 东八区时间统一入口


def page_records(d):
    if not is_ok(d):
        envelope = get_response_envelope()
        message = envelope.message(d) if envelope else "响应不是对象"
        return None, None, f"msg={message}"
    envelope = get_response_envelope()
    data = envelope.data(d) if envelope else None
    if data is None:
        return None, None, "data 缺失或为 null"
    if isinstance(data, list):
        data = {"records": data}
    elif not isinstance(data, dict):
        return None, None, "data 不是分页对象或列表"
    if "records" not in data:
        return None, None, "分页 data 缺少 records"
    recs = data.get("records")
    if not isinstance(recs, list):
        return None, None, "records 不是列表"
    if any(not isinstance(r, dict) for r in recs):
        return None, None, "records 含非对象元素"
    return data, recs, None


def _field_value(record, field):
    """支持 OpenAPI 嵌套 VO 字段名（如 chainDetail.signature）。"""
    value = record
    for part in str(field).split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def all_eq(recs, field, value):
    if not recs:
        return True
    return all(str(_field_value(r, field)) == str(value) for r in recs)


def all_contains(recs, field, needle):
    if not recs or not needle:
        return True
    needle = str(needle)
    return all(needle in str(r.get(field) or "") for r in recs)


def all_in_time(recs, field, t_from=None, t_to=None):
    """返回 (ok, err)。字段值存在但无法解析视为失败（防止格式漂移被默默放过）。"""
    if not recs:
        return True, None
    df, dt = _parse_dt(t_from), _parse_dt(t_to)
    for r in recs:
        raw = r.get(field)
        if raw is None or raw == "":
            continue
        t = _parse_dt(raw)
        if t is None:
            return False, f"{field}={raw!r} 无法解析为时间"
        if df and t < df:
            return False, f"{field}={raw} 早于下限 {t_from}"
        if dt and t > dt:
            return False, f"{field}={raw} 晚于上限 {t_to}"
    return True, None


def check_page_size(recs, size):
    if not size:
        return True
    return len(recs) <= int(size)


def _has_positive_assertion(rules):
    """rules 中是否含「预期命中」类断言（样本驱动，空结果即可疑）。"""
    if rules.get("sample_id") is not None:
        return True
    return any(rules.get(k) for k in ("exact", "contains", "enum_all"))


def validate_page(d, params=None, rules=None):
    """通用分页筛选校验。rules 示例：
    exact: {api_field: value}
    contains: {api_field: substr}
    status_field/status_value 或 params 内 status
    time_field + params 内 createTimeFrom/To 或 loginTimeFrom/To 等
    require_non_empty / require_empty: 显式非空 / 必空
    require_zero_total: require_empty 的分页响应还必须明确返回 total=0
    allow_empty: True  # 显式豁免「正向断言默认非空」规则（须写明理由）
    其他项目规则由所选 Pack 的响应数据 hook 解释。

    防假绿：rules 含 exact/contains/enum_all/sample_id 任一正向断言而结果为空时，
    默认判失败（疑似参数名传错、筛选未生效），除非显式 allow_empty/require_empty。
    """
    params = dict(params or {})
    rules = rules or {}
    data, recs, err = page_records(d)
    if err:
        return False, err

    if not check_page_size(recs, params.get("size")):
        return False, f"records={len(recs)} 超过 size={params.get('size')}"

    if (
        not recs
        and _has_positive_assertion(rules)
        and not rules.get("require_empty")
        and not rules.get("allow_empty")
    ):
        return False, "正向断言但结果为空：疑似筛选参数未生效或样本失效（确属预期请设 allow_empty 并注明理由）"

    status_val = rules.get("status_value", params.get("status"))
    status_field = rules.get("status_field", "status")
    if status_val is not None and recs and not all_eq(recs, status_field, status_val):
        return False, f"存在 {status_field}!={status_val}"

    for field, val in (rules.get("exact") or {}).items():
        if recs and not all_eq(recs, field, val):
            return False, f"存在 {field}!={val}"

    for field, needle in (rules.get("contains") or {}).items():
        if recs and not all_contains(recs, field, needle):
            return False, f"存在 {field} 不含 {needle!r}"

    for field, val in (rules.get("enum_all") or {}).items():
        if recs and not all_eq(recs, field, val):
            return False, f"存在 {field}!={val}"

    time_field = rules.get("time_field")
    if time_field:
        t_from = params.get(rules.get("time_from_param", "createTimeFrom"))
        t_to = params.get(rules.get("time_to_param", "createTimeTo"))
        if (t_from or t_to) and recs:
            t_ok, t_err = all_in_time(recs, time_field, t_from, t_to)
            if not t_ok:
                return False, t_err

    if rules.get("require_non_empty") and not recs:
        return False, "期望有记录但为空"

    if rules.get("require_empty") and recs:
        return False, f"期望为空但有 {len(recs)} 条"

    if rules.get("require_zero_total"):
        envelope = get_response_envelope()
        raw_data = envelope.data(d) if envelope and isinstance(d, dict) else None
        if (not isinstance(raw_data, dict)
                or not isinstance(raw_data.get("records"), list)
                or "total" not in raw_data):
            return False, "期望零结果，但分页响应缺少 records 列表或 total"
        raw_total = raw_data.get("total")
        try:
            total = Decimal(str(raw_total)) if not isinstance(raw_total, bool) else None
        except DecimalException:
            total = None
        if total is None or not total.is_finite() or total != 0:
            return False, f"期望 total=0，实际 total={raw_total!r}"

    sample_id = rules.get("sample_id")
    sample_id_field = rules.get("sample_id_field", "id")
    if sample_id is not None and recs:
        if not any(str(r.get(sample_id_field)) == str(sample_id) for r in recs):
            return False, f"样本 {sample_id_field}={sample_id} 不在结果中"

    project_ok, project_detail = validate_response_data(data, rules=rules)
    if not project_ok:
        return False, project_detail

    total = data.get("total")
    if total is None:
        return True, f"records={len(recs)}"
    return True, f"total={total} records={len(recs)}"


def run_page_case(c, fetch_fn, default_rules=None):
    params = c.get("params")
    body = c.get("body")
    rules = dict(default_rules or {})
    rules.update(c.get("rules") or {})
    if body is not None:
        status, d = fetch_fn(body=body, params=params, with_token=c.get("with_token", True))
    else:
        status, d = fetch_fn(params=params, with_token=c.get("with_token", True))
    if c.get("with_token", True) is False:
        ok, detail = assert_unauthorized(d, http_status=status)
        return ok, detail
    ok, detail = validate_page(d, params, rules)
    return ok, detail
