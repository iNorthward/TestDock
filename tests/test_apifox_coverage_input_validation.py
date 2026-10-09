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

import gen_apifox_coverage_wiki as coverage_wiki  # noqa: E402


class ApifoxCoverageInputValidationTests(unittest.TestCase):
    def setUp(self):
        coverage_wiki._apifox_ops = None

    def tearDown(self):
        coverage_wiki._apifox_ops = None

    def test_missing_index_does_not_poison_cache_before_a_later_valid_load(self):
        with tempfile.TemporaryDirectory() as td:
            index = Path(td) / "apifox-index.json"
            with patch.object(coverage_wiki, "APIFOX_INDEX", index):
                with self.assertRaisesRegex(ValueError, "无法读取 OpenAPI 快照"):
                    coverage_wiki.load_apifox_summaries()
                self.assertIsNone(coverage_wiki._apifox_ops)

                index.write_text(json.dumps({
                    "paths": {"/demo": {"get": {"summary": "demo read"}}},
                }), encoding="utf-8")
                self.assertEqual(
                    coverage_wiki.load_apifox_summaries(),
                    {("GET", "/demo"): "demo read"},
                )

    def test_empty_index_fails_before_existing_coverage_outputs_are_touched(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            index = root / "apifox-index.json"
            index.write_text(json.dumps({"paths": {}}), encoding="utf-8")
            output_dir = root / "coverage-wiki"
            output_dir.mkdir()
            index_page = output_dir / "覆盖清单.md"
            module_page = output_dir / "demo.md"
            coverage_path = root / "coverage.json"
            index_page.write_text("prior index", encoding="utf-8")
            module_page.write_text("prior module page", encoding="utf-8")
            coverage_path.write_text("prior coverage", encoding="utf-8")
            stderr = io.StringIO()

            with patch.object(coverage_wiki, "APIFOX_INDEX", index), \
                    patch.object(coverage_wiki, "OUT", output_dir), \
                    patch.object(coverage_wiki, "COVERAGE_JSON", coverage_path), \
                    patch.object(coverage_wiki, "collect", side_effect=AssertionError("must preflight OAS first")), \
                    patch.object(sys, "argv", ["gen_apifox_coverage_wiki.py"]), \
                    contextlib.redirect_stderr(stderr), \
                    self.assertRaises(SystemExit) as raised:
                coverage_wiki.main()

            self.assertEqual(raised.exception.code, 2)
            self.assertIn("拒绝生成覆盖产物", stderr.getvalue())
            self.assertEqual(index_page.read_text(encoding="utf-8"), "prior index")
            self.assertEqual(module_page.read_text(encoding="utf-8"), "prior module page")
            self.assertEqual(coverage_path.read_text(encoding="utf-8"), "prior coverage")

    def test_malformed_path_item_does_not_publish_partial_summary_cache(self):
        with tempfile.TemporaryDirectory() as td:
            index = Path(td) / "apifox-index.json"
            index.write_text(json.dumps({
                "paths": {
                    "/valid": {"get": {}},
                    "/invalid": None,
                },
            }), encoding="utf-8")
            with patch.object(coverage_wiki, "APIFOX_INDEX", index):
                with self.assertRaisesRegex(ValueError, r"paths\['/invalid'\]"):
                    coverage_wiki.load_apifox_summaries()
                self.assertIsNone(coverage_wiki._apifox_ops)


if __name__ == "__main__":
    unittest.main()
