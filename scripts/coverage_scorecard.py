#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R/F/D/E 候选↔用例统一覆盖率评分卡。

把「覆盖率」变成可度量的本地指标：对每个系列，统计
  应覆盖接口数（有候选的接口）  vs  已实现接口数（有该系列 ≥1 条用例的接口）
并给出实现率、候选总数、以及「候选最多但仍 0 用例」的高优先补全接口。

候选来源：gen_{e,f,d,r}_series_candidates.py（机械派生）。
实际用例：catalog._ALL 与 coverage.json 的 endpoint→cases 映射聚合；系列优先使用
         显式 coverageSeries，再回退到 id 系列字母（validate_catalog._series_of）。

用法：
  python3 scripts/coverage_scorecard.py                 # 全量评分卡
  python3 scripts/coverage_scorecard.py --covered-only  # 只看已接入用例的接口
  python3 scripts/coverage_scorecard.py --series D --top 20   # 单系列高优先缺口
  python3 scripts/coverage_scorecard.py --json          # 输出机器可读 JSON
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "test-platform"))

import gen_e_series_candidates as gen_e  # noqa: E402
import gen_f_series_candidates as gen_f  # noqa: E402
import gen_d_series_candidates as gen_d  # noqa: E402
import gen_r_series_candidates as gen_r  # noqa: E402
from candidate_inputs import iter_openapi_operations, load_coverage, load_openapi_snapshot  # noqa: E402
from project_inputs import pack_data_path  # noqa: E402


def _non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是非负整数") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("必须是非负整数")
    return parsed


def _iter_ops(prefix: str, covered_only: bool, cover: dict):
    oas = load_openapi_snapshot(gen_e.LOCAL_OAS)
    for m, path, spec in iter_openapi_operations(oas, prefix):
        cov = cover.get(f"{m} {path}", {})
        if covered_only and not cov.get("covered"):
            continue
        yield m, path, spec, cov


def collect_candidates(prefix: str, covered_only: bool) -> dict[str, dict]:
    """→ {series: {endpoint: {"candidates": n, "byKind": {...}}}}。"""
    cover = gen_e.load_coverage(required=covered_only)
    schema_tables = gen_d.load_schema_tables()
    out = {s: {} for s in ("E", "F", "D", "R")}

    def record(series, result):
        if not result:
            return
        ep = result["endpoint"]
        by_kind = {}
        for c in result["candidates"]:
            by_kind[c["kind"]] = by_kind.get(c["kind"], 0) + 1
        keys = [c["key"] for c in result["candidates"] if c.get("key")]
        out[series][ep] = {"candidates": result["candidateCount"], "byKind": by_kind, "keys": keys}

    for m, path, spec, cov in _iter_ops(prefix, covered_only, cover):
        record("E", gen_e.derive_for_op(m, path, spec, cov))
        record("F", gen_f.derive_for_op(m, path, spec, cov))
        record("D", gen_d.derive_for_op(m, path, spec, cov, schema_tables))
        record("R", gen_r.derive_for_op(m, path, spec, cov))
    return out


def _series_bucket(case_id: str, coverage_series: str | None = None) -> str:
    """按显式 coverageSeries 或 case id 将用例归入 R/F/D/E 四桶。"""
    from validate_catalog import _series_of
    s = (coverage_series or _series_of(case_id) or "").upper()
    if s.startswith("R"):
        return "R"
    if s.startswith("F"):
        return "F"
    if s.startswith("D"):
        return "D"
    if s.startswith("E") or s.startswith("B"):  # B=边界，归入异常/健壮性
        return "E"
    return ""  # LIVE 等不计入 R/F/D/E 评分


def collect_existing_cases() -> dict[str, dict[str, list]]:
    """→ {series: {endpoint: [case_id, ...]}}。

    直接复用 coverage.json 的权威 endpoint→cases 映射（由 gen_apifox_coverage_wiki.py
    生成），而非重解析 case.api（后者对多数用例返回 None）。
    """
    cov_path = pack_data_path("coverage.json")
    out: dict[str, dict[str, list]] = {}
    coverage = load_coverage(cov_path, required=True)
    import catalog
    coverage_series_by_id = {
        case.get("id"): case.get("coverageSeries")
        for case in catalog._ALL
        if case.get("id")
    }
    for p in coverage.values():
        ep = f"{p['method']} {p['path']}"
        for cid in p.get("cases", []):
            bucket = _series_bucket(cid, coverage_series_by_id.get(cid))
            if not bucket:
                continue
            out.setdefault(bucket, {}).setdefault(ep, []).append(cid)
    return out


