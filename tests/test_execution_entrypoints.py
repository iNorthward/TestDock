import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/"test-platform"), str(ROOT/"test-platform/common")]
import catalog
import execution_guard
import execution_jobs
import server
from test_server_duration_clock import FakePostHandler


class ExecutionEntrypointsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = execution_jobs.JobStore(Path(directory.name)/"jobs.db")
        for mock in (patch.object(execution_jobs,"get_store",return_value=self.store),
                     patch.object(execution_guard,"lock_directory",return_value=Path(directory.name)/"locks"),
                     patch.object(server,"execution_metadata",return_value={"packId":"synthetic"})):
            mock.start()
            self.addCleanup(mock.stop)

    def test_two_requests_for_same_case_do_not_enter_runner_together(self):
        entered, release = threading.Event(), threading.Event()
        def run(case_id):
            entered.set()
            self.assertTrue(release.wait(3))
            return {"id":case_id,"ok":True,"subs":[{"ok":True}]}
        runner = Mock(side_effect=run)
        module = SimpleNamespace(exec_case=runner)
        case = {"id":"S-L01","mode":"live"}
        extension = SimpleNamespace(execution_resources=lambda *a:{"exclusive":["synthetic-uid"],"shared":[]})
        with patch.object(catalog,"_EXEC",{"S-L01":module}), patch.object(catalog,"_ALL",[case]), \
             patch.object(catalog,"CATALOG_EXTENSIONS",[("synthetic",extension)]), \
             patch.object(catalog,"_case_write_guard",return_value=None), ThreadPoolExecutor(2) as executor:
            first = executor.submit(catalog.exec_case,"S-L01")
            self.assertTrue(entered.wait(3))
            busy = executor.submit(catalog.exec_case,"S-L01").result()
            self.assertEqual(busy["status"],"incomplete")
            self.assertIn("资源",busy["subs"][0]["detail"])
            release.set()
            self.assertEqual(first.result()["status"],"passed")
        runner.assert_called_once()

    def test_panel_retry_keeps_one_history_row_and_conflicting_params_return_409(self):
        body={"id":"S-L01","requestId":"request-1","orderOptions":{"volume":"0.01"}}
        with patch.object(server.cat,"_CAT",{"S-L01":{"suite":"synthetic","mode":"live"}}), \
             patch.object(server.cat,"exec_case",return_value={"id":"S-L01","ok":True}) as run, \
             patch.object(server.rh,"record_case",return_value=42) as record:
            one=FakePostHandler("/api/run-case",body)
            one.do_POST()
            two=FakePostHandler("/api/run-case",body)
            two.do_POST()
            conflict=FakePostHandler("/api/run-case",{**body,"orderOptions":{"volume":0.01}})
            conflict.do_POST()
        run.assert_called_once()
        record.assert_called_once()
        self.assertEqual(one.responses[0][1]["data"]["runId"],42)
        self.assertTrue(two.responses[0][1]["data"]["deduplicated"])
        self.assertEqual(conflict.responses[0][0],409)

    def test_batch_retry_is_deduplicated_and_history_failure_is_persisted_without_replay(self):
        body={"suite":"synthetic","requestId":"batch-1"}
        with patch.object(server.cat,"run_all",return_value={"ok":True,"cases":[]}) as run, \
             patch.object(server.rh,"record_run",side_effect=OSError("synthetic disk full")) as record, \
             self.assertLogs(server.__name__,level="ERROR"):
            one=FakePostHandler("/api/run",body)
            one.do_POST()
            two=FakePostHandler("/api/run",body)
            two.do_POST()
        run.assert_called_once()
        record.assert_called_once()
        self.assertFalse(two.responses[0][1]["data"]["historySaved"])

    def test_job_query_and_cancel_validate_ids_and_preserve_terminal_result(self):
        job, _ = self.store.create(kind="synthetic")
        self.store.start(job["id"])
        self.store.finish(job["id"], result={"ok": True})
        for value in (None, [], {}, 12, " ", " spaced ", "x"*129):
            with self.subTest(value=value):
                cancel = FakePostHandler("/api/execution/cancel", {"id": value})
                cancel.do_POST()
                self.assertEqual(cancel.responses[0][0], 400)
        for path, status in (("/api/execution/job", 400), ("/api/execution/job?id=missing", 404),
                             (f"/api/execution/job?id={job['id']}", 200)):
            handler = FakePostHandler(path, {})
            server.H.do_GET(handler)
            self.assertEqual(handler.responses[0][0], status)
        cancel = FakePostHandler("/api/execution/cancel", {"id": job["id"]})
        cancel.do_POST()
        self.assertEqual(cancel.responses[0][1]["data"]["status"], "done")
        self.assertEqual(cancel.responses[0][1]["data"]["result"], {"ok": True})
