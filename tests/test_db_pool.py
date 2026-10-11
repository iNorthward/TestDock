from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy.exc import TimeoutError
import pymysql

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "test-platform/common")]
import db_conn
import platform_config


class Cursor:
    def __init__(self, connection):
        self.connection = connection
    def __enter__(self): return self
    def __exit__(self, *args): self.close()
    def close(self): pass
    def execute(self, sql, params=None):
        self.connection.statements.append(sql)
        if self.connection.execute_error:
            raise self.connection.execute_error
    def fetchone(self): return {"value": 1}


class Connection:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.statements = []
        self.rollbacks = 0
        self.closed = False
        self.dead = False
        self.execute_error = None
        self.auto = kwargs["autocommit"]
        self.database = kwargs["database"]
    def cursor(self): return Cursor(self)
    def rollback(self):
        self.rollbacks += 1
        if self.dead: raise pymysql.OperationalError(2006, "synthetic disconnect")
    def close(self): self.closed = True
    def ping(self, reconnect=False):
        if reconnect: raise AssertionError("pool must not let driver reconnect implicitly")
        if self.dead: raise pymysql.OperationalError(2006, "synthetic disconnect")
    def autocommit(self, value): self.auto = value
    def select_db(self, value): self.database = value
    def set_charset(self, value): self.charset = value
    def set_character_set(self, value): self.charset = value
    def character_set_name(self): return "utf8mb4"


