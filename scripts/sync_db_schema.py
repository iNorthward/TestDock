#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据库结构同步：对比数据库和本地缓存，更新schema缓存文件"""
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

from atomic_file import atomic_write_text

try:
    import pymysql
except ImportError:
    pymysql = None

MYSQL_OPERATIONAL_ERROR = pymysql.err.OperationalError if pymysql else ()

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from project_inputs import init_project_pack  # noqa: E402

init_project_pack(REPO)

from platform_config import project_path, project_setting  # noqa: E402
sys.path.insert(0, str(REPO / "test-platform" / "common"))
from db_conn import db as open_db  # noqa: E402

SCHEMA_CACHE_FILE = project_path("PLATFORM_SCHEMA_JSON")
_RETRYABLE_MYSQL = (2006, 2013)  # server gone away / lost connection during query


def connect_db():
    """Schema 巡检专用连接：单连接批量读 information_schema，超时比业务查询更长。"""
    connect_timeout = int(project_setting("PLATFORM_DB_CONNECT_TIMEOUT", default="15") or 15)
    schema_read_timeout = project_setting("PLATFORM_SCHEMA_READ_TIMEOUT", default="")
    read_timeout = int(schema_read_timeout or project_setting("PLATFORM_DB_READ_TIMEOUT", default="120") or 120)
    write_timeout = int(project_setting("PLATFORM_DB_WRITE_TIMEOUT", default="60") or 60)
    database = project_setting("PLATFORM_DB_NAME", aliases=("DB_NAME",), default="")
    return open_db(
        database=database,
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        write_timeout=write_timeout,
    )


def _require_schema_cache_file() -> Path:
    if SCHEMA_CACHE_FILE is None:
        raise RuntimeError("未配置 PLATFORM_SCHEMA_JSON；请由所选 pack 提供 schema 路径或显式设置环境变量")
    return Path(SCHEMA_CACHE_FILE)


def _schema_cache_label() -> str:
    cache_file = _require_schema_cache_file()
    try:
        return str(cache_file.relative_to(REPO))
    except ValueError:
        return str(cache_file)


def _fetchall(conn, sql, args=None, retries=3):
    last_err = None
    for attempt in range(retries):
        try:
            with conn.cursor() as cur:
                cur.execute(sql, args or ())
                return cur.fetchall()
        except MYSQL_OPERATIONAL_ERROR as e:
            last_err = e
            code = e.args[0] if e.args else None
            if code not in _RETRYABLE_MYSQL or attempt >= retries - 1:
                raise
            time.sleep(0.4 * (attempt + 1))
            try:
                conn.ping(reconnect=True)
            except Exception:
                raise last_err
    raise last_err


def _strip_table_name(row):
    out = dict(row)
    out.pop("TABLE_NAME", None)
    return out


def _assemble_schema(db_name, tables, columns, stats, fks, progress_callback=None):
    cols_by_table = defaultdict(list)
    for row in columns:
        cols_by_table[row["TABLE_NAME"]].append(_strip_table_name(row))

    idx_parts = defaultdict(list)
    for row in stats:
        key = (row["TABLE_NAME"], row["INDEX_NAME"], row["NON_UNIQUE"])
        idx_parts[key].append((row["SEQ_IN_INDEX"], row["COLUMN_NAME"]))
    idx_by_table = defaultdict(list)
    for (tname, iname, non_unique), parts in idx_parts.items():
        cols = ",".join(name for _, name in sorted(parts, key=lambda x: x[0]))
        idx_by_table[tname].append({
            "INDEX_NAME": iname,
            "NON_UNIQUE": non_unique,
            "COLS": cols,
        })

    fks_by_table = defaultdict(list)
    for row in fks:
        fks_by_table[row["TABLE_NAME"]].append(_strip_table_name(row))

    results = []
    total = len(tables)
    for i, t in enumerate(tables, 1):
        tname = t["TABLE_NAME"]
        results.append({
            "name": tname,
            "engine": t["ENGINE"],
            "rows_est": t["TABLE_ROWS"],
            "comment": t["TABLE_COMMENT"] or "",
            "columns": cols_by_table.get(tname, []),
            "indexes": idx_by_table.get(tname, []),
            "fks": fks_by_table.get(tname, []),
        })
        if progress_callback:
            progress_callback(i, total, tname)
    return {"database": db_name, "tables": results}


def fetch_current_schema(conn=None, max_workers=8):
    """获取数据库当前的表结构。max_workers 保留兼容，实际为单连接 4 次批量查询。"""
    return fetch_current_schema_with_progress(conn, None, max_workers)


