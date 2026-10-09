# -*- coding: utf-8 -*-
"""Pack-discovered case registry: suite aggregation, execution, and navigation."""
import importlib
import os
from contextlib import ExitStack
import importlib.util
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from pack_registry import case_module_name, case_source_files, load_catalog_extensions, load_packs  # noqa: E402
from execution_guard import ExecutionBusy, resource_guard  # noqa: E402
from execution_jobs import JobCancelled, JobTimedOut, checkpoint  # noqa: E402

PACKS = load_packs()
MODULE_ORDER_HINT = [name for pack in PACKS for name in pack["module_order_hint"]]
NAV_TREE = [node for pack in PACKS for node in pack["navigation"]]
COMMON_ROOT = ROOT / "common"
if COMMON_ROOT.is_dir() and str(COMMON_ROOT) not in sys.path:
    sys.path.insert(0, str(COMMON_ROOT))

from case_result_status import normalize_case_result, summarize_cases  # noqa: E402
from security_helpers import guard_unscoped_write, guard_write_case  # noqa: E402


def discover_case_module_names():
    """Scan the active packs' case roots and apply their optional ordering hints."""
    found = [case_module_name(path) for path in case_source_files(PACKS)]
    duplicates = sorted({name for name in found if found.count(name) > 1})
    if duplicates:
        raise RuntimeError("多个 pack 提供同名 case module: " + ", ".join(duplicates))
    rank = {name: i for i, name in enumerate(MODULE_ORDER_HINT)}
    return sorted(found, key=lambda n: (rank.get(n, rank.get(n.rsplit(".", 1)[-1], len(rank))), n))


# ---------------- pack navigation ----------------
# 节点字段：id/name 必填；三选一：
#   suites: [suite_id]   显式挂载（优先声明，先到先得）
#   parents: [parent]    收编 suite.parent 匹配且未被 suites 显式认领的组
#   children: [node]     嵌套子节点
# 未被任何节点收录的 suite 由面板渲染在导航顶层，不会丢失。


def _collect_explicit_suites(nodes, claimed):
    """第一遍：登记所有显式 suites 声明（无论节点顺序，先于 parents 认领）。"""
    for node in nodes:
        for sid in node.get("suites") or []:
            claimed.add(sid)
        if node.get("children"):
            _collect_explicit_suites(node["children"], claimed)


def _resolve_node(node, suites, claimed):
    """把声明式节点解析成面板 GROUPS 结构（叶子含 suites 列表）。"""
    out = {"id": node["id"], "name": node["name"]}
    if node.get("children"):
        out["children"] = [_resolve_node(ch, suites, claimed) for ch in node["children"]]
        return out
    if node.get("suites"):
        out["suites"] = [s["id"] for s in suites if s["id"] in set(node["suites"])]
        return out
    parents = set(node.get("parents") or [])
    matched = [s["id"] for s in suites
               if s.get("parent") in parents and s["id"] not in claimed]
    claimed.update(matched)
    out["suites"] = matched
    return out


def _build_groups(suites):
    claimed = set()
    _collect_explicit_suites(NAV_TREE, claimed)
    return [_resolve_node(node, suites, claimed) for node in NAV_TREE]


def unclaimed_suites(suites=None, groups=None):
    """未被导航树收录的 suite id（面板会渲染在顶层；lint 用于告警）。"""
    suites = suites if suites is not None else SUITES
    groups = groups if groups is not None else GROUPS

    def walk(nodes, acc):
        for n in nodes:
            acc.update(n.get("suites") or [])
            walk(n.get("children") or [], acc)
        return acc

    grouped = walk(groups, set())
    return [s["id"] for s in suites if s["id"] not in grouped]


def _assign_modules(modules):
    """重建聚合索引（reload 后调用）。"""
    global MODULES, SUITES, GROUPS, _ALL, _CAT, _EXEC
    suites = [s for m in modules for s in m.SUITES]
    cases = [c for m in modules for c in m.CATALOG]
    for label, ids in (("suite", [s["id"] for s in suites]),
                       ("case", [c["id"] for c in cases]),
                       ("runner case", [cid for mod in modules for cid in mod.CAT])):
        duplicates = sorted(key for key, count in Counter(ids).items() if count > 1)
        if duplicates:
            raise RuntimeError(f"CATALOG {label} id 重复: " + ", ".join(duplicates))
    # Build completely before publishing so a rejected reload preserves indexes.
    groups = _build_groups(suites)
    executors = {cid: mod for mod in modules for cid in mod.CAT}
    MODULES, SUITES, GROUPS, _ALL = modules, suites, groups, cases
    _CAT, _EXEC = {c["id"]: c for c in cases}, executors