def collect_covers() -> dict[str, set]:
    """→ {series: set(candidate_key)}：读 CATALOG 用例的 `covers` 字段（kind 级追溯）。

    用例可选声明 `covers: ["GET /x/page|enum_each_value|status", ...]`，列出它满足的候选 key。
    有了它，评分卡就能算「候选 kind 真正被实现的比例」，而非只看接口有没有用例。
    尚无用例标注时返回空，kind 级实现率显示 n/a。
    """
    import catalog
    out: dict[str, set] = {}
    for c in catalog._ALL:
        covers = c.get("covers")
        if not covers:
            continue
        series = _series_bucket(c.get("id", ""), c.get("coverageSeries"))
        if series:
            out.setdefault(series, set()).update(covers)
    return out


def build_scorecard(prefix: str, covered_only: bool) -> dict:
    existing = collect_existing_cases()
    candidates = collect_candidates(prefix, covered_only)
    covers = collect_covers()

    scorecard = {}
    for series in ("R", "F", "D", "E"):
        cand_eps = candidates.get(series, {})
        real_eps = existing.get(series, {})
        expected = set(cand_eps.keys())
        realized = {ep for ep in expected if real_eps.get(ep)}
        total_cand = sum(v["candidates"] for v in cand_eps.values())
        # 候选最多但 0 用例的接口（补全优先级）
        gaps = sorted(
            ((ep, cand_eps[ep]["candidates"]) for ep in expected - realized),
            key=lambda x: -x[1],
        )
        by_kind = {}
        all_keys = set()
        for v in cand_eps.values():
            all_keys.update(v.get("keys", []))
            for k, n in v["byKind"].items():
                by_kind[k] = by_kind.get(k, 0) + n
        # kind 级实现（依赖用例的 covers 标注）
        series_covers = covers.get(series, set())
        realized_keys = series_covers & all_keys
        has_covers = bool(series_covers)
        scorecard[series] = {
            "expectedEndpoints": len(expected),
            "realizedEndpoints": len(realized),
            "realizationRate": round(len(realized) / max(len(expected), 1) * 100, 1),
            "totalCandidates": total_cand,
            "existingCasesEndpoints": len(real_eps),
            "candidatesByKind": by_kind,
            "topGaps": gaps,
            # kind 级（None 表示尚无 covers 标注）
            "kindTotal": len(all_keys),
            "kindRealized": len(realized_keys),
            "kindRate": round(len(realized_keys) / max(len(all_keys), 1) * 100, 1) if has_covers else None,
        }
    return scorecard


def main():
    ap = argparse.ArgumentParser(description="R/F/D/E 候选↔用例统一覆盖率评分卡")
    ap.add_argument("--prefix", default="", help="只看该路径前缀")
    ap.add_argument("--covered-only", action="store_true", help="只看已接入用例的接口")
    ap.add_argument("--series", default="", type=str.upper, choices=("R", "F", "D", "E"),
                    help="只详列某系列的高优先缺口（R/F/D/E）")
    ap.add_argument("--top", type=_non_negative_int, default=15, help="高优先缺口展示条数")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = ap.parse_args()

    try:
        sc = build_scorecard(args.prefix, args.covered_only)
    except ValueError as exc:
        ap.error(str(exc))

    if args.json:
        print(json.dumps(sc, ensure_ascii=False, indent=1))
        return

    scope = "已接入接口" if args.covered_only else "全部接口"
    print(f"R/F/D/E 覆盖率评分卡（范围：{scope}"
          + (f"，前缀 {args.prefix}" if args.prefix else "") + "）\n")
    print(f"  {'系列':<4} {'应覆盖':>6} {'已实现':>6} {'接口实现率':>9} {'候选总数':>8} {'kind实现率':>10}")
    print("  " + "-" * 56)
    for series in ("R", "F", "D", "E"):
        s = sc[series]
        kind = f"{s['kindRealized']}/{s['kindTotal']} {s['kindRate']}%" if s["kindRate"] is not None else "n/a"
        print(f"  {series:<5} {s['expectedEndpoints']:>6} {s['realizedEndpoints']:>6} "
              f"{s['realizationRate']:>7}% {s['totalCandidates']:>8} {kind:>12}")
    if all(sc[x]["kindRate"] is None for x in ("R", "F", "D", "E")):
        print("\n  注：kind 级实现率暂为 n/a——给用例加 `covers: [\"<candidate key>\"]` 即可开启逐类闭环。")
        print("      candidate key 见各生成器 --json 输出的 key 字段，如 \"GET /x/page|enum_each_value|status\"。")

    if args.series:
        series = args.series
        s = sc[series]
        print(f"\n{series} 系列 · 候选按 kind 分布：")
        for k, n in sorted(s["candidatesByKind"].items(), key=lambda x: -x[1]):
            print(f"    {k:28} {n:4} 条")
        print(f"\n{series} 系列 · 高优先补全接口（候选多、0 用例，前 {args.top}）：")
        for ep, n in s["topGaps"][:args.top]:
            print(f"    {n:3} 候选  {ep}")
    else:
        print("\n（加 --series R/F/D/E 查看单系列 kind 分布与高优先缺口）")


if __name__ == "__main__":
    main()
