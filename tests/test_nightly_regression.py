import io
import sys
import types
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import nightly_regression as nightly


class NightlyRegressionTests(unittest.TestCase):
    @staticmethod
    def result(cases, *, suite=None, mode="safe"):
        summary = nightly.summarize_cases(cases)
        return {"mode": mode, "suite": suite, "cases": cases, **summary}

    def run_nightly(self, argv, *, suites, result):
        catalog = types.ModuleType("catalog")
        catalog.SUITES = [{"id": sid} for sid in suites]
        catalog.run_all = Mock(return_value=result)
        history = types.ModuleType("run_history")
        history.record_run = Mock(return_value=42)
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(sys.modules, {"catalog": catalog, "run_history": history}), \
             patch.object(sys, "argv", ["nightly_regression.py", *argv]), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                nightly.main()
                exit_code = 0
            except SystemExit as exc:
                exit_code = exc.code
        return exit_code, stdout.getvalue(), stderr.getvalue(), catalog.run_all, history.record_run

    def test_unknown_suite_fails_before_execution_or_history(self):
        code, _out, err, run_all, record_run = self.run_nightly(
            ["--suite", "typo", "--no-notify"], suites=["known"],
            result=self.result([], suite="typo"),
        )
        self.assertNotEqual(code, 0)
        self.assertIn("未知 suite", err)
        run_all.assert_not_called()
        record_run.assert_not_called()

    def test_live_only_suite_does_not_record_false_success(self):
        code, out, err, run_all, record_run = self.run_nightly(
            ["--suite", "bonus-flow", "--no-notify"], suites=["bonus-flow"],
            result=self.result([], suite="bonus-flow"),
        )
        self.assertNotEqual(code, 0)
        self.assertIn("没有 SAFE 用例", err)
        self.assertNotIn("全部通过", out)
        run_all.assert_called_once_with(mode="safe", suite="bonus-flow")
        record_run.assert_not_called()

    def test_empty_full_run_does_not_record_false_success(self):
        code, out, err, _run_all, record_run = self.run_nightly(
            ["--no-notify"], suites=[], result=self.result([]),
        )
        self.assertNotEqual(code, 0)
        self.assertIn("全量回归", err)
        self.assertIn("没有 SAFE 用例", err)
        self.assertNotIn("全部通过", out)
        record_run.assert_not_called()

    def test_nonempty_run_still_records_result(self):
        code, out, _err, _run_all, record_run = self.run_nightly(
            ["--suite", "known", "--no-notify"], suites=["known"],
            result=self.result([{
                "id": "K-R01", "ok": True, "status": "passed",
                "subs": [{"ok": True, "status": "passed", "detail": "valid"}],
            }], suite="known"),
        )
        self.assertEqual(code, 0)
        self.assertIn("SAFE 通过 1/1，失败 0，跳过 0，未完成 0", out)
        record_run.assert_called_once()
        self.assertEqual(record_run.call_args.kwargs["source"], "nightly")

    def test_elapsed_duration_uses_monotonic_clock(self):
        with patch.object(nightly.time, "monotonic", side_effect=[100.0, 102.5]):
            code, _out, _err, _run_all, record_run = self.run_nightly(
                ["--suite", "known", "--no-notify"], suites=["known"],
                result=self.result([{
                    "id": "K-R01", "ok": True, "status": "passed",
                    "subs": [{"ok": True, "status": "passed", "detail": "valid"}],
                }], suite="known"),
            )
        self.assertEqual(code, 0)
        self.assertEqual(record_run.call_args.kwargs["duration_ms"], 2500)

    def test_skip_or_incomplete_never_reports_green(self):
        for status in ("skipped", "incomplete"):
            code, out, _err, _run_all, record_run = self.run_nightly(
                ["--no-notify"], suites=["known"], result=self.result([{
                    "id": "K-R01", "ok": None, "status": status, "subs": [],
                }], suite=None),
            )
            self.assertEqual(code, 2)
            self.assertNotIn("全部通过", out)
            self.assertIn("SAFE 通过 0/1，失败 0，", out)
            record_run.assert_called_once()

    def test_summary_drift_fails_before_history_or_green_result(self):
        result = self.result([], suite=None)
        result["total"] = 1
        code, out, err, _run_all, record_run = self.run_nightly(
            ["--no-notify"], suites=["known"], result=result,
        )
        self.assertEqual(code, 2)
        self.assertIn("total 与 cases 不一致", err)
        self.assertNotIn("全部通过", out)
        record_run.assert_not_called()

    def test_wrong_execution_mode_or_suite_fails_before_history(self):
        for mode, suite in (("live", None), ("safe", "other")):
            with self.subTest(mode=mode, suite=suite):
                result = self.result([{
                    "id": "K-R01", "ok": True, "status": "passed",
                    "subs": [{"ok": True, "status": "passed", "detail": "valid"}],
                }], suite=suite, mode=mode)
                code, out, err, _run_all, record_run = self.run_nightly(
                    ["--no-notify"], suites=["known"], result=result,
                )
                self.assertEqual(code, 2)
                self.assertIn("mode/suite 与请求不一致", err)
                self.assertNotIn("全部通过", out)
                record_run.assert_not_called()

    def test_webhook_failures_do_not_print_secret_url_from_exception(self):
        secret_url = "https://hooks.example.invalid/webhook?key=do-not-log-this"
        output = io.StringIO()
        with patch.dict("os.environ", {
            "PLATFORM_WEBHOOK_WECHAT": secret_url,
            "PLATFORM_WEBHOOK_DINGTALK": secret_url,
        }, clear=True), \
             patch.object(nightly, "_post_json", side_effect=urllib.error.URLError(secret_url)), \
             redirect_stdout(output):
            sent = nightly.notify("nightly failure")

        self.assertEqual(sent, [])
        self.assertNotIn("do-not-log-this", output.getvalue())
        self.assertEqual(output.getvalue().count("URLError"), 2)

    def test_webhook_non_2xx_status_is_not_counted_as_success(self):
        output = io.StringIO()
        with patch.dict("os.environ", {
            "PLATFORM_WEBHOOK_WECHAT": "https://hooks.example.invalid/wechat",
        }, clear=True), \
             patch.object(nightly, "_post_json", return_value=503), \
             redirect_stdout(output):
            sent = nightly.notify("nightly failure")

        self.assertEqual(sent, [])
        self.assertIn("RuntimeError", output.getvalue())

    def test_nightly_reports_http_ack_without_claiming_delivery(self):
        failed_case = {
            "id": "K-R01", "ok": False, "status": "failed",
            "subs": [{"ok": False, "status": "failed", "detail": "assertion failed"}],
        }
        with patch.dict("os.environ", {
            "PLATFORM_WEBHOOK_WECHAT": "https://hooks.example.invalid/wechat",
        }, clear=True), \
             patch.object(nightly, "_post_json", return_value=200):
            code, output, _err, _run_all, _record_run = self.run_nightly(
                ["--suite", "known"], suites=["known"],
                result=self.result([failed_case], suite="known"),
            )

        self.assertEqual(code, 1)
        self.assertIn("已收到 HTTP 2xx 响应: wechat", output)
        self.assertIn("应用层回执未核验", output)
        self.assertNotIn("已推送", output)


if __name__ == "__main__":
    unittest.main()
