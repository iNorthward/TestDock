#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Apifox 契约巡检 —— 对比本地 OpenAPI 快照与已接入用例覆盖范围。

用法：
  python3 scripts/apifox_contract_audit.py
  python3 scripts/apifox_contract_audit.py --apifox-index /path/to/apifox-index.json

Apifox 最新索引通过 ``scripts/fetch_apifox_oas.py`` 获取；脚本在保存前会清除认证请求头示例中的令牌和 Basic 凭据。
输出：stdout 摘要；完整报告由 run_apifox_inspection.py 写入 pack 配置目录。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT / "scripts"))
from project_inputs import init_project_pack, project_input_path, project_input_setting  # noqa: E402

init_project_pack(ROOT)
LOCAL_OAS = project_input_path("PLATFORM_OAS_BASELINE", required=False)
DEFAULT_APIFOX_INDEX = project_input_path("PLATFORM_APIFOX_INDEX", required=False)
from candidate_inputs import HTTP_METHODS, load_openapi_snapshot  # noqa: E402
from gen_apifox_coverage_wiki import collect, parse_http  # noqa: E402

def contract_path_prefixes():
    value = project_input_setting("PLATFORM_CONTRACT_PATH_PREFIXES", default="")
    if not value:
        return ()
    if not isinstance(value, str):
        raise ValueError("PLATFORM_CONTRACT_PATH_PREFIXES 须为逗号分隔的路径前缀")
    prefixes = tuple(part.strip() for part in value.split(","))
    if any(not part.startswith("/") for part in prefixes):
        raise ValueError("PLATFORM_CONTRACT_PATH_PREFIXES 每项须为以 / 开头的非空路径前缀")
    return prefixes


CONTRACT_PATH_PREFIXES = contract_path_prefixes()


def _in_scope(path):
    return not CONTRACT_PATH_PREFIXES or path.startswith(CONTRACT_PATH_PREFIXES)


def covered_ops() -> set[tuple[str, str]]:
    """已接入用例的 (METHOD, path) 集合——由各模块 case.api / RUNNER_API 自描述解析。"""
    _rows, path_index, _unresolved = collect()
    ops: set[tuple[str, str]] = set()
    for key in path_index:
        parsed = parse_http(key)
        if parsed:
            ops.add(parsed)
    return ops


def load_ops_from_oas(data: dict) -> dict[tuple[str, str], dict]:
    ops = {}
    paths = data.get("paths") if isinstance(data, dict) else None
    if not isinstance(paths, dict):
        return ops
    for path, methods in paths.items():
        if not isinstance(methods, dict):
            continue
        for method, spec in methods.items():
            if not isinstance(method, str) or method.lower() not in HTTP_METHODS:
                continue
            if not isinstance(spec, dict):
                continue
            if isinstance(spec, dict) and "$ref" in spec:
                # Apifox index: summary nested under method key
                inner = spec.get(method) if method in spec else None
                summary = (inner or spec).get("summary", "")
                ops[(method.upper(), path)] = {"summary": summary, "ref": spec.get("$ref")}
            else:
                ops[(method.upper(), path)] = spec if isinstance(spec, dict) else {}
    return ops


def param_fingerprint(spec: dict) -> dict:
    if not isinstance(spec, dict):
        return {}
    params = spec.get("parameters") or []
    query = []
    for p in params:
        if p.get("in") != "query":
            continue
        schema = p.get("schema") or {}
        typ = _prop_type(schema) or _prop_type(p)
        enum = schema.get("enum", p.get("enum", [])) or []
        query.append({"name": str(p.get("name", "")), "required": bool(p.get("required", False)),
                      "type": typ, "enum": sorted(enum, key=lambda x: str(x))})
    query.sort(key=lambda x: x["name"])
    body = spec.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {})
    body_ref = body.get("$ref", "").split("/")[-1] if body else ""
    resp = spec.get("responses", {}).get("200", {}).get("content", {}).get("application/json", {}).get("schema", {})
    resp_ref = resp.get("$ref", "").split("/")[-1] if resp else ""
    out = {"query": query, "body": body_ref, "response": resp_ref}
    for key, sch in (("bodyProps", body), ("responseProps", resp)):
        if sch and not sch.get("$ref") and isinstance(sch.get("properties"), dict):
            req = set(sch.get("required") or [])
            out[key] = {name: {"type": _prop_type(prop), "required": name in req,
                               "enum": sorted(prop.get("enum", []), key=lambda x: str(x))}
                        for name, prop in sorted(sch["properties"].items())}
    return out


# ---------------- schema 字段级深对比 ----------------
# param_fingerprint 只比 $ref 名字：VO/DTO 内部增删字段（如 icon/chainIcon）
# 时 ref 名不变，指纹恒等。以下按「已覆盖接口可达的 schema」逐字段比对。