def _load_case_modules(reload_modules=False):
    loaded = []
    sources = {case_module_name(path): path.resolve() for path in case_source_files(PACKS)}
    for name in discover_case_module_names():
        existing = sys.modules.get(name)
        origin = getattr(existing, "__file__", None) if existing else None
        if existing is None:
            spec = importlib.util.find_spec(name)
            origin = spec.origin if spec else None
        if not origin or Path(origin).resolve() != sources[name]:
            raise RuntimeError(f"case module {name} 的加载来源不属于当前 Pack")
        if reload_modules and name in sys.modules:
            loaded.append(importlib.reload(sys.modules[name]))
        else:
            loaded.append(sys.modules[name] if name in sys.modules else importlib.import_module(name))
    return loaded


# 首次 import
_assign_modules(_load_case_modules(reload_modules=False))
CATALOG_EXTENSIONS = load_catalog_extensions(PACKS)
_RELOAD_FAILED = False


def _call_catalog_extensions(method, *args, **kwargs):
    """Call an optional pack catalog hook without naming pack-owned behavior."""
    results = []
    for pack_id, extension in CATALOG_EXTENSIONS:
        callback = getattr(extension, method, None)
        if callable(callback):
            results.append((pack_id, callback(*args, **kwargs)))
    return results


def catalog_section(section_id, force_refresh=False):
    """Return a pack-owned lazy catalog section, if any selected pack provides it."""
    for _pack_id, payload in _call_catalog_extensions(
        "catalog_section", section_id, force_refresh=force_refresh,
    ):
        if payload is not None:
            return payload
    return {}


def reload_registry():
    """热重载所有 *_cases.py 并重建目录（改文件后无需重启 server）。"""
    global _RELOAD_FAILED
    try:
        modules = _load_case_modules(reload_modules=True)
        _assign_modules(modules)
        _call_catalog_extensions("after_reload")
    except Exception:
        # importlib.reload mutates module objects even if index validation fails.
        # Block execution until a successful reload instead of using stale indexes.
        _RELOAD_FAILED = True
        raise
    _RELOAD_FAILED = False
    return {"suites": len(SUITES), "cases": len(_ALL), "reloaded": True}


def _case_row(c):
    keys = (
        "id", "suite", "group", "desc", "mode", "payload", "expected", "liveRisk",
        "requires_unscoped_write_opt_in", "requires_write_opt_in", "write_risk_reason",
        "regressionNote", "regressionExcluded",
    )
    row = {k: c[k] for k in keys if k in c}
    if "payload" not in row:
        row["payload"] = c.get("payload_kw") or {}
    return row


def _case_write_guard(case):
    if case.get("requires_unscoped_write_opt_in"):
        return guard_unscoped_write(case["id"])
    if case.get("requires_write_opt_in"):
        return guard_write_case(
            case["id"], case.get("write_risk_reason") or "该用例可能产生持久化副作用",
        )
    return None


def catalog(lite=False):
    data = {
        "groups": GROUPS,
        "suites": SUITES,
        "cases": [_case_row(c) for c in _ALL],
        "lite": bool(lite),
    }
    for _pack_id, enriched in _call_catalog_extensions("enrich_catalog", data, lite=bool(lite)):
        if enriched is not None:
            data = enriched
    return data


def _ensure_case_identities(case, mod):
    """Core validates explicit fixture membership; Pack owns login prerequisites."""
    from identity_pool import get_identity

    for required in case.get("fixture_roles") or []:
        if isinstance(required, dict):
            get_identity(str(required.get("category") or ""),
                         str(required.get("role") or "unknown"), required=True)


