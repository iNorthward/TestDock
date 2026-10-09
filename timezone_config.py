"""Project-wide timezone policy for PLATFORM local tools and services."""
from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


PROJECT_TIMEZONE = "Asia/Shanghai"
PROJECT_ROOT = Path(__file__).resolve().parent
ENV_FILE = PROJECT_ROOT / ".env.local"
PROJECT_TZ = ZoneInfo(PROJECT_TIMEZONE)


def parse_env_value(raw: str) -> str:
    """Read a plain dotenv value without evaluating shell expansions."""
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def configured_env_file(env_file: Path | str | None = None, *, default=None) -> Path:
    """Use the same environment-file selector for all platform entry points."""
    value = env_file if env_file is not None else os.environ.get("PLATFORM_ENV_FILE", "").strip()
    path = Path(value or default or ENV_FILE).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def configure_timezone(env_file: Path | str | None = None) -> ZoneInfo:
    """Apply the configured PLATFORM timezone to libc/Python local-time operations.

    The timezone is read without evaluating shell syntax. Asia/Shanghai remains
    the default for clean checkouts that do not have a local .env.local file.
    """
    path = configured_env_file(env_file)
    configured = None
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == "TZ":
                configured = parse_env_value(value)
    if configured and configured != PROJECT_TIMEZONE:
        raise ValueError(
            f"PLATFORM requires TZ={PROJECT_TIMEZONE}; found a different timezone in {path}"
        )

    os.environ["TZ"] = PROJECT_TIMEZONE
    if hasattr(time, "tzset"):
        time.tzset()
    return PROJECT_TZ


def now_project() -> datetime:
    """Return an aware timestamp in the configured project timezone."""
    configure_timezone()
    return datetime.now(PROJECT_TZ)


def now_project_iso(*, timespec="seconds") -> str:
    """Return an ISO 8601 project-local timestamp with its UTC offset."""
    return now_project().isoformat(timespec=timespec)
