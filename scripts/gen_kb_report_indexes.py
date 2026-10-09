#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Refresh indexes for the selected pack's optional knowledge documents."""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from project_inputs import (business_knowledge_path, business_knowledge_subpath,
                            pack_data_path, project_input_setting)  # noqa: E402

KB = business_knowledge_path()
APIFOX_DIR = business_knowledge_subpath("PLATFORM_APIFOX_REPORT_SUBDIR")
SCHEMA_DIR = business_knowledge_subpath("PLATFORM_SCHEMA_REPORT_SUBDIR")
_AUDIT_REL = project_input_setting("PLATFORM_AUDIT_DOC_SUBDIR", default="")
AUDIT_DIR = business_knowledge_path(str(_AUDIT_REL)) if _AUDIT_REL else None
PROFILE_DIR = business_knowledge_subpath("PLATFORM_INTERFACE_PROFILE_SUBDIR")
COVERAGE_DIR = business_knowledge_subpath("PLATFORM_COVERAGE_DOC_SUBDIR")
CASE_DOC_DIR = business_knowledge_subpath("PLATFORM_CASE_DOC_SUBDIR")
COVERAGE_JSON = pack_data_path("coverage.json")
_PLAN_REL = project_input_setting("PLATFORM_APIFOX_PLAN_DOC", default="")
PLAN_DOC = business_knowledge_path(str(_PLAN_REL)) if _PLAN_REL else None

from gen_apifox_coverage_wiki import MODULE_META  # noqa: E402

MODULE_LINKS: dict[str, tuple[str, str]] = {
    label: (module.removesuffix("_cases"), doc)
    for module, (label, doc) in MODULE_META.items()
}


def _wiki_link(source_dir: Path, target: Path, alias: str | None = None) -> str:
    target_no_suffix = target.with_suffix("") if target.suffix == ".md" else target
    rel = os.path.relpath(target_no_suffix, source_dir).replace(os.sep, "/")
    return f"[[{rel}|{alias}]]" if alias else f"[[{rel}]]"


def _coverage_lookup() -> dict[tuple[str, str], list[str]]:
    if COVERAGE_JSON is None or not COVERAGE_JSON.exists():
        return {}
    data = json.loads(COVERAGE_JSON.read_text(encoding="utf-8"))
    out: dict[tuple[str, str], list[str]] = {}
    for p in data.get("paths", []):
        key = (p.get("method", "").upper(), p.get("path", ""))
        mods = p.get("modules") or []
        if key[0] and key[1] and mods:
            out[key] = mods
    return out


def _coverage_wiki_link(module: str, source_dir: Path) -> str:
    if COVERAGE_DIR is None:
        return "—"
    slug = MODULE_LINKS.get(module, (module.removesuffix("_cases"), ""))[0]
    return _wiki_link(source_dir, COVERAGE_DIR / slug)


def _doc_wiki_link(module: str, source_dir: Path) -> str:
    doc = MODULE_LINKS.get(module, ("", ""))[1]
    if not doc or CASE_DOC_DIR is None:
        return "—"
    return _wiki_link(source_dir, CASE_DOC_DIR / f"{Path(doc).name}.md")


def _date_sort_key(name: str) -> tuple:
    """Sort YYYY-MM-DD or YYYY-MM-DD~... report names descending."""
    base = name.split("__")[0].split("~")[0]
    try:
        return (datetime.strptime(base, "%Y-%m-%d"), name)
    except ValueError:
        return (datetime.min, name)


def _list_report_stems(directory: Path, *, exclude: set[str]) -> list[str]:
    stems = [p.stem for p in directory.glob("*.md") if p.stem not in exclude]
    stems.sort(key=_date_sort_key, reverse=True)
    return stems


def _table_rows(stems: list[str], directory: Path) -> str:
    if not stems:
        return "| — | （暂无） |\n"
    rows = []
    for stem in stems:
        label = stem.replace("__", " ").replace("~", " ~ ")
        rows.append(f"| {label} | {_wiki_link(directory, directory / stem)} |")
    return "\n".join(rows) + "\n"


def _knowledge_header(source_dir: Path) -> str:
    if KB is None:
        return ""
    return f"← **知识库入口**：{_wiki_link(source_dir, KB / 'index.md', '知识库 index')}\n\n"


def write_apifox_index() -> None:
    if APIFOX_DIR is None:
        print("  skip Apifox report index: [incomplete] PLATFORM_APIFOX_REPORT_SUBDIR 未配置")
        return
    stems = _list_report_stems(APIFOX_DIR, exclude={"索引", "报告清单"})
    plan = f"- 巡检计划：{_wiki_link(APIFOX_DIR, PLAN_DOC)}\n" if PLAN_DOC else ""
    coverage = (f"- 覆盖清单：{_wiki_link(APIFOX_DIR, (COVERAGE_DIR or APIFOX_DIR) / '覆盖清单.md')}\n"
                if COVERAGE_DIR else "")
    body = f"""# Apifox 契约巡检报告清单

{_knowledge_header(APIFOX_DIR)}按日保存契约巡检报告。此类产物不进入知识图谱。

{plan}{coverage}- 生成命令：`python3 scripts/run_apifox_inspection.py`

## 报告列表（新 → 旧）

| 日期 | 报告 |
| --- | --- |
{_table_rows(stems, APIFOX_DIR)}
> 本页由 `python3 scripts/gen_kb_report_indexes.py` 自动生成。
"""
    APIFOX_DIR.mkdir(parents=True, exist_ok=True)
    out = APIFOX_DIR / "报告清单.md"
    out.write_text(body, encoding="utf-8")
    (APIFOX_DIR / "索引.md").unlink(missing_ok=True)
    print(f"  ✓ {out} ({len(stems)} reports)")


