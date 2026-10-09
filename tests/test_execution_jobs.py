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

    def test_raw_input_validation_happens_before_storage(self):
        for kwargs in ({"timeout_seconds":True},{"timeout_seconds":float("nan")},{"request_key":" x "},
                       {"request_key":"r"},{"metadata":[]},{"progress":[]}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError): self.create(**kwargs)
        self.assertFalse(self.store.path.exists())
