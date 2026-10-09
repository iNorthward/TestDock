from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import candidate_inputs  # noqa: E402
import check_e_coverage_gap  # noqa: E402
import coverage_scorecard  # noqa: E402
import gen_d_series_candidates  # noqa: E402
import gen_e_series_candidates  # noqa: E402
import gen_f_series_candidates  # noqa: E402
import gen_r_series_candidates  # noqa: E402


GENERATORS = (
    gen_e_series_candidates,
    gen_f_series_candidates,
    gen_d_series_candidates,
    gen_r_series_candidates,
)


class CandidateInputValidationTests(unittest.TestCase):
    def test_openapi_snapshot_requires_at_least_one_http_operation(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "oas.json"
            for payload in (
                {"paths": {}},
                {"paths": {"/demo": {"x-note": {"value": 1}}}},
                {"paths": {"/demo": {"get": "not-an-operation"}}},
            ):
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.subTest(payload=payload), self.assertRaisesRegex(
                    ValueError, "没有有效 HTTP operation",
                ):
                    candidate_inputs.load_openapi_snapshot(path)

            path.write_text(
                json.dumps({"paths": {"/demo": {"GET": {"responses": {}}}}}),
                encoding="utf-8",
            )
            self.assertIn("/demo", candidate_inputs.load_openapi_snapshot(path)["paths"])

    def test_missing_coverage_is_optional_only_for_unfiltered_candidates(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "missing-coverage.json"
            self.assertEqual(candidate_inputs.load_coverage(path), {})
            with self.assertRaisesRegex(ValueError, "--covered-only 需要 coverage 文件"):
                candidate_inputs.load_coverage(path, required=True)

    def test_coverage_shape_and_boolean_contract_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "coverage.json"
            for payload in (
                {},
                {"paths": [None]},
                {"paths": [{"method": "GET", "path": "/demo"}]},
                {"paths": [{"method": "GET", "path": "/demo", "covered": "false"}]},
            ):
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.subTest(payload=payload), self.assertRaises(ValueError):
                    candidate_inputs.load_coverage(path)

            path.write_text(json.dumps({"paths": [
                {
                    "method": "get", "path": "/demo", "covered": True,
                    "caseCount": 1, "cases": ["E-01"],
                },
            ]}), encoding="utf-8")
            self.assertTrue(candidate_inputs.load_coverage(path)["GET /demo"]["covered"])

    def _assert_generator_rejects(
        self, module, *, oas_payload, covered_only, expected_text, coverage_payload=None,
    ):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            oas = root / "oas.json"
            coverage = root / "coverage.json"
            oas.write_text(json.dumps(oas_payload), encoding="utf-8")
            if covered_only and coverage_payload is None:
                coverage.unlink(missing_ok=True)
            else:
                payload = coverage_payload if coverage_payload is not None else {"paths": []}
                coverage.write_text(json.dumps(payload), encoding="utf-8")

            stderr = io.StringIO()
            argv = [module.__name__ + ".py", "--json"]
            if covered_only:
                argv.append("--covered-only")
            with patch.object(module, "LOCAL_OAS", oas), \
                    patch.object(module, "COVERAGE_JSON", coverage), \
                    patch.object(sys, "argv", argv), \
                    contextlib.redirect_stderr(stderr), \
                    self.assertRaises(SystemExit) as raised:
                module.main()

            self.assertEqual(raised.exception.code, 2)
            self.assertIn(expected_text, stderr.getvalue())

    def test_all_series_generators_reject_empty_openapi_instead_of_printing_empty_json(self):
        for module in GENERATORS:
            with self.subTest(module=module.__name__):
                self._assert_generator_rejects(
                    module,
                    oas_payload={"paths": {}},
                    covered_only=False,
                    expected_text="没有有效 HTTP operation",
                )

    def test_all_series_generators_require_coverage_for_covered_only(self):
        valid_oas = {"paths": {"/demo": {"get": {"responses": {}}}}}
        for module in GENERATORS:
            with self.subTest(module=module.__name__):
                self._assert_generator_rejects(
                    module,
                    oas_payload=valid_oas,
                    covered_only=True,
                    expected_text="--covered-only 需要 coverage 文件",
                )

    def test_all_series_generators_reject_truthy_strings_in_coverage(self):
        valid_oas = {"paths": {"/demo": {"get": {"responses": {}}}}}
        invalid_coverage = {"paths": [{
            "method": "GET", "path": "/demo", "covered": "false",
            "caseCount": 0, "cases": [],
        }]}
        for module in GENERATORS:
            with self.subTest(module=module.__name__):
                self._assert_generator_rejects(
                    module,
                    oas_payload=valid_oas,
                    covered_only=True,
                    coverage_payload=invalid_coverage,
                    expected_text="covered 必须是布尔值",
                )

    def test_scorecard_and_e_gap_commands_reject_empty_openapi(self):
        with tempfile.TemporaryDirectory() as td:
            oas = Path(td) / "oas.json"
            coverage = Path(td) / "coverage.json"
            oas.write_text(json.dumps({"paths": {}}), encoding="utf-8")
            coverage.write_text(json.dumps({"paths": []}), encoding="utf-8")
            stderr = io.StringIO()
            with patch.object(coverage_scorecard.gen_e, "LOCAL_OAS", oas), \
                    patch.object(coverage_scorecard, "pack_data_path", return_value=coverage), \
                    patch.object(coverage_scorecard.gen_e, "COVERAGE_JSON", coverage), \
                    patch.object(sys, "argv", ["coverage_scorecard.py", "--json"]), \
                    contextlib.redirect_stderr(stderr), \
                    self.assertRaises(SystemExit) as raised:
                coverage_scorecard.main()
            self.assertEqual(raised.exception.code, 2)
            self.assertIn("没有有效 HTTP operation", stderr.getvalue())

            stderr = io.StringIO()
            with patch.object(gen_e_series_candidates, "LOCAL_OAS", oas), \
                    patch.object(gen_e_series_candidates, "COVERAGE_JSON", coverage), \
                    patch.object(sys, "argv", ["check_e_coverage_gap.py", "--json"]), \
                    contextlib.redirect_stderr(stderr), \
                    self.assertRaises(SystemExit) as raised:
                check_e_coverage_gap.main()
            self.assertEqual(raised.exception.code, 2)
            self.assertIn("没有有效 HTTP operation", stderr.getvalue())

    def test_covered_only_scorecard_and_e_gap_require_coverage(self):
        with tempfile.TemporaryDirectory() as td:
            oas = Path(td) / "oas.json"
            coverage = Path(td) / "missing-coverage.json"
            oas.write_text(
                json.dumps({"paths": {"/demo": {"get": {"responses": {}}}}}),
                encoding="utf-8",
            )
            stderr = io.StringIO()
            with patch.object(coverage_scorecard.gen_e, "LOCAL_OAS", oas), \
                    patch.object(coverage_scorecard.gen_e, "COVERAGE_JSON", coverage), \
                    patch.object(sys, "argv", ["coverage_scorecard.py", "--covered-only", "--json"]), \
                    contextlib.redirect_stderr(stderr), \
                    self.assertRaises(SystemExit) as raised:
                coverage_scorecard.main()
            self.assertEqual(raised.exception.code, 2)
            self.assertIn("--covered-only 需要 coverage 文件", stderr.getvalue())

            stderr = io.StringIO()
            with patch.object(gen_e_series_candidates, "LOCAL_OAS", oas), \
                    patch.object(gen_e_series_candidates, "COVERAGE_JSON", coverage), \
                    patch.object(sys, "argv", ["check_e_coverage_gap.py", "--covered-only", "--json"]), \
                    contextlib.redirect_stderr(stderr), \
                    self.assertRaises(SystemExit) as raised:
                check_e_coverage_gap.main()
            self.assertEqual(raised.exception.code, 2)
            self.assertIn("--covered-only 需要 coverage 文件", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