def write_schema_index() -> None:
    if SCHEMA_DIR is None:
        print("  skip Schema report index: [incomplete] PLATFORM_SCHEMA_REPORT_SUBDIR 未配置")
        return
    stems = _list_report_stems(SCHEMA_DIR, exclude={"索引", "报告清单"})
    body = f"""# Schema 变更影响报告清单

{_knowledge_header(SCHEMA_DIR)}对比配置指向的 Schema 与本地缓存，分析对 SQL / FIELD_MAP / 用例的影响。

- 生成命令：`python3 scripts/db_schema_impact_audit.py --from-patrol-cache --write-report`
- Schema 来源：`PLATFORM_SCHEMA_JSON`

## 报告列表（新 → 旧）

| 日期 | 报告 |
| --- | --- |
{_table_rows(stems, SCHEMA_DIR)}
> 本页由 `python3 scripts/gen_kb_report_indexes.py` 自动生成。
"""
    SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
    out = SCHEMA_DIR / "报告清单.md"
    out.write_text(body, encoding="utf-8")
    (SCHEMA_DIR / "索引.md").unlink(missing_ok=True)
    print(f"  ✓ {out} ({len(stems)} reports)")


def write_audit_index() -> None:
    if AUDIT_DIR is None:
        print("  skip optional audit docs: [incomplete] PLATFORM_AUDIT_DOC_SUBDIR 未配置")
        return
    body = f"""# 可选异常日志审计

{_knowledge_header(AUDIT_DIR)}审计报告可能包含账号标识、堆栈与环境信息；由本机面板读取，不写入知识库索引或 Git。

- 默认产物目录由 `PLATFORM_ERROR_LOG_REPORT_DIR` 指定
- SSH 主机、用户、密钥、日志根目录由所选运维 pack 配置
- 只读 SSH；不执行重启、上传、写文件或 Docker 操作

本页不索引环境报告；请在面板可选运维页查看。
"""
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    out = AUDIT_DIR / "README.md"
    out.write_text(body, encoding="utf-8")
    print(f"  ✓ {out} (local-only report policy)")


def write_interface_profile_index() -> None:
    if PROFILE_DIR is None:
        print("  skip interface profile index: [incomplete] PLATFORM_INTERFACE_PROFILE_SUBDIR 未配置")
        return
    coverage = _coverage_lookup()
    rows: list[tuple[str, str, str, str, str]] = []
    for path in sorted(PROFILE_DIR.glob("*.md"), key=lambda p: p.stem):
        if path.stem in ("索引", "画像清单"):
            continue
        text = path.read_text(encoding="utf-8")
        m = re.match(r"^#\s+接口画像\s+·\s+(\w+)\s+(/[^\n]+)", text)
        if not m:
            continue
        method, api_path = m.group(1).upper(), m.group(2).strip()
        mods = coverage.get((method, api_path), [])
        mod = mods[0] if mods else ""
        rows.append((method, f"`{api_path}`", _wiki_link(PROFILE_DIR, path),
                     _doc_wiki_link(mod, PROFILE_DIR),
                     _coverage_wiki_link(mod, PROFILE_DIR) if mod else "—"))

    table = "\n".join(
        f"| {method} | {api} | {profile} | {doc} | {cov} |"
        for method, api, profile, doc, cov in rows
    ) or "| — | — | — | — | — |"
    coverage_link = (_wiki_link(PROFILE_DIR, (COVERAGE_DIR or PROFILE_DIR) / "覆盖清单.md")
                     if COVERAGE_DIR else "—")
    body = f"""# 接口画像清单

{_knowledge_header(PROFILE_DIR)}单接口结构化沉淀（参数 / 数据口径 / 权限 / 用例规划），作为编写候选用例前的事实源。

- 生成骨架：`python3 scripts/gen_interface_profile.py --path <PATH> [--method GET]`
- 覆盖索引：{coverage_link}

## 画像列表

| 方法 | 路径 | 画像 | 用例文档 | 模块覆盖 |
| --- | --- | --- | --- | --- |
{table}
> 本页由 `python3 scripts/gen_kb_report_indexes.py` 自动生成。
"""
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    out = PROFILE_DIR / "画像清单.md"
    out.write_text(body, encoding="utf-8")
    (PROFILE_DIR / "索引.md").unlink(missing_ok=True)
    print(f"  ✓ {out} ({len(rows)} profiles)")


def main() -> int:
    print("gen_kb_report_indexes")
    if KB is None:
        print("[incomplete] 所选 pack 未配置 PLATFORM_BIZ_KNOWLEDGE_ROOT；跳过知识库索引")
        return 2
    write_apifox_index()
    write_schema_index()
    write_audit_index()
    write_interface_profile_index()
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
