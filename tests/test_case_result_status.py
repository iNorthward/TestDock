from __future__ import annotations

import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "test-platform"))
sys.path.insert(0, str(ROOT / "test-platform" / "common"))

from case_result_status import (  # noqa: E402
    format_run_summary, format_sub_status, normalize_case_result,
    run_exit_code, summarize_cases, summarize_run,
)


def case(*subs, ok=True):
    return {"id": "C-1", "ok": ok, "subs": list(subs)}


class CaseResultStatusTests(unittest.TestCase):
    def test_explicit_success_with_normal_assertions_passes(self):
        result = normalize_case_result(case({"ok": True, "detail": "字段正确"}))
        self.assertEqual((result["status"], result["ok"]), ("passed", True))

    def test_legacy_skip_reasons_do_not_count_as_pass(self):
        result = normalize_case_result(case({"ok": True, "detail": "库内无样本，跳过"}))
        self.assertEqual((result["status"], result["ok"]), ("skipped", None))
        self.assertEqual(result["subs"][0]["status"], "skipped")

    def test_infrastructure_gap_is_incomplete_even_if_legacy_runner_says_ok(self):
        result = normalize_case_result(case({"ok": True, "detail": "接口未部署(404)，跳过"}))
        self.assertEqual((result["status"], result["ok"]), ("incomplete", None))
        self.assertEqual(normalize_case_result(case({
            "ok": True, "detail": "接口不支持该筛选，但已验证拒绝码",
        }))["status"], "passed")

    def test_mixed_pass_and_skip_is_incomplete(self):
        result = normalize_case_result(case(
            {"ok": True, "detail": "字段正确"},
            {"ok": True, "detail": "库内无样本，跳过"},
        ))
        self.assertEqual(result["status"], "incomplete")

    def test_failure_and_top_level_contradiction_fail_closed(self):
        self.assertEqual(normalize_case_result(case({"ok": False, "detail": "bad"}))["status"], "failed")
        self.assertEqual(normalize_case_result(case({"ok": True, "detail": "ok"}, ok=False))["status"], "failed")
        self.assertEqual(normalize_case_result({
            "id": "x", "ok": True, "status": "failed",
            "subs": [{"ok": True, "detail": "asserted"}],
        })["status"], "failed")
        self.assertEqual(normalize_case_result({
            "id": "x", "ok": False, "status": "skipped", "subs": [],
        })["status"], "failed")
        self.assertEqual(normalize_case_result({
            "id": "x", "ok": True, "status": "skipped",
            "subs": [{"ok": True, "detail": "asserted"}],
        })["status"], "incomplete")

    def test_empty_or_malformed_assertions_fail_closed(self):
        self.assertEqual(normalize_case_result({"id": "x", "ok": True, "subs": []})["status"], "failed")
        self.assertEqual(normalize_case_result({"id": "x", "ok": True})["status"], "failed")

    def test_run_summary_distinguishes_all_statuses_and_skips_prevent_green(self):
        results = [
            case({"ok": True, "detail": "ok"}),
            case({"ok": False, "detail": "bad"}),
            case({"ok": True, "detail": "无样本，跳过"}),
            case({"ok": True, "detail": "未部署，跳过"}),
        ]
        summary = summarize_cases(results)
        self.assertEqual(summary, {
            "status": "failed", "passed": 1, "failed": 1,
            "skipped": 1, "incomplete": 1, "total": 4,
        })
        self.assertEqual(summarize_cases(results[2:3])["status"], "incomplete")

    def test_direct_runner_summary_and_console_statuses_share_the_same_counts(self):
        run = summarize_run("safe", "demo", [
            case({"ok": True, "detail": "校验"}),
            case({"ok": True, "detail": "未部署，跳过"}),
        ])
        self.assertEqual((run["status"], run["passed"], run["incomplete"]), ("incomplete", 1, 1))
        self.assertEqual(format_sub_status(run["cases"][1]["subs"][0]), "INCOMPLETE")
        self.assertIn("跳过 0，未完成 1", format_run_summary(run))
        self.assertEqual(run_exit_code(run), 2)


if __name__ == "__main__":
    unittest.main()
