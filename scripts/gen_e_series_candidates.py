#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 OpenAPI 快照机械派生 E 系列（异常/安全）候选用例清单。

按接口参数 schema 自动枚举高度模板化的异常场景（对齐《异常与安全测试规范》§2 矩阵）：
  【通用鉴权】
  - 无 token（预期 401）
  - 坏/过期 token（预期 401）
  - 跨端/越权调用（低权限端 token 调管理/内部/风控接口，预期越权拒绝）
  - tenant-id 错位（拒绝或空，禁止串租户）
  - 公开接口（/public/）反向标注：无 token 应放行，不生成鉴权拒绝候选
  【参数健壮性】
  - 每个 required query 参数缺失一条
  - 每个 integer/枚举参数非法值一条 + 数值边界（溢出/负数）
  - 每个自由文本筛选参数注入探针（SQL/XSS/%通配符/超长串/emoji）
  - 时间参数非法（From > To）
  【分页滥用】
  - 分页越界（current=0/-1/999999, size=0/-1）
  - size 超大（size=10000，防全量拉表）
  【水平越权】（规范最高优先级）
  - 列表/详情接口带资源 id（userId/clientId/orderId/{id} 等）：用户 A token 传用户 B 资源 id
  - 单资源详情接口：不存在 id（0/-1/99999999）应 not-found 而非 5xx
  【写接口补充】
  - requestBody 逐字段：必填缺失 / 枚举违规 / 类型违规（$ref 自动展开 components/schemas）
  - 错误 method（GET 调写接口）应 405/拒绝
  - 错误 content-type（text/plain）应拒绝
  - create/save/submit 类：重复提交应幂等或拒绝

并对照 coverage.json 标记该路径现有用例数，供 Agent 按《异常与安全测试规范》
补全「具体业务码断言」后落地为 *_cases.py。本脚本只产出候选，不写死断言码。
注入/越权/拒绝断言统一复用 test-platform/common/security_helpers.py，本脚本不复制 payload。

用法：
  python3 scripts/gen_e_series_candidates.py --prefix /v1/items
  python3 scripts/gen_e_series_candidates.py --prefix /v1/users --json
  python3 scripts/gen_e_series_candidates.py --covered-only   # 只针对已接入用例的路径
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from candidate_inputs import load_coverage as _load_coverage
from candidate_inputs import load_openapi_snapshot, iter_openapi_operations

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "scripts"))
from project_inputs import init_project_pack, pack_data_path, project_input_path
LOCAL_OAS = project_input_path("PLATFORM_OAS_BASELINE", required=False)
COVERAGE_JSON = pack_data_path("coverage.json")
SELECTED_PACKS = init_project_pack(ROOT)

SKIP_PARAMS = {"current", "size"}

# Project role/path semantics are supplied by the selected Pack manifest. With no
# declaration, cross-side candidates stay explicitly unconfirmed.
ROLE_MAP = []
SELF_SERVICE_ROLES = set()
PUBLIC_PATH_SEGMENTS = set()
for selected_pack in SELECTED_PACKS:
    PUBLIC_PATH_SEGMENTS.update(selected_pack.get("e_series_public_path_segments") or [])
    for entry in selected_pack.get("e_series_role_map") or []:
        if not isinstance(entry, dict) or not str(entry.get("prefix") or "").startswith("/"):
            continue
        role = str(entry.get("role") or "other")
        ROLE_MAP.append((
            str(entry["prefix"]),
            (role, str(entry.get("cross_side_desc") or "用未授权身份调用该端接口"),
             bool(entry.get("auth_confirmed"))),
        ))
        if entry.get("self_service") is True:
            SELF_SERVICE_ROLES.add(role)
ROLE_MAP.sort(key=lambda item: len(item[0]), reverse=True)

# 资源标识参数名模式（含 userId/clientId/orderId 等，用于水平越权/越权 id）
RESOURCE_ID_PATTERN = r"(user|client|order|trade|inviter|invitee|agent|account|bag.*order|trade.*record|fix.*order|position).*id|^id$"

# 时间参数名模式（任何含 time/date 且含方向词的参数）
TIME_PARAM_PATTERN = r"(time|date).*(from|to|start|end)|(start|end).*(time|date)"

# 布尔/枚举/id 类参数：不应做字符串注入（注入只对自由文本有意义），避免噪声
NON_TEXT_PARAM_PATTERN = r"(^id$|.*id$|status|type|state|category|direction|mode|flag|^is[A-Z]|enable|sort|order|symbol$)"

