import sys,threading,time,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'test-platform'))
from execution_queue import BoundedExecutor,QueueFull
class ExecutionQueueTests(unittest.TestCase):
    def test_bound_fifo_and_cancel_release_capacity(self):
        executor=BoundedExecutor(1,1);release=threading.Event();entered=threading.Event();seen=[]
        self.addCleanup(executor.shutdown);self.addCleanup(release.set)
        def busy():entered.set();release.wait(2)
        executor.submit('first',busy);self.assertTrue(entered.wait(1));executor.submit('second',lambda:seen.append(2))
        with self.assertRaises(QueueFull):executor.submit('third',lambda:seen.append(3))
        self.assertEqual(executor.snapshot()['running'],1);self.assertEqual(executor.cancel_queued('second'),1)
        executor.submit('fourth',lambda:seen.append(4));release.set();executor.shutdown();self.assertEqual(seen,[4])
    def test_sync_and_background_share_limit_and_reentrant_slot(self):
        executor=BoundedExecutor(1,2);release=threading.Event();entered=threading.Event()
        self.addCleanup(executor.shutdown);self.addCleanup(release.set)
        with executor.synchronous_slot():
            with executor.synchronous_slot():pass
            executor.submit('queued',lambda:entered.set())
            self.assertFalse(entered.wait(.05))
        self.assertTrue(entered.wait(1));executor.shutdown()
    def test_invalid_limits_rejected_before_workers(self):
        for values in ((True,1),(0,2),(1,0),(17,1)):
            with self.assertRaises(ValueError):BoundedExecutor(*values)

    def test_durable_queued_cancel_releases_real_executor_capacity(self):
        import tempfile,os
        from unittest.mock import patch
        import execution_jobs as jobs
        with tempfile.TemporaryDirectory() as temp:
            executor=BoundedExecutor(1,1);release=threading.Event();entered=threading.Event()
            store=jobs.JobStore(Path(temp)/'jobs.db')
            def busy(*args):entered.set();release.wait(2);return {'ok':True}
            try:
                with patch.object(jobs,'get_store',return_value=store),patch.object(jobs,'_EXECUTOR',executor),patch.object(jobs,'_EXECUTOR_PID',os.getpid()):
                    first=jobs.start_background_job(kind='busy',payload={},operation=busy)
                    self.assertTrue(entered.wait(1))
                    second=jobs.start_background_job(kind='queued',payload={},operation=lambda *a:{'ok':True})
                    self.assertEqual(executor.snapshot()['queued'],1)
                    self.assertEqual(store.cancel(second)['status'],'cancelled')
                    self.assertEqual(executor.snapshot()['queued'],0)
                    third=jobs.start_background_job(kind='next',payload={},operation=lambda *a:{'ok':True})
                    self.assertEqual(store.get(third)['status'],'queued')
                    release.set();executor.shutdown()
                    self.assertEqual(store.get(third)['status'],'done')
            finally:release.set();executor.shutdown()
