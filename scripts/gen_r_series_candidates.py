#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 OpenAPI 成功响应 schema 机械派生 R 系列（只读契约）候选清单。

对齐《只读契约测试规范》：把「接口可调通 + 响应结构正确」升级为**逐字段契约**，
而非只断言 success=true。自动展开 components/schemas 走链路 wrapper→data→row VO：

  契约基线   → 仅对 OAS 明确声明 success/code/msg/data 的标准信封断言
  分页信封   → 按声明携带字段约束；线型与回显语义由 Pack/Adapter 确认
  字段契约   → 字段出现时类型匹配；存在性仅按 required，空值按 schema 与业务证据
  枚举契约   → 带 enum 的字段取值 ∈ 枚举集
  原始响应   → 按顶层标量/数组/对象类型生成候选，不臆造 data 信封
  文件/未知  → 明确二进制响应验证文件；schema 不足时标记待确认，不猜测信封或文件

字段清单随候选带出（fields），Agent 落地时可直接生成 assert_record_shape 断言。
本脚本只产出候选，不写死断言码。

用法：
  python3 scripts/gen_r_series_candidates.py --prefix /v1/items
  python3 scripts/gen_r_series_candidates.py --covered-only --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from candidate_inputs import load_coverage as _load_coverage
from candidate_inputs import load_openapi_snapshot, iter_openapi_operations

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT / "scripts"))
from project_inputs import pack_data_path, project_input_path  # noqa: E402
LOCAL_OAS = project_input_path("PLATFORM_OAS_BASELINE", required=False)
COVERAGE_JSON = pack_data_path("coverage.json")
from openapi_contract import resolve_schema, schema_issues, schema_type  # noqa: E402

FILE_MEDIA_TYPES = {
    "application/octet-stream",
    "application/pdf",
    "application/zip",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "text/csv",
}


def load_coverage(*, required: bool = False) -> dict[str, dict]:
    return _load_coverage(COVERAGE_JSON, required=required)


def candidate_key(endpoint: str, kind: str, param: str = None) -> str:
    key = f"{endpoint}|{kind}"
    if param:
        key += f"|{param}"
    return key


def _fields_of(schema: dict) -> list[dict]:
    """展开一个对象 schema 的字段清单：[{name,type,enum,format,required}]。

    required 取自 schema 声明的 required 集——只有它们才应断言「必须存在」；
    其余字段可空/可省略（部分实现会省略 null），只能断言「出现即类型正确」。
    """
    schema = resolve_schema(schema)
    required = set(schema.get("required") or [])
    out = []
    for name, fs in (schema.get("properties") or {}).items():
        fs = resolve_schema(fs) if isinstance(fs, dict) else {}
        out.append({
            "name": name,
            "type": schema_type(fs) or ("array" if fs.get("items") else "object" if fs.get("properties") else None),
            "nullable": fs.get("nullable", False) or (isinstance(fs.get("type"), list) and "null" in fs["type"]),
            "schema": fs,
            "enum": fs.get("enum") if isinstance(fs.get("enum"), list) else None,
            "format": fs.get("format"),
            "required": name in required,
        })
    return out


