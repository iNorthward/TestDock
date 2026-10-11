"""连接池配套修复（G1/G3/G4/G5/G6）：无需安装 SQLAlchemy/PyMySQL 也能运行。"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "test-platform"), str(ROOT / "test-platform/common"), str(ROOT / "scripts")]
import catalog
import check_env
import db_conn
import timezone_config


class PoolExhaustedIsIncompleteTests(unittest.TestCase):
    def _run(self, error):
        module = Mock()
        module.exec_case.side_effect = error
        case = {"id": "db-case", "suite": "selected", "mode": "safe"}
        with patch.object(catalog, "_ALL", [case]), patch.object(catalog, "_EXEC", {"db-case": module}), \
             patch.object(catalog, "MODULES", [module]), patch.object(catalog, "CATALOG_EXTENSIONS", []):
            return catalog.exec_case("db-case"), catalog.run_all("safe", "selected")

    def test_pool_exhausted_is_incomplete_not_failed(self):
        direct, summary = self._run(db_conn.DatabasePoolExhausted("[incomplete] 数据库连接池已满"))
        self.assertEqual((direct["status"], direct["ok"]), ("incomplete", None))
        self.assertEqual(direct["subs"][0]["label"], "数据库连接池")
        self.assertNotIn("error", direct)
        self.assertEqual((summary["failed"], summary["status"]), (0, "incomplete"))

    def test_generic_infra_marker_is_incomplete(self):
        class SyntheticInfraError(RuntimeError):
            case_status = "incomplete"
        direct, _summary = self._run(SyntheticInfraError("synthetic"))
        self.assertEqual(direct["status"], "incomplete")
        self.assertEqual(direct["subs"][0]["label"], "基础设施")

    def test_ordinary_exception_is_still_failed(self):
        direct, summary = self._run(RuntimeError("synthetic business error"))
        self.assertEqual(direct["status"], "failed")
        self.assertEqual(summary["failed"], 1)

    def test_missing_database_dependency_is_incomplete(self):
        direct, summary = self._run(db_conn.DatabaseDependencyUnavailable("[incomplete] dependency missing"))
        self.assertEqual((direct['status'], direct['ok']), ('incomplete', None))
        self.assertEqual(summary['failed'], 0)

    def test_missing_driver_and_pool_raise_typed_dependency_error(self):
        env={'PLATFORM_DB_HOST':'synthetic.invalid','PLATFORM_DB_USER':'reader',
             'PLATFORM_DB_PASSWORD':'synthetic','PLATFORM_DB_NAME':'synthetic'}
        with patch.dict(db_conn.os.environ,env,clear=True):
            with patch.object(db_conn,'pymysql',None):
                with self.assertRaises(db_conn.DatabaseDependencyUnavailable):db_conn.db()
            with patch.object(db_conn,'pymysql',Mock()),patch.object(db_conn,'create_pool_from_url',None):
                with self.assertRaises(db_conn.DatabaseDependencyUnavailable):db_conn.db()


class SessionTimezoneSingleSourceTests(unittest.TestCase):
    def test_offset_derives_from_project_timezone(self):
        self.assertEqual(timezone_config.mysql_session_time_zone(), "+08:00")
        self.assertEqual(db_conn._session_time_zone_sql(), "SET time_zone = '+08:00'")

    def test_db_conn_has_no_hard_coded_offset(self):
        source = (ROOT / "test-platform/common/db_conn.py").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"[+-]\d\d:\d\d", source))
        self.assertIn("init_command=_session_time_zone_sql()", source)
        self.assertIn("cursor.execute(_session_time_zone_sql())", source)


class PreflightDependencyTests(unittest.TestCase):
    def _db_missing(self, env, available):
        with patch.object(check_env, "_package_available", side_effect=lambda module: module in available):
            report = check_env.build_preflight_report(env=env)
        return next(group for group in report["groups"] if group["id"] == "database")

    def test_missing_sqlalchemy_is_reported_by_name(self):
        group = self._db_missing({}, {"pymysql"})
        self.assertIn("python:SQLAlchemy", group["missing"])
        self.assertNotIn("python:PyMySQL", group["missing"])
        self.assertFalse(group["ready"])

    def test_sqlalchemy_not_required_when_pool_disabled(self):
        group = self._db_missing({"PLATFORM_DB_POOL_ENABLED": "0"}, {"pymysql"})
        self.assertNotIn("python:SQLAlchemy", group["missing"])
        group = self._db_missing({"PLATFORM_DB_POOL_ENABLED": 0}, {"pymysql"})
        self.assertNotIn("python:SQLAlchemy", group["missing"])

    def test_all_installed_reports_no_package_gap(self):
        group = self._db_missing({}, {"pymysql", "sqlalchemy"})
        self.assertFalse([name for name in group["missing"] if name.startswith("python:")])

    def test_requirements_pin_pool_dependencies(self):
        text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertRegex(text, r"(?mi)^pymysql==")
        self.assertRegex(text, r"(?mi)^sqlalchemy==")


class PoolGuideDistributionTests(unittest.TestCase):
    GUIDE = "docs/guides/数据库连接池.md"

    def test_guide_is_linked_and_exported(self):
        self.assertTrue((ROOT / self.GUIDE).is_file())
        for doc in ("README.md", "docs/architecture/TEST_PLATFORM_ARCHITECTURE.md"):
            with self.subTest(doc=doc):
                self.assertIn("数据库连接池.md", (ROOT / doc).read_text(encoding="utf-8"))
        self.assertIn("_tree_files(source, 'docs/guides')",
                      (ROOT / "scripts/export_platform.py").read_text(encoding="utf-8"))

    def test_guide_documents_server_reset(self):
        text = (ROOT / self.GUIDE).read_text(encoding="utf-8")
        self.assertIn("COM_RESET_CONNECTION", text)
        self.assertNotIn("rollback 不是完整会话清理", text)


if __name__ == "__main__":
    unittest.main()
