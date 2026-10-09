# -*- coding: utf-8 -*-
"""SAFE 模式 API ↔ DB 对账辅助。

规范：docs/architecture/平台序列化与反序列化规范.md
"""
from decimal import Decimal, DecimalException

from page_filter_helpers import is_ok, page_records
from response_envelope import get_response_envelope


def api_total(d):
    if not is_ok(d):
        return None
    envelope = get_response_envelope()
    data = (envelope.data(d) if envelope else None) or {}
    if isinstance(data, dict):
        return data.get("total")
    return None


def compare_totals(api_val, db_val):
    """分页 total 对账。返回 (ok, detail)。"""
    def count(value):
        if value is None or isinstance(value, bool):
            return None
        try:
            number = Decimal(str(value))
            if number.is_finite() and number >= 0 and number == number.to_integral_value():
                return number
        except DecimalException:
            pass
        return None

    api_count, db_count = count(api_val), count(db_val)
    ok = api_count is not None and db_count is not None and api_count == db_count
    return ok, f"api.total={api_val} db={db_val}"


def reconcile_page_total(d, db_total):
    """API 分页响应 total 与 DB COUNT 对账。"""
    if not is_ok(d):
        return False, f"msg={d.get('msg')}"
    api_val = api_total(d)
    return compare_totals(api_val, db_total)


def reconcile_aggregate(
    data,
    db_row,
    field_map,
    tolerance=0.0001,
    allow_both_null=False,
    *,
    money_fields=None,
    optional_money_fields=None,
    money_places=4,
):
    """逐字段比对 API 对象与 DB 行。field_map: {api_field: db_column}。

    防假绿：
    - API 与 DB 双双为空默认失败（可能接口整体坏掉），确属预期（如查不存在 id
      的详情）时显式传 allow_both_null=True。
    - 单侧 None 与另一侧 0 视为不一致（None=字段缺失，0=真实值，语义不同）。
    - money_fields：金额字段集合（api 侧字段名），走 money_cmp.Decimal 比较，默认空值失败。
    - optional_money_fields：契约明确允许 API/DB 双空的金额字段；单侧空或无效值仍失败。
    """
    if data is None and db_row is None:
        if allow_both_null:
            return True, "均为 null（显式豁免）"
        return False, "API 与 DB 均为空：疑似接口异常或取样条件错误（预期双空请传 allow_both_null=True）"
    if data is None or db_row is None:
        return False, f"一方为空 api={data is not None} db={db_row is not None}"
    money_set = set(money_fields or ())
    optional_money_set = set(optional_money_fields or ())
    money_set.update(optional_money_set)
    if money_set:
        from money_cmp import money_eq  # 延迟导入，避免循环
    diffs = []
    for api_f, db_f in field_map.items():
        api_v = data.get(api_f)
        db_v = db_row.get(db_f)
        if api_f in money_set or db_f in money_set:
            ok, detail = money_eq(
                api_v,
                db_v,
                places=money_places,
                allow_both_null=api_f in optional_money_set,
            )
            if not ok:
                diffs.append(f"{api_f} {detail}")
            continue
        if api_v is None and db_v is None:
            continue
        if (api_v is None) != (db_v is None):
            diffs.append(f"{api_f} api={api_v} db={db_v}（None 与实值不一致）")
            continue
        try:
            api_num, db_num = Decimal(str(api_v)), Decimal(str(db_v))
        except DecimalException:
            if str(api_v) != str(db_v):
                diffs.append(f"{api_f} api={api_v} db={db_v}")
            continue
        if not api_num.is_finite() or not db_num.is_finite():
            diffs.append(f"{api_f} api={api_v} db={db_v}（非有限数值）")
            continue
        try:
            if abs(api_num - db_num) > Decimal(str(tolerance)):
                diffs.append(f"{api_f} api={api_v} db={db_v}")
        except DecimalException:
            diffs.append(f"{api_f} api={api_v} db={db_v}（数值无法比较）")
    if diffs:
        return False, "; ".join(diffs)
    return True, "一致"


def reconcile_record_fields(api_rec, db_row, field_map):
    """单条 record 与 DB 行字段映射对账。"""
    if not api_rec or not db_row:
        return False, "缺少 api 或 db 行"
    diffs = []
    for api_f, db_f in field_map.items():
        av, dv = api_rec.get(api_f), db_row.get(db_f)
        if str(av if av is not None else "") != str(dv if dv is not None else ""):
            diffs.append(f"{api_f}!={db_f} ({av}!={dv})")
    if diffs:
        return False, "; ".join(diffs[:3])
    return True, "字段一致"


def sample_in_records(recs, sample_id, id_field="id"):
    if sample_id is None:
        return False
    return any(str(r.get(id_field)) == str(sample_id) for r in (recs or []))


def nested_records(d, records_key="records"):
    """支持 data.records 或 data.stats + data.records 嵌套分页。"""
    envelope = get_response_envelope()
    data = (envelope.data(d) if envelope else None) or {}
    if records_key in data and isinstance(data[records_key], dict):
        inner = data[records_key]
        return inner.get("records") or [], inner.get("total"), data
    recs = data.get("records") if isinstance(data, dict) else None
    if recs is None and isinstance(data, list):
        recs = data
    return recs or [], data.get("total") if isinstance(data, dict) else None, data


def db_count(conn, table, where, params=None):
    """带业务口径的 COUNT。where 必须显式传入（软删/tenant/业务态），
    确属无口径的全表对账须显式写 where="1=1" 并在用例 desc 注明理由。
    """
    if not where or not str(where).strip():
        raise ValueError("db_count 需要显式 where 口径（禁止省略；全表对账请显式传 '1=1' 并注明理由）")
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) n FROM {table} WHERE {where}", params or ())
        return int(cur.fetchone()["n"])


def db_scalar(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        row = cur.fetchone()
        if not row:
            return None
        return row[0] if not isinstance(row, dict) else next(iter(row.values()))
