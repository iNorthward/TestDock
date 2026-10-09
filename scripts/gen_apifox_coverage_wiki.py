#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 test-platform CATALOG 生成 Apifox 接口 ↔ 测试用例 LLM Wiki 文档。

接口归属完全由模块自描述的 case.api / RUNNER_API 解析（《用例元信息规范》§4），
本脚本不再维护任何路径映射硬编码。

输出：所选 pack 配置的覆盖文档目录与 PLATFORM_PACK_DATA_DIR/coverage.json
维护：新增/改动 *_cases.py 后重新运行本脚本。
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

from atomic_file import atomic_write_text
from candidate_inputs import load_openapi_snapshot

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from timezone_config import configure_timezone  # noqa: E402
from project_inputs import (business_knowledge_path, business_knowledge_subpath,
                            pack_data_path, project_input_path)  # noqa: E402

configure_timezone()
TP = ROOT / "test-platform"
OUT = business_knowledge_subpath("PLATFORM_COVERAGE_DOC_SUBDIR")
KB_ROOT = business_knowledge_path()
CASE_DOC_DIR = business_knowledge_subpath("PLATFORM_CASE_DOC_SUBDIR")
APIFOX_INDEX = project_input_path("PLATFORM_APIFOX_INDEX", required=False)
COVERAGE_JSON = pack_data_path("coverage.json")

_apifox_ops: dict[tuple[str, str], str] | None = None


def apifox_module_from_path(path: str, summary: str = "") -> str:
    """Apifox 目录名（与 Apifox UI 文件夹一致，以 summary 前缀为准）。

    规则：summary 为 ``{模块}-{接口名}`` 时 → ``{urlSeg0} - {模块}``；
    否则回退 URL 路径段（``public`` → ``公开接口``）。
    """
    parts = [p for p in path.strip("/").split("/") if p and not p.startswith("{")]
    prefix = parts[0] if parts else "其它"
    s = (summary or "").strip()
    if s and "-" in s:
        cat = s.split("-", 1)[0].strip()
        if cat:
            return f"{prefix} - {cat}"
    if len(parts) >= 2 and parts[1] == "public":
        return f"{prefix} - 公开接口"
    if len(parts) >= 2:
        return f"{prefix} - {parts[1]}"
    return prefix


def apifox_summary_label(path: str, summary: str) -> str:
    """Apifox 摘要列：模块名在前，如 ``cli - 交易账户 · 交易账户-资产详情``。"""
    module = apifox_module_from_path(path, summary)
    text = (summary or "").strip()
    if text:
        return f"{module} · {text}"
    return module


def load_apifox_summaries() -> dict[tuple[str, str], str]:
    """从 Apifox OAS 索引按 (method, path) 加载接口摘要；不用 api-id 分享链接。"""
    global _apifox_ops
    if _apifox_ops is not None:
        return _apifox_ops
    if APIFOX_INDEX is None:
        raise ValueError("[incomplete] 未配置 PLATFORM_APIFOX_INDEX，跳过 Apifox 覆盖索引")
    data = load_openapi_snapshot(APIFOX_INDEX)
    valid = {"get", "post", "put", "delete", "patch", "head", "options"}
    apifox_ops = {}
    for path, methods in data.get("paths", {}).items():
        if not isinstance(methods, dict):
            raise ValueError(f"OpenAPI paths[{path!r}] 必须是对象: {APIFOX_INDEX}")
        for method, spec in methods.items():
            if method.lower() not in valid:  # 跳过 $ref / x-* 等非 HTTP 方法键
                continue
            summary = ""
            if isinstance(spec, dict):
                inner = spec.get(method) if isinstance(spec.get(method), dict) else None
                summary = (inner or spec).get("summary") or ""
            apifox_ops[(method.upper(), path)] = summary
    # Publish the cache only after the entire OAS has been loaded and validated.
    # A missing/bad index must not poison later calls in the same process.
    _apifox_ops = apifox_ops
    return apifox_ops


sys.path.insert(0, str(TP / "common"))
sys.path.insert(0, str(TP))
sys.path.insert(0, str(ROOT / "scripts"))

from validate_catalog import case_apis  # noqa: E402  统一的 case.api / RUNNER_API 解析

# Display labels and business-document locations belong to the selected pack.
from pack_registry import load_packs  # noqa: E402

MODULE_META: dict[str, tuple[str, str]] = {}
for _pack in load_packs():
    for _name, _meta in (_pack.get("module_metadata") or {}).items():
        if isinstance(_meta, dict):
            MODULE_META[str(_name)] = (
                str(_meta.get("label") or _name), str(_meta.get("doc") or "")
            )


