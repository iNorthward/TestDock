import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'test-platform'), str(ROOT / 'test-platform/common')]
import execution_guard as guard
import execution_jobs as jobs


class BackgroundExecutionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = jobs.JobStore(Path(directory.name) / 'jobs.db')
        for replacement in (patch.object(jobs, 'get_store', return_value=self.store),
                            patch.object(guard, 'lock_directory', return_value=Path(directory.name)/'locks')):
            replacement.start()
            self.addCleanup(replacement.stop)

    def wait(self, identifier):
        deadline = time.monotonic()+3
        while time.monotonic() < deadline:
            result = self.store.get(identifier)
            if result['status'] in jobs.TERMINAL:
                return result
            time.sleep(0.005)
        self.fail('离线任务未结束')

    def test_detached_worker_does_not_borrow_parent_lease(self):
        entered = threading.Event()
        writes = []
        def operation(identifier, store):
            entered.set()
            with guard.resource_guard(exclusive=['synthetic-owner']):
                writes.append('write')
            return {'ok': True}
        with guard.resource_guard(exclusive=['synthetic-owner']):
            identifier = jobs.start_background_job(kind='test', payload={}, operation=operation)
            self.assertTrue(entered.wait(2))
            self.assertEqual(self.wait(identifier)['status'], 'incomplete')
        self.assertEqual(writes, [])
        with guard.resource_guard(exclusive=['synthetic-owner']):
            pass

    def test_retry_runs_once_and_does_not_inherit_parent_cancellation(self):
        parent, _ = self.store.create(kind='parent')
        self.store.start(parent['id'])
        self.store.cancel(parent['id'])
        entered, release = threading.Event(), threading.Event()
        calls = []
        def run(identifier, store):
            calls.append(identifier)
            entered.set()
            self.assertTrue(release.wait(2))
            jobs.checkpoint()
            return {'ok': True}
        token = jobs._CONTROL.set(jobs.JobControl(self.store, parent['id']))
        try:
            identifier = jobs.start_background_job(kind='test', request_id='retry', payload={'amount':'1.00'}, operation=run)
        finally:
            jobs._CONTROL.reset(token)
        self.assertTrue(entered.wait(2))
        retry = jobs.start_background_job(kind='test', request_id='retry', payload={'amount':'1.00'}, operation=run)
        self.assertEqual(identifier, retry)
        with self.assertRaises(jobs.RequestConflict):
            jobs.start_background_job(kind='test', request_id='retry', payload={'amount':1.0}, operation=run)
        release.set()
        self.assertEqual(self.wait(identifier)['status'], 'done')
        self.assertEqual(calls, [identifier])

    def test_cleanup_failure_has_priority_over_cancel_and_recovery_never_replays(self):
        def run(identifier, store):
            store.cancel(identifier)
            return {'ok': False, 'needsReview': True, 'error':'synthetic cleanup failure'}
        identifier = jobs.start_background_job(kind='test', payload={}, operation=run)
        result = self.wait(identifier)
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(result['result']['needsReview'])
        pending, _ = self.store.create(kind='test')
        with self.store.connection() as conn:
            conn.execute('UPDATE execution_jobs SET owner_pid=99999999 WHERE id=?', (pending['id'],))
        self.assertEqual(self.store.recover(), 1)
        self.assertEqual(self.store.get(pending['id'])['status'], 'interrupted')
