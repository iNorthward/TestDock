# -*- coding: utf-8 -*-
"""把覆盖、候选评分卡和运行历史整理成洞察页可行动的分组。"""
from __future__ import annotations


SERIES_ORDER = ("R", "F", "D", "E")
ISSUE_STATUSES = {"failed", "skipped", "incomplete"}


def _endpoint_parts(endpoint: str) -> tuple[str, str]:
    method, _, path = str(endpoint or "").partition(" ")
    return (method, path) if path else ("", str(endpoint or ""))


def _case_reason(case: dict) -> str:
    details = []
    for sub in case.get("subs") or []:
        if not isinstance(sub, dict):
            continue
        status = sub.get("status")
        if status == "passed" or (status is None and sub.get("ok") is True):
            continue
        detail = str(sub.get("detail") or "").strip()
        label = str(sub.get("label") or "").strip()
        text = " · ".join(x for x in (label, detail) if x)
        if text and text not in details:
            details.append(text)
    if not details and case.get("error"):
        details.append(str(case["error"]).strip())
    return "；".join(details[:3]) or "执行明细未记录，请打开该次回归查看。"


def build_action_sections(coverage: dict, scorecard: dict, runs: list[dict],
                          run_details: dict[int, dict], *, limits: dict | None = None) -> dict:
    """Build bounded, display-safe action groups from existing toolchain outputs."""
    limits = limits or {}
    coverage_limit = limits.get("coverage", 12)
    candidate_limit = limits.get("candidate_per_series", 5)
    recent_limit = limits.get("recent", 12)

    paths = coverage.get("paths") if isinstance(coverage, dict) else []
    paths = paths if isinstance(paths, list) else []
    def module_for_path(path: dict) -> str:
        name = str(path.get("apifoxModule") or "").strip()
        if name:
            return name
        modules = path.get("modules")
        if isinstance(modules, list):
            names = [str(item).strip() for item in modules if str(item).strip()]
            if names:
                return "、".join(names)
        return str(path.get("domain") or "未归类")

    module_by_endpoint = {}
    for path in paths:
        if isinstance(path, dict) and path.get("method") and path.get("path"):
            endpoint = f"{str(path['method']).upper()} {path['path']}"
            module_by_endpoint[endpoint] = module_for_path(path)
    uncovered = [
        {
            "method": str(path.get("method") or ""),
            "path": str(path.get("path") or ""),
            "summaryLabel": str(path.get("summaryLabel") or path.get("summary") or ""),
            "apifoxModule": str(path.get("apifoxModule") or ""),
            "module": module_for_path(path),
        }
        for path in paths
        if isinstance(path, dict) and not path.get("covered")
    ]

    candidate_groups = []
    expected_endpoints = realized_endpoints = 0
    for series in SERIES_ORDER:
        summary = scorecard.get(series) or {}
        expected_endpoints += int(summary.get("expectedEndpoints") or 0)
        realized_endpoints += int(summary.get("realizedEndpoints") or 0)
        gaps = []
        for endpoint, count in (summary.get("topGaps") or [])[:candidate_limit]:
            method, path = _endpoint_parts(endpoint)
            gaps.append({
                "series": series,
                "method": method,
                "path": path,
                "endpoint": str(endpoint),
                "candidateCount": int(count or 0),
                "module": module_by_endpoint.get(str(endpoint), "未归类"),
            })
        if gaps or summary:
            candidate_groups.append({
                "series": series,
                "expectedEndpoints": int(summary.get("expectedEndpoints") or 0),
                "realizedEndpoints": int(summary.get("realizedEndpoints") or 0),
                "gapCount": len(summary.get("topGaps") or []),
                "items": gaps,
            })

    recent_items = []
    runs_with_issues = 0
    for run in sorted(runs, key=lambda item: (int(item.get("ts") or 0), int(item.get("id") or 0)), reverse=True):
        if run.get("status") not in ISSUE_STATUSES and not any(
            int(run.get(key) or 0) for key in ("failed", "skipped", "incomplete")
        ):
            continue
        runs_with_issues += 1
        run_id = int(run.get("id") or 0)
        detail = run_details.get(run_id) or {}
        cases = detail.get("cases") if isinstance(detail, dict) else []
        issue_cases = [
            case for case in (cases or [])
            if isinstance(case, dict) and case.get("status") in ISSUE_STATUSES
        ]
        for case in issue_cases:
            status = case.get("status")
            reason_type = {
                "failed": "真失败",
                "skipped": "已跳过（查看原因）",
                "incomplete": "未完成（检查 fixture / 门禁 / 环境原因）",
            }[status]
            recent_items.append({
                "runId": run_id,
                "ts": int(run.get("ts") or 0),
                "source": str(run.get("source") or ""),
                "suite": str(run.get("suite") or ""),
                "caseId": str(case.get("id") or ""),
                "status": status,
                "reasonType": reason_type,
                "reason": _case_reason(case),
            })
            if len(recent_items) >= recent_limit:
                break
        if len(recent_items) >= recent_limit:
            break
        if not issue_cases:
            recent_items.append({
                "runId": run_id,
                "ts": int(run.get("ts") or 0),
                "source": str(run.get("source") or ""),
                "suite": str(run.get("suite") or ""),
                "caseId": "",
                "status": str(run.get("status") or "incomplete"),
                "reasonType": "回归未收敛",
                "reason": (f"失败 {int(run.get('failed') or 0)} · 跳过 {int(run.get('skipped') or 0)} · "
                           f"未完成 {int(run.get('incomplete') or 0)}；打开回归历史查看详情。"),
            })
            if len(recent_items) >= recent_limit:
                break

    return {
        "coverage": {
            "generatedAt": str((coverage or {}).get("generatedAt") or ""),
            "uncoveredCount": len(uncovered),
            "items": uncovered[:coverage_limit],
        },
        "candidates": {
            "available": bool(scorecard),
            "expectedEndpoints": expected_endpoints,
            "realizedEndpoints": realized_endpoints,
            "gapCount": sum(group["gapCount"] for group in candidate_groups),
            "groups": candidate_groups,
        },
        "history": {"runsWithIssues": runs_with_issues, "items": recent_items},
    }