def case_doc_wiki_path(doc_stem: str) -> str:
    """Build a knowledge-root link using the selected pack's case-document path."""
    if KB_ROOT is None or CASE_DOC_DIR is None:
        return ""
    relative = (CASE_DOC_DIR / f"{Path(doc_stem).name}.md").relative_to(KB_ROOT)
    return relative.with_suffix("").as_posix()


def _wiki_link(source_dir: Path, target: Path, alias: str | None = None) -> str:
    target_no_suffix = target.with_suffix("") if target.suffix == ".md" else target
    rel = os.path.relpath(target_no_suffix, source_dir).replace(os.sep, "/")
    return f"[[{rel}|{alias}]]" if alias else f"[[{rel}]]"


def _knowledge_index_link(source_dir: Path, alias: str) -> str:
    return _wiki_link(source_dir, KB_ROOT / "index.md", alias) if KB_ROOT else ""


def _coverage_wiki_path(slug: str) -> str:
    if KB_ROOT is None or OUT is None:
        return slug
    return (OUT / slug).relative_to(KB_ROOT).as_posix()


def discover_modules() -> list[tuple[str, str, str, str]]:
    """(mod_name, label, code_path, doc_wiki)；模块列表来自 catalog 自动发现。"""
    import catalog as cat
    out = []
    for name in cat.discover_case_module_names():
        leaf = name.rsplit(".", 1)[-1]
        label, doc_wiki = MODULE_META.get(name, MODULE_META.get(leaf, (leaf, "")))
        mod = importlib.import_module(name)
        code_path = Path(mod.__file__).resolve().relative_to(ROOT).as_posix()
        out.append((name, label, code_path, doc_wiki))
    return out


def parse_http(s: str) -> tuple[str, str] | None:
    s = s.strip()
    m = re.match(r"^(GET|POST|PUT|DELETE|PATCH)\s+(/[^\s?]+)", s)
    if m:
        return m.group(1), m.group(2)
    if s.startswith("/"):
        return "?", s.split("?")[0]
    return None


def apifox_cell(method: str, path: str) -> str:
    """按 method + 全路径在 Apifox OAS 索引中匹配，返回「模块 · 摘要」或状态。"""
    if "*" in path:
        return "—（模式）"
    summary = load_apifox_summaries().get((method.upper(), path))
    if summary:
        return apifox_summary_label(path, summary)
    return "未收录"


def collect():
    rows = []
    path_index: dict[str, dict] = defaultdict(lambda: {"cases": [], "modules": set()})
    unresolved: list[str] = []

    for mod_name, label, code_path, doc_wiki in discover_modules():
        mod = importlib.import_module(mod_name)
        suites = {s["id"]: s for s in getattr(mod, "SUITES", [])}
        catalog = getattr(mod, "CATALOG", [])
        runner_api = getattr(mod, "RUNNER_API", {}) or {}

        api_map: dict[str, list[dict]] = defaultdict(list)
        for c in catalog:
            apis = case_apis(c, runner_api)
            if apis is None:
                unresolved.append(f"{mod_name}:{c.get('id')}")
                continue
            for hp in apis:
                parsed = parse_http(hp)
                if not parsed:
                    continue
                method, path = parsed
                key = f"{method} {path}"
                api_map[key].append(c)
                path_index[key]["cases"].append(c["id"])
                path_index[key]["modules"].add(label)

        suite_summaries = []
        for sid, suite in suites.items():
            cases = [c for c in catalog if c.get("suite") == sid]
            suite_summaries.append(
                {
                    "id": sid,
                    "name": suite.get("name", sid),
                    "endpoint": suite.get("endpoint", ""),
                    "case_count": len(cases),
                    "case_ids": [c["id"] for c in cases],
                }
            )

        rows.append(
            {
                "mod_name": mod_name,
                "label": label,
                "code_path": code_path,
                "doc_wiki": doc_wiki,
                "total_cases": len(catalog),
                "suites": suite_summaries,
                "api_map": dict(sorted(api_map.items())),
            }
        )
    return rows, path_index, unresolved


def fmt_cases(ids: list[str], limit: int = 12) -> str:
    if len(ids) <= limit:
        return ", ".join(f"`{x}`" for x in ids)
    head = ", ".join(f"`{x}`" for x in ids[:limit])
    return f"{head} …（共 {len(ids)} 条）"


