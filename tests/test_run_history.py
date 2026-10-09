from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "test-platform"))
sys.path.insert(0, str(ROOT / "test-platform" / "common"))

import run_history as history  # noqa: E402


class RunHistoryTest(unittest.TestCase):
    def test_single_execution_error_is_persisted_as_failure_with_detail(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "history.db"
            with patch.object(history, "DB_PATH", db_path):
                run_id = history.record_case(
                    {"id": "MFB-R04", "error": "TypeError: runner failed"},
                    duration_ms=17,
                )
                detail = history.run_detail(run_id)
                self.assertEqual(detail["passed"], 0)
                self.assertEqual(detail["total"], 1)
                self.assertEqual(detail["cases"][0]["id"], "MFB-R04")
                self.assertFalse(detail["cases"][0]["ok"])
                self.assertEqual(detail["cases"][0]["subs"][0]["detail"], "TypeError: runner failed")
                self.assertEqual(sum(s["label"] == "执行异常" for s in detail["cases"][0]["subs"]), 1)
                self.assertFalse(history.case_badges()["MFB-R04"]["ok"])
                self.assertEqual(history.case_history("MFB-R04")[0]["subs"][0]["detail"], "TypeError: runner failed")

    def test_batch_execution_error_is_persisted_as_failure_with_detail(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(history, "DB_PATH", Path(td) / "history.db"):
                run_id = history.record_run({
                    "mode": "safe", "suite": "demo", "passed": 1, "total": 1,
                    "cases": [{"id": "K-R01", "error": "RuntimeError: offline"}],
                })
                detail = history.run_detail(run_id)
                self.assertEqual(detail["passed"], 0)
                self.assertEqual(detail["total"], 1)
                self.assertFalse(detail["cases"][0]["ok"])
                self.assertEqual(detail["cases"][0]["subs"][0]["detail"], "RuntimeError: offline")
                self.assertEqual(sum(s["label"] == "执行异常" for s in detail["cases"][0]["subs"]), 1)

    def test_skip_is_stored_as_coverage_gap_and_not_a_pass(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(history, "DB_PATH", Path(td) / "history.db"):
                run_id = history.record_run({"mode": "safe", "cases": [{
                    "id": "K-R01", "ok": True,
                    "subs": [{"label": "sample", "ok": True, "detail": "库内无样本，跳过"}],
                }]})
                detail = history.run_detail(run_id)
                badge = history.case_badges()["K-R01"]
                run = history.recent_runs()[0]
                daily = history.summary(days=1)[0]
                self.assertEqual((detail["status"], detail["passed"], detail["skipped"]),
                                 ("incomplete", 0, 1))
                self.assertEqual((run["status"], run["failed"], run["skipped"], run["incomplete"]),
                                 ("incomplete", 0, 1, 0))
                self.assertEqual((daily["failed"], daily["skipped"], daily["incomplete"]), (0, 1, 0))
                self.assertEqual(detail["cases"][0]["status"], "skipped")
                self.assertIsNone(badge["ok"])
                self.assertEqual((badge["passed"], badge["skipped"], badge["evaluated"]), (0, 1, 0))
                self.assertEqual(history.flaky_cases(), [])

    def test_legacy_binary_history_is_migrated_from_assertion_details(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "legacy.db"
            import sqlite3
            with sqlite3.connect(db_path) as conn:
                conn.executescript("""
                    CREATE TABLE runs (
                        id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, mode TEXT NOT NULL,
                        suite TEXT, source TEXT NOT NULL, passed INTEGER NOT NULL,
                        total INTEGER NOT NULL, duration_ms INTEGER
                    );
                    CREATE TABLE case_results (
                        id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, case_id TEXT NOT NULL,
                        ok INTEGER NOT NULL, subs_json TEXT, ts INTEGER NOT NULL, duration_ms INTEGER
                    );
                    INSERT INTO runs VALUES (1, 100, 'safe', NULL, 'panel', 1, 1, NULL);
                    INSERT INTO case_results VALUES
                        (1, 1, 'OLD-SKIP', 1,
                         '[{"label":"sample","ok":true,"detail":"库内无样本，跳过"}]', 100, NULL);
                    INSERT INTO runs VALUES (2, 101, 'safe', NULL, 'panel', 2, 2, NULL);
                    INSERT INTO case_results VALUES
                        (2, 2, 'OLD-PASS', 1,
                         '[{"label":"sample","ok":true,"detail":"字段正确"}]', 101, NULL);
                    INSERT INTO runs VALUES (3, 102, 'safe', NULL, 'panel', 1, 1, NULL);
                """)
            with patch.object(history, "DB_PATH", db_path):
                runs = {run["id"]: run for run in history.recent_runs()}
                detail = history.run_detail(1)
                self.assertEqual((runs[1]["status"], runs[1]["passed"], runs[1]["skipped"], runs[1]["total"]),
                                 ("incomplete", 0, 1, 1))
                self.assertEqual(detail["cases"][0]["status"], "skipped")
                self.assertEqual((runs[2]["status"], runs[2]["passed"], runs[2]["incomplete"], runs[2]["total"]),
                                 ("incomplete", 1, 1, 2))
                self.assertEqual((runs[3]["status"], runs[3]["passed"], runs[3]["incomplete"], runs[3]["total"]),
                                 ("incomplete", 0, 1, 1))

    def test_concurrent_legacy_migration_is_serialized(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "legacy.db"
            import sqlite3
            with sqlite3.connect(db_path) as conn:
                conn.executescript("""
                    CREATE TABLE runs (
                        id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, mode TEXT NOT NULL,
                        suite TEXT, source TEXT NOT NULL, passed INTEGER NOT NULL,
                        total INTEGER NOT NULL, duration_ms INTEGER
                    );
                    CREATE TABLE case_results (
                        id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, case_id TEXT NOT NULL,
                        ok INTEGER NOT NULL, subs_json TEXT, ts INTEGER NOT NULL, duration_ms INTEGER
                    );
                """)
            with patch.object(history, "DB_PATH", db_path):
                with ThreadPoolExecutor(max_workers=8) as pool:
                    results = list(pool.map(lambda _index: history.case_badges(), range(16)))
            self.assertEqual(results, [{}] * 16)

    def test_concurrent_process_legacy_migration_is_serialized(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            db_path = base / "legacy.db"
            ready_dir = base / "ready"
            ready_dir.mkdir()
            gate = base / "start"
            import sqlite3
            with sqlite3.connect(db_path) as conn:
                conn.executescript("""
                    CREATE TABLE runs (
                        id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, mode TEXT NOT NULL,
                        suite TEXT, source TEXT NOT NULL, passed INTEGER NOT NULL,
                        total INTEGER NOT NULL, duration_ms INTEGER
                    );
                    CREATE TABLE case_results (
                        id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, case_id TEXT NOT NULL,
                        ok INTEGER NOT NULL, subs_json TEXT, ts INTEGER NOT NULL, duration_ms INTEGER
                    );
                """)

            child = """
import sys, time
from pathlib import Path
tp = Path(sys.argv[1])
sys.path[:0] = [str(tp), str(tp / "common")]
import run_history
run_history.DB_PATH = Path(sys.argv[2])
ready, gate = Path(sys.argv[3]), Path(sys.argv[4])
ready.touch()
while not gate.exists():
    time.sleep(0.002)
run_history.case_badges()
print("ok")
"""
            processes = []
            try:
                for index in range(8):
                    processes.append(subprocess.Popen(
                        [sys.executable, "-c", child, str(ROOT / "test-platform"),
                         str(db_path), str(ready_dir / str(index)), str(gate)],
                        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    ))

                deadline = time.monotonic() + 15
                while len(list(ready_dir.iterdir())) < len(processes) and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(len(list(ready_dir.iterdir())), len(processes),
                                 "子进程未全部到达并发迁移屏障")
                gate.touch()
                results = [process.communicate(timeout=30) for process in processes]
                failures = [
                    (process.returncode, stdout, stderr)
                    for process, (stdout, stderr) in zip(processes, results)
                    if process.returncode != 0
                ]
                self.assertEqual(failures, [])
                self.assertEqual([stdout.strip() for stdout, _stderr in results], ["ok"] * len(processes))
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.terminate()
                for process in processes:
                    try:
                        process.communicate(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate()


if __name__ == "__main__":
    unittest.main()