def _fetch_schema_bulk(conn, db_name, progress_callback=None):
    if progress_callback:
        progress_callback(0, 1, "读取表清单")
    tables = _fetchall(
        conn,
        """SELECT TABLE_NAME, ENGINE, TABLE_ROWS, TABLE_COMMENT
           FROM information_schema.TABLES
           WHERE TABLE_SCHEMA=%s ORDER BY TABLE_NAME""",
        (db_name,),
    )
    total = max(len(tables), 1)
    if progress_callback:
        progress_callback(0, total, "读取列定义")
    columns = _fetchall(
        conn,
        """SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_KEY,
                  COLUMN_DEFAULT, EXTRA, COLUMN_COMMENT
           FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA=%s
           ORDER BY TABLE_NAME, ORDINAL_POSITION""",
        (db_name,),
    )
    if progress_callback:
        progress_callback(0, total, "读取索引")
    stats = _fetchall(
        conn,
        """SELECT TABLE_NAME, INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME
           FROM information_schema.STATISTICS
           WHERE TABLE_SCHEMA=%s
           ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX""",
        (db_name,),
    )
    if progress_callback:
        progress_callback(0, total, "读取外键")
    fks = _fetchall(
        conn,
        """SELECT TABLE_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME
           FROM information_schema.KEY_COLUMN_USAGE
           WHERE TABLE_SCHEMA=%s AND REFERENCED_TABLE_NAME IS NOT NULL""",
        (db_name,),
    )
    return _assemble_schema(db_name, tables, columns, stats, fks, progress_callback)


def load_cached_schema():
    """加载并验证本地 schema 缓存；损坏缓存不能伪装成首次同步。"""
    cache_file = _require_schema_cache_file()
    if not cache_file.exists():
        return None
    try:
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError(f"读取 schema 缓存失败 {cache_file}: {exc}") from exc

    if not isinstance(cached, dict):
        raise ValueError(f"schema 缓存根节点必须是对象: {cache_file}")
    if not isinstance(cached.get("database"), str) or not cached["database"].strip():
        raise ValueError(f"schema 缓存缺少有效 database 字段: {cache_file}")
    tables = cached.get("tables")
    if not isinstance(tables, list):
        raise ValueError(f"schema 缓存 tables 必须是数组: {cache_file}")

    seen_tables = set()
    for index, table in enumerate(tables):
        if not isinstance(table, dict):
            raise ValueError(f"schema 缓存 tables[{index}] 必须是对象: {cache_file}")
        name = table.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"schema 缓存 tables[{index}] 缺少有效 name: {cache_file}")
        if name in seen_tables:
            raise ValueError(f"schema 缓存包含重复表名 {name!r}: {cache_file}")
        seen_tables.add(name)
        for key in ("columns", "indexes", "fks"):
            values = table.get(key)
            if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
                raise ValueError(
                    f"schema 缓存 tables[{index}].{key} 必须是对象数组: {cache_file}"
                )
    return cached


def _col_fingerprint(col):
    """列结构指纹，用于检测类型/约束变更。"""
    return {
        "type": col.get("COLUMN_TYPE", ""),
        "nullable": col.get("IS_NULLABLE", ""),
        "default": col.get("COLUMN_DEFAULT"),
        "extra": col.get("EXTRA", ""),
        "key": col.get("COLUMN_KEY", ""),
    }


