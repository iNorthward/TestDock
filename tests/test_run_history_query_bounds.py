from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "test-platform"))
sys.path.insert(0, str(ROOT / "test-platform" / "common"))

import run_history  # noqa: E402
import server  # noqa: E402


class FakeHistoryHandler:
    do_GET = server.H.do_GET

    def __init__(self, path):
        self.path = path
        self.responses = []

    def _json(self, status, payload):
        self.responses.append((status, payload))


class RunHistoryQueryBoundsTests(unittest.TestCase):
    def test_negative_limit_is_rejected_before_sqlite_limit_can_be_unbounded(self):
        with tempfile.TemporaryDirectory() as td, \
                patch.object(run_history, "DB_PATH", Path(td) / "history.db"):
            for index in range(3):
                run_history.record_case({
                    "id": f"K-R0{index + 1}", "ok": True,
                    "subs": [{"ok": True, "detail": "pass"}],
                })

            self.assertEqual(len(run_history.recent_runs(2)), 2)
            with self.assertRaisesRegex(ValueError, "limit"):
                run_history.recent_runs(-1)

    def test_history_functions_reject_invalid_bounds_before_opening_database(self):
        cases = (
            (run_history.recent_runs, (-1,)),
            (run_history.recent_runs, (run_history.MAX_HISTORY_LIMIT + 1,)),
            (run_history.case_history, ("K-R01", "-1")),
            (run_history.case_badges, (0,)),
            (run_history.flaky_cases, (run_history.MAX_HISTORY_WINDOW + 1,)),
            (run_history.summary, ("not-a-number",)),
        )
        with patch.object(run_history, "_conn", side_effect=AssertionError("database opened")) as open_db:
            for function, args in cases:
                with self.subTest(function=function.__name__, args=args):
                    with self.assertRaises(ValueError):
                        function(*args)
        open_db.assert_not_called()

    def test_http_history_routes_return_400_for_invalid_parameters(self):
        cases = (
            ("/api/history?limit=-1", "recent_runs"),
            ("/api/history/case?id=K-R01&limit=501", "case_history"),
            ("/api/history/summary?days=abc", "summary"),
            ("/api/flaky?window=501", "flaky_cases"),
            ("/api/case-badges?window=0", "case_badges"),
        )
        for path, function_name in cases:
            with self.subTest(path=path):
                handler = FakeHistoryHandler(path)
                with patch.object(server.rh, function_name) as query:
                    handler.do_GET()
                self.assertEqual(handler.responses[0][0], 400)
                self.assertFalse(handler.responses[0][1]["ok"])
                self.assertIn("必须是 1 到", handler.responses[0][1]["error"])
                query.assert_not_called()

    def test_valid_http_history_parameter_reaches_query(self):
        handler = FakeHistoryHandler("/api/history?limit=25")
        query = Mock(return_value=[])
        with patch.object(server.rh, "recent_runs", query):
            handler.do_GET()
        self.assertEqual(handler.responses, [(200, {"ok": True, "data": []})])
        query.assert_called_once_with(25)


if __name__ == "__main__":
    unittest.main()