def write_index(rows, path_index, today: str):
    total_cases = sum(r["total_cases"] for r in rows)
    covered = len(path_index)
    lines = [
        "# Apifox 接口测试覆盖清单",
        "",
        f"← **唯一主入口**：{_knowledge_index_link(OUT, '知识库 index')}",
        "",
        "记录 **Apifox 接口 ↔ 自动化测试用例 id** 的机器可读映射；用例说明文档统一从主 index 进入。",
        "",
        f"- 生成日期：{today}",
        f"- 生成命令：`python3 scripts/gen_apifox_coverage_wiki.py`",
        f"- OpenAPI 源：`PLATFORM_OAS_BASELINE` 配置的本地快照",
        f"- Apifox OAS：`PLATFORM_APIFOX_INDEX` 配置的在线索引（按 **method + 全路径** 匹配）",
        f"- 自动化用例：**{total_cases}** 条 · 已映射 HTTP 路径：**{covered}** 个",
        "",
        "> 「Apifox 摘要」列来自 OAS 索引中同路径接口的 summary；「未收录」表示 Apifox 在线文档无该 path。",
        "> **不用** api-id 分享链接匹配——以 `GET /agent/home/overview` 这类全路径为准。",
        "",
        "## 模块 → 详细清单",
        "",
        "| 模块 | 用例数 | 代码 | 详细清单 |",
        "| --- | ---: | --- | --- |",
    ]
    slug_map = {}
    for r in rows:
        slug = r["mod_name"].rsplit(".", 1)[-1].removesuffix("_cases")
        slug_map[r["label"]] = slug
        lines.append(
            f"| {r['label']} | {r['total_cases']} | `{r['code_path']}` | "
            f"[[{_coverage_wiki_path(slug)}]] |"
        )

    lines += [
        "",
        "## 已覆盖接口速查（按路径）",
        "",
        "| 方法 | 路径 | Apifox 摘要 | 用例数 | 模块 |",
        "| --- | --- | --- | ---: | --- |",
    ]
    for key in sorted(path_index.keys()):
        method, path = key.split(" ", 1)
        if "*" in path:
            continue
        info = path_index[key]
        mods = " · ".join(sorted(info["modules"]))
        lines.append(
            f"| {method} | `{path}` | {apifox_cell(method, path)} | "
            f"{len(info['cases'])} | {mods} |"
        )

    lines += [
        "",
        "## 维护约定",
        "",
        "1. 新增/改动 `*_cases.py` 后运行生成脚本，刷新本目录。",
        "2. Apifox 有更新时运行 `python3 scripts/fetch_apifox_oas.py` 刷新 `PLATFORM_APIFOX_INDEX` 指向的文件；脚本会先移除认证请求头示例中的访问令牌和 Basic 凭据。",
        "3. 匹配键一律为 **`METHOD` + 全路径**（如 `GET /mgr/user/client/list`），不用 api-id 链接。",
        f"4. 人工用例说明从 {_knowledge_index_link(OUT, '知识库 index')} → 业务文档目录进入；本页只负责 **接口 ↔ 用例 id** 映射。",
        "",
    ]
    out_path = OUT / "覆盖清单.md"
    atomic_write_text(out_path, "\n".join(lines))
    legacy = OUT / "索引.md"
    if legacy.exists():
        legacy.unlink()
    return slug_map


def write_module_page(r, today: str):
    slug = r["mod_name"].rsplit(".", 1)[-1].removesuffix("_cases")
    lines = [
        f"# Apifox 覆盖 · {r['label']}",
        "",
        f"- 模块代码：`{r['code_path']}`",
        (f"- 用例文档：[[{case_doc_wiki_path(r['doc_wiki'])}]]"
         if r["doc_wiki"] and case_doc_wiki_path(r["doc_wiki"])
         else "- 用例文档：暂无独立文档；用例明细见本页及 CATALOG。"),
        f"- 用例总数：**{r['total_cases']}**",
        f"- 生成日期：{today}",
        "",
        f"{_knowledge_index_link(OUT, '← 知识库 index')} · [[覆盖清单|覆盖清单]]",
        "",
        "## Suite 分组",
        "",
        "| Suite ID | 名称 | Endpoint 模式 | 用例数 | 用例 ID |",
        "| --- | --- | --- | ---: | --- |",
    ]
    for s in r["suites"]:
        lines.append(
            f"| `{s['id']}` | {s['name']} | `{s['endpoint']}` | "
            f"{s['case_count']} | {fmt_cases(s['case_ids'], 8)} |"
        )

    if r["api_map"]:
        lines += [
            "",
            "## 接口 ↔ 测试用例（精确映射）",
            "",
            "映射来自模块自描述的 `case.api` / `RUNNER_API`（《用例元信息规范》§4）。",
            "",
            "| 方法 | 路径 | Apifox 摘要 | 用例 ID | 说明 |",
            "| --- | --- | --- | --- | --- |",
        ]
        for key, cases in r["api_map"].items():
            method, path = key.split(" ", 1)
            descs = "; ".join(c.get("desc", "") for c in cases[:3])
            if len(cases) > 3:
                descs += f" …（{len(cases)} 条）"
            lines.append(
                f"| {method} | `{path}` | {apifox_cell(method, path)} | "
                f"{fmt_cases([c['id'] for c in cases], 10)} | {descs} |"
            )

    lines.append("")
    atomic_write_text(OUT / f"{slug}.md", "\n".join(lines))


