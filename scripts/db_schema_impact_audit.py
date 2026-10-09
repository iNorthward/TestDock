#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据库 Schema 变更影响分析 —— 扫描代码库中对变更表/列的引用，生成 Agent 待办报告。

用法：
  python3 scripts/db_schema_impact_audit.py --from-patrol-cache
  python3 scripts/db_schema_impact_audit.py --dry-run
  python3 scripts/db_schema_impact_audit.py --write-report

输入：sync_db_schema.compare_schemas 的 diff，或 data/db_schema_patrol_cache.json
输出：stdout JSON + pack 配置的 Schema 巡检报告目录（--write-report）
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import hashlib
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from project_inputs import (business_knowledge_subpath, init_project_pack,
                            pack_data_path, project_input_path)  # noqa: E402

PACKS = init_project_pack(ROOT)

from platform_config import project_path  # noqa: E402
from timezone_config import configure_timezone  # noqa: E402

configure_timezone()
REPORT_DIR = business_knowledge_subpath("PLATFORM_SCHEMA_REPORT_SUBDIR")
PATROL_CACHE = pack_data_path("db_schema_patrol_cache.json")
SCHEMA_CACHE = project_path("PLATFORM_SCHEMA_JSON")
APIFOX_INDEX = project_input_path("PLATFORM_APIFOX_INDEX", required=False)
sys.path.insert(0, str(ROOT / "scripts"))
from atomic_file import atomic_write_text  # noqa: E402
from report_hand_section import preserve_hand_section, wrap_hand_section

def _source_label(path):
    path = Path(path).resolve()
    try:
        return str(path.relative_to(ROOT.resolve()))
    except ValueError:
        return str(path)


def _scan_dirs(root, packs):
    """Include active project sources without scanning unselected Packs."""
    return (
        root / "test-platform", root / "scripts",
        *(pack["root"] for pack in packs),
        *(root / "adapters" / pack["id"] for pack in packs),
        *((root / relative).resolve() for pack in packs for relative in pack.get("case_dirs", [])),
        *((root / relative).resolve() for pack in packs for relative in pack.get("adapter_files", [])),
    )


SCAN_DIRS = _scan_dirs(ROOT, PACKS)
SCAN_GLOBS = ("**/*.py", "**/*.html")
SKIP_PARTS = {"__pycache__", ".git", "node_modules", "venv"}


def _schema_cache_label() -> str:
    if SCHEMA_CACHE is None:
        return "PLATFORM_SCHEMA_JSON 未配置"
    try:
        return str(SCHEMA_CACHE.relative_to(ROOT))
    except ValueError:
        return str(SCHEMA_CACHE)

# SQL / Python 中常见的表引用模式
TABLE_PATTERNS = (
    r"\bFROM\s+{t}\b",
    r"\bJOIN\s+{t}\b",
    r"\bINTO\s+{t}\b",
    r"\bUPDATE\s+{t}\b",
    r"\bTABLE\s*=\s*[\"']{t}[\"']",
    r"[\"']{t}[\"']",
)

# 列引用：SQL 列名 + FIELD_MAP 值
COL_PATTERNS = (
    r"\b{c}\b",
    r"[\"']{c}[\"']",
    r":\s*[\"']{c}[\"']",
)


