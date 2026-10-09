#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 OpenAPI 快照机械派生 F 系列（筛选覆盖）候选用例清单。

按《查询接口筛选覆盖测试规范》§3 最低覆盖矩阵,为每个查询参数派生候选:
  【无筛选基线】不传业务筛选 → 记录 total_all,作为「筛选是否真的收窄」的对照
  【精准匹配】id/userId/status 等 → 命中(exact) + 收窄证明(count_filtered < total_all,防假绿)
  【模糊匹配】keyword/userUid 等 → ① 正向命中(sample 片段) ② 反向必空(不存在子串)
  【枚举】读 schema.enum(字符串/整型皆可) 或描述回退 → 每个枚举值 1 次(enum_all)
  【布尔】isXxx/boolean → true / false 各一次,结果集应不同(互补或子集)
  【时间闭区间】*From + *To → ① 样本窗口 ② 仅 From ③ 仅 To ④ 未来空
  【排序】orderBy/sortField/isAsc → asc vs desc,断言首条记录相反(排序真的生效)
  【分页】size → 小于默认值; 翻页去重 current=1 vs 2 id 无交集
  【组合】高价值维度两两组合(pairwise,上限 4 条)

列表/分页判定放宽:GET 且 (路径含 /page /list /query /search /export /statistics /records /tree
或带 current/size 参数),并排除 /detail /info /view 单资源接口。

用法:
  python3 scripts/gen_f_series_candidates.py --prefix /v1/items
  python3 scripts/gen_f_series_candidates.py --covered-only --json