def exec_case(case_id, execution_options=None):
    checkpoint()
    if _RELOAD_FAILED:
        raise RuntimeError("目录重载失败，已停止案例执行；请修复源码并重新加载目录")
    mod = _EXEC.get(case_id)
    if not mod:
        return {"id": case_id, "error": "未知用例"}
    case = next((c for c in _ALL if c["id"] == case_id), None)
    if case and case.get("regressionExcluded"):
        note = str(case.get("regressionNote") or "本轮回归范围排除")
        return normalize_case_result({
            "id": case_id, "ok": True,
            "subs": [{"label": case_id, "ok": True, "status": "skipped", "detail": note}],
        })
    if case:
        guarded = _case_write_guard(case)
        if guarded:
            return normalize_case_result({"id": case_id, "ok": True, "subs": [guarded]})
    policy=PACKS[0].get('execution_policy',{})
    if case_id in policy.get('isolated_case_ids',[]) and os.environ.get('PLATFORM_ISOLATED_WORKER')!='1':
        if not case or case.get('mode')!='safe' or case.get('requires_write_opt_in') or case.get('requires_unscoped_write_opt_in'):
            raise ValueError('hard process isolation is only allowed for explicitly readonly SAFE cases')
        from isolated_runner import run_isolated
        return normalize_case_result(run_isolated(case_id,execution_options,policy.get('case_timeout_seconds',30),pack_id=PACKS[0]['id']))
    t0 = time.monotonic()
    try:
        with ExitStack() as contexts:
            policies = [value for _pack_id, value in _call_catalog_extensions(
                "execution_resources", case, execution_options or {}) if value is not None]
            if not policies and case and (case.get("mode") == "live" or case.get("requires_write_opt_in") or case.get("requires_unscoped_write_opt_in")):
                policies = [{"exclusive": ["pack:"+pack["id"]+":write" for pack in PACKS]}]
            contexts.enter_context(resource_guard(
                exclusive=sorted({key for value in policies for key in value.get("exclusive", [])}),
                shared=sorted({key for value in policies for key in value.get("shared", [])}),
            ))
            for _pack_id, extension in CATALOG_EXTENSIONS:
                context = getattr(extension, "case_context", None)
                if callable(context):
                    contexts.enter_context(context(case, mod, execution_options or {}))
            # Pack hooks may resolve account-pool fixtures. Keep those failures
            # inside the same incomplete/error classification as runner setup.
            _call_catalog_extensions("before_case", case, mod, execution_options or {})
            if case:
                _ensure_case_identities(case, mod)
            if case and case.get("fixture_scenario"):
                from identity_pool import ensure_scenario_ready

                ensure_scenario_ready(case["fixture_scenario"])
            result = mod.exec_case(case_id)
    except Exception as exc:
        from identity_pool import IdentityUnavailable

        if isinstance(exc, (JobCancelled, JobTimedOut)):
            raise
        if isinstance(exc, (IdentityUnavailable, ExecutionBusy)):
            return normalize_case_result({
                "id": case_id,
                "ok": None,
                "status": "incomplete",
                "subs": [{
                    "label": "执行隔离" if isinstance(exc, ExecutionBusy) else "账号池",
                    "ok": None,
                    "status": "incomplete",
                    "detail": str(exc),
                }],
                "durationMs": int((time.monotonic() - t0) * 1000),
            })
        return normalize_case_result({
            "id": case_id,
            "error": f"{type(exc).__name__}: {exc}",
            "durationMs": int((time.monotonic() - t0) * 1000),
        })
    if isinstance(result, dict) and "durationMs" not in result:
        result = dict(result)
        result["durationMs"] = int((time.monotonic() - t0) * 1000)
    normalized = normalize_case_result(result)
    normalized.setdefault("id", case_id)
    return normalized


def run_all(mode="safe", suite=None, execution_options=None):
    if _RELOAD_FAILED:
        raise RuntimeError("目录重载失败，已停止案例执行；请修复源码并重新加载目录")
    try:
        _call_catalog_extensions("before_suite", suite, execution_options or {})
    except Exception as exc:
        from identity_pool import IdentityUnavailable

        if not isinstance(exc, IdentityUnavailable):
            raise
        # Let individual cases report the missing pool prerequisite instead
        # of aborting the whole suite before a per-case result can be recorded.
    selected = [
        c for c in _ALL
        if (not suite or c.get("suite") == suite)
        and (mode != "safe" or c["mode"] == "safe")
    ]
    blocked = {}
    for case in selected:
        if case.get("regressionExcluded"):
            guarded = {
                "label": case["id"], "ok": True, "status": "skipped",
                "detail": str(case.get("regressionNote") or "本轮回归范围排除"),
            }
        else:
            guarded = _case_write_guard(case)
        if guarded:
            blocked[case["id"]] = guarded
    active_modules = {_EXEC[c["id"]] for c in selected if c["id"] not in blocked}
    for mod in MODULES:
        if mod not in active_modules:
            continue
        try:
            if hasattr(mod, "ensure_token"):
                mod.ensure_token()
            elif hasattr(mod, "ensure_tokens"):
                mod.ensure_tokens()
        except Exception as exc:
            from identity_pool import IdentityUnavailable

            if not isinstance(exc, IdentityUnavailable):
                raise
    out = []
    for c in selected:
        if c["id"] in blocked:
            out.append(normalize_case_result({
                "id": c["id"], "ok": True, "subs": [blocked[c["id"]]],
            }))
            continue
        out.append(exec_case(c["id"], execution_options=execution_options or {}))
    summary = summarize_cases(out)
    return {
        "mode": mode,
        "suite": suite,
        "cases": out,
        **summary,
    }
