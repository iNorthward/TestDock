"""Resolve optional project-pack knowledge, data and artifact roots for core code."""
from __future__ import annotations

from pathlib import Path

from pack_registry import load_packs
from platform_config import path_under_root


def _bootstrap() -> None:
    load_packs()


def project_path(key: str, *, aliases=(), required: bool = False) -> Path | None:
    _bootstrap()
    from platform_config import project_path as resolve

    result = resolve(key, aliases=aliases)
    if result is None and required:
        raise ValueError(f"[incomplete] 未配置 {key}；请在环境变量或所选 pack 中提供路径")
    return result


def project_setting(key: str, *, default=None):
    _bootstrap()
    from platform_config import project_setting as resolve

    return resolve(key, default=default)


def business_knowledge_path(*parts, required: bool = False) -> Path | None:
    root = project_path("PLATFORM_BIZ_KNOWLEDGE_ROOT", required=required)
    return path_under_root(root, *parts) if root else None


def business_knowledge_subpath(key: str, *, required: bool = False) -> Path | None:
    relative = project_setting(key, default="")
    if not str(relative or "").strip():
        if required:
            raise ValueError(f"[incomplete] 所选 pack 未配置 {key}")
        return None
    return business_knowledge_path(str(relative), required=required)


def pack_data_path(*parts, required: bool = False) -> Path | None:
    root = project_path("PLATFORM_PACK_DATA_DIR", required=required)
    return path_under_root(root, *parts) if root else None


def required_pack_data_path(*parts) -> Path:
    path = pack_data_path(*parts, required=True)
    assert path is not None
    return path


def artifact_path(*parts, required: bool = False) -> Path | None:
    root = project_path("PLATFORM_ARTIFACT_ROOT", required=required)
    return path_under_root(root, *parts) if root else None


def runtime_data_path(*parts) -> Path:
    """Core history defaults to the active pack, never another project's data."""
    configured = pack_data_path(*parts)
    if configured is not None:
        return configured
    packs = load_packs()
    if len(packs) != 1:
        raise ValueError("平台运行数据需要唯一的项目 Pack")
    return path_under_root(packs[0]["root"], "data", *parts)
