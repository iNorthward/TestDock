#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 OpenAPI 快照 + 表结构机械派生 D 系列（SAFE 数据对账）候选清单。

对齐《SAFE模式数据对账测试规范》「对账类型矩阵」，按接口形态派生对账检查项，
并自动推断涉及表 / 软删列（复用 gen_interface_profile 的表名推断）：

  分页列表   → total_reconcile：api.total = DB COUNT（同业务口径：软删+tenant+态）
             → sample_in_records：DB 样本 id 精准查回，应在结果中
  枚举字段   → enum_group_reconcile：api count(字段=值) = DB COUNT ... GROUP BY 字段（逐值）
  时间窗口   → time_window_reconcile：稳定历史窗口内 api total = DB COUNT（防并发抖动）
  详情/嵌套  → record_fields_reconcile：api 字段 = DB 同行映射
  统计/概览  → aggregate_reconcile：api 聚合 = DB SUM/COUNT（口径需人工确认）

产出的 db.where 是**口径骨架**（含软删列占位 + tenant/status TODO），Agent 落地时
须复刻接口业务口径，禁止 `1=1` 全表对账。活跃写入表（订单/成交/登录/出入金）自动
附「稳定历史窗口或重试」提醒。本脚本只产出候选，不写死断言码与 SQL 值。

用法：
  python3 scripts/gen_d_series_candidates.py --prefix /v1/items
  python3 scripts/gen_d_series_candidates.py --covered-only --json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT / "scripts"))
from project_inputs import pack_data_path, project_input_path, project_input_setting  # noqa: E402
LOCAL_OAS = project_input_path("PLATFORM_OAS_BASELINE", required=False)
COVERAGE_JSON = pack_data_path("coverage.json")
from candidate_inputs import load_coverage as _load_coverage, load_openapi_snapshot, iter_openapi_operations  # noqa: E402
from gen_interface_profile import load_schema_tables, infer_tables_from_path  # noqa: E402

SKIP_PARAMS = {"current", "size"}

LIST_PATH_PATTERN = r"/(page|list|query|search|records?)"
DETAIL_PATTERN = r"/(detail|info|view)(/|$)"
AGGREGATE_PATTERN = r"/(statistics|stats|overview|summary|chart|total|count|amount)(/|$)"
TIME_PARAM_PATTERN = r"(time|date).*(from|to|start|end)|(start|end).*(time|date)"
ENUM_NAME_PATTERN = r"(status|state|type|category|direction|result|mode)"
# 活跃写入表：API 查询与 DB COUNT 非原子，易闪断 → 需稳定窗口/重试
ACTIVE_WRITE_PATTERN = r"(order|trade|login|deposit|withdraw|position|entrust|bag)"


def load_coverage(*, required: bool = False) -> dict[str, dict]:
    return _load_coverage(COVERAGE_JSON, required=required)


def _enum_from_schema(schema: dict) -> list:
    return schema.get("enum", []) if isinstance(schema.get("enum"), list) else []


def candidate_key(endpoint: str, kind: str, param: str = None) -> str:
    key = f"{endpoint}|{kind}"
    if param:
        key += f"|{param}"
    return key


def _where_skeleton(table: str, schema_tables: dict) -> tuple[str, tuple, str | None, str | None]:
    """按**实际列**拼 where 骨架（fail-closed）：只有该表确有的列才写进去，绝不编造。

    返回 (where 文本, args, 软删列, 租户列)。表未知或无这些列时 where 只含业务态 TODO。
    """
    cols = schema_tables.get(table, {}).get("columns", {})
    parts, args = [], ()
    soft = "is_deleted" if "is_deleted" in cols else ("deleted" if "deleted" in cols else None)
    tenant = next((c for c in ("tenant_id", "tenant") if c in cols), None)
    if soft:
        parts.append(f"{soft} = 0")
    if tenant:
        parts.append(f"{tenant} = %s")
        args = (project_input_setting("PLATFORM_MGR_TENANT", default=""),)
    parts.append("/* + 业务态: status/发布态 等，据接口口径补，禁止 1=1 */")
    if not soft and not tenant:
        parts.insert(0, "/* 该表无 is_deleted/tenant 列，确认软删/隔离口径 */")
    return " AND ".join(parts), args, soft, tenant


