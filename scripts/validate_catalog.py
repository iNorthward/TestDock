#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CATALOG 静态体检 —— 不执行用例，秒级校验用例目录的结构与防假绿反模式。

校验项（全部为静态检查，不发 HTTP、不连 DB、不跑 runner）：
  结构类（error）
    1. 用例 id 全局唯一
    2. 必填字段齐全：id / suite / group / desc / mode / expected
    3. mode ∈ {safe, live}
    4. expect 能映射到模块 RUNNERS（模块无 RUNNERS 时用已知回退集）
    5. suite 存在于模块 SUITES
    6. id 含 -LIVE 者 mode 必须为 live
   11. 每条用例可解析出 api（case.api 或模块 RUNNER_API[expect]），格式 "METHOD /path"
        （用例↔接口显式映射，见《用例元信息规范》§4；覆盖率据此统计）
   12. D 系列 case.db.where 禁止 1=1
  规范类（warning）
    7. group 建议含系列/端侧标识（·只读/·筛选/·对账/·异常/·越权/·鉴权/LIVE 等）
    8. 主要接口为 GET 的 suite 建议至少有一条 R 系列（只读契约地基）
   13. api 路径不在配置的 Apifox OAS 索引中
   14. rules.allow_empty=True 但 desc/expected 未注明理由
   15. suite 未被 catalog.NAV_TREE 导航树收录
  防假绿源码 lint（warning，扫描 *_cases.py 与 common/*.py 文本）
    9. int(x or -1) 反模式（total=0 会被误判 -1，见 F 规范 §4.3）
   10. reconcile / db_count 处出现裸 where "1=1"（应带软删/tenant 口径）
  统一入口 lint（error）：本地 _http / build_opener / is_ok / is_reject 定义
  金额比较 lint（warning）：两值金额差值比较应使用 common/money_cmp.py

用法：
  python3 scripts/validate_catalog.py            # 人类可读报告
  python3 scripts/validate_catalog.py --json      # 机器可读（供面板/洞察）
  python3 scripts/validate_catalog.py --strict    # 有 error 时 exit 1（供验收 / pre-commit）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TP = ROOT / "test-platform"
for sub in ("order", "guess", "common"):
    sys.path.insert(0, str(TP / sub))
sys.path.insert(0, str(TP))

REQUIRED_FIELDS = ("id", "suite", "group", "desc", "mode", "expected")
VALID_MODES = {"safe", "live"}
VALID_COVERAGE_SERIES = {"R", "F", "D", "E"}
# 模块未定义 RUNNERS 字典时（兼容显式内联分发）的已知 expect 回退集
FALLBACK_EXPECTS = {"preview", "reject", "live"}


API_RE = re.compile(r"^(GET|POST|PUT|DELETE|PATCH)\s+/\S+$")
ALLOW_EMPTY_REASON_RE = re.compile(r"可空|可能无|无数据|预期空|允许为空|可为空|数据不足")

INT_OR_NEG1_RE = re.compile(r"int\(\s*[^)]*\bor\s+-1\s*\)")
# 仅命中 CATALOG 里 D 用例的 {"where": "1=1"} 全表对账反模式；
# 不误伤 db_count(where="1=1") 这类 helper 默认值或有意的全表 total。
WHERE_1EQ1_RE = re.compile(r"""["']where["']\s*:\s*["']\s*1\s*=\s*1\s*["']""", re.IGNORECASE)

def load_oas_paths() -> set:
    """(METHOD, path) 集合；索引不存在时返回空集（跳过收录校验）。"""
    from project_inputs import project_input_path

    oas_index = project_input_path("PLATFORM_APIFOX_INDEX", required=False)
    if oas_index is None or not oas_index.exists():
        return set()
    data = json.loads(oas_index.read_text(encoding="utf-8"))
    valid = {"get", "post", "put", "delete", "patch"}
    out = set()
    for path, methods in (data.get("paths") or {}).items():
        for method in methods:
            if method.lower() in valid:
                out.add((method.upper(), path))
    return out


def case_apis(case: dict, runner_api: dict):
    """解析用例触达的接口列表：case.api 优先，回退模块 RUNNER_API[expect]。

    - 返回 None 表示无法解析（两处都未声明，或 {path} 模板缺 case.path）；
      显式声明为空列表（无 HTTP 的用例）返回 []，不算错误。
    - "{path}" 模板替换为 case["path"]（去掉查询串），见《用例元信息规范》§4。
    """
    apis = case.get("api")
    if apis is None:
        apis = (runner_api or {}).get(case.get("expect"))
    if apis is None:
        return None
    if isinstance(apis, str):
        apis = [apis]
    out = []
    for a in apis:
        a = str(a)
        if "{path}" in a:
            p = str(case.get("path") or "").split("?")[0]
            if not p.startswith("/"):
                return None
            a = a.replace("{path}", p)
        out.append(a)
    return out


def _series_of(case_id: str) -> str:
    """从 id 推断系列：NC-R01→R，IV-LIVE-B01→LIVE，AGP-F03→F。"""
    parts = case_id.split("-")
    for seg in parts[1:]:
        if seg.upper().startswith("LIVE"):
            return "LIVE"
        m = re.match(r"^([A-Z]+)", seg)
        if m and m.group(1) in ("R", "F", "D", "E", "B", "C", "P", "FF"):
            return m.group(1)
    return ""


def check_structure():
    """返回 (errors, warnings, stats)。基于 catalog.py 已聚合的注册表。"""
    import catalog as cat  # noqa: E402  导入即完成所有 *_cases.py 加载

    errors: list[str] = []
    warnings: list[str] = []
    seen_ids: dict[str, str] = {}
    suite_series: dict[str, set] = {}
    write_only_suites: set[str] = set()
    per_module = []
    oas = load_oas_paths()

    for mod in cat.MODULES:
        mod_name = getattr(mod, "__name__", "?")
        catalog = getattr(mod, "CATALOG", [])
        suite_ids = {s.get("id") for s in getattr(mod, "SUITES", [])}
        for suite in getattr(mod, "SUITES", []):
            sid = suite.get("id")
            endpoint = str(suite.get("endpoint") or "")
            method_match = re.match(r"^\s*([A-Za-z]+(?:/[A-Za-z]+)*)\s+/", endpoint)
            if sid and method_match:
                declared_methods = {method.upper() for method in method_match.group(1).split("/")}
                if declared_methods and "GET" not in declared_methods:
                    write_only_suites.add(sid)
        runners = getattr(mod, "RUNNERS", None)
        runner_keys = set(runners.keys()) if isinstance(runners, dict) else None
        runner_api = getattr(mod, "RUNNER_API", {}) or {}
        mod_errors = 0

        for c in catalog:
            cid = c.get("id", "<无 id>")
            # 2. 必填字段
            missing = [f for f in REQUIRED_FIELDS if f not in c]
            if missing:
                errors.append(f"[{mod_name}] {cid} 缺字段：{', '.join(missing)}")
                mod_errors += 1
            # 1. 全局唯一 id
            if cid in seen_ids:
                errors.append(f"[{mod_name}] 用例 id 重复：{cid}（已存在于 {seen_ids[cid]}）")
                mod_errors += 1
            else:
                seen_ids[cid] = mod_name
            # 3. mode 合法
            mode = c.get("mode")
            if mode not in VALID_MODES:
                errors.append(f"[{mod_name}] {cid} mode 非法：{mode!r}（应为 safe/live）")
                mod_errors += 1
            # 6. -LIVE id 必须 live
            if "-LIVE" in str(cid).upper() and mode != "live":
                errors.append(f"[{mod_name}] {cid} id 含 -LIVE 但 mode={mode!r}（应为 live）")
                mod_errors += 1
            coverage_series = c.get("coverageSeries")
            if coverage_series is not None and (
                not isinstance(coverage_series, str)
                or coverage_series not in VALID_COVERAGE_SERIES
            ):
                errors.append(
                    f"[{mod_name}] {cid} coverageSeries={coverage_series!r} 非法 "
                    f"（应为 {'/'.join(sorted(VALID_COVERAGE_SERIES))}）"
                )
                mod_errors += 1
            # 4. expect → runner 映射
            exp = c.get("expect")
            if exp is not None:
                known = runner_keys if runner_keys is not None else FALLBACK_EXPECTS
                if exp not in known:
                    errors.append(f"[{mod_name}] {cid} expect={exp!r} 无对应 runner")
                    mod_errors += 1
            # 5. suite 归属
            sid = c.get("suite")
            if suite_ids and sid not in suite_ids:
                errors.append(f"[{mod_name}] {cid} suite={sid!r} 不在模块 SUITES 中")
                mod_errors += 1
            # 11. api 显式映射（error）；显式空列表 = 无 HTTP 用例，放行
            apis = case_apis(c, runner_api)
            if apis is None:
                errors.append(f"[{mod_name}] {cid} 无法解析 api（补 case.api 或 RUNNER_API[{exp!r}]，见《用例元信息规范》§4）")
                mod_errors += 1
            for a in apis or []:
                if not API_RE.match(str(a)):
                    errors.append(f"[{mod_name}] {cid} api 格式非法：{a!r}（应为 \"METHOD /path\"）")
                    mod_errors += 1
                elif oas:
                    method, path = str(a).split(" ", 1)
                    if (method, path) not in oas:
                        warnings.append(f"[{mod_name}] {cid} api 不在 Apifox OAS 索引：{a}")
            # 12. D 系列 db.where 禁止 1=1（error）
            db_spec = c.get("db")
            if isinstance(db_spec, dict) and re.search(r"\b1\s*=\s*1\b", str(db_spec.get("where") or "")):
                errors.append(f"[{mod_name}] {cid} db.where 含 1=1（须复刻接口业务口径）")
                mod_errors += 1
            # 14. allow_empty 需注明理由（warning）
            rules = c.get("rules") or {}
            if rules.get("allow_empty"):
                text = f"{c.get('desc', '')} {c.get('expected', '')}"
                if not ALLOW_EMPTY_REASON_RE.search(text):
                    warnings.append(f"[{mod_name}] {cid} allow_empty=True 但 desc/expected 未注明理由")
            # Project grouping conventions are optional Pack policy.
            grp = c.get('group')
            if not isinstance(grp,str) or not grp.strip():
                errors.append(f'[{mod_name}] {cid} group 须为非空字符串')
            policy=next(iter(getattr(cat,'PACKS',[])),{}).get('catalog_policy',{})
            tokens=policy.get('group_tokens',[])
            if tokens and isinstance(grp,str) and not any(token in grp for token in tokens):
                warnings.append(f'[{mod_name}] {cid} group 不符合当前项目声明的分组规范')
            # 收集 suite→系列，供 R 地基检查
            suite_series.setdefault(sid, set()).add(_series_of(cid))

        per_module.append({"module": mod_name, "cases": len(catalog), "errors": mod_errors})

    # 8. 只对主要接口明确包含 GET 的 suite 要求 R 地基；纯写 suite 不需要只读契约用例。
    for sid, series in sorted(suite_series.items()):
        if (next(iter(getattr(cat,'PACKS',[])),{}).get('catalog_policy',{}).get('require_readonly_baseline',False)
                and sid and sid not in write_only_suites and 'R' not in series and series - {''}):
            warnings.append(f"[suite:{sid}] 无 R 系列（只读契约）用例——建议先补 R 地基")

    # 15. suite 未被导航树收录（warning）
    if hasattr(cat, "unclaimed_suites"):
        for sid in cat.unclaimed_suites():
            warnings.append(f"[catalog] suite {sid} 未被 NAV_TREE 收录（将渲染在导航顶层）")

    if not oas and any('contract' in pack.get('diagnostic_capabilities', []) for pack in getattr(cat, 'PACKS', [])):
        warnings.append('项目已启用契约诊断，但契约索引未配置或不存在；API 收录校验未执行')

    stats = {
        "totalCases": len(seen_ids),
        "modules": len(cat.MODULES),
        "suites": len(suite_series),
        "perModule": per_module,
    }
    return errors, warnings, stats


def check_source_antipatterns():
    """扫描所选 Pack 与 Core common 的防假绿模式和统一入口违规。"""
    warnings: list[str] = []
    errors: list[str] = []
    from pack_registry import case_source_files, load_packs

    packs = load_packs()
    files = list(case_source_files(packs)) + list((TP / "common").glob("*.py"))
    # Cases and their helpers consume the platform API. Low-level adapters
    # implement those interfaces; operational collectors are separate tools.
    entrypoint_files = set(files)
    entrypoint_files.update(source for pack in packs for source in (pack["root"] / "helpers").rglob("*.py"))
    files += [source for pack in packs for source in pack["root"].rglob("*.py")]
    allowed_http = {"test-platform/common/api_client.py"}
    allowed_opener = {"test-platform/common/api_client.py"}
    allowed_response_defs = {
        "test-platform/common/security_helpers.py": {"is_ok", "is_reject"},
        "test-platform/common/page_filter_helpers.py": {"is_ok"},
        # The protocol declares these methods; project behavior still comes
        # from the registered ResponseEnvelope implementation.
        "test-platform/common/response_envelope.py": {"is_ok", "is_reject"},
    }
    money_diff_allowlist = {
        "test-platform/common/money_cmp.py",
        "test-platform/common/db_reconcile_helpers.py",
    }
    local_http_re = re.compile(r"^\s*def\s+_http\s*\(")
    opener_re = re.compile(r"urllib\.request\.build_opener")
    response_def_re = re.compile(r"^\s*def\s+(is_ok|is_reject)\s*\(")
    money_diff_re = re.compile(r"abs\(float\(.*\)\s*-\s*float\(")
    for fp in sorted(set(files)):
        try:
            text = fp.read_text(encoding="utf-8")
        except OSError:
            continue
        rel = fp.relative_to(ROOT).as_posix()
        lines = text.splitlines()
        skip_numeric_next = False
        for i, line in enumerate(lines, 1):
            if skip_numeric_next:
                skip_numeric_next = False
                continue
            if INT_OR_NEG1_RE.search(line):
                warnings.append(f"[防假绿] {rel}:{i} 疑似 int(x or -1)（total=0 会被判 -1，改 int(x if x is not None else -1)）")
            if WHERE_1EQ1_RE.search(line):
                warnings.append(f"[对账口径] {rel}:{i} 裸 where '1=1'（应带软删/tenant 口径，见 D 规范）")
            if fp in entrypoint_files and local_http_re.search(line) and rel not in allowed_http:
                errors.append(f"[统一入口] {rel}:{i} 本地定义 _http，请改用 common/api_client.py")
            if fp in entrypoint_files and opener_re.search(line) and rel not in allowed_opener:
                errors.append(f"[统一入口] {rel}:{i} 本地 build_opener，请改用 common/api_client.py")
            response_match = response_def_re.search(line)
            if fp in entrypoint_files and response_match and response_match.group(1) not in allowed_response_defs.get(rel, set()):
                errors.append(f"[统一入口] {rel}:{i} 本地定义 {response_match.group(1)}，请改用 common/api_response.py")
            if "# numeric-not-money" in line:
                skip_numeric_next = True
                continue
            if money_diff_re.search(line) and rel not in money_diff_allowlist:
                warnings.append(f"[金额比较] {rel}:{i} 疑似两值金额比较，请使用 money_cmp.money_eq")
    return errors, warnings


def _warn_kind(warning: str) -> str:
    if "未含系列标识" in warning:
        return "group 未含系列标识"
    if "allow_empty=True" in warning:
        return "allow_empty 未注明理由"
    if "无 R 系列" in warning:
        return "suite 无 R 系列"
    if "未被 NAV_TREE" in warning:
        return "suite 未进导航"
    return "其他"


def _print_warning_summary(warnings: list[str]) -> None:
    grouped: dict[str, list[str]] = {}
    for warning in warnings:
        grouped.setdefault(_warn_kind(warning), []).append(warning)
    for kind, items in grouped.items():
        print(f"  {kind}：{len(items)}")
        if kind == "group 未含系列标识":
            modules = Counter(item.split("]", 1)[0].lstrip("[") for item in items)
            detail = "，".join(f"{name} {count}" for name, count in modules.most_common())
            print(f"    {detail}")
            continue
        for item in items:
            print(f"    - {item}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    ap.add_argument("--strict", action="store_true", help="有 error 时 exit 1")
    args = ap.parse_args()

    errors, warnings, stats = check_structure()
    source_errors, source_warnings = check_source_antipatterns()
    errors.extend(source_errors)
    warnings = warnings + source_warnings

    if args.json:
        print(json.dumps({
            "ok": not errors,
            "errors": errors,
            "warnings": warnings,
            "stats": stats,
        }, ensure_ascii=False))
        sys.exit(1 if (args.strict and errors) else 0)

    print(f"CATALOG 体检：{stats['totalCases']} 条用例 / {stats['modules']} 模块 / {stats['suites']} suite")
    if errors:
        print(f"\n✗ 结构错误 {len(errors)} 处：")
        for e in errors:
            print(f"    - {e}")
    else:
        print("✓ 结构检查通过（id 唯一 / 字段齐全 / mode 合法 / expect 有 runner / suite 归属正确）")
    if warnings:
        print(f"\n⚠ 提示 {len(warnings)} 处：")
        _print_warning_summary(warnings)
    if not errors and not warnings:
        print("✓ 无任何问题。")

    sys.exit(1 if (args.strict and errors) else 0)


if __name__ == "__main__":
    main()
