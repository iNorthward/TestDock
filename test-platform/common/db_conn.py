# -*- coding: utf-8 -*-
"""Platform-neutral database connection factory.

新增 *_cases.py / *_query.py / *_ops.py 一律 `from db_conn import db`，
禁止再各自硬编码 host/端口，也禁止旁路再写一套 pymysql.connect。

连接目标与凭据来自所选 Pack 配置；别名仅由 Pack 显式声明：
  PLATFORM_DB_HOST / PLATFORM_DB_PORT / PLATFORM_DB_NAME / PLATFORM_DB_USER / PLATFORM_DB_PASSWORD
可选超时（秒）：
  PLATFORM_DB_CONNECT_TIMEOUT（默认 5）
  PLATFORM_DB_READ_TIMEOUT（默认 30）
  PLATFORM_DB_WRITE_TIMEOUT（默认 30）
"""
import os
import atexit
import hashlib
import json
import threading

try:
    from sqlalchemy import create_pool_from_url, event
    from sqlalchemy.exc import TimeoutError as _PoolTimeout
except ImportError:
    create_pool_from_url = event = None
    _PoolTimeout = None

try:
    import pymysql
except ImportError:
    pymysql = None

from platform_config import load_project_env, project_setting
from timezone_config import mysql_session_time_zone

load_project_env()

# PyMySQL 1.2.0 未公开 COM_RESET_CONNECTION；协议常量为 0x1F（MySQL >= 5.7.3 / MariaDB >= 10.2.4）。
_COM_RESET_CONNECTION = 0x1F


def _session_time_zone_sql():
    """init_command 与借出恢复共用的唯一时区语句来源。"""
    return "SET time_zone = '%s'" % mysql_session_time_zone()


class DatabasePoolExhausted(RuntimeError, *((_PoolTimeout,) if _PoolTimeout else ())):
    """池满等待超时：属于基础设施未就绪，案例应记为 incomplete 而非 failed。

    同时继承 SQLAlchemy TimeoutError，兼容已有 except TimeoutError 的调用方。
    """
    case_status = "incomplete"
    case_label = "数据库连接池"


class DatabaseDependencyUnavailable(RuntimeError):
    """Missing optional database runtime; never a business assertion failure."""
    case_status = "incomplete"
    case_label = "数据库依赖"


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
        "name": str(_first_nonempty_setting("PLATFORM_DB_NAME", default="") or "").strip(),
        "user": str(_first_nonempty_setting("PLATFORM_DB_USER", default="") or ""),
        "password": str(_first_nonempty_setting("PLATFORM_DB_PASSWORD", default="") or ""),
        "connect_timeout": _int_setting("PLATFORM_DB_CONNECT_TIMEOUT", 5),
        "read_timeout": _int_setting("PLATFORM_DB_READ_TIMEOUT", 30),
        "write_timeout": _int_setting("PLATFORM_DB_WRITE_TIMEOUT", 30),
    }



def db(database=None, *, connect_timeout=None, read_timeout=None, write_timeout=None):
    """返回独占连接租约；close 归还池，关闭池模式时返回原生短连接。"""
    settings = _db_settings()
    if not settings["host"]:
        raise RuntimeError("缺少 PLATFORM_DB_HOST；请先配置 .env.local 中的数据库连接参数")
    missing = []
    if not settings["user"]:
        missing.append("PLATFORM_DB_USER")
    if not settings["password"]:
        missing.append("PLATFORM_DB_PASSWORD")
    database = str(database or settings["name"] or "").strip()
    if not database:
        missing.append("PLATFORM_DB_NAME")
    if missing:
        raise RuntimeError("缺少数据库配置：" + "、".join(missing))
    if pymysql is None:
        raise DatabaseDependencyUnavailable("[incomplete] 数据库连接功能需要安装 pymysql")
    kwargs = dict(
        host=settings["host"],
        port=settings["port"],
        user=settings["user"],
        password=settings["password"],
        database=database,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
        init_command=_session_time_zone_sql(),
        connect_timeout=max(1, int(connect_timeout or settings["connect_timeout"])),
        read_timeout=max(1, int(read_timeout or settings["read_timeout"])),
        write_timeout=max(1, int(write_timeout or settings["write_timeout"])),
    )

    enabled = _int_setting("PLATFORM_DB_POOL_ENABLED", 1)
    if enabled not in (0, 1):
        raise ValueError("PLATFORM_DB_POOL_ENABLED 必须为 0 或 1")
    if not enabled:
        close_pools()
        return pymysql.connect(**kwargs)
    if create_pool_from_url is None:
        raise DatabaseDependencyUnavailable("[incomplete] 数据库连接池需要安装 SQLAlchemy；或设置 PLATFORM_DB_POOL_ENABLED=0 使用短连接")
    options = _pool_options()
    profile = (database, kwargs["connect_timeout"], kwargs["read_timeout"], kwargs["write_timeout"])
    fingerprint = hashlib.sha256(json.dumps([_kwargs_signature(kwargs), options], sort_keys=True).encode()).digest()
    with _POOL_LOCK:
        state = _POOLS.get(profile)
        if state is None or state.fingerprint != fingerprint:
            if state is None and len(_POOLS) >= 16:
                raise RuntimeError("数据库连接配置超过 16 组；请统一超时配置或清理连接池")
            replacement = _PoolState(kwargs, options, fingerprint)
            if state is not None:
                state.retire()
            state = _POOLS[profile] = replacement
    try:
        handle = state.pool.connect()
    except _PoolTimeout as exc:
        raise DatabasePoolExhausted(
            "[incomplete] 数据库连接池已满：等待 %s 秒仍无空闲连接（PLATFORM_DB_POOL_SIZE=%s，"
            "PLATFORM_DB_POOL_MAX_OVERFLOW=%s）；本次未取得连接，此前操作需另行核验"
            % (options["timeout"], options["pool_size"], options["max_overflow"])
        ) from exc
    return _Connection(state, handle)