def derive_for_op(method: str, path: str, spec: dict, cov: dict, schema_tables: dict) -> dict | None:
    if method != "GET":
        return None  # D 只对读接口；写路径一致性归 LIVE

    all_query = [p for p in (spec.get("parameters") or []) if p.get("in") == "query"]
    params = [p for p in all_query if p["name"] not in SKIP_PARAMS]

    is_list = bool(re.search(LIST_PATH_PATTERN, path, re.I)
                   or any(p["name"] in ("current", "size") for p in all_query))
    is_detail = bool(re.search(DETAIL_PATTERN, path, re.I) or re.search(r"\{[^}]+\}", path))
    is_aggregate = bool(re.search(AGGREGATE_PATTERN, path, re.I))
    if not (is_list or is_detail or is_aggregate):
        return None

    tables = infer_tables_from_path(path, schema_tables)
    # fail-closed：只有当推断表**确实存在于 schema** 时才认为解析成功；否则不编造口径。
    resolved = [t for t in tables if t in schema_tables]
    table0 = resolved[0] if resolved else None
    table_hint = ", ".join(resolved) if resolved else "（表未确认，先查 PLATFORM_SCHEMA_JSON 指向的文件，勿编造口径）"
    active = bool(re.search(ACTIVE_WRITE_PATTERN, path, re.I))
    ep = f"{method} {path}"

    if table0:
        where, where_args, soft_col, tenant_col = _where_skeleton(table0, schema_tables)
    else:
        where, where_args, soft_col, tenant_col = None, (), None, None

    candidates: list[dict] = []

    def add(kind, desc, expect, helper, param=None, **extra):
        c = {"kind": kind, "desc": desc, "expect": expect, "helper": helper,
             "table": table_hint, "tableResolved": bool(table0),
             "uncertain": True, "confirmation": "表存在不证明接口映射；先确认 Pack 的表关联、字段映射和隔离口径",
             "key": candidate_key(ep, kind, param)}
        if param:
            c["param"] = param
        if active:
            c["note"] = "活跃写入表：加稳定历史窗口（createTime < 今日0点）或失败重试 1 次"
        c.update(extra)
        candidates.append(c)

    # 分页列表：total 对账 + 样本回查
    if is_list and not is_detail:
        if table0:
            add("total_reconcile", f"分页 total 与 DB COUNT 一致（口径：{where}）",
                "api.total == db_count(同筛选口径)", "reconcile_page_total",
                db={"table": table0, "where": where, "args": where_args})
        else:
            add("total_reconcile", "分页 total 与 DB COUNT 一致（先确认表名与软删/租户口径）",
                "api.total == db_count(同口径)；**表未确认前勿写 SQL，避免对错表假绿**",
                "reconcile_page_total")
        add("sample_in_records", "DB 取样本 id → 精准查询 → 样本在结果中",
            "sample_id ∈ api.records", "sample_in_records + F 样本")

        # 枚举字段：逐值 GROUP BY 对账（高价值，防某一态漏统计）
        for p in params:
            enum_vals = _enum_from_schema(p.get("schema") or {})
            if enum_vals or re.search(ENUM_NAME_PATTERN, p["name"], re.I):
                vh = f"（枚举≈{enum_vals[:6]}）" if enum_vals else "（枚举全集查 DB/Wiki）"
                col = re.sub(r"([A-Z])", r"_\1", p["name"]).lower()  # camelCase → snake_case 列名提示
                add("enum_group_reconcile",
                    f"按 {p['name']} 逐值对账{vh}", param=p["name"],
                    expect=f"每个值：api count({p['name']}=v) == DB COUNT GROUP BY `{col}`（列名 snake_case，需确认实际列）",
                    helper="reconcile_page_total(逐值)")

        # 时间窗口对账（稳定历史窗口，防并发抖动）
        if any(re.search(TIME_PARAM_PATTERN, p["name"], re.I) for p in params):
            add("time_window_reconcile", "稳定历史时间窗口内 total 与 DB COUNT 一致",
                "api.total(窗口) == db_count(时间列 in 窗口 AND 软删口径)", "reconcile_page_total",
                note="窗口右界取今日0点，规避活跃写入抖动")

    # 详情/嵌套：字段映射对账
    if is_detail:
        add("record_fields_reconcile", "详情字段与 DB 同行映射一致（FIELD_MAP）",
            "api.data.<字段> == db_row.<列>（金额/时间单位对齐）", "reconcile_record_fields / reconcile_aggregate")

    # 统计/概览：聚合对账（口径需人工确认，禁止双空假绿）
    if is_aggregate:
        add("aggregate_reconcile", "聚合字段与 DB SUM/COUNT 一致（口径需人工确认）",
            "api 聚合 == DB SUM/COUNT；双空判失败除非 allow_both_null=True", "reconcile_aggregate")

    if not candidates:
        return None

    return {
        "endpoint": ep,
        "tables": resolved,
        "tableResolved": bool(table0),
        "softDeleteCol": soft_col,
        "activeWrite": active,
        "summary": spec.get("summary", ""),
        "existingCaseCount": cov.get("caseCount", 0),
        "covered": bool(cov.get("covered")),
        "candidateCount": len(candidates),
        "candidates": candidates,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prefix", default="", help="只处理该路径前缀")
    parser.add_argument("--covered-only", action="store_true", help="只针对已接入用例的路径")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    try:
        oas = load_openapi_snapshot(LOCAL_OAS)
        cover = load_coverage(required=args.covered_only)
    except ValueError as exc:
        parser.error(str(exc))
    schema_tables = load_schema_tables()

    results = []
    for method, path, spec in iter_openapi_operations(oas, args.prefix):
        key = f"{method.upper()} {path}"
        cov = cover.get(key, {})
        if args.covered_only and not cov.get("covered"):
            continue
        r = derive_for_op(method.upper(), path, spec, cov, schema_tables)
        if r:
            results.append(r)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=1))
        return

    total_cand = sum(r["candidateCount"] for r in results)
    no_table = sum(1 for r in results if not r["tables"])
    print(f"读接口 {len(results)} 个，派生 D 候选 {total_cand} 条"
          + (f"（前缀 {args.prefix}）" if args.prefix else "")
          + f"；未推断到表 {no_table} 个（需人工补表名）\n")
    for r in results[:12]:
        flag = f"已覆盖 {r['existingCaseCount']} 条" if r["covered"] else "★ 未覆盖"
        tbl = ", ".join(r["tables"]) or "表?"
        aw = " [活跃写入表]" if r["activeWrite"] else ""
        print(f"● {r['endpoint']}  [{r['summary']}]  ({flag})  表={tbl}{aw}")
        for c in r["candidates"]:
            print(f"    - [{c['kind']}] {c['desc']} → {c['expect']}")
        print()
    if len(results) > 12:
        print(f"... 还有 {len(results) - 12} 个接口，用 --json 查看完整列表")


if __name__ == "__main__":
    main()