def _prop_type(v) -> str:
    if not isinstance(v, dict):
        return ""
    ref = v.get("$ref", "")
    if ref:
        return ref.split("/")[-1]
    t = v.get("type", "")
    if t == "array":
        items = v.get("items") or {}
        it = items.get("$ref", "").split("/")[-1] or items.get("type", "")
        return f"array<{it}>"
    return t


def schema_props(components: dict, name: str) -> dict:
    """{字段名: type/required/enum}；schema 不存在时返回空 dict。"""
    sch = (components or {}).get(name) or {}
    props = sch.get("properties") or {}
    req = set(sch.get("required") or [])
    return {k: {"type": _prop_type(v), "required": k in req,
                "enum": sorted(v.get("enum", []), key=lambda x: str(x))}
            for k, v in props.items()}


def _collect_refs(node, acc: set) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and "/schemas/" in ref:
            acc.add(ref.split("/")[-1])
        for v in node.values():
            _collect_refs(v, acc)
    elif isinstance(node, list):
        for v in node:
            _collect_refs(v, acc)


def endpoint_schema_names(spec: dict, components: dict, max_depth: int = 6) -> set:
    """操作直接 + 传递引用到的 schema 名集合（限深防环）。"""
    seen: set = set()
    frontier: set = set()
    _collect_refs(spec, frontier)
    depth = 0
    while frontier and depth < max_depth:
        seen |= frontier
        nxt: set = set()
        for name in frontier:
            _collect_refs((components or {}).get(name) or {}, nxt)
        frontier = nxt - seen
        depth += 1
    return seen


def diff_covered_schemas(local: dict, apifox: dict, lo: dict, ao: dict,
                         covered: set) -> list[dict]:
    """已覆盖接口可达 schema 的字段级 diff（新增/删除/类型变更）。"""
    local_comps = (local.get("components") or {}).get("schemas") or {}
    apifox_comps = (apifox.get("components") or {}).get("schemas") or {}

    schema_endpoints: dict[str, list[str]] = {}
    for key in sorted(covered):
        if key not in lo or key not in ao:
            continue
        names = (endpoint_schema_names(ao[key], apifox_comps)
                 | endpoint_schema_names(lo[key], local_comps))
        for n in names:
            schema_endpoints.setdefault(n, []).append(f"{key[0]} {key[1]}")

    diffs: list[dict] = []
    for name in sorted(schema_endpoints):
        lp = schema_props(local_comps, name)
        ap = schema_props(apifox_comps, name)
        if lp == ap:
            continue
        added = sorted(set(ap) - set(lp))
        removed = sorted(set(lp) - set(ap))
        type_changed = sorted(k for k in set(lp) & set(ap) if lp[k]["type"] != ap[k]["type"])
        constraint_changed = sorted(k for k in set(lp) & set(ap) if lp[k]["type"] == ap[k]["type"] and lp[k] != ap[k])
        if not (added or removed or type_changed or constraint_changed):
            continue
        diffs.append({
            "schema": name,
            "added": added,
            "removed": removed,
            "typeChanged": [
                {"field": k, "local": lp[k]["type"], "apifox": ap[k]["type"]} for k in type_changed
            ],
            "constraintChanged": [
                {"field": k, "local": lp[k], "apifox": ap[k]} for k in constraint_changed
            ],
            "allEndpoints": sorted(set(schema_endpoints[name])),
            "endpoints": sorted(set(schema_endpoints[name]))[:6],
        })
    return diffs


def removed_from_apifox(
    local: dict,
    apifox: dict,
    path_index: dict | None = None,
) -> list[dict]:
    """Apifox 在线已不存在的所选项目操作（method+path），附关联用例 id。"""
    lo = load_ops_from_oas(local)
    ao = load_ops_from_oas(apifox)
    items: list[dict] = []
    for method, path in sorted(lo.keys()):
        if not _in_scope(path):
            continue
        if (method, path) in ao:
            continue
        key = f"{method} {path}"
        info = (path_index or {}).get(key) or {}
        case_ids = sorted(set(info.get("cases") or []))
        items.append({
            "endpoint": key,
            "method": method,
            "path": path,
            "hasCases": bool(case_ids),
            "caseIds": case_ids,
            "modules": sorted(info.get("modules") or []),
        })
    return items


