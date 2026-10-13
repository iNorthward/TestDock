import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT / "test-platform"))
import execution_jobs as jobs


class ExecutionJobsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = jobs.JobStore(Path(directory.name) / "jobs.db")
        mocked = patch.object(jobs,"get_store",return_value=self.store)
        mocked.start()
        self.addCleanup(mocked.stop)

    def create(self, **kwargs):
        return self.store.create(kind="test", **kwargs)[0]

    def test_isolated_stop_remains_terminal_in_synchronous_compatibility(self):
        for terminal in ('cancelled','timed_out'):
            result=jobs.run_idempotent(kind='isolated',request_id=terminal,payload={},operation=lambda:{'ok':None,'status':'incomplete','executionStatus':terminal})
            self.assertEqual(self.store.get(result['executionId'])['status'],terminal)

    def test_concurrent_retry_runs_operation_once_and_stores_redacted_result(self):
        entered, release = threading.Event(), threading.Event()
        def operation():
            entered.set()
            self.assertTrue(release.wait(3))
            return {"ok":True,"password":"SYNTHETIC","nested":{"accessToken":"SECRET","uid":"UID1"}}
        run = Mock(side_effect=operation)
        args = dict(kind="test",request_id="request-1",payload={"uid":"UID1","password":"SECRET"},operation=run)
        with ThreadPoolExecutor(2) as executor:
            first = executor.submit(jobs.run_idempotent,**args)
            self.assertTrue(entered.wait(3))
            pending = executor.submit(jobs.run_idempotent,**args).result()
            self.assertTrue(pending["pending"])
            self.assertIsNone(pending["ok"])
            release.set()
            done = first.result()
        retry = jobs.run_idempotent(**args)
        run.assert_called_once()
        self.assertEqual(retry["executionId"],done["executionId"])
        self.assertNotIn("password",retry)
        self.assertNotIn("accessToken",retry["nested"])
        with self.store.connection() as conn:
            raw = str(dict(conn.execute("SELECT * FROM execution_jobs").fetchone()))
        self.assertNotIn("SECRET",raw)
        self.assertNotIn("SYNTHETIC",raw)

    def test_different_payload_or_kind_is_conflict_and_failed_request_never_replays(self):
        args = dict(kind="one",request_id="r",payload={"amount":"1.00"},operation=lambda:{"ok":False})
        first = jobs.run_idempotent(**args)
        runner = Mock()
        for changed in ({"payload":{"amount":1}}, {"kind":"two"}):
            with self.assertRaises(jobs.RequestConflict):
                jobs.run_idempotent(**{**args,**changed,"operation":runner})
        retry = jobs.run_idempotent(**{**args,"operation":runner})
        runner.assert_not_called()
        self.assertFalse(retry["ok"])
        self.assertEqual(first["executionId"],retry["executionId"])

    def test_terminal_state_and_progress_survive_new_store_and_progress_is_bounded(self):
        job = self.create(metadata={"password":"secret","packId":"test"})
        self.assertTrue(self.store.start(job["id"]))
        self.assertFalse(self.store.start(job["id"]))
        self.store.progress(job["id"],lambda p:p["events"].extend({"n":n,"token":"secret"} for n in range(310)))
        self.store.finish(job["id"],result={"ok":True,"password":"secret"})
        restored = jobs.JobStore(self.store.path).get(job["id"])
        self.assertEqual(restored["status"],"done")
        self.assertEqual(len(restored["events"]),300)
        self.assertNotIn("password",restored["meta"])
        self.assertNotIn("token",restored["events"][0])
        self.store.finish(job["id"],status="failed",error="late")
        self.assertEqual(self.store.get(job["id"])["status"],"done")

    def test_cancellation_timeout_and_cleanup_context_restore(self):
        job = self.create()
        self.store.start(job["id"])
        with jobs.control_context(job["id"]):
            self.assertEqual(self.store.cancel(job["id"])["status"],"cancel_requested")
            with self.assertRaises(jobs.JobCancelled): jobs.checkpoint()
            with jobs.cleanup_context(): jobs.checkpoint()
            with self.assertRaises(jobs.JobCancelled): jobs.checkpoint()
        jobs.checkpoint()
        queued = self.create()
        self.assertEqual(self.store.cancel(queued["id"])["status"],"cancelled")
        timed = self.create(timeout_seconds=1)
        self.store.start(timed["id"])
        with self.store.connection() as conn:
            conn.execute("UPDATE execution_jobs SET deadline_at=? WHERE id=?",(time.time()-1,timed["id"]))
        with self.assertRaises(jobs.JobTimedOut):
            with jobs.control_context(timed["id"]): pass

    def test_recovery_does_not_replay_or_interrupt_living_owner(self):
        live, dead = self.create(), self.create()
        with self.store.connection() as conn:
            conn.execute("UPDATE execution_jobs SET owner_pid=99999999 WHERE id=?",(dead["id"],))
        self.assertEqual(self.store.recover(),1)
        self.assertEqual(self.store.get(live["id"])["status"],"queued")
        self.assertEqual(self.store.get(dead["id"])["status"],"interrupted")
        self.assertEqual(self.store.recover(),0)

    def test_prune_retains_active_and_fresh_idempotency_but_expires_old_results(self):
        active, fresh, old = self.create(), self.create(), self.create()
        self.store.finish(fresh["id"],result={"ok":True})
        self.store.finish(old["id"],result={"ok":True})
        with self.store.connection() as conn:
            conn.execute("UPDATE execution_jobs SET updated_at=? WHERE id IN (?,?)",(time.time()-8*86400,active["id"],old["id"]))
        self.assertEqual(self.store.prune(maximum_completed=1),1)
        self.assertIsNone(self.store.get(old["id"]))
        self.assertIsNotNone(self.store.get(active["id"]))
        self.assertIsNotNone(self.store.get(fresh["id"]))

    def test_independent_error_is_redacted_before_storage_and_on_query(self):
        job = self.create()
        message = "Bearer synthetic-token token=synthetic-secret"
        self.store.finish(job["id"], result={"ok": False, "error": message}, error=message)
        restored = self.store.get(job["id"])
        self.assertEqual(restored["error"], "Bearer [redacted] token=[redacted]")
        self.assertEqual(restored["result"]["error"], restored["error"])
        with self.store.connection() as conn:
            raw = str(dict(conn.execute("SELECT * FROM execution_jobs").fetchone()))
            self.assertNotIn("synthetic-token", raw)
            self.assertNotIn("synthetic-secret", raw)
            # Defense in depth protects reads even if an older writer adds a row.
            conn.execute("UPDATE execution_jobs SET error=? WHERE id=?", (message, job["id"]))
        self.assertNotIn("synthetic-secret", self.store.get(job["id"])["error"])

    def test_legacy_store_migrates_error_without_losing_result_or_request(self):
        with sqlite3.connect(self.store.path) as conn:
            conn.execute("""CREATE TABLE execution_jobs (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, request_key TEXT UNIQUE,
                fingerprint TEXT, status TEXT NOT NULL, created_at REAL NOT NULL,
                updated_at REAL NOT NULL, deadline_at REAL NOT NULL,
                owner_pid INTEGER, owner_tag TEXT, metadata_json TEXT NOT NULL,
                progress_json TEXT NOT NULL, result_json TEXT, error TEXT
            )""")
            conn.execute("""INSERT INTO execution_jobs VALUES
                (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("legacy", "test", "legacy-request", jobs.request_fingerprint({}), "failed",
                 1, time.time(), 2, None, None, "{}", "{}", '{"ok":false}', "token=legacy-secret"))
        restored = self.store.get("legacy")
        self.assertEqual(restored["error"], "token=[redacted]")
        self.assertEqual(restored["result"], {"ok": False})
        runner = Mock()
        retry = jobs.run_idempotent(kind="test", request_id="legacy-request", payload={}, operation=runner)
        runner.assert_not_called()
        self.assertEqual(retry["executionId"], "legacy")
        with sqlite3.connect(self.store.path) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT error FROM execution_jobs").fetchone()[0], "token=[redacted]")

    def test_pruned_request_keeps_original_id_and_never_replays_or_reports_success(self):
        runner = Mock(return_value={"ok": True, "uid": "synthetic-private-uid"})
        payload = {"amount": "1.00", "token": "synthetic-private-token"}
        args = dict(kind="test", request_id="synthetic-private-request", payload=payload, operation=runner)
        original = jobs.run_idempotent(**args)
        with self.store.connection() as conn:
            conn.execute("UPDATE execution_jobs SET updated_at=? WHERE id=?",
                         (time.time()-8*86400, original["executionId"]))
        self.assertEqual(self.store.prune(), 1)
        # Reopen the SQLite store to ensure deduplication survives process restart.
        self.store = jobs.JobStore(self.store.path)
        with patch.object(jobs, "get_store", return_value=self.store), ThreadPoolExecutor(4) as executor:
            retries = list(executor.map(lambda _: jobs.run_idempotent(**args), range(4)))
        runner.assert_called_once()
        for retry in retries:
            self.assertEqual(retry["executionId"], original["executionId"])
            self.assertTrue(retry["deduplicated"])
            self.assertTrue(retry["resultExpired"])
            self.assertEqual(retry["originalExecutionStatus"], "done")
            self.assertEqual(retry["status"], "incomplete")
            self.assertIsNone(retry["ok"])
            self.assertIn("核验业务状态", retry["error"])
        with self.store.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM execution_jobs").fetchone()[0], 0)
            tombstones = [dict(row) for row in conn.execute("SELECT * FROM execution_job_tombstones")]
        self.assertEqual(len(tombstones), 1)
        for private in (args["request_id"], payload["token"], "synthetic-private-uid", payload["amount"]):
            self.assertNotIn(private, str(tombstones))
        with self.assertRaises(jobs.RequestConflict):
            self.store.create(kind="test", request_key=args["request_id"], fingerprint=jobs.request_fingerprint({"amount": 1}))
        with self.assertRaises(jobs.RequestConflict):
            self.store.create(kind="another", request_key=args["request_id"], fingerprint=jobs.request_fingerprint(payload))

    def test_background_retry_queries_expired_job_without_enqueuing(self):
        payload = {"amount": "1.00"}
        job, _ = self.store.create(kind="test", request_key="background-expired",
            fingerprint=jobs.request_fingerprint({"payload": payload, "timeoutSeconds": 1200}))
        self.store.finish(job["id"], result={"ok": True})
        with self.store.connection() as conn:
            conn.execute("UPDATE execution_jobs SET updated_at=? WHERE id=?", (time.time()-8*86400, job["id"]))
        self.store.prune()
        runner = Mock()
        with patch.object(jobs, "get_executor") as executor:
            identifier = jobs.start_background_job(kind="test", request_id="background-expired", payload=payload, operation=runner)
        executor.assert_not_called()
        runner.assert_not_called()
        self.assertEqual(identifier, job["id"])
        restored = self.store.get(identifier)
        self.assertIn(restored["status"], jobs.TERMINAL)
        self.assertTrue(restored["result"]["resultExpired"])
        self.assertEqual(self.store.cancel(identifier), restored)

    def test_overflow_prune_preserves_tombstones_but_keeps_fresh_bodies(self):
        identifiers = []
        for index in range(3):
            job, _ = self.store.create(kind="test", request_key=f"overflow-{index}", fingerprint=jobs.request_fingerprint({}))
            self.store.finish(job["id"], result={"ok": True})
            identifiers.append(job["id"])
        with self.store.connection() as conn:
            conn.execute("UPDATE execution_jobs SET updated_at=? WHERE id IN (?,?)",
                         (time.time()-2*86400, *identifiers[:2]))
        self.assertEqual(self.store.prune(maximum_completed=1), 2)
        for identifier in identifiers[:2]:
            self.assertTrue(self.store.get(identifier)["resultExpired"])
        self.assertNotIn("resultExpired", self.store.get(identifiers[-1]))

    def test_prune_tombstone_and_body_delete_are_atomic(self):
        job, _ = self.store.create(kind="test", request_key="atomic", fingerprint=jobs.request_fingerprint({}))
        self.store.finish(job["id"], result={"ok": True})
        with self.store.connection() as conn:
            conn.execute("UPDATE execution_jobs SET updated_at=? WHERE id=?", (time.time()-8*86400, job["id"]))
            conn.execute("""CREATE TRIGGER synthetic_delete_failure BEFORE DELETE ON execution_jobs
                BEGIN SELECT RAISE(ABORT, 'synthetic prune failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.prune()
        self.assertEqual(self.store.get(job["id"])["result"], {"ok": True})
        with self.store.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM execution_job_tombstones").fetchone()[0], 0)

    def test_failure_evidence_has_priority_over_cancelled_or_timed_out(self):
        for stop in ("cancelled", "timed_out"):
            for evidence in ({"ok": False}, {"status": "failed"}, {"ok": None, "error": "cleanup failed"},
                             {"ok": None, "status": "incomplete", "error": "cleanup failed"}):
                with self.subTest(stop=stop, evidence=evidence):
                    job = self.create()
                    self.store.finish(job["id"], result={**evidence, "executionStatus": stop})
                    self.assertEqual(self.store.get(job["id"])["status"], "failed")

    def test_raw_input_validation_happens_before_storage(self):
        for kwargs in ({"timeout_seconds":True},{"timeout_seconds":float("nan")},{"request_key":" x "},
                       {"request_key":"r"},{"metadata":[]},{"progress":[]}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError): self.create(**kwargs)
        self.assertFalse(self.store.path.exists())

    def test_missing_success_evidence_never_marks_job_done(self):
        for result in (None, {}, {'ok':None}, {'status':'incomplete','ok':None},
                       {'status':'failed'}, {'ok':False,'status':'incomplete'}):
            with self.subTest(result=result):
                job=self.create()
                self.store.finish(job['id'],result=result)
                expected='failed' if isinstance(result,dict) and (result.get('ok') is False or result.get('status')=='failed') else 'incomplete'
                self.assertEqual(self.store.get(job['id'])['status'],expected)
        job=self.create();self.store.finish(job['id'],result={'status':'passed'})
        self.assertEqual(self.store.get(job['id'])['status'],'done')

    def test_synchronous_return_checks_pending_cancellation_and_timeout(self):
        for reason in ('cancelled','timed_out'):
            def operation():
                control=jobs._CONTROL.get()
                if reason=='cancelled':self.store.cancel(control.job_id)
                else:control.deadline=0
                return {'ok':True}
            result=jobs.run_idempotent(kind='late-stop',request_id=reason,payload={},operation=operation)
            self.assertEqual(result['executionStatus'],reason)
            self.assertIsNone(result['ok'])
            self.assertEqual(self.store.get(result['executionId'])['status'],reason)