def compare_schemas(current, cached):
    """对比当前 schema 与本地缓存，返回增删表及列级变更。"""
    if cached is None:
        return {
            "new_tables": sorted(t["name"] for t in current["tables"]),
            "removed_tables": [],
            "modified_tables": [],
            "unchanged_tables": [],
            "index_changes": [], "fk_changes": [],
        }

    current_database = current.get("database") if isinstance(current, dict) else None
    cached_database = cached.get("database") if isinstance(cached, dict) else None
    if current_database and cached_database and current_database != cached_database:
        raise ValueError(
            f"当前数据库 {current_database!r} 与 schema 缓存数据库 {cached_database!r} 不一致；"
            "拒绝生成跨数据库差异或覆盖缓存"
        )

    current_table_names = {t["name"] for t in current["tables"]}
    cached_table_names = {t["name"] for t in cached["tables"]}

    new_tables = sorted(current_table_names - cached_table_names)
    removed_tables = sorted(cached_table_names - current_table_names)
    common_tables = current_table_names & cached_table_names

    current_table_map = {t["name"]: t for t in current["tables"]}
    cached_table_map = {t["name"]: t for t in cached["tables"]}

    modified_tables = []
    unchanged_tables = []
    index_changes = []
    fk_changes = []

    def row_keys(table, field, keys):
        return {tuple(row.get(k) for k in keys) for row in (table.get(field) or [])}

    for tname in sorted(common_tables):
        curr_t = current_table_map[tname]
        cached_t = cached_table_map[tname]

        curr_col_map = {c["COLUMN_NAME"]: c for c in curr_t["columns"]}
        cached_col_map = {c["COLUMN_NAME"]: c for c in cached_t["columns"]}
        curr_cols = set(curr_col_map)
        cached_cols = set(cached_col_map)

        added_columns = sorted(curr_cols - cached_cols)
        removed_columns = sorted(cached_cols - curr_cols)
        changed_columns = []
        for cname in sorted(curr_cols & cached_cols):
            before = _col_fingerprint(cached_col_map[cname])
            after = _col_fingerprint(curr_col_map[cname])
            if before != after:
                index_only = all(before[k] == after[k] for k in ("type", "nullable", "default", "extra"))
                changed_columns.append({"name": cname, "before": before, "after": after, "indexOnly": index_only})

        idxkeys = ("INDEX_NAME", "NON_UNIQUE", "COLS")
        idx_add = row_keys(curr_t, "indexes", idxkeys) - row_keys(cached_t, "indexes", idxkeys)
        idx_del = row_keys(cached_t, "indexes", idxkeys) - row_keys(curr_t, "indexes", idxkeys)
        fkkeys = ("COLUMN_NAME", "REFERENCED_TABLE_NAME", "REFERENCED_COLUMN_NAME")
        fk_add = row_keys(curr_t, "fks", fkkeys) - row_keys(cached_t, "fks", fkkeys)
        fk_del = row_keys(cached_t, "fks", fkkeys) - row_keys(curr_t, "fks", fkkeys)
        if idx_add or idx_del:
            index_changes.append({"table": tname, "added": sorted(idx_add, key=str), "removed": sorted(idx_del, key=str)})
        if fk_add or fk_del:
            fk_changes.append({"table": tname, "added": sorted(fk_add, key=str), "removed": sorted(fk_del, key=str)})

        if added_columns or removed_columns or changed_columns:
            modified_tables.append({
                "name": tname,
                "added_columns": added_columns,
                "removed_columns": removed_columns,
                "changed_columns": changed_columns,
            })
        else:
            unchanged_tables.append(tname)

    return {
        "new_tables": new_tables,
        "removed_tables": removed_tables,
        "modified_tables": modified_tables,
        "unchanged_tables": sorted(unchanged_tables),
        "index_changes": index_changes,
        "fk_changes": fk_changes,
    }


def _schema_diff_has_changes(diff):
    return bool(diff.get("new_tables") or diff.get("removed_tables") or diff.get("modified_tables") or diff.get("index_changes") or diff.get("fk_changes"))


def _build_patrol_result(current, cached):
    diff = compare_schemas(current, cached)
    return {
        "ok": True,
        "database": current["database"],
        "total_tables": len(current["tables"]),
        "cached_tables": len(cached["tables"]) if cached else 0,
        "cache_exists": cached is not None,
        "cache_file": _schema_cache_label(),
        "diff": diff,
        "has_changes": _schema_diff_has_changes(diff),
    }


def patrol_schema_with_progress(conn=None, progress_callback=None, max_workers=16):
    """读取线上表结构并与本地缓存对比（不更新缓存）。"""
    del max_workers
    _require_schema_cache_file()
    current = fetch_current_schema_with_progress(conn, progress_callback)
    cached = load_cached_schema()
    return _build_patrol_result(current, cached)


def sync_schema_with_progress(conn=None, progress_callback=None, max_workers=16):
    """同步数据库schema到本地缓存（带进度回调）

    Args:
        conn: 数据库连接
        progress_callback: 进度回调函数 callback(current, total, table_name)
        max_workers: 并行线程数（兼容参数，已忽略）

    Returns:
        dict: 同步结果
    """
    del max_workers
    cache_file = _require_schema_cache_file()
    current = fetch_current_schema_with_progress(conn, progress_callback)
    cached = load_cached_schema()
    if cached is not None and cached["database"] != current["database"]:
        raise ValueError(
            f"当前数据库 {current['database']!r} 与 schema 缓存数据库 "
            f"{cached['database']!r} 不一致；拒绝覆盖缓存"
        )

    cache_file.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        cache_file,
        json.dumps(current, ensure_ascii=False, indent=2, default=str),
    )

    return {
        "ok": True,
        "database": current["database"],
        "total_tables": len(current["tables"]),
        "cache_file": _schema_cache_label(),
        "updated": True,
    }


