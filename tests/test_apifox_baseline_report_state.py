from __future__ import annotations

import io
import json
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fetch_apifox_oas  # noqa: E402
import run_apifox_inspection as inspection  # noqa: E402


class ApifoxBaselineReportStateTests(unittest.TestCase):
    def test_inspection_safe_duration_uses_monotonic_clock(self):
        catalog = types.ModuleType("catalog")
        catalog.run_all = Mock(return_value={
            "cases": [{
                "id": "K-R01", "status": "passed", "ok": True,
                "subs": [{"status": "passed", "ok": True, "detail": "pass"}],
            }],
            "passed": 1, "failed": 0, "skipped": 0, "incomplete": 0,
            "total": 1, "status": "passed",
        })
        catalog._CAT = {"K-R01": {"suite": "demo"}}
        history = types.ModuleType("run_history")
        history.record_run = Mock(return_value=17)

        with patch.dict(sys.modules, {"catalog": catalog, "run_history": history}), \
                patch.object(inspection.time, "monotonic", side_effect=[40.0, 42.5]):
            result = inspection.step_c_safe(False)

        self.assertEqual(result["durationMs"], 2500)
        self.assertEqual(history.record_run.call_args.kwargs["duration_ms"], 2500)

    @staticmethod
    def _audit_result():
        return {
            "covered_count": 1,
            "local_trade_paths": 1,
            "apifox_trade_paths": 1,
            "contract_diffs": [],
            "schema_diffs": [],
            "removed_from_apifox": [],
            "missing_in_apifox": [],
            "missing_in_local": [],
            "new_in_apifox": [],
            "stale_in_local": [],
        }

    def _run(self, temp_root, sync_side_effect):
        reports = temp_root / "reports"
        reports.mkdir()
        stdout = io.StringIO()
        baseline_state = []

        def render(_today, summary, _safe, _final):
            state = (summary.get("baselineAdvanced"), summary.get("baselinePending"))
            baseline_state.append(state)
            return f"baselineAdvanced={state[0]} baselinePending={state[1]}"

        summary = {
            "coveredCount": 1, "missingInLocal": 0, "missingInApifox": 0,
            "newInApifox": 0, "staleInLocal": 0, "removedFromApifox": 0,
            "removedWithCases": 0, "removedFromApifoxDetail": [],
            "contractDiffs": 0, "schemaDiffs": 0, "schemaDiffSample": [],
        }
        with patch.object(sys, "argv", ["run_apifox_inspection.py", "--skip-safe", "--json"]), \
                patch.object(inspection, "REPORT_DIR", reports), \
                patch.object(inspection, "UNDIGESTED", temp_root / "undigested.json"), \
                patch.object(inspection, "step_a_fetch", return_value=({"skipped": False}, {"downloadTime": "now"})), \
                patch.object(inspection, "step_b_audit", return_value={"audit": self._audit_result(), "summary": summary}), \
                patch.object(inspection, "step_c_safe", return_value={"skipped": True, "skippedReason": "cli"}), \
                patch.object(inspection, "step_d_finalize", return_value={
                    "docSyncOk": True, "coverage": {}, "catalogLint": {"ok": True, "errors": [], "warnings": []},
                }), \
                patch.object(inspection, "render_report", side_effect=render), \
                patch.object(inspection, "preserve_hand_section", side_effect=lambda _old, fresh, **_kwargs: fresh), \
                patch.object(fetch_apifox_oas, "sync_local_snapshot", side_effect=sync_side_effect), \
                redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as raised:
                inspection.main()

        report = next(reports.glob("*.md"))
        return raised.exception.code, json.loads(stdout.getvalue()), report.read_text(encoding="utf-8"), baseline_state

    def test_failed_snapshot_sync_never_leaves_report_claiming_baseline_advanced(self):
        with tempfile.TemporaryDirectory() as td:
            code, output, report, states = self._run(
                Path(td), OSError("snapshot write failed"),
            )

        self.assertEqual(code, 1)
        self.assertFalse(output["ok"])
        self.assertIn("baselineAdvanced=False", report)
        self.assertIn("baselinePending=True", report)
        self.assertEqual(states, [(False, True)])

    def test_successful_snapshot_sync_updates_report_to_advanced(self):
        with tempfile.TemporaryDirectory() as td:
            code, output, report, states = self._run(Path(td), None)

        self.assertEqual(code, 0)
        self.assertTrue(output["ok"])
        self.assertTrue(output["baselineAdvanced"])
        self.assertIn("baselineAdvanced=True", report)
        self.assertIn("baselinePending=False", report)
        self.assertEqual(states, [(False, True), (True, False)])


if __name__ == "__main__":
    unittest.main()
