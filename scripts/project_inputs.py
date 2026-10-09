"""Shared bootstrap for CLI tools that consume selected project-pack inputs."""
from __future__ import annotations

import sys
from pathlib import Path


def init_project_pack(repo_root: Path | None = None):
    """Load dotenv and selected pack defaults without importing case modules."""
    root = Path(repo_root or Path(__file__).resolve().parents[1]).resolve()
    platform_dir = root / "test-platform"
    for entry in (root, platform_dir):
        if str(entry) not in sys.path:
            sys.path.insert(0, str(entry))
    from pack_registry import load_packs

    return load_packs()


def project_input_path(key: str, *, required: bool = True, aliases=()) -> Path | None:
    """Resolve a configured pack input path after registering selected-pack defaults."""
    init_project_pack()
    from platform_config import project_path

    path = project_path(key, aliases=aliases)
    if path is None and required:
        raise ValueError(
            f"[incomplete] 未配置 {key}；请在项目环境或所选 pack 中提供输入路径"
        )
    return path


def project_input_setting(key: str, *, aliases=(), default=None):
    """Read a configured project/pack value after selected-pack bootstrap."""
    init_project_pack()
    from platform_config import project_setting

    return project_setting(key, aliases=aliases, default=default)


def project_code_root(*, required: bool = False) -> Path | None:
    """Resolve the optional source root for tools that inspect the system under test."""
    return project_input_path("PLATFORM_BIZ_CODE_ROOT", required=required)


def _pack_path(root_key: str, parts, *, required: bool = False) -> Path | None:
    root = project_input_path(root_key, required=required)
    if root is None:
        return None
    from platform_config import path_under_root

    return path_under_root(root, *parts)


def business_knowledge_path(*parts, required: bool = False) -> Path | None:
    """Resolve a path under the selected pack's optional business knowledge root."""
    return _pack_path("PLATFORM_BIZ_KNOWLEDGE_ROOT", parts, required=required)


def business_knowledge_subpath(setting_key: str, *, required: bool = False) -> Path | None:
    """Resolve a pack-declared relative knowledge directory under its root."""
    relative = project_input_setting(setting_key, default="")
    if not str(relative or "").strip():
        if required:
            raise ValueError(f"[incomplete] 未配置 {setting_key}；所选 pack 未提供知识目录")
        return None
    return business_knowledge_path(str(relative), required=required)


def pack_data_path(*parts, required: bool = False) -> Path | None:
    """Resolve a path under the selected pack's generated/runtime data root."""
    return _pack_path("PLATFORM_PACK_DATA_DIR", parts, required=required)


def artifact_path(*parts, required: bool = False) -> Path | None:
    """Resolve a path under the selected pack's optional artifact root."""
    return _pack_path("PLATFORM_ARTIFACT_ROOT", parts, required=required)