def walk_response(spec: dict) -> dict | None:
    """解析成功响应，区分标准信封、原始值、文件和 OAS 未知结构。"""
    responses = spec.get("responses") or {}
    successful = {str(status): response for status, response in responses.items()
                  if str(status).startswith("2") and isinstance(response, dict)}
    if not successful:
        return {"kind": "unresolved", "fields": [], "mediaTypes": [],
                "enveloped": False, "reason": "缺少可用成功响应，需确认状态与响应契约"}
    if len(successful) != 1:
        return {"kind": "unresolved", "fields": [], "mediaTypes": [],
                "enveloped": False, "reason": "多个成功响应，需分别确认状态和响应形状"}
    status, response = next(iter(successful.items()))
    response_issues = schema_issues(response)
    if response_issues:
        return {"kind": "unresolved", "fields": [], "mediaTypes": [],
                "enveloped": False, "reason": "; ".join(dict.fromkeys(response_issues))}
    if status == "204" and not response.get("content"):
        return {"kind": "no_content", "fields": [], "mediaTypes": [], "enveloped": False}
    r = response.get("content", {})
    if not isinstance(r, dict) or not r:
        return {"kind": "unresolved", "fields": [], "mediaTypes": [],
                "enveloped": False, "reason": "成功响应缺少 content/schema，需确认响应体"}
    media_types = [str(media_type).split(";", 1)[0].strip().lower() for media_type in r]
    media_entries = [(media_type, value) for media_type, value in zip(media_types, r.values())
                     if isinstance(value, dict)]
    if len(media_entries) != 1 or len(media_entries) != len(r):
        return {
            "kind": "unresolved", "fields": [], "mediaTypes": media_types,
            "enveloped": False,
            "reason": "多个或无效的响应媒体类型，需确认实际响应表示",
        }

    media_type, media = media_entries[0]
    raw_schema = media.get("schema", {})
    if not isinstance(raw_schema, dict):
        raw_schema = {}
    schema = resolve_schema(raw_schema)

    issues = schema_issues(schema)
    if issues:
        return {"kind": "unresolved", "fields": [], "mediaTypes": media_types,
                "enveloped": False, "reason": "; ".join(dict.fromkeys(issues))}

    file_media_types = [media_type for media_type in media_types if media_type in FILE_MEDIA_TYPES]
    is_binary_schema = schema.get("type") == "string" and schema.get("format") == "binary"
    if file_media_types or is_binary_schema:
        return {
            "kind": "file",
            "mediaTypes": media_types,
            "enveloped": False,
            "fields": [],
        }

    wrapper = resolve_schema(schema)
    props = wrapper.get("properties") or {}
    envelope_markers = {"success", "code", "msg", "data"}
    if "data" in props and not envelope_markers.issubset(props):
        return {
            "kind": "unresolved", "fields": [], "mediaTypes": media_types,
            "enveloped": False,
            "reason": "schema 含 data 但未声明完整 success/code/msg/data 标准信封",
        }
    if "data" not in props:
        top_type = schema_type(wrapper)
        if top_type == "union":
            return {"kind": "raw_union", "schema": wrapper, "fields": [],
                    "mediaTypes": media_types, "enveloped": False}
        if top_type in ("boolean", "string", "integer", "number", "null"):
            return {
                "kind": "raw_scalar", "scalarType": top_type, "schema": wrapper,
                "mediaTypes": media_types, "enveloped": False, "fields": [],
            }
        if top_type == "array":
            item_schema = wrapper.get("items") or {}
            return {
                "kind": "raw_array", "fields": _fields_of(item_schema),
                "itemType": schema_type(resolve_schema(item_schema)), "itemSchema": resolve_schema(item_schema),
                "mediaTypes": media_types, "enveloped": False,
            }
        if top_type == "object" and props:
            return {
                "kind": "raw_object", "fields": _fields_of(wrapper),
                "mediaTypes": media_types, "enveloped": False,
            }
        return {
            "kind": "unresolved", "fields": [],
            "mediaTypes": media_types, "enveloped": False,
        }

    data = resolve_schema(props["data"])
    # 分页：data 有 records 数组
    dprops = data.get("properties") or {}
    if "records" in dprops:
        records = resolve_schema(dprops["records"] or {})
        if schema_type(records) != "array":
            return {"kind": "unresolved", "fields": [], "mediaTypes": media_types,
                    "enveloped": False, "reason": "records 未声明 array，不推断分页结构"}
        row = records.get("items", {})
        return {"kind": "page", "fields": _fields_of(row), "pageFields": _fields_of(data), "mediaTypes": media_types, "enveloped": True, "envelopeFields": _fields_of(wrapper)}
    # 数组：data 本身是数组
    if schema_type(data) == "array":
        item = (props["data"].get("items") or data.get("items") or {})
        return {"kind": "array", "fields": _fields_of(item), "itemType": resolve_schema(item).get("type"), "itemSchema": resolve_schema(item), "mediaTypes": media_types, "enveloped": True, "envelopeFields": _fields_of(wrapper)}
    if schema_type(data) == "union":
        return {"kind": "union", "dataSchema": data, "fields": [], "mediaTypes": media_types,
                "enveloped": True, "envelopeFields": _fields_of(wrapper)}
    # 标量
    if schema_type(data) in ("boolean", "string", "integer", "number", "null"):
        return {"kind": "scalar", "scalarType": schema_type(data), "dataSchema": data, "mediaTypes": media_types, "enveloped": True, "envelopeFields": _fields_of(wrapper)}
    # 对象 VO（详情）
    if dprops:
        return {"kind": "object", "fields": _fields_of(data), "mediaTypes": media_types, "enveloped": True, "envelopeFields": _fields_of(wrapper)}
    return {"kind": "envelope_only", "fields": [], "mediaTypes": media_types, "enveloped": True, "envelopeFields": _fields_of(wrapper)}