def fetch_current_schema_with_progress(conn=None, progress_callback=None, max_workers=16):
    """获取数据库当前的表结构（带进度）。max_workers 保留兼容，不再并行开连接。"""
    del max_workers  # 避免 16 路并发打 information_schema 导致 2013 断连
    db_name = str(project_setting("PLATFORM_DB_NAME", aliases=("DB_NAME",), default="") or "").strip()
    if not db_name:
        raise RuntimeError("缺少 PLATFORM_DB_NAME；请由所选 pack 提供数据库名或显式设置环境变量")
    should_close = False
    work_conn = conn
    if work_conn is None:
        work_conn = connect_db()
        should_close = True
    try:
        return _fetch_schema_bulk(work_conn, db_name, progress_callback)
    except MYSQL_OPERATIONAL_ERROR as e:
        code = e.args[0] if e.args else None
        if code not in _RETRYABLE_MYSQL:
            raise
        if should_close:
            try:
                work_conn.close()
            except Exception:
                pass
        work_conn = connect_db()
        should_close = True
        return _fetch_schema_bulk(work_conn, db_name, progress_callback)
    finally:
        if should_close:
            work_conn.close()


def sync_schema(conn=None, dry_run=False, json_output=False):
    """同步数据库schema到本地缓存

    Args:
        conn: 数据库连接
        dry_run: 只对比不更新
        json_output: 输出JSON格式

    Returns:
        dict: 同步结果
    """
    cache_file = _require_schema_cache_file()
    should_close = False
    if conn is None:
        conn = connect_db()
        should_close = True

    try:
        # 获取当前schema
        current = fetch_current_schema(conn)

        # 加载缓存
        cached = load_cached_schema()

        # 对比差异
        diff = compare_schemas(current, cached)

        result = {
            "ok": True,
            "database": current["database"],
            "total_tables": len(current["tables"]),
            "cache_exists": cached is not None,
            "cache_file": _schema_cache_label(),
            "diff": diff,
            "updated": False,
        }

        # 如果不是dry_run，则更新缓存
        if not dry_run:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(
                cache_file,
                json.dumps(current, ensure_ascii=False, indent=2, default=str),
            )
            result["updated"] = True

        if json_output:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"\n数据库Schema同步")
            print(f"=" * 60)
            print(f"数据库: {current['database']}")
            print(f"当前表数: {len(current['tables'])}")
            print(f"缓存文件: {_schema_cache_label()}")
            print()

            if not cached:
                print("⚠️  本地无缓存，首次同步")
            else:
                print(f"✅ 新增表: {len(diff['new_tables'])}")
                if diff["new_tables"]:
                    for t in diff["new_tables"]:
                        print(f"   + {t}")

                print(f"❌ 删除表: {len(diff['removed_tables'])}")
                if diff["removed_tables"]:
                    for t in diff["removed_tables"]:
                        print(f"   - {t}")

                print(f"📝 修改表: {len(diff['modified_tables'])}")
                for t in diff["modified_tables"]:
                    print(f"   ~ {t['name']}")
                    if t["added_columns"]:
                        print(f"     + 列: {', '.join(t['added_columns'])}")
                    if t["removed_columns"]:
                        print(f"     - 列: {', '.join(t['removed_columns'])}")
                    for ch in t.get("changed_columns") or []:
                        print(f"     ~ 列 {ch['name']}: {ch['before']['type']} → {ch['after']['type']}")

                print(f"⚪ 未变化: {len(diff['unchanged_tables'])} 张表")

            if not dry_run:
                print(f"\n✅ 已更新缓存文件")
            else:
                print(f"\n💡 预览模式，未更新缓存（去掉 --dry-run 以更新）")

        return result

    finally:
        if should_close:
            conn.close()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="数据库Schema同步")
    parser.add_argument("--dry-run", action="store_true", help="只对比不更新")
    parser.add_argument("--json", action="store_true", help="输出JSON格式")

    args = parser.parse_args()

    try:
        result = sync_schema(dry_run=args.dry_run, json_output=args.json)
        sys.exit(0 if result["ok"] else 1)
    except Exception as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        else:
            print(f"同步失败: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
