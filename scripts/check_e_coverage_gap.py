#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E 系列候选与实际用例缺口分析 —— 候选↔用例闭环追踪。

读取 gen_e_series_candidates.py 派生的候选清单,比对 test-platform/*_cases.py
已落地的 E 系列用例,按接口/kind 输出缺口统计。

用法：
  python3 scripts/check_e_coverage_gap.py                    # 汇总统计
  python3 scripts/check_e_coverage_gap.py --detail           # 详细缺口（每接口每类）
  python3 scripts/check_e_coverage_gap.py --json             # JSON 输出
  python3 scripts/check_e_coverage_gap.py --prefix /v1 # 只看某路径前缀
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "test-platform"))
sys.path.insert(0, str(ROOT / "scripts"))

from gen_e_series_candidates import candidate_key
from candidate_inputs import load_openapi_snapshot, iter_openapi_operations


def load_candidates(prefix: str = "", covered_only: bool = False) -> dict:
    """返回 {candidate_key: candidate_dict}。"""
    import gen_e_series_candidates as gen
    oas = load_openapi_snapshot(gen.LOCAL_OAS)
    cover = gen.load_coverage(required=covered_only)
    candidates = {}
    for method, path, spec in iter_openapi_operations(oas, prefix):
        key = f"{method.upper()} {path}"
        cov = cover.get(key, {})
        if covered_only and not cov.get("covered"):
            continue
        result = gen.derive_for_op(method.upper(), path, spec, cov)
        for c in result["candidates"]:
            candidates[c["key"]] = {
                "endpoint": result["endpoint"],
                "kind": c["kind"],
                "desc": c["desc"],
                "param": c.get("param"),
            }
    return candidates


def load_existing_e_cases() -> dict:
    """返回 {endpoint: [case_dict]}（按接口聚合已有 E 用例）。"""
    import catalog
    sys.path.insert(0, str(ROOT / "scripts"))
    from validate_catalog import case_apis

    by_endpoint = {}
    for c in catalog._ALL:
        cid = c.get("id", "")
        if not cid or "-E" not in cid.upper():
            continue
        # 从 case.api 或 RUNNER_API 获取接口
        # A module may own several suites (for example, user_client_cases owns
        # both user-client-mgr and user-client-status). Resolve the runner from
        # the catalog's case→module index instead of guessing from suite text.
        mod = catalog._EXEC.get(cid)
        runner_api = getattr(mod, "RUNNER_API", {}) if mod else {}
        apis = case_apis(c, runner_api)
        if not apis:
            continue
        case_row = {
            "id": cid,
            "suite": c.get("suite"),
            "desc": c.get("desc"),
            "group": c.get("group"),
        }
        # A runner can touch multiple APIs. Count this case against each API,
        # but only once per endpoint if the mapping repeats one.
        for endpoint in dict.fromkeys(apis):
            by_endpoint.setdefault(endpoint, []).append(case_row)
    return by_endpoint


def analyze_gap(candidates: dict, existing_by_endpoint: dict, detail: bool = False):
    """比对候选与实际,按接口聚合输出。

    不强求候选与用例精确匹配,只标记:
      - 每接口有多少候选(按 kind 分类)
      - 已有多少 E 用例(不论是否匹配候选)
      - 供人工对照已有断言的通用 E 候选类型（不推断其是否已覆盖）
    """
    # 按接口聚合候选
    cand_by_endpoint = {}
    for key, c in candidates.items():
        ep = c["endpoint"]
        cand_by_endpoint.setdefault(ep, []).append(c)

    # 通用 E 类型优先级(规范要求的最低覆盖)
    UNIVERSAL_KINDS = {
        "no_token": 1,
        "bad_token": 2,
        "bad_tenant": 3,
        "cross_side": 4,
        "page_abuse": 5,
        "page_size_abuse": 6,
        "cross_user": 7,  # CLI 最高优先级
    }

    # 统计
    endpoints_with_candidates = set(cand_by_endpoint.keys())
    endpoints_with_cases = set(existing_by_endpoint.keys())
    no_coverage = endpoints_with_candidates - endpoints_with_cases
    partial = endpoints_with_candidates & endpoints_with_cases

    summary = {
        "totalCandidates": len(candidates),
        "totalEndpoints": len(endpoints_with_candidates),
        # Keep prefix-filtered reports scoped to candidate endpoints; otherwise
        # unrelated E-covered APIs can make the percentage exceed 100%.
        "endpointsWithECases": len(partial),
        "endpointsNoECases": len(no_coverage),
        "candidatesByKind": {},
    }

    # 按 kind 统计候选总数
    for c in candidates.values():
        kind = c["kind"]
        summary["candidatesByKind"][kind] = summary["candidatesByKind"].get(kind, 0) + 1

    result = {"summary": summary}

    if detail:
        gaps = []
        for ep in sorted(no_coverage):
            cands = cand_by_endpoint.get(ep, [])
            kinds = {}
            for c in cands:
                kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
            # 候选来自 OAS schema；不根据 case ID/描述猜测已有断言是否覆盖。
            candidate_universal = [k for k in UNIVERSAL_KINDS if kinds.get(k, 0) > 0]
            gaps.append({
                "endpoint": ep,
                "candidateCount": len(cands),
                "candidatesByKind": kinds,
                "existingECases": 0,
                "candidateUniversalKinds": candidate_universal,
            })

        partial_list = []
        for ep in sorted(partial):
            cands = cand_by_endpoint.get(ep, [])
            cases = existing_by_endpoint.get(ep, [])
            kinds = {}
            for c in cands:
                kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
            candidate_universal = [k for k in UNIVERSAL_KINDS if kinds.get(k, 0) > 0]
            partial_list.append({
                "endpoint": ep,
                "candidateCount": len(cands),
                "candidatesByKind": kinds,
                "existingECases": len(cases),
                "existingCaseIds": [c["id"] for c in cases],
                "candidateUniversalKinds": candidate_universal,
            })

        result["noECoverage"] = gaps
        result["withECoverage"] = partial_list

    return result


def main():
    ap = argparse.ArgumentParser(description="E 系列候选与实际用例缺口分析")
    ap.add_argument("--prefix", default="", help="只看该路径前缀")
    ap.add_argument("--covered-only", action="store_true", help="只看已有用例的接口")
    ap.add_argument("--detail", action="store_true", help="输出详细缺口（每接口每类）")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    args = ap.parse_args()

    try:
        candidates = load_candidates(prefix=args.prefix, covered_only=args.covered_only)
    except ValueError as exc:
        ap.error(str(exc))
    existing_by_endpoint = load_existing_e_cases()
    result = analyze_gap(candidates, existing_by_endpoint, detail=args.detail)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return

    s = result["summary"]
    print(f"E 系列候选覆盖分析（通用异常/安全候选 vs 已有 E 用例）")
    print(f"  总候选数: {s['totalCandidates']}")
    print(f"  涉及接口: {s['totalEndpoints']}")
    print(f"  已有 E 用例的接口: {s['endpointsWithECases']} ({round(s['endpointsWithECases']/max(s['totalEndpoints'],1)*100,1)}%)")
    print(f"  完全无 E 用例的接口: {s['endpointsNoECases']}")
    print(f"\n候选按 kind 分布（需与已有断言逐项核验，不代表当前缺失）:")
    for kind, cnt in sorted(s["candidatesByKind"].items(), key=lambda x: -x[1])[:10]:
        print(f"  {kind:25} {cnt:3} 条")

    if args.detail:
        if result.get("withECoverage"):
            print(f"\n已有 E 用例的接口（通用候选需与现有断言逐项核验，前 10）:")
            for item in result["withECoverage"][:10]:
                candidates_to_review = item.get("candidateUniversalKinds", [])
                review_hint = f" → 待核验候选: {', '.join(candidates_to_review[:3])}" if candidates_to_review else ""
                print(f"\n● {item['endpoint']}")
                print(f"    已有 {item['existingECases']} 条 E 用例 ({', '.join(item['existingCaseIds'])})")
                print(f"    候选 {item['candidateCount']} 条{review_hint}")
                for kind in candidates_to_review[:5]:
                    cnt = item["candidatesByKind"].get(kind, 0)
                    print(f"      - [{kind}] {cnt} 条")
            if len(result["withECoverage"]) > 10:
                print(f"\n... 还有 {len(result['withECoverage']) - 10} 个接口有 E 用例")

        if result.get("noECoverage"):
            print(f"\n完全无 E 用例的接口（优先补通用候选，前 10）:")
            for item in result["noECoverage"][:10]:
                univ = item.get("candidateUniversalKinds", [])
                print(f"\n● {item['endpoint']} → 候选 {item['candidateCount']} 条，实际 0 条")
                print(f"    可优先评估的候选: {', '.join(univ[:5])}")
            if len(result["noECoverage"]) > 10:
                print(f"\n... 还有 {len(result['noECoverage']) - 10} 个接口无 E 覆盖")


if __name__ == "__main__":
    main()