def _load_patrol_cache() -> dict | None:
    if PATROL_CACHE is None or not PATROL_CACHE.exists():
        return None
    try:
        return json.loads(PATROL_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return None


def _iter_py_files() -> list[Path]:
    out: set[Path] = set()
    for base in SCAN_DIRS:
        if not base.exists():
            continue
        if base.is_file():
            out.add(base.resolve())
            continue
        for pattern in SCAN_GLOBS:
            for p in base.glob(pattern):
                if any(part in SKIP_PARTS for part in p.parts):
                    continue
                out.add(p.resolve())
    return sorted(out)


def _scan_table(table: str, files: list[Path]) -> list[dict]:
    hits: list[dict] = []
    patterns = [re.compile(p.format(t=re.escape(table)), re.I) for p in TABLE_PATTERNS]
    for fp in files:
        try:
            text = fp.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        lines = text.splitlines()
        matched_lines: list[int] = []
        for i, line in enumerate(lines, 1):
            if any(pat.search(line) for pat in patterns) or re.search(rf"(?<![\w])['\"]{re.escape(table)}['\"](?![\w])", line, re.I):
                matched_lines.append(i)
        if matched_lines:
            rel = _source_label(fp)
            module = _guess_module(rel)
            hits.append({
                "file": rel,
                "module": module,
                "lines": matched_lines[:8],
                "lineCount": len(matched_lines),
            })
    return hits


def _camel(name: str) -> str:
    bits = name.split("_")
    return bits[0] + "".join(x[:1].upper() + x[1:] for x in bits[1:])


def _token_re(name: str) -> re.Pattern:
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", re.I)


def _table_lines(lines: list[str], table: str) -> set[int]:
    t = re.escape(table)
    pat = re.compile(rf"\b(?:FROM|JOIN|UPDATE|INTO)\s+[`\"']?{t}[`\"']?(?![A-Za-z0-9_])|(?<![A-Za-z0-9_])['\"]{t}['\"](?![A-Za-z0-9_])", re.I)
    return {i for i, line in enumerate(lines) if pat.search(line)}


def _field_map_hits(text: str, variants: set[str]) -> bool:
    # Restrict matching to literal dictionary-like key/value tokens in FIELD_MAP assignments.
    for m in re.finditer(r"(?:\w*_)?FIELD_MAP\s*=\s*\{([^{}]*)\}", text, re.S):
        body = m.group(1)
        for literal in re.findall(r"['\"]([^'\"]+)['\"]", body):
            if literal in variants:
                return True
    return False


def _scan_column(table: str, column: str, table_files: list[dict], files: list[Path]) -> tuple[list[dict], list[dict]]:
    """Find exact SQL-neighborhood / uniquely attributable FIELD_MAP references."""
    hits, ambiguous = [], []
    variants = {column, _camel(column)}
    pats = [_token_re(x) for x in variants]
    duplicate_tables = {table}
    try:
        if SCHEMA_CACHE is None:
            raise FileNotFoundError("PLATFORM_SCHEMA_JSON 未配置")
        schema = json.loads(SCHEMA_CACHE.read_text(encoding="utf-8"))
        duplicate_tables.update(t["name"] for t in schema.get("tables", [])
                                if any(c.get("COLUMN_NAME") == column for c in t.get("columns", [])))
    except Exception:
        pass
    for fp in files:
        try:
            lines = fp.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        table_refs = _table_lines(lines, table)
        candidates = set()
        local_ambiguous = set()
        for table_line in table_refs:
            lo, hi = max(0, table_line - 8), min(len(lines), table_line + 9)
            window_lines = lines[lo:hi]
            referenced = {candidate for candidate in duplicate_tables if _table_lines(window_lines, candidate)}
            if len(referenced) > 1:
                if any(p.search("\n".join(window_lines)) for p in pats):
                    local_ambiguous.update(i + 1 for i in range(lo, hi) if any(p.search(lines[i]) for p in pats))
                continue
            for i in range(lo, hi):
                if any(p.search(lines[i]) for p in pats):
                    candidates.add(i + 1)
        # 没有表名的同名列（例如 panel.html 里的 create_time）不算引用，也不算歧义。
        if local_ambiguous:
            ambiguous.append({"file": _source_label(fp), "module": _guess_module(_source_label(fp)), "lines": sorted(local_ambiguous)[:6]})
        if candidates:
            rel = _source_label(fp)
            hits.append({"file": rel, "module": _guess_module(rel), "lines": sorted(candidates)[:6], "lineCount": len(candidates)})
        elif table_refs and len({candidate for candidate in duplicate_tables if _table_lines(lines, candidate)}) == 1 and _field_map_hits("\n".join(lines), variants):
            rel = _source_label(fp)
            hits.append({"file": rel, "module": _guess_module(rel), "lines": sorted(x + 1 for x in table_refs)[:6], "lineCount": len(table_refs)})
    return hits, ambiguous


def _scan_column_global(column: str, files: list[Path]) -> list[dict]:
    hits: list[dict] = []
    for fp in files:
        try:
            text = fp.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if column not in text:
            continue
        lines = [i + 1 for i, line in enumerate(text.splitlines()) if column in line]
        if lines:
            rel = _source_label(fp)
            hits.append({"file": rel, "module": _guess_module(rel), "lines": lines[:6], "lineCount": len(lines)})
    return hits


def _guess_module(rel_path: str) -> str:
    name = Path(rel_path).name
    if name.endswith("_cases.py"):
        return name.replace("_cases.py", "")
    if name.endswith("_query.py"):
        return name.replace("_query.py", "")
    if name.endswith("_ops.py"):
        return name.replace("_ops.py", "")
    if name.endswith("_db.py"):
        return name.replace("_db.py", "")
    return name


def _action_hint(change_type: str, column: str | None = None) -> str:
    if change_type == "removed_table":
        return "删除或迁移 SQL/对账逻辑；确认用例是否仍有效"
    if change_type == "new_table":
        return "可选：为新表补充 query/ops 工具与 D 对账用例"
    if change_type == "removed_column":
        return f"删除列 `{column}` → 更新 SELECT/FIELD_MAP/R 断言/面板工具 SQL"
    if change_type == "added_column":
        return f"新增列 `{column}` → 若 Apifox VO 已含该字段，补 FIELD_MAP 与 D 对账"
    if change_type == "changed_column":
        return f"列 `{column}` 类型/约束变更 → 核对 reconcile 精度、WHERE 口径、枚举断言"
    return "人工核对 SQL 与断言"


def _column_severity(change: str, refs: list[dict], index_only: bool = False, vo_match=None) -> tuple[str, str]:
    if index_only:
        return "cache_only", "仅索引成员变化，同步缓存即可"
    if change == "removed_column":
        return ("must_fix", "删除列 → 更新 SELECT/FIELD_MAP/R 断言/面板工具 SQL") if refs else ("cache_only", "库结构变化，无真实列引用")
    if change == "added_column":
        if vo_match:
            return "must_fix", "补 FIELD_MAP 与 D 对账"
        if refs:
            return "review", "代码已引用该新列，核对 SQL / FIELD_MAP"
        return "cache_only", "库先于文档，只同步缓存"
    return ("review", "核对真实引用与类型/约束影响") if refs else ("cache_only", "无真实列引用，同步缓存即可")


def _load_vo_matches() -> dict[str, list[dict]] | None:
    if APIFOX_INDEX is None or not APIFOX_INDEX.is_file():
        return None
    try:
        from apifox_contract_audit import covered_ops, endpoint_schema_names, load_ops_from_oas
        data = json.loads(APIFOX_INDEX.read_text(encoding="utf-8"))
        ops = load_ops_from_oas(data)
        comps = (data.get("components") or {}).get("schemas") or {}
        out: dict[str, list[dict]] = {}
        for method, path in covered_ops():
            spec = ops.get((method, path))
            if not spec:
                continue
            for schema in endpoint_schema_names(spec, comps):
                for field in (comps.get(schema, {}).get("properties") or {}):
                    out.setdefault(field, []).append({"schema": schema, "field": field, "endpoints": [f"{method} {path}"]})
        return out
    except Exception:
        return None


def _case_mapping(column: str, matches: list[dict]) -> dict:
    case_ids, modules = set(), set()
    endpoints = sorted({ep for match in matches for ep in match.get("endpoints", [])})
    try:
        from gen_apifox_coverage_wiki import collect, discover_modules
        _, path_index, _ = collect()
        module_map = {label: TP_PATH for _name, label, TP_PATH, _doc in discover_modules()}
        for endpoint in endpoints:
            info = path_index.get(endpoint, {})
            case_ids.update(info.get("cases") or [])
            modules.update(info.get("modules") or [])
        fields = {column, _camel(column)}
        hits = []
        for module in sorted(modules):
            source = module_map.get(module)
            if not source:
                continue
            for fp in sorted(source.parent.glob("*_*.py")):
                if not fp.name.endswith(("_cases.py", "_query.py", "_db.py", "_ops.py")):
                    continue
                try:
                    tree = ast.parse(fp.read_text(encoding="utf-8"))
                except (OSError, SyntaxError):
                    continue
                for node in ast.walk(tree):
                    if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict) and any(isinstance(t, ast.Name) and (t.id == "FIELD_MAP" or t.id.endswith("_FIELD_MAP")) for t in node.targets):
                        for key, value in zip(node.value.keys, node.value.values):
                            if any(isinstance(x, ast.Constant) and x.value in fields for x in (key, value)):
                                hits.append({"file": _source_label(fp), "field": next(x.value for x in (key, value) if isinstance(x, ast.Constant) and x.value in fields)})
        return {"caseIds": sorted(case_ids), "fieldMapHits": hits[:8]}
    except Exception:
        return {"caseIds": [], "fieldMapHits": []}