def audit(local: dict, apifox: dict, covered: set[tuple[str, str]]) -> dict:
    lo = load_ops_from_oas(local)
    ao = load_ops_from_oas(apifox)

    local_trade = {f"{m} {p}" for m, p in lo if _in_scope(p)}
    apifox_trade = {f"{m} {p}" for m, p in ao if _in_scope(p)}

    missing_in_local = sorted(f"{m} {p}" for (m, p) in covered if (m, p) not in lo)
    missing_in_apifox = sorted(f"{m} {p}" for (m, p) in covered if (m, p) not in ao)
    new_in_apifox = sorted(apifox_trade - local_trade)
    stale_in_local = sorted(local_trade - apifox_trade)

    contract_diffs = []
    for key in sorted(covered):
        if key not in lo or key not in ao:
            continue
        lp = param_fingerprint(lo[key])
        # Apifox index may not have full spec; skip empty
        ap = param_fingerprint(ao[key])
        if ap.get("query") or ap.get("body") or ap.get("response") or ap.get("bodyProps") or ap.get("responseProps"):
            if lp != ap:
                contract_diffs.append({"endpoint": f"{key[0]} {key[1]}", "local": lp, "apifox": ap})

    schema_diffs = diff_covered_schemas(local, apifox, lo, ao, covered)

    return {
        "covered_count": len(covered),
        "local_trade_paths": len(local_trade),
        "apifox_trade_paths": len(apifox_trade),
        "missing_in_local": missing_in_local,
        "missing_in_apifox": missing_in_apifox,
        "new_in_apifox": new_in_apifox,
        "stale_in_local": stale_in_local,
        "contract_diffs": contract_diffs,
        "schema_diffs": schema_diffs,
    }


