"""Neutral project configuration and plain-text dotenv loading."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Mapping

from timezone_config import configured_env_file, configure_timezone, parse_env_value


PROJECT_ROOT = Path(__file__).resolve().parent
ENV_FILE = PROJECT_ROOT / ".env.local"
_PROJECT_DEFAULTS: dict[str, str] = {}
_PROJECT_ALIASES: dict[str, tuple[str, ...]] = {}


def load_project_env(env_file: Path | str | None = None) -> None:
    """Load dotenv values as plain text; never evaluate shell expressions."""
    path = configured_env_file(env_file, default=ENV_FILE)
    if path.is_file():
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key != "PLATFORM_ENV_FILE" and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                os.environ.setdefault(key, parse_env_value(value))
    configure_timezone(path)


def register_project_defaults(values: Mapping[str, str]) -> None:
    """Register defaults supplied by the selected project pack or adapter."""
    for key, value in values.items():
        _PROJECT_DEFAULTS.setdefault(str(key), str(value))


def register_project_aliases(values):
    """Compatibility names are supplied only by the selected Pack."""
    for key, aliases in values.items():
        _PROJECT_ALIASES[key] = tuple(aliases)


def project_setting(key: str, *, aliases=(), default=None):
    """Read a neutral project key, then legacy aliases, then pack defaults."""
    aliases = tuple(aliases) + _PROJECT_ALIASES.get(key, ())
    empty_neutral = key in os.environ
    for candidate in (key, *aliases):
        value = os.environ.get(candidate)
        if value is not None and str(value).strip():
            return value
    if key in _PROJECT_DEFAULTS:
        return _PROJECT_DEFAULTS[key]
    if empty_neutral:
        return os.environ[key]
    return default


def project_path(key: str, *, aliases=(), default=None) -> Path | None:
    """Resolve a configured project path relative to the repository root.

    Blank environment values fall through to pack defaults (same as project_setting),
    so a copied `.env.example` with empty KEY= slots does not disable pack paths.
    """
    aliases = tuple(aliases) + _PROJECT_ALIASES.get(key, ())
    value = os.environ.get(key)
    if not str(value or "").strip():
        value = next((os.environ.get(alias) for alias in aliases
                      if str(os.environ.get(alias) or "").strip()), None)
    if not str(value or "").strip():
        value = _PROJECT_DEFAULTS.get(key)
    if not str(value or "").strip():
        value = default
    if not str(value or "").strip():
        return None
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def path_under_root(root: Path, *parts) -> Path:
    """Join project resource subpaths without escaping their configured root."""
    root = Path(root).resolve()
    components = [Path(str(part)) for part in parts]
    if any(part.is_absolute() for part in components):
        raise ValueError("项目资源子路径必须为相对路径")
    path = root.joinpath(*components).resolve()
    if path != root and root not in path.parents:
        raise ValueError("项目资源子路径必须位于配置的根目录内")
    return path


def panel_port(*, default=8801) -> int:
    fallback = str(default).strip() or "8801"
    value = str(project_setting("PLATFORM_PANEL_PORT", default=fallback) or "").strip() or fallback
    if not value.isascii() or not value.isdecimal() or not 1 <= len(value) <= 5:
        raise ValueError("PLATFORM_PANEL_PORT 必须是 1 到 65535 的十进制端口")
    port = int(value)
    if not 1 <= port <= 65535:
        raise ValueError("PLATFORM_PANEL_PORT 必须是 1 到 65535 的十进制端口")
    return port