def audit_impact(diff: dict, files: list[Path] | None = None) -> dict:
    """根据 schema diff 扫描代码影响面。"""
    files = files or _iter_py_files()
    impacts: list[dict] = []
    vo_matches = _load_vo_matches()

    for tname in diff.get("new_tables") or []:
        refs = _scan_table(tname, files)
        severity = "review" if refs else "optional"
        impacts.append({
            "table": tname,
            "change": "new_table",
            "detail": "新增表",
            "action": "核对现有 SQL" if refs else "新增表无表级引用",
            "fileRefs": refs,
            "severity": severity,
            "columnImpacts": [],
        })

    for tname in diff.get("removed_tables") or []:
        refs = _scan_table(tname, files)
        impacts.append({
            "table": tname,
            "change": "removed_table",
            "detail": "删除表",
            "action": _action_hint("removed_table"),
            "fileRefs": refs,
            "severity": "must_fix" if refs else "cache_only",
            "columnImpacts": [],
        })

    for mod in diff.get("modified_tables") or []:
        tname = mod["name"]
        refs = _scan_table(tname, files)
        col_impacts: list[dict] = []
        for c in mod.get("removed_columns") or []:
            col_refs, ambiguous = _scan_column(tname, c, refs, files)
            vm = (vo_matches or {}).get(c) or (vo_matches or {}).get(_camel(c)) or []
            mapping = _case_mapping(c, vm)
            severity, action = _column_severity("removed_column", col_refs)
            if ambiguous and not col_refs:
                severity, action = "review", "同名列，需人工确认属于哪张表"
            col_impacts.append({
                "column": c,
                "change": "removed_column",
                "action": action, "severity": severity, "ambiguousRefs": ambiguous,
                "voMatch": vm, **mapping,
                "fileRefs": col_refs,
            })
        for c in mod.get("added_columns") or []:
            col_refs, ambiguous = _scan_column(tname, c, refs, files)
            vm = None if vo_matches is None else vo_matches.get(c) or vo_matches.get(_camel(c)) or []
            mapping = _case_mapping(c, vm or [])
            severity, action = _column_severity("added_column", col_refs, vo_match=vm)
            if ambiguous and not col_refs:
                severity, action = "review", "同名列，需人工确认属于哪张表"
            col_impacts.append({
                "column": c,
                "change": "added_column",
                "action": action if vo_matches is not None else "未读到 Apifox 索引，未做 VO 对照",
                "severity": severity, "voMatch": vm, "ambiguousRefs": ambiguous, **mapping,
                "fileRefs": col_refs,
            })
        for ch in mod.get("changed_columns") or []:
            c = ch["name"]
            col_refs, ambiguous = _scan_column(tname, c, refs, files)
            severity, action = _column_severity("changed_column", col_refs, ch.get("indexOnly"))
            if ambiguous and not col_refs and not ch.get("indexOnly"):
                severity, action = "review", "同名列，需人工确认属于哪张表"
            col_impacts.append({
                "column": c,
                "change": "changed_column",
                "before": ch.get("before"),
                "after": ch.get("after"),
                "action": action, "severity": severity, "indexOnly": bool(ch.get("indexOnly")),
                "changedKeys": [k for k in (ch.get("before") or {}) if (ch.get("before") or {}).get(k) != (ch.get("after") or {}).get(k)],
                "ambiguousRefs": ambiguous,
                "fileRefs": col_refs,
            })
        detail_parts = []
        if mod.get("added_columns"):
            detail_parts.append("新增列 " + ", ".join(f"`{x}`" for x in mod["added_columns"]))
        if mod.get("removed_columns"):
            detail_parts.append("删除列 " + ", ".join(f"`{x}`" for x in mod["removed_columns"]))
        if mod.get("changed_columns"):
            detail_parts.append("变更列 " + ", ".join(f"`{x['name']}`" for x in mod["changed_columns"]))
        impacts.append({
            "table": tname,
            "change": "modified_table",
            "detail": "；".join(detail_parts) or "结构变更",
            "action": "见各列影响",
            "fileRefs": refs,
            "columnImpacts": col_impacts,
        })

    affected_files: dict[str, dict] = {}
    for imp in impacts:
        if imp.get("change") != "modified_table" and imp.get("severity") in ("must_fix", "review"):
            for ref in imp.get("fileRefs") or []:
                affected_files.setdefault(ref["file"], {"file": ref["file"], "module": ref["module"], "tables": set(), "columns": set()})
                affected_files[ref["file"]]["tables"].add(imp["table"])
        for ci in imp.get("columnImpacts") or []:
            if ci.get("severity") not in ("must_fix", "review"):
                continue
            for ref in ci.get("fileRefs") or []:
                affected_files.setdefault(ref["file"], {"file": ref["file"], "module": ref["module"], "tables": set(), "columns": set()})
                affected_files[ref["file"]]["tables"].add(imp["table"])
                affected_files[ref["file"]]["columns"].add(ci["column"])

    affected_list = []
    for v in sorted(affected_files.values(), key=lambda x: x["file"]):
        affected_list.append({
            "file": v["file"],
            "module": v["module"],
            "tables": sorted(v["tables"]),
            "columns": sorted(v["columns"]),
        })

    severity_counts = {s: 0 for s in ("must_fix", "review", "cache_only", "optional")}
    for imp in impacts:
        if imp.get("change") != "modified_table":
            severity_counts[imp.get("severity", "review")] += 1
        for ci in imp.get("columnImpacts") or []:
            severity_counts[ci.get("severity", "review")] += 1
    return {
        "impactCount": len(impacts),
        "affectedFileCount": len(affected_list),
        "impacts": impacts,
        "affectedFiles": affected_list,
        "severityCounts": severity_counts,
    }


