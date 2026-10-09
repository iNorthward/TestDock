#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 OpenAPI 快照生成接口画像骨架（供用例生成前沉淀事实源）。

按《接口用例生成工作流》，先为接口产出结构化画像（参数表 + 数据口径 + 权限占位），
Agent/人工补全业务口径后确认，再据此写 R/F/D/E/LIVE 用例。接口变更时重新生成 diff。

输出：所选 pack 配置的接口画像目录（不覆盖已确认画像，除非 --force）

用法：
  python3 scripts/gen_interface_profile.py --path /v1/items/page
  python3 scripts/gen_interface_profile.py --path /v1/items/page --method GET --force
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

from atomic_file import atomic_write_text
from candidate_inputs import HTTP_METHODS, load_openapi_snapshot, iter_openapi_operations

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from project_inputs import (business_knowledge_path, business_knowledge_subpath, init_project_pack,
                            pack_data_path, project_input_path, project_input_setting)  # noqa: E402

init_project_pack(ROOT)

from timezone_config import configure_timezone  # noqa: E402

configure_timezone()
LOCAL_OAS = project_input_path("PLATFORM_OAS_BASELINE", required=False)
COVERAGE_JSON = pack_data_path("coverage.json")
KB_ROOT = business_knowledge_path()
OUT_DIR = business_knowledge_subpath("PLATFORM_INTERFACE_PROFILE_SUBDIR")
from platform_config import project_path  # noqa: E402
SCHEMA_JSON = project_path("PLATFORM_SCHEMA_JSON")

SKIP_PARAMS: set[str] = set()


def select_operation(path_item: object, requested_method: str | None = None) -> tuple[str, dict]:
    """从 Path Item 中只选择有效 HTTP operation，不把 OAS 元数据当方法。"""
    if not isinstance(path_item, dict):
        raise ValueError("Path Item 必须是对象")

    if requested_method is not None:
        method = requested_method.strip().lower()
        if method not in HTTP_METHODS:
            raise ValueError(f"不支持的 HTTP 方法：{requested_method}")
        for key, operation in path_item.items():
            if isinstance(key, str) and key.lower() == method and isinstance(operation, dict):
                return method, operation
        raise ValueError(f"路径下没有有效的 {method.upper()} operation")

    for key, operation in path_item.items():
        if isinstance(key, str) and key.lower() in HTTP_METHODS and isinstance(operation, dict):
            return key.lower(), operation
    raise ValueError("Path Item 中没有有效 HTTP operation")


def load_schema_tables() -> dict[str, dict]:
    """加载 schema.json,返回 {表名: 表元信息}。"""
    if SCHEMA_JSON is None or not SCHEMA_JSON.exists():
        return {}
    data = json.loads(SCHEMA_JSON.read_text(encoding="utf-8"))
    tables = {}
    for t in data.get("tables", []):
        name = t.get("name")
        if name:
            # 列对象用 COLUMN_NAME（information_schema 导出格式）；旧代码取 c.get("name")
            # 恒为 None，导致 columns 变成 {None: ...}，软删/列检测长期失效。
            tables[name] = {
                "comment": t.get("comment", ""),
                "columns": {(c.get("COLUMN_NAME") or c.get("name")): c
                            for c in t.get("columns", [])},
            }
    return tables


def infer_tables_from_path(path: str, schema_tables: dict) -> list[str]:
    """Find tables in the configured schema whose suffix matches path entities.

    This is a candidate heuristic, not a contract; generated mappings still need
    human confirmation. Project table prefixes are discovered from the schema.
    """
    operation_words = {
        "page", "list", "query", "search", "detail", "info", "view", "create",
        "update", "delete", "remove", "save", "publish", "export", "statistics",
    }
    path_tokens: list[str] = []
    for segment in path.strip("/").split("/"):
        if not segment or segment.lower() == "api":
            continue
        if re.fullmatch(r"v\d+", segment, re.I) or segment.lower() in operation_words:
            continue
        snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", segment)
        path_tokens.extend(token.lower() for token in re.findall(r"[A-Za-z0-9]+", snake))
    if not path_tokens:
        return []

    for width in range(len(path_tokens), 0, -1):
        suffix = path_tokens[-width:]
        matched = []
        for table in schema_tables:
            table_tokens = [token.lower() for token in table.split("_") if token]
            if len(table_tokens) >= width and table_tokens[-width:] == suffix:
                matched.append(table)
        if matched:
            return sorted(matched)
    return []


