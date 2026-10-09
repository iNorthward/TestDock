from __future__ import annotations

import sys
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "test-platform"))

import catalog  # noqa: E402


class CatalogRunTest(unittest.TestCase):
    def test_regression_excluded_case_is_skipped_without_auth_or_execution(self):
        module = Mock()
        case = {
            "id": "excluded", "suite": "selected", "mode": "safe",
            "regressionExcluded": True,
            "regressionNote": "本轮排除：环境暂无模拟",
        }
        with patch.object(catalog, "_ALL", [case]), \
             patch.object(catalog, "_EXEC", {"excluded": module}), \
             patch.object(catalog, "MODULES", [module]):
            result = catalog.run_all("safe", "selected")
            direct = catalog.exec_case("excluded")
        module.ensure_token.assert_not_called()
        module.ensure_tokens.assert_not_called()
        module.exec_case.assert_not_called()
        self.assertEqual((result["status"], result["skipped"], result["total"]),
                         ("incomplete", 1, 1))
        self.assertEqual(result["cases"][0]["status"], "skipped")
        self.assertEqual(direct["status"], "skipped")

    def test_run_prepares_only_selected_safe_modules(self):
        selected_module = Mock()
        unrelated_module = Mock()
        cases = [
            {"id": "a", "suite": "selected", "mode": "safe"},
            {"id": "b", "suite": "selected", "mode": "live"},
            {"id": "c", "suite": "other", "mode": "safe"},
        ]
        with patch.object(catalog, "_ALL", cases), \
             patch.object(catalog, "_EXEC", {"a": selected_module, "b": unrelated_module, "c": unrelated_module}), \
             patch.object(catalog, "MODULES", [selected_module, unrelated_module]), \
             patch.object(catalog, "exec_case", return_value={
                 "id": "a", "ok": True, "subs": [{"ok": True, "detail": "asserted"}],
             }) as execute:
            result = catalog.run_all("safe", "selected")
        selected_module.ensure_token.assert_called_once_with()
        unrelated_module.ensure_token.assert_not_called()
        execute.assert_called_once_with("a", execution_options={})
        self.assertEqual((result["passed"], result["total"]), (1, 1))

    def test_pack_hooks_receive_namespaced_execution_options(self):
        module = Mock()
        module.exec_case.return_value = {
            "id": "a", "ok": True, "subs": [{"ok": True, "detail": "asserted"}],
        }
        extension = Mock(spec=["before_suite", "before_case"])
        case = {"id": "a", "suite": "selected", "mode": "safe"}
        options = {"sample-pack": {"fixture": "offline"}}
        with patch.object(catalog, "CATALOG_EXTENSIONS", [("sample-pack", extension)]), \
             patch.object(catalog, "_ALL", [case]), \
             patch.object(catalog, "_EXEC", {"a": module}), \
             patch.object(catalog, "MODULES", [module]):
            result = catalog.run_all("safe", "selected", execution_options=options)
        extension.before_suite.assert_called_once_with("selected", options)
        extension.before_case.assert_called_once_with(case, module, options)
        self.assertEqual((result["passed"], result["total"]), (1, 1))

    def test_live_write_gate_runs_before_module_authentication(self):
        module = Mock()
        cases = [{
            "id": "live-write", "suite": "selected", "mode": "live",
            "requires_unscoped_write_opt_in": True,
        }]
        with patch.dict("os.environ", {}, clear=True), \
             patch.object(catalog, "_ALL", cases), \
             patch.object(catalog, "_EXEC", {"live-write": module}), \
             patch.object(catalog, "MODULES", [module]):
            result = catalog.run_all("live", "selected")
        module.ensure_token.assert_not_called()
        module.ensure_tokens.assert_not_called()
        module.exec_case.assert_not_called()
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["incomplete"], 1)

    def test_direct_live_write_gate_runs_before_order_configuration(self):
        module = Mock()
        case = {
            "id": "live-write", "suite": "order", "mode": "live",
            "requires_unscoped_write_opt_in": True,
        }
        with patch.dict("os.environ", {}, clear=True), \
             patch.object(catalog, "_ALL", [case]), \
             patch.object(catalog, "_EXEC", {"live-write": module}):
            result = catalog.exec_case(
                "live-write", execution_options={"sample_project": {"order": {"username": "test"}}},
            )
        module.apply_order_options.assert_not_called()
        module.exec_case.assert_not_called()
        self.assertEqual(result["status"], "incomplete")

    def test_run_all_authenticates_only_after_exact_write_opt_in(self):
        module = Mock()
        module.exec_case.return_value = {
            "id": "live-write", "ok": True, "subs": [{"ok": True, "detail": "mocked"}],
        }
        case = {
            "id": "live-write", "suite": "selected", "mode": "live",
            "requires_unscoped_write_opt_in": True,
        }
        with patch.dict("os.environ", {"PLATFORM_ENABLE_UNSCOPED_WRITE_CASE": "live-write"}, clear=True), \
             patch.object(catalog, "_ALL", [case]), \
             patch.object(catalog, "_EXEC", {"live-write": module}), \
             patch.object(catalog, "MODULES", [module]):
            result = catalog.run_all("live", "selected")
        module.ensure_token.assert_called_once_with()
        module.exec_case.assert_called_once_with("live-write")
        self.assertEqual(result["status"], "passed")

    def test_scoped_write_gate_runs_before_module_authentication(self):
        module = Mock()
        case = {
            "id": "scoped-write", "suite": "selected", "mode": "live",
            "requires_write_opt_in": True, "write_risk_reason": "async persistence",
        }
        with patch.dict("os.environ", {}, clear=True), \
             patch.object(catalog, "_ALL", [case]), \
             patch.object(catalog, "_EXEC", {"scoped-write": module}), \
             patch.object(catalog, "MODULES", [module]):
            result = catalog.run_all("live", "selected")
        module.ensure_token.assert_not_called()
        module.ensure_tokens.assert_not_called()
        module.exec_case.assert_not_called()
        self.assertEqual((result["status"], result["incomplete"]), ("incomplete", 1))

    def test_direct_scoped_write_gate_accepts_only_exact_opt_in(self):
        module = Mock()
        module.exec_case.return_value = {
            "id": "scoped-write", "ok": True, "subs": [{"ok": True, "detail": "mocked"}],
        }
        case = {
            "id": "scoped-write", "suite": "selected", "mode": "live",
            "requires_write_opt_in": True,
        }
        with patch.dict("os.environ", {"PLATFORM_ENABLE_WRITE_CASE": "another-case"}, clear=True), \
             patch.object(catalog, "_ALL", [case]), \
             patch.object(catalog, "_EXEC", {"scoped-write": module}):
            blocked = catalog.exec_case("scoped-write")
        self.assertEqual(blocked["status"], "incomplete")
        module.exec_case.assert_not_called()

        with patch.dict("os.environ", {"PLATFORM_ENABLE_WRITE_CASE": "scoped-write"}, clear=True), \
             patch.object(catalog, "_ALL", [case]), \
             patch.object(catalog, "_EXEC", {"scoped-write": module}):
            allowed = catalog.exec_case("scoped-write")
        self.assertEqual(allowed["status"], "passed")
        module.exec_case.assert_called_once_with("scoped-write")

    def test_case_exception_becomes_a_failed_result(self):
        module = Mock()
        module.exec_case.side_effect = RuntimeError("database unavailable")
        case = {"id": "a", "suite": "selected", "mode": "safe"}
        with patch.object(catalog, "_ALL", [case]), \
             patch.object(catalog, "_EXEC", {"a": module}), \
             patch.object(catalog.time, "monotonic", side_effect=[10.0, 10.25]):
            result = catalog.exec_case("a")
        self.assertEqual(result["id"], "a")
        self.assertIn("RuntimeError: database unavailable", result["error"])
        self.assertEqual(result["durationMs"], 250)

    def test_success_duration_uses_monotonic_clock(self):
        module = Mock()
        module.exec_case.return_value = {
            "id": "a", "ok": True, "subs": [{"ok": True, "detail": "pass"}],
        }
        case = {"id": "a", "suite": "selected", "mode": "safe"}
        with patch.object(catalog, "_ALL", [case]), \
             patch.object(catalog, "_EXEC", {"a": module}), \
             patch.object(catalog.time, "monotonic", side_effect=[2.0, 2.125]):
            result = catalog.exec_case("a")
        self.assertEqual(result["durationMs"], 125)

    def test_legacy_skip_result_is_normalized_and_excluded_from_passed(self):
        module = Mock()
        module.exec_case.return_value = {
            "id": "a", "ok": True,
            "subs": [{"label": "sample", "ok": True, "detail": "库内无样本，跳过"}],
        }
        case = {"id": "a", "suite": "selected", "mode": "safe"}
        with patch.object(catalog, "_ALL", [case]), \
             patch.object(catalog, "_EXEC", {"a": module}):
            result = catalog.run_all("safe", "selected")
        self.assertEqual((result["status"], result["passed"], result["skipped"], result["total"]),
                         ("incomplete", 0, 1, 1))
        self.assertEqual(result["cases"][0]["status"], "skipped")


if __name__ == "__main__":
    unittest.main()