def render_report(patrol: dict, impact: dict, audit_date: str) -> str:
    diff = patrol.get("diff") or {}
    lines = [
        f"# 数据库 Schema 巡检报告 · {audit_date}",
        "",
        f"[[../数据库索引|← 数据库索引]] · 对比基准：`{_schema_cache_label()}`",
        "",
        "## 摘要",
        "",
        "| 项 | 值 |",
        "| --- | --- |",
        f"| 数据库 | {patrol.get('database', '—')} |",
        f"| 线上表数 | {patrol.get('total_tables', 0)} |",
        f"| 缓存表数 | {patrol.get('cached_tables', 0)} |",
        f"| 新增表 | {len(diff.get('new_tables') or [])} |",
        f"| 删除表 | {len(diff.get('removed_tables') or [])} |",
        f"| 变更表 | {len(diff.get('modified_tables') or [])} |",
        f"| 受影响代码文件 | {impact.get('affectedFileCount', 0)} |",
        f"| diffDigest | `{patrol.get('diffDigest', '—')}` |",
        f"| 严重级别 | must_fix {impact.get('severityCounts', {}).get('must_fix', 0)} / review {impact.get('severityCounts', {}).get('review', 0)} / cache_only {impact.get('severityCounts', {}).get('cache_only', 0)} / optional {impact.get('severityCounts', {}).get('optional', 0)} |",
        "",
    ]
    if patrol.get("undigested"):
        lines.insert(4, f"未消化：与上次巡检 digest 相同，本地 {_schema_cache_label()} 尚未 sync。")

    if not patrol.get("has_changes"):
        lines += ["## 结论", "", "与本地缓存一致，无结构变更，无需 Agent 处理。", ""]
        lines += ["## 人工结论", "", wrap_hand_section(""), ""]
        return "\n".join(lines)

    lines += ["## 变更明细", ""]
    for imp in impact.get("impacts") or []:
        tag = {"new_table": "新增表", "removed_table": "删除表", "modified_table": "修改表"}.get(imp["change"], imp["change"])
        lines.append(f"### `{imp['table']}`（{tag}）")
        lines.append("")
        lines.append(f"- {imp['detail']}")
        lines.append(f"- **建议**：{imp['action']}")
        refs = imp.get("fileRefs") or []
        if refs:
            lines.append(f"- **引用文件**（{len(refs)}）：")
            for r in refs[:10]:
                ln = ", ".join(str(x) for x in r.get("lines") or [])[:80]
                lines.append(f"  - `{r['file']}` L{ln}")
        cols = imp.get("columnImpacts") or []
        if cols:
            lines += ["", "| 列 | 变更 | 级别 | 真实引用 | 建议 |", "| --- | --- | --- | --- | --- |"]
            for ci in cols:
                change = ci["change"]
                if ci.get("indexOnly"):
                    change_text = "仅索引成员变化"
                elif ci.get("before") is not None:
                    before, after = ci["before"], ci["after"]
                    change_text = "、".join(k for k in before.keys() | after.keys() if before.get(k) != after.get(k)) or change
                else:
                    change_text = {"added_column": "新增", "removed_column": "删除"}.get(change, change)
                refs = ci.get("fileRefs") or []
                ref_text = ", ".join(f"`{x['file']}`" for x in refs[:4]) if refs else "同名列，未归属" if ci.get("ambiguousRefs") else "无"
                lines.append(f"| `{ci['column']}` | {change_text} | {ci.get('severity', 'review')} | {ref_text} | {ci['action']} |")
            lines.append("")
        lines.append("")

    aff = impact.get("affectedFiles") or []
    if aff:
        lines += ["## 受影响文件汇总（交 Agent 逐项修复）", "", "| 文件 | 模块 | 涉及表 | 涉及列 |", "| --- | --- | --- | --- |"]
        for a in aff:
            cols = ", ".join(f"`{c}`" for c in (a.get("columns") or [])[:5]) or "—"
            tabs = ", ".join(f"`{t}`" for t in (a.get("tables") or [])[:3])
            lines.append(f"| `{a['file']}` | {a.get('module', '—')} | {tabs} | {cols} |")
        lines.append("")

    index_changes = (diff.get("index_changes") or []) + (diff.get("fk_changes") or [])
    lines += ["## 索引与外键", "", "级别：`cache_only`。同步缓存，不要为此改断言。", ""]
    if index_changes:
        for entry in index_changes:
            lines.append(f"- `{entry['table']}`：索引/外键新增 {len(entry.get('added') or [])}，删除 {len(entry.get('removed') or [])}")
    else:
        lines.append("无变化。")
    vo_matches = _load_vo_matches() or {}
    lines += ["", "## 与 Apifox 契约对照", "", "| 库列（表.列） | 契约字段 | 用例 / FIELD_MAP | 结论 |", "| --- | --- | --- | --- |"]
    for imp in impact.get("impacts") or []:
        for ci in imp.get("columnImpacts") or []:
            if ci["change"] not in ("added_column", "removed_column"):
                continue
            matches = ci.get("voMatch") or vo_matches.get(ci["column"]) or vo_matches.get(_camel(ci["column"])) or []
            contract = ", ".join(f"{m['schema']}.{m.get('field', ci['column'])}" for m in matches) if matches else "契约中无此字段"
            mapping = ci.get("caseIds", [])
            field_hits = ci.get("fieldMapHits", [])
            usage = ", ".join(mapping[:8]) or "无用例"
            if field_hits:
                usage += " / " + ", ".join(x["file"] for x in field_hits[:4])
            conclusion = "必须补映射" if ci["change"] == "added_column" and matches else "库先于文档" if ci["change"] == "added_column" else "契约有、库列已删需改断言" if matches else "仅缓存"
            lines.append(f"| `{imp['table']}.{ci['column']}` | {contract} | {usage} | {conclusion} |")
    if not any(ci.get("change") in ("added_column", "removed_column") for imp in impact.get("impacts", []) for ci in imp.get("columnImpacts", [])):
        lines.append("| — | 本轮无新增/删除列 | — | 仅缓存 |")
    lines.append("")
    lines += [
        "## Agent 处理清单",
        "",
        "1. 先处理 `must_fix`，再处理 `review`；`cache_only` 执行 `python3 scripts/sync_db_schema.py` 写入缓存",
        "2. `optional` 新表不要当成故障",
        "5. 收尾：`python3 scripts/validate_catalog.py` → 受影响模块 SAFE 全绿 → `./start.sh restart` → 再点「开始巡检」确认收敛",
        "",
    ]
    lines += ["", "## 人工结论", "", wrap_hand_section(""), ""]
    return "\n".join(lines)


