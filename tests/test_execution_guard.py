import os
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "test-platform"))
sys.path.insert(0, str(ROOT / "test-platform/common"))
import execution_guard as guard
from execution_context import context_callable


class ExecutionGuardTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        mocked = patch.object(guard, "lock_directory", return_value=self.directory)
        mocked.start()
        self.addCleanup(mocked.stop)

    def test_same_resource_blocks_other_thread_and_distinct_resource_runs(self):
        def probe(key):
            try:
                with guard.resource_guard(exclusive=[key], shared=["domain"]):
                    return True
            except guard.ExecutionBusy:
                return False
        with guard.resource_guard(exclusive=["one"], shared=["domain"]), ThreadPoolExecutor(2) as executor:
            self.assertFalse(executor.submit(probe,"one").result())
            self.assertTrue(executor.submit(probe,"two").result())
            def unknown_scope():
                with guard.resource_guard(exclusive=["domain"]):
                    pass
            with self.assertRaises(guard.ExecutionBusy):
                executor.submit(unknown_scope).result()
        self.assertTrue(probe("one"))

    def test_nested_and_delegated_context_reuse_lease_but_released_context_reacquires(self):
        with guard.resource_guard(exclusive=["one"]):
            task = context_callable(lambda: self._probe("one"))
            with ThreadPoolExecutor(1) as executor:
                self.assertTrue(executor.submit(task).result())
            with guard.resource_guard(exclusive=["one"]):
                self.assertTrue(guard.guarded())
        with guard.resource_guard(exclusive=["one"]), ThreadPoolExecutor(1) as executor:
            with self.assertRaises(guard.ExecutionBusy):
                executor.submit(task).result()
        self.assertFalse(guard.guarded())

    @staticmethod
    def _probe(key):
        with guard.resource_guard(exclusive=[key]):
            return True

    def test_partial_acquisition_and_runner_exception_release_locks(self):
        with guard.resource_guard(exclusive=["z"]), ThreadPoolExecutor(1) as executor:
            def fails():
                with guard.resource_guard(exclusive=["a","z"]):
                    pass
            with self.assertRaises(guard.ExecutionBusy):
                executor.submit(fails).result()
            self.assertTrue(executor.submit(self._probe,"a").result())
        with self.assertRaises(RuntimeError):
            with guard.resource_guard(exclusive=["a"]):
                raise RuntimeError("runner")
        self.assertTrue(self._probe("a"))

    def test_cross_process_lock_and_shared_upgrade_are_rejected(self):
        source = """import sys
from pathlib import Path
import execution_guard as g
g.lock_directory=lambda: Path(sys.argv[1])
try:
    with g.resource_guard(exclusive=['one']): pass
except g.ExecutionBusy: sys.exit(23)
"""
        with guard.resource_guard(exclusive=["one"]):
            proc = subprocess.run([sys.executable,"-c",source,str(self.directory)], cwd=ROOT,
                                  env={**os.environ,"PYTHONPATH":str(ROOT/"test-platform")},capture_output=True,text=True)
            self.assertEqual(proc.returncode,23,proc.stderr)
        with guard.resource_guard(shared=["one"]), self.assertRaises(guard.ExecutionBusy):
            with guard.resource_guard(exclusive=["one"]):
                pass

    def test_fork_does_not_inherit_execution_lease_permission(self):
        with guard.resource_guard(exclusive=["one"]):
            pid=os.fork()
            if pid==0:
                try:
                    self._probe("one")
                except guard.ExecutionBusy:
                    os._exit(23)
                except BaseException:
                    os._exit(24)
                os._exit(0)
            _pid,status=os.waitpid(pid,0)
            self.assertEqual(os.waitstatus_to_exitcode(status),23)

    def test_unlock_failure_still_closes_all_leases_and_restores_context(self):
        original = guard.fcntl.flock
        failed = False
        def flock(fd, mode):
            nonlocal failed
            if mode == guard.fcntl.LOCK_UN and not failed:
                failed = True
                raise OSError("synthetic unlock failure")
            return original(fd, mode)
        with patch.object(guard.fcntl, "flock", side_effect=flock), \
             patch.object(guard.os, "close", wraps=os.close) as close, self.assertRaises(OSError):
            with guard.resource_guard(exclusive=["one", "two"]):
                pass
        self.assertEqual(close.call_count, 2)
        self.assertFalse(guard.guarded())
        with ThreadPoolExecutor(1) as executor:
            self.assertTrue(executor.submit(self._probe, "one").result())
            self.assertTrue(executor.submit(self._probe, "two").result())