"""
from __future__ import annotations

import argparse
import json
import re
from itertools import combinations
from pathlib import Path

from candidate_inputs import load_coverage as _load_coverage
from candidate_inputs import load_openapi_snapshot, iter_openapi_operations

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "scripts"))
from project_inputs import pack_data_path, project_input_path
LOCAL_OAS = project_input_path("PLATFORM_OAS_BASELINE", required=False)
COVERAGE_JSON = pack_data_path("coverage.json")

SKIP_PARAMS = {"current", "size"}


def candidate_key(endpoint: str, kind: str, param: str = None) -> str:
    """候选唯一键：endpoint|kind|param（与 E/D/R 一致，供 covers 追溯与评分卡 kind 级统计）。"""
    key = f"{endpoint}|{kind}"
    if param:
        key += f"|{param}"
    return key


# 参数类型模式
EXACT_MATCH_PATTERN = r"^(id|.*id|status|type|.*type|state|.*state|category)$"
FUZZY_MATCH_PATTERN = r"(keyword|name|account|uid|fingerprint|mobile|email|remark|symbol.*keyword)"
ENUM_NAME_PATTERN = r"(type|status|state|category|direction|result|mode|problem.*type|client.*type|buy.*sell)"
TIME_PARAM_PATTERN = r"(time|date).*(from|to|start|end)|(start|end).*(time|date)"
BOOL_NAME_PATTERN = r"^(is[A-Z]|enable|has[A-Z]|include)"
SORT_NAME_SET = {"orderby", "orderbyfield", "sortfield", "sort", "sortorder", "isasc", "asc", "desc", "ordermode"}

# 列表/分页接口路径特征（放宽，不再只认 /page /list）
LIST_PATH_PATTERN = r"/(page|list|query|search|export|statistics|records?|tree|history|summary)"
# 单资源（详情）接口：不做筛选覆盖
DETAIL_PATTERN = r"/(detail|info|view)(/|$)"


def load_coverage(*, required: bool = False) -> dict[str, dict]:
    return _load_coverage(COVERAGE_JSON, required=required)


def _enum_from_schema(schema: dict) -> list:
    return schema.get("enum", []) if isinstance(schema.get("enum"), list) else []


def _enum_from_desc(desc: str) -> list[str]:
    """从中文描述里粗略抓 '0-禁用，1-启用' 这类枚举值（回退兜底）。"""
    vals = re.findall(r"(?<![0-9])(-?\d+)\s*[-:：]", desc or "")
    return sorted(set(vals))


def is_list_endpoint(method: str, path: str, spec: dict) -> bool:
    """列表/分页查询接口才做筛选覆盖；单资源详情接口排除。"""
    if method.upper() != "GET":
        return False
    if re.search(DETAIL_PATTERN, path, re.I) or re.search(r"\{[^}]+\}", path):
        return False
    if re.search(LIST_PATH_PATTERN, path, re.I):
        return True
    q = [p for p in (spec.get("parameters") or []) if p.get("in") == "query"]
    return any(p["name"] in ("current", "size") for p in q)


def derive_for_op(method: str, path: str, spec: dict, cov: dict) -> dict | None:
    if not is_list_endpoint(method, path, spec):
        return None

    all_query = [p for p in (spec.get("parameters") or []) if p.get("in") == "query"]
    params = [p for p in all_query if p["name"] not in SKIP_PARAMS]
    if not params:
        return None

    has_pagination = any(p["name"] in ("current", "size") for p in all_query)
    candidates = []
    filter_dims = []  # 供组合筛选挑选的高价值维度参数名

    # ---- 无筛选基线（对照 total_all，供后续收窄断言引用）----
    candidates.append({
        "kind": "baseline_no_filter",
        "desc": "不传任何业务筛选（记录 total_all 作对照）",
        "rules": "total_all = 同业务口径基线；count_filtered <= total_all；严格收窄仅作软信号",
    })

    # ---- 收集时间参数对 ----
    time_pairs = {}
    for p in params:
        name = p["name"]
        if re.search(TIME_PARAM_PATTERN, name, re.I):
            lower = name.lower()
            if "from" in lower:
                time_pairs.setdefault(re.sub(r"from$", "", lower), {})["from"] = name
            elif "to" in lower:
                time_pairs.setdefault(re.sub(r"to$", "", lower), {})["to"] = name
            elif lower == "starttime" or lower.endswith("start"):
                time_pairs.setdefault("time", {})["from"] = name
            elif lower == "endtime" or lower.endswith("end"):
                time_pairs.setdefault("time", {})["to"] = name

    time_names = {n for pair in time_pairs.values() for n in pair.values()}

    for p in params:
        name = p["name"]
        schema = p.get("schema") or {}
        ptype = schema.get("type", "string")
        desc = p.get("description", "")
        lower = name.lower()

        if name in time_names:  # 时间参数统一在下方成对处理
            continue

        enum_vals = _enum_from_schema(schema)

        # 1. 枚举（schema.enum 优先，字符串/整型皆可；否则整型 + 名字像枚举 → 描述回退）
        if enum_vals:
            vals_hint = enum_vals[:8]
            candidates.append({
                "kind": "enum_each_value",
                "param": name,
                "desc": f"{name} 每个枚举值各 1 次（共 {len(enum_vals)} 个）",
                "rules": f"enum_all={{{name}: each of {vals_hint}}}；每值 success 且命中记录的 {name} 等于该值；"
                         f"**某值合法零数据允许（不强制非空）**",
            })
            filter_dims.append(name)
            continue

        if ptype in ("integer", "number") and re.search(ENUM_NAME_PATTERN, name, re.I):
            desc_vals = _enum_from_desc(desc)
            candidates.append({
                "kind": "enum_each_value",
                "param": name,
                "desc": f"{name} 每个枚举值（描述提取{('≈' + str(desc_vals)) if desc_vals else '，需查 DB/Wiki 全集'}）",
                "rules": f"enum_all={{{name}: each_value}}",
            })
            filter_dims.append(name)
            continue

        # 2. 布尔 → true / false 各一次
        if ptype == "boolean" or re.search(BOOL_NAME_PATTERN, name):
            candidates.append({
                "kind": "bool_toggle",
                "param": name,
                "desc": f"{name}=true / false 各一次",
                "rules": "两次结果集应不同（互补或子集），证明该开关生效",
            })
            filter_dims.append(name)
            continue

        # 3. 排序参数 → asc vs desc
        if lower in SORT_NAME_SET:
            candidates.append({
                "kind": "sort_order",
                "param": name,
                "desc": f"{name} 升序 vs 降序",
                "rules": "两序首条记录相反（或整体逆序），证明排序生效",
            })
            continue

        # 4. 模糊匹配（排除 camelCase 外键 xxxId，那是精准匹配）
        if re.search(FUZZY_MATCH_PATTERN, name, re.I) and not name.endswith("Id"):
            candidates.append({
                "kind": "fuzzy_match_positive",
                "param": name,
                "desc": f"{name} 模糊匹配正向（DB 样本字段片段）",
                "rules": "sample_id 应在结果中；命中条数 <= total_all",
            })
            candidates.append({
                "kind": "fuzzy_match_negative",
                "param": name,
                "desc": f"{name} 反向必空（不存在子串 zzz_no_such_kw_991）",
                "rules": f"{name}='zzz_no_such_kw_991', require_empty=True",
            })
            filter_dims.append(name)
            continue

        # 5. 精准匹配（含收窄证明，防假绿）
        if re.search(EXACT_MATCH_PATTERN, name, re.I):
            candidates.append({
                "kind": "exact_match",
                "param": name,
                "desc": f"{name} 单条件命中（DB 取样本值）+ 收窄软信号",
                "rules": f"exact={{{name}: sample_value}}, require_non_empty；"
                         f"收窄为**软信号**：count_filtered <= total_all，"
                         f"< 则强证明有效、==（可能全表同值/低基数）仅提示不判失败",
            })
            filter_dims.append(name)

    # 6. 时间参数（闭区间 4 场景）
    for base, pair in time_pairs.items():
        if "from" in pair and "to" in pair:
            candidates.append({
                "kind": "time_window", "params": [pair["from"], pair["to"]],
                "desc": f"{pair['from']}+{pair['to']} 覆盖样本窗口（DB 样本时间 ±1 天）",
                "rules": f"time_field=<记录时间字段>, time_from_param={pair['from']}, time_to_param={pair['to']}",
            })
            candidates.append({
                "kind": "time_from_only", "param": pair["from"],
                "desc": f"只传 {pair['from']}（近 1 年）",
                "rules": f"有数据则均 >= {pair['from']}",
            })
            candidates.append({
                "kind": "time_to_only", "param": pair["to"],
                "desc": f"只传 {pair['to']}",
                "rules": f"有数据则均 <= {pair['to']}",
            })
            candidates.append({
                "kind": "time_future_empty", "params": [pair["from"], pair["to"]],
                "desc": f"{pair['from']}+{pair['to']} 未来区间（2099 年）",
                "rules": "require_empty=True",
            })
            filter_dims.append(pair["from"])

    # 7. 分页 size + 翻页去重
    if has_pagination:
        candidates.append({
            "kind": "page_size_small", "param": "size",
            "desc": "size 小于默认值（如 size=5）", "rules": "len(records) <= size",
        })
        candidates.append({
            "kind": "page_dedup", "params": ["current"],
            "desc": "current=1 与 current=2 id 集合无交集",
            "rules": "两页 id 无交集（size 取小值保证有第 2 页）",
        })

    # 8. 组合筛选（高价值维度两两 pairwise，上限 4 条）
    combo_pool = filter_dims[:4]
    combos = list(combinations(combo_pool, 2))[:4]
    for a, b in combos:
        candidates.append({
            "kind": "combo_filter", "params": [a, b],
            "desc": f"组合筛选：{a} + {b}",
            "rules": f"两维度同时生效；count(a+b) <= min(count(a), count(b))",
        })

    if not candidates or len(candidates) == 1:  # 只有 baseline 没意义
        return None

    # 统一补稳定 key（供 covers 追溯 / 评分卡 kind 级统计）
    for c in candidates:
        pk = c.get("param") or ("+".join(c["params"]) if c.get("params") else None)
        c["uncertain"] = True
        c["confirmation"] = "Pack 筛选字段映射、匹配方式、时间边界及样本"
        c["key"] = candidate_key(f"{method} {path}", c["kind"], pk)

    return {
        "endpoint": f"{method} {path}",
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

    results = []
    for method, path, spec in iter_openapi_operations(oas, args.prefix):
        key = f"{method.upper()} {path}"
        cov = cover.get(key, {})
        if args.covered_only and not cov.get("covered"):
            continue
        result = derive_for_op(method.upper(), path, spec, cov)
        if result:
            results.append(result)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=1))
        return

    total_cand = sum(r["candidateCount"] for r in results)
    print(f"查询接口 {len(results)} 个，派生 F 候选 {total_cand} 条"
          + (f"（前缀 {args.prefix}）" if args.prefix else "") + "\n")
    for r in results[:10]:
        flag = f"已覆盖 {r['existingCaseCount']} 条" if r["covered"] else "★ 未覆盖"
        print(f"● {r['endpoint']}  [{r['summary']}]  ({flag})")
        for c in r["candidates"][:6]:
            param_hint = c.get("param") or (",".join(c.get("params", [])) if c.get("params") else "")
            print(f"    - [{c['kind']}] {param_hint}: {c['desc']}")
        if r["candidateCount"] > 6:
            print(f"    ... 还有 {r['candidateCount'] - 6} 条")
        print()
    if len(results) > 10:
        print(f"... 还有 {len(results) - 10} 个接口，用 --json 查看完整列表")


if __name__ == "__main__":
    main()