class DatabasePoolTests(unittest.TestCase):
    def setUp(self):
        db_conn.close_pools()
        self.addCleanup(db_conn.close_pools)
        self.connections = []
        self.lock = threading.Lock()
        self.env = {"PLATFORM_DB_HOST": "synthetic-db.invalid", "PLATFORM_DB_NAME": "synthetic_one",
                    "PLATFORM_DB_USER": "synthetic-user", "PLATFORM_DB_PASSWORD": "synthetic-password",
                    "PLATFORM_DB_POOL_SIZE": "1", "PLATFORM_DB_POOL_MAX_OVERFLOW": "0", "PLATFORM_DB_POOL_TIMEOUT": "1"}
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, self.env, clear=True).start()
        patch.object(platform_config, "_PROJECT_DEFAULTS", {}).start()
        patch.object(db_conn.pymysql, "connect", side_effect=self.create).start()
    def create(self, **kwargs):
        connection = Connection(**kwargs)
        with self.lock: self.connections.append(connection)
        return connection
    def test_close_reuses_physical_connection_and_is_idempotent(self):
        a = db_conn.db(); a.close(); a.close()
        with db_conn.db() as b:
            self.assertEqual(b.cursor().fetchone(), {"value": 1})
        self.assertEqual(len(self.connections), 1)
        self.assertGreaterEqual(self.connections[0].rollbacks, 2)
        self.assertFalse(self.connections[0].closed)
        with self.assertRaisesRegex(RuntimeError, "归还"): a.cursor()
    def test_full_pool_has_bounded_wait_and_recovers(self):
        a = db_conn.db()
        started = time.monotonic()
        with self.assertRaises(TimeoutError): db_conn.db()
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.9)
        self.assertLess(elapsed, 3)
        a.close()
        db_conn.db().close()
        self.assertEqual(len(self.connections), 1)

    def test_returned_lease_cursor_cannot_touch_next_borrower(self):
        a = db_conn.db()
        cursor = a.cursor()
        execute = cursor.execute
        a.close()
        with db_conn.db() as b:
            for operation in (lambda: cursor.execute("SELECT stale"),
                              lambda: execute("SELECT cached"),
                              lambda: cursor.fetchone(), lambda: cursor.close(),
                              lambda: iter(cursor)):
                with self.assertRaisesRegex(RuntimeError, "归还"):
                    operation()
            b.cursor().execute("SELECT current")
        self.assertNotIn("SELECT stale", self.connections[0].statements)
        self.assertNotIn("SELECT cached", self.connections[0].statements)
        self.assertIn("SELECT current", self.connections[0].statements)

    def test_invalidated_lease_rejects_existing_cursor(self):
        a = db_conn.db()
        cursor = a.cursor()
        a.invalidate()
        with self.assertRaisesRegex(RuntimeError, "归还"):
            cursor.execute("SELECT stale")
        self.assertTrue(self.connections[0].closed)

    def test_cursor_rejects_cross_process_access(self):
        with db_conn.db() as a:
            cursor = a.cursor()
            execute = cursor.execute
            with patch.object(db_conn.os, "getpid", return_value=os.getpid() + 1):
                with self.assertRaisesRegex(RuntimeError, "跨进程"):
                    execute("SELECT stale")

    def test_cursor_preserves_attributes_and_exposes_lease(self):
        with db_conn.db() as a:
            with a.cursor() as cursor:
                cursor.arraysize = 7
                self.assertEqual(cursor.arraysize, 7)
                self.assertIs(cursor.connection, a)
                self.assertEqual(cursor.fetchone(), {"value": 1})
    def test_concurrent_checkout_is_exclusive_and_waiter_gets_returned_connection(self):
        a = db_conn.db()
        started = threading.Event(); finished = threading.Event(); errors = []
        def worker():
            started.set()
            try:
                with db_conn.db() as b: b.cursor().execute("SELECT 1")
            except Exception as exc: errors.append(exc)
            finally: finished.set()
        thread = threading.Thread(target=worker); thread.start()
        self.assertTrue(started.wait(1))
        self.assertFalse(finished.wait(0.05))
        a.close(); thread.join(2)
        self.assertFalse(thread.is_alive()); self.assertEqual(errors, [])
        self.assertEqual(len(self.connections), 1)
    def test_overflow_is_bounded_and_not_cached(self):
        os.environ["PLATFORM_DB_POOL_MAX_OVERFLOW"] = "1"
        a = db_conn.db(); b = db_conn.db()
        self.assertEqual(len(self.connections), 2)
        a.close(); b.close()
        self.assertEqual(sum(not c.closed for c in self.connections), 1)
    def test_idle_disconnect_is_replaced_before_sql(self):
        db_conn.db().close(); self.connections[0].dead = True
        db_conn.db().close()
        self.assertEqual(len(self.connections), 2)
        self.assertTrue(self.connections[0].closed)
    def test_sql_failure_is_not_replayed(self):
        a = db_conn.db(); raw = self.connections[0]
        raw.execute_error = pymysql.OperationalError(2013, "synthetic lost response")
        with self.assertRaises(pymysql.OperationalError): a.cursor().execute("INSERT synthetic")
        self.assertEqual(raw.statements.count("INSERT synthetic"), 1)
        self.assertEqual(len(self.connections), 1)
        raw.execute_error = None; a.close()
    def test_return_reset_failure_discards_connection(self):
        a = db_conn.db(); self.connections[0].dead = True
        with self.assertLogs("sqlalchemy.pool", level="ERROR"): a.close()
        db_conn.db().close()
        self.assertEqual(len(self.connections), 2)
    def test_session_defaults_restored(self):
        a = db_conn.db(); raw = self.connections[0]
        a.autocommit(False); a.select_db("synthetic_other"); a.set_charset("latin1"); a.close()
        with db_conn.db():
            self.assertTrue(raw.auto); self.assertEqual(raw.database, "synthetic_one")
            self.assertEqual(raw.charset, "utf8mb4")
            self.assertEqual(raw.statements[-1], "SET time_zone = '+08:00'")
    def test_database_and_timeout_profiles_do_not_share(self):
        db_conn.db().close(); db_conn.db("synthetic_two").close(); db_conn.db(read_timeout=17).close()
        self.assertEqual(len(self.connections), 3)
        self.assertEqual(self.connections[2].kwargs["read_timeout"], 17)
    def test_credentials_change_retires_idle_and_checked_out_leases(self):
        a = db_conn.db(); old = self.connections[0]
        os.environ["PLATFORM_DB_PASSWORD"] = "synthetic-new-password"
        db_conn.db().close(); a.close()
        self.assertTrue(old.closed)
        self.assertEqual(self.connections[1].kwargs["password"], "synthetic-new-password")
    def test_close_pools_closes_idle_and_retires_busy(self):
        a = db_conn.db(); db_conn.close_pools(); a.close()
        self.assertTrue(self.connections[0].closed)
        self.assertEqual(db_conn._POOLS, {})
    def test_recycle_replaces_old_connection(self):
        db_conn.db().close()
        state = next(iter(db_conn._POOLS.values()))
        with patch("sqlalchemy.pool.base.time.time", return_value=time.time() + 3600): db_conn.db().close()
        self.assertEqual(len(self.connections), 2)
        self.assertTrue(self.connections[0].closed)
    def test_gc_returns_lease_after_exception_scope(self):
        import gc
        a = db_conn.db(); del a; gc.collect()
        db_conn.db().close()
        self.assertEqual(len(self.connections), 1)

    def test_context_exception_returns_connection(self):
        with self.assertRaises(ValueError):
            with db_conn.db(): raise ValueError("synthetic")
        db_conn.db().close()
        self.assertEqual(len(self.connections), 1)

    def test_invalid_limits_fail_before_connect(self):
        for key, value in [("PLATFORM_DB_POOL_SIZE", "0"), ("PLATFORM_DB_POOL_MAX_OVERFLOW", "-1"),
                           ("PLATFORM_DB_POOL_TIMEOUT", "0"), ("PLATFORM_DB_POOL_RECYCLE", "0"), ("PLATFORM_DB_POOL_SIZE", "101"), ("PLATFORM_DB_POOL_ENABLED", "2")]:
            with self.subTest(key=key, value=value), patch.dict(os.environ, {key: value}):
                with self.assertRaises(ValueError): db_conn.db()
        self.assertEqual(self.connections, [])
    def test_pool_can_be_disabled(self):
        os.environ["PLATFORM_DB_POOL_ENABLED"] = "0"
        db_conn.db().close(); db_conn.db().close()
        self.assertEqual(len(self.connections), 2)
        self.assertEqual(db_conn._POOLS, {})

    def test_missing_pool_dependency_is_explicit_and_short_connections_still_work(self):
        with patch.object(db_conn, "create_pool_from_url", None):
            with self.assertRaisesRegex(RuntimeError, "incomplete.*SQLAlchemy"):
                db_conn.db()
            os.environ["PLATFORM_DB_POOL_ENABLED"] = "0"
            db_conn.db().close()
        self.assertEqual(len(self.connections), 1)

    def test_session_restore_failure_discards_connection(self):
        db_conn.db().close()
        self.connections[0].execute_error = ValueError("synthetic session failure")
        with self.assertRaisesRegex(ValueError, "session failure"):
            db_conn.db()
        self.assertTrue(self.connections[0].closed)
        db_conn.db().close()
        self.assertEqual(len(self.connections), 2)

    def test_profile_limit_is_bounded(self):
        for index in range(16):
            db_conn.db("synthetic_%s" % index).close()
        with self.assertRaisesRegex(RuntimeError, "16"):
            db_conn.db("synthetic_over_limit")
        self.assertEqual(len(self.connections), 16)
    @unittest.skipUnless(hasattr(os, "fork"), "当前系统无 fork")
    def test_actual_fork_builds_child_pool_without_touching_parent_connection(self):
        a = db_conn.db()
        read_fd, write_fd = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(read_fd)
            try:
                rejected = False
                try: a.cursor()
                except RuntimeError: rejected = True
                db_conn.db().close()
                ok = rejected and len(self.connections) == 2 and not self.connections[0].closed
                db_conn.close_pools()
                os.write(write_fd, b"ok" if ok else b"failed")
            except BaseException:
                os.write(write_fd, b"error")
            finally:
                os.close(write_fd); os._exit(0)
        os.close(write_fd)
        try:
            result = os.read(read_fd, 20)
            _, status = os.waitpid(child, 0)
            self.assertEqual(status, 0); self.assertEqual(result, b"ok")
            a.cursor().execute("SELECT 1")
            self.assertFalse(self.connections[0].closed)
        finally:
            os.close(read_fd); a.close()

    def test_disable_retires_previously_enabled_pool(self):
        a = db_conn.db()
        os.environ["PLATFORM_DB_POOL_ENABLED"] = "0"
        db_conn.db().close(); a.close()
        self.assertTrue(self.connections[0].closed)
        self.assertEqual(db_conn._POOLS, {})

    def test_child_registry_is_fresh_and_parent_lease_cannot_be_used(self):
        a = db_conn.db(); original_pools = db_conn._POOLS; original_lock = db_conn._POOL_LOCK
        try:
            db_conn._after_fork()
            db_conn.db().close()
            with patch.object(db_conn.os, "getpid", return_value=os.getpid()+1):
                with self.assertRaisesRegex(RuntimeError, "跨进程"): a.cursor()
            self.assertEqual(len(self.connections), 2)
            db_conn.close_pools()
        finally:
            db_conn._POOLS = original_pools; db_conn._POOL_LOCK = original_lock; a.close()


if __name__ == "__main__": unittest.main()