def write_coverage_json(rows, path_index, today: str):
    """机器可读覆盖数据：面板覆盖页 / 巡检脚本共用。"""
    apifox = load_apifox_summaries()
    covered_keys = {k for k in path_index if "*" not in k.split(" ", 1)[1]}
    paths = []
    for (method, path), summary in sorted(apifox.items(), key=lambda x: (x[0][1], x[0][0])):
        key = f"{method} {path}"
        info = path_index.get(key)
        paths.append({
            "method": method,
            "path": path,
            "summary": summary,
            "apifoxModule": apifox_module_from_path(path, summary),
            "summaryLabel": apifox_summary_label(path, summary),
            "covered": bool(info),
            "caseCount": len(info["cases"]) if info else 0,
            "cases": sorted(set(info["cases"])) if info else [],
            "modules": sorted(info["modules"]) if info else [],
            "domain": path.strip("/").split("/")[0] if path.strip("/") else "",
        })
    # 覆盖清单中存在但 Apifox 未收录的路径也记录
    for key in sorted(covered_keys):
        method, path = key.split(" ", 1)
        if (method, path) not in apifox:
            info = path_index[key]
            paths.append({
                "method": method, "path": path, "summary": "",
                "apifoxModule": apifox_module_from_path(path, ""),
                "summaryLabel": apifox_module_from_path(path),
                "covered": True, "caseCount": len(info["cases"]),
                "cases": sorted(set(info["cases"])),
                "modules": sorted(info["modules"]),
                "domain": path.strip("/").split("/")[0] if path.strip("/") else "",
                "notInApifox": True,
            })
    data = {
        "generatedAt": today,
        "totalCases": sum(r["total_cases"] for r in rows),
        "coveredPaths": len(covered_keys),
        "apifoxPaths": len(apifox and {p for (_, p) in apifox}),
        "modules": [
            {"name": r["label"], "code": r["code_path"], "cases": r["total_cases"]}
            for r in rows
        ],
        "paths": paths,
    }
    if COVERAGE_JSON is None:
        raise ValueError("[incomplete] 未配置 PLATFORM_PACK_DATA_DIR，跳过 coverage.json 生成")
    COVERAGE_JSON.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(COVERAGE_JSON, json.dumps(data, ensure_ascii=False, indent=1))
    return data


def main():
    parser = argparse.ArgumentParser(description="生成 Apifox 接口与自动化用例覆盖文档")
    parser.parse_args()
    try:
        load_apifox_summaries()
    except ValueError as exc:
        parser.error(f"Apifox OAS 索引不可用，拒绝生成覆盖产物：{exc}")
    if COVERAGE_JSON is None:
        parser.error("[incomplete] 所选 pack 未配置 PLATFORM_PACK_DATA_DIR，跳过覆盖索引生成")
    if OUT is None:
        parser.error("[incomplete] 所选 pack 未配置业务知识库与 PLATFORM_COVERAGE_DOC_SUBDIR，跳过覆盖文档生成")
    OUT.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    rows, path_index, unresolved = collect()
    if unresolved:
        print(f"⚠ {len(unresolved)} 条用例无法解析 api（先跑 validate_catalog.py 修复）：")
        for u in unresolved[:20]:
            print(f"    - {u}")
        sys.exit(1)
    write_index(rows, path_index, today)
    for r in rows:
        write_module_page(r, today)
    cov = write_coverage_json(rows, path_index, today)
    print(f"Wrote {len(rows) + 1} files to {OUT}")
    print(f"Wrote {COVERAGE_JSON} "
          f"(covered {cov['coveredPaths']} paths, apifox {cov['apifoxPaths']} paths)")


if __name__ == "__main__":
    main()