def render_agent_prompt(patrol: dict, impact: dict, report_path: str | None) -> str:
    if not patrol.get("has_changes"):
        return ""

    diff = patrol.get("diff") or {}
    report = report_path or (str(REPORT_DIR / f"{date.today().isoformat()}.md")
                             if REPORT_DIR else "[incomplete] 未配置 PLATFORM_SCHEMA_REPORT_SUBDIR")
    new_n = len(diff.get("new_tables") or [])
    rem_n = len(diff.get("removed_tables") or [])
    mod_n = len(diff.get("modified_tables") or [])
    aff = impact.get("affectedFiles") or []

    lines = [
        "请根据数据库 Schema 巡检报告修复代码影响（更新 SQL / FIELD_MAP / 断言后再跑 SAFE）：",
        "",
        f"报告：{report}",
        f"对比基准：{_schema_cache_label()}",
        "",
        f"· 结构变更 {new_n + rem_n + mod_n} 处（新增表 {new_n} · 删除表 {rem_n} · 修改表 {mod_n}）",
    ]

    removed_cols: list[str] = []
    changed_cols: list[str] = []
    for imp in impact.get("impacts") or []:
        for ci in imp.get("columnImpacts") or []:
            if ci.get("severity") not in ("must_fix", "review"):
                continue
            if ci["change"] == "removed_column":
                removed_cols.append(f"{imp['table']}.{ci['column']}")
            elif ci["change"] == "changed_column" and not ci.get("indexOnly"):
                changed_cols.append(f"{imp['table']}.{ci['column']}")
            elif ci["change"] == "added_column":
                changed_cols.append(f"{imp['table']}.{ci['column']}（新增列，补 FIELD_MAP / D 对账）")

    if removed_cols:
        sample = ", ".join(removed_cols[:8])
        lines.append(f"· 删除列 {len(removed_cols)} 个（{sample}{'…' if len(removed_cols) > 8 else ''}）→ 从 SELECT/FIELD_MAP/断言中移除")
    if changed_cols:
        sample = ", ".join(changed_cols[:8])
        lines.append(f"· 变更列 {len(changed_cols)} 个（{sample}{'…' if len(changed_cols) > 8 else ''}）→ 核对 reconcile 与 WHERE 口径")

    if aff:
        lines.append(f"· 受影响代码文件 {len(aff)} 个 → 逐项修复：")
        for i, a in enumerate(aff[:12], 1):
            cols = ", ".join(a.get("columns") or [])[:60]
            tabs = ", ".join(a.get("tables") or [])
            extra = f" · 列 {cols}" if cols else ""
            lines.append(f"  {i}. `{a['file']}`（{tabs}{extra}）")
        if len(aff) > 12:
            lines.append(f"  … 另有 {len(aff) - 12} 个文件，见报告 §受影响文件汇总")

    counts = impact.get("severityCounts") or {}
    if counts.get("cache_only"):
        lines.append(f"· cache_only {counts['cache_only']} 项：执行 `python3 scripts/sync_db_schema.py` 写入缓存")
    if counts.get("optional"):
        lines.append(f"· optional {counts['optional']} 项新表，不要当成故障")

    lines += [
        "",
        "修复顺序：先处理 must_fix，再处理 review；`*_db.py` / `*_query.py` SQL → `FIELD_MAP` → `*_cases.py` R/D 断言 → 面板 `server.py` 工具",
        f"收尾：python3 scripts/sync_db_schema.py（写入 {_schema_cache_label()}）→ python3 scripts/validate_catalog.py → 受影响模块 SAFE 全绿 → ./start.sh restart",
        "完成后请再点「开始巡检」确认无结构变更或影响面已收敛。",
    ]
    return "\n".join(lines)