def _kwargs_signature(kwargs):
    return {key: value for key, value in kwargs.items() if key != "cursorclass"}


def _pool_options():
    values = {
        "pool_size": _int_setting("PLATFORM_DB_POOL_SIZE", 5),
        "max_overflow": _int_setting("PLATFORM_DB_POOL_MAX_OVERFLOW", 5),
        "timeout": _int_setting("PLATFORM_DB_POOL_TIMEOUT", 5),
        "recycle": _int_setting("PLATFORM_DB_POOL_RECYCLE", 1800),
    }
    if values["pool_size"] < 1 or values["max_overflow"] < 0 or values["timeout"] < 1 or values["recycle"] < 1:
        raise ValueError("连接池 size/timeout/recycle 必须为正整数，max_overflow 必须为非负整数")
    if values["pool_size"] + values["max_overflow"] > 100:
        raise ValueError("每组数据库连接上限不得超过 100")
    return values


_POOL_LOCK = threading.RLock()
_POOLS = {}


class _PoolState:
    def __init__(self, kwargs, options, fingerprint):
        self.fingerprint = fingerprint
        self.retired = False
        self.lock = threading.Lock()
        self.pool = create_pool_from_url(
            "mysql+pymysql://", creator=lambda: pymysql.connect(**kwargs),
            pre_ping=True, reset_on_return="rollback", use_lifo=True, **options,
        )
        # 归还时由服务端整体清理会话（用户变量、临时表、GET_LOCK、会话变量、
        # 未提交事务、预处理语句）。任何失败都抛出，SQLAlchemy 会记录错误并
        # invalidate 该物理连接（fail-closed），绝不把脏会话交给下一个借用者。
        @event.listens_for(self.pool, "reset")
        def reset_session(connection, record, reset_state):
            if reset_state.terminate_only:
                return
            _reset_server_session(connection)

        # COM_RESET_CONNECTION 会把 autocommit/字符集/时区恢复为服务端默认，
        # 借出时统一恢复平台约定的会话默认值。
        @event.listens_for(self.pool, "checkout")
        def restore_session(connection, record, proxy):
            connection.autocommit(True)
            connection.select_db(kwargs["database"])
            connection.set_character_set("utf8mb4")
            with connection.cursor() as cursor:
                cursor.execute(_session_time_zone_sql())

    def retire(self):
        with self.lock:
            self.retired = True
            self.pool.dispose()


def _reset_server_session(connection):
    """发送 COM_RESET_CONNECTION；驱动不支持或服务端拒绝时抛出，由连接池淘汰该连接。"""
    execute = getattr(connection, "_execute_command", None)
    read_ok = getattr(connection, "_read_ok_packet", None)
    if not callable(execute) or not callable(read_ok):
        raise RuntimeError("当前 PyMySQL 不支持 COM_RESET_CONNECTION；淘汰该连接以免会话状态泄漏")
    execute(_COM_RESET_CONNECTION, "")
    read_ok()


class _Connection:
    """Exclusive DB-API lease; close returns it, without replaying statements."""
    def __init__(self, state, handle):
        self._state = state
        self._handle = handle
        self._pid = os.getpid()

    def _check(self):
        if self._pid != os.getpid():
            raise RuntimeError("数据库连接不得跨进程使用")
        if self._handle is None:
            raise RuntimeError("数据库连接已归还连接池")
        return self._handle

    def __getattr__(self, name):
        return getattr(self._check(), name)

    def cursor(self, *args, **kwargs):
        return _Cursor(self, self._check().cursor(*args, **kwargs))

    def __enter__(self):
        self._check()
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self._handle is None:
            return
        handle = self._check()
        self._handle = None
        with self._state.lock:
            if self._state.retired:
                handle.invalidate()
            else:
                handle.close()

    def __del__(self):
        # SQLAlchemy already provides GC return; this also honors retirement.
        if self._handle is not None and self._pid == os.getpid():
            try:
                self.close()
            except Exception:
                # Interpreter teardown may already have released pool state.
                pass

    def invalidate(self):
        handle = self._check()
        self._handle = None
        handle.invalidate()


class _Cursor:
    """Keep cursor operations inside the lifetime of their original lease."""
    def __init__(self, connection, cursor):
        object.__setattr__(self, "_connection", connection)
        object.__setattr__(self, "_cursor", cursor)

    @property
    def connection(self):
        self._connection._check()
        return self._connection

    def __setattr__(self, name, value):
        self._connection._check()
        setattr(self._cursor, name, value)

    def __getattr__(self, name):
        self._connection._check()
        value = getattr(self._cursor, name)
        if not callable(value):
            return value

        def checked_call(*args, **kwargs):
            self._connection._check()
            return value(*args, **kwargs)
        return checked_call

    def __enter__(self):
        self._connection._check()
        self._cursor.__enter__()
        return self

    def __exit__(self, *exc):
        self.close()

    def __iter__(self):
        self._connection._check()
        return self

    def __next__(self):
        self._connection._check()
        return next(self._cursor)

    def close(self):
        self._connection._check()
        self._cursor.close()


def close_pools():
    """Retire cached pools; checked-out leases close physically on return."""
    with _POOL_LOCK:
        for state in _POOLS.values():
            state.retire()
        _POOLS.clear()


def _after_fork():
    # Do not call dispose in the child: it must not send commands over parent
    # connections. Each child constructs a fresh registry and synchronization.
    global _POOL_LOCK, _POOLS
    _POOL_LOCK = threading.RLock()
    _POOLS = {}


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)
atexit.register(close_pools)
