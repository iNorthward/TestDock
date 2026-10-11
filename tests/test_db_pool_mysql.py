"""真实 MySQL/MariaDB 会话隔离回归（可选）。

默认跳过；设置以下变量后对一个可丢弃的测试库运行（只建会话级临时表，不写业务表）：
  PLATFORM_TEST_MYSQL_HOST  PLATFORM_TEST_MYSQL_PORT(默认 3306)
  PLATFORM_TEST_MYSQL_USER  PLATFORM_TEST_MYSQL_PASSWORD  PLATFORM_TEST_MYSQL_DB
"""
from __future__ import annotations

import os
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "test-platform/common")]
import db_conn
import platform_config

_KEYS = ("HOST", "USER", "PASSWORD", "DB")
CONFIGURED = all(os.environ.get("PLATFORM_TEST_MYSQL_" + key) for key in _KEYS)
DEPS = db_conn.pymysql is not None and db_conn.create_pool_from_url is not None


@unittest.skipUnless(DEPS, "未安装 PyMySQL/SQLAlchemy")
@unittest.skipUnless(CONFIGURED, "未设置 PLATFORM_TEST_MYSQL_*，跳过真实数据库会话隔离测试")
class RealDatabaseSessionIsolationTests(unittest.TestCase):
    def setUp(self):
        env = {
            "PLATFORM_DB_HOST": os.environ["PLATFORM_TEST_MYSQL_HOST"],
            "PLATFORM_DB_PORT": os.environ.get("PLATFORM_TEST_MYSQL_PORT", "3306"),
            "PLATFORM_DB_USER": os.environ["PLATFORM_TEST_MYSQL_USER"],
            "PLATFORM_DB_PASSWORD": os.environ["PLATFORM_TEST_MYSQL_PASSWORD"],
            "PLATFORM_DB_NAME": os.environ["PLATFORM_TEST_MYSQL_DB"],
            "PLATFORM_DB_POOL_SIZE": "1", "PLATFORM_DB_POOL_MAX_OVERFLOW": "0", "PLATFORM_DB_POOL_TIMEOUT": "2",
        }
        db_conn.close_pools()
        self.addCleanup(db_conn.close_pools)
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, env, clear=True).start()
        patch.object(platform_config, "_PROJECT_DEFAULTS", {}).start()

    @staticmethod
    def _one(conn, sql):
        with conn.cursor() as cursor:
            cursor.execute(sql)
            return cursor.fetchone()

    def test_case_state_is_cleared_between_leases_on_same_physical_connection(self):
        with db_conn.db() as case_a:
            conn_id = self._one(case_a, "SELECT CONNECTION_ID() AS id")["id"]
            default_sql_mode = self._one(case_a, "SELECT @@GLOBAL.sql_mode AS m")["m"]
            lock_name = 'td_pool_case_' + uuid.uuid4().hex
            self._one(case_a, "SET @case_marker = 'case-A'")
            changed_sql_mode = '' if default_sql_mode else 'ANSI_QUOTES'
            self._one(case_a, f"SET SESSION sql_mode = '{changed_sql_mode}'")
            self._one(case_a, "SET time_zone = '+00:00'")
            self.assertEqual(self._one(case_a, f"SELECT GET_LOCK('{lock_name}', 0) AS got")["got"], 1)
            case_a.autocommit(False)
            self._one(case_a, "SET @tx_marker = 1")
        with db_conn.db() as case_b:
            self.assertEqual(self._one(case_b, "SELECT CONNECTION_ID() AS id")["id"], conn_id,
                             "pool_size=1 必须复用同一物理连接，才能证明是清理而非换连接")
            self.assertIsNone(self._one(case_b, "SELECT @case_marker AS v")["v"])
            self.assertIsNone(self._one(case_b, f"SELECT IS_USED_LOCK('{lock_name}') AS owner")["owner"])
            self.assertEqual(self._one(case_b, "SELECT @@SESSION.sql_mode AS m")["m"], default_sql_mode)
            self.assertEqual(self._one(case_b, "SELECT @@SESSION.time_zone AS tz")["tz"],
                             db_conn.mysql_session_time_zone())
            self.assertEqual(self._one(case_b, "SELECT @@SESSION.autocommit AS ac")["ac"], 1)
            self.assertEqual(self._one(case_b, "SELECT DATABASE() AS db")["db"], os.environ["PLATFORM_DB_NAME"])
            self.assertEqual(self._one(case_b, "SELECT @@SESSION.character_set_client AS cs")["cs"], "utf8mb4")

    def test_temporary_table_is_cleared_between_leases(self):
        with db_conn.db() as case_a:
            conn_id = self._one(case_a, "SELECT CONNECTION_ID() AS id")["id"]
            try:
                self._one(case_a, "CREATE TEMPORARY TABLE td_pool_case_tmp (id INT)")
            except db_conn.pymysql.err.OperationalError as exc:
                if exc.args and exc.args[0] in (1044, 1142):
                    self.skipTest("测试账号缺少 CREATE TEMPORARY TABLE 权限")
                raise
        with db_conn.db() as case_b:
            self.assertEqual(self._one(case_b, "SELECT CONNECTION_ID() AS id")["id"], conn_id)
            with self.assertRaises(db_conn.pymysql.err.ProgrammingError) as caught:
                self._one(case_b, "SELECT * FROM td_pool_case_tmp")
            self.assertEqual(caught.exception.args[0], 1146)


if __name__ == "__main__":
    unittest.main()
