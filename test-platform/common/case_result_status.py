# -*- coding: utf-8 -*-
"""Normalize case results so skipped or incomplete assertions never count as passes."""
from __future__ import annotations

from collections import Counter


CASE_STATUSES = frozenset({"passed", "failed", "skipped", "incomplete"})
_INFRASTRUCTURE_GAPS = (
    "未配置", "未部署", "不支持", "未就绪", "不可达", "未注册", "表不存在",
    "超时", "网络错误", "网络异常", "连接失败",
)


def _sub_status(sub):
    if not isinstance(sub, dict):
        return "failed"
    explicit = sub.get("status")
    if isinstance(explicit, str) and explicit in CASE_STATUSES:
        if explicit == "passed" and sub.get("ok") is not True:
            return "failed"
        if explicit in ("skipped", "incomplete") and sub.get("ok") is False:
            return "failed"
        return explicit
    if sub.get("ok") is not True:
        return "failed"
    detail = sub.get("detail")
    if not isinstance(detail, str):
        detail = ""
    if "跳过" in detail:
        return "incomplete" if any(term in detail for term in _INFRASTRUCTURE_GAPS) else "skipped"
    return "passed"


def normalize_case_result(case_result):
    """Return a defensive, status-aware copy of one runner result.

    A passing case must have an explicit successful top-level result and at least
    one successful assertion. Legacy assertion details that say they were skipped
    are classified here until each runner adopts explicit statuses.
    """
    if not isinstance(case_result, dict):
        return {
            "id": "?", "ok": False, "status": "failed",
            "error": "runner returned a non-object result",
            "subs": [{"label": "用例结果", "ok": False, "status": "failed",
                      "detail": "runner 返回了非对象结果"}],
        }

    result = dict(case_result)
    raw_subs = result.get("subs")
    if not isinstance(raw_subs, list) or not raw_subs:
        explicit = result.get("status")
        if not result.get("error") and explicit in ("skipped", "incomplete") and result.get("ok") is not False:
            result["status"] = explicit
            result["ok"] = None
            result["subs"] = [] if not isinstance(raw_subs, list) else raw_subs
            return result
        detail = "未返回子断言" if not isinstance(raw_subs, list) else "子断言为空"
        result["status"] = "failed"
        result["ok"] = False
        result["subs"] = [{"label": "用例结果", "ok": False, "status": "failed", "detail": detail}]
        return result

    subs = []
    statuses = []
    for raw_sub in raw_subs:
        if not isinstance(raw_sub, dict):
            sub = {"label": "子断言", "ok": False, "status": "failed",
                   "detail": "子断言格式错误"}
        else:
            sub = dict(raw_sub)
            sub_status = _sub_status(sub)
            sub["status"] = sub_status
            # Keep legacy consumers from interpreting a skipped assertion as a pass.
            if sub_status in ("skipped", "incomplete"):
                sub["ok"] = None
            elif sub_status == "failed":
                sub["ok"] = False
            else:
                sub["ok"] = True
        subs.append(sub)
        statuses.append(sub["status"])

    result["subs"] = subs
    explicit_case_status = result.get("status")
    if result.get("error") or explicit_case_status == "failed" or result.get("ok") is False or "failed" in statuses:
        status = "failed"
    elif result.get("ok") is not True and explicit_case_status not in ("skipped", "incomplete"):
        status = "failed"
    elif explicit_case_status == "incomplete":
        status = "incomplete"
    elif "incomplete" in statuses:
        status = "incomplete"
    elif "skipped" in statuses:
        status = "skipped" if all(s == "skipped" for s in statuses) else "incomplete"
    else:
        status = "incomplete" if explicit_case_status == "skipped" else "passed"
    result["status"] = status
    result["ok"] = True if status == "passed" else False if status == "failed" else None
    return result


def summarize_cases(cases):
    """Return mutually exclusive case counts and an overall run status."""
    statuses = [normalize_case_result(case).get("status", "failed") for case in cases]
    counts = Counter(statuses)
    total = len(statuses)
    if counts["failed"]:
        overall = "failed"
    elif not total or counts["skipped"] or counts["incomplete"]:
        overall = "incomplete"
    else:
        overall = "passed"
    return {
        "status": overall,
        "passed": counts["passed"],
        "failed": counts["failed"],
        "skipped": counts["skipped"],
        "incomplete": counts["incomplete"],
        "total": total,
    }


def summarize_run(mode, suite, cases):
    normalized = [normalize_case_result(case) for case in cases]
    return {"mode": mode, "suite": suite, "cases": normalized, **summarize_cases(normalized)}


def format_sub_status(sub):
    status = sub.get("status") if isinstance(sub, dict) else None
    return {
        "passed": "PASS", "failed": "FAIL", "skipped": "SKIP", "incomplete": "INCOMPLETE",
    }.get(status, "PASS" if isinstance(sub, dict) and sub.get("ok") is True else "FAIL")


def format_run_summary(result):
    return (
        f"通过 {result.get('passed', 0)}/{result.get('total', 0)}，"
        f"失败 {result.get('failed', 0)}，跳过 {result.get('skipped', 0)}，"
        f"未完成 {result.get('incomplete', 0)}"
    )


def run_exit_code(result):
    return {"passed": 0, "failed": 1, "skipped": 2, "incomplete": 2}.get(
        result.get("status"), 1,
    )
