# -*- coding: utf-8 -*-
"""Platform-neutral database connection factory.

新增 *_cases.py / *_query.py / *_ops.py 一律 `from db_conn import db`，
禁止再各自硬编码 host/端口，也禁止旁路再写一套 pymysql.connect。

连接目标与凭据来自配置；旧凭据键保持兼容：
  PLATFORM_DB_HOST / PLATFORM_DB_PORT / PLATFORM_DB_NAME / PLATFORM_DB_USER / PLATFORM_DB_PASSWORD
  aliases: DB_USER / DB_PWD
可选超时（秒）：
  PLATFORM_DB_CONNECT_TIMEOUT（默认 5）
  PLATFORM_DB_READ_TIMEOUT（默认 30）
  PLATFORM_DB_WRITE_TIMEOUT（默认 30）
"""
import os

try:
    import pymysql
except ImportError:
    pymysql = None

from platform_config import load_project_env, project_setting

load_project_env()


def _first_nonempty_setting(key, *, aliases=(), default=None):
    for candidate in (key, *aliases):
        value = os.environ.get(candidate)
        if str(value or "").strip():
            return value
    return project_setting(key, default=default)


def _int_setting(key, default, *, aliases=()):
    value = _first_nonempty_setting(key, aliases=aliases, default=default)
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} 必须是整数") from exc


def _db_settings():
    return {
        "host": str(_first_nonempty_setting("PLATFORM_DB_HOST", default="") or "").strip(),
        "port": _int_setting("PLATFORM_DB_PORT", 3306),
        "name": str(_first_nonempty_setting("PLATFORM_DB_NAME", aliases=("DB_NAME",), default="") or "").strip(),
        "user": str(_first_nonempty_setting("PLATFORM_DB_USER", aliases=("DB_USER",), default="") or ""),
        "password": str(_first_nonempty_setting("PLATFORM_DB_PASSWORD", aliases=("DB_PWD",), default="") or ""),
        "connect_timeout": _int_setting("PLATFORM_DB_CONNECT_TIMEOUT", 5),
        "read_timeout": _int_setting("PLATFORM_DB_READ_TIMEOUT", 30, aliases=("PLATFORM_SCHEMA_READ_TIMEOUT",)),
        "write_timeout": _int_setting("PLATFORM_DB_WRITE_TIMEOUT", 30),
    }


# Compatibility snapshots for existing consumers; db() resolves settings at call time.
_INITIAL_SETTINGS = _db_settings()
DB_HOST = _INITIAL_SETTINGS["host"]
DB_PORT = _INITIAL_SETTINGS["port"]
DB_NAME = _INITIAL_SETTINGS["name"]
DB_CONNECT_TIMEOUT = _INITIAL_SETTINGS["connect_timeout"]
DB_READ_TIMEOUT = _INITIAL_SETTINGS["read_timeout"]
DB_WRITE_TIMEOUT = _INITIAL_SETTINGS["write_timeout"]


def db(database=None, *, connect_timeout=None, read_timeout=None, write_timeout=None):
    """返回 DictCursor + autocommit 的 pymysql 连接。"""
    settings = _db_settings()
    if not settings["host"]:
        raise RuntimeError("缺少 PLATFORM_DB_HOST；请先配置 .env.local 中的数据库连接参数")
    missing = []
    if not settings["user"]:
        missing.append("PLATFORM_DB_USER（兼容 DB_USER）")
    if not settings["password"]:
        missing.append("PLATFORM_DB_PASSWORD（兼容 DB_PWD）")
    database = str(database or settings["name"] or "").strip()
    if not database:
        missing.append("PLATFORM_DB_NAME")
    if missing:
        raise RuntimeError("缺少数据库配置：" + "、".join(missing))
    if pymysql is None:
        raise RuntimeError("[incomplete] 数据库连接功能需要安装 pymysql")
    return pymysql.connect(
        host=settings["host"],
        port=settings["port"],
        user=settings["user"],
        password=settings["password"],
        database=database,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
        init_command="SET time_zone = '+08:00'",
        connect_timeout=max(1, int(connect_timeout or settings["connect_timeout"])),
        read_timeout=max(1, int(read_timeout or settings["read_timeout"])),
        write_timeout=max(1, int(write_timeout or settings["write_timeout"])),
    )
