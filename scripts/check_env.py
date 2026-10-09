#!/usr/bin/env python3
"""只读检查连接配置与所选 pack 的账号池完整度，不读取配置值。"""
from __future__ import annotations

import ast
import json
import os
import sys
from pathlib import Path
from typing import Mapping


REPO_ROOT = Path(__file__).resolve().parents[1]
_PANEL_GROUP = {
    "id": "panel",
    "name": "打开面板所需",
    "required": [],
    "missing": [],
    "ready": True,
    "blocking": False,
    "note": "账号池为空时面板仍可打开；依赖身份的用例会标记未完成。",
}
_DB_REQUIREMENTS = (
    ("PLATFORM_DB_HOST", ()),
    ("PLATFORM_DB_USER", ("DB_USER",)),
    ("PLATFORM_DB_PASSWORD", ("DB_PWD",)),
)
_APIFOX_REQUIREMENTS = (("APIFOX_ACCESS_TOKEN", ("APIFOX_TOKEN",)),)
_WEBHOOK_NAMES = ("PLATFORM_WEBHOOK_WECHAT", "PLATFORM_WEBHOOK_DINGTALK")


def _has_value(env: Mapping[str, object], name: str, aliases=()) -> bool:
    return any(str(env.get(key) or "").strip() for key in (name, *aliases))


def _missing(env: Mapping[str, object], requirements) -> list[str]:
    return [name for name, aliases in requirements if not _has_value(env, name, aliases)]


def _literal_suite_ids(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(target, ast.Name) and target.id == "SUITES" for target in targets):
            continue
        value = node.value
        if not isinstance(value, (ast.List, ast.Tuple)):
            continue
        for item in value.elts:
            if not isinstance(item, ast.Dict):
                continue
            row = {
                key.value: val.value
                for key, val in zip(item.keys, item.values)
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
                and isinstance(val, ast.Constant) and isinstance(val.value, str)
            }
            if row.get("id"):
                found.append(row["id"])
    return found


def _case_files(root: Path, case_dirs=None) -> list[Path]:
    if case_dirs is None:
        if root.resolve() != REPO_ROOT:
            return []
        platform_dir = root / "test-platform"
        if str(platform_dir) not in sys.path:
            sys.path.insert(0, str(platform_dir))
        from pack_registry import load_packs

        case_dirs = [path for pack in load_packs() for path in pack.get("case_dirs") or []]
    paths = []
    for directory in case_dirs:
        path = Path(directory)
        if not path.is_absolute():
            path = root / path
        if path.is_dir():
            paths.extend(path.glob("*_cases.py"))
    return sorted(set(paths))


def _pool_status(root: Path) -> dict:
    if root.resolve() != REPO_ROOT:
        return {}
    platform_dir = root / "test-platform"
    if str(platform_dir) not in sys.path:
        sys.path.insert(0, str(platform_dir))
    from identity_pool import panel_snapshot

    categories = panel_snapshot().get("categories") or {}
    return {
        category: {
            "count": int((categories.get(category) or {}).get("count") or 0),
            "missing": list((categories.get(category) or {}).get("missing") or []),
        }
        for category in categories
    }


def _optional_section(env: Mapping[str, object], name: str, names, *, any_is_enough=False) -> dict:
    configured = [key for key in names if _has_value(env, key)]
    missing = [] if any_is_enough and configured else [key for key in names if key not in configured]
    if not configured:
        state = "not_configured"
    elif missing:
        state = "incomplete"
    else:
        state = "ready"
    return {
        "name": name,
        "variables": list(names),
        "missing": missing,
        "state": state,
        "blocking": False,
        "any_is_enough": any_is_enough,
    }


def _pack_optional_checks(root):
    if root.resolve() != REPO_ROOT:
        return []
    sys.path.insert(0, str(root / "test-platform"))
    from pack_registry import load_packs

    return [check for pack in load_packs() for check in pack.get("optional_environment_checks", [])]


