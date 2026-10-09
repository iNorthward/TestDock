#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Apifox 接口巡检 —— 完整执行 pack 提供的巡检计划中的四步。

  A  拉 Apifox 最新 OAS → PLATFORM_APIFOX_INDEX + 可选 PLATFORM_OAS_BASELINE
  B  静态契约对比（本地快照 vs Apifox vs 已接入用例）
  C  全量 SAFE 动态验证 → run_history
  D  覆盖索引 / 文档校验 + 写入完整巡检报告

用法：
  python3 scripts/run_apifox_inspection.py
  python3 scripts/run_apifox_inspection.py --json
  python3 scripts/run_apifox_inspection.py --skip-fetch --skip-safe
"""
from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
import time
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from timezone_config import configure_timezone, now_project_iso  # noqa: E402
from project_inputs import (business_knowledge_path, business_knowledge_subpath,
                            init_project_pack, pack_data_path,
                            project_input_path, project_input_setting)  # noqa: E402

init_project_pack(ROOT)
configure_timezone()
TP = ROOT / "test-platform"
REPORT_DIR = business_knowledge_subpath("PLATFORM_APIFOX_REPORT_SUBDIR")
COVERAGE_JSON = pack_data_path("coverage.json")
UNDIGESTED = pack_data_path("apifox_undigested.json")
PATROL_CACHE = pack_data_path("db_schema_patrol_cache.json")
CASE_DOC_DIR = business_knowledge_subpath("PLATFORM_CASE_DOC_SUBDIR")
_PLAN_DOC_REL = project_input_setting("PLATFORM_APIFOX_PLAN_DOC", default="")
APIFOX_PLAN_DOC = business_knowledge_path(str(_PLAN_DOC_REL)) if _PLAN_DOC_REL else None
APIFOX_INDEX = project_input_path("PLATFORM_APIFOX_INDEX", required=False)
LOCAL_OAS = project_input_path("PLATFORM_OAS_BASELINE", required=False)
FETCH_SCRIPT = ROOT / "scripts" / "fetch_apifox_oas.py"

sys.path.insert(0, str(TP / "common"))
sys.path.insert(0, str(TP))
sys.path.insert(0, str(ROOT / "scripts"))

from atomic_file import atomic_write_text  # noqa: E402
from apifox_contract_audit import audit, removed_from_apifox  # noqa: E402
from candidate_inputs import load_openapi_snapshot  # noqa: E402
from report_hand_section import preserve_hand_section, wrap_hand_section  # noqa: E402

def _source_label(path):
    path = Path(path).resolve()
    try:
        return str(path.relative_to(ROOT.resolve()))
    except ValueError:
        return str(path)


def should_advance_baseline(audit_result: dict, skip_fetch: bool, force: bool = False) -> bool:
    if skip_fetch:
        return False
    actionable = any(audit_result.get(k) for k in ("contract_diffs", "schema_diffs", "removed_from_apifox", "missing_in_apifox", "missing_in_local"))
    return force or not actionable


def _field_map_hits(modules: list[str], fields: list[str]) -> list[dict]:
    variants = set(fields)
    for field in fields:
        parts = field.split("_")
        variants.add(parts[0] + "".join(x[:1].upper() + x[1:] for x in parts[1:]))
        variants.add(__import__("re").sub(r"(?<!^)([A-Z])", r"_\1", field).lower())
    hits = []
    try:
        from gen_apifox_coverage_wiki import discover_modules
        module_paths = {label: TP / code_path for _name, label, code_path, _doc in discover_modules()}
    except Exception:
        module_paths = {}
    for module in modules:
        source = module_paths.get(module)
        base = source.parent if source else TP / module
        # 用例文件的同目录 query/db/ops 里也可能有 FIELD_MAP。
        candidates = sorted(base.glob("*_*.py")) if base.is_dir() else []
        for fp in candidates:
            if not fp.name.endswith(("_cases.py", "_query.py", "_db.py", "_ops.py")):
                continue
            try:
                tree = ast.parse(fp.read_text(encoding="utf-8"))
            except (OSError, SyntaxError):
                continue
            for node in ast.walk(tree):
                value = None
                names = []
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
                    if any(isinstance(t, ast.Name) and (t.id == "FIELD_MAP" or t.id.endswith("_FIELD_MAP")) for t in node.targets):
                        value = node.value
                elif isinstance(node, ast.AnnAssign) and isinstance(node.value, ast.Dict):
                    t = node.target
                    if isinstance(t, ast.Name) and (t.id == "FIELD_MAP" or t.id.endswith("_FIELD_MAP")):
                        value = node.value
                if value:
                    for key, val in zip(value.keys, value.values):
                        for item in (key, val):
                            if isinstance(item, ast.Constant) and isinstance(item.value, str) and item.value in variants:
                                hits.append({"file": _source_label(fp), "field": item.value})
            if len(hits) >= 8:
                return hits[:8]
    return hits[:8]


def _run(cmd: list[str], timeout: int | None = None, *, full_stdout: bool = False) -> dict:
    p = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout)
    return {
        "cmd": " ".join(cmd),
        "ok": p.returncode == 0,
        "stdout": p.stdout if full_stdout else p.stdout[-6000:],
        "stderr": p.stderr[-1500:],
    }


def _parse_json_stdout(stdout: str) -> dict:
    if not stdout:
        return {}
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return {}


def step_a_fetch(skip: bool) -> tuple[dict, dict | None]:
    if skip:
        if APIFOX_INDEX is None or not APIFOX_INDEX.is_file():
            raise RuntimeError("[incomplete] PLATFORM_APIFOX_INDEX 未配置或文件不存在")
        try:
            load_openapi_snapshot(APIFOX_INDEX)
        except ValueError as exc:
            raise RuntimeError(f"跳过拉取时 Apifox OAS 索引不可用：{exc}") from exc
        return {"skipped": True}, None
    run = _run(["python3", str(FETCH_SCRIPT.relative_to(ROOT)), "--json"], timeout=300)
    payload = _parse_json_stdout(run.get("stdout", ""))
    if not run.get("ok") or not payload.get("ok"):
        raise RuntimeError(payload.get("error") or run.get("stderr") or "步骤 A 失败")
    return {"run": run, "result": payload}, payload.get("fetch")


def step_b_audit() -> dict:
    if LOCAL_OAS is None or APIFOX_INDEX is None:
        raise RuntimeError("[incomplete] 请配置 PLATFORM_OAS_BASELINE 和 PLATFORM_APIFOX_INDEX")
    if not LOCAL_OAS.is_file() or not APIFOX_INDEX.is_file():
        raise RuntimeError("OpenAPI 快照文件不存在；请检查 PLATFORM_OAS_BASELINE 和 PLATFORM_APIFOX_INDEX")
    local = load_openapi_snapshot(LOCAL_OAS)
    apifox = load_openapi_snapshot(APIFOX_INDEX)
    from apifox_contract_audit import covered_ops  # noqa: E402
    from gen_apifox_coverage_wiki import collect  # noqa: E402

    _rows, path_index, _unresolved = collect()
    audit_result = audit(local, apifox, covered_ops())
    removed = removed_from_apifox(local, apifox, path_index)
    audit_result["removed_from_apifox"] = removed
    audit_result["stale_in_local"] = sorted(x["endpoint"] for x in removed)
    for item in audit_result["contract_diffs"]:
        info = path_index.get(item["endpoint"], {})
        item["caseIds"] = sorted(set(info.get("cases") or []))
        item["modules"] = sorted(set(info.get("modules") or []))
    from gen_apifox_coverage_wiki import collect
    _, full_index, _ = collect()
    for item in audit_result["schema_diffs"]:
        ids, modules = set(), set()
        for endpoint in item.get("allEndpoints", item.get("endpoints", [])):
            info = full_index.get(endpoint, {})
            ids.update(info.get("cases") or [])
            modules.update(info.get("modules") or [])
        item["caseIds"] = sorted(ids)
        item["modules"] = sorted(modules)
        fields = item.get("added", []) + item.get("removed", []) + [x["field"] for x in item.get("typeChanged", []) + item.get("constraintChanged", [])]
        item["fieldMapHits"] = _field_map_hits(item["modules"], fields)
    dl = (apifox.get("info") or {}).get("x-download-time")
    return {
        "audit": audit_result,
        "apifoxDownloadTime": dl,
        "summary": {
            "coveredCount": audit_result["covered_count"],
            "missingInLocal": len(audit_result["missing_in_local"]),
            "missingInApifox": len(audit_result["missing_in_apifox"]),
            "newInApifox": len(audit_result["new_in_apifox"]),
            "staleInLocal": len(audit_result["stale_in_local"]),
            "removedFromApifox": len(removed),
            "removedWithCases": sum(1 for r in removed if r["hasCases"]),
            "removedFromApifoxDetail": removed,
            "contractDiffs": len(audit_result["contract_diffs"]),
            "schemaDiffs": len(audit_result["schema_diffs"]),
            "schemaDiffSample": [
                {"schema": d["schema"], "added": d["added"], "removed": d["removed"],
                 "typeChanged": [t["field"] for t in d["typeChanged"]],
                 "constraintChanged": [t["field"] for t in d.get("constraintChanged", [])],
                 "caseIds": d.get("caseIds", [])[:12], "fieldMapHits": d.get("fieldMapHits", [])[:8]}
                for d in audit_result["schema_diffs"][:8]
            ],
        },
    }


def step_c_safe(skip: bool, reason: str = "cli") -> dict:
    if skip:
        return {"skipped": True, "skippedReason": reason}
    import catalog as cat  # noqa: E402
    import run_history as rh  # noqa: E402

    t0 = time.monotonic()
    result = cat.run_all(mode="safe")
    dur_ms = int((time.monotonic() - t0) * 1000)
    run_id = rh.record_run(result, source="inspection", duration_ms=dur_ms)

    by_suite: dict[str, dict] = defaultdict(lambda: {
        "passed": 0, "failed": [], "skipped": [], "incomplete": [], "total": 0,
    })
    for c in result["cases"]:
        cid = c.get("id", "?")
        meta = cat._CAT.get(cid) or {}
        suite = meta.get("suite") or "unknown"
        by_suite[suite]["total"] += 1
        status = c.get("status", "failed")
        if status == "passed":
            by_suite[suite]["passed"] += 1
        elif status in ("skipped", "incomplete"):
            by_suite[suite][status].append({"id": cid, "detail": next(
                (s.get("detail") for s in (c.get("subs") or []) if s.get("status") == status),
                status,
            )})
        else:
            err = c.get("error") or next(
                (s.get("detail") for s in (c.get("subs") or []) if s.get("status") == "failed"), "FAIL"
            )
            by_suite[suite]["failed"].append({"id": cid, "error": str(err)[:120]})

    failed = [c.get("id", "?") for c in result["cases"] if c.get("status") == "failed"]
    skipped = [c.get("id", "?") for c in result["cases"] if c.get("status") == "skipped"]
    incomplete = [c.get("id", "?") for c in result["cases"] if c.get("status") == "incomplete"]
    return {
        "runId": run_id,
        "status": result.get("status", "failed"),
        "passed": result["passed"],
        "failed": result.get("failed", len(failed)),
        "skippedCount": result.get("skipped", len(skipped)),
        "incomplete": result.get("incomplete", len(incomplete)),
        "total": result["total"],
        "durationMs": dur_ms,
        "failedIds": failed,
        "skippedIds": skipped,
        "incompleteIds": incomplete,
        "bySuite": {k: dict(v) for k, v in sorted(by_suite.items())},
    }


def step_d_finalize() -> dict:
    out = {}
    cov_run = _run(["python3", "scripts/gen_apifox_coverage_wiki.py"], timeout=120)
    out["coverageWiki"] = cov_run
    if not cov_run.get("ok"):
        raise RuntimeError("覆盖索引生成失败")

    # --strict：有「文档缺 id / 缺文档」时 exit 1，避免非 strict 恒 exit 0 造成假绿
    doc_run = _run(["python3", "scripts/check_case_doc_sync.py", "--strict"], timeout=60)
    out["docSync"] = doc_run
    doc_ok = doc_run.get("ok", False)
    out["docSyncOk"] = doc_ok
    if not doc_ok:
        tail = [ln for ln in (doc_run.get("stdout") or "").strip().splitlines() if ln.strip()]
        out["docSyncSummary"] = tail[-1] if tail else "文档与 CATALOG id 有差异"

    # CATALOG 静态体检结果一并入报告（错误应阻断，提示仅参考）
    vc_run = _run(
        ["python3", "scripts/validate_catalog.py", "--json"],
        timeout=60,
        full_stdout=True,
    )
    vc = _parse_json_stdout(vc_run.get("stdout", ""))
    out["catalogLint"] = {
        "ok": bool(vc.get("ok")),
        "errors": vc.get("errors") or [],
        "warnings": vc.get("warnings") or [],
    }

    coverage = {}
    if COVERAGE_JSON.is_file():
        cov = json.loads(COVERAGE_JSON.read_text(encoding="utf-8"))
        coverage = {
            "generatedAt": cov.get("generatedAt"),
            "totalCases": cov.get("totalCases"),
            "coveredPaths": cov.get("coveredPaths"),
            "apifoxPaths": cov.get("apifoxPaths"),
            "uncovered": sum(1 for p in cov.get("paths", []) if not p.get("covered")),
        }
    out["coverage"] = coverage
    return out


def _render_removed_section(removed: list[dict]) -> list[str]:
    if not removed:
        return []
    with_cases = [r for r in removed if r.get("hasCases")]
    lines = [
        "",
        "## Apifox 已删除接口（基线有、在线无）",
        "",
        f"共 **{len(removed)}** 条操作，其中 **{len(with_cases)}** 条仍有用例。",
        "有用例的项请先在 Agent 提示词中标注「删除用例」或「保留」；Agent 仅执行你已确认删除的项。",
        "",
        "| 接口 | 用例 | 模块 |",
        "| --- | --- | --- |",
    ]
    for r in removed:
        cases = ", ".join(f"`{x}`" for x in r["caseIds"]) if r["caseIds"] else "—"
        mods = ", ".join(r["modules"]) if r["modules"] else "—"
        lines.append(f"| `{r['endpoint']}` | {cases} | {mods} |")
    return lines


def render_report(today: str, b: dict, c: dict, d: dict) -> str:
    audit_r = b["audit"]
    safe = c if not c.get("skipped") else None
    cov = d.get("coverage") or {}
    dl = b.get("apifoxDownloadTime")
    schema_diffs = audit_r.get("schema_diffs") or []
    removed_ops = audit_r.get("removed_from_apifox") or []
    lint = d.get("catalogLint") or {}
    baseline_label = (
        "已推进" if b.get("baselineAdvanced") else
        "推进状态待确认（报告已写入，快照同步尚未确认）" if b.get("baselinePending") else
        "未推进：存在未消化的契约差异" if b.get("undigested") else
        "未推进：本轮未拉取在线契约"
    )

    lines = [
        f"# Apifox 契约巡检报告 · {today}",
        "",
        "[[../Apifox接口巡检计划|← 巡检计划]] · [[../../index|知识库 index]] · [[../Apifox测试覆盖/覆盖清单|覆盖清单]]",
        "",
        "> 由洞察页「运行 Apifox 巡检」/ `scripts/run_apifox_inspection.py` 自动生成。",
        "> 契约 diff 基线 = 上一轮巡检的本地快照；有 actionable diff 时保持基线不动。",
        "",
        "## 1. 摘要",
        "",
        "| 项 | 值 |",
        "| --- | --- |",
        f"| 已接入 HTTP 操作 | {audit_r['covered_count']} |",
        f"| 本地基线 trade 操作 | {audit_r['local_trade_paths']} |",
        f"| Apifox 在线 trade 操作 | {audit_r['apifox_trade_paths']} |",
        f"| 覆盖路径（自动化） | {cov.get('coveredPaths', '—')} / Apifox {cov.get('apifoxPaths', '—')} |",
        f"| 契约变更（指纹级） | {len(audit_r['contract_diffs'])} |",
        f"| 契约变更（schema 字段级） | {len(schema_diffs)} |",
        f"| 本地基线缺失（已覆盖） | {len(audit_r['missing_in_local'])} |",
        f"| 覆盖清单与 Apifox 不一致 | {len(audit_r['missing_in_apifox'])} |",
        f"| Apifox 已删除（基线有） | {len(removed_ops)} |",
        f"| Apifox 新增 trade 操作 | {len(audit_r['new_in_apifox'])} |",
        f"| 基线 | {baseline_label} |",
    ]
    if dl:
        lines.append(f"| Apifox 拉取时间 | {dl} |")
    if safe:
        rate = round(safe["passed"] / safe["total"] * 100) if safe["total"] else 0
        lines.append(
            f"| SAFE 通过率 | {safe['passed']}/{safe['total']} ({rate}%) · 失败 {safe.get('failed', 0)} "
            f"· 跳过 {safe.get('skippedCount', 0)} · 未完成 {safe.get('incomplete', 0)} · run #{safe['runId']} |"
        )
    else:
        why = "勾选了跳过 SAFE" if c.get("skippedReason") == "ui" else "命令行 --skip-safe"
        lines.append(f"| SAFE 通过率 | 未执行（{why}） |")
    doc_txt = "通过" if d.get("docSyncOk") else f"有差异 — {d.get('docSyncSummary', '见 check_case_doc_sync.py')}"
    lines.append(f"| 文档 id 校验（strict） | {doc_txt} |")
    if lint:
        lint_txt = ("0 error" if lint.get("ok") else f"{len(lint.get('errors') or [])} error") \
            + f" / {len(lint.get('warnings') or [])} 提示"
        lines.append(f"| CATALOG 静态体检 | {lint_txt} |")

    lines += ["", "## 2. 契约变更", ""]
    if not audit_r["contract_diffs"] and not schema_diffs:
        lines.append("_无（基线与在线契约在已覆盖接口范围内一致）_")
    if audit_r["contract_diffs"]:
        lines += ["### 指纹级（query 参数 / body / response $ref）", ""]
        for dff in audit_r["contract_diffs"][:15]:
            cases = ", ".join(f"`{x}`" for x in dff.get("caseIds", [])) or "覆盖索引中无用例 id"
            lines.append(f"- `{dff['endpoint']}`：local={dff['local']} → apifox={dff['apifox']}；用例 {cases}")
        if len(audit_r["contract_diffs"]) > 15:
            lines.append(f"- … 另有 {len(audit_r['contract_diffs']) - 15} 条")
        lines.append("")
    if schema_diffs:
        lines += ["### schema 字段级（VO/DTO 增删字段、类型变更）", "",
                  "| Schema | 新增 | 删除 | 类型变更 | 约束变更 | 用例 | 影响接口 |",
                  "| --- | --- | --- | --- | --- | --- | --- |"]
        for sd in schema_diffs[:20]:
            added = ", ".join(f"`{x}`" for x in sd["added"]) or "—"
            removed_cols = ", ".join(f"`{x}`" for x in sd["removed"]) or "—"
            changed = ", ".join(f"`{t['field']}`" for t in sd["typeChanged"]) or "—"
            constraints = ", ".join(f"`{t['field']}`" for t in sd.get("constraintChanged", [])) or "—"
            cases = ", ".join(f"`{x}`" for x in sd.get("caseIds", [])[:8]) or "覆盖索引中无用例 id"
            hits = ", ".join(f"`{x['file']}`({x['field']})" for x in sd.get("fieldMapHits", [])) or "无 FIELD_MAP 命中"
            eps = "<br>".join(f"`{e}`" for e in sd["endpoints"][:3])
            if len(sd.get("allEndpoints", sd["endpoints"])) > 3:
                eps += f"<br>另有 {len(sd.get('allEndpoints', sd['endpoints'])) - 3} 个"
            lines.append(f"| `{sd['schema']}` | {added} | {removed_cols} | {changed} | {constraints} | {cases}<br>{hits} | {eps} |")
        if len(schema_diffs) > 20:
            lines.append(f"| … 另有 {len(schema_diffs) - 20} 条 | | | | |")

    lines += _render_removed_section(removed_ops)

    lines += ["", "## 与数据库巡检对照", ""]
    if PATROL_CACHE is None or not PATROL_CACHE.is_file():
        lines.append("尚无数据库巡检缓存，未做列对照。")
    else:
        try:
            dbdiff = json.loads(PATROL_CACHE.read_text(encoding="utf-8")).get("diff") or {}
            if not dbdiff:
                lines.append("尚无数据库巡检缓存，未做列对照。")
            else:
                raw_fields = {x for sd in schema_diffs for x in sd.get("added", []) + sd.get("removed", [])}
                fields = set(raw_fields)
                for field in raw_fields:
                    parts = field.split("_")
                    fields.add(parts[0] + "".join(x[:1].upper() + x[1:] for x in parts[1:]))
                    fields.add(__import__("re").sub(r"(?<!^)([A-Z])", r"_\1", field).lower())
                dbfields = set()
                for mod in dbdiff.get("modified_tables", []):
                    for field in mod.get("added_columns", []) + mod.get("removed_columns", []):
                        dbfields.add(field)
                        parts = field.split("_")
                        dbfields.add(parts[0] + "".join(x[:1].upper() + x[1:] for x in parts[1:]))
                matches = sorted(fields & dbfields)
                lines.append("本轮契约字段与未消化的库列无同名项。" if not matches else "同名字段：" + ", ".join(f"`{x}`" for x in matches))
        except Exception:
            lines.append("尚无数据库巡检缓存，未做列对照。")

    lines += ["", "## 3. 已修复", "", wrap_hand_section(""), ""]

    todo: list[str] = []
    if removed_ops:
        with_cases = sum(1 for r in removed_ops if r.get("hasCases"))
        todo.append(
            f"- Apifox 已删除 {len(removed_ops)} 条操作（{with_cases} 条仍有用例）"
            " → 见 §「Apifox 已删除接口」，在 Agent 提示词中逐条确认是否删除用例"
        )
    if audit_r["missing_in_local"]:
        todo.append(f"- 本地基线缺 {len(audit_r['missing_in_local'])} 条已覆盖操作")
    if audit_r["contract_diffs"] or schema_diffs:
        todo.append(f"- 契约变更 {len(audit_r['contract_diffs'])} 指纹级 / {len(schema_diffs)} 字段级 → "
                    "对照 §2 更新 R 系列 VO 断言、D 系列 FIELD_MAP、LIVE payload 与查库工具")
    if safe and safe.get("failedIds"):
        todo.append(f"- SAFE 失败 {len(safe['failedIds'])} 条 → 对照 MCP `$ref` 修 `*_cases.py` / `*_db.py`")
    if safe and (safe.get("skippedIds") or safe.get("incompleteIds")):
        todo.append(
            f"- SAFE 覆盖不完整：跳过 {len(safe.get('skippedIds') or [])} 条、未完成 "
            f"{len(safe.get('incompleteIds') or [])} 条 → 补齐样本/部署/配置后重跑"
        )
    if not d.get("docSyncOk"):
        doc_label = f"`{CASE_DOC_DIR}/*.md`" if CASE_DOC_DIR else "所选 pack 的业务用例文档目录"
        todo.append(f"- 同步 {doc_label} 与 CATALOG id（`python3 scripts/check_case_doc_sync.py` 查明细）")
    if lint and not lint.get("ok"):
        todo.append(f"- CATALOG 结构错误 {len(lint.get('errors') or [])} 处 → `python3 scripts/validate_catalog.py`")
    if c.get("skipped"):
        todo.insert(0, f"- 动态验证未执行（{'勾选了跳过 SAFE' if c.get('skippedReason') == 'ui' else '命令行 --skip-safe'}）。静态契约无差异不等于用例已通过。")
    if b.get("undigested"):
        todo.append("- 处理完代码后执行 `python3 scripts/run_apifox_inspection.py --advance-baseline`；在此之前同一批 diff 会重复出现。")
    lines += ["", "## 4. 待办", ""]
    lines += todo if todo else (["_静态侧无待办；动态验证未执行。_"] if c.get("skipped") else ["_无待办。_"])

    if audit_r["missing_in_local"]:
        lines += ["", "### 本地基线缺失（抽样）", ""]
        for x in audit_r["missing_in_local"][:12]:
            lines.append(f"- `{x}`")

    if audit_r["new_in_apifox"]:
        lines += ["", "### Apifox 新增（抽样）", ""]
        for p in audit_r["new_in_apifox"][:15]:
            lines.append(f"- `{p}`")

    if safe:
        lines += ["", "## 5. 模块 SAFE 结果", "", "| 套件 | 通过 | 失败 | 跳过 | 未完成 |", "| --- | --- | --- | --- | --- |"]
        for suite, s in safe.get("bySuite", {}).items():
            fail_n = len(s.get("failed") or [])
            skipped_n = len(s.get("skipped") or [])
            incomplete_n = len(s.get("incomplete") or [])
            mark = "✅" if fail_n == 0 and skipped_n == 0 and incomplete_n == 0 else "⚠️"
            lines.append(f"| `{suite}` | {s['passed']}/{s['total']} {mark} | {fail_n} | {skipped_n} | {incomplete_n} |")
        if safe.get("failedIds"):
            lines += ["", "### FAIL 用例", ""]
            for suite, s in safe.get("bySuite", {}).items():
                for f in s.get("failed") or []:
                    lines.append(f"- `{f['id']}` ({suite}): {f['error']}")
        if safe.get("skippedIds") or safe.get("incompleteIds"):
            lines += ["", "### 覆盖缺口", ""]
            for suite, s in safe.get("bySuite", {}).items():
                for status in ("skipped", "incomplete"):
                    for item in s.get(status) or []:
                        lines.append(f"- `{item['id']}` ({suite}, {status}): {item['detail']}")

    lines += [
        "",
        "## 后续（巡检计划步骤 D · 人工）",
        "",
        "1. 失败项：MCP `read_project_oas_ref_resources_y0qspa` 读契约详情 → 改代码。",
        "2. 改完后 `./start.sh restart`；SAFE 未整轮跳过，且失败、跳过、未完成均为 0，并且基线已推进或本轮无 actionable diff，才算静态+动态都收敛。",
        "",
    ]
    # CATALOG warnings are grouped by their leading label.
    warns = lint.get("warnings") or []
    if warns:
        groups = defaultdict(list)
        for warning in warns:
            match = __import__("re").match(r"^\[([^\]]+)\]", warning)
            groups[match.group(1) if match else "其他"].append(warning)
        pos = lines.index("## 后续（巡检计划步骤 D · 人工）")
        cat = ["### CATALOG 提示（不阻断）", ""]
        for label, samples in sorted(groups.items()):
            cat.append(f"- {label} × {len(samples)}")
            cat.extend(f"  - {sample}" for sample in samples[:5])
        lines[pos:pos] = cat + [""]
    if b.get("undigested"):
        lines.insert(4, "> 本轮存在未消化契约差异；基线未推进。")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--skip-fetch", action="store_true")
    parser.add_argument("--skip-safe", action="store_true", help="跳过步骤 C（仅静态 + 索引，较快）")
    parser.add_argument("--skip-safe-reason", choices=("ui", "cli"), default="cli")
    parser.add_argument("--advance-baseline", action="store_true", help="人工确认后推进未消化基线")
    args = parser.parse_args()

    if REPORT_DIR is None:
        parser.error("[incomplete] 所选 pack 未配置业务知识库与 PLATFORM_APIFOX_REPORT_SUBDIR；巡检报告无法归档")
    if COVERAGE_JSON is None or UNDIGESTED is None:
        parser.error("[incomplete] 所选 pack 未配置 PLATFORM_PACK_DATA_DIR；巡检缓存无法读写")

    today = date.today().isoformat()
    out: dict = {
        "ok": True,
        "plan": str(APIFOX_PLAN_DOC) if APIFOX_PLAN_DOC else None,
        "planSteps": {},
    }

    try:
        a_step, fetch_info = step_a_fetch(args.skip_fetch)
        out["planSteps"]["A"] = {"label": "拉取 Apifox 最新契约", **a_step}
        if fetch_info:
            out["fetch"] = fetch_info

        out["planSteps"]["B"] = {"label": "静态契约对比"}
        b = step_b_audit()
        out["audit"] = b["summary"]
        out["planSteps"]["B"]["result"] = b["summary"]

        out["planSteps"]["C"] = {"label": "全量 SAFE 动态验证"}
        c = step_c_safe(args.skip_safe, args.skip_safe_reason)
        out["safe"] = c
        out["planSteps"]["C"]["result"] = {
            k: c[k] for k in ("skipped", "status", "passed", "failed", "skippedCount", "incomplete", "total", "runId", "durationMs", "failedIds", "skippedIds", "incompleteIds")
            if k in c
        }

        out["planSteps"]["D"] = {"label": "覆盖索引 / 文档校验"}
        d = step_d_finalize()
        out["planSteps"]["D"]["result"] = {
            "docSyncOk": d.get("docSyncOk"),
            "coverage": d.get("coverage"),
        }
        out["coverage"] = d.get("coverage")
        out["docSyncOk"] = d.get("docSyncOk")
        if d.get("docSyncSummary"):
            out["docSyncSummary"] = d["docSyncSummary"]
        if d.get("catalogLint"):
            out["catalogLint"] = {
                "ok": d["catalogLint"].get("ok"),
                "errors": len(d["catalogLint"].get("errors") or []),
                "warnings": len(d["catalogLint"].get("warnings") or []),
            }

        audit_result = b["audit"]
        advance = should_advance_baseline(audit_result, args.skip_fetch, args.advance_baseline)
        # The report must exist before the baseline moves, so retain a pending
        # state until the snapshot write actually succeeds.
        b["baselineAdvanced"] = False
        b["baselinePending"] = advance
        actionable = any(audit_result.get(k) for k in ("contract_diffs", "schema_diffs", "removed_from_apifox", "missing_in_apifox", "missing_in_local"))
        b["undigested"] = bool(not advance and actionable and not args.advance_baseline)
        out["baselineAdvanced"] = False
        out["baselinePending"] = advance
        out["baselineHoldReason"] = "pending" if advance else "skip-fetch" if args.skip_fetch else "actionable"
        out["undigested"] = b["undigested"]
        out["safe"] = c
        if b["undigested"] and not args.skip_fetch:
            UNDIGESTED.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(UNDIGESTED, json.dumps({"generatedAt": now_project_iso(), "baselineAdvanced": False,
                "contractDiffs": len(audit_result["contract_diffs"]), "schemaDiffs": len(audit_result["schema_diffs"]),
                "removedFromApifox": len(audit_result["removed_from_apifox"]), "missingInApifox": len(audit_result["missing_in_apifox"]),
                "missingInLocal": len(audit_result["missing_in_local"]),
                "contractDiffSample": audit_result["contract_diffs"][:8],
                "schemaDiffSample": [{"schema": d["schema"], "added": d["added"], "removed": d["removed"],
                    "typeChanged": d["typeChanged"], "constraintChanged": d.get("constraintChanged", []),
                    "endpoints": d.get("endpoints", [])[:6], "caseIds": d.get("caseIds", [])[:12]} for d in audit_result["schema_diffs"][:8]],
                "removedFromApifoxSample": audit_result["removed_from_apifox"][:8],
                "missingInApifoxSample": audit_result["missing_in_apifox"][:8],
                "missingInLocalSample": audit_result["missing_in_local"][:8]}, ensure_ascii=False, indent=2))
        elif not args.skip_fetch:
            UNDIGESTED.unlink(missing_ok=True)

        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        report_path = REPORT_DIR / f"{today}.md"
        rendered = render_report(today, b, c, d)
        old = report_path.read_text(encoding="utf-8") if report_path.exists() else None
        atomic_write_text(report_path, preserve_hand_section(old, rendered, migrate_apifox=True))
        try:
            out["reportPath"] = str(report_path.relative_to(ROOT))
        except ValueError:
            out["reportPath"] = str(report_path)

        # diff 已捕获进报告 → 基线推进为本次在线契约，下轮 diff 从本轮起算
        if advance:
            from fetch_apifox_oas import sync_local_snapshot  # noqa: E402
            sync_local_snapshot()
            b["baselineAdvanced"] = True
            b["baselinePending"] = False
            out["baselineAdvanced"] = True
            out["baselinePending"] = False
            out["baselineHoldReason"] = "forced" if args.advance_baseline else "advanced"

            # Finalize the report only after the baseline write succeeds. If
            # this rewrite fails, the earlier report remains explicitly
            # pending instead of falsely claiming the snapshot was advanced.
            rendered = render_report(today, b, c, d)
            old = report_path.read_text(encoding="utf-8") if report_path.exists() else None
            atomic_write_text(report_path, preserve_hand_section(old, rendered, migrate_apifox=True))

        warnings = []
        if b["summary"].get("newInApifox"):
            warnings.append(f"Apifox 新增 {b['summary']['newInApifox']} 条 trade 操作")
        if b["summary"].get("contractDiffs"):
            warnings.append(f"契约指纹变更 {b['summary']['contractDiffs']} 条（见报告 §2）")
        if b["summary"].get("schemaDiffs"):
            warnings.append(f"schema 字段级变更 {b['summary']['schemaDiffs']} 处（见报告 §2）")
        if b["summary"].get("removedFromApifox"):
            n = b["summary"]["removedFromApifox"]
            wc = b["summary"].get("removedWithCases", 0)
            warnings.append(f"Apifox 已删除 {n} 条（{wc} 条仍有用例，需确认是否删用例）")
        if not c.get("skipped") and c.get("failedIds"):
            warnings.append(f"SAFE 失败 {len(c['failedIds'])} 条")
        if not c.get("skipped") and (c.get("skippedIds") or c.get("incompleteIds")):
            warnings.append(
                f"SAFE 覆盖不完整：跳过 {len(c.get('skippedIds') or [])} 条、未完成 "
                f"{len(c.get('incompleteIds') or [])} 条"
            )
        if not d.get("docSyncOk"):
            warnings.append("文档与 CATALOG id 不一致（strict）")
        lint = d.get("catalogLint") or {}
        if lint and not lint.get("ok"):
            warnings.append(f"CATALOG 结构错误 {len(lint.get('errors') or [])} 处")
        catalog_warnings = lint.get("warnings") or []
        groups = defaultdict(int)
        for warning in catalog_warnings:
            m = __import__("re").match(r"^\[([^\]]+)\]", warning)
            groups[m.group(1) if m else "其他"] += 1
        if groups:
            out["catalogWarningSummary"] = {"total": len(catalog_warnings), "top": sorted(groups.items(), key=lambda x: (-x[1], x[0]))[:3]}
        out["warnings"] = warnings

    except subprocess.TimeoutExpired:
        out = {"ok": False, "error": "巡检超时（SAFE 用例较多时可加 --skip-safe 先跑静态部分）"}
    except Exception as e:
        out = {"ok": False, "error": str(e)}

    print(json.dumps(out, ensure_ascii=False, indent=None if args.json else 2))
    sys.exit(0 if out.get("ok") else 1)


if __name__ == "__main__":
    main()