def derive_for_op(method: str, path: str, spec: dict, cov: dict) -> dict | None:
    if method != "GET":
        return None  # R 只读契约针对读接口
    resp = walk_response(spec)
    if resp is None:
        return None
    ep = f"{method} {path}"
    candidates: list[dict] = []

    def add(kind, desc, expect, **extra):
        c = {"kind": kind, "desc": desc, "expect": expect, "key": candidate_key(ep, kind)}
        c.update(extra)
        candidates.append(c)

    kind = resp["kind"]
    fields = resp.get("fields", [])
    biz_fields = fields
    enum_fields = [f for f in biz_fields if f["enum"]]

    req_names = [f["name"] for f in biz_fields if f["required"]]

    def _contract_expect(scope: str) -> str:
        base = f"{scope}每字段「出现即类型匹配」（可空/可省略字段不强制存在）"
        if req_names:
            return base + f"；OAS 必返字段 {req_names[:6]} 必须存在；null/空值是否允许按各字段 schema 与业务证据判断"
        return base + "；OAS 未标必返字段，故不断言存在性（空对象/空数组是否允许须由业务样本与契约确认）"

    def _fields_payload(fs):
        return [dict(f) for f in fs]

    if resp.get("enveloped"):
        add("contract_base", "响应信封契约", "HTTP 状态按接口契约；字段类型/必返按 schema；成功业务码由所选 Adapter 确认",
            uncertain=True, confirmation="Adapter 成功语义", fields=_fields_payload(resp.get("envelopeFields", [])))
    elif kind == "no_content":
        add("no_content_response", "204 无响应体契约", "HTTP 204；响应体为空")
    elif kind == "file":
        add("file_response_contract", "文件响应契约",
            "HTTP 成功；Content-Type 与文件类型相符；响应体非空，并按已知格式校验文件签名",
            mediaTypes=resp.get("mediaTypes", []))
    elif kind == "raw_union":
        add("raw_response_union", "未包裹响应联合约束",
            "校验完整 schema：oneOf 恰好命中一个分支，anyOf 至少命中一个；保留各分支线型与值域，不预先转换",
            schema=resp["schema"], mediaTypes=resp.get("mediaTypes", []))
    elif kind == "raw_scalar":
        add("raw_response_type", f"未包裹响应标量类型 = {resp['scalarType']}",
            f"响应 body 顶层类型匹配 {resp['scalarType']}（OAS 未声明标准 data 信封）",
            scalarType=resp["scalarType"], schema=resp.get("schema"), mediaTypes=resp.get("mediaTypes", []))
    elif kind == "raw_array":
        add("raw_response_array", "未包裹响应数组契约",
            "响应 body 顶层为数组，数组项类型与 OAS schema 一致",
            itemType=resp.get("itemType"), itemSchema=resp.get("itemSchema"), fields=_fields_payload(biz_fields),
            mediaTypes=resp.get("mediaTypes", []))
    elif kind == "raw_object":
        add("raw_response_object", "未包裹响应对象契约",
            _contract_expect("响应对象 "),
            fields=_fields_payload(biz_fields), mediaTypes=resp.get("mediaTypes", []))
    elif kind == "unresolved":
        add("response_schema_unresolved", "响应 schema 不足以派生契约",
            resp.get("reason") or
            "按实际接口确认响应 body 与 Content-Type；OAS 未给可用结构，不假设 success/code/msg 或文件格式",
            mediaTypes=resp.get("mediaTypes", []), uncertain=True)

    if kind == "page":
        add("page_envelope", "分页信封结构",
            "分页字段类型和必返按 schema；请求回显、total 线型及统计口径由 Pack/Adapter 确认",
            fields=_fields_payload(resp.get("pageFields", [])), uncertain=True)
        if biz_fields:
            add("field_contract", f"records 行字段契约（{len(biz_fields)} 字段，必返 {len(req_names)}）",
                _contract_expect("records 非空时，"), fields=_fields_payload(biz_fields))
    elif kind == "object":
        if biz_fields:
            add("field_contract", f"详情字段契约（{len(biz_fields)} 字段，必返 {len(req_names)}）",
                _contract_expect(""), fields=_fields_payload(biz_fields))
        else:
            add("field_contract", "data 对象存在", "data 非 null（结构见 OAS）")
    elif kind == "array":
        add("array_contract", f"data 为数组（项 {len(biz_fields)} 字段，必返 {len(req_names)}）",
            "data 为 list；项类型与 schema 一致；" + _contract_expect("非空时每项，"),
            fields=_fields_payload(biz_fields), itemType=resp.get("itemType"), itemSchema=resp.get("itemSchema"))
    elif kind == "union":
        add("data_union_contract", "data 联合线型与值域",
            "按完整 schema 校验原始 data：oneOf 恰好一个分支，anyOf 至少一个；禁止任意混收或只选首分支",
            schema=resp["dataSchema"])
    elif kind == "scalar":
        add("data_type", f"data 标量类型 = {resp['scalarType']}",
            f"data 类型匹配 {resp['scalarType']}；null 许可按 schema 确认", schema=resp.get("dataSchema"))

    if enum_fields:
        add("enum_field_contract",
            f"枚举字段取值受控（{', '.join(f['name'] for f in enum_fields)}）",
            "字段出现时值按 enum 校验；nullable 按 schema 版本/项目线型核实",
            fields=_fields_payload(enum_fields))

    return {
        "endpoint": ep,
        "respKind": kind,
        "fieldCount": len(biz_fields),
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
        r = derive_for_op(method.upper(), path, spec, cov)
        if r:
            results.append(r)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=1))
        return

    total_cand = sum(r["candidateCount"] for r in results)
    print(f"读接口 {len(results)} 个，派生 R 契约候选 {total_cand} 条"
          + (f"（前缀 {args.prefix}）" if args.prefix else "") + "\n")
    for r in results[:12]:
        flag = f"已覆盖 {r['existingCaseCount']} 条" if r["covered"] else "★ 未覆盖"
        print(f"● {r['endpoint']}  [{r['summary']}]  ({flag})  {r['respKind']}/{r['fieldCount']}字段")
        for c in r["candidates"]:
            extra = f"  fields={[f['name'] for f in c['fields']][:8]}" if c.get("fields") else ""
            print(f"    - [{c['kind']}] {c['desc']}{extra}")
        print()
    if len(results) > 12:
        print(f"... 还有 {len(results) - 12} 个接口，用 --json 查看完整列表")


if __name__ == "__main__":
    main()