def run_audit(patrol: dict | None = None, write_report: bool = False) -> dict:
    patrol = patrol or _load_patrol_cache()
    if not patrol:
        return {"ok": False, "error": "无巡检缓存，请先执行数据库巡检"}

    diff = patrol.get("diff") or {}
    digest_payload = {k: diff.get(k, []) for k in ("new_tables", "removed_tables", "modified_tables", "index_changes", "fk_changes")}
    # Strip volatile estimates and retain only stable structural values.
    for table in digest_payload.get("modified_tables", []):
        table.pop("rows_est", None)
    patrol["diffDigest"] = hashlib.sha256(json.dumps(digest_payload, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:16]
    old = _load_patrol_cache()
    patrol["undigested"] = bool(patrol.get("has_changes") and old and old.get("diffDigest") == patrol["diffDigest"])
    impact = audit_impact(diff)
    report_path = None
    agent_prompt = render_agent_prompt(patrol, impact, None)

    if write_report and patrol.get("has_changes"):
        if REPORT_DIR is None:
            return {"ok": False, "error": "[incomplete] 所选 pack 未配置业务知识库与 PLATFORM_SCHEMA_REPORT_SUBDIR"}
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        today = date.today().isoformat()
        path = REPORT_DIR / f"{today}.md"
        rendered = render_report(patrol, impact, today)
        prior = path.read_text(encoding="utf-8") if path.exists() else None
        atomic_write_text(path, preserve_hand_section(prior, rendered))
        try:
            report_path = str(path.relative_to(ROOT))
        except ValueError:
            report_path = str(path)
        agent_prompt = render_agent_prompt(patrol, impact, report_path)

    return {
        "ok": True,
        "hasChanges": bool(patrol.get("has_changes")),
        "impact": impact,
        "diffDigest": patrol["diffDigest"],
        "undigested": patrol["undigested"],
        "agentPrompt": agent_prompt,
        "reportPath": report_path,
    }


def main():
    parser = argparse.ArgumentParser(description="数据库 Schema 变更影响分析")
    parser.add_argument("--from-patrol-cache", action="store_true", help="读取所选 pack 数据目录中的 db_schema_patrol_cache.json")
    parser.add_argument("--dry-run", action="store_true", help="即时对比线上与缓存并分析（需 DB 连接）")
    parser.add_argument("--write-report", action="store_true", help="写入 Schema巡检报告")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    patrol = None
    if args.dry_run:
        sys.path.insert(0, str(ROOT / "scripts"))
        import sync_db_schema as sds  # noqa: E402
        result = sds.sync_schema(dry_run=True, json_output=False)
        patrol = {
            "ok": True,
            "database": result.get("database"),
            "total_tables": result.get("total_tables", 0),
            "cached_tables": len((sds.load_cached_schema() or {}).get("tables") or []),
            "cache_exists": result.get("cache_exists"),
            "cache_file": result.get("cache_file"),
            "has_changes": sds._schema_diff_has_changes(result.get("diff") or {}),
            "diff": result.get("diff") or {},
        }
    elif args.from_patrol_cache or True:
        patrol = _load_patrol_cache()

    out = run_audit(patrol, write_report=args.write_report)

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    elif not out.get("ok"):
        print(out.get("error", "audit failed"), file=sys.stderr)
        sys.exit(1)
    else:
        imp = out.get("impact") or {}
        print(f"Affected files: {imp.get('affectedFileCount', 0)}")
        if out.get("reportPath"):
            print(f"Wrote {out['reportPath']}")
        if out.get("agentPrompt"):
            print("\n--- Agent Prompt ---\n")
            print(out["agentPrompt"])

    sys.exit(0 if out.get("ok") else 1)


if __name__ == "__main__":
    main()