def build_preflight_report(env: Mapping[str, object] | None = None, *, root: Path | str = REPO_ROOT,
                           case_dirs=None) -> dict:
    """Return connection key names and pool role names; never return their values."""
    current_env = os.environ if env is None else env
    root = Path(root).resolve()
    db_missing = _missing(current_env, _DB_REQUIREMENTS)
    apifox_missing = _missing(current_env, _APIFOX_REQUIREMENTS)
    optional_sections = [
        _optional_section(current_env, check["name"], check["variables"],
                          any_is_enough=check.get("any_is_enough", False))
        for check in _pack_optional_checks(root)
    ]
    optional_sections.append(_optional_section(current_env, "回归通知", _WEBHOOK_NAMES, any_is_enough=True))
    pool = _pool_status(root)
    pool_missing = [f"{category}:{role}" for category, detail in pool.items() for role in detail["missing"]]
    groups = [
        dict(_PANEL_GROUP),
        {
            "id": "identity-pool",
            "name": "项目资源声明",
            "required": [],
            "missing": pool_missing,
            "ready": not pool_missing,
            "blocking": False,
            "categories": pool,
            "note": "业务身份只从当前 pack 本机账号池读取；缺项对应的用例会标记未完成。",
        },
        {
            "id": "database",
            "name": "跑数据库对账所需",
            "required": [name for name, _aliases in _DB_REQUIREMENTS],
            "missing": db_missing,
            "ready": not db_missing,
            "blocking": False,
            "note": "缺少连接项只影响数据库对账。",
        },
        {
            "id": "apifox",
            "name": "拉取 Apifox 所需",
            "required": [name for name, _aliases in _APIFOX_REQUIREMENTS],
            "missing": apifox_missing,
            "ready": not apifox_missing,
            "blocking": False,
            "note": "缺少 token 可跳过在线拉取。",
        },
        {
            "id": "optional",
            "name": "项目可选能力 / 通知",
            "required": [],
            "missing": sorted({name for section in optional_sections for name in section["missing"]}),
            "ready": all(section["state"] == "ready" for section in optional_sections),
            "blocking": False,
            "sections": optional_sections,
            "note": "只检查所选 Pack 声明的可选能力；webhook 留空时不发送通知。",
        },
    ]
    modules = []
    warnings = []
    try:
        for path in _case_files(root, case_dirs):
            modules.append({
                "module": path.stem,
                "suite_ids": _literal_suite_ids(path),
                "sample_env_vars": [],
                "missing": [],
            })
    except (OSError, SyntaxError, UnicodeError, RuntimeError) as exc:
        warnings.append(f"用例目录状态暂不可用：{type(exc).__name__}")
    return {
        "read_only": True,
        "overall_ready": True,
        "summary": "面板基础配置可用",
        "groups": groups,
        "modules": modules,
        "warnings": warnings,
    }


def render_preflight_report(report: dict) -> str:
    """Render a terminal summary containing names only, never configuration values."""
    lines = ["PLATFORM 环境预检（只读；不会登录、连库或拉取 Apifox）"]
    for group in report.get("groups", []):
        lines.append(f"\n[{group.get('name', '')}]")
        missing = group.get("missing") or []
        lines.append("缺少配置或池角色：" + "、".join(missing) if missing else "无缺少项")
        if group.get("note"):
            lines.append(str(group["note"]))
        for section in group.get("sections") or []:
            lines.append(f"- {section['name']}：")
            if section["state"] == "not_configured":
                choice = "可任选其一：" if section.get("any_is_enough") else "需配置："
                lines.append("  未配置（可留空）；如需此功能，" + choice + "、".join(section["missing"]))
            elif section["missing"]:
                lines.append("  配置不完整，缺少：" + "、".join(section["missing"]))
            else:
                lines.append("  已配置")
    lines.append("\n[按模块列出的用例组]")
    for module in report.get("modules") or []:
        suites = "、".join(module.get("suite_ids") or []) or "无 suite 声明"
        lines.append(f"- {module['module']}: {suites}")
    for warning in report.get("warnings") or []:
        lines.append("提示：" + str(warning))
    lines.append("\n面板基础配置：可启动")
    return "\n".join(lines)


def main() -> int:
    root = REPO_ROOT
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from platform_config import load_project_env

    load_project_env()
    print(render_preflight_report(build_preflight_report()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
