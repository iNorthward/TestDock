from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "test-platform"))
sys.path.insert(0, str(ROOT / "test-platform" / "common"))

import server  # noqa: E402


class FakePostHandler:
    do_POST = server.H.do_POST

    def __init__(self, path, body):
        self.path = path
        self.body = body
        self.responses = []

    def _body(self):
        return self.body

    def _json(self, status, payload):
        self.responses.append((status, payload))


class ServerDurationClockTests(unittest.TestCase):
    def test_history_save_failure_is_visible_and_does_not_repeat_runner(self):
        case = {"id": "K-R01", "ok": True, "status": "passed", "subs": [{"ok": True}]}
        handler = FakePostHandler("/api/run-case", {"id": "K-R01"})
        with patch.object(server.cat, "_CAT", {"K-R01": {"suite": "demo"}}), \
                patch.object(server.cat, "exec_case", return_value=case) as run, \
                patch.object(server.rh, "record_case", side_effect=OSError("disk full")) as record, \
                self.assertLogs(server.__name__, level="ERROR") as logs:
            handler.do_POST()
        result = handler.responses[0][1]["data"]
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "passed")
        self.assertFalse(result["historySaved"])
        self.assertEqual(result["historyError"], "disk full")
        self.assertIn("K-R01", logs.output[0])
        run.assert_called_once()
        record.assert_called_once()

    def test_successful_history_save_returns_one_run_id_without_error(self):
        handler = FakePostHandler("/api/run-case", {"id": "K-R01"})
        with patch.object(server.cat, "_CAT", {"K-R01": {"suite": "demo"}}), \
                patch.object(server.cat, "exec_case", return_value={"id": "K-R01", "ok": True}) as run, \
                patch.object(server.rh, "record_case", return_value=42) as record:
            handler.do_POST()
        result = handler.responses[0][1]["data"]
        self.assertEqual(result["runId"], 42)
        self.assertTrue(result["historySaved"])
        self.assertNotIn("historyError", result)
        run.assert_called_once()
        record.assert_called_once()

    def test_batch_run_history_duration_uses_monotonic_clock(self):
        result = {"mode": "safe", "suite": "demo", "cases": []}
        record = Mock(return_value=31)
        handler = FakePostHandler("/api/run", {"mode": "safe", "suite": "demo"})
        clock = types.SimpleNamespace(monotonic=Mock(side_effect=[10.0, 12.75]))

        with patch.object(server, "time", clock), \
                patch.object(server.cat, "run_all", return_value=result), \
                patch.object(server.rh, "record_run", record):
            handler.do_POST()

        self.assertEqual(record.call_args.kwargs["duration_ms"], 2750)
        self.assertEqual(handler.responses[0][0], 200)

    def test_single_case_history_duration_uses_monotonic_clock(self):
        case_result = {
            "id": "K-R01", "ok": True,
            "subs": [{"ok": True, "status": "passed", "detail": "pass"}],
        }
        record = Mock()
        handler = FakePostHandler("/api/run-case", {"id": "K-R01"})
        clock = types.SimpleNamespace(monotonic=Mock(side_effect=[50.0, 50.5]))

        with patch.object(server, "time", clock), \
                patch.object(server.cat, "_CAT", {"K-R01": {"suite": "demo", "mode": "safe"}}), \
                patch.object(server.cat, "exec_case", return_value=case_result), \
                patch.object(server.rh, "record_case", record):
            handler.do_POST()

        self.assertEqual(record.call_args.kwargs["duration_ms"], 500)
        self.assertEqual(handler.responses[0][0], 200)


if __name__ == "__main__":
    unittest.main()