# 单资源（详情）接口特征：路径动词或路径模板参数
SINGLE_RESOURCE_PATTERN = r"/(detail|info|view|get)(/|$)"

# 创建类写接口：重复提交应幂等或拒绝
CREATE_LIKE_PATTERN = r"/(create|save|submit|add|apply|bind|open)(/|$)"

from openapi_contract import resolve_schema, schema_issues


def load_coverage(*, required: bool = False) -> dict[str, dict]:
    return _load_coverage(COVERAGE_JSON, required=required)


def body_schema_of(spec: dict) -> dict:
    """取 requestBody 的 json schema 并展开 $ref。"""
    request_body = spec.get("requestBody") or {}
    if schema_issues(request_body):
        return resolve_schema(request_body)
    raw = (request_body
           .get("content", {})
           .get("application/json", {})
           .get("schema", {}))
    return resolve_schema(raw) if raw else {}


def _enum_from_schema(schema: dict) -> list:
    """从 schema.enum 提取枚举值（优先）。"""
    return schema.get("enum", []) if isinstance(schema.get("enum"), list) else []


def _enum_from_desc(desc: str) -> list[str]:
    """从中文描述里粗略抓 '0-禁用，1-启用' 这类枚举值（回退兜底）。"""
    vals = re.findall(r"(?<![0-9])(-?\d+)\s*[-:：]", desc or "")
    return sorted(set(vals))


def side_of(path: str) -> tuple[str, str, bool]:
    """返回 (角色标签, cross_side 文案, auth_confirmed)；未知端鉴权模型视为未确认。"""
    for prefix, role in ROLE_MAP:
        if path == prefix.rstrip("/") or path.startswith(prefix.rstrip("/") + "/"):
            return role
    return "other", "用低权限/他端 token 调该接口（确认端角色后细化）", False


def is_public(path: str) -> bool:
    """公开路径规则由所选 Pack 声明；没有规则不猜测公开语义。"""
    return bool(set(path.strip("/").split("/")) & PUBLIC_PATH_SEGMENTS)


def is_single_resource(method: str, path: str, all_query: list) -> bool:
    """单资源（详情）接口：适合派生「不存在 id」候选。"""
    if re.search(SINGLE_RESOURCE_PATTERN, path, re.I):
        return True
    if re.search(r"\{[^}]+\}", path):  # 路径模板 /xxx/{id}
        return True
    has_page = any(p["name"] in ("current", "size") for p in all_query)
    id_q = [p for p in all_query if re.fullmatch(r"id", p["name"], re.I)]
    return method == "GET" and not has_page and bool(id_q)


def candidate_key(endpoint: str, kind: str, param: str = None) -> str:
    """候选唯一键：endpoint + kind + param（用于跟踪哪些候选已实现）。

    例：
      GET /v1/items | no_token → "GET:/v1/items|no_token"
      GET /v1/records | cross_user | clientId → "GET:/v1/records|cross_user|clientId"
    """
    key = f"{endpoint}|{kind}"
    if param:
        key += f"|{param}"
    return key


