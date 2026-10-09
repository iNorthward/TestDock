# -*- coding: utf-8 -*-
"""只读取样统一入口 —— F/D 系列取一行样本。

用法::

    from db_sample import fetch_one, fetch_one_or_none, no_sample

    row = fetch_one(
        "SELECT id, title FROM example_items WHERE is_deleted=0 ORDER BY id DESC LIMIT 1",
    )
    # 无样本时 fetch_one 返回 (None, "库无样本: …")；fetch_one_or_none 只返回 row|None

SQL 与业务口径由调用方传入（软删/tenant/状态），本模块不拼全局 WHERE。
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence, Tuple, Union

from db_conn import db


def no_sample(case_id=None, label="库无样本"):
    """产出 runner 常用的跳过/失败条目结构（由用例决定 ok=False 还是跳过）。"""
    detail = label if not case_id else f"{label} ({case_id})"
    return {"label": case_id or label, "ok": False, "detail": detail}


def fetch_one(
    sql: str,
    params: Union[Sequence[Any], Mapping[str, Any], None] = None,
    *,
    conn=None,
    label="库无样本",
) -> Tuple[Optional[dict], Optional[str]]:
    """执行 SQL 取第一行。

    返回 (row, err)：
    - 有行 → (dict, None)
    - 无行 → (None, label)
    调用方传入的 conn 不会被关闭；未传则本函数自建并关闭。
    """
    own = conn is None
    connection = conn or db()
    try:
        with connection.cursor() as cur:
            cur.execute(sql, params or ())
            row = cur.fetchone()
        if not row:
            return None, label
        return row, None
    finally:
        if own:
            connection.close()


def fetch_one_or_none(sql: str, params=None, *, conn=None) -> Optional[dict]:
    row, _ = fetch_one(sql, params, conn=conn)
    return row


def fetch_all(
    sql: str,
    params=None,
    *,
    conn=None,
    limit=None,
) -> list:
    """执行 SQL 取多行。limit 在已有结果上截断（SQL 侧应自带 LIMIT）。"""
    own = conn is None
    connection = conn or db()
    try:
        with connection.cursor() as cur:
            cur.execute(sql, params or ())
            rows = list(cur.fetchall() or [])
        if limit is not None:
            rows = rows[: int(limit)]
        return rows
    finally:
        if own:
            connection.close()


def count_sql(sql: str, params=None, *, conn=None) -> int:
    """执行 COUNT 类 SQL，返回 int。第一列或 dict 首值。"""
    own = conn is None
    connection = conn or db()
    try:
        with connection.cursor() as cur:
            cur.execute(sql, params or ())
            row = cur.fetchone()
        if not row:
            return 0
        if isinstance(row, dict):
            return int(next(iter(row.values())) or 0)
        return int(row[0] or 0)
    finally:
        if own:
            connection.close()