def slugify(method: str, path: str) -> str:
    return f"{method.lower()}_" + re.sub(r"[^a-zA-Z0-9]+", "_", path.strip("/"))


def side_of(path: str) -> str:
    parts = [part for part in path.strip("/").split("/") if part]
    if parts and parts[0].lower() == "api" and len(parts) > 1:
        return parts[1]
    return parts[0] if parts else "other"


def _wiki_link(target: Path | None, alias: str | None = None) -> str:
    if OUT_DIR is None or target is None:
        return ""
    target_no_suffix = target.with_suffix("") if target.suffix == ".md" else target
    rel = os.path.relpath(target_no_suffix, OUT_DIR).replace(os.sep, "/")
    return f"[[{rel}|{alias}]]" if alias else f"[[{rel}]]"


def summary_from_coverage(method: str, path: str) -> str:
    if COVERAGE_JSON is None or not COVERAGE_JSON.exists():
        return ""
    data = json.loads(COVERAGE_JSON.read_text(encoding="utf-8"))
    for p in data.get("paths", []):
        if p["method"] == method and p["path"] == path:
            return p.get("summary", "")
    return ""


def render(method: str, path: str, spec: dict, schema_tables: dict) -> str:
    params = spec.get("parameters") or []
    query = [p for p in params if p.get("in") == "query" and p["name"] not in SKIP_PARAMS]
    has_page = any(p["name"] in ("current", "size") for p in query)
    side = side_of(path)
    summary = spec.get("summary") or summary_from_coverage(method, path)

    # 推断涉及表
    inferred_tables = infer_tables_from_path(path, schema_tables)
    if inferred_tables:
        table_line = ", ".join(f"`{t}`" for t in inferred_tables)
        table_comment = " / ".join(schema_tables[t]["comment"] for t in inferred_tables if schema_tables[t]["comment"])
        table_hint = f"{table_line}" + (f" ({table_comment})" if table_comment else "")
    else:
        table_hint = "（待补，查 PLATFORM_SCHEMA_JSON 指向的 schema 文件）"

    # 检测软删列名（从推断表的 columns 中找 is_deleted/deleted）
    soft_delete_col = None
    if inferred_tables:
        for t in inferred_tables:
            cols = schema_tables[t]["columns"]
            if "is_deleted" in cols:
                soft_delete_col = "is_deleted"
                break
            elif "deleted" in cols:
                soft_delete_col = "deleted"
                break

    lines = [
        f"# 接口画像 · {method} {path}",
        "",
        "> 由 `scripts/gen_interface_profile.py` 生成骨架；「数据口径」「权限模型」需人工/Agent 补全后确认。",
        f"> 关联规范：{_wiki_link(KB_ROOT / '规范' / '接口用例生成工作流.md' if KB_ROOT else None)}。",
        "",
        "## 基本信息",
        "",
        "| 项 | 值 |",
        "| --- | --- |",
        f"| 方法 / 路径 | `{method} {path}` |",
        f"| 端 | {side} |",
        f"| Apifox 摘要 | {summary or '（待补）'} |",
        f"| 分页 | {'是' if has_page else '否'} |",
        f"| 归属模块 | `test-platform/.../（待填）_cases.py` |",
        "",
        "## 参数（来自 OAS，禁止凭记忆增删）",
        "",
        "| 参数 | 位置 | 类型 | 必填 | 枚举/取值来源 | 说明 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    if query:
        for p in query:
            if p["name"] in ("current", "size"):
                continue
            ptype = (p.get("schema") or {}).get("type", "string")
            required = "是" if p.get("required") else "否"
            desc = (p.get("description") or "").replace("\n", " ").strip()
            lines.append(f"| `{p['name']}` | query | {ptype} | {required} | （待补） | {desc} |")
    else:
        lines.append("| （无 query 参数或为写接口 body） | | | | | |")

    body = spec.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {})
    if body:
        ref = body.get("$ref", "")
        lines += ["", f"> 请求体 schema：`{ref or 'inline'}`（写接口，body 字段见 OAS `$ref`，"
                      f"必要时用 MCP `read_project_oas_ref_resources_uqn5vu` 展开）。"]

    lines += [
        "",
        "## 数据口径（写 D 系列 SQL 前必填）",
        "",
        f"- **涉及表**：{table_hint}",
        "- **关联字段**：`（待补，查 Wiki 外键）`",
        f"- **软删过滤**：`{soft_delete_col or 'is_deleted'} = 0`" + ("（推断）" if soft_delete_col else "（确认本表列名）"),
        "- **租户过滤**：`（如适用）`",
        "- **业务态过滤**：`（发布态/审核态等）`",
        "- **金额/时间单位**：`（分/元 · 秒/毫秒）`",
        "",
        "## 权限模型",
        "",
        f"- **鉴权端**：{side}",
        "- **越权面**：（水平越权资源 id？跨端？租户隔离？）",
        "- **项目响应规则**：" + project_input_setting(
            "PLATFORM_RESPONSE_RULE_GUIDANCE", default="（待确认：依据所选 Pack / Adapter 与接口契约填写）"),
        "",
        "## 用例规划（R/F/D/E/LIVE）",
        "",
        "- **R**：（契约 + 结构要点）",
        f"- **F**：{'、'.join('`'+p['name']+'`' for p in query if p['name'] not in ('current','size')) or '（无筛选参数）'}",
        "- **D**：（对账口径）",
        f"- **E**：见 `python3 scripts/gen_e_series_candidates.py --prefix {path}`",
        "- **LIVE**：（写路径候选，待用户描述）" if method != "GET" else "- **LIVE**：读接口无",
        "",
        "## 变更记录",
        "",
        "| 日期 | 变更 |",
        "| --- | --- |",
        f"| {date.today().isoformat()} | 首次生成骨架（自动推断表名" + (f"：{', '.join(inferred_tables[:2])}" if inferred_tables else "") + "） |",
        "",
        "## 相关链接",
        "",
        f"- {_wiki_link(OUT_DIR / '画像清单.md' if OUT_DIR else None, '画像清单')} · {_wiki_link(KB_ROOT / 'index.md', '知识库 index') if KB_ROOT else ''}",
        f"- 工作流：{_wiki_link(KB_ROOT / '规范' / '接口用例生成工作流.md' if KB_ROOT else None)}",
        "- 覆盖清单：（生成后运行 `gen_kb_report_indexes.py` 刷新索引页关联）",
        "",
    ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", required=True, help="接口路径，如 /v1/items/page")
    parser.add_argument("--method", default=None, help="不指定则取该 path 下第一个方法")
    parser.add_argument("--force", action="store_true", help="覆盖已存在画像")
    args = parser.parse_args()

    if OUT_DIR is None:
        parser.error("[incomplete] 所选 pack 未配置业务知识库与 PLATFORM_INTERFACE_PROFILE_SUBDIR")

    oas = load_openapi_snapshot(LOCAL_OAS)
    schema_tables = load_schema_tables()

    methods = oas.get("paths", {}).get(args.path)
    if not methods:
        print(f"OAS 中未找到路径：{args.path}", file=sys.stderr)
        sys.exit(1)

    try:
        normalized = {method.lower(): spec for method, path, spec in iter_openapi_operations(oas)
                      if path == args.path}
        method, spec = select_operation(normalized, args.method)
    except ValueError as exc:
        print(f"路径 {args.path} 无有效 operation：{exc}", file=sys.stderr)
        sys.exit(1)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    slug = slugify(method, args.path)
    out = OUT_DIR / f"{slug}.md"
    if out.exists() and not args.force:
        print(f"画像已存在（用 --force 覆盖）：{out}")
        sys.exit(0)
    atomic_write_text(out, render(method.upper(), args.path, spec, schema_tables))

    # 刷新接口画像索引（双链到 index / 用例文档 / 覆盖清单）
    try:
        from gen_kb_report_indexes import write_interface_profile_index
        write_interface_profile_index()
    except Exception as exc:
        print(f"warn: 未刷新接口画像索引：{exc}", file=sys.stderr)

    # 输出推断的表名供用户确认
    inferred = infer_tables_from_path(args.path, schema_tables)
    if inferred:
        print(f"Wrote {out} （自动推断表名：{', '.join(inferred)}）")
    else:
        print(f"Wrote {out} （未推断到表名，需手工补充）")


if __name__ == "__main__":
    main()