def derive_for_op(method: str, path: str, spec: dict, cov: dict) -> dict:
    ep = f"{method} {path}"
    role, cross_side_desc, auth_confirmed = side_of(path)
    security = spec.get("security")
    public = security == [] or (isinstance(security, list) and {} in security)
    auth_known = security is not None or auth_confirmed
    if security is None:
        public = is_public(path)
        auth_known = auth_known or public

    all_params = [p for p in (spec.get("parameters") or [])]
    all_query = [p for p in all_params if p.get("in") == "query"]
    path_params = [p for p in all_params if p.get("in") == "path"]
    query = [p for p in all_query if p["name"] not in SKIP_PARAMS]
    has_pagination = any(p["name"] in ("current", "size") for p in all_query)
    single = is_single_resource(method, path, all_query)

    body = body_schema_of(spec)
    has_body = bool(body.get("properties")) and not schema_issues(body)

    candidates: list[dict] = []

    def add(kind, desc, expect, param=None, **extra):
        c = {"kind": kind, "desc": desc, "expect": expect,
             "key": candidate_key(ep, kind, param)}
        if param:
            c["param"] = param
        c.update(extra)
        candidates.append(c)

    # ===== 1. 通用鉴权 =====
    if public:
        add("public_no_auth", "公开接口：无 token 访问应放行",
            "按实际响应形状和 Adapter 判断成功；不得因缺认证拒绝")
    else:
        add("no_token", "无 token 访问", "按 Auth Adapter 确认 HTTP 状态与未登录业务码")
        add("bad_token", "伪造/过期 token", "按 Auth Adapter 确认 HTTP 状态与未登录业务码")
        add("bad_tenant", "错误 tenant-id", "确认项目租户隔离后断言拒绝或隔离，禁止串数据", uncertain=True)
        if auth_confirmed:
            add("cross_side", cross_side_desc,
                "按 Pack 权限与 Adapter 拒绝契约验证越权拒绝或隔离")
        else:
            add("cross_side", cross_side_desc + "（先确认该端是否走 user-token 鉴权）",
                "若走 user-token 则越权拒绝；否则据实际鉴权模型（内网/签名）调整预期，勿假设必拒",
                uncertain=True)

    if not auth_known:
        for candidate in candidates:
            candidate["uncertain"] = True
            candidate["expect"] = "先确认 Pack/Adapter 鉴权与租户契约；" + candidate["expect"]

    # ===== 2. 分页滥用 =====
    if has_pagination:
        add("page_abuse", "分页越界（current=0/-1/999999, size=0/-1）",
            "拒绝或安全兜底（空页），禁止 5xx")
        add("page_size_abuse", "size=10000 超大分页",
            "截断到上限或拒绝，禁止全量拉表（响应时间 & 条数双重判断）")

    # ===== 3. 水平越权：仅自服务端（cli/agent 查自己）才有意义 =====
    #   管理/内部端管理员本就可查任意用户，越权轴是跨租户（bad_tenant），不发 cross_user，
    #   否则会生成「预期方向错」的候选（管理员查他人本是设计内行为）。
    if role in SELF_SERVICE_ROLES:
        id_params = [p for p in (query + path_params)
                     if re.search(RESOURCE_ID_PATTERN, p["name"], re.I)]
        for p in id_params:
            name = p["name"]
            add("cross_user", f"用户 A token 传用户 B 的 {name}",
                "拒绝或结果仅归属 A，禁止返回 B 数据（security_helpers.assert_no_cross_user_leak）",
                param=name)

    # ===== 3b. 单资源不存在 id =====
    if single:
        for p in (query + path_params):
            if re.fullmatch(r"id", p["name"], re.I) or re.search(r"id$", p["name"], re.I):
                add("resource_not_found", f"{p['name']}=0/-1/99999999（不存在）", param=p["name"],
                    expect="not-found / 空 data，禁止 5xx")
                break  # 单资源只需主键一条

    # ===== 4. query 参数健壮性 =====
    time_params = {}  # {base: {"from": name, "to": name}}
    for p in query:
        name = p["name"]
        schema = p.get("schema") or {}
        ptype = schema.get("type")
        desc = p.get("description", "")

        # 4.1 必填缺失
        if p.get("required"):
            add("missing_required", f"必填参数 {name} 缺失", param=name,
                expect="参数校验失败业务码")

        if _enum_from_schema(schema):
            add("invalid_enum", f"{name} 传声明 enum 之外的值", param=name,
                expect="按参数与业务契约断言拒绝或空结果，禁止 5xx", enum=schema["enum"])

        # 4.2 数值：非法类型 + 边界
        if ptype in ("integer", "number"):
            enums = _enum_from_schema(schema) or _enum_from_desc(desc)
            hint = f"（合法枚举≈{enums[:3]}{'...' if len(enums) > 3 else ''}）" if enums else ""
            add("invalid_type", f"{name} 传非法值 -999/abc{hint}", param=name,
                expect="参数校验失败或空结果（勿假绿）")
            if not enums:  # 非枚举数值才谈边界（按 format 选溢出值，int64 不用 int32 值）
                fmt = schema.get("format")
                limits = {k: schema[k] for k in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum") if k in schema}
                if limits or (ptype == "integer" and fmt in ("int32", "int64")):
                    boundary = str(limits) if limits else ("9223372036854775808（超 int64）" if fmt == "int64" else "2147483648（超 int32）")
                    add("number_boundary", f"{name} 边界：{boundary}", param=name,
                        expect="按声明边界测试拒绝或安全兜底，禁止 5xx", constraints=limits, format=fmt)

        # 4.3 注入：仅自由文本参数（跳过 id/枚举/布尔/时间/带 schema.enum）
        elif ptype == "string":
            is_enum = bool(_enum_from_schema(schema))
            is_non_text = re.search(NON_TEXT_PARAM_PATTERN, name, re.I) or \
                re.search(TIME_PARAM_PATTERN, name, re.I)
            if not is_enum and not is_non_text:
                add("injection", f"{name} 注入探针（SQL/XSS/%_/超长串/emoji，见 security_helpers.INJECTION_STRINGS）",
                    param=name,
                    expect="被转义/无结果；禁止 5xx/SQL 报错/%_ 通配符全量命中")

        # 4.4 收集时间参数配对
        if re.search(TIME_PARAM_PATTERN, name, re.I):
            lower = name.lower()
            if "from" in lower:
                time_params.setdefault(re.sub(r"from$", "", lower), {})["from"] = name
            elif "to" in lower:
                time_params.setdefault(re.sub(r"to$", "", lower), {})["to"] = name
            elif lower == "starttime" or lower.endswith("start"):
                time_params.setdefault("time", {})["from"] = name
            elif lower == "endtime" or lower.endswith("end"):
                time_params.setdefault("time", {})["to"] = name

    # 4.5 时间倒置
    for base, pair in time_params.items():
        if "from" in pair and "to" in pair:
            add("invalid_time_range", f"{pair['from']} > {pair['to']}（时间倒置）",
                param=f"{pair['from']}+{pair['to']}",
                params=[pair["from"], pair["to"]],
                expect="拒绝或空结果，禁止 5xx")

    # ===== 5. 写接口（POST/PUT） =====
    if method in ("POST", "PUT", "PATCH", "DELETE"):
        # 5.1 错误 method / content-type
        if "GET" not in spec.get("x-platform-path-methods", []):
            add("method_not_allowed", "用 GET 调该写接口", "确认路由后断言 405 / 拒绝", uncertain=True)
        if has_body:
            add("wrong_content_type", "Content-Type: text/plain 提交 body", "拒绝，禁止按 JSON 解析落库")

        # 5.2 body 逐字段
        if has_body:
            props = body.get("properties", {})
            required = body.get("required", []) or []
            for fname in required:
                add("body_missing_required", f"requestBody 缺必填字段 {fname}", param=fname,
                    expect="参数校验失败业务码，禁止落库")
            for fname, fschema in props.items():
                fschema = resolve_schema(fschema) if isinstance(fschema, dict) else {}
                enums = _enum_from_schema(fschema)
                if enums:
                    add("body_enum_violation",
                        f"{fname} 传非法枚举值（合法≈{enums[:4]}{'...' if len(enums) > 4 else ''}）",
                        param=fname, expect="枚举校验失败业务码，禁止落库")
                elif fschema.get("type") in ("integer", "number"):
                    add("body_type_violation", f"{fname} 传类型错误值（字符串/负数越界）", param=fname,
                        expect="类型校验失败业务码，禁止落库")
                elif fschema.get("type") == "string" and fschema.get("maxLength"):
                    add("body_maxlength", f"{fname} 超长（> maxLength={fschema['maxLength']}）", param=fname,
                        expect="长度校验失败或安全截断，禁止落库超长值")
        elif spec.get("requestBody"):
            # body 存在但 $ref 未能展开 → 占位提醒
            add("request_schema_unresolved", "requestBody 未能派生字段约束，需核对完整契约",
                expect="确认 schema/媒体类型后再生成拒绝断言", uncertain=True,
                reasons=schema_issues(body))

        # 5.3 重复提交（create 类）
        if re.search(CREATE_LIKE_PATTERN, path, re.I):
            add("duplicate_submit", "相同 body 连续提交两次",
                "幂等或拒绝重复，禁止产生两条重复记录（需库表校验）")

    return {
        "endpoint": ep,
        "side": role,
        "public": public,
        "single_resource": single,
        "summary": spec.get("summary", ""),
        "existingCaseCount": cov.get("caseCount", 0),
        "covered": bool(cov.get("covered")),
        "candidateCount": len(candidates),
        "candidates": candidates,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prefix", default="", help="只处理该路径前缀，如 /v1/users")
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
        results.append(derive_for_op(method.upper(), path, spec, cov))

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=1))
        return

    total_cand = sum(r["candidateCount"] for r in results)
    print(f"接口 {len(results)} 个，派生 E 候选 {total_cand} 条"
          + (f"（前缀 {args.prefix}）" if args.prefix else "") + "\n")
    for r in results:
        flag = f"已覆盖 {r['existingCaseCount']} 条" if r["covered"] else "★ 未覆盖"
        tags = "".join(t for t in [" [公开]" if r["public"] else "",
                                   " [单资源]" if r["single_resource"] else ""])
        print(f"● {r['endpoint']}  [{r['summary']}]  ({flag}){tags}")
        for c in r["candidates"]:
            print(f"    - [{c['kind']}] {c['desc']} → {c['expect']}")
        print()


if __name__ == "__main__":
    main()