def render_report(result: dict, audit_date: str, apifox_download: str | None) -> str:
    raise RuntimeError("报告只由 scripts/run_apifox_inspection.py 生成")
    # Kept below only for historical context; unreachable.
    lines = [
        f"# Apifox 契约巡检报告 · {audit_date}",
        "",
        "[[../Apifox接口巡检计划|← 巡检计划]] · [[../../index|知识库 index]] · [[../Apifox测试覆盖/覆盖清单|覆盖清单]]",
        "",
        "## 摘要",
        "",
        f"| 项 | 值 |",
        f"| --- | --- |",
        f"| 已接入 HTTP 操作 | {result['covered_count']} |",
        f"| 本地快照 trade 路径数 | {result['local_trade_paths']} |",
        f"| Apifox 在线 trade 路径数 | {result['apifox_trade_paths']} |",
        f"| 覆盖接口缺失于本地快照 | {len(result['missing_in_local'])} |",
        f"| 覆盖接口缺失于 Apifox | {len(result['missing_in_apifox'])} |",
        f"| Apifox 新增（本地无） | {len(result['new_in_apifox'])} |",
        f"| Apifox 已删除（基线有） | {len(result.get('removed_from_apifox') or [])} |",
        f"| 契约变更（指纹级） | {len(result['contract_diffs'])} |",
        f"| 契约变更（schema 字段级） | {len(result['schema_diffs'])} |",
    ]
    if apifox_download:
        lines.append(f"| Apifox 拉取时间 | {apifox_download} |")

    if result["schema_diffs"]:
        lines += ["", "## schema 字段级契约变更（更新 R 断言 / D FIELD_MAP / LIVE payload）", ""]
        for sd in result["schema_diffs"][:20]:
            parts = []
            if sd["added"]:
                parts.append("新增 " + ", ".join(f"`{x}`" for x in sd["added"]))
            if sd["removed"]:
                parts.append("删除 " + ", ".join(f"`{x}`" for x in sd["removed"]))
            if sd["typeChanged"]:
                parts.append("类型变更 " + ", ".join(f"`{t['field']}`" for t in sd["typeChanged"]))
            eps = " ".join(f"`{e}`" for e in sd["endpoints"][:3])
            lines.append(f"- `{sd['schema']}`：{'；'.join(parts)}（{eps}）")

    if result["missing_in_local"]:
        lines += ["", "## 本地 OpenAPI 快照缺失（需配置并更新 `PLATFORM_OAS_BASELINE`）", ""]
        for x in result["missing_in_local"]:
            lines.append(f"- `{x}`")

    removed = result.get("removed_from_apifox") or []
    if removed:
        with_cases = [r for r in removed if r.get("hasCases")]
        lines += [
            "",
            "## Apifox 已删除接口（基线有、在线无）",
            "",
            f"共 **{len(removed)}** 条操作，其中 **{len(with_cases)}** 条仍有用例。"
            " 有用例的项需人工确认「删除用例」或「保留（Apifox 误删/待恢复）」后再交 Agent。",
            "",
            "| 接口 | 用例 | 模块 |",
            "| --- | --- | --- |",
        ]
        for r in removed:
            cases = ", ".join(f"`{x}`" for x in r["caseIds"]) if r["caseIds"] else "—"
            mods = ", ".join(r["modules"]) if r["modules"] else "—"
            lines.append(f"| `{r['endpoint']}` | {cases} | {mods} |")

    if result["missing_in_apifox"]:
        lines += ["", "## 覆盖清单与 Apifox 不一致（修正模块 RUNNER_API / case.api）", ""]
        for x in result["missing_in_apifox"]:
            lines.append(f"- `{x}`")

    if result["new_in_apifox"]:
        lines += ["", "## Apifox 新增接口（未接入用例，抽样前 20）", ""]
        for p in result["new_in_apifox"][:20]:
            lines.append(f"- `{p}`")
        if len(result["new_in_apifox"]) > 20:
            lines.append(f"- … 另有 {len(result['new_in_apifox']) - 20} 条")

    lines += [
        "",
        "## 后续动作",
        "",
        "1. 运行各模块 `python3 test-platform/.../*_cases.py`（SAFE）验证现网契约。",
        "2. 失败项对照 Apifox `$ref` 详情（MCP `read_project_oas_ref_resources_y0qspa`）更新 `*_cases.py` / `*_db.py` / 面板工具。",
        "3. 从 Apifox 导出完整 OpenAPI 并更新 `PLATFORM_OAS_BASELINE` 指向的基线文件。",
        "4. 刷新 [[../Apifox测试覆盖/覆盖清单]]：`python3 scripts/gen_apifox_coverage_wiki.py`。",
        "",
    ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apifox-index", type=Path, default=None)
    parser.add_argument("--write-report", action="store_true")
    parser.add_argument("--json", action="store_true", help="输出 JSON 摘要（供 sync / 面板）")
    args = parser.parse_args()

    apifox_index = args.apifox_index or DEFAULT_APIFOX_INDEX
    if apifox_index is None or not apifox_index.exists():
        err = "Apifox index not found: configure PLATFORM_APIFOX_INDEX or pass --apifox-index"
        if args.json:
            print(json.dumps({"ok": False, "error": err}, ensure_ascii=False))
        else:
            print(err, file=sys.stderr)
            print("Run fetch_apifox_oas.py or MCP refresh first", file=sys.stderr)
        sys.exit(1)
    try:
        if LOCAL_OAS is None:
            raise ValueError("[incomplete] 未配置 PLATFORM_OAS_BASELINE")
        local = load_openapi_snapshot(LOCAL_OAS)
        apifox = load_openapi_snapshot(apifox_index)
    except ValueError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        else:
            print(str(exc), file=sys.stderr)
        sys.exit(1)
    covered = covered_ops()
    _rows, path_index, _unresolved = collect()
    result = audit(local, apifox, covered)
    result["removed_from_apifox"] = removed_from_apifox(local, apifox, path_index)
    dl = apifox.get("info", {}).get("x-download-time")

    if args.write_report:
        print("报告只由 python3 scripts/run_apifox_inspection.py 生成。", file=sys.stderr)
        sys.exit(2)

    if not args.json:
        print(f"Covered ops: {result['covered_count']}")
        print(f"Missing in local snapshot: {len(result['missing_in_local'])}")
        print(f"Missing in Apifox (coverage map error): {len(result['missing_in_apifox'])}")
        print(f"New in Apifox (trade): {len(result['new_in_apifox'])}")
        print(f"Contract diffs (param/ref): {len(result['contract_diffs'])}")
        print(f"Schema field diffs: {len(result['schema_diffs'])}")
        for sd in result["schema_diffs"][:10]:
            parts = []
            if sd["added"]:
                parts.append("+" + " +".join(sd["added"]))
            if sd["removed"]:
                parts.append("-" + " -".join(sd["removed"]))
            if sd["typeChanged"]:
                parts.append("~" + " ~".join(t["field"] for t in sd["typeChanged"]))
            print(f"  {sd['schema']}: {' '.join(parts)}")

    if args.json:
        payload = {
            "ok": True,
            "audit": {
                "coveredCount": result["covered_count"],
                "missingInLocal": len(result["missing_in_local"]),
                "missingInApifox": len(result["missing_in_apifox"]),
                "newInApifox": len(result["new_in_apifox"]),
                "staleInLocal": len(result["stale_in_local"]),
                "contractDiffs": len(result["contract_diffs"]),
                "schemaDiffs": len(result["schema_diffs"]),
                "newInApifoxSample": result["new_in_apifox"][:8],
                "missingInApifoxSample": result["missing_in_apifox"][:8],
                "removedFromApifox": len(result["removed_from_apifox"]),
                "removedWithCases": sum(1 for r in result["removed_from_apifox"] if r["hasCases"]),
                "removedFromApifoxDetail": result["removed_from_apifox"],
                "schemaDiffSample": result["schema_diffs"][:8],
            },
            "apifoxDownloadTime": dl,
        }
        print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
